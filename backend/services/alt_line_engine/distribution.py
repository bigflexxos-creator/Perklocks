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
# UNIVERSAL COVERAGE (2026-06-30) — every player-prop family the
# runtime can publish has a grid so the universal projected-
# distribution fallback (see ``universal_projection.py``) has a
# scoring surface for ANY sport / ANY market.
_THRESHOLD_GRIDS: dict[tuple[str, str], list[float]] = {
    # NFL — passing / rushing / receiving / TDs
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
    ("NFL", "rushing_tds"):
        [0.5, 1.5, 2.5],
    ("NFL", "receiving_tds"):
        [0.5, 1.5, 2.5],
    ("NFL", "passing_completions"):
        [14.5, 17.5, 19.5, 21.5, 23.5, 25.5, 27.5, 29.5],
    ("NFL", "passing_attempts"):
        [24.5, 27.5, 29.5, 31.5, 33.5, 35.5, 37.5, 39.5],
    ("NFL", "rush_attempts"):
        [6.5, 9.5, 11.5, 13.5, 15.5, 17.5, 19.5, 21.5],
    ("NFL", "targets"):
        [3.5, 4.5, 5.5, 6.5, 7.5, 8.5, 9.5, 10.5],
    ("NFL", "carries"):
        [6.5, 9.5, 11.5, 13.5, 15.5, 17.5, 19.5, 21.5],
    ("NFL", "passing_ints"):
        [0.5, 1.5],
    # MLB — batter + pitcher families
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
    ("MLB", "pitcher_outs"):
        [11.5, 12.5, 13.5, 14.5, 15.5, 16.5, 17.5, 18.5, 19.5,
         20.5, 21.5],
    ("MLB", "runs_scored"): [0.5, 1.5],
    ("MLB", "rbi"):         [0.5, 1.5, 2.5],
    ("MLB", "walks"):       [0.5, 1.5, 2.5],
    ("MLB", "hits_runs_rbis"):
        [0.5, 1.5, 2.5, 3.5, 4.5],
    # NBA — points / rebounds / assists / defensive / composite
    ("NBA", "points"):
        [9.5, 14.5, 19.5, 24.5, 29.5, 34.5, 39.5, 44.5],
    ("NBA", "rebounds"):
        [3.5, 5.5, 7.5, 9.5, 11.5, 13.5],
    ("NBA", "assists"):
        [1.5, 3.5, 5.5, 7.5, 9.5, 11.5],
    ("NBA", "threes"):
        [0.5, 1.5, 2.5, 3.5, 4.5],
    ("NBA", "threes_made"):
        [0.5, 1.5, 2.5, 3.5, 4.5],
    ("NBA", "steals"):
        [0.5, 1.5, 2.5, 3.5],
    ("NBA", "blocks"):
        [0.5, 1.5, 2.5, 3.5],
    ("NBA", "points_rebounds_assists"):
        [19.5, 29.5, 39.5, 49.5],
    # Tennis
    ("TENNIS", "aces"):
        [2.5, 4.5, 6.5, 8.5, 10.5, 12.5, 14.5],
    ("TENNIS", "double_faults"):
        [0.5, 1.5, 2.5, 3.5, 4.5, 5.5],
    ("TENNIS", "break_points_won"):
        [0.5, 1.5, 2.5, 3.5, 4.5],
    ("TENNIS", "total_games"):
        [18.5, 20.5, 21.5, 22.5, 23.5, 24.5, 25.5, 26.5, 27.5],
    # Soccer — player + team
    ("SOCCER", "goals"):
        [0.5, 1.5, 2.5],
    ("SOCCER", "goalscorer"):
        [0.5],
    ("SOCCER", "assists"):
        [0.5, 1.5],
    ("SOCCER", "shots_on_target"):
        [0.5, 1.5, 2.5, 3.5],
    ("SOCCER", "shots"):
        [0.5, 1.5, 2.5, 3.5, 4.5],
    ("SOCCER", "score_or_assist"):
        [0.5],
    ("SOCCER", "goal_contributions"):
        [0.5, 1.5],
    # NHL
    ("NHL", "goals"):
        [0.5, 1.5],
    ("NHL", "assists"):
        [0.5, 1.5, 2.5],
    ("NHL", "points"):
        [0.5, 1.5, 2.5, 3.5],
    ("NHL", "shots_on_goal"):
        [1.5, 2.5, 3.5, 4.5, 5.5],
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
    pick: Optional[dict] = None,
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

    UNIVERSAL COVERAGE (2026-06-30) — every threshold gets scored:
      1. If a trained ML model exists for (sport, stat), scores via
         ``predict_player_prop`` (highest accuracy path).
      2. Otherwise falls back to the universal projected-distribution
         helper — back-solves a Poisson (count stats) or Normal
         (continuous stats) from the pick's own ``win_probability``
         + ``line`` and evaluates the grid.  No fabrication: the two
         inputs come from the immutable ``PublishedPickContract``.
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
    # ── Universal fallback ────────────────────────────────────────
    # When the trained model produced NO usable rows (no model for
    # this family / all thresholds unsupported), synthesize the
    # distribution from the pick's own win_probability + line.
    if not rows and isinstance(pick, dict):
        from .universal_projection import universal_distribution
        # win_probability is stored as a percent (0-100) on picks;
        # the helper auto-detects & normalises.
        wp = pick.get("win_probability")
        line = pick.get("line")
        # Some picks carry the line only in the market string — the
        # ranker already parsed it upstream, but be defensive.
        if line is None:
            try:
                import re as _re
                m = _re.search(r"(?:Over|Under|O|U)\s+(-?\d+(?:\.\d+)?)",
                                str(pick.get("market") or ""), _re.I)
                if m:
                    line = float(m.group(1))
            except Exception:
                line = None
        fallback = universal_distribution(
            stat=stat, line=line, win_probability=wp, grid=grid,
        )
        if fallback and fallback.get("supported"):
            fallback["notes"] = notes + fallback.get("notes", [])
            return fallback
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
