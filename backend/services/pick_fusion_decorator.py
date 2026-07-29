"""Pick → Fusion enrichment decorator (2026-07-28).

Wires the existing Prediction Fusion Engine into the production pick
delivery path.  READ-ONLY at the DB level: attaches a `fusion` block
to picks in-memory (and optionally persists it to `fusion_predictions`
for backtesting telemetry).

**Never** modifies simulator math, `lock_score`, `win_probability`, or
any existing pick field.  Adds a NEW top-level `fusion` key on the
pick doc so downstream consumers can opt-in.

Two entry points
────────────────
    # Lazy on-read enrichment (used by GET /api/picks/{id}):
    await enrich_pick_with_fusion(db, pick, persist=True)

    # Bulk enrichment (used by pregame refresh loops when opted-in):
    await enrich_picks_bulk(db, picks, persist=True, concurrency=5)

    # Post-settlement grading (callable job):
    counts = await grade_settled_fusion_predictions(
        db, hours_lookback=48, limit=500,
    )
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

logger = logging.getLogger("lockscore.services.pick_fusion_decorator")


# ─────────────────────────────────────────────────────────────────────
# Pick parsing (reuses the same logic as pick_matchup_wiring but with
# our own light-weight parser to keep the two decoupled).
# ─────────────────────────────────────────────────────────────────────
_MONEYLINE_RE = re.compile(r"moneyline|winner", re.I)
_TEAM_TOTALS_RE = re.compile(r"total\s*(?:runs|goals|points|score)", re.I)
_PLAYER_TEAM_RE = re.compile(r"^([A-Z][A-Za-z\.'\-\s]+?)\s*\(([A-Z0-9]{2,4})\)")
_THRESHOLD_RE = re.compile(r"(?:Over|Under|O|U)\s+(-?\d+(?:\.\d+)?)", re.I)


def _parse_pick(pick: dict) -> Optional[dict]:
    """Return {player, stat, opponent, threshold, sport} or None if
    the pick isn't a supported player-prop shape."""
    sport = (pick.get("sport") or "").strip()
    market = pick.get("market") or ""
    event = pick.get("event") or ""
    selection = pick.get("selection") or ""
    if not sport or not market:
        return None
    if _MONEYLINE_RE.search(market) or _TEAM_TOTALS_RE.search(market):
        return None
    # Player + team-abbrev shape (MLB) → resolve opponent via existing
    # helper. Otherwise use `selection` as the player name.
    m = _PLAYER_TEAM_RE.match(market)
    player_name = m.group(1).strip() if m else selection.strip()
    team_abbr = m.group(2).upper() if m else None
    if not player_name:
        return None
    # Stat detection — reuse `pick_matchup_wiring._detect_stat`.
    from services.pick_matchup_wiring import _detect_stat  # lazy
    stat = _detect_stat(sport, market)
    if not stat:
        return None
    # Threshold.
    threshold = None
    tm = _THRESHOLD_RE.search(market)
    if tm:
        try:
            threshold = float(tm.group(1))
        except (TypeError, ValueError):
            threshold = None
    # Opponent.
    from services.pick_matchup_wiring import (
        _parse_opponent_mlb, _parse_opponent_generic,
    )
    if sport.upper() == "MLB":
        opponent = _parse_opponent_mlb(event, team_abbr)
    else:
        opponent = _parse_opponent_generic(event, team_abbr)
    return {
        "sport":     sport,
        "player":    player_name,
        "stat":      stat,
        "threshold": threshold,
        "opponent":  opponent or "",
    }


