"""Sport-specific extras extractors for Team History.

Every extractor returns a ``dict`` of sport-specific metadata that
lives on ``TeamHistoryEvidence.extras``.  Missing data returns
``None`` (never zero, never fabricated) — the shared populate
contract handles win/loss/scored/conceded uniformly across sports.
"""
from __future__ import annotations

from typing import Optional


def _get(row: dict, *keys) -> Optional[float]:
    actuals = row.get("actuals") or {}
    for k in keys:
        v = actuals.get(k) if actuals else None
        if v is None:
            v = row.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
    return None


# ═══════════════════════════════════════════════════════════════════
# MLB
# ═══════════════════════════════════════════════════════════════════
def mlb_context(rows: list[dict]) -> dict:
    if not rows:
        return {}
    latest = rows[0]
    return {
        "latest_starting_pitcher_id":  latest.get("starting_pitcher_id"),
        "latest_opponent_sp_id":       latest.get("opponent_starting_pitcher_id"),
        "latest_innings":              latest.get("innings"),
        "latest_extra_innings":        latest.get("extra_innings"),
        "latest_venue":                latest.get("venue"),
    }


# ═══════════════════════════════════════════════════════════════════
# NFL
# ═══════════════════════════════════════════════════════════════════
def nfl_context(rows: list[dict]) -> dict:
    if not rows:
        return {}
    latest = rows[0]
    return {
        "latest_week":         latest.get("week"),
        "latest_venue":        latest.get("venue"),
        "latest_turnovers":    _get(latest, "turnovers"),
        "latest_off_yards":    _get(latest, "off_yards", "yards_gained"),
        "latest_def_yards":    _get(latest, "def_yards", "yards_allowed"),
    }


# ═══════════════════════════════════════════════════════════════════
# NBA
# ═══════════════════════════════════════════════════════════════════
def nba_context(rows: list[dict]) -> dict:
    if not rows:
        return {}
    latest = rows[0]
    return {
        "latest_overtime":     latest.get("overtime"),
        "latest_venue":        latest.get("venue"),
        "latest_rest_days":    latest.get("rest_days"),
        "latest_pace":         _get(latest, "pace"),
    }


# ═══════════════════════════════════════════════════════════════════
# Soccer
# ═══════════════════════════════════════════════════════════════════
def soccer_context(rows: list[dict]) -> dict:
    if not rows:
        return {}
    latest = rows[0]
    # xG summary across the sample where the field is present —
    # never fabricated when absent.
    xg_for: list[float] = []
    xg_against: list[float] = []
    corners_for: list[float] = []
    for r in rows:
        v = _get(r, "xg_for", "xg")
        if v is not None:
            xg_for.append(v)
        v = _get(r, "xg_against")
        if v is not None:
            xg_against.append(v)
        v = _get(r, "corners_for", "corners")
        if v is not None:
            corners_for.append(v)
    return {
        "latest_competition":   latest.get("competition")
                                 or latest.get("league"),
        "latest_venue":         latest.get("venue"),
        "latest_extra_time":    latest.get("extra_time"),
        "latest_penalties":     latest.get("penalties"),
        "xg_for_avg":           (round(sum(xg_for) / len(xg_for), 3)
                                  if xg_for else None),
        "xg_against_avg":       (round(sum(xg_against) / len(xg_against), 3)
                                  if xg_against else None),
        "corners_for_avg":      (round(sum(corners_for) / len(corners_for), 3)
                                  if corners_for else None),
    }


# ═══════════════════════════════════════════════════════════════════
# NHL
# ═══════════════════════════════════════════════════════════════════
def nhl_context(rows: list[dict]) -> dict:
    if not rows:
        return {}
    latest = rows[0]
    shots_for: list[float] = []
    shots_against: list[float] = []
    for r in rows:
        v = _get(r, "shots_for", "shots")
        if v is not None:
            shots_for.append(v)
        v = _get(r, "shots_against")
        if v is not None:
            shots_against.append(v)
    # OT / SO markers preserved on latest row.
    return {
        "latest_overtime":      latest.get("overtime"),
        "latest_shootout":      latest.get("shootout"),
        "latest_venue":         latest.get("venue"),
        "shots_for_avg":        (round(sum(shots_for) / len(shots_for), 3)
                                  if shots_for else None),
        "shots_against_avg":    (round(sum(shots_against) / len(shots_against), 3)
                                  if shots_against else None),
    }


# ═══════════════════════════════════════════════════════════════════
# CFB
# ═══════════════════════════════════════════════════════════════════
def cfb_context(rows: list[dict]) -> dict:
    if not rows:
        return {}
    latest = rows[0]
    return {
        "latest_week":     latest.get("week"),
        "latest_venue":    latest.get("venue"),
        "latest_neutral":  latest.get("neutral_site"),
    }


__all__ = [
    "mlb_context",
    "nfl_context",
    "nba_context",
    "soccer_context",
    "nhl_context",
    "cfb_context",
]
