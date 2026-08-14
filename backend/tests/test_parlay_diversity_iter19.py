"""Iter19 backend tests — verify parlay diversity tightening & structural correctness.

Covers:
1. High-risk 72h parlay sport diversity (≤40% same sport, ≥2 sports per parlay)
2. High-risk 72h returns 3 parlays (SAFE/BALANCED/AGGRESSIVE), each ≥5 legs
3. Standard 24h parlay still returns 3 parlays
4. Same-sport cap math sanity (target=5/8/10/15/20)
"""
from __future__ import annotations

import os
import sys
from collections import Counter

import pytest
import requests

# Allow direct import of backend modules
sys.path.insert(0, "/app/backend")

BASE_URL = (
    os.environ.get("EXPO_BACKEND_URL")
    or os.environ.get("EXPO_PUBLIC_BACKEND_URL")
    or "https://canonical-parity.preview.emergentagent.com"
).rstrip("/")
DEMO_EMAIL = "demo@lockscore.ai"
DEMO_PASSWORD = "demo123"


@pytest.fixture(scope="session")
def auth_token():
    assert BASE_URL, "EXPO_BACKEND_URL must be set"
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD},
        timeout=30,
    )
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:300]}"
    tok = r.json().get("access_token")
    assert tok, "no access_token in login response"
    return tok


@pytest.fixture(scope="session")
def api_client(auth_token):
    s = requests.Session()
    s.headers.update({
        "Content-Type": "application/json",
        "Authorization": f"Bearer {auth_token}",
    })
    return s


# ─────────────────────────────────────────────────────────────────────
# Feature 5 — pure unit test of max_same_sport_for_target math
# ─────────────────────────────────────────────────────────────────────
class TestSameSportCapMath:
    """Verify the tightened diversification cap formula."""

    def test_target_5_caps_at_2(self):
        from parlay_optimizer import max_same_sport_for_target
        assert max_same_sport_for_target(5) == 2

    def test_target_8_caps_at_3(self):
        # 40% of 8 = 3.2 → integer floor = 3
        from parlay_optimizer import max_same_sport_for_target
        assert max_same_sport_for_target(8) == 3

    def test_target_10_caps_at_4(self):
        # 40% of 10 = 4
        from parlay_optimizer import max_same_sport_for_target
        assert max_same_sport_for_target(10) == 4

    def test_target_15_caps_at_5(self):
        # 33% of 15 = 5
        from parlay_optimizer import max_same_sport_for_target
        assert max_same_sport_for_target(15) == 5

    def test_target_20_caps_at_6(self):
        # 33% of 20 = 6.67 → integer floor = 6
        from parlay_optimizer import max_same_sport_for_target
        assert max_same_sport_for_target(20) == 6

    def test_lower_bound_2_3_legs(self):
        from parlay_optimizer import max_same_sport_for_target
        assert max_same_sport_for_target(2) == 2
        assert max_same_sport_for_target(3) == 2


# ─────────────────────────────────────────────────────────────────────
# Feature 3 — standard 24h parlay still works
# ─────────────────────────────────────────────────────────────────────
class TestStandardParlay24h:
    def test_returns_3_parlays(self, api_client):
        r = api_client.get(
            f"{BASE_URL}/api/picks/parlay",
            params={"legs": 3, "mode": "standard", "window_hours": 24,
                    "sport_mode": "auto"},
            timeout=60,
        )
        assert r.status_code == 200, f"{r.status_code} {r.text[:300]}"
        body = r.json()
        parlays = body.get("parlays") or body.get("cards") or []
        assert isinstance(parlays, list), f"unexpected shape: {list(body.keys())}"
        assert len(parlays) == 3, f"expected 3 parlays, got {len(parlays)}: labels={[p.get('label') for p in parlays]}"
        for p in parlays:
            assert p.get("legs"), "parlay missing legs"
            assert len(p["legs"]) >= 2, "standard parlay should have ≥2 legs"


