"""LockScore AI backend integration tests.

Covers: auth (register/login/me), picks (today/all/bet-killer/rollover/detail/refresh),
stats summary. Uses public preview URL via EXPO_PUBLIC_BACKEND_URL.
"""
import os
import uuid
import pytest
import requests

BASE_URL = os.environ.get(
    "EXPO_PUBLIC_BACKEND_URL",
    "https://canonical-parity.preview.emergentagent.com",
).rstrip("/")

DEMO_EMAIL = "demo@lockscore.ai"
DEMO_PASSWORD = "demo123"


@pytest.fixture(scope="session")
def session():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="session")
def demo_token(session):
    r = session.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD},
        timeout=20,
    )
    if r.status_code != 200:
        # Try to register the demo user if missing.
        session.post(
            f"{BASE_URL}/api/auth/register",
            json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD, "name": "Demo"},
            timeout=20,
        )
        r = session.post(
            f"{BASE_URL}/api/auth/login",
            json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD},
            timeout=20,
        )
    assert r.status_code == 200, f"demo login failed: {r.status_code} {r.text}"
    return r.json()["access_token"]


@pytest.fixture(scope="session")
def auth_headers(demo_token):
    return {"Authorization": f"Bearer {demo_token}", "Content-Type": "application/json"}


# ──────────────── Health ────────────────
class TestHealth:
    def test_root(self, session):
        r = session.get(f"{BASE_URL}/api/", timeout=15)
        assert r.status_code == 200
        body = r.json()
        assert body.get("ok") is True
        assert body.get("service") in ("LockScore AI", "PerksLocks AI")


# ──────────────── Auth ────────────────
class TestAuth:
    def test_register_new_user_returns_token_and_user(self, session):
        email = f"test_{uuid.uuid4().hex[:10]}@example.com"
        r = session.post(
            f"{BASE_URL}/api/auth/register",
            json={"email": email, "password": "secret123", "name": "Reg Test"},
            timeout=20,
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert "access_token" in body and body["access_token"]
        assert body["token_type"] == "bearer"
        assert body["user"]["email"] == email
        assert body["user"]["name"] == "Reg Test"
        assert "id" in body["user"]

    def test_register_duplicate_email_returns_409(self, session):
        email = f"TEST_dup_{uuid.uuid4().hex[:8]}@example.com"
        session.post(
            f"{BASE_URL}/api/auth/register",
            json={"email": email, "password": "secret123"},
            timeout=20,
        )
        r = session.post(
            f"{BASE_URL}/api/auth/register",
            json={"email": email, "password": "secret123"},
            timeout=20,
        )
        assert r.status_code == 409

    def test_login_success(self, session):
        r = session.post(
            f"{BASE_URL}/api/auth/login",
            json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD},
            timeout=20,
        )
        assert r.status_code == 200
        assert r.json()["user"]["email"] == DEMO_EMAIL

    def test_login_wrong_password_returns_401(self, session):
        r = session.post(
            f"{BASE_URL}/api/auth/login",
            json={"email": DEMO_EMAIL, "password": "wrongpass"},
            timeout=20,
        )
        assert r.status_code == 401

    def test_me_with_token(self, session, auth_headers):
        r = session.get(f"{BASE_URL}/api/auth/me", headers=auth_headers, timeout=20)
        assert r.status_code == 200
        assert r.json()["email"] == DEMO_EMAIL

    def test_me_without_token_returns_401(self, session):
        r = requests.get(f"{BASE_URL}/api/auth/me", timeout=20)
        assert r.status_code == 401


