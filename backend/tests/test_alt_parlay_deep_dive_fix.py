"""Regression tests for the 2026.06.23 fix:
  (1) deep_dive.py compute_*_score defensive coercion against non-numeric factor values
  (2) parlay_optimizer.is_eligible_leg() alt-prop carve-out (min_edge → 1.0% even in standard mode)
  (3) /api/version data_version bumped to 2026.06.23-alt-parlay-eligible
  (4) Regression: /api/picks/today, tennis_alt market, MLB pitcher-h2h still work
"""
import os
import sys
import re
import requests
import pytest

# Allow importing backend modules for direct unit tests
sys.path.insert(0, "/app/backend")

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "").rstrip("/")
assert BASE_URL, "EXPO_PUBLIC_BACKEND_URL must be set"

DEMO_EMAIL = "demo@lockscore.ai"
DEMO_PW = "demo123"


@pytest.fixture(scope="module")
def auth_token():
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": DEMO_EMAIL, "password": DEMO_PW},
        timeout=15,
    )
    assert r.status_code == 200, f"Login failed: {r.status_code} {r.text}"
    return r.json()["access_token"]


@pytest.fixture(scope="module")
def auth_headers(auth_token):
    return {"Authorization": f"Bearer {auth_token}", "Content-Type": "application/json"}


# ──────────────────────────────────────────────────────────────────────
# (3) DATA_VERSION
# ──────────────────────────────────────────────────────────────────────

class TestDataVersion:
    def test_version_endpoint(self):
        r = requests.get(f"{BASE_URL}/api/version", timeout=10)
        assert r.status_code == 200
        body = r.json()
        assert body.get("data_version") == "2026.06.23-alt-parlay-eligible", (
            f"Expected data_version=2026.06.23-alt-parlay-eligible, got {body}"
        )


# ──────────────────────────────────────────────────────────────────────
# (1) deep_dive.py defensive coercion — unit tests on the module directly
# ──────────────────────────────────────────────────────────────────────

class TestDeepDiveCoercion:
    """Compute_*_score functions must not crash when factors contain strings."""

    def test_compute_confidence_with_string_factors(self):
        from deep_dive import compute_confidence_score
        pick = {
            "win_probability": 72.0,
            "factors": {
                "form": "strong recent run",        # string!
                "matchup": 65.0,
                "rank": "#12",                       # string!
                "h2h": 70.0,
                "fatigue": None,                     # None!
            },
        }
        # should NOT raise
        score = compute_confidence_score(pick, bucket_n=10, bucket_hit_rate=70.0)
        assert isinstance(score, float)
        assert 0.0 <= score <= 100.0

    def test_compute_confidence_with_all_string_factors(self):
        from deep_dive import compute_confidence_score
        pick = {
            "win_probability": "82.5",  # string wp too
            "factors": {
                "a": "good", "b": "bad", "c": "neutral",
            },
        }
        score = compute_confidence_score(pick)
        assert isinstance(score, float)
        # win_prob string "82.5" should still coerce
        assert score >= 80.0

    def test_compute_edge_with_string_edge(self):
        from deep_dive import compute_edge_score
        pick = {"edge_percent": "5.5", "odds_at_pick": "-110", "closing_odds": "-120"}
        score = compute_edge_score(pick)
        assert isinstance(score, float)
        assert 0.0 <= score <= 100.0

    def test_compute_edge_with_garbage_edge(self):
        from deep_dive import compute_edge_score
        pick = {"edge_percent": "N/A", "book_odds": None}
        # should not raise; defaults edge → 0.0 → base 50
        score = compute_edge_score(pick)
        assert score == 50.0

    def test_compute_risk_with_string_wp(self):
        from deep_dive import compute_risk_score
        pick = {"win_probability": "not a number", "book_odds": "junk"}
        score = compute_risk_score(pick)
        assert isinstance(score, float)
        assert 0.0 <= score <= 100.0


# ──────────────────────────────────────────────────────────────────────
# (2) parlay_optimizer.is_eligible_leg() — alt-prop carve-out
# ──────────────────────────────────────────────────────────────────────

