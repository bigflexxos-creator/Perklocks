"""MAGIC 3B — Simulator + Calibration → Magic adapter.

Reads persisted per-pick simulator and calibration outputs from the
dedicated collections (see `services.magic.sim_cal_store`) and emits
distinct `EvidenceItem`s:

    EvidenceType.SIMULATOR_PROBABILITY
    EvidenceType.CALIBRATED_PROBABILITY

These are NEVER computed by this adapter — it only surfaces already-
persisted evidence.  Missing evidence yields ``Availability.UNAVAILABLE``.

Guardrails (per Magic 3B directive)
───────────────────────────────────
* Never substitute model_probability for simulator_probability.
* Never substitute model_probability for p_calibrated.
* Never emit an evidence item with a fabricated method/version.
* A stale-fingerprint pick returns UNAVAILABLE (stale ≠ available).
"""
from __future__ import annotations

from typing import Optional

from services.magic.contract import (
    EvidenceItem, EvidenceType, Availability,
)
from services.magic.sim_cal_store import (
    read_simulator_output, read_calibration,
)


async def build_simulator_evidence(db, pick: dict) -> EvidenceItem:
    """Return SIMULATOR_PROBABILITY evidence — reads persisted row."""
    doc = await read_simulator_output(db, pick)
    if doc is None:
        return EvidenceItem(
            evidence_type=EvidenceType.SIMULATOR_PROBABILITY,
            availability=Availability.UNAVAILABLE,
            sport=pick.get("sport") or "",
            market=pick.get("market"),
            selection=pick.get("selection"),
            line=pick.get("line"),
            notes="no persisted simulator output for this pick fingerprint",
            provenance={"reason": "no_persisted_simulator_output"},
        )
    p_hit = doc.get("p_hit")
    valid = bool(doc.get("valid", True))
    if p_hit is None or not valid:
        return EvidenceItem(
            evidence_type=EvidenceType.SIMULATOR_PROBABILITY,
            availability=Availability.UNAVAILABLE,
            sport=pick.get("sport") or "",
            market=pick.get("market"),
            selection=pick.get("selection"),
            line=doc.get("line"),
            notes=doc.get("invalid_reason") or "simulator marked invalid",
            provenance={"reason": "sim_invalid"},
        )
    # Direction ("positive"/"negative"/"neutral") is derived from the
    # sim probability threshold.  This is a display hint only —
    # never used to substitute model_probability.
    if p_hit >= 0.55:
        direction = "positive"
    elif p_hit <= 0.45:
        direction = "negative"
    else:
        direction = "neutral"

    return EvidenceItem(
        evidence_type=EvidenceType.SIMULATOR_PROBABILITY,
        availability=Availability.AVAILABLE,
        sport=doc.get("sport") or "",
        league=doc.get("league"),
        market=doc.get("market"),
        selection=doc.get("selection"),
        line=doc.get("line"),
        canonical_player_id=doc.get("canonical_player_id"),
        canonical_team_id=doc.get("canonical_team_id"),
        value=float(p_hit),
        direction=direction,
        sample_size=int(doc.get("simulation_runs") or 0),
        source=str(doc.get("simulator_name") or "simulator"),
        timestamp=doc.get("generated_at"),
        provenance={
            "simulator_name":       doc.get("simulator_name"),
            "simulator_version":    doc.get("simulator_version"),
            "simulator_type":       doc.get("simulator_type"),
            "simulation_runs":      doc.get("simulation_runs"),
            "seed":                 doc.get("seed"),
            "independent_evidence": doc.get("independent_evidence"),
            "input_fingerprint":    doc.get("input_fingerprint"),
            "ci_lower":             doc.get("ci_lower"),
            "ci_upper":             doc.get("ci_upper"),
            "generated_at":         doc.get("generated_at"),
            "source":               "db.simulator_outputs",
        },
    )


async def build_calibration_evidence(db, pick: dict) -> EvidenceItem:
    """Return CALIBRATED_PROBABILITY evidence — reads persisted row."""
    doc = await read_calibration(db, pick)
    if doc is None:
        return EvidenceItem(
            evidence_type=EvidenceType.CALIBRATED_PROBABILITY,
            availability=Availability.UNAVAILABLE,
            sport=pick.get("sport") or "",
            market=pick.get("market"),
            selection=pick.get("selection"),
            line=pick.get("line"),
            notes="no persisted calibrated probability for this pick fingerprint",
            provenance={"reason": "no_persisted_calibration"},
        )
    p_cal = doc.get("p_calibrated")
    method = doc.get("calibration_method")
    if p_cal is None or not method or method in ("legacy_unknown", ""):
        return EvidenceItem(
            evidence_type=EvidenceType.CALIBRATED_PROBABILITY,
            availability=Availability.UNAVAILABLE,
            sport=pick.get("sport") or "",
            market=pick.get("market"),
            line=doc.get("line"),
            notes="calibration output missing method/version or p_calibrated",
            provenance={"reason": "invalid_calibration_metadata",
                          "calibration_method": method},
        )
    return EvidenceItem(
        evidence_type=EvidenceType.CALIBRATED_PROBABILITY,
        availability=Availability.AVAILABLE,
        sport=doc.get("sport") or "",
        league=doc.get("league"),
        market=doc.get("market"),
        selection=doc.get("selection"),
        line=doc.get("line"),
        canonical_player_id=doc.get("canonical_player_id"),
        canonical_team_id=doc.get("canonical_team_id"),
        value=float(p_cal),
        sample_size=doc.get("sample_size"),
        source=str(method),
        timestamp=doc.get("generated_at"),
        provenance={
            "calibration_method":   method,
            "calibration_version":  doc.get("calibration_version"),
            "raw_input_probability": doc.get("raw_input_probability"),
            "band":                 doc.get("band"),
            "band_expected":        doc.get("band_expected"),
            "band_actual":          doc.get("band_actual"),
            "sample_size":          doc.get("sample_size"),
            "input_fingerprint":    doc.get("input_fingerprint"),
            "generated_at":         doc.get("generated_at"),
            "source":               "db.calibrated_probabilities",
        },
    )


__all__ = [
    "build_simulator_evidence",
    "build_calibration_evidence",
]
