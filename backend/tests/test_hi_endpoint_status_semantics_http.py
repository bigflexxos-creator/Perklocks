"""HTTP-level regression for HI status semantics on the Trevor Lawrence
pick (target of iteration 142 P0 parity fix)."""
import os
import pytest
import requests

BASE_URL = os.environ.get("EXPO_BACKEND_URL",
                          "https://canonical-parity.preview.emergentagent.com").rstrip("/")
PICK_ID = "6d76acfa-6c03-560f-b0b8-8a1626adeee6"


@pytest.fixture(scope="module")
def token():
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"email": "demo@lockscore.ai", "password": "demo123"},
                      timeout=20)
    assert r.status_code == 200, r.text
    tok = r.json().get("access_token") or r.json().get("token")
    assert tok, f"no token in {r.json()}"
    return tok


@pytest.fixture(scope="module")
def headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _hi(headers, sample_scope="L10", venue_scope="ALL"):
    return requests.get(
        f"{BASE_URL}/api/picks/{PICK_ID}/historical-intelligence",
        params={"sample_scope": sample_scope, "venue_scope": venue_scope},
        headers=headers, timeout=20,
    )


def test_L10_ALL_returns_available_with_data(headers):
    r = _hi(headers)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "AVAILABLE_WITH_DATA"
    assert d["sample_size"] == 10
    assert d["data_coverage"]["total_observations"] == 80
    sb = d.get("served_by") or {}
    assert sb.get("host"), sb
    assert sb.get("db"), sb


def test_L5_ALL_available_with_data(headers):
    r = _hi(headers, sample_scope="L5")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "AVAILABLE_WITH_DATA"
    assert d["sample_size"] == 5


def test_L10_HOME_available_with_data(headers):
    r = _hi(headers, sample_scope="L10", venue_scope="HOME")
    assert r.status_code == 200, r.text
    d = r.json()
    # Source-level availability; venue filter may reduce sample size
    assert d["status"] == "AVAILABLE_WITH_DATA"


def test_unknown_pick_returns_404(headers):
    r = requests.get(
        f"{BASE_URL}/api/picks/does-not-exist-zzz/historical-intelligence",
        params={"sample_scope": "L10", "venue_scope": "ALL"},
        headers=headers, timeout=20,
    )
    assert r.status_code == 404