# ─────────────────────────────────────────────────────────────────────
# Enrichment builders
# ─────────────────────────────────────────────────────────────────────
def _build_why_this_pick(fusion_result: dict) -> dict:
    """Shape the "Why This Pick" payload the UI can render directly.

    Layout mirrors the spec:
      final_prediction_probability, engines_agreed[], engines_disagreed[],
      matchup_summary, similar_summary, monte_carlo_summary,
      trained_model_summary, confidence_level, top_factors, sample_sizes.
    """
    comps = fusion_result.get("components") or {}
    final_p = fusion_result.get("final_probability")
    agreement = fusion_result.get("model_agreement") or ""

    def _cp(name: str) -> dict:
        c = comps.get(name) or {}
        if isinstance(c, dict):
            return c
        return {}

    def _summary(name: str, prefix: str) -> Optional[str]:
        c = _cp(name)
        if not c.get("available"):
            return None
        p = c.get("probability")
        proj = c.get("projected")
        n = c.get("sample_size")
        parts = [prefix]
        if p is not None:
            parts.append(f"{int(round(float(p) * 100))}% over")
        if proj is not None:
            parts.append(f"projects {round(float(proj), 1)}")
        if n:
            parts.append(f"n={n}")
        return " · ".join(parts)

    # Which engines "agreed" vs "disagreed" with the final lean.
    lean_over = (final_p is not None and final_p >= 0.50)
    agreed, disagreed = [], []
    for name in ("ml", "similar", "player_h2h", "simulator"):
        c = _cp(name)
        if not c.get("available") or c.get("probability") is None:
            continue
        p = float(c["probability"])
        picks_over = p >= 0.50
        if picks_over == lean_over:
            agreed.append(name)
        else:
            disagreed.append(name)

    return {
        "final_probability":       final_p,
        "confidence_level":        fusion_result.get("confidence"),
        "agreement_label":         agreement,
        "agreement_score":         fusion_result.get("agreement_score"),
        "engines_agreed":          agreed,
        "engines_disagreed":       disagreed,
        "matchup_summary":         _summary("player_h2h", "Direct H2H"),
        "similar_matchup_summary": _summary("similar",   "Similar defense"),
        "monte_carlo_summary":     _summary("simulator", "Monte Carlo"),
        "trained_model_summary":   _summary("ml",        "Trained ML"),
        "top_factors":             fusion_result.get("factors_for") or [],
        "counter_factors":         fusion_result.get("factors_against") or [],
        "sample_sizes":            {
            name: (_cp(name).get("sample_size") if _cp(name).get("available")
                    else None)
            for name in ("similar", "player_h2h")
        },
        "explanation":             fusion_result.get("explanation"),
    }


def _extract_actual_from_pick(pick: dict) -> Optional[float]:
    """Given a settled pick doc, pull the actual raw stat value (if any).
    Looks at `settlement_detail.value` first (ESPN player-prop settler),
    then falls back to parsing `final_score`."""
    sd = pick.get("settlement_detail")
    if isinstance(sd, dict):
        v = sd.get("value")
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    # Fallback: final_score dict often has "<Player> <Stat>: N".
    fs = pick.get("final_score")
    if isinstance(fs, dict):
        # Ignore "Line" key; return the first non-Line numeric value.
        for k, v in fs.items():
            if isinstance(k, str) and k.lower() == "line":
                continue
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


# ─────────────────────────────────────────────────────────────────────
# Enrichment entry points
# ─────────────────────────────────────────────────────────────────────
async def enrich_pick_with_fusion(
    db,
    pick: dict,
    *,
    persist: bool = False,
    weights: Optional[dict] = None,
    include_simulator: bool = False,
) -> dict:
    """Attach a `fusion` block to a pick in-place.

    Returns the pick (same object) with:
        pick["fusion"] = {
            "supported":            True/False,
            "prediction_id":        UUID,
            "final_probability":    float,
            "confidence":           str,
            "agreement":            str,
            "why_this_pick":        { ... },  # UI-ready block
            "components":           { ml: {...}, similar: {...}, ...},
            "weights_used":         { ml: 0.44, ...},
            "data_sources_used":    [...],
        }

    Never raises.  On any parse/engine error, sets
    `fusion.supported=False` with a `reason`.
    """
    parsed = _parse_pick(pick)
    if not parsed:
        pick["fusion"] = {
            "supported": False,
            "reason": "not a supported player-prop market",
        }
        return pick
    try:
        from services.prediction_fusion_engine import fuse_prediction
        result = await fuse_prediction(
            db,
            sport=parsed["sport"],
            player=parsed["player"],
            stat=parsed["stat"],
            opponent=parsed["opponent"],
            threshold=parsed["threshold"],
            weights=weights,
            include_simulator=include_simulator,
            persist=False,   # we persist ourselves below with pick_id linkage
        )
    except Exception as e:
        logger.exception("fusion failed for pick %s: %s", pick.get("id"), e)
        pick["fusion"] = {"supported": False, "reason": f"engine error: {e}"}
        return pick

    fusion_dict = result.to_dict()
    pick["fusion"] = {
        "supported":         True,
        "prediction_id":     result.prediction_id,
        "final_probability": result.final_probability,
        "projected_stat":    result.projected_stat,
        "confidence":        result.confidence,
        "agreement":         result.model_agreement,
        "agreement_score":   result.agreement_score,
        "why_this_pick":     _build_why_this_pick(fusion_dict),
        "components":        fusion_dict.get("components"),
        "weights_used":      result.weights_used,
        "data_sources_used": result.data_sources_used,
        "notes":             result.notes,
        "created_at":        result.created_at,
    }

    # Persist telemetry linked to the pick (extended schema from Step 5:
    # add `pick_id`, `market`, `event`, `pick_date`, `league` for filters).
    if persist:
        try:
            doc = fusion_dict
            doc["pick_id"]   = pick.get("id")
            doc["market"]    = pick.get("market")
            doc["event"]     = pick.get("event")
            doc["pick_date"] = pick.get("pick_date")
            doc["league"]    = pick.get("league")
            doc["actual_value"] = None
            doc["outcome"]      = None
            doc["correct"]      = None
            doc["winning_component"] = None
            await db.fusion_predictions.insert_one(doc)
        except Exception as e:
            logger.debug("fusion persist failed for pick %s: %s",
                         pick.get("id"), e)
    return pick


