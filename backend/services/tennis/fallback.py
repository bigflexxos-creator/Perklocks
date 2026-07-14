"""Tennis ingest orchestration + per-player aggregations.

Ingests historical matches from TML-Database, upserts into
`tennis_matches_history`, then computes rolling 52-week aggregate stats
per player and caches into `tennis_player_stats`.

Aggregated fields per player (Sackmann's canonical set):
    n_matches, n_wins, n_losses
    ace_pct              — aces / service points
    df_pct               — double faults / service points
    first_serve_pct      — 1stIn / svpt
    first_serve_won_pct  — 1stWon / 1stIn
    second_serve_won_pct — 2ndWon / (svpt - 1stIn)
    hold_pct             — 1 - (bpFaced / SvGms)
    break_saved_pct      — bpSaved / bpFaced
    retirement_rate      — % of matches ended in RET/WO in last 12 months

Per-surface variants: hard / clay / grass / carpet also cached.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from services.tennis.sources import tml_database as tml

logger = logging.getLogger("lockscore.services.tennis.fallback")

_CANONICAL_SURFACES = ("Hard", "Clay", "Grass", "Carpet")


def _safe_div(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None or b == 0:
        return None
    return a / b


def _match_key(m: dict) -> dict:
    return {
        "date":       m.get("date"),
        "tourney_id": m.get("tourney_id"),
        "winner_id":  m.get("winner_id"),
        "loser_id":   m.get("loser_id"),
    }


async def refresh_tennis_history(db, years: tuple = None) -> dict:
    """Bulk fetch year files from TML-Database + upsert.
    Then recompute per-player rolling aggregates."""
    if years is None:
        this_year = datetime.now(timezone.utc).year
        years = tuple(range(this_year - 3, this_year + 1))
    total_matches = 0
    for year in years:
        matches = await tml.fetch_year(year)
        for m in matches:
            await db.tennis_matches_history.update_one(
                _match_key(m), {"$set": m}, upsert=True,
            )
        total_matches += len(matches)
        logger.info("Tennis history year %d: %d matches", year, len(matches))
    # Recompute player rolling stats
    stats_count = await _recompute_player_stats(db)
    logger.info("Tennis player stats: %d players aggregated", stats_count)
    return {"years": years, "matches": total_matches, "players": stats_count}


async def _recompute_player_stats(db) -> int:
    """Recompute 52-week rolling stats. Uses the LATEST cached match date
    as the reference point (rather than today's date) so that when the
    tennis calendar is in off-season, we still compute stats from the
    most recent 52 weeks of actual play rather than getting an empty
    window."""
    # Find the latest match date in the cache
    latest = None
    async for m in db.tennis_matches_history.find({}, {"date": 1}) \
                                              .sort("date", -1).limit(1):
        latest = m.get("date")
    if not latest:
        return 0
    from datetime import date
    try:
        ref = date.fromisoformat(latest)
    except Exception:
        ref = datetime.now(timezone.utc).date()
    cutoff = (ref - timedelta(days=365)).isoformat()

    # Collect per-player raw counters in one pass through the collection.
    counters: dict[tuple[str, str], dict] = defaultdict(lambda: {
        "n": 0, "w": 0, "l": 0, "ret": 0, "wo": 0,
        "ace": 0, "df": 0, "svpt": 0, "firstin": 0, "firstwon": 0, "secondwon": 0,
        "svgms": 0, "bp_saved": 0, "bp_faced": 0,
    })

    async for m in db.tennis_matches_history.find(
        {"date": {"$gte": cutoff}}, {"_id": 0},
    ):
        surface = m.get("surface") or "Unknown"
        for side, name, prefix in (
            ("w", m.get("winner_name"), "w"),
            ("l", m.get("loser_name"),  "l"),
        ):
            if not name:
                continue
            # Track both "All" and surface-specific keys.
            for key in ((name, "All"), (name, surface)):
                c = counters[key]
                c["n"] += 1
                if side == "w":
                    c["w"] += 1
                else:
                    c["l"] += 1
                if m.get("retirement"):
                    c["ret"] += 1
                if m.get("walkover"):
                    c["wo"] += 1
                for stat, target in (
                    ("ace", "ace"), ("df", "df"), ("svpt", "svpt"),
                    ("1stIn", "firstin"), ("1stWon", "firstwon"),
                    ("2ndWon", "secondwon"), ("SvGms", "svgms"),
                    ("bpSaved", "bp_saved"), ("bpFaced", "bp_faced"),
                ):
                    v = m.get(f"{prefix}_{stat}")
                    if isinstance(v, (int, float)):
                        c[target] += int(v)

    written = 0
    for (name, surface), c in counters.items():
        if c["n"] < 3:
            continue  # need at least 3 matches for a meaningful rate
        stats = {
            "name":     name,
            "surface":  surface,
            "window":   "52w",
            "computed_at":         datetime.now(timezone.utc).isoformat(),
            "n_matches":           c["n"],
            "n_wins":              c["w"],
            "n_losses":            c["l"],
            "win_pct":             round(c["w"] * 100.0 / c["n"], 2),
            "retirement_rate_pct": round((c["ret"] + c["wo"]) * 100.0 / c["n"], 2),
            "ace_pct":             _pct(c["ace"], c["svpt"]),
            "df_pct":              _pct(c["df"], c["svpt"]),
            "first_serve_pct":     _pct(c["firstin"], c["svpt"]),
            "first_serve_won_pct": _pct(c["firstwon"], c["firstin"]),
            "second_serve_won_pct": _pct(c["secondwon"],
                                          (c["svpt"] - c["firstin"]) if c["svpt"] > c["firstin"] else 0),
            "hold_pct":            _pct(c["svgms"] - c["bp_faced"], c["svgms"])
                                   if c["svgms"] else None,
            "break_saved_pct":     _pct(c["bp_saved"], c["bp_faced"]),
            "source":              "tml_database",
        }
        await db.tennis_player_stats.update_one(
            {"name": name, "surface": surface, "window": "52w"},
            {"$set": stats}, upsert=True,
        )
        written += 1
    return written


def _pct(num: float, den: float) -> Optional[float]:
    if not den:
        return None
    return round(num * 100.0 / den, 2)


# ── Public lookups ──────────────────────────────────────────────────
async def get_player_stats(db, name: str,
                           surface: str = "All") -> Optional[dict]:
    """Return rolling 52-week stats for a player.

    Handles two name formats commonly used by upstream ingesters:
      • "Firstname Lastname"   (canonical, Sackmann-format)
      • "Lastname F."          (short form used by Tennis Explorer scrapes)

    The short-form lookup converts to a regex that matches ANY first name
    starting with the initial. E.g. "Vandromme J." → matches "Julien
    Vandromme" and "James Vandromme" both. Falls back to the 'All'
    surface when no surface-specific row exists."""
    if not name:
        return None
    # Detect short "Lastname F." format
    short_re = None
    stripped = name.strip()
    if stripped.endswith("."):
        parts = stripped[:-1].strip().rsplit(" ", 1)
        if len(parts) == 2:
            last, initial = parts[0], parts[1]
            if len(initial) <= 2 and initial.isalpha():
                # Reconstruct as "^initial\w* last$"
                short_re = f"^{initial}\\w*\\s+{last}$"
    lookup_pattern = short_re or f"^{name}$"
    doc = await db.tennis_player_stats.find_one(
        {"name": {"$regex": lookup_pattern, "$options": "i"},
         "surface": surface, "window": "52w"},
        {"_id": 0},
    )
    if doc:
        return doc
    if surface != "All":
        return await db.tennis_player_stats.find_one(
            {"name": {"$regex": lookup_pattern, "$options": "i"},
             "surface": "All", "window": "52w"},
            {"_id": 0},
        )
    return None


def _name_regex(name: str) -> str:
    """Convert player name to a regex that matches both 'Firstname
    Lastname' AND 'Lastname F.' formats commonly used by upstream
    scrapers."""
    stripped = name.strip()
    if stripped.endswith("."):
        parts = stripped[:-1].strip().rsplit(" ", 1)
        if len(parts) == 2:
            last, initial = parts[0], parts[1]
            if len(initial) <= 2 and initial.isalpha():
                return f"^{initial}\\w*\\s+{last}$"
    return f"^{name}$"


async def get_h2h(db, player_a: str, player_b: str,
                  surface: Optional[str] = None) -> dict:
    """Career head-to-head. Returns {a_wins, b_wins, matches} filtered by
    optional surface."""
    if not player_a or not player_b:
        return {"a": player_a, "b": player_b, "a_wins": 0, "b_wins": 0, "matches": 0}
    a_re = _name_regex(player_a)
    b_re = _name_regex(player_b)
    q: dict = {
        "$or": [
            {"winner_name": {"$regex": a_re, "$options": "i"},
             "loser_name":  {"$regex": b_re, "$options": "i"}},
            {"winner_name": {"$regex": b_re, "$options": "i"},
             "loser_name":  {"$regex": a_re, "$options": "i"}},
        ],
    }
    if surface:
        q["surface"] = surface
    a_wins = 0
    b_wins = 0
    async for m in db.tennis_matches_history.find(q, {"winner_name": 1}):
        # Ambiguous short-form → check via regex match
        import re
        if re.match(a_re, m["winner_name"], re.IGNORECASE):
            a_wins += 1
        else:
            b_wins += 1
    return {"a": player_a, "b": player_b, "a_wins": a_wins, "b_wins": b_wins,
            "matches": a_wins + b_wins, "surface": surface or "All"}


async def get_recent_matches(db, player: str, limit: int = 10) -> list[dict]:
    if not player:
        return []
    q = {
        "$or": [
            {"winner_name": {"$regex": f"^{player}$", "$options": "i"}},
            {"loser_name":  {"$regex": f"^{player}$", "$options": "i"}},
        ],
    }
    return await db.tennis_matches_history.find(q, {"_id": 0}) \
                                            .sort("date", -1) \
                                            .limit(limit) \
                                            .to_list(limit)


__all__ = [
    "refresh_tennis_history",
    "get_player_stats",
    "get_h2h",
    "get_recent_matches",
]
