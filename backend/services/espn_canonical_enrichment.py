"""ESPN completed-data canonical enrichment (B2 — item 33).

Normalises ESPN ``gamecast`` / ``summary`` completed-event payloads
into Perklocks canonical identity so downstream HI consumers can
read ESPN truth without any synchronous network calls inside scoring
(ESPN is context / history only — never a scoring dependency).

This module is PURE: given a raw ESPN event dict, return a canonical
row suitable for ``player_game_actuals`` / ``team_game_actuals``.

Supported sports:
    NFL, MLB, NBA, NHL, CFB, Soccer
"""
from __future__ import annotations

from typing import Any, Optional


def _get(d: dict, *path: str, default=None):
    cur: Any = d
    for p in path:
        if isinstance(cur, dict):
            cur = cur.get(p)
        else:
            return default
    return cur if cur is not None else default


def _derive_event_time(event: dict) -> Optional[str]:
    for k in ("date", "startDate", "startTime", "event_time",
              "game_date"):
        v = event.get(k)
        if v:
            return str(v)
    return None


def canonical_team_row(event: dict, team_side: dict,
                       opponent_side: dict, sport: str) -> dict:
    """Return a ``team_game_actuals``-shaped canonical row."""
    event_time = _derive_event_time(event)
    team_id = (team_side.get("canonical_team_id")
                or team_side.get("id") or team_side.get("team_id"))
    opp_id = (opponent_side.get("canonical_team_id")
                or opponent_side.get("id") or opponent_side.get("team_id"))
    return {
        "sport": sport,
        "event_time": event_time,
        "canonical_event_id": event.get("id") or event.get("event_id"),
        "canonical_team_id":  f"{sport.lower()}:t:{team_id}" if team_id else None,
        "canonical_opponent_id": f"{sport.lower()}:t:{opp_id}" if opp_id else None,
        "team":                team_side.get("name") or team_side.get("displayName"),
        "opponent":            opponent_side.get("name") or opponent_side.get("displayName"),
        "team_score":          team_side.get("score"),
        "opponent_score":      opponent_side.get("score"),
        "home_away":           team_side.get("home_away")
                                 or ("home" if team_side.get("homeAway") == "home" else "away"),
        "season":              event.get("season"),
        "week":                event.get("week"),
        "result":              (
            "W" if _as_int(team_side.get("score"), 0) > _as_int(opponent_side.get("score"), 0)
            else "L" if _as_int(team_side.get("score"), 0) < _as_int(opponent_side.get("score"), 0)
            else "T"
        ) if team_side.get("score") is not None and opponent_side.get("score") is not None else None,
        "source": "espn_canonical_enrichment",
    }


def canonical_player_row(event: dict, player: dict, team_side: dict,
                         opponent_side: dict, sport: str,
                         actuals: Optional[dict] = None) -> dict:
    """Return a ``player_game_actuals``-shaped canonical row."""
    event_time = _derive_event_time(event)
    pid = (player.get("canonical_player_id")
            or player.get("id") or player.get("athlete_id"))
    team_id = (team_side.get("canonical_team_id")
                or team_side.get("id") or team_side.get("team_id"))
    opp_id = (opponent_side.get("canonical_team_id")
                or opponent_side.get("id") or opponent_side.get("team_id"))
    out = {
        "sport": sport,
        "event_time": event_time,
        "canonical_event_id":     event.get("id") or event.get("event_id"),
        "canonical_player_id":    (
            f"{sport.lower()}:p:{pid}" if pid else None
        ),
        "canonical_team_id":      f"{sport.lower()}:t:{team_id}" if team_id else None,
        "canonical_opponent_id":  f"{sport.lower()}:t:{opp_id}" if opp_id else None,
        "player_name":            player.get("name") or player.get("displayName"),
        "team":                   team_side.get("name") or team_side.get("displayName"),
        "opponent":               opponent_side.get("name") or opponent_side.get("displayName"),
        "home_away":              team_side.get("home_away")
                                    or ("home" if team_side.get("homeAway") == "home" else "away"),
        "season":                 event.get("season"),
        "week":                   event.get("week"),
        "actuals":                dict(actuals or {}),
        "source":                 "espn_canonical_enrichment",
    }
    return out


def _as_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


__all__ = ["canonical_team_row", "canonical_player_row"]
