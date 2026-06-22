"""Simulator runner — applies sport-specific Monte Carlo to picks at refresh time.

Currently routes MLB picks to brain.sim_mlb. Soccer / NBA / Tennis sims slot
into the same `sport → simulator` map when Phase B ships.
"""
from __future__ import annotations
import logging
from typing import Optional

logger = logging.getLogger("lockscore.brain.sim_runner")

# Lazy import per sport — keeps the runner lightweight when only some sports
# have simulators.
_SPORTS_WITH_SIM = {"MLB"}


def _player_stats_from_pick(pick: dict) -> dict:
    """Extract any player stats already enriched on the pick.

    Reads from `mlb_bvp` enrichment + `player_intel` cache. Falls back to
    league averages inside the simulator if these are missing.
    """
    stats: dict = {}
    bvp = pick.get("mlb_bvp") or {}
    pi = pick.get("player_intel") or {}
    # Hitter stats
    if "ba" in bvp: stats["ba"] = bvp.get("ba")
    elif "season_ba" in pi: stats["ba"] = pi.get("season_ba")
    if "hr_per_ab" in bvp: stats["hr_per_ab"] = bvp.get("hr_per_ab")
    elif "season_hr_rate" in pi: stats["hr_per_ab"] = pi.get("season_hr_rate")
    if "rbi_per_ab" in pi: stats["rbi_per_ab"] = pi.get("rbi_per_ab")
    # Pitcher stats
    if "k_rate" in bvp: stats["k_rate"] = bvp.get("k_rate")
    elif "season_k_rate" in pi: stats["k_rate"] = pi.get("season_k_rate")
    if "bf_per_inning" in pi: stats["bf_per_inning"] = pi.get("bf_per_inning")
    if "expected_innings" in pi: stats["expected_innings"] = pi.get("expected_innings")
    return stats


def simulate_pick(pick: dict) -> Optional[dict]:
    """Route a pick to its sport's simulator. Returns sim output dict or None."""
    sport = pick.get("sport") or ""
    if sport not in _SPORTS_WITH_SIM:
        return None
    try:
        if sport == "MLB":
            from brain.sim_mlb import simulate_mlb_pick
            stats = _player_stats_from_pick(pick)
            return simulate_mlb_pick(pick, stats)
    except Exception as e:
        logger.warning("Simulator failed for pick %s: %s", pick.get("id", "?")[:8], e)
    return None


def apply_simulations(picks: list[dict]) -> dict:
    """Run simulators across the slate. Mutates each pick in-place with
    sim_* fields. Returns counts {applied, stronger, weaker, neutral}."""
    counts = {"applied": 0, "stronger": 0, "weaker": 0, "neutral": 0}
    for p in picks:
        sim = simulate_pick(p)
        if not sim:
            continue
        p.update(sim)
        counts["applied"] += 1
        counts[sim.get("sim_signal", "neutral")] = counts.get(sim.get("sim_signal", "neutral"), 0) + 1
    return counts
