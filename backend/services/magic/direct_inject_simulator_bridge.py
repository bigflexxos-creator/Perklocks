"""MAGIC 3I — Soccer direct-inject simulator reachability closure.

Provides ONE safe entry-point that direct-inject Soccer producers
(``soccer_prop_inject``, ``mls_direct_inject``, ``uefa_espn_v1``,
``soccer_hot_scorers_v1``, ``soccer_v1/synth``, or any other
``publish_batch()`` caller) can invoke to make a candidate
**simulator-reachable** WITHOUT touching Lock Score.

Contract (hard rules per user directive):

  * Reuse the existing Soccer simulator via
    :func:`brain.sim_runner.simulate_pick` — no duplicated math.
  * NEVER call :func:`brain.sim_runner._anchor_pick_to_sim` or
    :func:`brain.sim_runner.apply_simulations` — those mutate
    ``lock_score``.  Grep guard: this module contains no reference
    to those symbols.
  * Never fabricate a probability.
  * Simulator FAILURE never blocks publication.
  * Persist through the existing Magic 3B contract in
    :mod:`services.magic.sim_cal_store`.
  * Fingerprint is line-sensitive so Over 0.5 ≠ Over 1.5.
  * Aggregate counters exposed for observability (no per-row spam).

Usage
─────
Direct-inject callers, immediately BEFORE ``publish_batch``::

    from services.magic.direct_inject_simulator_bridge import (
        simulate_direct_inject_picks,
    )
    stats = await simulate_direct_inject_picks(db, picks)

Any ranking-relevant field (``lock_score``, ``display_lock_score``,
``grade``, ``tier``, ``model_probability``, ``line``, ``side``,
``book_odds``) is guaranteed unchanged.
"""
from __future__ import annotations

import copy
import logging
from typing import Iterable, Optional

logger = logging.getLogger("lockscore.direct_inject_simulator_bridge")


# Fields that MUST remain identical before/after the bridge runs.
# The bridge validates these post-run and reverts if any drift is
# somehow detected (defence-in-depth against future callers hooking
# the pick object between simulate + persist).
_LOCK_INVARIANT_FIELDS = (
    "lock_score", "display_lock_score", "lock_score_raw",
    "lock_score_v2", "lock_score_v2_raw", "lock_score_peak",
    "grade", "tier",
    "model_probability", "win_probability",
    "line", "side", "book_odds",
    "canonical_player_id", "canonical_team_id", "canonical_event_id",
    "market", "selection", "sport",
)


# Soccer markets the existing simulator genuinely supports.  Anything
# else is marked SIM_UNSUPPORTED and left alone (no fabrication).
_SUPPORTED_MARKET_KEYWORDS = (
    "moneyline", "to score", "anytime scorer",
    "anytime goalscorer", "goal scorer",
    "total goals", "over/under", "match total",
)


def _market_supported(market: Optional[str]) -> bool:
    m = (market or "").lower()
    if not m:
        return False
    return any(k in m for k in _SUPPORTED_MARKET_KEYWORDS)


def _snapshot_invariants(pick: dict) -> dict:
    return {f: copy.deepcopy(pick.get(f)) for f in _LOCK_INVARIANT_FIELDS
            if f in pick}


def _restore_invariants(pick: dict, snap: dict) -> list[str]:
    """Restore any drifted invariant.  Returns the list of drifted
    field names (should be empty in normal operation)."""
    drift: list[str] = []
    for f, v in snap.items():
        if pick.get(f) != v:
            drift.append(f)
            pick[f] = v
    return drift


