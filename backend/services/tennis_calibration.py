"""Tennis league-average calibration — Phase 3c.

Solves the "everyone scores 99" problem in `tennis_engine.py`:

BEFORE:
    _surface_score, _serve_return_score, _form_score all anchor on
    the book's implied probability (`_implied_to_form_signal`), scaled
    to [55, 100]. So a 75%-implied favorite is 90/100 whether they're
    Alcaraz or an unranked Challenger player — because the market has
    "priced in" surface fit and serve dominance. Result: every pick's
    lock_score clusters at 91-92, no differentiation.

AFTER (this module):
    Compute league averages from `tennis_player_stats` (populated by
    `services.tennis` Sackmann ingester with ~2250 player records)
    then normalize each metric via z-score → 0-100 scale:
        z = (player_stat - league_avg) / league_stddev
        score = 50 + z * 20  (clamped to [0, 100])

    So a 78% hold_pct at league-avg 71% (SD=8) becomes:
        z = (78 - 71) / 8 = 0.875
        score = 50 + 17.5 = 67.5 (above average, but not "elite")

    An elite server like Isner (85% hold, 90% 1st-serve-won):
        score = 87  (correctly identified as top-tier)

    An ITF Futures player (65% hold, 60% 1st-serve-won):
        score = 32  (correctly identified as sub-average)

    Super-locks CAN still reach 99 when EVERY metric is elite
    (serve/return + surface fit + Elo edge + motivation all aligned).

Public API:
    from services.tennis_calibration import (
        refresh_league_averages,     # daily background refresh
        get_calibrated_serve_return, # 0-100 z-score of hold+1st%+return
        get_calibrated_surface_fit,  # 0-100 z-score of surface win %
    )
"""
from __future__ import annotations

import asyncio
import logging
import statistics
from typing import Optional

logger = logging.getLogger("lockscore.services.tennis_calibration")


# Fallback league averages (used when the cache hasn't populated yet).
# Numbers derived from Sackmann 2020-2025 tour data.
_DEFAULT_AVERAGES = {
    "Hard": {
        "hold_pct":            {"mean": 78.5, "std": 6.5},
        "first_serve_won_pct": {"mean": 72.5, "std": 5.5},
        "break_saved_pct":     {"mean": 61.0, "std": 7.0},
        "win_pct":             {"mean": 50.0, "std": 15.0},
    },
    "Clay": {
        "hold_pct":            {"mean": 74.0, "std": 6.5},
        "first_serve_won_pct": {"mean": 70.0, "std": 5.5},
        "break_saved_pct":     {"mean": 60.0, "std": 7.0},
        "win_pct":             {"mean": 50.0, "std": 15.0},
    },
    "Grass": {
        "hold_pct":            {"mean": 82.0, "std": 6.0},
        "first_serve_won_pct": {"mean": 76.0, "std": 5.5},
        "break_saved_pct":     {"mean": 63.0, "std": 6.5},
        "win_pct":             {"mean": 50.0, "std": 15.0},
    },
    "All": {
        "hold_pct":            {"mean": 78.0, "std": 6.8},
        "first_serve_won_pct": {"mean": 72.0, "std": 5.7},
        "break_saved_pct":     {"mean": 61.0, "std": 7.0},
        "win_pct":             {"mean": 50.0, "std": 15.0},
    },
}

# Runtime cache — refreshed daily by refresh_league_averages.
_cache: dict = {"averages": _DEFAULT_AVERAGES, "loaded": False}


# ── Refresh (daily job) ─────────────────────────────────────────────
async def refresh_league_averages(db) -> dict:
    """Recompute league averages per surface from tennis_player_stats.

    Filters to players with ≥8 matches (small-sample players skew the
    mean toward noise). Falls back to defaults if the collection is
    empty (e.g. fresh deployment).
    """
    averages: dict[str, dict] = {}
    for surface in ("Hard", "Clay", "Grass", "All"):
        cursor = db.tennis_player_stats.find(
            {"surface": surface, "n_matches": {"$gte": 8}},
            {"_id": 0, "hold_pct": 1, "first_serve_won_pct": 1,
             "break_saved_pct": 1, "win_pct": 1},
        )
        rows = await cursor.to_list(length=5000)
        if len(rows) < 20:
            # Not enough data → keep default for this surface
            averages[surface] = _DEFAULT_AVERAGES.get(
                surface, _DEFAULT_AVERAGES["All"])
            continue
        stat_agg = {}
        for stat in ("hold_pct", "first_serve_won_pct",
                     "break_saved_pct", "win_pct"):
            vals = [r[stat] for r in rows
                    if isinstance(r.get(stat), (int, float))]
            if len(vals) < 20:
                stat_agg[stat] = _DEFAULT_AVERAGES[surface][stat]
                continue
            mean = statistics.fmean(vals)
            std = statistics.pstdev(vals) or _DEFAULT_AVERAGES[surface][stat]["std"]
            stat_agg[stat] = {"mean": round(mean, 2),
                              "std": round(max(std, 1.5), 2)}
        averages[surface] = stat_agg

    _cache["averages"] = averages
    _cache["loaded"] = True
    try:
        await db.tennis_league_averages.update_one(
            {"_id": "current"}, {"$set": {"averages": averages}}, upsert=True,
        )
    except Exception as e:
        logger.debug("league_averages persist failed: %s", e)
    logger.info("Tennis league averages refreshed for %d surfaces",
                len(averages))
    return averages


