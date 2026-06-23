"""Tests for the Unified Probability Engine (iter 36).

Validates:
  • /api/picks/{pick_id}/probability returns full spec schema
  • Ensemble math: 0.30·v1 + 0.45·v2 + 0.25·sim
  • Edge clamping [-0.15, +0.40]
  • Classification rules (LOCK_99 / PREMIUM / CHALK / NORMAL)
  • /api/version data_version stamp
  • Regression: /api/picks/today unchanged shape, 100+ picks
  • Source code invariants: no v1/v2 subtraction; compute_edge is sole edge fn
"""
from __future__ import annotations

import os
import re
import sys
import pathlib
import pytest
import requests

BASE_URL = os.environ["EXPO_PUBLIC_BACKEND_URL"].rstrip("/")

# Add backend path so we can unit-test the pure-python engine directly
sys.path.insert(0, "/app/backend")

EMAIL = "demo@lockscore.ai"
PASSWORD = "demo123"


# ──────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────
@pytest.fixture(scope="session")
def session():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="session")
def auth_token(session):
    r = session.post(f"{BASE_URL}/api/auth/login",
                     json={"email": EMAIL, "password": PASSWORD},
                     timeout=20)
    if r.status_code != 200:
        pytest.skip(f"Login failed: {r.status_code} {r.text}")
    tok = r.json().get("access_token")
    assert tok, "no access_token returned"
    return tok


@pytest.fixture(scope="session")
def auth_session(session, auth_token):
    session.headers.update({"Authorization": f"Bearer {auth_token}"})
    return session


@pytest.fixture(scope="session")
def todays_picks(auth_session):
    r = auth_session.get(f"{BASE_URL}/api/picks/today", timeout=60)
    assert r.status_code == 200, r.text
    body = r.json()
    # Could be a list or {"picks": [...]}
    if isinstance(body, dict):
        picks = body.get("picks") or body.get("data") or []
    else:
        picks = body
    return picks


# ──────────────────────────────────────────────────────────────────────────
# /api/version
# ──────────────────────────────────────────────────────────────────────────
class TestVersion:
    def test_data_version_stamp(self, session):
        r = session.get(f"{BASE_URL}/api/version", timeout=15)
        assert r.status_code == 200
        body = r.json()
        assert body.get("data_version") == "2026.06.23-unified-probability-engine"


# ──────────────────────────────────────────────────────────────────────────
# /api/picks/today regression
# ──────────────────────────────────────────────────────────────────────────
class TestPicksTodayRegression:
    def test_picks_count(self, todays_picks):
        assert isinstance(todays_picks, list)
        assert len(todays_picks) >= 100, f"only {len(todays_picks)} picks returned"

    def test_legacy_fields_preserved(self, todays_picks):
        # The spec is explicit: lock_score & edge_percent must remain on the pick.
        sample = todays_picks[0]
        assert "lock_score" in sample, list(sample.keys())[:20]
        assert "edge_percent" in sample, list(sample.keys())[:20]

    def test_tennis_edge_not_universally_zero(self, todays_picks):
        tennis = [p for p in todays_picks if str(p.get("sport", "")).lower() == "tennis"]
        if not tennis:
            pytest.skip("no tennis picks in today's slate")
        # At least one tennis pick should have a non-zero edge_percent
        non_zero = [p for p in tennis if float(p.get("edge_percent") or 0) != 0]
        assert non_zero, "all tennis picks have edge_percent=0 — regression!"


# ──────────────────────────────────────────────────────────────────────────
# /api/picks/{id}/probability — schema + invariants
# ──────────────────────────────────────────────────────────────────────────
SPEC_FIELDS = {
    "p_v1", "p_v2", "sim_probability",
    "p_final", "p_calibrated", "edge",
    "classification", "simulator_variance",
}
AUX_FIELDS = {"stability_score", "implied_probability", "weights", "calibration"}


