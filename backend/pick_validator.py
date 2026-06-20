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
        model_wp = p.get("model_win_probability")
        if model_wp is not None and bucket_index:
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
        wp = p.get("win_probability")
        ip = p.get("implied_probability")
        if wp is not None and ip is not None:
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
            factors_pct = p.get("factors") or {}
            if factors_pct:
                factors = {k: v / 100.0 for k, v in factors_pct.items()}
                target_lock, _ = compute_lock_score(
                    factors, win_prob=wp, pick=p,
                )
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
                    updates["lock_score"] = target_lock
                    p["lock_score"] = target_lock
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