class TestAltParlayEligibility:
    def _base_pick(self, **kw):
        p = {
            "lock_score": 95.0,
            "edge_percent": 2.0,
            "win_probability": 80.0,
            "sport": "Tennis",
            "market": "tennis_alt_total",
        }
        p.update(kw)
        return p

    def test_alt_pick_eligible_in_standard_mode(self):
        from parlay_optimizer import is_eligible_leg
        pick = self._base_pick(is_alt=True)
        ok, reason = is_eligible_leg(pick, bucket_map={}, high_risk=False)
        assert ok is True, f"Alt pick rejected: {reason}"
        assert reason == ""

    def test_legacy_is_alt_prop_eligible_in_standard_mode(self):
        from parlay_optimizer import is_eligible_leg
        pick = self._base_pick(is_alt_prop=True)
        ok, reason = is_eligible_leg(pick, bucket_map={}, high_risk=False)
        assert ok is True, f"Legacy alt-prop pick rejected: {reason}"

    def test_non_alt_pick_rejected_for_low_edge_in_standard(self):
        from parlay_optimizer import is_eligible_leg
        pick = self._base_pick()  # no is_alt flag
        ok, reason = is_eligible_leg(pick, bucket_map={}, high_risk=False)
        assert ok is False
        assert "edge" in reason.lower() and ("+3" in reason or "3%" in reason), reason

    def test_alt_pick_still_blocked_if_lock_low(self):
        from parlay_optimizer import is_eligible_leg
        pick = self._base_pick(is_alt=True, lock_score=70.0)
        ok, reason = is_eligible_leg(pick, bucket_map={}, high_risk=False)
        assert ok is False
        assert "lock" in reason.lower()

    def test_alt_pick_with_negative_edge_rejected(self):
        from parlay_optimizer import is_eligible_leg
        pick = self._base_pick(is_alt=True, edge_percent=-0.5)
        ok, reason = is_eligible_leg(pick, bucket_map={}, high_risk=False)
        assert ok is False
        assert "edge" in reason.lower()


# ──────────────────────────────────────────────────────────────────────
# (4) /api/picks/parlay — ALT legs included
# ──────────────────────────────────────────────────────────────────────

class TestParlayEndpointAltInclusion:
    def _leg_is_alt(self, leg: dict) -> bool:
        # selection text or flags
        if leg.get("is_alt") or leg.get("is_alt_prop"):
            return True
        sel = (leg.get("selection") or "").lower()
        market = (leg.get("market") or "").lower()
        return "(alt)" in sel or "alt" in market

    def _extract_legs(self, body):
        # API returns {"parlay": {"legs": [...]}, "parlays": [{...}]}
        if isinstance(body, dict):
            parlay = body.get("parlay") or {}
            legs = parlay.get("legs") or body.get("legs") or []
            return legs
        return []

    def test_parlay_5leg_24h_includes_alt(self, auth_headers):
        r = requests.get(
            f"{BASE_URL}/api/picks/parlay?legs=5&window_hours=24",
            headers=auth_headers,
            timeout=30,
        )
        assert r.status_code == 200, f"parlay endpoint failed: {r.status_code} {r.text[:300]}"
        body = r.json()
        legs = self._extract_legs(body)
        assert len(legs) > 0, f"No legs returned: {list(body.keys())}"
        alt_legs = [L for L in legs if self._leg_is_alt(L)]
        print(f"5-leg parlay returned {len(legs)} legs, {len(alt_legs)} ALT")
        # ALT inclusion is the focus of this fix
        assert len(alt_legs) >= 1, (
            f"Expected ≥1 ALT leg; got 0. Legs selections: "
            f"{[(L.get('sport'), L.get('selection'), L.get('is_alt')) for L in legs]}"
        )

    def test_parlay_tennis_only_3leg_includes_alt(self, auth_headers):
        # Endpoint param is `sport` (singular). NOTE: in current implementation
        # the sport filter doesn't strictly restrict — observed Soccer legs in
        # response. That's a pre-existing issue outside this fix's scope.
        # We only assert that ALT legs are included in the resulting parlay.
        r = requests.get(
            f"{BASE_URL}/api/picks/parlay?sport=Tennis&legs=3&window_hours=24",
            headers=auth_headers,
            timeout=30,
        )
        assert r.status_code == 200, f"tennis parlay failed: {r.status_code} {r.text[:300]}"
        body = r.json()
        legs = self._extract_legs(body)
        if not legs:
            pytest.skip(f"No parlay returned (pool maybe empty): {list(body.keys())}")
        alt_legs = [L for L in legs if self._leg_is_alt(L)]
        tennis_legs = [L for L in legs if (L.get("sport") or "").lower() == "tennis"]
        print(
            f"Tennis-filtered 3-leg parlay: {len(legs)} legs total, "
            f"{len(tennis_legs)} tennis, {len(alt_legs)} ALT"
        )
        assert len(alt_legs) >= 1, (
            f"Expected ≥1 ALT leg in tennis-filtered parlay; got 0. selections: "
            f"{[(L.get('sport'), L.get('selection'), L.get('is_alt')) for L in legs]}"
        )


