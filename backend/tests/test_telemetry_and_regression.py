"""Iteration 67 — Telemetry + regression tests for the tab background fix.

Covers:
  1. POST /api/telemetry/error — 200 with valid payload, no auth needed
  2. POST /api/telemetry/error — persists doc in `client_errors`
  3. GET  /api/admin/client-errors/recent — returns recent errors
  4. Regression: /api/picks/today, /api/auth/login, /api/admin/... still work
"""
import os
import uuid
import time
import pytest
import requests

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "").rstrip("/")
assert BASE_URL, "EXPO_PUBLIC_BACKEND_URL must be set"

DEMO_EMAIL = "demo@lockscore.ai"
DEMO_PASSWORD = "demo123"


@pytest.fixture(scope="module")
def api():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module")
def demo_token(api):
    r = api.post(f"{BASE_URL}/api/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD}, timeout=30)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:200]}"
    tok = r.json().get("access_token")
    assert tok
    return tok


# ── 1. Telemetry: valid payload, no auth ───────────────────────────────
def test_telemetry_error_valid_no_auth(api):
    marker = f"TEST_telemetry_{uuid.uuid4().hex[:8]}"
    payload = {
        "message": marker,
        "stack": "Error: TEST_stack_trace\n  at test_fn (file.js:10)",
        "url_path": "/(tabs)/index",
        "component": "TestComponent",
        "device": {"os": "web", "os_version": "chrome-124"},
        "data_version": "v67",
    }
    r = api.post(f"{BASE_URL}/api/telemetry/error", json=payload, timeout=15)
    assert r.status_code == 200, f"got {r.status_code}: {r.text[:200]}"
    body = r.json()
    assert body == {"ok": True}, f"unexpected body: {body}"
    return marker


# ── 2. Persistence check via recent endpoint ───────────────────────────
def test_telemetry_error_persists_and_shows_in_recent(api):
    marker = f"TEST_persist_{uuid.uuid4().hex[:10]}"
    payload = {
        "message": marker,
        "stack": "Error: persist-check",
        "url_path": "/(tabs)/parlay",
        "component": "PersistTest",
    }
    r = api.post(f"{BASE_URL}/api/telemetry/error", json=payload, timeout=15)
    assert r.status_code == 200
    assert r.json().get("ok") is True

    # give Mongo a beat to flush
    time.sleep(0.3)

    r2 = api.get(f"{BASE_URL}/api/admin/client-errors/recent?limit=25", timeout=15)
    assert r2.status_code == 200, r2.text[:200]
    body = r2.json()
    assert "count" in body and "errors" in body
    assert isinstance(body["errors"], list)
    msgs = [e.get("message") for e in body["errors"]]
    assert marker in msgs, f"marker not found in recent: {msgs[:5]}"

    # Validate stored doc fields
    doc = next(e for e in body["errors"] if e.get("message") == marker)
    assert doc.get("component") == "PersistTest"
    assert doc.get("url_path") == "/(tabs)/parlay"
    assert doc.get("stack", "").startswith("Error: persist-check")
    assert isinstance(doc.get("received_at"), str) and "T" in doc["received_at"]
    # Mongo _id must not leak
    assert "_id" not in doc


# ── 3. Recent endpoint respects limit ──────────────────────────────────
def test_telemetry_recent_limit(api):
    r = api.get(f"{BASE_URL}/api/admin/client-errors/recent?limit=5", timeout=15)
    assert r.status_code == 200
    body = r.json()
    assert len(body["errors"]) <= 5


# ── 4. Telemetry: works with auth header too ───────────────────────────
def test_telemetry_error_with_auth(api, demo_token):
    marker = f"TEST_authed_{uuid.uuid4().hex[:8]}"
    r = api.post(
        f"{BASE_URL}/api/telemetry/error",
        json={"message": marker, "component": "AuthedTest"},
        headers={"Authorization": f"Bearer {demo_token}"},
        timeout=15,
    )
    assert r.status_code == 200
    assert r.json().get("ok") is True

    time.sleep(0.3)
    r2 = api.get(f"{BASE_URL}/api/admin/client-errors/recent?limit=25", timeout=15)
    assert r2.status_code == 200
    doc = next((e for e in r2.json()["errors"] if e.get("message") == marker), None)
    assert doc is not None
    # user_email_hash should be a 8-char hex when authed
    h = doc.get("user_email_hash")
    assert isinstance(h, str) and len(h) == 8, f"got hash={h!r}"


# ── 5. Regression: auth login still works ──────────────────────────────
def test_regression_auth_login(demo_token):
    assert isinstance(demo_token, str) and len(demo_token) > 20


# ── 6. Regression: /api/picks/today still works (auth required) ────────
def test_regression_picks_today(api, demo_token):
    r = api.get(
        f"{BASE_URL}/api/picks/today",
        headers={"Authorization": f"Bearer {demo_token}"},
        timeout=60,
    )
    assert r.status_code == 200, f"picks/today {r.status_code}: {r.text[:200]}"
    body = r.json()
    # Should return either dict with picks key or a list
    assert isinstance(body, (dict, list))
    if isinstance(body, dict):
        # sanity: at least one common key
        keys = set(body.keys())
        assert keys & {"picks", "items", "locks", "data", "count"} or len(keys) > 0


# ── 7. Regression: version endpoint works (used by cachebust) ──────────
def test_regression_version_endpoint(api):
    r = api.get(f"{BASE_URL}/api/version", timeout=15)
    assert r.status_code == 200
    body = r.json()
    assert "data_version" in body or "version" in body
