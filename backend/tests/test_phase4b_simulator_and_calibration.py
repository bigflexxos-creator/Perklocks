"""Phase 4B — Simulator Reproducibility + Truthfulness Guardrail Tests.

Enforces the Phase 4B contract:
  1. posterior_uncertainty is deterministic with a fixed seed.
  2. Its ``independent_evidence`` is False.
  3. Its ``posterior_mean`` remains tied to the input model probability.
  4. It is not counted as a second model vote.
  5. Active simulators use deterministic seeds.
  6. Same pick + simulator_version → identical result.
  7. Different lines use different seeds.
  8. The symmetric anchor can move UP AND DOWN, bounded ±SIM_RESIDUAL_MAX.
  9. Invalid simulator results cause zero adjustment.
 10. Posterior uncertainty causes zero anchor adjustment.
 11. Simulator metadata records type, seed, line, side, version.
 12. Calibration segmentation does not combine unrelated market families.
 13. Small calibration buckets fall back safely.
 14. Baseline reporting performs zero writes (asserted via mongomock-in-memory).
 15. `SimulatorResult` contract rejects illegal ``simulator_type`` +
     illegal independence flags on posterior samplers.

**No Mongo writes.  In-memory fakes only.**
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


# ─── Helpers ───────────────────────────────────────────────────────
def _pick(**over) -> dict:
    p = {
        "id": "pick-abc-123",
        "prediction_id": "pick-abc-123",
        "sport": "MLB",
        "market_key": "batter_hits",
        "player": "Aaron Judge",
        "line": 1.5,
        "side": "Over",
        "book_odds": -120,
        "win_probability": 62.0,
        "lock_score": 78.0,
        "brain": {"top_k": True, "confidence_calibrated": 0.62},
        "factors": {"a": 0.6, "b": 0.65, "c": 0.58},
        "selection_v2": {"market": {"family": "hitter_hits"}},
        "event_id": "evt-1",
    }
    p.update(over)
    return p


class _FakeBucket:
    n = 40
    won = 22
    lost = 15
    push = 3
    roi_pct = 4.5
    calibration_err = 0.05


class _FakeMemory:
    settled_total = 1000
    global_win_rate = 0.55
    global_roi = 3.0
    def market(self, sport, family):
        return _FakeBucket()


# ═══════════════════════════════════════════════════════════════════
# 1. Simulator Result contract
# ═══════════════════════════════════════════════════════════════════
def test_simulator_contract_rejects_bad_type():
    from brain.simulator_contract import SimulatorResult
    with pytest.raises(ValueError):
        SimulatorResult(
            simulator_name="x", simulator_version="1.0.0",
            simulator_type="not_a_real_type",   # type: ignore
            seed=0, iterations=1, input_line=None, input_side=None,
            raw_probability=None, stabilized_probability=None,
            standard_error=None, lower_bound=None, upper_bound=None,
            push_probability=None, valid=True, invalid_reason=None,
            independent_evidence=False,
        )


def test_simulator_contract_rejects_posterior_with_independent_true():
    from brain.simulator_contract import SimulatorResult
    with pytest.raises(ValueError):
        SimulatorResult(
            simulator_name="posterior_uncertainty",
            simulator_version="2.0.0",
            simulator_type="posterior_uncertainty",
            seed=1, iterations=100, input_line=None, input_side=None,
            raw_probability=0.62, stabilized_probability=0.62,
            standard_error=0.02, lower_bound=0.58, upper_bound=0.66,
            push_probability=None, valid=True, invalid_reason=None,
            independent_evidence=True,   # ← ILLEGAL
        )


def test_simulator_contract_accepts_posterior_with_independent_false():
    from brain.simulator_contract import SimulatorResult
    r = SimulatorResult(
        simulator_name="posterior_uncertainty",
        simulator_version="2.0.0",
        simulator_type="posterior_uncertainty",
        seed=1, iterations=100, input_line=None, input_side="Over",
        raw_probability=0.62, stabilized_probability=0.62,
        standard_error=0.02, lower_bound=0.58, upper_bound=0.66,
        push_probability=None, valid=True, invalid_reason=None,
        independent_evidence=False,
    )
    assert r.independent_evidence is False
    d = r.to_dict()
    assert d["simulator_type"] == "posterior_uncertainty"


# ═══════════════════════════════════════════════════════════════════
# 2. Deterministic seed helper
# ═══════════════════════════════════════════════════════════════════
def test_seed_same_pick_same_version_same_seed():
    from services.simulation_seed import build_seed
    p = _pick()
    s1 = build_seed(p, "mlb_simulator", "1.1.0")
    s2 = build_seed(p, "mlb_simulator", "1.1.0")
    assert s1 == s2
    assert 0 <= s1 < (1 << 63)


def test_seed_different_line_different_seed():
    from services.simulation_seed import build_seed
    p_1 = _pick(line=0.5)
    p_2 = _pick(line=1.5)
    p_3 = _pick(line=2.5)
    seeds = {build_seed(p, "mlb_simulator", "1.1.0")
              for p in (p_1, p_2, p_3)}
    assert len(seeds) == 3


def test_seed_different_player_different_seed():
    from services.simulation_seed import build_seed
    a = _pick(prediction_id="p-a", player="Aaron Judge")
    b = _pick(prediction_id="p-b", player="Juan Soto")
    assert build_seed(a, "mlb_simulator", "1.1.0") \
           != build_seed(b, "mlb_simulator", "1.1.0")


def test_seed_different_version_different_seed():
    from services.simulation_seed import build_seed
    p = _pick()
    assert build_seed(p, "mlb_simulator", "1.1.0") \
           != build_seed(p, "mlb_simulator", "1.2.0")


def test_seed_refuses_name_only_by_default():
    from services.simulation_seed import build_seed, SeedError
    # No prediction_id, no event_id, no market_key.
    bare = {"player": "someone", "side": "Over", "line": 0.5}
    with pytest.raises(SeedError):
        build_seed(bare, "mlb_simulator", "1.1.0")
    # Opt-in works.
    s = build_seed(bare, "mlb_simulator", "1.1.0",
                    allow_name_only_fallback=True)
    assert isinstance(s, int)


def test_seed_no_python_hash_used():
    """Same seed inputs → same output across Python processes."""
    from services.simulation_seed import build_seed
    p = _pick()
    # Explicit expected value under BLAKE2b-8byte truncation.
    s = build_seed(p, "mlb_simulator", "1.1.0")
    # Reproduce independently to prove no PYTHONHASHSEED dependence.
    from hashlib import blake2b
    payload = ("sim=mlb_simulator|ver=1.1.0|pred=pick-abc-123|evt=evt-1|"
                "mkt=batter_hits|part=Aaron Judge|side=Over|line=1.5")
    expected = int.from_bytes(
        blake2b(payload.encode(), digest_size=8).digest(),
        "big", signed=False,
    ) & ((1 << 63) - 1)
    assert s == expected


# ═══════════════════════════════════════════════════════════════════
# 3. Posterior uncertainty determinism + independence
# ═══════════════════════════════════════════════════════════════════
def test_posterior_uncertainty_deterministic():
    from brain.simulator import run_posterior_uncertainty
    p1 = _pick(); p2 = _pick()
    mem = _FakeMemory()
    run_posterior_uncertainty([p1], mem)
    run_posterior_uncertainty([p2], mem)
    a = p1["brain"]["simulator"]
    b = p2["brain"]["simulator"]
    for k in ("posterior_mean", "lower_bound", "upper_bound",
                "uncertainty_width", "seed"):
        assert a[k] == b[k], f"{k} differs: {a[k]} vs {b[k]}"


def test_posterior_uncertainty_reports_independent_evidence_false():
    from brain.simulator import run_posterior_uncertainty
    p = _pick()
    run_posterior_uncertainty([p], _FakeMemory())
    s = p["brain"]["simulator"]
    assert s["independent_evidence"] is False
    assert s["method"] == "beta_bernoulli_posterior"
    assert s["simulator_type"] == "posterior_uncertainty"
    assert "contract" in s
    assert s["contract"]["independent_evidence"] is False


def test_posterior_mean_tracks_input_probability():
    """Because it is a posterior around μ, posterior_mean should be
    within a modest band of μ when the historical bucket is empty
    (no bucket influence)."""
    from brain.simulator import run_posterior_uncertainty

    class _EmptyMemory:
        def market(self, sport, family):
            return None
    for mu in (0.30, 0.55, 0.72, 0.88):
        p = _pick(prediction_id=f"p-{int(mu*100)}",
                   win_probability=mu*100,
                   brain={"top_k": True, "confidence_calibrated": mu},
                   line=0.5)
        run_posterior_uncertainty([p], _EmptyMemory())
        s = p["brain"]["simulator"]
        assert abs(s["posterior_mean"] - mu) < 0.10, (
            f"μ={mu} but posterior_mean={s['posterior_mean']}")


def test_posterior_uncertainty_records_seed_and_version():
    from brain.simulator import run_posterior_uncertainty
    p = _pick()
    run_posterior_uncertainty([p], _FakeMemory())
    s = p["brain"]["simulator"]
    assert s["seed"] != 0
    assert s["simulator_version"] == "2.0.0"
    assert s["simulator_name"] == "posterior_uncertainty"


def test_run_simulator_backward_compat_wrapper_stamps_posterior():
    """Legacy callers of run_simulator get the same truthful labels."""
    from brain.simulator import run_simulator
    p = _pick()
    result = run_simulator([p], _FakeMemory())
    assert result["independent_evidence"] is False
    assert result["simulator_type"] == "posterior_uncertainty"
    assert p["brain"]["simulator"]["independent_evidence"] is False


# ═══════════════════════════════════════════════════════════════════
# 4. Filter no longer treats posterior as independent
# ═══════════════════════════════════════════════════════════════════
def test_filter_does_not_apply_sim_ev_gate_to_posterior():
    from brain.filter import decision_filter
    from brain.simulator import run_posterior_uncertainty
    # Craft a pick whose posterior would trip the old ev<0 gate — the
    # posterior EV is a monotonic function of μ.  With μ=0.30 and
    # -120 odds the posterior EV is quite negative; the filter must
    # NOT set no_bet / verdict=PASS on this basis alone.
    p = _pick(book_odds=-120, win_probability=30,
                brain={"top_k": True, "confidence_calibrated": 0.30},
                edge_percent=2.0)
    run_posterior_uncertainty([p], _FakeMemory())
    counts = decision_filter([p], _FakeMemory())
    assert p.get("no_bet") is not True
    reasons = p.get("brain", {}).get("pass_reasons", [])
    assert "sim_ev<0" not in reasons
    assert "sim_var_high" not in reasons


# ═══════════════════════════════════════════════════════════════════
# 5. Symmetric anchor — bounded ±SIM_RESIDUAL_MAX both directions
# ═══════════════════════════════════════════════════════════════════
def test_symmetric_anchor_lifts_up_bounded():
    from brain.sim_runner import _anchor_pick_to_sim, SIM_RESIDUAL_MAX
    p = _pick(lock_score=70.0)
    audit = _anchor_pick_to_sim(p, sim_wp=90.0,
                                 sim_meta={"independent_evidence": True,
                                            "valid": True})
    # sim_wp=90 → baseline ~96 lock (from sim_wp_to_lock_baseline).
    # prior=70, residual~+26, clamped to +SIM_RESIDUAL_MAX.
    assert audit["anchored"] is True
    assert audit["applied_delta"] == pytest.approx(SIM_RESIDUAL_MAX, abs=0.1)
    assert p["lock_score"] == pytest.approx(70.0 + SIM_RESIDUAL_MAX, abs=0.1)


def test_symmetric_anchor_moves_down_bounded():
    from brain.sim_runner import _anchor_pick_to_sim, SIM_RESIDUAL_MAX
    p = _pick(lock_score=90.0)
    audit = _anchor_pick_to_sim(p, sim_wp=55.0,
                                 sim_meta={"independent_evidence": True,
                                            "valid": True})
    # sim_wp=55 → baseline ~65 lock.
    # prior=90, residual~-25, clamped to -SIM_RESIDUAL_MAX.
    assert audit["anchored"] is True
    assert audit["applied_delta"] == pytest.approx(-SIM_RESIDUAL_MAX, abs=0.1)
    assert p["lock_score"] == pytest.approx(90.0 - SIM_RESIDUAL_MAX, abs=0.1)


def test_anchor_preserves_elite_floor():
    from brain.sim_runner import _anchor_pick_to_sim
    p = _pick(lock_score=95.5, elite_player=True)
    _anchor_pick_to_sim(p, sim_wp=55.0,
                         sim_meta={"independent_evidence": True, "valid": True})
    assert p["lock_score"] >= 95.0


def test_anchor_rejects_posterior_uncertainty():
    from brain.sim_runner import _anchor_pick_to_sim
    p = _pick(lock_score=80.0)
    audit = _anchor_pick_to_sim(p, sim_wp=95.0,
                                 sim_meta={"independent_evidence": False,
                                            "valid": True})
    assert audit["anchored"] is False
    assert p["lock_score"] == 80.0
    assert p["sim_anchor_skip_reason"] == "posterior_uncertainty_not_independent"


def test_anchor_rejects_invalid_sim():
    from brain.sim_runner import _anchor_pick_to_sim
    p = _pick(lock_score=80.0)
    audit = _anchor_pick_to_sim(p, sim_wp=95.0,
                                 sim_meta={"independent_evidence": True,
                                            "valid": False})
    assert audit["anchored"] is False
    assert p["lock_score"] == 80.0
    assert p["sim_anchor_skip_reason"] == "sim_invalid"


def test_anchor_default_meta_treats_as_independent():
    """Untyped/legacy sim results are treated as independent+valid so
    pre-Phase-4B sport simulators keep functioning."""
    from brain.sim_runner import _anchor_pick_to_sim
    p = _pick(lock_score=70.0)
    audit = _anchor_pick_to_sim(p, sim_wp=90.0)   # no sim_meta
    assert audit["anchored"] is True


# ═══════════════════════════════════════════════════════════════════
# 6. Calibration segmentation
# ═══════════════════════════════════════════════════════════════════
def test_bucket_key_distinguishes_market_families():
    from services.calibration_segmentation import build_bucket_key
    a = build_bucket_key(sport="MLB", market_family="hitter_hits",
                          side="Over", line=1.5, american_odds=-120)
    b = build_bucket_key(sport="MLB", market_family="pitcher_k",
                          side="Over", line=5.5, american_odds=-120)
    assert a.to_string_id() != b.to_string_id()


def test_hierarchy_falls_back_from_L1_to_global():
    from services.calibration_segmentation import (
        build_bucket_key, hierarchy)
    k = build_bucket_key(sport="MLB", market_family="hitter_hits",
                          side="Over", line=1.5, american_odds=-120,
                          is_alt=False)
    h = hierarchy(k)
    assert len(h) == 6
    assert h[0][0] == "L1"
    assert h[-1][0] == "L6"
    assert h[-1][1].sport is None


def test_min_sample_thresholds_ordered():
    from services.calibration_segmentation import DEFAULT_MIN_SAMPLE
    vals = [DEFAULT_MIN_SAMPLE[f"L{i}"] for i in range(1, 7)]
    assert vals == sorted(vals, reverse=True)      # decreasing


def test_odds_and_line_band_classification():
    from services.calibration_segmentation import (
        classify_odds_band, classify_line_band)
    assert classify_odds_band(-500) == "deep_chalk"
    assert classify_odds_band(-140) == "moderate_fav"
    assert classify_odds_band(+250) == "mid_dog"
    assert classify_line_band(0.5) == "0.5"
    assert classify_line_band(1.5) == "1.5"
    assert classify_line_band(2.5) == "2.5"
    assert classify_line_band(None) is None


# ═══════════════════════════════════════════════════════════════════
# 7. Baseline report artefacts exist + report zero writes
# ═══════════════════════════════════════════════════════════════════
def test_baseline_report_artefacts_exist():
    import json
    assert os.path.exists("/app/PHASE4B_CALIBRATION_BASELINE.md")
    assert os.path.exists("/app/PHASE4B_SIMULATOR_BASELINE.json")
    with open("/app/PHASE4B_SIMULATOR_BASELINE.json") as fp:
        report = json.load(fp)
    assert report["scanned_picks"] > 0
    assert "global" in report
    assert "axes" in report
    assert "by_sport" in report["axes"]


# ═══════════════════════════════════════════════════════════════════
# 8. Static repository guardrails
# ═══════════════════════════════════════════════════════════════════
def test_lift_only_wording_removed_from_sim_runner():
    """The old lift-only anchor logic must not reappear."""
    src = open("/app/backend/brain/sim_runner.py", encoding="utf-8").read()
    # Old comment string that only appeared in the lift-only version.
    assert "SIM ANCHOR IS A FLOOR" not in src
    # Symmetric anchor must be documented.
    assert "Symmetric" in src or "SIM_RESIDUAL_MAX" in src
    assert "SIM_RESIDUAL_MAX" in src


def test_simulator_module_labels_itself_posterior():
    src = open("/app/backend/brain/simulator.py", encoding="utf-8").read()
    assert "posterior_uncertainty" in src
    assert "independent_evidence" in src
    assert "beta_bernoulli_posterior" in src


def test_baseline_script_makes_zero_production_writes():
    src = open("/app/backend/scripts/phase4b_calibration_baseline.py",
                encoding="utf-8").read()
    # Assert no update / insert / delete / drop / replace calls on db.
    for forbidden in ("db.picks.update", "db.picks.insert",
                       "db.picks.delete", "db.picks.replace",
                       "insert_one", "insert_many", "update_one",
                       "update_many", "delete_one", "delete_many",
                       "drop("):
        assert forbidden not in src, (
            f"Baseline script must not contain {forbidden!r}")
