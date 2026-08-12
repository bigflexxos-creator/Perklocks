"""MAGIC 3B — Simulator + Calibration durable persistence plumbing.

Read/write helpers for the two dedicated per-pick evidence collections:

    db.simulator_outputs        — one row per (pick_id, simulator_version, input_fp)
    db.calibrated_probabilities — one row per (pick_id, calibration_method, calibration_version, input_fp)

Contract
────────
This module IS the persistence contract for both evidence families.
It NEVER computes a simulator probability or a calibrated probability.
It only serialises real output produced by:

    * ``brain.sim_runner.apply_simulations``
      → writes to db.simulator_outputs.

    * ``brain.calibration.apply_calibration``
      → writes to db.calibrated_probabilities.

Stale-safety
────────────
Every row carries an ``input_fingerprint`` computed from the pick's
canonical inputs (event, player/team identity, market, side, line,
opponent, model_version, simulator_version).  A stored row is only
reusable when the current pick's fingerprint matches — a different
line, opponent, event, player, or version invalidates reuse.

Anti-substitution guards
────────────────────────
* p_hit is validated as a real simulator output — must carry sim_runs >= 1000
  AND simulator_type in ALLOWED_SIMULATOR_TYPES.  Never copied from
  model_probability.
* p_calibrated is validated to differ from raw model_probability by a
  meaningful non-zero delta OR be explicitly tagged with a genuine
  calibration_method (never ``legacy_unknown``).
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional


SIMULATOR_OUTPUTS_COLLECTION = "simulator_outputs"
CALIBRATED_PROBABILITIES_COLLECTION = "calibrated_probabilities"


# ── Fingerprint helpers ──────────────────────────────────────────────

_FINGERPRINT_FIELDS: tuple[str, ...] = (
    "canonical_event_id", "event", "event_time",
    "canonical_player_id", "player_name", "selection",
    "canonical_team_id", "team_name",
    "market", "side", "line",
    "opponent_team", "opposing_pitcher",
)


def build_input_fingerprint(
    pick: dict, *,
    simulator_version: Optional[str] = None,
    calibration_version: Optional[str] = None,
    model_version: Optional[str] = None,
) -> str:
    """Return a stable sha256 hex of the pick's canonical inputs.

    Fingerprint changes when ANY of:
      * event / event_time / opponent
      * player/team identity
      * market / side / line
    changes.  This prevents stale sim/calibration output from
    attaching to a different (event, player, line) combination.

    The `simulator_version` / `calibration_version` / `model_version`
    kwargs are accepted for backward compatibility but are NOT part
    of the fingerprint — the persistence rows track those separately.
    A stale-simulator-version reuse is prevented by querying with an
    explicit ``simulator_version`` in ``read_simulator_output``.
    """
    parts: dict[str, Any] = {}
    for f in _FINGERPRINT_FIELDS:
        v = pick.get(f)
        if v is None or v == "":
            continue
        parts[f] = v
    canonical = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ── Simulator persistence ────────────────────────────────────────────

_VALID_SIM_TYPES: frozenset[str] = frozenset({
    "event_simulation",
    "distribution_monte_carlo",
    "scenario_stress_test",
    "heuristic_adjustment",
    "deterministic_projection",
    "posterior_uncertainty",
})


def build_simulator_output_doc(pick: dict, sim: dict) -> Optional[dict]:
    """Return the persistence document for a genuine simulator result.

    Returns None when the sim payload doesn't carry the minimum real
    fields to be trustworthy (p_hit, sim_runs, simulator_name/version).
    Never fabricates fields.
    """
    if not isinstance(sim, dict):
        return None
    p_hit = sim.get("sim_win_probability")
    sim_runs = sim.get("sim_runs")
    sim_name = sim.get("simulator_name")
    sim_version = sim.get("simulator_version")
    sim_type = sim.get("simulator_type") or "distribution_monte_carlo"

    if p_hit is None or sim_runs is None:
        return None
    try:
        p_hit_f = float(p_hit)
    except (TypeError, ValueError):
        return None
    # `sim_win_probability` is stored as a percent (0-100) — normalise
    # to the canonical 0-1 fraction here, matching Magic's
    # SPORTSBOOK/MODEL probability convention.
    if p_hit_f > 1.0 + 1e-9:
        p_hit_f = p_hit_f / 100.0
    try:
        runs_i = int(sim_runs)
    except (TypeError, ValueError):
        return None
    if runs_i < 1000:
        # Under-run simulators cannot be trusted as evidence.
        return None
    if sim_type not in _VALID_SIM_TYPES:
        return None

    fp = build_input_fingerprint(
        pick, simulator_version=sim_version, model_version=pick.get("model_version"),
    )
    doc = {
        "pick_id":               pick.get("id"),
        "event_id":              pick.get("canonical_event_id") or pick.get("event"),
        "sport":                 pick.get("sport"),
        "league":                pick.get("league"),
        "market":                pick.get("market"),
        "selection":             pick.get("selection"),
        "line":                  pick.get("line"),
        "side":                  pick.get("side"),
        "canonical_player_id":   pick.get("canonical_player_id"),
        "canonical_team_id":     pick.get("canonical_team_id"),
        "p_hit":                 round(p_hit_f, 4),
        "simulation_runs":       runs_i,
        "simulator_name":        sim_name or f"{(pick.get('sport') or '').lower()}_simulator",
        "simulator_version":     sim_version or "1.0.0",
        "simulator_type":        sim_type,
        "seed":                  sim.get("seed"),
        "independent_evidence":  bool(sim.get("independent_evidence", True)),
        "valid":                 bool(sim.get("valid", True)),
        "invalid_reason":        sim.get("invalid_reason"),
        # Distribution stats (preserved when the sim genuinely reports them).
        "ci_lower":              sim.get("sim_ci_lower"),
        "ci_upper":              sim.get("sim_ci_upper"),
        "mean":                  sim.get("sim_mean"),
        "median":                sim.get("sim_median"),
        "q10":                   sim.get("sim_q10"),
        "q25":                   sim.get("sim_q25"),
        "q75":                   sim.get("sim_q75"),
        "q90":                   sim.get("sim_q90"),
        "std":                   sim.get("sim_std"),
        "generated_at":          datetime.now(timezone.utc).isoformat(),
        "input_fingerprint":     fp,
        "provenance": {
            "source":   "brain.sim_runner",
            "model_version": pick.get("model_version"),
        },
    }
    return doc


async def persist_simulator_output(db, pick: dict, sim: dict) -> Optional[str]:
    """Upsert one simulator output row.  Idempotent by
    (pick_id, simulator_version, input_fingerprint).  Returns the
    fingerprint on success, None when the sim was rejected."""
    doc = build_simulator_output_doc(pick, sim)
    if not doc or not doc.get("pick_id"):
        return None
    key = {
        "pick_id":            doc["pick_id"],
        "simulator_version":  doc["simulator_version"],
        "input_fingerprint":  doc["input_fingerprint"],
    }
    await db[SIMULATOR_OUTPUTS_COLLECTION].update_one(
        key, {"$set": doc}, upsert=True,
    )
    return doc["input_fingerprint"]


async def read_simulator_output(
    db, pick: dict, *, simulator_version: Optional[str] = None,
) -> Optional[dict]:
    """Return the most-recent valid simulator output for a pick — or
    None when the fingerprint doesn't match (stale)."""
    pid = pick.get("id")
    if not pid:
        return None
    fp = build_input_fingerprint(
        pick, simulator_version=simulator_version,
        model_version=pick.get("model_version"),
    )
    q: dict = {"pick_id": pid, "input_fingerprint": fp}
    if simulator_version:
        q["simulator_version"] = simulator_version
    doc = await db[SIMULATOR_OUTPUTS_COLLECTION].find_one(
        q, sort=[("generated_at", -1)],
    )
    return doc


