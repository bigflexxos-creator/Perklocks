"""
PerksLocks Iteration Review — Odds API circuit breaker, admin diagnostic
endpoints, HEAL PICKS NOW button backend, auto-relax floor logic, and
regression checks for the existing picks endpoints.

Run with:
    pytest /app/backend/tests/test_perkslocks_circuit_breaker_review.py -v \
        --junitxml=/app/test_reports/pytest/perkslocks_circuit_review.xml
"""
import os
import time
import pytest
import requests

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "").rstrip("/") or \
           "https://canonical-parity.preview.emergentagent.com"

DEMO_EMAIL = "demo@lockscore.ai"
DEMO_PASSWORD = "demo123"
ADMIN_EMAIL = "test_admin_review@perkslocks.com"
ADMIN_PASSWORD = "testpw123"


# ──────────────────────────────────────────── Shared fixtures ────────────
@pytest.fixture(scope="session")
def demo_token():
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD},
        timeout=20,
    )
    assert r.status_code == 200, f"demo login failed: {r.status_code} {r.text}"
    return r.json()["access_token"]


@pytest.fixture(scope="session")
def admin_token():
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        timeout=20,
    )
    assert r.status_code == 200, f"admin login failed: {r.status_code} {r.text}"
    return r.json()["access_token"]


def H(tok):
    return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}


# ───────────────────────── Auth gating on new admin endpoints ────────────
class TestAdminAuthGating:
    """All new admin endpoints must 401 unauth and 403 for non-admin users."""

    def test_odds_diagnostic_requires_auth(self):
        r = requests.get(f"{BASE_URL}/api/admin/odds-diagnostic", timeout=15)
        assert r.status_code == 401, f"expected 401 unauth, got {r.status_code}"

    def test_odds_diagnostic_forbids_non_admin(self, demo_token):
        r = requests.get(
            f"{BASE_URL}/api/admin/odds-diagnostic",
            headers=H(demo_token), timeout=15,
        )
        assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"

    def test_circuit_reset_requires_auth(self):
        r = requests.post(f"{BASE_URL}/api/admin/odds-circuit/reset", timeout=15)
        assert r.status_code == 401

    def test_circuit_reset_forbids_non_admin(self, demo_token):
        r = requests.post(
            f"{BASE_URL}/api/admin/odds-circuit/reset",
            headers=H(demo_token), timeout=15,
        )
        assert r.status_code == 403

    def test_force_refresh_requires_auth(self):
        r = requests.post(f"{BASE_URL}/api/admin/picks/force-refresh", timeout=15)
        assert r.status_code == 401

    def test_force_refresh_forbids_non_admin(self, demo_token):
        r = requests.post(
            f"{BASE_URL}/api/admin/picks/force-refresh",
            headers=H(demo_token), timeout=15,
        )
        assert r.status_code == 403

    def test_heal_requires_auth(self):
        r = requests.get(f"{BASE_URL}/api/admin/picks/heal", timeout=15)
        assert r.status_code == 401

    def test_heal_forbids_non_admin(self, demo_token):
        r = requests.get(
            f"{BASE_URL}/api/admin/picks/heal",
            headers=H(demo_token), timeout=15,
        )
        assert r.status_code == 403


# ────────────────────────────── Diagnostic shape ─────────────────────────
class TestOddsDiagnostic:
    """Verify JSON shape of GET /api/admin/odds-diagnostic."""

    def test_shape_and_fields(self, admin_token):
        r = requests.get(
            f"{BASE_URL}/api/admin/odds-diagnostic",
            headers=H(admin_token), timeout=20,
        )
        assert r.status_code == 200, r.text
        body = r.json()

        # Top-level keys
        for k in ("odds_api", "picks_today_total", "picks_today_high_lock",
                  "today_utc", "latest_picks_sample"):
            assert k in body, f"missing top-level key: {k}"

        # odds_api sub-shape
        oa = body["odds_api"]
        for k in ("has_key", "key_tail", "disabled", "disabled_reason",
                  "consecutive_401s", "consecutive_failures",
                  "total_ok", "total_fail", "last_error"):
            assert k in oa, f"odds_api missing key: {k}"

        # Type validation
        assert isinstance(oa["has_key"], bool)
        assert isinstance(oa["disabled"], bool)
        assert isinstance(oa["consecutive_401s"], int)
        assert isinstance(oa["consecutive_failures"], int)
        assert isinstance(oa["total_ok"], int)
        assert isinstance(oa["total_fail"], int)
        assert isinstance(body["picks_today_total"], int)
        assert isinstance(body["picks_today_high_lock"], int)
        assert isinstance(body["latest_picks_sample"], list)

        # Today's UTC date format
        assert len(body["today_utc"]) == 10 and body["today_utc"][4] == "-"

        # Healthy state expectations from the review request
        assert oa["has_key"] is True, "API key should be loaded locally"
        # Key tail is `...XXXX` for >=4 char key
        assert oa["key_tail"].startswith("...") and len(oa["key_tail"]) == 7

    def test_picks_today_total_matches_expectation(self, admin_token):
        """User context: ~189 picks locally for today. Verify >0."""
        r = requests.get(
            f"{BASE_URL}/api/admin/odds-diagnostic",
            headers=H(admin_token), timeout=20,
        )
        body = r.json()
        assert body["picks_today_total"] > 0, (
            f"expected picks today > 0, got {body['picks_today_total']}"
        )