async def _load_from_cache(db) -> None:
    """Best-effort load of persisted averages into runtime cache."""
    if _cache["loaded"]:
        return
    try:
        doc = await db.tennis_league_averages.find_one({"_id": "current"})
        if doc and isinstance(doc.get("averages"), dict):
            _cache["averages"] = doc["averages"]
    except Exception:
        pass
    _cache["loaded"] = True


# ── Z-score normalization ───────────────────────────────────────────
def _z_to_score(z: float) -> float:
    """Map z-score to 0-100 with a fatter tail (z=+2 → 90, z=-2 → 10)."""
    return max(0.0, min(100.0, 50.0 + z * 20.0))


def _stat_score(value: Optional[float],
                mean: float, std: float) -> Optional[float]:
    if not isinstance(value, (int, float)) or std <= 0:
        return None
    z = (float(value) - mean) / std
    return round(_z_to_score(z), 1)


async def _resolve_player_name(db, name: str, surface: str) -> Optional[str]:
    """Fuzzy-match a pick's player name against Sackmann's canonical names.

    Pick sources use various formats:
      "Rublev A."      — last-name + initial (tennis_extra)
      "A. Rublev"      — initial + last-name
      "Rublev, Andrey" — CSV format
      "Andrey Rublev"  — canonical (Sackmann's storage format)

    We try exact match first, then extract last-name + initial and
    do a prefix regex match on the canonical stored names."""
    if not name:
        return None
    clean = name.strip()
    # 1. Exact match
    hit = await db.tennis_player_stats.find_one(
        {"name": clean, "surface": surface}, {"_id": 0, "name": 1},
    )
    if hit:
        return hit["name"]

    # 2. Parse "Lastname X." or "Lastname X" → Lastname + Initial
    import re
    m = re.match(r"^([A-Za-zÀ-ÿ' -]+?)\s+([A-Z])\.?$", clean)
    if m:
        last, initial = m.group(1).strip(), m.group(2).strip()
        # Match canonical "FirstName LastName" where FirstName starts with `initial`
        # and LastName is exactly `last`. Escape user input for regex safety.
        first_re = f"^{re.escape(initial)}[a-zA-Z-]+ {re.escape(last)}$"
        hit = await db.tennis_player_stats.find_one(
            {"name": {"$regex": first_re, "$options": "i"}, "surface": surface},
            {"_id": 0, "name": 1},
        )
        if hit:
            return hit["name"]
        # Fallback — any player with that last name (accept most-matched)
        hit = await db.tennis_player_stats.find_one(
            {"name": {"$regex": f" {re.escape(last)}$", "$options": "i"},
             "surface": surface},
            {"_id": 0, "name": 1}, sort=[("n_matches", -1)],
        )
        if hit:
            return hit["name"]

    # 3. Parse "X. Lastname" → Initial + Lastname
    m = re.match(r"^([A-Z])\.?\s+([A-Za-zÀ-ÿ' -]+)$", clean)
    if m:
        initial, last = m.group(1).strip(), m.group(2).strip()
        first_re = f"^{re.escape(initial)}[a-zA-Z-]+ {re.escape(last)}$"
        hit = await db.tennis_player_stats.find_one(
            {"name": {"$regex": first_re, "$options": "i"}, "surface": surface},
            {"_id": 0, "name": 1},
        )
        if hit:
            return hit["name"]

    return None


