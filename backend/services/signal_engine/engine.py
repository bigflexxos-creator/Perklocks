"""Signal Engine orchestrator — Phase A.

Runs the six universal calculators, combines them into a 0-100 Signal
Score, rewrites the signal-driven "Why This Pick" bullets, and (bulk
path) persists the block back to `db.picks` so the ranking engine can
read `signal_score` on subsequent queries without re-decoration.

Score model:
    score = clamp(50 + Σ component points, 0, 100)
    component budgets: form ±12 · matchup ±8 · volume ±7
                       · injury ±8 · market ±7 · value ±8

Freshness: the market signal moves with live odds, so a stored block
older than 30 minutes is recomputed on the next read. Recompute is
cheap — the only I/O is `get_player_form` which is TTL-cached in
memory.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from pymongo import UpdateOne

from .calculators import (
    form_signal, injury_signal, market_signal, matchup_signal,
    mlb_deep_signal, soccer_deep_signal, tennis_deep_signal,
    value_signal, volume_signal,
)
from .mlb_deep import enrich_mlb_pick
from .soccer_deep import enrich_soccer_pick
from .tennis_deep import enrich_tennis_pick
from .rationale import build_why, signal_breakdown_line

logger = logging.getLogger("lockscore.services.signal_engine")

SIGNAL_VERSION = 5  # bumped for Phase 2 (adds park-hand, pitch-mix, rolling xG, context, first-set)
_REFRESH_SECS = 1800  # 30 min — market signal tracks live line movement


def _grade(score: int) -> str:
    if score >= 80:
        return "Elite"
    if score >= 65:
        return "Strong"
    if score >= 50:
        return "Moderate"
    if score >= 35:
        return "Weak"
    return "Fade"


def _is_fresh(block: dict) -> bool:
    if block.get("version") != SIGNAL_VERSION:
        return False
    ts = block.get("computed_at")
    if not ts:
        return False
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds() < _REFRESH_SECS
    except Exception:
        return False


async def compute_signals(db, pick: dict) -> dict:
    """Mutate `pick` in place: adds `signal_engine` + `signal_score` and
    injects the top signal bullets into `pick_rationale.evidence`.
    No-op when a fresh same-version block already exists."""
    if not pick:
        return pick
    existing = pick.get("signal_engine")
    if isinstance(existing, dict) and _is_fresh(existing):
        pick.setdefault("signal_score", existing.get("score"))
        return pick

    # Phase B.1/B.4/B.5 enrichment (idempotent). Only mutates picks
    # whose sport matches; every other sport is a fast no-op.
    try:
        enrich_mlb_pick(pick)
    except Exception as e:
        logger.debug("mlb_deep enrich failed for pick %s: %s", pick.get("id"), e)
    try:
        enrich_soccer_pick(pick)
    except Exception as e:
        logger.debug("soccer_deep enrich failed for pick %s: %s", pick.get("id"), e)
    try:
        await enrich_tennis_pick(db, pick)
    except Exception as e:
        logger.debug("tennis_deep enrich failed for pick %s: %s", pick.get("id"), e)

    # ── Phase-1 external-data enrichments (2026-07-19) ────────────────
    # User feedback: "Are we missing any data or anything to make picks
    # and signal better?". These three enrichments were the highest-
    # impact gaps:
    #   • Weather (OpenWeather) — wind/temp/rain shifts MLB HR + totals
    #   • MLB umpire K-tendency — a wide-zone plate ump adds 0.6 K/starter
    #   • Confirmed lineup — kills the "bench player" false-positive
    # Each `enrich_*` is idempotent and no-ops on missing keys / network
    # errors, so a failure here never breaks the signal computation.
    try:
        from services.enrichment.weather import enrich_pick_with_weather
        await enrich_pick_with_weather(pick)
    except Exception as e:
        logger.debug("weather enrich failed for pick %s: %s", pick.get("id"), e)
    try:
        from services.enrichment.umpires import enrich_pick_with_umpire
        await enrich_pick_with_umpire(pick)
    except Exception as e:
        logger.debug("umpire enrich failed for pick %s: %s", pick.get("id"), e)
    try:
        from services.enrichment.lineups import enrich_pick_with_lineup
        await enrich_pick_with_lineup(pick)
    except Exception as e:
        logger.debug("lineup enrich failed for pick %s: %s", pick.get("id"), e)

    # ── Phase-2 sport-specific depth (2026-07-19) ────────────────────
    # MLB: park HR by batter hand, pitch-mix vs batter tendency.
    # Soccer: rolling 10-match team xG, set-piece / manager / pressure context.
    # Tennis: first-set return-points-won estimate.
    # All are pure additive enrichers \u2014 blocking-free and no-op on missing
    # data. Wrap each in a defensive try so a single API/DB miss can't
    # break signal computation for the entire slate.
    try:
        from services.enrichment.mlb_park_hand import enrich_pick_with_hand_factor
        enrich_pick_with_hand_factor(pick)
    except Exception as e:
        logger.debug("mlb_park_hand enrich failed for pick %s: %s", pick.get("id"), e)
    try:
        from services.enrichment.mlb_pitch_mix import enrich_pick_with_pitch_mix
        enrich_pick_with_pitch_mix(pick)
    except Exception as e:
        logger.debug("mlb_pitch_mix enrich failed for pick %s: %s", pick.get("id"), e)
    try:
        from services.enrichment.soccer_rolling_xg import enrich_pick_with_rolling_xg
        await enrich_pick_with_rolling_xg(db, pick)
    except Exception as e:
        logger.debug("soccer_rolling_xg enrich failed for pick %s: %s", pick.get("id"), e)
    try:
        from services.enrichment.soccer_context import enrich_pick_with_context
        enrich_pick_with_context(pick)
    except Exception as e:
        logger.debug("soccer_context enrich failed for pick %s: %s", pick.get("id"), e)
    try:
        from services.enrichment.tennis_first_set import enrich_pick_with_first_set
        enrich_pick_with_first_set(pick)
    except Exception as e:
        logger.debug("tennis_first_set enrich failed for pick %s: %s", pick.get("id"), e)

    components = [
        await form_signal(db, pick),
        await matchup_signal(db, pick),
        volume_signal(pick),
        injury_signal(pick),
        market_signal(pick),
        value_signal(pick),
        mlb_deep_signal(pick),
        soccer_deep_signal(pick),
        tennis_deep_signal(pick),
    ]

    # ── Phase-1 external-signal components (2026-07-19) ───────────────
    # Bolt-on components from the newly-added enrichments. Each returns
    # (delta_points, explanation) and only contributes when there's
    # actual data — otherwise 0 points, no component added to the list.
    try:
        from services.enrichment.weather import weather_signal_component
        from services.enrichment.umpires import umpire_signal_component
        from services.enrichment.lineups import lineup_signal_component
        for label, comp_fn in [
            ("weather", weather_signal_component),
            ("umpire",  umpire_signal_component),
            ("lineup",  lineup_signal_component),
        ]:
            pts, why = comp_fn(pick)
            if pts != 0.0 or why:
                components.append({
                    "label": label,
                    "points": float(pts),
                    "delta": float(pts),
                    "why": why,
                })
    except Exception as e:
        logger.debug("phase1 signal components failed: %s", e)

    total = sum(c["points"] for c in components)
    # ── Range amplification (2026-07-17) ─────────────────────────────
    # Raw signal budget is ~±50 but real components rarely stack above
    # ±15 in aggregate, so scores compressed to 45-58 and the user
    # filter had nothing to slice on. Non-linear stretch pushes strong
    # positive signals into the 70-95 band and cold signals into 20-40:
    #    total ≥ 0 → amplify positive delta ×2.5  (delta 8 → 20 = score 70)
    #    total < 0 → amplify negative delta ×2.0  (delta -6 → -12 = score 38)
    # Capped at ±48 so we never quite hit the extremes.
    if total >= 0:
        adjusted = min(48.0, total * 2.5)
    else:
        adjusted = max(-48.0, total * 2.0)

    # ── Elite Player + Lock-Score conviction floor (2026-07-18) ─────
    # User feedback: "I see only 1 goalscorer Saka not Mbappe or Kane".
    # Root cause verified in the API response — Mbappe / Kane /
    # Bellingham were shipping with signal 21-25 (bottom of the slate)
    # despite lock_score=99 (Elite Lock). The composite calculators
    # can't produce a strong positive signal for an international
    # friendly (no player_form data because it's a national team
    # game, no injury chip, no historical H2H) so raw stayed around
    # 50 and the per-sport rank buried them behind grinding
    # Norwegian Eliteserien scorers with lucky component alignments.
    #
    # The Elite Player pipeline (`elite_players.py`) already flags
    # these picks with `is_elite=True` / `elite_boost>0`. If the
    # betting engine has ALREADY committed to a 99-Elite-Lock
    # conviction on the pick, the SIGNAL engine cannot be putting
    # them in the bottom 25% of the slate — that's a contradictory
    # story and the user rightly reads it as broken.
    #
    # Apply a conviction floor: elite-tagged picks OR picks with
    # lock_score ≥ 97 receive a signal-raw uplift so their bucket
    # rank lands in the top third of their sport instead of the
    # bottom third. This is a POST-composite adjustment (not a
    # component) because we're conveying meta-model confidence, not
    # signal-strength evidence.
    is_elite_tagged = bool(
        pick.get("is_elite")
        or pick.get("elite_boost")
        or pick.get("elite_striker")
        or (pick.get("player_tags") or {}).get("elite")
    )
    # Read BOTH lock_score and lock_score_v2 and use the max. The v2
    # is the calibrated shadow score that's been tuned per-sport per-
    # market — it's often more accurate than the base lock_score. On
    # 2026-07-18, Mbappe's SoA had lock=85 but lock_v2=95.4 (Elite);
    # trusting only the base score meant the ≥92 conviction boost
    # never fired for him. Taking the max protects users from either
    # scoring path suppressing a signal that the other confirms. Also
    # include `lock_score_peak` because that's the highest confidence
    # the pick has ever earned (elite-boost/always-starter floor lifts
    # write to peak) — it's the same number the read-time
    # canonicalizer surfaces to the user, so the signal engine should
    # be scoring against the same conviction the user sees on the card.
    _lock_a = pick.get("lock_score")
    _lock_b = pick.get("lock_score_v2")
    _lock_c = pick.get("lock_score_peak")
    def _f(x):
        try:
            return float(x) if x is not None else 0.0
        except (TypeError, ValueError):
            return 0.0
    lock_v = max(_f(_lock_a), _f(_lock_b), _f(_lock_c))
    conviction_boost = 0.0
    if is_elite_tagged:
        # Star player on a market they're priced to hit — floor at
        # +12 raw points (score 62 minimum before rank).
        conviction_boost = max(conviction_boost, 12.0)
    if lock_v >= 97:
        # Elite Lock — floor at +18 (score 68). Only stacks if it's
        # ALSO an elite-tagged player (which pushes it further).
        conviction_boost = max(conviction_boost, 18.0)
    elif lock_v >= 92:
        # Strong Lock — modest floor at +6 raw (score 56).
        conviction_boost = max(conviction_boost, 6.0)
    if is_elite_tagged and lock_v >= 97:
        # Elite player on an Elite Lock — add a stacking bonus so
        # they DEFINITELY land top-quartile of their sport bucket.
        conviction_boost = min(conviction_boost + 10.0, 40.0)
    if conviction_boost > 0:
        adjusted = max(adjusted, conviction_boost)

    score = int(round(max(0.0, min(100.0, 50.0 + adjusted))))
    why = build_why(pick, score, components)

    pick["signal_engine"] = {
        "version": SIGNAL_VERSION,
        "score": score,
        "score_raw": score,   # explicit alias for downstream consumers
        "grade": _grade(score),
        "breakdown": signal_breakdown_line(components),
        "components": components,
        "why": why,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }
    # ── signal_score vs signal_score_raw (2026-07-17) ────────────────
    # `signal_score_raw` is the amplified 0-100 raw calculator output.
    # `signal_score` is the slate-wide percentile rank (0-100) written
    # by `services.signal_engine.rank.refresh_slate_signal_rank`. Once
    # a pick has been ranked, we MUST NOT clobber `signal_score` with
    # the raw value here — that would silently un-rank the pick on the
    # next /picks/today decoration pass and the min_signal slider
    # would collapse back to a 45-86 band (user report 2026-07-17:
    # "signal filter is there just no picks"). We only seed
    # signal_score with the raw value when no rank has ever been
    # persisted; the very next slate-rank pass will replace it with
    # the correct percentile.
    pick["signal_score_raw"] = score
    if pick.get("signal_score") is None:
        pick["signal_score"] = score

    _inject_rationale(pick, score, components)
    return pick


def _inject_rationale(pick: dict, score: int, components: list[dict]) -> None:
    """Push the two strongest signal bullets into `pick_rationale.evidence`
    so the card's expandable "Why This Pick?" panel surfaces them.
    Deduped case-insensitively against existing lines."""
    strongest = sorted(
        (c for c in components if abs(c["points"]) >= 2 and c["details"]),
        key=lambda c: abs(c["points"]), reverse=True,
    )[:2]
    if not strongest:
        return
    rationale = pick.setdefault("pick_rationale", {})
    if not isinstance(rationale, dict):
        return
    evidence = rationale.setdefault("evidence", [])
    if not isinstance(evidence, list):
        return
    seen = {str(line).lower() for line in evidence}
    for c in strongest:
        line = (f"📡 {c['label']} signal "
                f"{'+' if c['points'] > 0 else ''}{c['points']:g}: {c['details'][0]}")
        if line.lower() not in seen:
            evidence.append(line)
            seen.add(line.lower())


async def decorate_signals_bulk(db, picks: list[dict], persist: bool = True) -> list[dict]:
    """Bulk entry-point used by /picks/today and detail endpoints.
    Persists changed blocks best-effort so the Rollover ranker (which
    queries raw docs) can read `signal_score` without re-decoration.

    2026-07-15 fix — user complaint "why are all tennis picks 92?":
    the persist step was only writing `signal_engine` + `signal_score`,
    which meant the `tennis_deep` / `soccer_deep` / `mlb_deep` enrichment
    blocks (populated in `compute_signals`) were being recomputed on
    every request AND weren't being fed into the visible lock_score,
    so users couldn't tell a Sackmann-rich pick apart from a chalk trap.

    Fix (two parts):
      1) Persist the deep-signal blocks too (`tennis_deep`, `mlb_deep`,
         `soccer_deep`) so subsequent DB reads have the data.
      2) Feed `signal_score` back into a `lock_score_signal_adjusted`
         field: signal >= 70 → +3 to base lock, 60-70 → +1, <40 → -3,
         <30 → -5. This actually spreads the on-card score from
         a single-signal 85-95 into a multi-signal 75-98 band.
    """
    if not picks:
        return picks
    ops: list[UpdateOne] = []
    for p in picks:
        try:
            before = (p.get("signal_engine") or {}).get("computed_at")
            await compute_signals(db, p)
            after = (p.get("signal_engine") or {}).get("computed_at")

            # ── Feed signal_score into lock_score adjustment ─────────
            score = p.get("signal_score")
            base_lock = p.get("lock_score")
            if isinstance(score, (int, float)) and isinstance(base_lock, (int, float)):
                if score >= 70:
                    adj = 3.0
                elif score >= 60:
                    adj = 1.0
                elif score < 30:
                    adj = -5.0
                elif score < 40:
                    adj = -3.0
                else:
                    adj = 0.0
                new_lock = max(50.0, min(99.0, base_lock + adj))
                p["lock_score_signal_adjusted"] = round(new_lock, 1)
                p["lock_score_signal_adj_delta"] = round(adj, 1)

            if persist and p.get("id") and after and after != before:
                set_doc = {
                    "signal_engine": p["signal_engine"],
                }
                # Only overwrite `signal_score` in the DB if we don't
                # already have a persisted percentile rank. If we do,
                # let the slate-rank refresher own that field (see
                # `services.signal_engine.rank`).
                if p.get("signal_score_raw") is not None:
                    set_doc["signal_score_raw"] = p["signal_score_raw"]
                if p.get("signal_score") is not None and \
                   p.get("signal_score_raw") is None:
                    # Legacy path: only write signal_score if we haven't
                    # split it out into raw + rank yet.
                    set_doc["signal_score"] = p["signal_score"]
                # Persist deep-signal blocks so subsequent queries have
                # them without re-enrichment (Rollover ranker, admin
                # dashboards, mobile detail modal all read raw docs).
                for k in ("tennis_deep", "mlb_deep", "soccer_deep",
                          "lock_score_signal_adjusted",
                          "lock_score_signal_adj_delta"):
                    if k in p and p[k] is not None:
                        set_doc[k] = p[k]
                ops.append(UpdateOne(
                    {"id": p["id"]},
                    {"$set": set_doc},
                ))
        except Exception as e:
            logger.warning("signal engine failed for pick %s: %s", p.get("id"), e)
    if ops:
        try:
            await db.picks.bulk_write(ops, ordered=False)
        except Exception as e:
            logger.warning("signal engine persist failed: %s", e)
    return picks
