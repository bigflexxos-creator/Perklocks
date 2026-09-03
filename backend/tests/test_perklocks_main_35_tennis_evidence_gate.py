"""PERKLOCKS-MAIN 35 · POST-CERT — TENNIS EVIDENCE GATE REACHABILITY.

Locks in the invariant that a Tennis pick carrying a real brain
calibration block (`brain.confidence_calibrated` + a real
`confidence_band_n`) counts as authoritative model evidence in
`board_validator.evidence_threshold(...)`.

The regression bug: when today's Tennis pipeline stopped populating
`lock_components` (bucket_n / ev_units) for the primary Odds-API path,
every Tennis pick fell to 2-of-3 signals and the entire Tennis board
went to 0. This test guarantees it cannot return.
"""
from __future__ import annotations
import pytest


def _tennis_pick(**overrides):
    base = {
        "id": "tennis-guard-1",
        "sport": "Tennis",
        "market": "Nikola Bartunkova Moneyline",
        "selection": "Nikola Bartunkova",
        "book_odds": -231,
        "edge_percent": 9.5,          # ≥1.5 → 1 signal
        "pick_rationale": {"summary": "real rationale", "evidence": ["x"]},
        "brain": {
            "version": "1.0.0",
            "confidence_calibrated": 0.6980,
            "confidence_band": "95-98",
            "confidence_band_expected": 0.80,
            "confidence_band_actual": 0.6980,
            "confidence_band_n": 872,   # >= 100 → 1 signal
        },
        # NOTE: `lock_components` intentionally absent (mirrors today's
        # Tennis pipeline output that triggered the regression).
    }
    base.update(overrides)
    return base


def test_tennis_pick_with_brain_calibration_passes_evidence_gate():
    from board_validator import evidence_threshold
    picks = [_tennis_pick()]
    kept, _stats = evidence_threshold(picks)
    assert len(kept) == 1, kept


def test_tennis_pick_without_brain_still_fails_gate():
    """A Tennis pick with NO brain calibration and no lock_components
    must still fail closed — we did NOT weaken the gate globally."""
    from board_validator import evidence_threshold
    p = _tennis_pick(brain=None)
    kept, _stats = evidence_threshold([p])
    # Only pick_rationale + edge_percent = 2 signals — below the 3-of-N
    # threshold.
    assert kept == []


def test_tennis_pick_with_only_weak_brain_still_fails():
    """Brain block without confidence_calibrated must not count as
    evidence."""
    from board_validator import evidence_threshold
    p = _tennis_pick(brain={"version": "1.0.0"})
    kept, _stats = evidence_threshold([p])
    assert kept == []


def test_non_tennis_pick_not_affected_by_tennis_evidence_branch():
    """MLB moneyline picks must not receive the Tennis evidence
    branch — regression guard against a sport-agnostic bug."""
    from board_validator import evidence_threshold
    p = _tennis_pick(sport="MLB", market="Yankees Moneyline",
                     brain={"confidence_calibrated": 0.65,
                            "confidence_band_n": 500})
    kept, _stats = evidence_threshold([p])
    # 2 signals (pick_rationale + edge_percent) — MLB does not get the
    # Tennis brain shortcut; must remain below the 3-of-N threshold.
    assert kept == []


def test_tennis_alt_total_pick_passes_gate():
    """Alt-total Tennis picks (Over/Under Games) must survive the
    evidence gate too — same brain provenance."""
    from board_validator import evidence_threshold
    p = _tennis_pick(
        market="Over 38.5 Games (Alt)",
        selection="Over",
        book_odds=-141,
        edge_percent=6.2,
        brain={
            "version": "1.0.0",
            "confidence_calibrated": 0.633,
            "confidence_band_n": 500,
        },
    )
    kept, _stats = evidence_threshold([p])
    assert len(kept) == 1