# ──────────────────────────────────────────────────────────────────────
# Regression tests
# ──────────────────────────────────────────────────────────────────────

class TestRegression:
    def test_picks_today_returns_many(self, auth_headers):
        r = requests.get(f"{BASE_URL}/api/picks/today", headers=auth_headers, timeout=20)
        assert r.status_code == 200
        picks = r.json().get("picks") or r.json()
        if isinstance(picks, dict):  # in case nested
            picks = picks.get("picks") or []
        assert isinstance(picks, list)
        assert len(picks) > 100, f"Expected >100 picks, got {len(picks)}"

    def test_tennis_alt_tab(self, auth_headers):
        r = requests.get(
            f"{BASE_URL}/api/picks/today?sport=Tennis&market=tennis_alt",
            headers=auth_headers,
            timeout=20,
        )
        assert r.status_code == 200
        picks = r.json().get("picks") or r.json()
        if isinstance(picks, dict):
            picks = picks.get("picks") or []
        assert isinstance(picks, list)
        assert len(picks) >= 30, f"Expected ≥30 tennis_alt picks, got {len(picks)}"

    def test_mlb_pitcher_h2h(self, auth_headers):
        # Endpoint is /api/picks/{pick_id}/pitcher-h2h — fetch an MLB pitcher pick first
        r = requests.get(
            f"{BASE_URL}/api/picks/today?sport=MLB&market=pitcher_strikeouts",
            headers=auth_headers, timeout=20,
        )
        if r.status_code != 200:
            pytest.skip(f"Could not list MLB pitcher picks: {r.status_code}")
        picks = r.json().get("picks") or r.json()
        if isinstance(picks, dict):
            picks = picks.get("picks") or []
        if not picks:
            pytest.skip("No MLB pitcher_strikeouts picks today")
        pid = picks[0].get("id")
        assert pid, f"No id in pick: {picks[0]}"
        r2 = requests.get(
            f"{BASE_URL}/api/picks/{pid}/pitcher-h2h",
            headers=auth_headers, timeout=20,
        )
        assert r2.status_code in (200, 404, 422), f"Unexpected: {r2.status_code} {r2.text[:200]}"
        if r2.status_code == 200:
            body = r2.json()
            assert isinstance(body, (list, dict))


# ──────────────────────────────────────────────────────────────────────
# Log scan: no new deep_dive crashes after last refresh
# ──────────────────────────────────────────────────────────────────────

class TestNoNewDeepDiveCrashes:
    LOG_PATH = "/var/log/supervisor/backend.err.log"

    def test_log_check_no_recent_crashes(self):
        """Inspect log: count 'deep_dive failed for pick ... unsupported operand'
        warnings in the LAST 5 minutes. After the fix these should be 0."""
        if not os.path.exists(self.LOG_PATH):
            pytest.skip("Backend log not found")
        with open(self.LOG_PATH, "r", errors="ignore") as f:
            lines = f.readlines()
        # last 10 minutes worth of lines (tail aggressively)
        recent = lines[-5000:]
        pattern = re.compile(r"deep_dive failed for pick .+unsupported operand type")
        # We further filter to lines containing today's date heuristically:
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(minutes=10)
        ts_re = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
        recent_crashes = []
        for L in recent:
            if not pattern.search(L):
                continue
            m = ts_re.match(L)
            if not m:
                continue
            try:
                ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if ts >= cutoff:
                recent_crashes.append(L.strip())
        print(f"Recent (last 10m) deep_dive crashes: {len(recent_crashes)}")
        for line in recent_crashes[:5]:
            print(f"  {line}")
        assert len(recent_crashes) == 0, (
            f"Found {len(recent_crashes)} new deep_dive 'unsupported operand' "
            f"warnings in the last 10 minutes after fix should have prevented all."
        )
