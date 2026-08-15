"""Phase 1B — Platinum NFL GAME-market production runtime wiring.

Bridges the production game-market builder
(``sports_engine._picks_from_game``) to the Platinum causal simulator
(``services.platinum_nfl.game_markets.simulate_game_market``) so that
NFL regular-season AND preseason Moneyline / Spread / Total candidates
are evaluated by the authoritative model instead of the legacy
sportsbook-follow path.

Model inputs (expected home margin + expected total) come from
``nfl_game_engine._team_ratings`` — a real independent team-strength
model built from final scores in ``db.games`` (nflverse ingest).
No sportsbook probability is ever used as model probability.

Determinism: every simulation is seeded via
``services.simulation_seed.build_seed`` on stable identifiers
(event id + market + side + line), so same input ⇒ same output.
"""
from __future__ import annotations

import logging
import random
from typing import Any, Optional

logger = logging.getLogger("lockscore.platinum_nfl.game_runtime")

SIMULATOR_NAME = "platinum_nfl_game_sim"
SIMULATOR_VERSION = "1.0.0"
N_SIMS = 5000


async def build_nfl_game_model_context(game: dict) -> dict:
    """Async prefetch (called from ``_fetch_picks_for_sport``).

    Returns a ctx dict consumed by the sync builder:
        nfl_model_available : bool
        expected_margin_home: float   (only when available)
        expected_total      : float   (only when available)
        nfl_model_reason    : str     (only when unavailable)
    """
    home = game.get("home_team") or ""
    away = game.get("away_team") or ""
    if not home or not away:
        return {"nfl_model_available": False,
                "nfl_model_reason": "MISSING_TEAMS"}
    try:
        from services.database import get_database
        from nfl_game_engine import _team_ratings, HFA_POINTS
        db = get_database()
        ctx = await _team_ratings(db)
        ratings = ctx.get("ratings") or {}
        hr = ratings.get(home)
        ar = ratings.get(away)
        if not hr or not ar:
            missing = ",".join(t for t, r in (("home", hr), ("away", ar))
                               if not r)
            return {
                "nfl_model_available": False,
                "nfl_model_reason": f"TEAM_RATINGS_MISSING({missing})",
                "n_teams_indexed": len(ratings),
            }
        expected_margin_home = hr["rating"] - ar["rating"] + HFA_POINTS
        expected_total = (hr["ppg"] + ar["opp_ppg"]
                          + ar["ppg"] + hr["opp_ppg"]) / 2.0
        return {
            "nfl_model_available": True,
            "expected_margin_home": round(float(expected_margin_home), 3),
            "expected_total": round(float(expected_total), 2),
            "home_rating": hr,
            "away_rating": ar,
        }
    except Exception as e:
        logger.warning("NFL game model ctx failed for %s @ %s: %s",
                       away, home, e)
        return {"nfl_model_available": False,
                "nfl_model_reason": f"RATINGS_LOOKUP_ERROR:{type(e).__name__}"}


