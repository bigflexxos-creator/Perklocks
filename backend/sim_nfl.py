"""NFL + CFB Monte Carlo simulator — foundation stub.

Goal: provide the same interface as `sim_mlb.py` / `sim_soccer.py` so
the Unified Probability Engine can call `simulate(pick)` for football
picks without erroring. Full 10k-run Monte Carlo (floor/median/ceiling
per market, with QB stats, weather, travel) lands in a follow-up
session — it's its own week of work.

Until then this stub returns a clean "did-not-run" signal:
    `{"ran": False, "reason": "nfl_simulator_pending_implementation"}`

The probability engine already handles `sim_ran=False` correctly
(falls back to v1↔v2 agreement for stability — see
probability_engine.py:174). So NFL/CFB picks blend cleanly through
v1+v2 today, and seamlessly gain sim signal when this file fills in.
"""

from __future__ import annotations

from typing import Optional


def simulate(pick: dict) -> dict:
    """Stub: report sim-not-yet-wired.

    Returns the same dict shape `sim_mlb.simulate` returns when it
    short-circuits — `{ran: bool, ...}`. The probability engine reads
    `pick.sim_win_probability` only when it's > 0, so this function
    intentionally leaves that field alone.
    """
    return {
        "ran": False,
        "reason": "nfl_simulator_pending_implementation",
        "sport": pick.get("sport"),
        "market": pick.get("market"),
    }


def supports(pick: dict) -> bool:
    """Whether this simulator claims jurisdiction over the pick.

    Returns True for NFL + CFB so the dispatcher knows to call us
    (vs. defaulting to v2 fallback for those sports). The actual
    simulation isn't implemented yet, but the routing is in place.
    """
    sport = (pick.get("sport") or "").upper()
    return sport in {"NFL", "CFB"}


__all__ = ["simulate", "supports"]
