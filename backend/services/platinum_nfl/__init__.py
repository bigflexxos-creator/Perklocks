"""Platinum NFL simulator — foundation package (Block 2B.1A, 2026-08).

This package is a NEW simulator that will run **alongside** the existing
Magic 3H empirical simulator (``services/magic/simulators/nfl_simulator.py``)
under an explicit **Champion / Challenger** provenance contract:

    Champion   = empirical / model outputs (existing production)
    Challenger = Platinum causal simulator (this package)

The Champion is authoritative for canonical publication.  Challenger
outputs are FROZEN and attached to the candidate for evaluation, but
DO NOT replace Champion pricing in Block 2B.1A.  Full production
wiring lands in Block 2B.1B.

────────────────────────────────────────────────────────────────
Architecture (spec §4, §5, §6)
────────────────────────────────────────────────────────────────
Expected possessions
    ↓
Expected plays
    ↓
Game-script distribution
    ↓
Role / opportunity distributions
    ↓
Efficiency distributions
    ↓
Correlated football outcomes
    ↓
Player / game stat samples
    ↓
Exact-line probabilities + Q10..Q90 + variance/std

Season-type detection is authoritative and automatic
(``services.platinum_nfl.season_type``).  No manual toggle.

Simulator failure contract (§32) is explicit — the simulator returns
``{"ran": False, "reason": "SIMULATOR_UNAVAILABLE" | "SIMULATOR_FAILED"}``
on any inability to produce a genuine distribution.  Never
``sim_probability = model_probability``.
"""
from __future__ import annotations

from services.platinum_nfl.season_type import (
    SeasonType,
    classify_season_type,
    is_preseason,
    is_regular_season,
    is_postseason,
)
from services.platinum_nfl.football_core import (
    expected_possessions,
    expected_plays,
    sample_game_script,
    ShrinkageEstimator,
    QuantileSummary,
    quantile_summary,
    sim_seed,
)
from services.platinum_nfl.opportunity import (
    QBOpportunity,
    RBOpportunity,
    WROpportunity,
    apply_preseason_regime,
    sample_qb_opportunity,
    sample_rb_opportunity,
    sample_wr_opportunity,
)
from services.platinum_nfl.game_markets import (
    simulate_game_market,
)
from services.platinum_nfl.player_markets import (
    simulate_player_market,
)
from services.platinum_nfl.simulator import (
    PLATINUM_NAME,
    PLATINUM_VERSION,
    PLATINUM_TYPE,
    simulate,
    attach_challenger_output,
    ChampionChallengerFrozenRow,
)
from services.platinum_nfl.rogue_guard import (
    verify_no_rogue_nfl_runtime,
    APPROVED_NFL_RUNTIMES,
    APPROVED_NFL_PUBLISHERS,
)
from services.platinum_nfl.rejection_funnel import (
    NFLRejectionStage,
    record_nfl_rejection,
    classify_from_sim_output,
)

__all__ = [
    # season
    "SeasonType", "classify_season_type",
    "is_preseason", "is_regular_season", "is_postseason",
    # core
    "expected_possessions", "expected_plays", "sample_game_script",
    "ShrinkageEstimator", "QuantileSummary", "quantile_summary", "sim_seed",
    # opportunity
    "QBOpportunity", "RBOpportunity", "WROpportunity",
    "apply_preseason_regime",
    "sample_qb_opportunity", "sample_rb_opportunity", "sample_wr_opportunity",
    # markets
    "simulate_game_market", "simulate_player_market",
    # simulator
    "PLATINUM_NAME", "PLATINUM_VERSION", "PLATINUM_TYPE",
    "simulate", "attach_challenger_output", "ChampionChallengerFrozenRow",
    # guard
    "verify_no_rogue_nfl_runtime",
    "APPROVED_NFL_RUNTIMES", "APPROVED_NFL_PUBLISHERS",
    # funnel
    "NFLRejectionStage", "record_nfl_rejection",
    "classify_from_sim_output",
]
