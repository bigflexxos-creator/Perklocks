"""NHL Player History adapter — Perklocks FINAL FLOW + SETTLEMENT PARITY (2026-06).

Reads normalized per-game actuals from ``db.player_game_actuals``
(sport=``nhl``) with fallback to legacy ``db.player_game_logs``.

Market → extractor mapping — every extractor returns ``None`` when
the required component is missing.  Missing data NEVER becomes 0.
Preserved markets:
    Goals / Assists / Points / Shots on Goal

Added markets (ready for when NHL resumes):
    Saves (goalie)
"""
from __future__ import annotations

from typing import Optional

from .models import PlayerHistoryEvidence
from ._shared import populate_standard_evidence


def _extract_nhl_actual(market: str, row: dict) -> Optional[float]:
    m = (market or "").lower()
    actuals = row.get("actuals") or {}

    def _get(*keys) -> Optional[float]:
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

    if m in ("player_goals", "player_anytime_goal", "player_goal_scorer",
              "goals"):
        return _get("goals", "g")
    if m in ("player_assists", "assists"):
        return _get("assists", "a")
    if m in ("player_points", "points"):
        g = _get("goals", "g")
        a = _get("assists", "a")
        if g is None or a is None:
            # Some feeds already store the combined "points" atom.
            return _get("points", "pts")
        return g + a
    if m in ("player_shots_on_goal", "player_shots", "shots_on_goal",
              "shots", "sog"):
        return _get("shots_on_goal", "shots", "sog")
    if m in ("player_total_saves", "player_saves", "saves"):
        return _get("saves", "sv")
    return None


def _nhl_context(rows: list[dict]) -> dict:
    if not rows:
        return {}
    latest = rows[0]
    return {
        "position":         latest.get("position"),
        "latest_opponent":  latest.get("opponent"),
    }


async def populate_nhl_evidence(
    db,
    ev: PlayerHistoryEvidence,
    *,
    player_id: Optional[str],
    canonical_player_id: Optional[str],
    player_name: Optional[str],
    opponent: Optional[str],
    home_away: Optional[str],
) -> PlayerHistoryEvidence:
    market = (ev.market or "").lower()
    # Anytime Goal is a milestone (goals >= 1) — preserve semantics.
    milestone = market in {"player_anytime_goal", "player_goal_scorer"}
    return await populate_standard_evidence(
        db, ev,
        sport="nhl",
        player_id=player_id,
        canonical_player_id=canonical_player_id,
        market_extractor=_extract_nhl_actual,
        opponent=opponent,
        home_away=home_away,
        milestone_market=milestone,
        milestone_semantics="gte",
        row_context_fn=_nhl_context,
    )


__all__ = ["populate_nhl_evidence", "_extract_nhl_actual"]
