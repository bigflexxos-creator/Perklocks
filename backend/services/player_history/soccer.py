"""Soccer Player History adapter (Phase 5.3 Stage 2).

Reads normalized per-match actuals from ``db.player_game_actuals``
(sport=``soccer``) with fallback to legacy ``db.player_game_logs``.

Preserved fields include: goals, assists, shots, shots_on_target,
minutes, starter/substitute state, competition, historical club.

Rules (§8):
* Historical club membership is preserved as ``historical_team`` and
  MUST NOT be treated as current roster validation.  Current
  roster/fixture truth remains a separate production gate.
* Anytime-scorer markets are milestone (>=1 goal) — never Over/Under.
* Score-or-assist derived market: contribution = goals + assists;
  if either is missing → ``None`` (never zero).
"""
from __future__ import annotations

from typing import Optional

from .models import PlayerHistoryEvidence
from ._shared import (
    populate_standard_evidence,
    window_dict,
)


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


def _extract_soccer_actual(market: str, row: dict) -> Optional[float]:
    m = (market or "").lower()

    if m in ("player_goal_scorer_anytime", "player_first_goal_scorer",
              "anytime_scorer", "goals", "player_goals",
              "player_goals_scored"):
        return _get(row, "goals")
    if m in ("player_assists", "assists"):
        return _get(row, "assists")
    if m in ("player_shots", "shots"):
        return _get(row, "shots")
    if m in ("player_shots_on_target", "shots_on_target", "sot"):
        return _get(row, "shots_on_target", "sot")
    if m in ("player_to_score_or_assist", "score_or_assist"):
        g = _get(row, "goals")
        a = _get(row, "assists")
        if g is None or a is None:
            return None
        return g + a
    if m in ("player_tackles", "tackles"):
        return _get(row, "tackles")
    if m in ("player_passes", "passes"):
        return _get(row, "passes")
    return None


def _soccer_context(rows: list[dict]) -> dict:
    """Preserve club membership + minutes + starter state on the
    latest row.  The dispatcher exposes this on ``ev.extras``.
    Historical fields are separate from current_team (§8)."""
    if not rows:
        return {}
    latest = rows[0]
    # Also aggregate a by-competition breakdown when present.
    by_comp: dict[str, int] = {}
    for r in rows:
        comp = r.get("competition") or r.get("league")
        if isinstance(comp, str):
            by_comp[comp] = by_comp.get(comp, 0) + 1
    return {
        "latest_competition":   latest.get("competition")
                                 or latest.get("league"),
        "historical_club":      latest.get("team") or latest.get("club"),
        "latest_minutes":       latest.get("minutes"),
        "latest_started":       latest.get("started")
                                 or latest.get("starter"),
        "matches_by_competition": by_comp,
    }


def _by_competition_breakdown(
    rows: list[dict],
    all_actuals: list[Optional[float]],
    *,
    threshold: float,
    direction: str,
    milestone: bool,
) -> Optional[dict]:
    """Compute a per-competition window dict.  Returns None when the
    dataset has no ``competition`` field on any row."""
    labeled: list[tuple[str, Optional[float]]] = []
    for r, a in zip(rows, all_actuals):
        comp = r.get("competition") or r.get("league")
        if isinstance(comp, str) and comp:
            labeled.append((comp, a))
    if not labeled:
        return None
    grouped: dict[str, list[Optional[float]]] = {}
    for comp, a in labeled:
        grouped.setdefault(comp, []).append(a)
    return {
        comp: window_dict(
            vals, comp, threshold, direction,
            milestone=milestone, milestone_semantics="gte",
            requested=len(vals),
        )
        for comp, vals in grouped.items()
    }


async def populate_soccer_evidence(
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
    milestone = market in {
        "player_goal_scorer_anytime", "player_first_goal_scorer",
        "anytime_scorer",
    }
    ev = await populate_standard_evidence(
        db, ev,
        sport="soccer",
        player_id=player_id,
        canonical_player_id=canonical_player_id,
        market_extractor=_extract_soccer_actual,
        opponent=opponent,
        home_away=home_away,
        milestone_market=milestone,
        milestone_semantics="gte",
        row_context_fn=_soccer_context,
    )
    # By-competition breakdown attached after the standard populate.
    if ev.games_available:
        # Re-read the rows via the source label — we only have the
        # extracted actuals still available via ``ev`` state; instead
        # we perform a second (cheap) load using the same helper.
        from ._shared import load_actuals_rows
        rows, _ = await load_actuals_rows(
            db, sport="soccer",
            player_id=player_id,
            canonical_player_id=canonical_player_id,
            history_as_of=ev.history_as_of,
        )
        actuals = [_extract_soccer_actual(ev.market or "", r) for r in rows]
        threshold = float(ev.threshold) if ev.threshold is not None else 0.5
        direction = ev.direction or "over"
        by_comp = _by_competition_breakdown(
            rows, actuals,
            threshold=threshold,
            direction=direction,
            milestone=milestone,
        )
        if by_comp:
            ev.by_competition = by_comp
    return ev


__all__ = ["populate_soccer_evidence", "_extract_soccer_actual"]