class TestProbabilityEndpoint:
    @pytest.fixture(scope="class")
    def pick_id(self, todays_picks):
        # Pick the first one with a usable id field
        for p in todays_picks:
            pid = p.get("id") or p.get("pick_id")
            if pid:
                return pid
        pytest.skip("no pick with id found")

    @pytest.fixture(scope="class")
    def probability_payload(self, auth_session, pick_id):
        r = auth_session.get(f"{BASE_URL}/api/picks/{pick_id}/probability", timeout=30)
        assert r.status_code == 200, f"{r.status_code} {r.text}"
        return r.json()

    def test_spec_fields_present(self, probability_payload):
        missing = SPEC_FIELDS - set(probability_payload.keys())
        assert not missing, f"missing spec fields: {missing}"

    def test_aux_fields_present(self, probability_payload):
        missing = AUX_FIELDS - set(probability_payload.keys())
        assert not missing, f"missing aux fields: {missing}"

    def test_probability_bounds(self, probability_payload):
        for k in ("p_v1", "p_v2", "sim_probability", "p_final",
                  "p_calibrated", "stability_score", "implied_probability"):
            v = probability_payload[k]
            assert 0.0 <= v <= 1.0, f"{k}={v} not in [0,1]"

    def test_edge_clamped(self, probability_payload):
        e = probability_payload["edge"]
        assert -0.15 <= e <= 0.40, f"edge={e} not in [-0.15, +0.40]"

    def test_ensemble_math_consistency(self, probability_payload):
        """p_final must equal 0.30*v1 + 0.45*v2 + 0.25*sim (within rounding)."""
        expected = (0.30 * probability_payload["p_v1"]
                    + 0.45 * probability_payload["p_v2"]
                    + 0.25 * probability_payload["sim_probability"])
        assert abs(expected - probability_payload["p_final"]) < 0.005, \
            f"ensemble mismatch: expected {expected:.4f}, got {probability_payload['p_final']}"

    def test_weights_published(self, probability_payload):
        w = probability_payload["weights"]
        assert w == {"v1": 0.30, "v2": 0.45, "sim": 0.25}

    def test_classification_value(self, probability_payload):
        assert probability_payload["classification"] in {
            "LOCK_99", "PREMIUM", "NORMAL", "CHALK"
        }

    def test_unknown_pick_returns_404(self, auth_session):
        r = auth_session.get(
            f"{BASE_URL}/api/picks/__definitely_not_a_real_pick__/probability",
            timeout=15)
        assert r.status_code == 404


