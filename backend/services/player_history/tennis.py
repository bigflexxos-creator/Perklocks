"""Tennis Player History adapter (Phase 5.3 Stage 2).

Tennis-appropriate schema — NEVER forced into a team-sport shape (§9).

Preserved:  surface, tournament, round, sets, games, aces,
double_faults, serve/break metrics, and the match result.

Surface is queryable INDEPENDENTLY — the standard windows blend all
surfaces (behaviour that some analytics genuinely want), and a
separate ``ev.by_surface`` dict exposes per-surface splits so
consumers can pick the appropriate context without blending.
"""
from __future__ import annotations

from typing import Optional

from .models import PlayerHistoryEvidence
from ._shared import (
    populate_standard_evidence,
    load_actuals_rows,
    window_dict,
)


VALID_SURFACES = {"hard", "clay", "grass", "carpet", "indoor",
                    "indoor_hard", "outdoor_hard"}


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


def _extract_tennis_actual(market: str, row: dict) -> Optional[float]:
    m = (market or "").lower()

    if m in ("player_aces", "aces"):
        return _get(row, "aces")
    if m in ("player_double_faults", "double_faults", "dfs"):
        return _get(row, "double_faults", "dfs")
    if m in ("player_games_won", "games_won"):
        return _get(row, "games_won")
    if m in ("player_games_lost", "games_lost"):
        return _get(row, "games_lost")
    if m in ("player_sets_won", "sets_won"):
        return _get(row, "sets_won")
    if m in ("total_games", "games_total", "total_games_over_under",
              "player_total_games"):
        gw = _get(row, "games_won")
        gl = _get(row, "games_lost")
        if gw is None or gl is None:
            return None
        return gw + gl
    if m in ("player_break_points_won", "break_points_won"):
        return _get(row, "break_points_won", "bp_won")
    if m in ("player_first_serve_pct", "first_serve_pct"):
        return _get(row, "first_serve_pct", "fs_pct")
    return None


def _tennis_context(rows: list[dict]) -> dict:
    if not rows:
        return {}
    latest = rows[0]
    by_surface: dict[str, int] = {}
    for r in rows:
        s = (r.get("surface") or "").lower()
        if s:
            by_surface[s] = by_surface.get(s, 0) + 1
    return {
        "latest_surface":   (latest.get("surface") or "").lower() or None,
        "latest_tournament": latest.get("tournament") or latest.get("event"),
        "latest_round":     latest.get("round"),
        "matches_by_surface": by_surface,
    }


def _by_surface_breakdown(
    rows: list[dict],
    all_actuals: list[Optional[float]],
    *,
    threshold: float,
    direction: str,
    milestone: bool,
) -> Optional[dict]:
    """Per-surface window dicts.  Only surfaces that appear at least
    once are included; empty groups are omitted.  Never blend."""
    grouped: dict[str, list[Optional[float]]] = {}
    for r, a in zip(rows, all_actuals):
        s = (r.get("surface") or "").lower()
        if not s:
            continue
        grouped.setdefault(s, []).append(a)
    if not grouped:
        return None
    return {
        s: window_dict(
            vals, s, threshold, direction,
            milestone=milestone, milestone_semantics="gte",
            requested=len(vals),
        )
        for s, vals in grouped.items()
    }


async def populate_tennis_evidence(
    db,
    ev: PlayerHistoryEvidence,
    *,
    player_id: Optional[str],
    canonical_player_id: Optional[str],
    player_name: Optional[str],
    opponent: Optional[str],
    home_away: Optional[str],
) -> PlayerHistoryEvidence:
    # Tennis has no home/away in the classic sense — mark it as an
    # extra rather than fabricating a value on the shared field.
    ev = await populate_standard_evidence(
        db, ev,
        sport="tennis",
        player_id=player_id,
        canonical_player_id=canonical_player_id,
        market_extractor=_extract_tennis_actual,
        opponent=opponent,
        home_away=None,     # explicitly not applicable
        milestone_market=False,
        row_context_fn=_tennis_context,
    )
    if ev.games_available:
        rows, _ = await load_actuals_rows(
            db, sport="tennis",
            player_id=player_id,
            canonical_player_id=canonical_player_id,
            history_as_of=ev.history_as_of,
        )
        actuals = [_extract_tennis_actual(ev.market or "", r) for r in rows]
        threshold = float(ev.threshold) if ev.threshold is not None else 0.5
        direction = ev.direction or "over"
        by_surface = _by_surface_breakdown(
            rows, actuals, threshold=threshold, direction=direction,
            milestone=False,
        )
        if by_surface:
            ev.by_surface = by_surface
    return ev


__all__ = ["populate_tennis_evidence", "_extract_tennis_actual",
             "VALID_SURFACES"]