# ───────────────────────── Circuit reset endpoint ────────────────────────
class TestCircuitReset:
    def test_reset_returns_healthy_state(self, admin_token):
        r = requests.post(
            f"{BASE_URL}/api/admin/odds-circuit/reset",
            headers=H(admin_token), timeout=15,
        )
        assert r.status_code == 200, r.text
        s = r.json()
        # Post-reset must show disabled=False and 0 streaks
        assert s["disabled"] is False
        assert s["consecutive_401s"] == 0
        assert s["consecutive_failures"] == 0
        assert s["disabled_reason"] == ""


# ─────────────────────── Force refresh (admin only) ──────────────────────
class TestForceRefresh:
    def test_force_refresh_queues(self, admin_token):
        r = requests.post(
            f"{BASE_URL}/api/admin/picks/force-refresh",
            headers=H(admin_token), timeout=20,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["queued"] is True
        assert "date" in body
        assert "existing_count" in body
        assert "circuit_state_after_reset" in body
        cs = body["circuit_state_after_reset"]
        assert cs["disabled"] is False
        assert cs["consecutive_401s"] == 0


# ─────────────────────────── HEAL PICKS NOW ──────────────────────────────
class TestHealPicks:
    def test_heal_returns_healing_queued(self, admin_token):
        r = requests.get(
            f"{BASE_URL}/api/admin/picks/heal",
            headers=H(admin_token), timeout=20,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["healing_queued"] is True
        assert "pre_state" in body
        assert "common_causes" in body
        assert isinstance(body["common_causes"], list)
        assert len(body["common_causes"]) >= 3
        # pre_state nested shape
        assert "odds_api" in body["pre_state"]
        assert "picks_today_count" in body["pre_state"]


# ───────────────────────────── /api/version ──────────────────────────────
class TestVersion:
    def test_data_version(self):
        r = requests.get(f"{BASE_URL}/api/version", timeout=15)
        assert r.status_code == 200, r.text
        body = r.json()
        # DATA_VERSION may be exposed under data_version or similar
        flat = str(body)
        assert "2026.06.26-auto-relax-thin-slate" in flat, (
            f"DATA_VERSION mismatch. Response: {body}"
        )


# ───────────────────────── Picks Today + Auto-Relax ──────────────────────
class TestPicksTodayAutoRelax:
    def test_picks_today_default_no_params(self, demo_token):
        t0 = time.time()
        r = requests.get(
            f"{BASE_URL}/api/picks/today",
            headers=H(demo_token), timeout=10,
        )
        elapsed = time.time() - t0
        assert r.status_code == 200, r.text
        body = r.json()
        # Either a bare list of picks OR object with picks key
        picks = body if isinstance(body, list) else body.get("picks", [])
        assert isinstance(picks, list)
        assert len(picks) > 0, "expected at least 1 pick for default feed"
        assert elapsed < 5.0, f"response too slow: {elapsed:.2f}s (target <2s, ceiling 5s)"

    def test_picks_today_default_no_auto_relax_with_healthy_slate(self, demo_token, admin_token):
        """With ~189 picks today, default floor=85 should produce >=8 high-lock
        picks, so auto-relax should NOT trigger. Verify via diagnostic that
        picks_today_high_lock is large."""
        diag = requests.get(
            f"{BASE_URL}/api/admin/odds-diagnostic",
            headers=H(admin_token), timeout=15,
        ).json()
        if diag["picks_today_high_lock"] < 8:
            pytest.skip(
                f"slate genuinely thin ({diag['picks_today_high_lock']} high-lock); "
                "auto-relax expected to trigger; cannot test no-relax path"
            )
        # Healthy slate path — picks endpoint should return high-lock picks
        r = requests.get(
            f"{BASE_URL}/api/picks/today",
            headers=H(demo_token), timeout=10,
        )
        assert r.status_code == 200
        picks = r.json() if isinstance(r.json(), list) else r.json().get("picks", [])
        # Most picks returned should have lock_score >= 85 (auto-relax did NOT fire)
        high = sum(1 for p in picks
                   if (p.get("lock_score") or p.get("lock_score_v2") or 0) >= 85)
        assert high >= 8, (
            f"expected >=8 high-lock picks in default feed (no auto-relax), got {high}"
        )

    def test_picks_today_lite(self, demo_token):
        r = requests.get(
            f"{BASE_URL}/api/picks/today?lite=true",
            headers=H(demo_token), timeout=10,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        picks = body if isinstance(body, list) else body.get("picks", [])
        assert isinstance(picks, list)
        assert len(picks) > 0

    def test_picks_today_min_lock_override(self, demo_token):
        """Explicit min_lock=70 should NOT trigger auto-relax path."""
        r = requests.get(
            f"{BASE_URL}/api/picks/today?min_lock=70",
            headers=H(demo_token), timeout=10,
        )
        assert r.status_code == 200, r.text
        picks = r.json() if isinstance(r.json(), list) else r.json().get("picks", [])
        # Should still be a list (could be empty if slate thin, but no crash)
        assert isinstance(picks, list)
        # Every returned pick should respect min_lock=70 (no relax below)
        for p in picks[:30]:
            ls = (p.get("lock_score") or p.get("lock_score_v2") or 0)
            # ELITE-bypass picks may be exempt; tolerate them
            if p.get("elite_bypass") or p.get("is_elite_anchor"):
                continue
            assert ls >= 70 or p.get("edge_percent") is None, (
                f"pick below floor: lock={ls} for {p.get('id')}"
            )

    def test_picks_today_market_override(self, demo_token):
        """Explicit market should NOT trigger auto-relax (user override path)."""
        r = requests.get(
            f"{BASE_URL}/api/picks/today?market=moneyline",
            headers=H(demo_token), timeout=10,
        )
        assert r.status_code == 200, r.text
        picks = r.json() if isinstance(r.json(), list) else r.json().get("picks", [])
        assert isinstance(picks, list)


# ─────────────────────────── Regression — existing endpoints ─────────────
class TestRegressionExistingPicks:
    def test_picks_all(self, demo_token):
        r = requests.get(f"{BASE_URL}/api/picks/all",
                         headers=H(demo_token), timeout=15)
        assert r.status_code == 200, r.text

    def test_under_of_the_day(self, demo_token):
        r = requests.get(f"{BASE_URL}/api/picks/under-of-the-day",
                         headers=H(demo_token), timeout=15)
        assert r.status_code == 200, r.text

    def test_rollover(self, demo_token):
        r = requests.get(f"{BASE_URL}/api/picks/rollover",
                         headers=H(demo_token), timeout=15)
        assert r.status_code == 200, r.text

    def test_login_demo_user(self):
        r = requests.post(
            f"{BASE_URL}/api/auth/login",
            json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD},
            timeout=15,
        )
        assert r.status_code == 200
        assert "access_token" in r.json()

    def test_login_admin_user(self):
        r = requests.post(
            f"{BASE_URL}/api/auth/login",
            json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
            timeout=15,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["user"]["role"] == "admin"

    def test_pick_detail(self, demo_token):
        # Fetch one pick id from today
        r = requests.get(
            f"{BASE_URL}/api/picks/today?lite=true",
            headers=H(demo_token), timeout=10,
        )
        picks = r.json() if isinstance(r.json(), list) else r.json().get("picks", [])
        if not picks:
            pytest.skip("no picks to test detail endpoint")
        pid = picks[0].get("id") or picks[0].get("_id") or picks[0].get("pick_id")
        assert pid, f"no id in pick: {picks[0]}"
        r2 = requests.get(
            f"{BASE_URL}/api/picks/{pid}",
            headers=H(demo_token), timeout=15,
        )
        assert r2.status_code == 200, r2.text
        body = r2.json()
        # Enriched payload should at least include sport/market/lock_score
        for k in ("sport", "market"):
            assert k in body, f"pick detail missing key: {k}"


# ─────────────── Circuit breaker stays healthy on clean run ──────────────
class TestCircuitHealth:
    def test_circuit_not_tripped_post_picks(self, admin_token, demo_token):
        # Hit picks endpoint, then re-read diagnostic
        requests.get(f"{BASE_URL}/api/picks/today?lite=true",
                     headers=H(demo_token), timeout=15)
        r = requests.get(
            f"{BASE_URL}/api/admin/odds-diagnostic",
            headers=H(admin_token), timeout=15,
        )
        oa = r.json()["odds_api"]
        assert oa["disabled"] is False, (
            f"breaker tripped on clean run: reason={oa['disabled_reason']}, "
            f"last_err={oa['last_error']}"
        )
        # Fail counter should be well below trip thresholds (2 for 401, 8 for any)
        assert oa["consecutive_401s"] < 2
        assert oa["consecutive_failures"] < 8
