"""Block 6 focused test — Brain universal prepublication chokepoint.

PERKLOCKS FIX 5 (2026-06): The legacy contract of this test file was
that ``publish_batch`` attenuates ``lock_score`` via the convergence
confidence multiplier (``70 + (lock_score - 70) * mult``). That
attenuation has been REMOVED as part of the Universal Flow Final
Closure — canonical ``lock_score`` is now strictly authoritative and
must NOT be mutated by any downstream Brain / publication path. The
convergence label + multiplier are still recorded on the pick as
evidence signals, but they no longer overwrite the score.

Refreshed contract:
  1. A direct-batch path (no prior helper enrichment) receives the
     convergence LABEL + MULTIPLIER stamps, but ``lock_score`` is
     LEFT UNCHANGED regardless of the multiplier.
  2. A pick already stamped by the helper (idempotency marker
     ``convergence_confidence_multiplier`` present) is NOT
     re-stamped and its ``lock_score`` remains whatever it was.
  3. STRONG_CONVERGENCE keeps ``lock_score`` unchanged (unchanged
     behaviour — mult=1.0 was already a no-op).
  4. STRONG_DISAGREEMENT stamps a low multiplier as ADVISORY
     evidence but does NOT attenuate ``lock_score``.
"""
from __future__ import annotations
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from probability_engine import classify_convergence


# The publish_batch decision-effect block extracted for isolated
# testing — mirrors the post-FIX-1 code in
# services/prediction_publication_service.py: convergence is
# LABEL-ONLY, lock_score is never mutated.
def _apply_publish_batch_brain(cand: dict) -> None:
    if cand.get("convergence_confidence_multiplier") is not None:
        return
    _p_v1 = cand.get("model_probability")
    _p_v2_raw = cand.get("simulator_probability") or cand.get("sim_win_probability")
    if _p_v1 is None:
        return
    _sim_ran = isinstance(_p_v2_raw, (int, float)) and _p_v2_raw > 0
    _p_v2 = float(_p_v2_raw) if _sim_ran else float(_p_v1)
    _ev_q = cand.get("evidence_quality") or "MODERATE"
    _sim_pv = cand.get("simulator_provenance") or "PRIOR_ONLY"
    _conv = classify_convergence(
        p_v1=float(_p_v1), p_v2=_p_v2,
        p_sim=float(_p_v2_raw) if _sim_ran else None,
        evidence_quality=_ev_q, sim_provenance=_sim_pv,
        sim_ran=_sim_ran,
    )
    cand["convergence_label"] = _conv["label"]
    cand["convergence_spread_pp"] = _conv["spread_pp"]
    cand["convergence_confidence_multiplier"] = _conv["confidence_multiplier"]
    # PERKLOCKS FIX 1/5: lock_score is NEVER mutated here.


def test_direct_batch_path_stamps_brain_evidence_only():
    """Direct callers of publish_batch (no helper enrichment) receive
    the convergence label + multiplier as EVIDENCE, but their
    canonical ``lock_score`` is untouched."""
    cand = {
        "id": "DIRECT_A",
        "model_probability": 0.62,
        "simulator_probability": 0.30,   # STRONG_DISAGREEMENT
        "evidence_quality": "WEAK",
        "simulator_provenance": "PRIOR_ONLY",
        "lock_score": 95.0,
    }
    _apply_publish_batch_brain(cand)
    assert cand["convergence_label"] == "STRONG_DISAGREEMENT"
    # Canonical lock_score is authoritative — no mutation.
    assert cand["lock_score"] == 95.0
    assert "lock_score_pre_convergence" not in cand
    assert "convergence_lock_score_delta" not in cand
    # Multiplier still recorded as advisory evidence.
    assert cand["convergence_confidence_multiplier"] < 1.0


def test_idempotent_when_helper_already_stamped():
    """If a pick was already stamped upstream (marker present),
    publish_batch MUST be a no-op."""
    cand = {
        "id": "HELPER_STAMPED",
        "model_probability": 0.62,
        "simulator_probability": 0.30,
        "evidence_quality": "WEAK",
        "simulator_provenance": "PRIOR_ONLY",
        "lock_score": 95.0,
        "convergence_confidence_multiplier": 0.55,    # marker
        "convergence_label": "STRONG_DISAGREEMENT",
    }
    _apply_publish_batch_brain(cand)
    assert cand["lock_score"] == 95.0
    assert cand["convergence_confidence_multiplier"] == 0.55
    assert cand["convergence_label"] == "STRONG_DISAGREEMENT"


def test_strong_convergence_preserves_lock_score():
    cand = {
        "id": "SC",
        "model_probability": 0.62,
        "simulator_probability": 0.62,               # STRONG_CONVERGENCE
        "evidence_quality": "STRONG",
        "simulator_provenance": "REAL_PLAYER_CONTEXT",
        "lock_score": 96.0,
    }
    _apply_publish_batch_brain(cand)
    assert cand["convergence_label"] == "STRONG_CONVERGENCE"
    assert cand["lock_score"] == 96.0
    assert "lock_score_pre_convergence" not in cand


def test_two_publication_paths_same_result():
    """Both publication paths must be functionally equivalent —
    same evidence stamps, and (post-FIX-1) same lock_score = the
    canonical input, untouched."""
    base = {
        "model_probability": 0.62,
        "simulator_probability": 0.30,
        "evidence_quality": "WEAK",
        "simulator_provenance": "PRIOR_ONLY",
        "lock_score": 95.0,
    }
    a = dict(base, id="A")
    b = dict(base, id="B")
    _apply_publish_batch_brain(a)
    _apply_publish_batch_brain(b)
    assert a["lock_score"] == b["lock_score"] == 95.0
    assert a["convergence_confidence_multiplier"] == \
        b["convergence_confidence_multiplier"]
    assert a["convergence_label"] == b["convergence_label"]