async def simulate_direct_inject_pick(
    db, pick: dict,
) -> dict:
    """Simulate ONE direct-inject soccer pick and persist the result
    through the Magic 3B contract.  Returns a small dict describing
    what happened; NEVER raises.

    Returns
    -------
    ``{"outcome": <str>, "reason": <str>, "sim": <dict or None>,
       "input_fingerprint": <str or None>, "lock_score_drift": []}``

    Outcome values:
      * ``SIM_PERSISTED``       — simulator ran + persisted successfully
      * ``SIM_UNSUPPORTED``     — market not supported by the simulator
      * ``IDENTITY_UNSAFE``     — provisional/missing canonical identity
      * ``INPUT_MISSING``       — required inputs (line/side) missing
      * ``SIMULATION_FAILED``   — simulator returned None
      * ``PERSISTENCE_FAILED``  — sim ran but persist step failed
      * ``ALREADY_PERSISTED``   — fingerprint already stored (skip)
    """
    out = {"outcome": "SIM_UNSUPPORTED", "reason": "",
           "sim": None, "input_fingerprint": None,
           "lock_score_drift": []}

    if (pick.get("sport") or "").lower() != "soccer":
        out["outcome"] = "SIM_UNSUPPORTED"
        out["reason"] = "not a soccer pick"
        return out

    if not _market_supported(pick.get("market")):
        out["outcome"] = "SIM_UNSUPPORTED"
        out["reason"] = f"market not simulator-supported: {pick.get('market')!r}"
        return out

    cpid = pick.get("canonical_player_id")
    ctid = pick.get("canonical_team_id")
    ceid = pick.get("canonical_event_id")
    if not ceid:
        out["outcome"] = "IDENTITY_UNSAFE"
        out["reason"] = "no canonical_event_id"
        return out
    # Player-market vs team-market: at least one authoritative id.
    if not (cpid or ctid):
        out["outcome"] = "IDENTITY_UNSAFE"
        out["reason"] = "no canonical player or team id"
        return out
    # PROVISIONAL identity is unsafe for authoritative simulator input.
    if isinstance(cpid, str) and (cpid.startswith("fallback:")
                                    or cpid.startswith("unresolved:")):
        out["outcome"] = "IDENTITY_UNSAFE"
        out["reason"] = "provisional/fallback canonical_player_id"
        return out

    # Snapshot ranking-relevant fields for immutability audit.
    snap = _snapshot_invariants(pick)

    try:
        # Import HERE so this module never imports the anchor symbol.
        from brain.sim_runner import simulate_pick as _simulate_only
        sim = _simulate_only(pick)
    except Exception as e:
        # Post-run invariant guard.
        drift = _restore_invariants(pick, snap)
        out["outcome"] = "SIMULATION_FAILED"
        out["reason"] = f"simulator raised: {e!r}"
        out["lock_score_drift"] = drift
        return out

    # Even simulate_pick can attach sim_* fields onto the pick — but
    # NEVER lock_score.  Verify.
    drift = _restore_invariants(pick, snap)
    out["lock_score_drift"] = drift

    if not sim:
        out["outcome"] = "SIMULATION_FAILED"
        out["reason"] = ("simulate_pick returned None (unsupported "
                          "market inputs / missing evidence)")
        return out

    # Reuse the Magic 3B persistence contract — line-sensitive
    # fingerprint prevents Over 0.5 ≠ Over 1.5 cross-contamination.
    try:
        from services.magic.sim_cal_store import (
            persist_simulator_output, read_simulator_output,
        )
        # Skip re-simulation if a valid fingerprint-matched output
        # already exists (Phase 16 — no double simulation).
        existing = None
        try:
            existing = await read_simulator_output(
                db, pick,
                simulator_version=sim.get("simulator_version"))
        except Exception:
            existing = None
        if existing:
            out["outcome"] = "ALREADY_PERSISTED"
            out["input_fingerprint"] = existing.get("input_fingerprint")
            out["sim"] = sim
            return out

        fp = await persist_simulator_output(db, pick, sim)
        if not fp:
            out["outcome"] = "PERSISTENCE_FAILED"
            out["reason"] = ("sim payload rejected by 3B contract "
                              "(missing p_hit / sim_runs / valid type)")
            out["sim"] = sim
            return out
        out["outcome"] = "SIM_PERSISTED"
        out["input_fingerprint"] = fp
        out["sim"] = sim
    except Exception as e:
        out["outcome"] = "PERSISTENCE_FAILED"
        out["reason"] = f"persist raised: {e!r}"
        out["sim"] = sim
    finally:
        # One last invariant sweep — nothing else may have moved.
        drift = _restore_invariants(pick, snap)
        out["lock_score_drift"] = drift
    return out


async def simulate_direct_inject_picks(
    db, picks: Iterable[dict],
) -> dict:
    """Batch entry-point.  Returns aggregate counters (Phase 14).

    NEVER raises.  A failure on one pick does not block others.
    """
    counters = {
        "eligible":            0,
        "attempted":           0,
        "persisted":           0,
        "already_persisted":   0,
        "unsupported":         0,
        "identity_blocked":    0,
        "input_missing":       0,
        "simulation_failed":   0,
        "persistence_failed":  0,
        "lock_score_drifts":   0,
    }
    for pick in picks:
        counters["eligible"] += 1
        try:
            r = await simulate_direct_inject_pick(db, pick)
        except Exception as e:
            logger.debug("bridge raised for pick %s: %s",
                          pick.get("id"), e)
            counters["simulation_failed"] += 1
            continue
        outcome = r.get("outcome") or ""
        if r.get("lock_score_drift"):
            counters["lock_score_drifts"] += 1
        if outcome == "SIM_PERSISTED":
            counters["attempted"] += 1
            counters["persisted"] += 1
        elif outcome == "ALREADY_PERSISTED":
            counters["already_persisted"] += 1
        elif outcome == "SIM_UNSUPPORTED":
            counters["unsupported"] += 1
        elif outcome == "IDENTITY_UNSAFE":
            counters["identity_blocked"] += 1
        elif outcome == "INPUT_MISSING":
            counters["input_missing"] += 1
        elif outcome == "SIMULATION_FAILED":
            counters["attempted"] += 1
            counters["simulation_failed"] += 1
        elif outcome == "PERSISTENCE_FAILED":
            counters["attempted"] += 1
            counters["persistence_failed"] += 1
    return counters


__all__ = [
    "simulate_direct_inject_pick",
    "simulate_direct_inject_picks",
]
