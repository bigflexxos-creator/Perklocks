"""Platinum NFL game-market simulation (Block 2B.1A §6).

Simulates NFL game markets (ML / Spread / Total) via:

    offense/defense strength → expected margin + total
        ↓
    sample game-script draws
        ↓
    convert margin/total → market outcome probabilities

Favorite/underdog neutrality (§17) is preserved — we compute margin
draws from the expected differential and return the exact-line
probability directly.  No safety floor for favorites, no penalty for
underdogs.  No Over/Under bias.
"""
from __future__ import annotations

import math
import random
from typing import Any, Optional

from services.platinum_nfl.football_core import (
    sample_game_script, quantile_summary, p_over, p_under, p_push,
    LEAGUE_TOTAL_MEAN, LEAGUE_TOTAL_STD,
)


def _sport_key_to_market_side(pick: dict) -> tuple[str, str]:
    """Return (market, side) from a pick.  ``market`` ∈
    {"moneyline","spread","total"}; ``side`` is the sportsbook side."""
    m = str(pick.get("market") or "").lower()
    side = str(pick.get("side") or pick.get("pick_side") or "").strip()
    if "moneyline" in m or "ml" == m.strip():
        return "moneyline", side
    if "spread" in m or "handicap" in m:
        return "spread", side
    if "total" in m or "over/under" in m or "o/u" in m:
        return "total", side
    return "unknown", side


def simulate_game_market(
    pick: dict,
    *,
    expected_margin_home: float,
    total_line: float,
    seed: random.Random,
    n_sims: int = 5000,
    is_home_side: Optional[bool] = None,
) -> dict:
    """Simulate an NFL game market.

    Parameters
    ----------
    pick
        The candidate pick.  Uses ``pick["line"]`` for spread/total
        markets and ``pick["side"]`` (Over/Under, Home/Away) to pick
        the exact side.  Never overwrites the sportsbook line.
    expected_margin_home
        Model's expected HOME point margin (positive = home favored).
        Fed by ``nfl_game_engine`` or an upstream feature engine.
    total_line
        Sportsbook total O/U line — the threshold that the sim
        distribution is compared against.
    seed
        Deterministic RNG (§33).
    n_sims
        Number of game-script realizations.

    Returns
    -------
    A simulator-output dict with quantile summary + exact-line prob.
    Never overwrites ``pick["model_probability"]``.  Fails safely
    with ``ran=False`` if inputs are missing.
    """
    market, side = _sport_key_to_market_side(pick)
    if market == "unknown":
        return _failed("UNSUPPORTED_MARKET",
                       market_threshold=pick.get("line"))
    if total_line is None or not isinstance(total_line, (int, float)):
        return _failed("MISSING_TOTAL_LINE",
                       market_threshold=pick.get("line"))
    if expected_margin_home is None:
        return _failed("MISSING_EXPECTED_MARGIN",
                       market_threshold=pick.get("line"))

    # Draw game-script realizations.
    scripts = sample_game_script(
        expected_margin_home=expected_margin_home,
        total_line=float(total_line),
        seed=seed, n=n_sims,
    )
    margins = [s["margin_home"] for s in scripts]
    totals  = [s["total_points"] for s in scripts]

    if market == "moneyline":
        # ML: home wins iff margin > 0.  Side must resolve to home/away.
        home_side = _resolve_home_side(pick, side, is_home_side)
        if home_side is None:
            return _failed("UNRESOLVED_ML_SIDE")
        n_home_win = sum(1 for m in margins if m > 0)
        n_push     = sum(1 for m in margins if m == 0)
        p_home = n_home_win / len(margins)
        p_a = p_home if home_side else (1.0 - p_home - n_push / len(margins))
        return _summary(
            samples=margins,
            sim_probability=p_a,
            market_threshold=0.0,
            side=side,
            market="moneyline",
        )

    if market == "spread":
        # Spread: HOME covers iff margin > home_spread (i.e. wins by
        # more than |spread| when home is favored, or loses by less
        # than |spread| when home is dog).
        line = pick.get("line")
        try:
            line = float(line)
        except (TypeError, ValueError):
            return _failed("MISSING_SPREAD_LINE")
        # Convention: ``line`` is the sportsbook posted spread from
        # the side's perspective.  For a Home -3.5 pick, side=Home,
        # line = -3.5 → home covers iff margin_home > 3.5.
        home_side = _resolve_home_side(pick, side, is_home_side)
        if home_side is None:
            return _failed("UNRESOLVED_SPREAD_SIDE")
        threshold = -float(line)     # e.g. line=-3.5 → threshold=3.5
        if home_side:
            n_cover = sum(1 for m in margins if m > threshold)
        else:
            n_cover = sum(1 for m in margins if m < threshold)
        p = n_cover / len(margins)
        return _summary(
            samples=margins,
            sim_probability=p,
            market_threshold=float(line),
            side=side,
            market="spread",
        )

    if market == "total":
        line = pick.get("line")
        try:
            line = float(line)
        except (TypeError, ValueError):
            return _failed("MISSING_TOTAL_LINE_ON_PICK")
        s = side.lower()
        if s.startswith("over"):
            p = p_over(totals, line)
        elif s.startswith("under"):
            p = p_under(totals, line)
        else:
            return _failed("UNRESOLVED_TOTAL_SIDE")
        return _summary(
            samples=totals,
            sim_probability=p,
            market_threshold=float(line),
            side=side,
            market="total",
        )

    return _failed("UNSUPPORTED_MARKET_INTERNAL")


def _resolve_home_side(pick: dict, side: str,
                       is_home_side: Optional[bool]) -> Optional[bool]:
    """Return True if the pick sides with the home team, False if
    away, None if unresolvable."""
    if is_home_side is not None:
        return bool(is_home_side)
    s = (side or "").strip().lower()
    home_team = str(pick.get("home_team") or "").strip().lower()
    away_team = str(pick.get("away_team") or "").strip().lower()
    if not s:
        return None
    if home_team and home_team in s:
        return True
    if away_team and away_team in s:
        return False
    # Home/Away literal.
    if s in ("home", "h"):
        return True
    if s in ("away", "a"):
        return False
    return None


def _summary(*, samples: list[float], sim_probability: float,
             market_threshold: float, side: str, market: str) -> dict:
    q = quantile_summary(samples)
    return {
        "ran":              True,
        "market":           market,
        "side":             side,
        "market_threshold": market_threshold,
        "sim_probability":  float(sim_probability),
        "distribution_mean":   q.mean,
        "distribution_median": q.median,
        "q10": q.q10, "q25": q.q25, "q75": q.q75, "q90": q.q90,
        "variance": q.variance, "std": q.std,
        "simulation_count":  q.n,
    }


def _failed(reason: str, **extra) -> dict:
    """Simulator failure contract (§32) — never fake a probability."""
    return {
        "ran":              False,
        "reason":           reason,
        "sim_probability":  None,
        **extra,
    }


__all__ = ["simulate_game_market"]
