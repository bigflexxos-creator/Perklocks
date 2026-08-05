"""Phase 8 — build an outcome-probability distribution for one player prop.

Reuses `trained_prediction_engine.predict_player_prop` to score a
grid of candidate thresholds. Each threshold's P(over) is the ML
probability + Monte-Carlo shrinkage produced by the SAME code that
serves live picks — no new inference logic.
"""
from __future__ import annotations

from typing import Optional


# Standard sportsbook threshold grids per stat.
# Values chosen to match real book increments (0.5 lines).
_THRESHOLD_GRIDS: dict[tuple[str, str], list[float]] = {
    # NFL
    ("NFL", "passing_yards"):
        [150.5, 175.5, 200.5, 225.5, 250.5, 275.5, 300.5, 325.5, 350.5],
    ("NFL", "rushing_yards"):
        [24.5, 39.5, 49.5, 59.5, 69.5, 79.5, 99.5, 124.5, 149.5],
    ("NFL", "receiving_yards"):
        [19.5, 29.5, 39.5, 49.5, 59.5, 74.5, 99.5, 124.5],
    ("NFL", "receptions"):
        [1.5, 2.5, 3.5, 4.5, 5.5, 6.5, 7.5, 8.5],
    ("NFL", "passing_tds"):
        [0.5, 1.5, 2.5, 3.5],
    # MLB
    ("MLB", "hits"):
        [0.5, 1.5, 2.5, 3.5],
    ("MLB", "total_bases"):
        [0.5, 1.5, 2.5, 3.5, 4.5],
    ("MLB", "home_runs"):
        [0.5, 1.5],
    ("MLB", "strikeouts"):
        [0.5, 1.5, 2.5, 3.5],
    ("MLB", "pitcher_strikeouts"):
        [3.5, 4.5, 5.5, 6.5, 7.5, 8.5, 9.5, 10.5, 11.5],
    ("MLB", "runs_scored"): [0.5, 1.5],
    ("MLB", "rbi"):        [0.5, 1.5, 2.5],
    # NBA
    ("NBA", "points"):
        [9.5, 14.5, 19.5, 24.5, 29.5, 34.5, 39.5, 44.5],
    ("NBA", "rebounds"):
        [3.5, 5.5, 7.5, 9.5, 11.5, 13.5],
    ("NBA", "assists"):
        [1.5, 3.5, 5.5, 7.5, 9.5, 11.5],
    ("NBA", "threes"):
        [0.5, 1.5, 2.5, 3.5, 4.5],
    ("NBA", "points_rebounds_assists"):
        [19.5, 29.5, 39.5, 49.5],
    # Tennis
    ("TENNIS", "aces"):
        [2.5, 4.5, 6.5, 8.5, 10.5, 12.5, 14.5],
    ("TENNIS", "double_faults"):
        [0.5, 1.5, 2.5, 3.5, 4.5, 5.5],
    ("TENNIS", "break_points_won"):
        [0.5, 1.5, 2.5, 3.5, 4.5],
}


def _grid_for(sport: str, stat: str) -> list[float]:
    return _THRESHOLD_GRIDS.get((sport.upper(), stat.lower()), [])


async def build_outcome_distribution(
    db, *,
    sport: str,
    player: str,
    stat: str,
    opponent: Optional[str] = None,
    thresholds: Optional[list[float]] = None,
) -> dict:
    """Return a distribution dict:
        {
          "supported":    bool,
          "reason":       Optional[str],
          "thresholds":   [(line, p_over, ml_meta), ...],
          "projected":    Optional[float],   # ML point projection
          "residual_std": Optional[float],
          "notes":        [...],
        }

    Each threshold's `p_over` comes from `predict_player_prop`, which
    already blends the trained model output with Monte-Carlo shrinkage
    from the base pipeline.
    """
    from services.trained_prediction_engine import predict_player_prop
    grid = thresholds or _grid_for(sport, stat)
    if not grid:
        return {"supported": False,
                 "reason": f"no threshold grid for {sport}/{stat}",
                 "thresholds": [], "notes": []}
    projected: Optional[float] = None
    residual_std: Optional[float] = None
    rows: list[tuple[float, float, dict]] = []
    notes: list[str] = []
    for line in grid:
        r = await predict_player_prop(
            db, sport=sport.upper(), player=player, stat=stat,
            opponent=opponent, line=line,
        )
        if not r.get("supported"):
            notes.append(r.get("reason") or "unsupported")
            continue
        p = r.get("prediction_probability")
        if p is None:
            continue
        if projected is None:
            projected = r.get("expected_value")
            residual_std = r.get("residual_std")
        rows.append((line, float(p), {
            "model":            r.get("model"),
            "top_factors":      r.get("top_factors") or [],
            "expected_value":   r.get("expected_value"),
        }))
    if not rows:
        return {"supported": False,
                 "reason": "distribution empty (all thresholds unsupported)",
                 "thresholds": [], "notes": notes}
    return {
        "supported":     True,
        "thresholds":    rows,
        "projected":     projected,
        "residual_std":  residual_std,
        "notes":         notes,
    }


__all__ = ["build_outcome_distribution", "_grid_for", "_THRESHOLD_GRIDS"]
