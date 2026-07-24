"""Admin Dashboard backend tests (iter52).

Covers:
- /api/admin/overview (admin OK, non-admin 403)
- /api/admin/users pagination + filters
- /api/admin/top-api-users sorting
- /api/admin/users/{id}/status suspend/resume + self-protect (409)
- Suspended user gets 403 on subsequent /api/* requests
- API usage tracker increments user_activity.api_calls
- Owner auto-promotion of bossmanperkins@yahoo.com on startup
- Admin login still works and returns user.role == 'admin'
"""
from __future__ import annotations

import os
import uuid
import time
import pytest
import requests
from pathlib import Path
from dotenv import load_dotenv
from pymongo import MongoClient

# Pull MONGO_URL/DB_NAME from backend/.env so we hit the same DB the server uses.
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

BASE_URL = (
    os.environ.get("EXPO_BACKEND_URL")
    or os.environ.get("EXPO_PUBLIC_BACKEND_URL")
    or "https://player-intel-engine.preview.emergentagent.com"
).rstrip("/")

MONGO_URL = os.environ.get("MONGO_URL") or "mongodb://localhost:27017"
DB_NAME = os.environ.get("DB_NAME") or "lockscore_db"

OWNER_EMAIL = "bossmanperkins@yahoo.com"
DEMO_EMAIL = "demo@lockscore.ai"
DEMO_PASSWORD = "demo123"

_mongo = MongoClient(MONGO_URL)
_dbsync = _mongo[DB_NAME]


# ───────────────────── fixtures ─────────────────────
@pytest.fixture(scope="session")
def session():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


def _register(session, email, password="testpass123", name=None):
    r = session.post(
        f"{BASE_URL}/api/auth/register",
        json={"email": email, "password": password, "name": name or email.split("@")[0]},
    )
    return r


def _login(session, email, password):
    return session.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": email, "password": password},
    )


@pytest.fixture(scope="session")
def admin_ctx(session):
    """Create a throwaway test user, promote to admin via direct Mongo update,
    return {token, id, email}."""
    email = f"TEST_admin_{uuid.uuid4().hex[:8]}@example.com"
    password = "AdminPass123!"
    r = _register(session, email, password=password, name="TEST Admin")
    assert r.status_code == 201, f"register failed: {r.status_code} {r.text}"
    data = r.json()
    user_id = data["user"]["id"]

    # Promote directly in Mongo.
    res = _dbsync.users.update_one(
        {"id": user_id},
        {"$set": {"role": "admin", "status": "active"}},
    )
    assert res.matched_count == 1, "direct promotion failed"

    # Re-login to get a fresh token (role on token doc reflects DB state at request time).
    r2 = _login(session, email, password)
    assert r2.status_code == 200, f"admin re-login: {r2.status_code} {r2.text}"
    token = r2.json()["access_token"]
    yield {"id": user_id, "email": email, "password": password, "token": token}

    # Cleanup
    try:
        _dbsync.users.delete_one({"id": user_id})
        _dbsync.user_activity.delete_one({"user_id": user_id})
    except Exception:
        pass


@pytest.fixture(scope="session")
def non_admin_ctx(session):
    email = f"TEST_user_{uuid.uuid4().hex[:8]}@example.com"
    password = "UserPass123!"
    r = _register(session, email, password=password, name="TEST User")
    assert r.status_code == 201, f"register failed: {r.status_code} {r.text}"
    data = r.json()
    yield {
        "id": data["user"]["id"],
        "email": email,
        "password": password,
        "token": data["access_token"],
    }
    try:
        _dbsync.users.delete_one({"email": email})
        _dbsync.user_activity.delete_one({"user_id": data["user"]["id"]})
    except Exception:
        pass


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


# ───────────────────── tests ─────────────────────

# Auto-promotion of owner email
class TestOwnerPromotion:
    def test_owner_auto_promoted_to_admin(self):
        """Owner must be admin if the row exists; if missing, the seed is a no-op,
        so we ensure a row exists then verify the role flips to admin on next boot.
        This test only asserts: IF the user exists, they have role == 'admin'.
        """
        doc = _dbsync.users.find_one({"email": OWNER_EMAIL})
        if not doc:
            pytest.skip(
                f"Owner {OWNER_EMAIL} not registered yet — promotion is idempotent "
                "and only runs against existing users."
            )
        assert doc.get("role") == "admin", (
            f"Owner {OWNER_EMAIL} should be admin, got role={doc.get('role')}"
        )
        assert (doc.get("status") or "active") == "active"