# ── Calibrated lookups ──────────────────────────────────────────────
async def get_calibrated_serve_return(
    db, player: str, surface: str = "All",
) -> Optional[float]:
    """0-100 z-score composite of hold% + 1st-serve-won% + break-saved%.

    Returns None if player has <5 matches (fall back to heuristic).
    Super-elite servers (Isner-tier) can score 90+; ITF Futures
    players correctly land in 15-35."""
    if not player:
        return None
    await _load_from_cache(db)
    surface_key = (surface or "All").capitalize() if surface else "All"
    if surface_key not in _cache["averages"]:
        surface_key = "All"
    avgs = _cache["averages"][surface_key]

    # Resolve pick's abbreviated name to Sackmann's canonical form.
    canonical = await _resolve_player_name(db, player, surface_key)
    if not canonical:
        # Try "All" as fallback surface for name resolution
        canonical = await _resolve_player_name(db, player, "All")
    if not canonical:
        return None
    stat_doc = await db.tennis_player_stats.find_one(
        {"name": canonical, "surface": surface_key},
        {"_id": 0, "n_matches": 1, "hold_pct": 1,
         "first_serve_won_pct": 1, "break_saved_pct": 1},
    )
    # Fallback: player has data on All surfaces even if not this one
    if not stat_doc:
        stat_doc = await db.tennis_player_stats.find_one(
            {"name": canonical, "surface": "All"},
            {"_id": 0, "n_matches": 1, "hold_pct": 1,
             "first_serve_won_pct": 1, "break_saved_pct": 1},
        )
        if stat_doc:
            avgs = _cache["averages"].get("All", avgs)
    if not stat_doc or (stat_doc.get("n_matches") or 0) < 5:
        return None
    hold_s  = _stat_score(stat_doc.get("hold_pct"),
                          avgs["hold_pct"]["mean"], avgs["hold_pct"]["std"])
    first_s = _stat_score(stat_doc.get("first_serve_won_pct"),
                          avgs["first_serve_won_pct"]["mean"],
                          avgs["first_serve_won_pct"]["std"])
    save_s  = _stat_score(stat_doc.get("break_saved_pct"),
                          avgs["break_saved_pct"]["mean"],
                          avgs["break_saved_pct"]["std"])
    parts = [s for s in (hold_s, first_s, save_s) if s is not None]
    if not parts:
        return None
    # Weighted composite: hold% and 1st-serve-won% carry more signal
    # than break-saved (smaller-sample noise).
    if len(parts) == 3:
        composite = hold_s * 0.4 + first_s * 0.4 + save_s * 0.2
    else:
        composite = sum(parts) / len(parts)
    # Small sample regression: <20 matches → shrink toward league mean (50)
    n = stat_doc.get("n_matches") or 0
    if n < 20:
        weight = n / 20.0
        composite = composite * weight + 50.0 * (1 - weight)
    return round(composite, 1)


async def get_calibrated_surface_fit(
    db, player: str, surface: str = "Hard",
) -> Optional[float]:
    """0-100 z-score of the player's win% on THIS surface vs league avg.

    High score = surface specialist. Nadal on clay = 95+; a hard-court
    specialist on clay = 25-40."""
    if not player or not surface:
        return None
    await _load_from_cache(db)
    surface_key = surface.capitalize()
    if surface_key not in _cache["averages"]:
        surface_key = "All"
    avgs = _cache["averages"][surface_key]

    # Resolve pick's abbreviated name to Sackmann's canonical form.
    canonical = await _resolve_player_name(db, player, surface_key)
    if not canonical:
        canonical = await _resolve_player_name(db, player, "All")
    if not canonical:
        return None
    stat_doc = await db.tennis_player_stats.find_one(
        {"name": canonical, "surface": surface_key},
        {"_id": 0, "n_matches": 1, "win_pct": 1},
    )
    if not stat_doc or (stat_doc.get("n_matches") or 0) < 5:
        # Fall back to "All" surface as a rough proxy — still better
        # than the heuristic that returned 99 for everyone.
        stat_doc = await db.tennis_player_stats.find_one(
            {"name": canonical, "surface": "All"},
            {"_id": 0, "n_matches": 1, "win_pct": 1},
        )
        if not stat_doc or (stat_doc.get("n_matches") or 0) < 5:
            return None
        avgs = _cache["averages"].get("All", avgs)
    score = _stat_score(stat_doc.get("win_pct"),
                        avgs["win_pct"]["mean"], avgs["win_pct"]["std"])
    if score is None:
        return None
    n = stat_doc.get("n_matches") or 0
    if n < 20:
        weight = n / 20.0
        score = score * weight + 50.0 * (1 - weight)
    return round(score, 1)


__all__ = [
    "refresh_league_averages",
    "get_calibrated_serve_return",
    "get_calibrated_surface_fit",
]