async def enrich_picks_bulk(
    db,
    picks: list[dict],
    *,
    persist: bool = False,
    weights: Optional[dict] = None,
    include_simulator: bool = False,
    concurrency: int = 5,
) -> list[dict]:
    """Fan out enrichment across many picks with bounded concurrency."""
    if not picks:
        return picks
    sem = asyncio.Semaphore(max(1, int(concurrency)))
    async def _one(p):
        async with sem:
            return await enrich_pick_with_fusion(
                db, p, persist=persist,
                weights=weights,
                include_simulator=include_simulator,
            )
    await asyncio.gather(*(_one(p) for p in picks), return_exceptions=False)
    return picks


# ─────────────────────────────────────────────────────────────────────
# Post-settlement grading job
# ─────────────────────────────────────────────────────────────────────
async def grade_settled_fusion_predictions(
    db,
    *,
    hours_lookback: int = 48,
    limit: int = 500,
) -> dict:
    """For every ungraded fusion prediction whose linked pick has been
    settled, extract the actual stat value and call
    `record_prediction_actual` to grade it.

    Returns counters: `{scanned, graded, no_actual, no_pick, errors}`.
    Never raises — safe to run on a cron.
    """
    from services.prediction_fusion_engine import record_prediction_actual

    counts = {"scanned": 0, "graded": 0, "no_actual": 0,
               "no_pick": 0, "errors": 0}
    since = (datetime.now(timezone.utc)
              - timedelta(hours=hours_lookback)).isoformat()
    cursor = db.fusion_predictions.find(
        {"actual_value": None,
          "pick_id":      {"$ne": None},
          "created_at":   {"$gte": since}},
        {"_id": 0, "prediction_id": 1, "pick_id": 1, "threshold": 1},
    ).limit(int(limit))
    ungraded = [d async for d in cursor]
    counts["scanned"] = len(ungraded)
    for row in ungraded:
        pid = row.get("pick_id")
        if not pid:
            counts["no_pick"] += 1
            continue
        pick = await db.picks.find_one(
            {"id": pid, "status": {"$in": ["won", "lost", "push"]}},
            {"_id": 0, "settlement_detail": 1, "final_score": 1,
              "status": 1, "market": 1},
        )
        if not pick:
            continue   # not yet settled
        actual = _extract_actual_from_pick(pick)
        if actual is None:
            counts["no_actual"] += 1
            continue
        try:
            r = await record_prediction_actual(
                db, row["prediction_id"], actual,
            )
            if r.get("ok"):
                counts["graded"] += 1
            else:
                counts["errors"] += 1
        except Exception as e:
            logger.debug("grade failed for %s: %s",
                         row.get("prediction_id"), e)
            counts["errors"] += 1
    return counts


__all__ = [
    "enrich_pick_with_fusion",
    "enrich_picks_bulk",
    "grade_settled_fusion_predictions",
    "_build_why_this_pick",       # exposed for tests
    "_extract_actual_from_pick",  # exposed for tests
    "_parse_pick",                # exposed for tests
]
