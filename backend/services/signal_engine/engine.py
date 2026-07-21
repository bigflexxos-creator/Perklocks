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

SIGNAL_VERSION = 11  # 2026-07-21 Tier-1: Real MLB team K% vs pitcher hand
# (statsapi.mlb.com), Under K props enabled, pitcher K% + stamina factored
# into mlb_deep_signal. Replaces previous random-uniform placeholder for
# "Opp K% vs same hand" that had no real data behind it.
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

    # ── Data-driven contributions surfacing (2026-07-19) ─────────────
    # When the pick was generated by ``sports_engine`` using the
    # data-driven MLB model, the actual per-feature contribution
    # dict is attached as ``pick['data_driven_contribs']``. Surface
    # those numbers as a proper signal component so the Pick Breakdown
    # panel shows "weather +0.024 · park_hr +0.012 · pitching -0.008"
    # etc. — mirroring the real reasoning the model used to choose
    # this side.
    dd_contribs = pick.get("data_driven_contribs")
    dd_total_pp = 0.0
    dd_num_signals = 0
    if isinstance(dd_contribs, dict) and dd_contribs:
        # 2026-07-21 — Boosted DD weighting: the user's mandate is
        # "picks should be based on data". When the DD model actually
        # HAS data (≥3 signals aligned in the same direction), we
        # want the signal_score to REFLECT that conviction — not to
        # be capped at ±4 like a minor tie-breaker. Weighting now:
        #     1-2 signals → ±6 pp cap  (light DD support)
        #     3-4 signals → ±12 pp cap (moderate DD support)
        #     5+ signals  → ±18 pp cap (strong DD support)
        # Each pp of prob-lift = 0.7 signal points.
        dd_total_pp = sum(
            float(v) for v in dd_contribs.values() if isinstance(v, (int, float))
        )
        dd_num_signals = sum(
            1 for v in dd_contribs.values() if isinstance(v, (int, float)) and abs(v) >= 0.003
        )
        if dd_num_signals >= 5:
            dd_cap = 18.0
        elif dd_num_signals >= 3:
            dd_cap = 12.0
        else:
            dd_cap = 6.0
        dd_points = round(max(-dd_cap, min(dd_cap, dd_total_pp * 70.0)), 2)
        dd_why = " · ".join(
            f"{k}: {v:+.1%}"
            for k, v in sorted(dd_contribs.items(), key=lambda kv: -abs(kv[1]))
            if isinstance(v, (int, float))
        )
        components.append({
            "key":     "data_driven",
            "label":   "Data-Driven Model",
            "points":  dd_points,
            "max":     dd_cap,
            "details": [dd_why] if dd_why else [],
            "delta":   dd_points,
            "why":     dd_why,
            "found":   True,
        })

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
    # ── Tennis elite-player detection (2026-07-21 Option C) ────────────
    # Tennis picks never received the `is_elite` tag because the elite
    # pipeline is team-sport shaped. Consult the curated top-ATP/WTA
    # registry so Alcaraz / Sinner / Djokovic / Sabalenka / Świątek etc.
    # inherit the same +22 conviction floor Mbappé gets in soccer.
    is_elite_tennis = False
    if (pick.get("sport") or "").lower() == "tennis":
        try:
            from services.tennis_elite_players import is_elite_tennis_player
            pick_side_name = (pick.get("pick_side")
                              or pick.get("selection")
                              or pick.get("pick") or "").strip()
            # Selection is often "Player Name Moneyline" — strip trailing market words.
            for tail in (" Moneyline", " ML", " to Win", " Over", " Under"):
                if pick_side_name.endswith(tail):
                    pick_side_name = pick_side_name[: -len(tail)].strip()
            if pick_side_name and is_elite_tennis_player(pick_side_name):
                is_elite_tennis = True
                is_elite_tagged = True
                pick["tennis_elite"] = True
        except Exception as e:
            logger.debug("tennis elite check failed for pick %s: %s", pick.get("id"), e)
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
    # 2026-07-19 — Conviction boost ceiling raised so elite Locks land
    # in the 88-99 raw band (matching what users expect when the card
    # says "SIGNAL 90+"). Previous ceiling (~68 raw for lock=97) made
    # every strong Lock look mediocre. Bands:
    #   Elite Lock 97+           →  +40 raw (score 90)
    #   Strong Lock 92-96        →  +28 raw (score 78)
    #   Moderate Lock 85-91      →  +14 raw (score 64)
    #   Elite tag alone          →  +22 raw (score 72)
    #   Elite tag + Elite Lock   →  +48 raw (score ~98)  \u2190 top of scale
    conviction_boost = 0.0
    if is_elite_tagged:
        # Star player on a market they're priced to hit — floor at
        # +22 raw points (score 72 minimum before rank).
        conviction_boost = max(conviction_boost, 22.0)
    if lock_v >= 97:
        # Elite Lock — floor at +40 (score 90). Elite Locks are the
        # highest-conviction picks the engine emits; they should
        # dominate the top of the signal board.
        conviction_boost = max(conviction_boost, 40.0)
    elif lock_v >= 92:
        # Strong Lock — floor at +28 raw (score 78).
        conviction_boost = max(conviction_boost, 28.0)
    elif lock_v >= 85:
        # Moderate Lock — floor at +14 raw (score 64).
        conviction_boost = max(conviction_boost, 14.0)
    if is_elite_tagged and lock_v >= 97:
        # Elite player on an Elite Lock — stack for a 98 ceiling.
        conviction_boost = min(conviction_boost + 8.0, 48.0)

    # ── Negative-Edge Conviction Cap (2026-07-21 fix) ─────────────────
    # USER REPORT: "How is this a 90 signal when he doesn't do good
    # against this pitcher?" — Mookie Betts Over 0.5 Hits @ -194 with
    # edge=-4.8%, BvP 0/8 lifetime vs Wheeler, but Signal 90 / Lock 99.
    #
    # ROOT CAUSE: Conviction floors above fire purely off `lock_score`,
    # which can be inflated by chalk favoritism (star player, favored
    # team) unrelated to whether the actual PRICE offers value. Result:
    # a pick where the book has us beat (negative edge) still gets a
    # Signal 90 conviction floor. This is exactly the "chalky-lock
    # inversion" bleed pattern — a Lock 99 with -4.8% edge is by
    # definition losing (model says 61%, market prices 66%).
    #
    # POLICY: cap the conviction floor based on edge quality:
    #   edge >= +2pp  →  no cap (floor works as designed)
    #   edge -2..+2pp →  cap at +22 (score 72 max, "solid lean" band)
    #   edge -5..-2pp →  cap at +14 (score 64, "moderate" band)
    #   edge < -5pp   →  cap at 0   (no floor — signal earned by components)
    #
    # This preserves Elite Lock ceiling for genuinely +EV picks while
    # denying artificial boost to picks the market has already priced
    # against us. Component-earned points (Form, Matchup, Value, MLB
    # Context, etc.) still flow through; we just remove the free
    # "Lock band" uplift.
    try:
        _edge_pp = float(pick.get("edge_percent") or 0.0)
    except (TypeError, ValueError):
        _edge_pp = 0.0
    if _edge_pp < -5.0:
        conviction_boost = 0.0
        pick["neg_edge_cap"] = "hard"
    elif _edge_pp < -2.0:
        conviction_boost = min(conviction_boost, 14.0)
        pick["neg_edge_cap"] = "moderate"
    elif _edge_pp < 2.0:
        conviction_boost = min(conviction_boost, 22.0)
        pick["neg_edge_cap"] = "soft"
    # else: edge >= +2pp — full conviction floor available.

    # ── Data-driven conviction floor (2026-07-21) ─────────────────────
    # User mandate: "make sure everything is wired" — picks that were
    # actually GENERATED by the data-driven model (not just tagged with
    # random weather noise) should not be buried at signal 78. When the
    # DD model has aligned signals with positive total lift, it's a
    # "data-anchored strong pick" and deserves a signal in the 80-95
    # band regardless of the base Lock band.
    if dd_num_signals >= 2 and dd_total_pp >= 0.020:
        # 2+ signals, ≥2pp positive lift → floor at +26 raw (score 76)
        # Slight bump above pure Strong-Lock 78 baseline when data
        # is at least corroborating the pick.
        conviction_boost = max(conviction_boost, 26.0)
    if dd_num_signals >= 3 and dd_total_pp >= 0.015:
        # 3+ signals, ≥1.5pp positive lift → floor at +32 raw (score 82)
        conviction_boost = max(conviction_boost, 32.0)
    if dd_num_signals >= 4 and dd_total_pp >= 0.020:
        # 4+ signals, ≥2pp positive lift → floor at +38 raw (score 88)
        conviction_boost = max(conviction_boost, 38.0)
    if dd_num_signals >= 5 and dd_total_pp >= 0.025:
        # 5+ signals, ≥2.5pp positive lift → floor at +44 raw (score 94)
        conviction_boost = max(conviction_boost, 44.0)

    # ── Tennis data-anchored conviction floor (2026-07-21 Option C) ───
    # Tennis-specific floor bump: universal calculators (form / matchup /
    # volume / injury / market) contribute ~0 for tennis picks, so the
    # generic floor bands consistently produced score 76-78 for every
    # Strong Lock. When tennis_deep itself reports strong evidence
    # (≥3 aligned pillars OR high absolute point total), raise the
    # floor so aligned Strong Locks land in the 82-88 band that other
    # sports' Strong Locks reach when their universal calcs fire.
    if (pick.get("sport") or "").lower() == "tennis":
        tennis_comp = next(
            (c for c in components if c.get("key") == "tennis_deep"),
            None,
        )
        if tennis_comp:
            tp = float(tennis_comp.get("points") or 0.0)
            pillars = int(tennis_comp.get("aligned_pillars") or 0)
            if pillars >= 4 or tp >= 5.0:
                # Data-anchored strong tennis pick: floor at +38 (score 88).
                conviction_boost = max(conviction_boost, 38.0)
            elif pillars >= 3 or tp >= 3.5:
                # Solid alignment: floor at +32 (score 82).
                conviction_boost = max(conviction_boost, 32.0)
            elif pillars >= 2 or tp >= 2.0:
                # Modest alignment: floor at +26 (score 76).
                conviction_boost = max(conviction_boost, 26.0)
            # Strong-Lock tennis floor bump: even without strong pillar
            # alignment, a 92-96 Lock tennis pick should not be pinned
            # at 76 when the universal calcs are structurally 0. Bump
            # Strong-Lock tennis baseline to +30 (score 80) — no higher
            # unless data corroborates.
            if 92.0 <= lock_v < 97.0 and tp >= 0.0:
                conviction_boost = max(conviction_boost, 30.0)

    # ── Re-apply negative-edge cap AFTER all floor bumps (2026-07-21) ──
    # DD floors, tennis floors, and elite stackers above can push the
    # conviction floor past the initial edge-cap. Re-cap here so a
    # negative-edge pick can never end up with a Signal ≥ 78 no matter
    # how many "aligned signals" it has. If the market has us beat, we
    # do not have edge — additional model agreement doesn't create it.
    _neg_cap = pick.get("neg_edge_cap")
    if _neg_cap == "hard":
        conviction_boost = 0.0
    elif _neg_cap == "moderate":
        conviction_boost = min(conviction_boost, 14.0)
    elif _neg_cap == "soft":
        conviction_boost = min(conviction_boost, 22.0)

    if conviction_boost > 0:
        # ── Signal spread fix (2026-07-21) ─────────────────────────────
        # OLD: `adjusted = max(adjusted, conviction_boost)` — this
        # HARD-PINNED every pick in each Lock band to the same score
        # (133/163 picks were showing signal=78 because every Strong
        # Lock landed at exactly the 78 floor). The floor overrode all
        # component signal so weather / matchup / DD contribs became
        # invisible.
        # NEW: use conviction as a soft floor with headroom for
        # component-driven variance. Elite Locks (≥40 boost) get NO
        # headroom so they stay pinned at 90+; lower bands get a 4pt
        # window to reveal component differentiation.
        # Effect: Strong Locks now spread 74-88 based on data strength
        # (was pinned at 78); Elite Locks stay 88-98 depending on data.
        if conviction_boost >= 40.0:
            floor_headroom = 0.0   # Elite Lock — pin at floor minimum
        elif conviction_boost >= 28.0:
            floor_headroom = 4.0   # Strong Lock — 4pt drop allowed
        else:
            floor_headroom = 3.0   # Moderate/Elite-tag — 3pt drop
        base = max(adjusted, conviction_boost - floor_headroom)
        # Add a bounded slice of the raw component signal on top so
        # picks with real data lift/drag beat picks with no evidence.
        component_kick = max(-8.0, min(10.0, total * 0.6))
        adjusted = base + component_kick
        # Never fall below (floor - headroom); never exceed 48.
        adjusted = max(conviction_boost - floor_headroom, min(48.0, adjusted))

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
        (c for c in components if abs(c.get("points", 0)) >= 2 and c.get("details")),
        key=lambda c: abs(c.get("points", 0)), reverse=True,
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
