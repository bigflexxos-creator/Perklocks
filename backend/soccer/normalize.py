"""Normalize football-data.org v4 payloads → PerksLocks-flat shapes.

football-data.org wraps its data in a slightly different shape than
api-sports.io. We translate every consumer-facing dict to a stable
snake_case layout so the predictor doesn't need to know about the
upstream format.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any


def _safe(v: Any, default: Any = None) -> Any:
    return v if v is not None else default


def normalize_match(raw: dict) -> dict:
    """Flatten one match object from /matches.

    football-data.org match shape:
      { id, utcDate, status, matchday, stage, lastUpdated,
        homeTeam: {id, name, shortName, tla, crest},
        awayTeam: {id, name, ...},
        score: {fullTime: {home, away}, halfTime: {...}, winner},
        competition: {id, name, code, type, emblem},
        season: {...} }
    """
    home = raw.get("homeTeam") or {}
    away = raw.get("awayTeam") or {}
    comp = raw.get("competition") or {}
    score = raw.get("score") or {}
    full = score.get("fullTime") or {}
    return {
        "fixture_id":       raw.get("id"),
        "date":             raw.get("utcDate"),
        "status":           raw.get("status"),   # SCHEDULED/LIVE/FINISHED/POSTPONED
        "matchday":         raw.get("matchday"),
        "stage":            raw.get("stage"),
        "league_id":        comp.get("id"),
        "league_name":      comp.get("name"),
        "league_code":      comp.get("code"),    # e.g. PL, BL1, PD
        "home_team_id":     home.get("id"),
        "home_team_name":   home.get("name"),
        "home_team_short":  home.get("shortName") or home.get("tla"),
        "away_team_id":     away.get("id"),
        "away_team_name":   away.get("name"),
        "away_team_short":  away.get("shortName") or away.get("tla"),
        "home_goals":       _safe(full.get("home")),
        "away_goals":       _safe(full.get("away")),
        "winner":           score.get("winner"),   # HOME_TEAM/AWAY_TEAM/DRAW or null
    }


def normalize_standing_row(raw: dict) -> dict:
    """Flatten one row inside a `standings[].table[]` for a league."""
    team = raw.get("team") or {}
    return {
        "rank":      raw.get("position"),
        "team_id":   team.get("id"),
        "team_name": team.get("name"),
        "points":    raw.get("points"),
        "played":    raw.get("playedGames"),
        "win":       raw.get("won"),
        "draw":      raw.get("draw"),
        "lose":      raw.get("lost"),
        "goals_for":     raw.get("goalsFor"),
        "goals_against": raw.get("goalsAgainst"),
        "goal_diff": raw.get("goalDifference"),
        "form":      raw.get("form"),     # e.g. "W,W,D,L,W" → handled by predictor
    }


def status_is_pregame(status: str | None) -> bool:
    """True for matches that haven't started yet.
    football-data.org statuses: SCHEDULED, TIMED, IN_PLAY, PAUSED,
    FINISHED, SUSPENDED, POSTPONED, CANCELLED, AWARDED."""
    return status in {"SCHEDULED", "TIMED"}


def normalize_form_string(form: str | None) -> str:
    """football-data.org returns form as comma-separated 'W,W,D,L,W'.
    The predictor expects a contiguous string 'WWDLW'."""
    if not form:
        return ""
    return form.replace(",", "").replace(" ", "").upper()[-5:]


def parse_iso(d: str | None) -> datetime | None:
    if not d:
        return None
    try:
        return datetime.fromisoformat(d.replace("Z", "+00:00"))
    except Exception:
        return None