# /api/admin/overview
class TestOverview:
    def test_overview_admin_ok(self, session, admin_ctx):
        r = session.get(f"{BASE_URL}/api/admin/overview", headers=_auth(admin_ctx["token"]))
        assert r.status_code == 200, r.text
        data = r.json()
        assert "users" in data and "activity" in data
        for k in ("total", "admins", "suspended", "new_24h", "new_7d", "active_24h"):
            assert k in data["users"], f"users.{k} missing"
            assert isinstance(data["users"][k], int)
        for k in ("parlays_total", "parlays_24h", "picks_today"):
            assert k in data["activity"], f"activity.{k} missing"
        assert data["users"]["total"] >= 1
        assert data["users"]["admins"] >= 1  # at least our test admin

    def test_overview_non_admin_403(self, session, non_admin_ctx):
        r = session.get(f"{BASE_URL}/api/admin/overview", headers=_auth(non_admin_ctx["token"]))
        assert r.status_code == 403, f"expected 403, got {r.status_code} {r.text}"

    def test_overview_no_token_401(self, session):
        r = session.get(f"{BASE_URL}/api/admin/overview")
        assert r.status_code in (401, 403)


# /api/admin/users list
class TestUsersList:
    def test_list_users_page_size_50(self, session, admin_ctx):
        r = session.get(
            f"{BASE_URL}/api/admin/users",
            params={"page": 1, "page_size": 50},
            headers=_auth(admin_ctx["token"]),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        for k in ("users", "total", "page", "page_size", "pages"):
            assert k in body, f"{k} missing"
        assert body["page"] == 1
        assert body["page_size"] == 50
        assert isinstance(body["users"], list)
        assert len(body["users"]) <= 50
        if body["users"]:
            u = body["users"][0]
            for k in ("id", "email", "role", "status"):
                assert k in u
            # Hashed password MUST NOT leak
            assert "hashed_password" not in u
            assert "_id" not in u

    def test_list_users_non_admin_403(self, session, non_admin_ctx):
        r = session.get(
            f"{BASE_URL}/api/admin/users?page=1&page_size=50",
            headers=_auth(non_admin_ctx["token"]),
        )
        assert r.status_code == 403


# /api/admin/top-api-users
class TestTopApiUsers:
    def test_top_api_users_sorted(self, session, admin_ctx):
        # Generate activity for admin user
        for _ in range(3):
            session.get(f"{BASE_URL}/api/picks/today", headers=_auth(admin_ctx["token"]))
        time.sleep(0.5)
        r = session.get(
            f"{BASE_URL}/api/admin/top-api-users",
            headers=_auth(admin_ctx["token"]),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert "top" in body
        assert isinstance(body["top"], list)
        if len(body["top"]) >= 2:
            calls = [row.get("api_calls") or 0 for row in body["top"]]
            assert calls == sorted(calls, reverse=True), f"not sorted desc: {calls}"
        if body["top"]:
            row = body["top"][0]
            for k in ("user_id", "api_calls"):
                assert k in row

    def test_top_api_users_non_admin_403(self, session, non_admin_ctx):
        r = session.get(
            f"{BASE_URL}/api/admin/top-api-users",
            headers=_auth(non_admin_ctx["token"]),
        )
        assert r.status_code == 403


# Status mutation + suspension enforcement
class TestStatusMutation:
    def test_suspend_blocks_further_api_calls(self, session, admin_ctx):
        # Create throwaway victim
        victim_email = f"TEST_victim_{uuid.uuid4().hex[:8]}@example.com"
        victim_pw = "VictimPw123!"
        r = _register(session, victim_email, password=victim_pw)
        assert r.status_code == 201, r.text
        victim_token = r.json()["access_token"]
        victim_id = r.json()["user"]["id"]

        # Confirm pre-suspend call works
        pre = session.get(f"{BASE_URL}/api/auth/me", headers=_auth(victim_token))
        assert pre.status_code == 200, f"pre-suspend /me should 200, got {pre.status_code}"

        # Suspend
        r = session.post(
            f"{BASE_URL}/api/admin/users/{victim_id}/status",
            json={"status": "suspended"},
            headers=_auth(admin_ctx["token"]),
        )
        assert r.status_code == 200, r.text
        assert r.json().get("status") == "suspended"

        # Suspended → existing JWT should now 403 on protected endpoints
        post = session.get(f"{BASE_URL}/api/auth/me", headers=_auth(victim_token))
        assert post.status_code == 403, (
            f"suspended user should be 403, got {post.status_code} {post.text}"
        )

        # Resume → /me works again
        r = session.post(
            f"{BASE_URL}/api/admin/users/{victim_id}/status",
            json={"status": "active"},
            headers=_auth(admin_ctx["token"]),
        )
        assert r.status_code == 200, r.text
        assert r.json().get("status") == "active"

        resumed = session.get(f"{BASE_URL}/api/auth/me", headers=_auth(victim_token))
        assert resumed.status_code == 200, (
            f"resumed user should be 200, got {resumed.status_code} {resumed.text}"
        )

        # Cleanup
        try:
            _dbsync.users.delete_one({"id": victim_id})
            _dbsync.user_activity.delete_one({"user_id": victim_id})
        except Exception:
            pass

    def test_cannot_suspend_self(self, session, admin_ctx):
        r = session.post(
            f"{BASE_URL}/api/admin/users/{admin_ctx['id']}/status",
            json={"status": "suspended"},
            headers=_auth(admin_ctx["token"]),
        )
        assert r.status_code == 409, f"expected 409, got {r.status_code} {r.text}"

    def test_status_non_admin_forbidden(self, session, non_admin_ctx, admin_ctx):
        r = session.post(
            f"{BASE_URL}/api/admin/users/{admin_ctx['id']}/status",
            json={"status": "suspended"},
            headers=_auth(non_admin_ctx["token"]),
        )
        assert r.status_code == 403


# API usage tracker middleware
class TestApiUsageTracker:
    def test_three_api_calls_increment_counter(self, session):
        # Fresh user so we know exact starting count
        email = f"TEST_tracker_{uuid.uuid4().hex[:8]}@example.com"
        pw = "TrackerPw123!"
        r = _register(session, email, password=pw)
        assert r.status_code == 201, r.text
        token = r.json()["access_token"]
        uid = r.json()["user"]["id"]

        before = _dbsync.user_activity.find_one({"user_id": uid})
        start_count = (before or {}).get("api_calls", 0)

        for _ in range(3):
            rr = session.get(f"{BASE_URL}/api/picks/today", headers=_auth(token))
            assert rr.status_code in (200, 404, 500), f"unexpected: {rr.status_code}"

        # Middleware is fire-and-forget; give Mongo a beat
        time.sleep(1.5)

        after = _dbsync.user_activity.find_one({"user_id": uid})
        assert after is not None, "user_activity row should exist after API calls"
        end_count = after.get("api_calls", 0)
        delta = end_count - start_count
        assert delta >= 3, f"expected ≥3 increments, got {delta} (start={start_count} end={end_count})"
        assert "last_call_at" in after

        # Cleanup
        try:
            _dbsync.users.delete_one({"id": uid})
            _dbsync.user_activity.delete_one({"user_id": uid})
        except Exception:
            pass


# Admin login still returns role=admin
class TestAdminLoginShape:
    def test_admin_login_returns_role_admin(self, session, admin_ctx):
        r = _login(session, admin_ctx["email"], admin_ctx["password"])
        assert r.status_code == 200, r.text
        body = r.json()
        assert "access_token" in body
        assert "user" in body
        user = body["user"]
        # iter53 patch: login response must now carry role+status straight from DB.
        assert user.get("role") == "admin", (
            f"login response should show user.role='admin' for admins, got {user.get('role')!r}"
        )
        assert (user.get("status") or "active") == "active", (
            f"login response should show user.status='active', got {user.get('status')!r}"
        )
        # And /auth/me agrees (read straight from DB).
        me = session.get(f"{BASE_URL}/api/auth/me", headers=_auth(body["access_token"]))
        assert me.status_code == 200
        me_body = me.json()
        assert me_body.get("role") == "admin"
        assert (me_body.get("status") or "active") == "active"


# ───── iter53 patch: login role/status forwarding + suspended 403 + last_login_at ─────
class TestLoginRoleForwarding:
    """Verify iter53 patches on POST /api/auth/login."""

    def test_regular_user_login_returns_role_user_status_active(self, session):
        """A freshly-registered, non-promoted user must come back as role=user, status=active."""
        email = f"TEST_loginrole_{uuid.uuid4().hex[:8]}@example.com"
        password = "RegularPw123!"
        r = _register(session, email, password=password)
        assert r.status_code == 201, r.text
        uid = r.json()["user"]["id"]

        r = _login(session, email, password)
        assert r.status_code == 200, r.text
        body = r.json()
        user = body["user"]
        assert user.get("role") == "user", (
            f"regular user login should show role='user', got {user.get('role')!r}"
        )
        assert user.get("status") == "active", (
            f"regular user login should show status='active', got {user.get('status')!r}"
        )
        # And the response shape is otherwise sane.
        for k in ("id", "email"):
            assert k in user
        assert "hashed_password" not in user

        # Cleanup
        try:
            _dbsync.users.delete_one({"id": uid})
            _dbsync.user_activity.delete_one({"user_id": uid})
        except Exception:
            pass

    def test_suspended_user_login_returns_403(self, session, admin_ctx):
        """Suspended users must NOT be able to mint a JWT — login returns 403."""
        email = f"TEST_sus_login_{uuid.uuid4().hex[:8]}@example.com"
        password = "SusPw123!"
        r = _register(session, email, password=password)
        assert r.status_code == 201, r.text
        uid = r.json()["user"]["id"]

        # Pre-suspend, login works (200 + token).
        pre = _login(session, email, password)
        assert pre.status_code == 200, f"pre-suspend login should 200, got {pre.status_code}"
        assert "access_token" in pre.json()

        # Suspend via admin endpoint
        sr = session.post(
            f"{BASE_URL}/api/admin/users/{uid}/status",
            json={"status": "suspended"},
            headers=_auth(admin_ctx["token"]),
        )
        assert sr.status_code == 200, sr.text
        assert sr.json().get("status") == "suspended"

        # Login MUST now 403, NOT 200
        post = _login(session, email, password)
        assert post.status_code == 403, (
            f"suspended user login should 403, got {post.status_code} body={post.text!r}"
        )
        # No JWT leaked
        try:
            body = post.json()
            assert "access_token" not in body, "suspended login must not return access_token"
        except ValueError:
            pass

        # Cleanup
        try:
            _dbsync.users.delete_one({"id": uid})
            _dbsync.user_activity.delete_one({"user_id": uid})
        except Exception:
            pass

    def test_login_stamps_last_login_at(self, session):
        """Every successful login bumps users.last_login_at to a fresh UTC ISO timestamp."""
        from datetime import datetime, timezone, timedelta

        email = f"TEST_lastlogin_{uuid.uuid4().hex[:8]}@example.com"
        password = "StampPw123!"
        r = _register(session, email, password=password)
        assert r.status_code == 201, r.text
        uid = r.json()["user"]["id"]

        # Sanity: registration shouldn't have already stamped a last_login_at
        # (only login should). If it did, that's fine — we just measure deltas.
        before_doc = _dbsync.users.find_one({"id": uid}) or {}
        before_ts = before_doc.get("last_login_at")

        time.sleep(1.1)  # ensure visible delta
        r2 = _login(session, email, password)
        assert r2.status_code == 200, r2.text

        # Mongo write is awaited inside the login handler, so it's present by the
        # time the HTTP response returns. Still give a small beat for robustness.
        time.sleep(0.3)
        after_doc = _dbsync.users.find_one({"id": uid}) or {}
        after_ts = after_doc.get("last_login_at")
        assert after_ts, f"users.last_login_at not set after login; doc={after_doc}"

        # Parse + assert it's recent (last 60s) and in UTC.
        try:
            parsed = datetime.fromisoformat(after_ts.replace("Z", "+00:00"))
        except Exception as e:
            pytest.fail(f"last_login_at not ISO-parseable: {after_ts!r} err={e}")
        now = datetime.now(timezone.utc)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        delta = abs((now - parsed).total_seconds())
        assert delta < 60, f"last_login_at not fresh: {after_ts} (delta={delta}s)"

        # And must have advanced vs. the pre-login value if one existed.
        if before_ts:
            assert after_ts != before_ts, (
                f"last_login_at not updated; before={before_ts} after={after_ts}"
            )

        # Login again, assert it advances again.
        time.sleep(1.1)
        r3 = _login(session, email, password)
        assert r3.status_code == 200
        time.sleep(0.3)
        again = _dbsync.users.find_one({"id": uid}) or {}
        again_ts = again.get("last_login_at")
        assert again_ts and again_ts != after_ts, (
            f"second login should bump last_login_at again; first={after_ts} second={again_ts}"
        )

        # Cleanup
        try:
            _dbsync.users.delete_one({"id": uid})
            _dbsync.user_activity.delete_one({"user_id": uid})
        except Exception:
            pass
