"""NBA Player History adapter (Phase 5.3 Stage 2).

Reads normalized per-game actuals from ``db.player_game_actuals``
(sport=``nba``) with fallback to legacy ``db.player_game_logs``
(populated by ``services.nba_gamelog_ingest``).

Derived combo markets (PRA / PR / PA / RA) use authoritative
components — if ANY required component is missing the derived actual
is ``None`` (§7).  Missing data NEVER becomes zero.
"""
from __future__ import annotations

from typing import Optional

from .models import PlayerHistoryEvidence
from ._shared import populate_standard_evidence


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


def _points(row):   return _get(row, "points", "pts")
def _rebounds(row): return _get(row, "rebounds", "reb", "trb")
def _assists(row):  return _get(row, "assists", "ast")
def _threes(row):   return _get(row, "threes_made", "3pm", "three_pm",
                                  "threes", "three_pointers_made")


def _extract_nba_actual(market: str, row: dict) -> Optional[float]:
    m = (market or "").lower()

    # Base atoms ───────────────────────────────────────────────────
    if m in ("player_points", "player_points_alternate", "points"):
        return _points(row)
    if m in ("player_rebounds", "player_rebounds_alternate", "rebounds"):
        return _rebounds(row)
    if m in ("player_assists", "player_assists_alternate", "assists"):
        return _assists(row)
    if m in ("player_threes", "player_threes_alternate", "threes",
              "player_three_pointers_made"):
        return _threes(row)
    if m in ("player_steals", "steals"):
        return _get(row, "steals", "stl")
    if m in ("player_blocks", "blocks"):
        return _get(row, "blocks", "blk")
    if m in ("player_turnovers", "turnovers"):
        return _get(row, "turnovers", "tov", "to")

    # Derived combo markets — §7 explicit rule:
    # missing component ⇒ derived actual is None, not 0.
    if m in ("player_points_rebounds_assists",
              "player_points_rebounds_assists_alternate",
              "pra"):
        p, r, a = _points(row), _rebounds(row), _assists(row)
        if p is None or r is None or a is None:
            return None
        return p + r + a
    if m in ("player_points_rebounds", "pr"):
        p, r = _points(row), _rebounds(row)
        if p is None or r is None:
            return None
        return p + r
    if m in ("player_points_assists", "pa"):
        p, a = _points(row), _assists(row)
        if p is None or a is None:
            return None
        return p + a
    if m in ("player_rebounds_assists", "ra"):
        r, a = _rebounds(row), _assists(row)
        if r is None or a is None:
            return None
        return r + a
    return None


def _nba_context(rows: list[dict]) -> dict:
    if not rows:
        return {}
    latest = rows[0]
    return {
        "position":        latest.get("position"),
        "latest_season":   latest.get("season"),
        "latest_opponent": latest.get("opponent") or latest.get("opp_team_id"),
        "rest_days":       latest.get("rest_days"),
        "is_b2b":          latest.get("is_b2b"),
    }


async def populate_nba_evidence(
    db,
    ev: PlayerHistoryEvidence,
    *,
    player_id: Optional[str],
    canonical_player_id: Optional[str],
    player_name: Optional[str],
    opponent: Optional[str],
    home_away: Optional[str],
) -> PlayerHistoryEvidence:
    return await populate_standard_evidence(
        db, ev,
        sport="nba",
        player_id=player_id,
        canonical_player_id=canonical_player_id,
        market_extractor=_extract_nba_actual,
        opponent=opponent,
        home_away=home_away,
        milestone_market=False,
        row_context_fn=_nba_context,
    )


__all__ = ["populate_nba_evidence", "_extract_nba_actual"]
