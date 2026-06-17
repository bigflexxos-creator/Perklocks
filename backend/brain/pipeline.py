"""Brain Pipeline Orchestrator.

Single public entry point — call once at the end of every pick refresh:

    from brain import process_brain
    summary = await process_brain(picks, db)

Responsibilities (in strict order):
  1. Refresh / fetch the Prediction Memory snapshot (cached 5min).
  2. Apply Confidence Calibration to every pick.
  3. Run the Candidate Ranker (composite score 0..1 per pick).
  4. Run the hidden Monte Carlo simulator on top-K candidates.
  5. Apply the Decision Filter (sets `no_bet=True` on PASS picks).

Side effects ONLY on the input picks list; returns a summary dict for
logging. Failure-tolerant: any step that raises is logged but the rest
of the pipeline continues so a bad pick never poisons the slate.
"""
from __future__ import annotations

import logging
import time

from .memory import get_or_build_memory, invalidate_memory
from .calibration import apply_calibration
from .candidates import rank_candidates
from .simulator import run_simulator
from .filter import decision_filter

logger = logging.getLogger("lockscore.brain")

BRAIN_VERSION = "1.0.0"


async def process_brain(picks: list[dict], db) -> dict:
    """Run the full brain pipeline on a freshly-built pick slate."""
    if not picks:
        return {"version": BRAIN_VERSION, "empty": True}

    summary: dict = {"version": BRAIN_VERSION, "n_picks": len(picks), "steps": {}}
    t0 = time.monotonic()

    # Tag every pick with the brain version so analytics can isolate clean
    # samples once we iterate.
    for p in picks:
        b = p.setdefault("brain", {})
        b["version"] = BRAIN_VERSION

    try:
        memory = await get_or_build_memory(db)
        summary["settled_total"] = memory.settled_total
        summary["global_win_rate"] = round(memory.global_win_rate, 3)
        summary["global_roi_pct"] = round(memory.global_roi, 2)
    except Exception as e:
        logger.warning("brain memory unavailable, skipping pipeline: %s", e)
        return summary | {"error": str(e)}

    # 1. Calibration
    try:
        summary["steps"]["calibration"] = apply_calibration(picks, memory)
    except Exception as e:
        logger.warning("calibration step failed: %s", e)
        summary["steps"]["calibration"] = {"error": str(e)}

    # 2. Ranking
    try:
        summary["steps"]["candidates"] = rank_candidates(picks, memory)
    except Exception as e:
        logger.warning("ranker step failed: %s", e)
        summary["steps"]["candidates"] = {"error": str(e)}

    # 3. Monte Carlo (top-K only)
    try:
        summary["steps"]["simulator"] = run_simulator(picks, memory)
    except Exception as e:
        logger.warning("simulator step failed: %s", e)
        summary["steps"]["simulator"] = {"error": str(e)}

    # 4. Decision filter (PASS verdict)
    try:
        summary["steps"]["filter"] = decision_filter(picks, memory)
    except Exception as e:
        logger.warning("filter step failed: %s", e)
        summary["steps"]["filter"] = {"error": str(e)}

    summary["elapsed_ms"] = round((time.monotonic() - t0) * 1000, 1)
    logger.info(
        "Brain v%s: %d picks · keep=%d pass=%d elite_override=%d · %dms",
        BRAIN_VERSION, len(picks),
        summary["steps"].get("filter", {}).get("KEEP", 0),
        summary["steps"].get("filter", {}).get("PASS", 0),
        summary["steps"].get("filter", {}).get("elite_override", 0),
        summary["elapsed_ms"],
    )
    return summary


async def on_settlement(db) -> None:
    """Hook the settlement scheduler calls so the memory rebuilds on the
    next pick refresh — cheap (just invalidates cache)."""
    await invalidate_memory()