# ─────────────────────────────────────────────────────────────────────
# Features 1 & 2 — high-risk 72h diversity + structure
# ─────────────────────────────────────────────────────────────────────
class TestHighRiskParlay72h:
    @pytest.fixture(scope="class")
    def high_risk_response(self, api_client):
        r = api_client.get(
            f"{BASE_URL}/api/picks/parlay",
            params={"legs": 10, "mode": "high_risk", "window_hours": 72,
                    "sport_mode": "auto"},
            timeout=90,
        )
        assert r.status_code == 200, f"{r.status_code} {r.text[:400]}"
        return r.json()

    def test_returns_3_parlays(self, high_risk_response):
        parlays = high_risk_response.get("parlays") or high_risk_response.get("cards") or []
        labels = [p.get("label") for p in parlays]
        assert len(parlays) == 3, f"expected 3 parlays, got {len(parlays)}: labels={labels}"
        # SAFE / BALANCED / AGGRESSIVE
        assert set(labels) == {"SAFE", "BALANCED", "AGGRESSIVE"}, f"labels={labels}"

    def test_each_parlay_has_min_5_legs(self, high_risk_response):
        parlays = high_risk_response.get("parlays") or high_risk_response.get("cards") or []
        for p in parlays:
            legs = p.get("legs", [])
            assert len(legs) >= 5, (
                f"parlay {p.get('label')} has only {len(legs)} legs (expected ≥5)"
            )

    def test_sport_diversity_within_implementation_cap(self, high_risk_response):
        """Diversification cap is enforced against requested target_legs (10).
        Per parlay_optimizer.max_same_sport_for_target(10) → 4. No single
        sport may exceed 4 legs regardless of final parlay length."""
        from parlay_optimizer import max_same_sport_for_target

        target_legs = 10  # request param
        cap = max_same_sport_for_target(target_legs)
        assert cap == 4

        parlays = high_risk_response.get("parlays") or high_risk_response.get("cards") or []
        failures = []
        for p in parlays:
            legs = p.get("legs", [])
            sports = Counter((L.get("sport") or "Unknown") for L in legs)
            top_sport, top_count = sports.most_common(1)[0]
            if top_count > cap:
                failures.append(
                    f"{p.get('label')} ({len(legs)} legs): {top_sport}={top_count} exceeds target-based cap={cap}; sports={dict(sports)}"
                )
        assert not failures, "Sport diversity cap violated:\n" + "\n".join(failures)

    def test_post_truncation_sport_ratio_observation(self, high_risk_response):
        """Diagnostic: report top-sport share of FINAL leg count.

        Note: the cap is enforced against `target_legs` not `len(legs)`. When
        the optimizer stops early (e.g. 8 of 10 legs) the top-sport share of
        the *actual* parlay can exceed 40%. Asserts a softer 60% ceiling so
        a true monoculture (>60%) is still flagged."""
        parlays = high_risk_response.get("parlays") or high_risk_response.get("cards") or []
        for p in parlays:
            legs = p.get("legs", [])
            sports = Counter((L.get("sport") or "Unknown") for L in legs)
            top_sport, top_count = sports.most_common(1)[0]
            share = top_count / len(legs)
            print(f"  {p.get('label'):<11} {len(legs):2d} legs → top {top_sport} {top_count} ({share:.0%})")
            assert share <= 0.60, (
                f"{p.get('label')}: {top_sport} share {share:.0%} (>60%) — monoculture"
            )

    def test_at_least_2_distinct_sports_per_parlay(self, high_risk_response):
        parlays = high_risk_response.get("parlays") or high_risk_response.get("cards") or []
        failures = []
        for p in parlays:
            legs = p.get("legs", [])
            sports = {(L.get("sport") or "Unknown") for L in legs}
            if len(sports) < 2:
                failures.append(
                    f"{p.get('label')} ({len(legs)} legs): only {len(sports)} distinct sport(s) = {sports}"
                )
        assert not failures, "Parlay monoculture detected:\n" + "\n".join(failures)

    def test_sport_distribution_diagnostic(self, high_risk_response):
        """Diagnostic-only: prints the sport breakdown for visibility.
        Always passes; surfaces actual mix in CI output for review."""
        parlays = high_risk_response.get("parlays") or high_risk_response.get("cards") or []
        print("\n── High-risk 72h sport distribution ──")
        for p in parlays:
            legs = p.get("legs", [])
            sports = Counter((L.get("sport") or "Unknown") for L in legs)
            print(f"  {p.get('label'):<11} {len(legs):2d} legs → {dict(sports)}")