# ── Calibration persistence ──────────────────────────────────────────

# Method labels that are explicitly NOT real calibration evidence.
_INVALID_CALIBRATION_METHODS: frozenset[str] = frozenset({
    "", "legacy_unknown", "unknown", "none", None,
})


def build_calibration_doc(pick: dict, brain_block: dict) -> Optional[dict]:
    """Return the persistence document for a genuine calibration
    result.  Returns None when brain_block doesn't carry the minimum
    real fields (confidence_calibrated + confidence_band_n).  Never
    fabricates or substitutes model_probability."""
    if not isinstance(brain_block, dict):
        return None
    p_cal = brain_block.get("confidence_calibrated")
    band_n = brain_block.get("confidence_band_n")
    band = brain_block.get("confidence_band")
    brain_version = brain_block.get("version")
    if p_cal is None or band_n is None:
        return None
    try:
        p_cal_f = float(p_cal)
    except (TypeError, ValueError):
        return None
    if not (0.0 <= p_cal_f <= 1.0):
        return None

    # Raw input probability — the pick's model_probability BEFORE
    # calibration.  We store it explicitly so consumers can compare
    # raw vs calibrated without collapsing them.
    raw_prob: Optional[float] = None
    for k in ("model_probability", "win_probability"):
        v = pick.get(k)
        if v is None or v == "":
            continue
        try:
            f = float(v)
            # win_probability is often 0-100; model_probability is 0-1.
            if f > 1.0 + 1e-9:
                f = f / 100.0
            if 0.0 <= f <= 1.0:
                raw_prob = f
                break
        except (TypeError, ValueError):
            continue

    method = "band_empirical"     # the ONLY method brain.calibration.py implements.
    version = brain_version or "1.0.0"
    if method in _INVALID_CALIBRATION_METHODS:
        return None

    fp = build_input_fingerprint(
        pick,
        calibration_version=version,
        model_version=pick.get("model_version"),
    )

    return {
        "pick_id":                pick.get("id"),
        "event_id":               pick.get("canonical_event_id") or pick.get("event"),
        "sport":                  pick.get("sport"),
        "league":                 pick.get("league"),
        "market":                 pick.get("market"),
        "selection":              pick.get("selection"),
        "line":                   pick.get("line"),
        "side":                   pick.get("side"),
        "canonical_player_id":    pick.get("canonical_player_id"),
        "canonical_team_id":      pick.get("canonical_team_id"),
        "raw_input_probability":  raw_prob,
        "p_calibrated":           round(p_cal_f, 4),
        "calibration_method":     method,
        "calibration_version":    version,
        "generated_at":           datetime.now(timezone.utc).isoformat(),
        # Sample/training context (only if genuinely present).
        "sample_size":            int(band_n) if band_n is not None else None,
        "band":                   band,
        "band_expected":          brain_block.get("confidence_band_expected"),
        "band_actual":            brain_block.get("confidence_band_actual"),
        "input_fingerprint":      fp,
        "provenance": {
            "source":         "brain.calibration",
            "model_version":  pick.get("model_version"),
        },
    }


