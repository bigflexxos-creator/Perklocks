"""Self-healing pick validator.

Runs as part of the settlement loop. Goal: catch and auto-fix any drift in
the math behind picks WITHOUT burning Odds-API credits (purely DB-side).

Checks performed on every settled-or-pending pick:

  1. **Edge math consistency** — `edge_percent` must equal
     `win_probability - implied_probability` (within 0.05 rounding). If not,
     recompute and persist.
  2. **Implied probability sanity** — must match `book_odds`. If `book_odds`
     is known, recompute `implied_probability` from American odds.
  3. **Win-prob stacking** — if `model_win_probability` exists but the
     learning delta has been applied more than once, restore WP from the
     model baseline + the CURRENT bucket weight (single application).
  4. **Lock-score anchor** — `lock_score` must be the right band for the
     current `win_probability` (e.g. WP 50% should never carry lock 85+
     after the formula fix).

Returns counts so the settlement log can surface drift detection.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("lockscore.validator")


def _american_to_implied_pct(american: float) -> float:
    if not american:
        return 0.0
    a = float(american)
    if a > 0:
        return 100.0 / (a + 100.0) * 100
    return abs(a) / (abs(a) + 100.0) * 100


async def validate_and_heal(db) -> dict:
    """Walk today's pick population and auto-fix any math drift. Cheap (one
    cursor + a handful of mutations) and idempotent."""
    counts = {
        "scanned": 0, "fixed_edge": 0, "fixed_implied": 0,
        "fixed_stack": 0, "fixed_lock": 0, "deep_dive_refreshed": 0,
    }
    from datetime import datetime, timezone
    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cursor = db.picks.find(
        {"pick_date": today_iso},
        {"_id": 0},
    )
    picks = await cursor.to_list(length=2000)
    counts["scanned"] = len(picks)
    if not picks:
        return counts

    # Cache learning weights once.
    try:
        from learning_engine import _load_weights
        from analytics import _market_label
        weights_doc = await _load_weights(db)
        bucket_index: dict[tuple[str, str], dict] = {}
        cal_index: dict[str, dict] = {}
        if weights_doc:
            for b in weights_doc.get("buckets", []):
                if b.get("active"):
                    bucket_index[(b["sport"], b["market_label"])] = b
            for c in weights_doc.get("calibration", []):
                if c.get("active"):
                    cal_index[c["band"]] = c
    except Exception:
        weights_doc = None
        bucket_index = {}
        cal_index = {}
        _market_label = lambda x: x  # noqa: E731

    try:
        from deep_dive import deep_dive
    except Exception:
        deep_dive = None

    try:
        from sports_engine import compute_lock_score
    except Exception:
        compute_lock_score = None

    for p in picks:
        updates: dict = {}

        # 1) Implied probability from book_odds.
        book = p.get("book_odds")
        if book:
            recomputed_implied = round(_american_to_implied_pct(book), 1)
            stored_implied = p.get("implied_probability")
            if stored_implied is None or abs(stored_implied - recomputed_implied) > 0.1:
                updates["implied_probability"] = recomputed_implied
                p["implied_probability"] = recomputed_implied
                counts["fixed_implied"] += 1

        # 2) Reverse-stack: if `model_win_probability` exists, the original
        # model output is known — recompute the LEARNING-adjusted win_prob
        # from baseline + current bucket weight (single application).
        #
        # CARVE-OUT (added 2026-06-23 — user bug "Fix edge on tennis bets
        # still not seeing none on board"): tennis_extra picks are
        # book-anchored — their `win_probability` is intentionally set
        # equal to the book's implied probability because we have no
        # independent model for those scraped Eastbourne / Mallorca /
        # Bad Homburg matches. Running the reverse-stack on them applies
        # the Tennis ML bucket's negative learning weight on top of an
        # already-honest probability and produces a phantom -9% to -12%
        # edge that looks broken on the board. Skip the stack for them
        # and keep edge ≈ 0 (which is the truth for book-anchored picks).
        source = (p.get("source") or "").lower()
        is_book_anchored = source in ("tennis_extra", "tennis_extra_model")
        model_wp = p.get("model_win_probability")
        if model_wp is not None and bucket_index and not is_book_anchored:
            sport = p.get("sport") or ""
            label = _market_label(p.get("market"))
            bucket = bucket_index.get((sport, label))
            cal = None
            if cal_index:
                from analytics import confidence_bucket
                band = confidence_bucket(p.get("lock_score"))
                cal = cal_index.get(band)
            delta = (bucket.get("weight", 0) if bucket else 0.0) + (cal.get("adjustment", 0) if cal else 0.0)
            target_wp = max(1.0, min(99.0, round(model_wp + delta * 100, 1)))
            current_wp = p.get("win_probability") or 0
            if abs(current_wp - target_wp) > 0.5:
                updates["win_probability"] = target_wp
                p["win_probability"] = target_wp
                counts["fixed_stack"] += 1

        # 3) Edge math = WP − implied. Recompute if drifted.
        #
        # For tennis_extra (book-anchored) picks, force edge = 0 instead
        # of computing it from wp - implied. Otherwise tiny float drift
        # between win_probability and implied_probability shows as a
        # bogus -0.2% to -1% edge on the board. Honest edge for a
        # book-anchored pick is 0.
        wp = p.get("win_probability")
        ip = p.get("implied_probability")
        if is_book_anchored and wp is not None and ip is not None:
            stored_edge = p.get("edge_percent")
            if stored_edge is None or abs(stored_edge - 0.0) > 0.05:
                updates["edge_percent"] = 0.0
                p["edge_percent"] = 0.0
                counts["fixed_edge"] += 1
        elif wp is not None and ip is not None:
            expected_edge = round(wp - ip, 2)
            stored_edge = p.get("edge_percent")
            if stored_edge is None or abs(stored_edge - expected_edge) > 0.05:
                updates["edge_percent"] = expected_edge
                p["edge_percent"] = expected_edge
                counts["fixed_edge"] += 1

        # 4) Lock-score anchor — re-derive from current factors + WP.
        # CARVE-OUT: skip tennis picks — `tennis_engine.py` v2 already
        # produces calibrated lock scores via its own component-based
        # formula (`edge_v2_score`). Re-running the generic formula here
        # systematically LOWERS tennis locks (e.g. Sabalenka 99 → 83)
        # because tennis spreads have modest model-edges relative to other
        # sports. Trust the sport-specific engine.
        # FIX (drift bug): we MUST pass `pick=p` so the validator hits the
        # v3 six-component formula that was used at generation time —
        # otherwise it falls into the legacy WP-only fallback and writes
        # back a different value every cycle, causing the user's complaint
        # that "Mbappé and other goalscorer locks keep changing".
        # Also widen the diff tolerance from 1.0 → 4.0 so micro-jitter in
        # factor recomputation doesn't trigger pointless rewrites.
        if compute_lock_score and wp is not None and (p.get("sport") or "") != "Tennis":
            # ── ANCHOR CARVE-OUTS ─────────────────────────────────────────
            # Two pick categories whose lock_score is intentionally NOT a
            # direct function of factors+win_prob — re-running the generic
            # formula here would systematically over-write the anchor that
            # gave the pick its current lock value:
            #
            #   1. ELITE PLAYERS (Salah, Mbappé, Haaland, Messi, Kane, Judge,
            #      Sinner, etc.): elite_players.apply_elite_boost sets a 95+
            #      floor based on REPUTATION + CAREER HISTORY, independent of
            #      sim WP. Without this carve-out the validator demoted Salah
            #      95 → 51 on a goalscorer prop where sim WP was 37%
            #      (2026-06-26 user report).
            #
            #   2. SIM-ANCHORED picks: brain.sim_runner.apply_simulations sets
            #      lock_score from the 20K-run Monte Carlo consensus AND
            #      pins lock_score_raw = lock_score with multiplier 1.0.
            #      Validator re-derivation would undo this consensus on the
            #      very next cycle.
            #
            # For both categories the pre-anchored lock is trusted as the
            # canonical value — we skip the lock-anchor recompute but still
            # run steps 5 (grade/confidence reconcile) and 6 (deep-dive).
            _skip_lock_anchor = bool(
                p.get("elite_player")
                or p.get("lock_anchored_to_sim")
                or p.get("is_model_only")
                or p.get("is_synthetic_scorer")
                or (p.get("source") or "").startswith("sportdb_scorer")
            )
            factors_pct = {} if _skip_lock_anchor else (p.get("factors") or {})
            if factors_pct:
                # Coerce factor values to float — legacy DB rows occasionally
                # have stringified percentages (e.g. "78") that break the
                # division. Defensively cast + skip non-numeric values so the
                # validator never crashes on schema drift.
                factors = {}
                for k, v in factors_pct.items():
                    try:
                        factors[k] = float(v) / 100.0
                    except (TypeError, ValueError):
                        continue
                if not factors:
                    continue
                target_lock, _ = compute_lock_score(
                    factors, win_prob=wp, pick=p,
                )
                # ── Universal Evidence System governor ──
                # Apply the FULL govern_pick pass — not just the lock
                # multiplier — so lock_score_raw, lock_score_v2,
                # lock_score_v2_raw, and evidence_breakdown all stay
                # in lockstep with lock_score. The earlier approach
                # only multiplied target_lock and left lock_score_raw
                # frozen at generation-time, which caused the
                # validator to fight itself and push lock_score above
                # lock_score_raw across cycles (89 of 376 picks
                # corrupt as of iter-49).
                #
                # NOTE: governance is applied AT THE END of this block,
                # AFTER drift-capping and CLV demotion. Reordering: those
                # later steps mutate target_lock (raise it to enforce the
                # 10-pt drift cap, lower it via CLV) — if we governed
                # BEFORE those mutations, target_lock would diverge from
                # lock_score_raw and the audit would show
                # lock_score > lock_score_raw (the iter-49 canary bug).
                _gov_lock_raw = None
                _gov_lock_v2 = None
                _gov_lock_v2_raw = None
                _gov_evidence = None
                _gov_breakdown = None
                current_lock = p.get("lock_score") or 0
                # The edge_percent is recomputed earlier in this same
                # cycle — its drift cascades into lock_score even when
                # the underlying win_probability is unchanged. So we
                # widen the tolerance further to absorb that cascade
                # plus normal factor jitter. The user complaint that
                # "Mbappé locks keep changing" is solved when the
                # validator only intervenes on REAL drift (>6 points),
                # which corresponds to a full lock-band shift (85-89
                # Strong → 90-94 Premium etc.) rather than cosmetic
                # micro-drift between 86.5 and 87.2.
                if abs(current_lock - target_lock) > 6.0:
                    # ─── Delta guard (recommendation #7) ───
                    # Cap downward drift at 10 points per validator run. The
                    # validator was demoting Yordan Alvarez 95 → 56 in one
                    # cycle, hiding good picks. Real model drift never moves
                    # that fast; only odds-staleness or factor jitter does.
                    # Upward moves are unrestricted (we always want to surface
                    # newly-discovered edges immediately).
                    if target_lock < current_lock - 10.0:
                        target_lock = current_lock - 10.0
                    # ─── CLV demotion (recommendation #2) ───
                    # If closing line moved AGAINST us (book got sharper),
                    # apply an additional 2-5 point lock penalty so the pick
                    # naturally drops off the feed. CLV < -1 unit ≈ market
                    # disagreed strongly. CLV > 0 = market agreed (free EV).
                    try:
                        clv = float(p.get("clv_value") or 0)
                    except Exception:
                        clv = 0.0
                    if clv < -1.5:
                        target_lock = max(60.0, target_lock - 5.0)
                    elif clv < -0.5:
                        target_lock = max(60.0, target_lock - 2.0)

                    # ── Universal Evidence System — applied LAST so the
                    # drift cap + CLV demotion above have already settled
                    # target_lock into its FINAL raw value. govern_pick
                    # records this as lock_score_raw and writes the
                    # governed value into lock_score. Without this
                    # ordering, target_lock could be raised by the drift
                    # cap AFTER governance and end up > lock_score_raw
                    # (the iter-49 canary corruption).
                    try:
                        from evidence_engine import (
                            build_features_from_pick, govern_pick,
                        )
                        p_copy = dict(p)
                        p_copy["lock_score"] = target_lock
                        p_copy.pop("lock_score_raw", None)
                        p_copy.pop("lock_score_v2_raw", None)
                        govern_pick(p_copy, build_features_from_pick(p_copy))
                        target_lock = p_copy.get("lock_score", target_lock)
                        _gov_lock_raw     = p_copy.get("lock_score_raw")
                        _gov_lock_v2      = p_copy.get("lock_score_v2")
                        _gov_lock_v2_raw  = p_copy.get("lock_score_v2_raw")
                        _gov_evidence     = p_copy.get("evidence_score")
                        _gov_breakdown    = p_copy.get("evidence_breakdown")
                    except Exception:
                        pass

                    updates["lock_score"] = target_lock
                    p["lock_score"] = target_lock
                    # ── Persist the full governance payload alongside
                    # the new lock_score so the audit trail (raw,
                    # multiplier, breakdown, evidence_score, V2 raw)
                    # stays in lockstep across every validator pass.
                    if _gov_lock_raw is not None:
                        updates["lock_score_raw"] = _gov_lock_raw
                        p["lock_score_raw"] = _gov_lock_raw
                    if _gov_lock_v2 is not None:
                        updates["lock_score_v2"] = _gov_lock_v2
                        p["lock_score_v2"] = _gov_lock_v2
                    if _gov_lock_v2_raw is not None:
                        updates["lock_score_v2_raw"] = _gov_lock_v2_raw
                        p["lock_score_v2_raw"] = _gov_lock_v2_raw
                    if _gov_evidence is not None:
                        updates["evidence_score"] = _gov_evidence
                        p["evidence_score"] = _gov_evidence
                    if _gov_breakdown:
                        updates["evidence_breakdown"] = _gov_breakdown
                        p["evidence_breakdown"] = _gov_breakdown
                    counts["fixed_lock"] += 1
                    # ── CRITICAL: re-sync grade + confidence to the new
                    # lock_score so the UI badge ("Elite Lock", "Strong
                    # Lock", …) doesn't lie about a pick whose score has
                    # been demoted to 71 while still flashing the gold
                    # ELITE LOCK chip. Same for confidence rail. This is
                    # the V1/V2 inconsistency the user flagged.
                    try:
                        from sports_engine import _grade, _confidence
                        new_grade = _grade(target_lock)
                        new_conf  = _confidence(target_lock)
                        if new_grade != p.get("grade"):
                            updates["grade"] = new_grade
                            p["grade"] = new_grade
                        if new_conf != p.get("confidence"):
                            updates["confidence"] = new_conf
                            p["confidence"] = new_conf
                    except Exception:
                        # If sports_engine isn't importable for some
                        # reason, leaving grade alone is better than
                        # raising — pick still has SOME label.
                        pass

                # ── Unconditional governance coherence pass ──
                # Run regardless of whether the drift cap fired. Without
                # this, corruption introduced by ANY upstream mutation
                # (V2 shadow tagging, learning-engine apply_learning,
                # CLV grade reshuffles) leaks into the DB and we end up
                # with lock_score > lock_score_raw (iter-49 canary bug).
                # govern_pick is idempotent — when the pick is already
                # coherent, this is a no-op write.
                try:
                    from evidence_engine import (
                        build_features_from_pick as _bf,
                        govern_pick as _gp,
                    )
                    cur_lock = p.get("lock_score")
                    cur_raw  = p.get("lock_score_raw")
                    needs_regovern = (
                        cur_lock is not None and cur_raw is not None and
                        float(cur_lock) > float(cur_raw) + 0.5
                    )
                    if needs_regovern:
                        # Strip stale governance so the recompute treats
                        # the CURRENT lock_score (which equals
                        # post-drift-cap value) as the new raw.
                        gp_copy = dict(p)
                        gp_copy.pop("lock_score_raw", None)
                        gp_copy.pop("lock_score_v2_raw", None)
                        _gp(gp_copy, _bf(gp_copy))
                        # Epsilon guard — only count this as a true
                        # "fixed_lock" mutation if the lock score
                        # actually changed by >= 0.5. Otherwise we
                        # churn the DB every cycle and falsely inflate
                        # the fixed_lock metric (iter-50 secondary).
                        new_lock = gp_copy.get("lock_score")
                        if new_lock is None or abs(float(new_lock) - float(cur_lock or 0)) < 0.5:
                            # No real change — skip the write entirely.
                            raise StopIteration("no-op")
                        for fld in (
                            "lock_score", "lock_score_raw",
                            "lock_score_v2", "lock_score_v2_raw",
                            "evidence_score", "evidence_breakdown",
                            "key_insights",
                        ):
                            v = gp_copy.get(fld)
                            if v is not None:
                                updates[fld] = v
                                p[fld] = v
                except StopIteration:
                    pass
                except Exception:
                    pass

        # 5) Grade + Confidence consistency — ALWAYS reconcile the badge
        # labels with the CURRENT lock_score, regardless of whether the
        # validator touched lock_score this cycle. The user-visible bug
        # ("ELITE LOCK chip on a 71 lock") happens when a previous
        # cycle demoted lock_score but didn't update the stored grade.
        # This unconditional pass guarantees grade and lock_score always
        # tell the same story to the user.
        try:
            from sports_engine import _grade as _g_fn, _confidence as _c_fn
            ls = p.get("lock_score")
            if ls is not None:
                gx = _g_fn(float(ls))
                cx = _c_fn(float(ls))
                if gx != p.get("grade"):
                    updates["grade"] = gx
                    p["grade"] = gx
                    counts.setdefault("fixed_grade", 0)
                    counts["fixed_grade"] += 1
                if cx != p.get("confidence"):
                    updates["confidence"] = cx
                    p["confidence"] = cx
        except Exception:
            pass

        # 6) If any math changed, refresh deep-dive scores too.
        if updates and deep_dive:
            await deep_dive(db, p)
            updates["edge_score"] = p.get("edge_score")
            updates["confidence_score"] = p.get("confidence_score")
            updates["risk_score"] = p.get("risk_score")
            updates["top_reasons"] = p.get("top_reasons")
            updates["no_bet"] = p.get("no_bet", False)
            counts["deep_dive_refreshed"] += 1

        if updates:
            await db.picks.update_one({"id": p["id"]}, {"$set": updates})

    if any(v > 0 for k, v in counts.items() if k.startswith("fixed") or k == "deep_dive_refreshed"):
        logger.info("Self-heal validator: %s", counts)
    return counts
