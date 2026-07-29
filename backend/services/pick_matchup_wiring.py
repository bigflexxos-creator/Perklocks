"""Pick → Matchup Intelligence wiring (2026-07-28).

Bridges the production `picks` collection to the read-only
`player_matchup_intelligence` and `nfl_matchup_intelligence` engines.

──────────────────────────────────────────────────────────────────────
Contract
──────────────────────────────────────────────────────────────────────
    payload = await build_matchup_payload(db, pick_doc)

    → dict with schema:
        {
          "supported": bool,          # False → sport/market unhandled
          "sport": str,
          "player_name": str,
          "opponent_team": str | None,
          "stat": str,                # canonical stat key
          "threshold": float | None,  # over/under line if any
          "matchup_grade": "A+..F",
          "sample_confidence": "high|medium|low|none",
          "sample_size": int,
          "threshold_hit_rate": float,
          "avg_stat_output": float,
          "consistency_score": float,
          "career_vs_opponent": { games, avg, hit_rate, ... },
          "recent_vs_similar":  { ... },
          "overall_last_10":    { ... },
          "overall_season":     { ... },
          "last_meeting": { ... } | None,  # NFL only
          "stat_lines":  { ... } | None,   # NFL only (multi-stat)
          "data_sources_used": [...],
          "notes": [...],
        }

Zero writes. Zero HTTP calls. Empty payloads are returned when parsing
fails or the sport/market isn't supported — never raises.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

logger = logging.getLogger("lockscore.services.pick_matchup_wiring")

# ─────────────────────────────────────────────────────────────────────
# Market → canonical stat key
# ─────────────────────────────────────────────────────────────────────
# Ordered by specificity: check longer/more specific patterns first.
_MLB_MARKET_STAT_MAP: list[tuple[re.Pattern, str]] = [
    (re.compile(r"hits.*runs.*rbis|hrri|hits\s*\+\s*runs\s*\+\s*rbis", re.I),
     "hits_runs_rbis"),
    (re.compile(r"total\s+bases", re.I),  "total_bases"),
    (re.compile(r"home\s*runs?|\bhr\b", re.I), "home_runs"),
    (re.compile(r"\brbis?\b", re.I),       "rbi"),
    (re.compile(r"\bhits?\b", re.I),       "hits"),
    (re.compile(r"strikeouts?|\bks?\b", re.I), "strikeouts"),
]

_NFL_MARKET_STAT_MAP: list[tuple[re.Pattern, str]] = [
    (re.compile(r"pass.*yard", re.I),      "passing_yards"),
    (re.compile(r"pass.*td|pass.*touchdown", re.I), "passing_tds"),
    (re.compile(r"pass.*attempt", re.I),   "attempts"),
    (re.compile(r"pass.*completion", re.I),"completions"),
    (re.compile(r"interception|\bint\b", re.I), "passing_ints"),
    (re.compile(r"rush.*yard|rushing", re.I), "rushing_yards"),
    (re.compile(r"rush.*td", re.I),        "rushing_tds"),
    (re.compile(r"carr(y|ies)", re.I),     "carries"),
    (re.compile(r"recept", re.I),          "receptions"),
    (re.compile(r"recv.*yard|receiving.*yard", re.I), "receiving_yards"),
    (re.compile(r"recv.*td|receiving.*td", re.I),     "receiving_tds"),
    (re.compile(r"target", re.I),          "targets"),
]

_NBA_MARKET_STAT_MAP: list[tuple[re.Pattern, str]] = [
    (re.compile(r"point", re.I),   "points"),
    (re.compile(r"rebound", re.I), "rebounds"),
    (re.compile(r"assist", re.I),  "assists"),
    (re.compile(r"three|3-?pt", re.I), "threes"),
]

_TENNIS_MARKET_STAT_MAP: list[tuple[re.Pattern, str]] = [
    (re.compile(r"ace", re.I),          "aces"),
    (re.compile(r"double\s*fault", re.I), "double_faults"),
    (re.compile(r"total\s*games", re.I),  "total_games"),
]


def _detect_stat(sport: str, market: str) -> Optional[str]:
    sport_u = (sport or "").upper()
    if sport_u == "MLB":
        table = _MLB_MARKET_STAT_MAP
    elif sport_u == "NFL":
        table = _NFL_MARKET_STAT_MAP
    elif sport_u == "NBA":
        table = _NBA_MARKET_STAT_MAP
    elif sport_u == "TENNIS":
        table = _TENNIS_MARKET_STAT_MAP
    else:
        return None
    for pat, key in table:
        if pat.search(market or ""):
            return key
    return None


# ─────────────────────────────────────────────────────────────────────
# Player + threshold parsing
# ─────────────────────────────────────────────────────────────────────
# Handles common market string shapes:
#   "Noah Cameron (KC) Over 2.5 Strikeouts  · ALT LOCK"
#   "Aaron Judge Over 1.5 Total Bases"
#   "Joe Burrow Over 249.5 Passing Yards"
_THRESHOLD_RE = re.compile(
    r"(?:Over|Under|O|U)\s+(-?\d+(?:\.\d+)?)", re.I,
)
_PLAYER_PARENS_RE = re.compile(r"^([A-Z][A-Za-z\.'\-\s]+?)\s*\(([A-Z0-9]{2,4})\)")
_MONEYLINE_RE = re.compile(r"moneyline|winner", re.I)
_TEAM_RE = re.compile(r"total\s*(?:runs|goals|points|score)", re.I)


def _parse_player_and_team_abbr(pick: dict) -> tuple[str, Optional[str]]:
    """Extract (player_name, team_abbrev) from selection + market.

    Returns ("", None) if the pick is a team/moneyline market — matchup
    intelligence isn't applicable to those.
    """
    market = pick.get("market") or ""
    selection = pick.get("selection") or ""
    # Team markets short-circuit — no player.
    if _MONEYLINE_RE.search(market) or _TEAM_RE.search(market):
        return "", None
    # MLB / player-with-team-abbrev shape.
    m = _PLAYER_PARENS_RE.match(market)
    if m:
        return m.group(1).strip(), m.group(2).upper()
    # Fallback: use selection as player name (props usually store the
    # player in `selection`).
    if selection:
        return selection.strip(), None
    return "", None


def _parse_threshold(market: str) -> Optional[float]:
    m = _THRESHOLD_RE.search(market or "")
    if not m:
        return None
    try:
        return float(m.group(1))
    except (TypeError, ValueError):
        return None


# ─────────────────────────────────────────────────────────────────────
# Name → MLB player_id resolution (cached)
# ─────────────────────────────────────────────────────────────────────
# Both `player_game_logs` and `props_history` use the MLB Stats API
# player_id integer — never the player name. We resolve the id via the
# same helper the H2H endpoint uses, and cache per-name in-process.
_MLB_PID_CACHE: dict[str, Optional[int]] = {}


async def _resolve_mlb_player_id(name: str) -> Optional[int]:
    if not name:
        return None
    if name in _MLB_PID_CACHE:
        return _MLB_PID_CACHE[name]
    pid: Optional[int] = None
    # 1. Try `mlb_bvp.lookup_player_id` (batter route) — free, cached.
    try:
        from mlb_bvp import lookup_player_id as _bvp_lookup   # type: ignore
        pid = await _bvp_lookup(name)
    except Exception:
        pid = None
    # 2. Fallback to the pitcher-search endpoint.
    if not pid:
        try:
            from mlb_pitcher_h2h import _resolve_pitcher_id  # type: ignore
            pid = await _resolve_pitcher_id(name)
        except Exception:
            pid = None
    _MLB_PID_CACHE[name] = pid
    return pid


def _parse_opponent_mlb(event: str, pitcher_abbrev: Optional[str]) -> Optional[str]:
    """Delegate to `mlb_pitcher_h2h.resolve_opp_team_name` when possible."""
    if not event:
        return None
    if pitcher_abbrev:
        try:
            from mlb_pitcher_h2h import resolve_opp_team_name  # lazy
            return resolve_opp_team_name(event, pitcher_abbrev)
        except Exception:
            pass
    # Fallback: split on @/vs and return whichever half doesn't match
    # the pick's home_team/away_team hint.
    parts = re.split(r"\s+(?:@|vs)\s+", event)
    if len(parts) != 2:
        return None
    return parts[1].strip()   # best-effort


def _parse_opponent_generic(event: str, own_team_hint: Optional[str]) -> Optional[str]:
    """For sports where we don't have an abbreviation → team resolver.

    Splits event on @/vs; if `own_team_hint` is present we return the
    OTHER side; otherwise we return the second side (best-effort).
    """
    if not event:
        return None
    parts = re.split(r"\s+(?:@|vs)\s+", event)
    if len(parts) != 2:
        return None
    a, b = parts[0].strip(), parts[1].strip()
    if own_team_hint:
        oh = own_team_hint.lower()
        if oh in a.lower() or a.lower() in oh:
            return b
        if oh in b.lower() or b.lower() in oh:
            return a
    return b


# ─────────────────────────────────────────────────────────────────────
# Empty payload builder
# ─────────────────────────────────────────────────────────────────────
def _empty_payload(sport: str, reason: str) -> dict:
    return {
        "supported": False,
        "sport": sport,
        "reason": reason,
        "matchup_grade": None,
        "sample_confidence": "none",
        "sample_size": 0,
        "notes": [reason],
    }


# ─────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────
async def build_matchup_payload(db, pick: dict) -> dict:
    """Build the Matchup Intelligence payload for a single pick.

    Never raises — errors are folded into a `notes` list. Always
    returns a dict the frontend can render defensively.
    """
    sport = (pick.get("sport") or "").strip()
    market = pick.get("market") or ""
    event = pick.get("event") or ""

    if not sport:
        return _empty_payload(sport, "pick missing sport")

    # 1. Player + team abbrev.
    player_name, team_abbr = _parse_player_and_team_abbr(pick)
    if not player_name:
        return _empty_payload(sport, "team/moneyline market — no player matchup")

    # 2. Stat.
    stat = _detect_stat(sport, market)
    if not stat:
        return _empty_payload(sport, f"unrecognised stat in market: {market!r}")

    # 3. Threshold.
    threshold = _parse_threshold(market)

    # 4. Opponent.
    if sport.upper() == "MLB":
        opponent = _parse_opponent_mlb(event, team_abbr)
    else:
        # Use home_team / away_team hint if present, else naïve split.
        own_hint = None
        if team_abbr:
            own_hint = team_abbr
        opponent = _parse_opponent_generic(event, own_hint)

    # 5. Route to the right engine.
    try:
        if sport.upper() == "NFL":
            from services.nfl_matchup_intelligence import (
                get_nfl_matchup_intelligence,
            )
            nfl_res = await get_nfl_matchup_intelligence(
                db,
                player_name=player_name,
                opponent_team=opponent or "",
            )
            # Pull the stat's threshold hit at the requested line (if any)
            grade, hit_rate = _grade_from_nfl(nfl_res, stat, threshold)
            return {
                "supported": True,
                "sport": sport,
                "player_name": player_name,
                "opponent_team": opponent,
                "stat": stat,
                "threshold": threshold,
                "matchup_grade": grade,
                "sample_confidence": nfl_res.sample_confidence,
                "sample_size": nfl_res.games_played,
                "threshold_hit_rate": hit_rate,
                "position": nfl_res.position,
                "last_meeting": nfl_res.last_meeting,
                "stat_lines": {
                    k: v.to_dict() for k, v in nfl_res.stat_lines.items()
                },
                "data_sources_used": nfl_res.data_sources_used,
                "notes": nfl_res.notes,
            }
        # Generic engine for MLB / Tennis / NBA / Soccer.
        from services.player_matchup_intelligence import (
            get_matchup_intelligence,
        )
        # MLB: resolve name → int player_id so props_history +
        # player_game_logs can be queried (both are id-keyed only).
        pid: Optional[int] = None
        if sport.upper() == "MLB":
            pid = await _resolve_mlb_player_id(player_name)
        res = await get_matchup_intelligence(
            db,
            sport=sport,
            player_name=player_name,
            stat=stat,
            player_id=pid,
            opponent_team=opponent,
            threshold=threshold,
        )
        return {
            "supported": True,
            "sport": sport,
            "player_name": player_name,
            "opponent_team": opponent,
            "stat": res.stat,
            "threshold": threshold,
            "matchup_grade": res.matchup_grade,
            "sample_confidence": res.sample_confidence,
            "sample_size": res.sample_size,
            "threshold_hit_rate": res.threshold_hit_rate,
            "avg_stat_output": res.avg_stat_output,
            "median_stat_output": res.median_stat_output,
            "consistency_score": res.consistency_score,
            "career_vs_opponent": res.career_vs_opponent.to_dict(),
            "recent_vs_similar":  res.recent_vs_similar.to_dict(),
            "overall_last_5":     res.overall_last_5.to_dict(),
            "overall_last_10":    res.overall_last_10.to_dict(),
            "overall_season":     res.overall_season.to_dict(),
            "data_sources_used": res.data_sources_used,
            "notes": res.notes,
        }
    except Exception as e:
        logger.exception("matchup engine failed for pick %s: %s",
                         pick.get("id"), e)
        return _empty_payload(sport, f"engine error: {e}")


def _grade_from_nfl(nfl_res, stat: str, threshold: Optional[float]) -> tuple[str, float]:
    """Turn an NFL matchup result into a letter grade + hit-rate.

    Uses the stat_line's threshold that's closest to the pick's
    threshold; falls back to games_played tier when no threshold set.
    """
    if not nfl_res or nfl_res.games_played == 0:
        return "F", 0.0
    sl = nfl_res.stat_lines.get(stat)
    if not sl or not sl.thresholds:
        # No stat breakdown → grade from sample confidence alone.
        conf = nfl_res.sample_confidence
        return ({"high": "B", "medium": "C", "low": "D"}.get(conf, "F"), 0.0)
    if threshold is None:
        # No pick line → use the median threshold's hit-rate.
        thr_keys = sorted(sl.thresholds.keys())
        pick_t = thr_keys[len(thr_keys) // 2]
    else:
        # Snap to nearest stored threshold.
        pick_t = min(sl.thresholds.keys(),
                     key=lambda t: abs(float(t) - float(threshold)))
    hit = sl.thresholds[pick_t]
    hr = float(hit.hit_rate)
    games = int(hit.games)
    sample_mult = min(1.0, games / 8.0)   # 8 games saturates
    raw = hr * (0.6 + 0.4 * sample_mult)
    grade = (
        "A+" if raw >= 0.85 else
        "A"  if raw >= 0.72 else
        "B"  if raw >= 0.60 else
        "C"  if raw >= 0.48 else
        "D"  if raw >= 0.34 else
        "F"
    )
    return grade, round(hr, 4)


__all__ = ["build_matchup_payload"]