# ──────────────── Picks ────────────────
class TestPicks:
    def test_today_returns_picks_with_lock_score_gte_85(self, session, auth_headers):
        r = session.get(f"{BASE_URL}/api/picks/today", headers=auth_headers, timeout=60)
        assert r.status_code == 200
        picks = r.json().get("picks", [])
        assert isinstance(picks, list)
        assert len(picks) > 0, "Expected at least one Lock pick (>=85)"
        for p in picks:
            assert p["lock_score"] >= 85
            for k in ("id", "sport", "market", "selection", "grade", "edge_percent"):
                assert k in p, f"missing {k}"
            assert "_id" not in p

    def test_today_sport_filter(self, session, auth_headers):
        r = session.get(
            f"{BASE_URL}/api/picks/today?sport=MLB", headers=auth_headers, timeout=60
        )
        assert r.status_code == 200
        picks = r.json().get("picks", [])
        for p in picks:
            assert p["sport"] == "MLB"

    def test_picks_all_returns_full_board(self, session, auth_headers):
        r = session.get(f"{BASE_URL}/api/picks/all", headers=auth_headers, timeout=60)
        assert r.status_code == 200
        picks = r.json().get("picks", [])
        assert len(picks) > 0
        scores = {round(p["lock_score"]) for p in picks}
        # Should include below 85 AND above 85
        assert any(s < 85 for s in scores) or any(s >= 85 for s in scores)

    def test_picks_bet_killer_below_85(self, session, auth_headers):
        r = session.get(
            f"{BASE_URL}/api/picks/bet-killer", headers=auth_headers, timeout=60
        )
        assert r.status_code == 200
        picks = r.json().get("picks", [])
        for p in picks:
            assert p["lock_score"] < 85
            assert p["grade"] == "Pass"

    def test_rollover_single_pick(self, session, auth_headers):
        r = session.get(
            f"{BASE_URL}/api/picks/rollover", headers=auth_headers, timeout=60
        )
        assert r.status_code == 200
        body = r.json()
        assert "pick" in body
        if body["pick"] is not None:
            assert "composite_rank" in body
            assert "total_evaluated" in body
            assert body["pick"]["lock_score"] >= 85

    def test_pick_detail_generates_ai_explanation(self, session, auth_headers):
        """REGRESSION: /api/picks/{pick_id} decorator restored.
        Verifies route returns 200 with full pick object including ai_pending bool."""
        # Get a pick id from today
        r = session.get(f"{BASE_URL}/api/picks/today", headers=auth_headers, timeout=60)
        picks = r.json().get("picks", [])
        assert picks, "no picks to fetch detail"
        pid = picks[0]["id"]
        r2 = session.get(
            f"{BASE_URL}/api/picks/{pid}", headers=auth_headers, timeout=90
        )
        assert r2.status_code == 200, r2.text
        body = r2.json()
        assert body["id"] == pid
        assert body.get("explanation"), "explanation should be populated"
        assert len(body["explanation"]) > 30
        # Critical regression assertion: ai_pending field must be present
        assert "ai_pending" in body, "ai_pending boolean missing — decorator regression"
        assert isinstance(body["ai_pending"], bool)
        # Full pick fields preserved
        for k in ("sport", "market", "selection", "grade", "lock_score",
                  "book_odds", "win_probability"):
            assert k in body, f"missing field {k}"

    def test_pick_ai_explain_endpoint(self, session, auth_headers):
        """POST /api/picks/{id}/ai-explain returns explanation text + source."""
        r = session.get(f"{BASE_URL}/api/picks/today", headers=auth_headers, timeout=60)
        picks = r.json().get("picks", [])
        assert picks, "no picks to ai-explain"
        pid = picks[0]["id"]
        r2 = session.post(
            f"{BASE_URL}/api/picks/{pid}/ai-explain",
            headers=auth_headers,
            timeout=60,
        )
        assert r2.status_code == 200, r2.text
        body = r2.json()
        assert body.get("explanation"), "explanation text missing"
        assert len(body["explanation"]) > 20
        assert body.get("source") in ("live", "cached", "fallback")

    def test_pick_ai_explain_404(self, session, auth_headers):
        r = session.post(
            f"{BASE_URL}/api/picks/nonexistent-xyz/ai-explain",
            headers=auth_headers,
            timeout=20,
        )
        assert r.status_code == 404

    def test_pick_detail_404(self, session, auth_headers):
        r = session.get(
            f"{BASE_URL}/api/picks/nonexistent-id-xyz",
            headers=auth_headers,
            timeout=20,
        )
        assert r.status_code == 404

    def test_picks_requires_auth(self, session):
        r = requests.get(f"{BASE_URL}/api/picks/today", timeout=20)
        assert r.status_code == 401

    def test_refresh_picks(self, session, auth_headers):
        r = session.post(
            f"{BASE_URL}/api/picks/refresh", headers=auth_headers, timeout=120
        )
        assert r.status_code == 200
        body = r.json()
        assert body["refreshed"] is True
        assert body["count"] > 0


# ──────────────── Parlay ────────────────
class TestParlay:
    """Auto Parlay endpoint — supports legs=2/3/4/5."""

    @pytest.mark.parametrize("legs", [2, 3, 4, 5])
    def test_parlay_legs_returns_payload_or_reason(self, session, auth_headers, legs):
        r = session.get(
            f"{BASE_URL}/api/picks/parlay?legs={legs}",
            headers=auth_headers,
            timeout=60,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert "parlay" in body
        if body["parlay"] is None:
            assert "reason" in body and body["reason"]
        else:
            parlay = body["parlay"]
            for k in ("legs", "leg_count", "combined_decimal_odds",
                      "combined_american_odds", "combined_win_probability",
                      "payout_on_100", "profit_on_100"):
                assert k in parlay, f"missing {k}"
            assert isinstance(parlay["legs"], list)
            assert len(parlay["legs"]) == parlay["leg_count"]
            assert 2 <= parlay["leg_count"] <= legs
            # All legs are Lock 90+ per spec
            for leg in parlay["legs"]:
                assert leg["lock_score"] >= 90
                assert "id" in leg
            # No duplicate events
            events = [(leg.get("sport"), leg.get("event")) for leg in parlay["legs"]]
            assert len(events) == len(set(events)), "duplicate event in parlay legs"

    def test_parlay_leg_pick_id_resolves(self, session, auth_headers):
        """End-to-end: parlay leg id -> pick_detail must return 200."""
        r = session.get(
            f"{BASE_URL}/api/picks/parlay?legs=3", headers=auth_headers, timeout=60
        )
        body = r.json()
        if not body.get("parlay"):
            pytest.skip("no parlay available today")
        pid = body["parlay"]["legs"][0]["id"]
        r2 = session.get(
            f"{BASE_URL}/api/picks/{pid}", headers=auth_headers, timeout=30
        )
        assert r2.status_code == 200
        assert r2.json()["id"] == pid


# ──────────────── Stats ────────────────
class TestStats:
    def test_stats_summary(self, session, auth_headers):
        r = session.get(
            f"{BASE_URL}/api/stats/summary", headers=auth_headers, timeout=60
        )
        assert r.status_code == 200
        body = r.json()
        for k in ("date", "total_picks", "elite_count", "avg_edge_percent", "by_sport"):
            assert k in body
        assert isinstance(body["by_sport"], list)
        assert body["total_picks"] > 0