def platinum_game_side_probability(
    *, game: dict, ctx: dict, market: str, side: str,
    line: Optional[float] = None,
    is_home_side: Optional[bool] = None,
    book_total_line: Optional[float] = None,
) -> dict[str, Any]:
    """Evaluate ONE side of an NFL game market with the Platinum sim.

    market: "Moneyline" | "Spread" | "Total"
    side:   team name (ML/Spread) or "Over"/"Under" (Total)
    line:   sportsbook line for Spread/Total (None for ML)
    book_total_line: the game's real O/U line when available — anchors
        the total-points distribution.  Falls back to the model's
        expected_total (still model-derived, never fabricated odds).

    Returns {"available": bool, "prob": float, "sim": dict,
             "season_type": str, "reason": str-on-failure}.
    """
    if not ctx.get("nfl_model_available"):
        return {"available": False,
                "reason": ctx.get("nfl_model_reason") or "MODEL_UNAVAILABLE"}
    try:
        from services.platinum_nfl.game_markets import simulate_game_market
        from services.platinum_nfl.season_type import classify_season_type
        from services.simulation_seed import build_seed

        sport_key = game.get("sport_key") or ""
        season_type = classify_season_type(
            {"sport_key": sport_key, "commence_time": game.get("commence_time")}
        )
        anchor_total = book_total_line
        if not isinstance(anchor_total, (int, float)):
            anchor_total = ctx.get("expected_total")
        pick_stub = {
            "market": market,
            "side": side,
            "line": line,
            "home_team": game.get("home_team"),
            "away_team": game.get("away_team"),
            "canonical_event_id": game.get("id"),
            "sport_key": sport_key,
        }
        seed_int = build_seed(pick_stub, SIMULATOR_NAME, SIMULATOR_VERSION,
                              allow_name_only_fallback=True)
        sim = simulate_game_market(
            pick_stub,
            expected_margin_home=float(ctx["expected_margin_home"]),
            total_line=float(anchor_total),
            seed=random.Random(seed_int),
            n_sims=N_SIMS,
            is_home_side=is_home_side,
        )
        if not sim.get("ran"):
            return {"available": False,
                    "reason": sim.get("reason") or "SIM_FAILED",
                    "sim": sim,
                    "season_type": getattr(season_type, "value",
                                           str(season_type))}
        st_val = getattr(season_type, "value", str(season_type))
        prob = float(sim["sim_probability"])
        preseason_uncertainty = None
        if st_val == "PRESEASON":
            # ── PHASE 2A (Part 4/5) — explicit preseason uncertainty ─
            # Preseason outcomes carry measurably lower comparability:
            # uncertain starter participation, QB rotation, snap
            # allocation and coaching experimentation.  We model this
            # as a bounded confidence shrink applied to the SIMULATED
            # probability (p' = 0.5 + (p-0.5)·k, k=0.85 ≈ widening the
            # margin sigma ~18%).  It flows through probability → edge
            # → score (never a raw score subtraction), is deterministic,
            # bounded, and visible in provenance.  Regular season is
            # untouched.
            PRESEASON_CONFIDENCE_SHRINK = 0.85
            shrunk = 0.5 + (prob - 0.5) * PRESEASON_CONFIDENCE_SHRINK
            preseason_uncertainty = {
                "confidence_shrink": PRESEASON_CONFIDENCE_SHRINK,
                "raw_sim_probability": round(prob, 4),
                "adjusted_probability": round(shrunk, 4),
                "basis": "starter_participation/QB_rotation/"
                         "snap_allocation/coaching_experimentation",
            }
            prob = shrunk
        return {
            "available": True,
            "prob": prob,
            "sim": sim,
            "season_type": st_val,
            "preseason_uncertainty": preseason_uncertainty,
            "expected_margin_home": ctx["expected_margin_home"],
            "expected_total": ctx["expected_total"],
            "simulator": f"{SIMULATOR_NAME}@{SIMULATOR_VERSION}",
        }
    except Exception as e:
        logger.warning("Platinum game sim failed (%s %s %s): %s",
                       market, side, line, e)
        return {"available": False,
                "reason": f"SIM_EXCEPTION:{type(e).__name__}"}


def attach_game_sim_provenance(pick: dict, result: dict) -> None:
    """Stamp authoritative-model provenance on an emitted NFL pick."""
    if not pick or not result or not result.get("available"):
        return
    pick["model_source"] = SIMULATOR_NAME
    pick["season_type"] = result.get("season_type")
    if result.get("preseason_uncertainty"):
        pick["preseason_uncertainty"] = result["preseason_uncertainty"]
    sim = result.get("sim") or {}
    pick["platinum_game_sim"] = {
        "sim_probability": sim.get("sim_probability"),
        "market": sim.get("market"),
        "side": sim.get("side"),
        "market_threshold": sim.get("market_threshold"),
        "distribution_mean": sim.get("distribution_mean"),
        "distribution_median": sim.get("distribution_median"),
        "q10": sim.get("q10"), "q25": sim.get("q25"),
        "q75": sim.get("q75"), "q90": sim.get("q90"),
        "expected_margin_home": result.get("expected_margin_home"),
        "expected_total": result.get("expected_total"),
        "simulator": result.get("simulator"),
    }
