"""Simulator runner — applies sport-specific Monte Carlo to picks at refresh time.

Currently routes MLB picks to brain.sim_mlb. Soccer / NBA / Tennis sims slot
into the same `sport → simulator` map when Phase B ships.
"""
from __future__ import annotations
import logging
import math
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


# Max ± lift the simulator can tilt `lock_score`. Kept small so it acts as a
# nudge alongside player_form (±5) and bandit (±LIFT_MAX), never dominating
# the engine. Sim disagreement is mapped through a soft curve so a 10-point
# disagreement = ~+2 lift, 20 points = ~+3.5 lift, 30+ = clamps to ±SIM_LIFT_MAX.
SIM_LIFT_MAX = 4.0

# Threshold (in raw model-percentage points) below which we don't lift at all.
# Anything inside ±SIM_NEUTRAL_BAND is treated as agreement → no lift.
SIM_NEUTRAL_BAND = 5.0


def _sim_lift_from_disagreement(disagreement: float) -> float:
    """Map sim − model (percentage points) to a bounded ± lock_score lift.

    Inside ±SIM_NEUTRAL_BAND → 0 lift.
    Outside band → log-style decay so big disagreements get diminishing returns.
    """
    if disagreement is None:
        return 0.0
    abs_d = abs(disagreement)
    if abs_d <= SIM_NEUTRAL_BAND:
        return 0.0
    # Soft saturating curve: 10pp → ~2.1, 20pp → ~3.4, 30pp → ~4.0
    over = abs_d - SIM_NEUTRAL_BAND
    lift = SIM_LIFT_MAX * (1 - math.exp(-over / 12.0))
    return lift if disagreement > 0 else -lift


def apply_simulations(picks: list[dict]) -> dict:
    """Run simulators across the slate. Mutates each pick in-place with
    sim_* fields AND tilts lock_score by ±SIM_LIFT_MAX based on the sim's
    disagreement with the blended model. Returns counts."""
    counts = {"applied": 0, "stronger": 0, "weaker": 0, "neutral": 0, "lifted_up": 0, "lifted_down": 0}
    for p in picks:
        sim = simulate_pick(p)
        if not sim:
            continue
        p.update(sim)
        counts["applied"] += 1
        sig = sim.get("sim_signal", "neutral")
        counts[sig] = counts.get(sig, 0) + 1

        # ── Apply lock_score lift ──────────────────────────────────────
        disagreement = float(sim.get("sim_disagreement_with_model") or 0.0)
        lift = _sim_lift_from_disagreement(disagreement)
        if abs(lift) >= 0.1:
            try:
                cur = float(p.get("lock_score") or 0.0)
            except (TypeError, ValueError):
                cur = 0.0
            new_lock = max(0.0, min(99.0, cur + lift))
            p["lock_score"] = round(new_lock, 1)
            p["sim_lock_lift"] = round(lift, 2)
            if lift > 0:
                counts["lifted_up"] += 1
            else:
                counts["lifted_down"] += 1
    return counts
