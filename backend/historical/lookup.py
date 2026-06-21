"""Player-form lookup helpers — the read-side of the Historical Engine.

The Lock Engine (sports_engine.py / learning_system_v2.py) calls these
functions to fetch a player's recent form + season profile and use them
as a third signal layer ALONGSIDE the existing `elite_players.py` and
`auto_elite.py` checks (per user spec: "don't remove elite players").

Design rules:
  • ZERO HTTP calls here — reads strictly from MongoDB.
  • In-process LRU cache (TTL 5 min) so the Lock Engine isn't hammered
    on every pick generation.
  • Never throws — returns `None` if data is missing so the Lock Engine
    can degrade gracefully.
  • Accent-insensitive name matching — "Mbappe" → "Mbappé",
    "Pena" → "Peña", etc. Critical for soccer where football-data.org
    stores names with diacritics but odds feeds often strip them.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from typing import Optional

logger = logging.getLogger("lockscore.historical.lookup")

_db = None
_cache: dict[tuple[str, str], tuple[float, Optional[dict]]] = {}
_TTL = 300.0  # 5 minutes


def _set_db(db) -> None:
    global _db
    _db = db


def _norm_name(name: str) -> str:
    """Lowercase, strip accents-ish, collapse spaces."""
    if not name:
        return ""
    n = name.lower().strip()
    # Drop common honorifics/qualifiers
    n = re.sub(r"\b(jr|sr|ii|iii|iv)\.?\b", "", n)
    n = re.sub(r"[^a-z0-9\s]", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def _strip_accents(s: str) -> str:
    """Mbappé → Mbappe, Peña → Pena, Vázquez → Vazquez."""
    if not s:
        return ""
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _name_match_query(name: str) -> dict:
    """Build a case-insensitive regex match for player names.

    Mongo regex doesn't natively strip accents, so we build a pattern that
    matches each base letter against itself + any accented variant.
    """
    safe = re.escape(name.strip())
    return {"$regex": f"^{safe}$", "$options": "i"}


def _accent_insensitive_regex(name: str) -> str:
    """Build a regex that matches `name` regardless of diacritics.

    'Mbappe' → matches 'Mbappé', 'Mbappe', 'Mbáppé', etc.
    We escape the input first, then expand each ASCII letter to a class
    containing its common Unicode variants.
    """
    if not name:
        return ""
    # Strip accents from the input first so we work from a normalized base.
    base = _strip_accents(name.strip())
    out: list[str] = []
    # Map each ASCII letter to a character class with common variants.
    table = {
        "a": "[aàáâãäåāăąǎ]",
        "c": "[cçćĉčċ]",
        "e": "[eèéêëēĕėęě]",
        "i": "[iìíîïīĩĭįı]",
        "l": "[lľĺļŀł]",
        "n": "[nñńņňŋ]",
        "o": "[oòóôõöøōŏőǒ]",
        "s": "[sśŝšșş]",
        "u": "[uùúûüūŭůűųǔ]",
        "y": "[yýÿŷȳ]",
        "z": "[zźżž]",
        "d": "[dďđ]",
        "g": "[gĝğġģ]",
        "h": "[hĥħ]",
        "j": "[jĵǰ]",
        "k": "[kķ]",
        "r": "[rŕŗř]",
        "t": "[tţťŧ]",
        "w": "[wŵẁẃẅ]",
    }
    for ch in base:
        lo = ch.lower()
        if lo in table:
            out.append(table[lo])
        else:
            # Escape regex meta characters; keep spaces/hyphens as-is.
            out.append(re.escape(ch))
    return "".join(out)


def _name_match_query(name: str) -> dict:
    """Case-insensitive + accent-insensitive exact match."""
    pattern = _accent_insensitive_regex(name)
    return {"$regex": f"^{pattern}$", "$options": "i"}


async def get_player_form(
    sport: str,
    name: str,
    *,
    market_hint: str | None = None,
) -> Optional[dict]:
    """Return a compact form summary for a player.

    Shape:
      {
        "player_id":     str,
        "name":          str,
        "sport":         str,
        "games_logged":  int,
        "last5_avg":     {stat: float},   # avg over last 5 logs
        "last10_avg":    {stat: float},
        "season_total": {stat: number},
        "consistency":  float,   # 0..1 (fraction of last10 games where they
                                 # produced the headline stat at all)
        "trend":         str,    # "hot" | "cold" | "steady"
      }

    Returns None if the player has no logs (e.g. not yet backfilled).
    """
    if _db is None or not name:
        return None
    sport_l = (sport or "").strip().lower()
    if sport_l == "mlb":
        sport_key = "mlb"
    elif sport_l in ("soccer", "football"):
        sport_key = "soccer"
    elif sport_l == "nba":
        sport_key = "nba"
    elif sport_l == "nfl":
        sport_key = "nfl"
    elif sport_l == "nhl":
        sport_key = "nhl"
    else:
        return None

    ckey = (sport_key, _norm_name(name))
    now = time.time()
    cached = _cache.get(ckey)
    if cached and (now - cached[0]) < _TTL:
        return cached[1]

    try:
        player = await _db.players.find_one(
            {"sport": sport_key, "name": _name_match_query(name)}
        )
        if not player:
            # Fallback: try a partial (last-name) match for soccer where
            # football-data sometimes truncates names.
            parts = name.strip().split()
            if len(parts) >= 2:
                last = parts[-1]
                player = await _db.players.find_one(
                    {"sport": sport_key, "name": {"$regex": re.escape(last) + "$", "$options": "i"}}
                )
        if not player:
            _cache[ckey] = (now, None)
            return None

        pid = player.get("player_id")

        # Pull last 10 game logs (most recent first).
        logs_cursor = _db.player_game_logs.find(
            {"player_id": pid, "sport": sport_key}
        ).sort("date", -1).limit(10)
        logs = [doc async for doc in logs_cursor]

        # Season total (soccer: keyed on competition; for others we sum logs).
        season = None
        if sport_key == "soccer":
            season = await _db.season_totals.find_one(
                {"player_id": pid, "sport": "soccer"},
                sort=[("updated_at", -1)],
            )

        summary = _summarize(sport_key, player, logs, season, market_hint)
        _cache[ckey] = (now, summary)
        return summary
    except Exception as e:
        logger.warning("get_player_form(%s, %s) failed: %s", sport, name, e)
        return None


def _summarize(
    sport: str,
    player: dict,
    logs: list[dict],
    season: Optional[dict],
    market_hint: str | None,
) -> dict:
    """Build the compact form summary used by the Lock Engine."""
    headline = _headline_stat(sport, market_hint)
    last5 = logs[:5]
    last10 = logs[:10]

    def _avg(rows, key):
        vals = [float(r.get(key) or 0) for r in rows if r.get(key) is not None]
        return round(sum(vals) / len(vals), 3) if vals else 0.0

    # Determine which numeric keys to surface per sport.
    keys = _stat_keys(sport)
    last5_avg = {k: _avg(last5, k) for k in keys}
    last10_avg = {k: _avg(last10, k) for k in keys}

    # Consistency: fraction of last10 logs where they produced the headline
    # stat (e.g. "got a hit" / "scored a goal" / "scored a point").
    consistency = 0.0
    if last10 and headline:
        hits = sum(1 for r in last10 if (r.get(headline) or 0) and float(r.get(headline) or 0) > 0)
        consistency = round(hits / len(last10), 3)

    # Trend: compare last5 headline avg vs last10
    trend = "steady"
    if headline and last10_avg.get(headline, 0) > 0:
        d = last5_avg.get(headline, 0) - last10_avg.get(headline, 0)
        if d > 0.15 * last10_avg.get(headline, 1):
            trend = "hot"
        elif d < -0.15 * last10_avg.get(headline, 1):
            trend = "cold"

    out: dict = {
        "player_id": player.get("player_id"),
        "name": player.get("name"),
        "sport": sport,
        "team": player.get("team"),
        "position": player.get("position"),
        "games_logged": len(logs),
        "last5_avg": last5_avg,
        "last10_avg": last10_avg,
        "consistency": consistency,
        "trend": trend,
        "headline_stat": headline,
    }
    if season:
        out["season_total"] = {
            "games": season.get("games"),
            "goals": season.get("goals"),
            "assists": season.get("assists"),
            "competition": season.get("competition"),
        }
    return out


def _stat_keys(sport: str) -> list[str]:
    if sport == "mlb":
        return ["hits", "home_runs", "rbi", "strikeouts", "total_bases", "pitcher_strikeouts"]
    if sport == "nba":
        return ["points", "rebounds", "assists", "threes_made", "steals", "blocks"]
    if sport == "nfl":
        return ["nfl_yds", "nfl_td", "nfl_rec"]
    if sport == "nhl":
        return ["goals", "assists", "points", "shots", "saves"]
    if sport == "soccer":
        return ["goals", "assists", "shots"]
    return []


def _headline_stat(sport: str, market_hint: str | None) -> str:
    """Pick the most relevant stat based on the market the pick is on."""
    m = (market_hint or "").lower()
    if sport == "mlb":
        if "strikeout" in m and "pitcher" in m:
            return "pitcher_strikeouts"
        if "home run" in m or "hr" in m:
            return "home_runs"
        if "rbi" in m:
            return "rbi"
        if "total base" in m:
            return "total_bases"
        return "hits"
    if sport == "nba":
        if "three" in m or "3pt" in m or "3-pt" in m:
            return "threes_made"
        if "rebound" in m:
            return "rebounds"
        if "assist" in m:
            return "assists"
        return "points"
    if sport == "nfl":
        return "nfl_yds"
    if sport == "nhl":
        if "shot" in m:
            return "shots"
        if "save" in m:
            return "saves"
        return "points"
    if sport == "soccer":
        if "assist" in m:
            return "assists"
        if "shot" in m:
            return "shots"
        return "goals"
    return ""


def invalidate_cache() -> None:
    _cache.clear()