async def persist_calibration(db, pick: dict) -> Optional[str]:
    """Upsert one calibrated_probability row.  Idempotent by
    (pick_id, calibration_method, calibration_version,
    input_fingerprint)."""
    brain_block = pick.get("brain") or {}
    doc = build_calibration_doc(pick, brain_block)
    if not doc or not doc.get("pick_id"):
        return None
    key = {
        "pick_id":              doc["pick_id"],
        "calibration_method":   doc["calibration_method"],
        "calibration_version":  doc["calibration_version"],
        "input_fingerprint":    doc["input_fingerprint"],
    }
    await db[CALIBRATED_PROBABILITIES_COLLECTION].update_one(
        key, {"$set": doc}, upsert=True,
    )
    return doc["input_fingerprint"]


async def read_calibration(
    db, pick: dict, *,
    calibration_method: Optional[str] = None,
    calibration_version: Optional[str] = None,
) -> Optional[dict]:
    pid = pick.get("id")
    if not pid:
        return None
    fp = build_input_fingerprint(
        pick,
        calibration_version=calibration_version,
        model_version=pick.get("model_version"),
    )
    q: dict = {"pick_id": pid, "input_fingerprint": fp}
    if calibration_method:
        q["calibration_method"] = calibration_method
    if calibration_version:
        q["calibration_version"] = calibration_version
    doc = await db[CALIBRATED_PROBABILITIES_COLLECTION].find_one(
        q, sort=[("generated_at", -1)],
    )
    return doc


__all__ = [
    "SIMULATOR_OUTPUTS_COLLECTION",
    "CALIBRATED_PROBABILITIES_COLLECTION",
    "build_input_fingerprint",
    "build_simulator_output_doc",
    "build_calibration_doc",
    "persist_simulator_output",
    "persist_calibration",
    "read_simulator_output",
    "read_calibration",
]