# ──────────────────────────────────────────────────────────────────────────
# Pure-unit tests against probability_engine.py (no HTTP)
# ──────────────────────────────────────────────────────────────────────────
class TestProbabilityEngineUnit:
    def test_ensemble_exact_spec_example(self):
        from probability_engine import ensemble
        got = ensemble(0.5, 0.6, 0.7)
        # 0.30*0.5 + 0.45*0.6 + 0.25*0.7 = 0.15 + 0.27 + 0.175 = 0.595
        assert abs(got - 0.595) < 1e-9, f"ensemble({0.5},{0.6},{0.7}) = {got}, expected 0.595"

    def test_ensemble_no_subtraction(self):
        """If ensemble subtracted v1 from v2 anywhere, low-v1/high-v2 would
        diverge from the weighted-average baseline. Check the identity."""
        from probability_engine import ensemble
        # Symmetric check: each input separately => weight equals coefficient.
        assert abs(ensemble(1.0, 0.0, 0.0) - 0.30) < 1e-9
        assert abs(ensemble(0.0, 1.0, 0.0) - 0.45) < 1e-9
        assert abs(ensemble(0.0, 0.0, 1.0) - 0.25) < 1e-9

    def test_edge_clamps_high(self):
        from probability_engine import compute_edge
        # p=1.0, odds=+10000 → implied≈0.0099 → raw_edge≈0.99 → clamp to 0.40
        e = compute_edge(1.0, 10000)
        assert e == 0.40

    def test_edge_clamps_low(self):
        from probability_engine import compute_edge
        # p=0.0, odds=-10000 → implied≈0.99 → raw_edge≈-0.99 → clamp to -0.15
        e = compute_edge(0.0, -10000)
        assert e == -0.15

    def test_implied_from_odds(self):
        from probability_engine import implied_probability_from_odds
        # -200 → 200/(200+100) = 0.6667
        assert abs(implied_probability_from_odds(-200) - (2/3)) < 1e-3
        # +150 → 100/(150+100) = 0.40
        assert abs(implied_probability_from_odds(150) - 0.40) < 1e-3

    def test_classify_lock99(self):
        from probability_engine import classify
        assert classify(0.80, 0.10, 0.90, 0.30) == "LOCK_99"
        # Fails stability
        assert classify(0.80, 0.10, 0.50, 0.30) == "PREMIUM"
        # Fails edge
        assert classify(0.80, 0.01, 0.90, 0.30) == "PREMIUM"
        # Fails p_cal
        assert classify(0.71, 0.10, 0.90, 0.30) == "PREMIUM"

    def test_classify_premium(self):
        from probability_engine import classify
        assert classify(0.65, 0.02, 0.50, 0.30) == "PREMIUM"
        # Spec says PREMIUM beats CHALK label when p_cal >= 0.60
        assert classify(0.65, 0.02, 0.50, 0.70) == "PREMIUM"

    def test_classify_chalk(self):
        from probability_engine import classify
        # implied >= 0.65, p_cal below PREMIUM bar → CHALK label
        assert classify(0.55, 0.01, 0.50, 0.70) == "CHALK"

    def test_classify_normal(self):
        from probability_engine import classify
        assert classify(0.55, 0.01, 0.50, 0.40) == "NORMAL"

    def test_bieber_style_pick(self):
        """A Bieber-style strong-favorite pitcher pick: high p_v1/v2/sim,
        American odds short → implied ~0.86, p_cal ~0.82, edge ≈ -0.05 → PREMIUM.
        """
        from probability_engine import unified_probability_report
        pick = {
            # 0..100 percentage inputs (engine normalises to /100)
            "model_win_probability": 87.2,
            "win_probability": 79.2,
            "lock_score_v2": 80.0,
            "sim_win_probability": 91.3,
            "sim_ci_lower": 89.0,
            "sim_ci_upper": 93.0,
            "book_odds": -650,   # implied ≈ 0.867
        }
        r = unified_probability_report(pick)
        assert 0.60 <= r["p_calibrated"] <= 1.0
        # Implied > p_cal → edge negative but clamped above -0.15
        assert -0.15 <= r["edge"] <= 0.0
        # Should classify as PREMIUM (p_cal >= 0.60, but edge<0.05 so not LOCK_99)
        assert r["classification"] in {"PREMIUM", "LOCK_99"}, r
        assert r["classification"] == "PREMIUM", f"expected PREMIUM, got {r['classification']}"

    # ──────────────────────────────────────────────────────────────────
    # Static-source-code invariants from the spec
    # ──────────────────────────────────────────────────────────────────
    def test_source_has_no_v1_v2_subtraction(self):
        src = pathlib.Path("/app/backend/probability_engine.py").read_text()
        # Look for things like  p_v1 - p_v2,  v1 - v2, etc.
        bad = re.findall(r"(?:p_)?v1\s*-\s*(?:p_)?v2|(?:p_)?v2\s*-\s*(?:p_)?v1", src)
        assert not bad, f"forbidden v1/v2 subtraction found: {bad}"

    def test_compute_edge_is_only_edge_fn(self):
        """Inside probability_engine.py, only `compute_edge` and
        `implied_probability_from_odds` should be defining the canonical
        edge. Confirm no other `def *_edge*` function snuck in."""
        src = pathlib.Path("/app/backend/probability_engine.py").read_text()
        defs = re.findall(r"^def\s+(\w+)\s*\(", src, flags=re.M)
        edgey = [d for d in defs if "edge" in d.lower()]
        assert edgey == ["compute_edge"], f"unexpected edge fns: {edgey}"
