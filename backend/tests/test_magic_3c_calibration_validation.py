"""MAGIC 3C — Calibration validation isolation + Phase 14/20 guards.

Static/unit tests proving:
* Shadow output (isotonic) is never read by production consumers.
* Magic evidence still reads only `db.calibrated_probabilities`.
* Double-calibration is prevented.
* Isotonic PAV monotonicity + clamp behavior.
* Reliability + Brier + log-loss + ECE metric implementations are
  correct (invariants).
"""
import math

import pytest


# ── Metric correctness ─────────────────────────────────────────────

def test_brier_score_correctness():
    from scripts.magic_3c_shadow_calibration import brier
    assert brier(0.5, 1) == pytest.approx(0.25)
    assert brier(0.5, 0) == pytest.approx(0.25)
    assert brier(1.0, 1) == pytest.approx(0.0)
    assert brier(0.0, 1) == pytest.approx(1.0)


def test_log_loss_correctness():
    from scripts.magic_3c_shadow_calibration import log_loss
    assert log_loss(0.5, 1) == pytest.approx(-math.log(0.5), rel=1e-9)
    assert log_loss(1.0, 1) < 1e-9
    # log_loss(0, 1) is very large (safe-clipped to log(1e-12))
    assert log_loss(0.0, 1) > 20


def test_ece_zero_for_perfect_calibration():
    from scripts.magic_3c_shadow_calibration import ece
    # A perfectly-calibrated system: p=0.5 for 100 picks, 50 win.
    preds = [0.5] * 100
    ys = [1] * 50 + [0] * 50
    # equal-bin ECE = |0.5 - 0.5| = 0 within the bin.
    assert ece(preds, ys) < 1e-9


# ── Isotonic PAV monotonicity ─────────────────────────────────────

def test_isotonic_output_is_monotone_non_decreasing():
    from scripts.magic_3c_shadow_calibration import IsotonicCurve
    xs = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]
    ys = [0, 1, 0, 1, 0, 1, 1, 1, 1]  # noisy
    iso = IsotonicCurve().fit(xs, ys)
    outs = [iso.predict(x) for x in [0.1, 0.3, 0.5, 0.7, 0.9]]
    for i in range(len(outs) - 1):
        assert outs[i] <= outs[i + 1] + 1e-9, (
            f"non-monotone at {i}: {outs[i]} > {outs[i+1]}"
        )


def test_isotonic_tail_clamp():
    """PAV clamp prevents 0% / 100% off tiny samples."""
    from scripts.magic_3c_shadow_calibration import IsotonicCurve
    xs = [0.05, 0.95]
    ys = [0, 1]
    iso = IsotonicCurve().fit(xs, ys)
    # Every knot y must be in [0.02, 0.98].
    for y in iso.ky:
        assert 0.02 <= y <= 0.98


def test_isotonic_identity_when_empty():
    from scripts.magic_3c_shadow_calibration import IsotonicCurve
    iso = IsotonicCurve()
    # No fit → identity.
    assert iso.predict(0.5) == 0.5


# ── Isolation: shadow never leaks to production ───────────────────

def test_magic_calibration_adapter_never_reads_shadow_collection():
    """Magic evidence adapter must query only
    `calibrated_probabilities`, never `calibration_shadow_evaluation`."""
    from services.magic.adapters.sim_cal import build_calibration_evidence
    import inspect
    src = inspect.getsource(build_calibration_evidence)
    assert "calibration_shadow_evaluation" not in src
    assert "shadow" not in src.lower()


def test_sim_cal_store_never_writes_to_shadow_collection():
    """Persistence layer must not write to shadow collection."""
    from services.magic import sim_cal_store as scs
    import inspect
    src = inspect.getsource(scs)
    # Production writes go to CALIBRATED_PROBABILITIES_COLLECTION only.
    assert scs.CALIBRATED_PROBABILITIES_COLLECTION == (
        "calibrated_probabilities")
    assert "shadow" not in src.lower()


def test_lock_score_reads_no_shadow_calibration():
    """Lock Score computation must not consult shadow output."""
    # Static check — no import of the shadow script or collection.
    import services.magic.magic_score as ms
    import inspect
    src = inspect.getsource(ms)
    assert "calibration_shadow_evaluation" not in src
    assert "magic_3c_shadow" not in src


# ── Phase 14: No double-calibration ────────────────────────────────

def test_no_double_calibration_in_pipeline():
    """The production path invokes `apply_calibration` exactly once
    per pick per refresh (via brain.pipeline.process_brain)."""
    import inspect
    from brain import pipeline as pl
    src = inspect.getsource(pl)
    # `apply_calibration(picks)` appears once in pipeline.
    assert src.count("apply_calibration(") == 1


def test_calibration_persist_hook_uses_brain_output_only():
    """MAGIC 3B persist hook reads pick['brain']['confidence_calibrated']
    and NEVER recomputes calibration or double-applies isotonic."""
    from services.magic import sim_cal_store as scs
    import inspect
    src = inspect.getsource(scs.build_calibration_doc)
    # No isotonic import inside the persistence path.
    assert "isotonic" not in src.lower()
    assert "IsotonicCurve" not in src


# ── Phase 15: versioning contract ─────────────────────────────────

def test_calibration_doc_carries_full_versioning():
    from services.magic.sim_cal_store import build_calibration_doc
    pick = {"id": "x", "sport": "MLB", "market": "over 1.5 hits",
            "line": 1.5, "side": "over"}
    brain = {"version": "1.0.0", "confidence_calibrated": 0.6,
             "confidence_band": "60-64", "confidence_band_n": 42}
    doc = build_calibration_doc(pick, brain)
    assert doc["calibration_method"] == "band_empirical"
    assert doc["calibration_version"] == "1.0.0"
    assert doc["input_fingerprint"] is not None
    assert doc["generated_at"] is not None


def test_isotonic_activation_would_require_new_version():
    """If isotonic is later activated, method must NOT be
    'band_empirical' — enforced at build_calibration_doc call sites."""
    from services.magic.sim_cal_store import build_calibration_doc
    # The current production method label is a code constant, not a
    # runtime free-string.  Verify by inspection.
    import inspect
    src = inspect.getsource(build_calibration_doc)
    assert 'method = "band_empirical"' in src or \
           "method = 'band_empirical'" in src


# ── Phase 12: high-confidence overconfidence is captured ──────────

def test_high_conf_bucket_ranges():
    from scripts.magic_3c_shadow_calibration import high_conf_bucket
    assert high_conf_bucket(0.99) == "95+"
    assert high_conf_bucket(0.92) == "90+"
    assert high_conf_bucket(0.87) == "85+"
    assert high_conf_bucket(0.80) is None


# ── Phase 20: Magic reads only production probability ─────────────

def test_magic_calibration_evidence_reads_production_collection():
    from services.magic.adapters.sim_cal import build_calibration_evidence
    import inspect
    src = inspect.getsource(build_calibration_evidence)
    assert "read_calibration" in src
    # And no path to lock_calibration or shadow.
    assert "lock_calibration" not in src
    assert "calibration_shadow_evaluation" not in src
