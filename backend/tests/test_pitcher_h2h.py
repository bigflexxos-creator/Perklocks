"""Tests for /api/picks/{pick_id}/pitcher-h2h endpoint and helpers.

Covers:
  • Backend endpoint returns 200 with required fields for MLB strikeout pick
  • opp_team is correctly resolved (NOT same as the pitcher's team)
  • 404 returned for non-MLB / non-strikeout picks
  • /api/version returns data_version='2026.06.22-pitcher-h2h-ui'
  • resolve_opp_team_name helper unit tests (KC vs STL, TEX vs SD)
"""
import os
import sys
import pytest
import requests

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    BASE_URL = "https://canonical-parity.preview.emergentagent.com"

DEMO_EMAIL = "demo@lockscore.ai"
DEMO_PASSWORD = "demo123"

# Make backend importable for unit test of resolve_opp_team_name
sys.path.insert(0, "/app/backend")


@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module")
def auth_token(session):
    r = session.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD},
        timeout=15,
    )
    if r.status_code != 200:
        pytest.skip(f"Demo auth failed: {r.status_code} {r.text[:200]}")
    return r.json().get("access_token")


@pytest.fixture(scope="module")
def auth_headers(auth_token):
    return {"Authorization": f"Bearer {auth_token}"}


# -------- /api/version --------

def test_version_data_version(session):
    r = session.get(f"{BASE_URL}/api/version", timeout=10)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j.get("data_version") == "2026.06.22-pitcher-h2h-ui", (
        f"data_version mismatch: got {j.get('data_version')!r}"
    )


# -------- resolve_opp_team_name unit tests --------

def test_resolve_opp_team_kc_at_stl():
    from mlb_pitcher_h2h import resolve_opp_team_name
    # Cameron is on KC; event = "St. Louis Cardinals @ Kansas City Royals"
    opp = resolve_opp_team_name("St. Louis Cardinals @ Kansas City Royals", "KC")
    assert opp == "St. Louis Cardinals", f"expected STL, got {opp!r}"


def test_resolve_opp_team_tex_vs_sd():
    from mlb_pitcher_h2h import resolve_opp_team_name
    # deGrom on TEX; event = "Texas Rangers vs San Diego Padres"
    opp = resolve_opp_team_name("Texas Rangers vs San Diego Padres", "TEX")
    assert opp == "San Diego Padres", f"expected SD, got {opp!r}"


def test_resolve_opp_team_reverse_order():
    from mlb_pitcher_h2h import resolve_opp_team_name
    # SD pitcher in same game — should return Texas
    opp = resolve_opp_team_name("Texas Rangers vs San Diego Padres", "SD")
    assert opp == "Texas Rangers", f"expected TEX, got {opp!r}"


# -------- Endpoint tests --------

MLB_K_PICK_ID = "48600ae0-0268-5e82-99f2-8aab24d563cd"  # Noah Cameron (KC) Over 2.5 K


def test_pitcher_h2h_unauthorized(session):
    r = session.get(f"{BASE_URL}/api/picks/{MLB_K_PICK_ID}/pitcher-h2h", timeout=15)
    assert r.status_code in (401, 403), f"expected 401/403, got {r.status_code}"


def test_pitcher_h2h_mlb_strikeout_pick(session, auth_headers):
    r = session.get(
        f"{BASE_URL}/api/picks/{MLB_K_PICK_ID}/pitcher-h2h",
        headers=auth_headers,
        timeout=30,
    )
    # Could be 404 if seed pick is missing in this environment — surface that
    if r.status_code == 404:
        pytest.skip(f"Pick {MLB_K_PICK_ID} not in DB this env: {r.text}")
    assert r.status_code == 200, f"status={r.status_code} body={r.text[:300]}"
    j = r.json()
    # Required fields
    for f in ("pitcher", "opp_team", "ok"):
        assert f in j, f"missing field {f!r} in {j}"
    # Pitcher name should resolve to a Cameron
    assert "Cameron" in (j.get("pitcher") or ""), f"pitcher unexpected: {j.get('pitcher')!r}"
    # opp_team must NOT be Kansas City (the pitcher's own team)
    opp = (j.get("opp_team") or "").lower()
    assert "kansas city" not in opp, f"opp_team should not be pitcher's own team: {j.get('opp_team')!r}"
    # opp_team should be the Cardinals for this seed
    assert "cardinals" in opp or "st. louis" in opp, f"opp_team unexpected: {j.get('opp_team')!r}"
    # If ok=True ensure stats fields are typed properly
    if j.get("ok"):
        assert isinstance(j.get("season_avg_k", 0), (int, float))
        assert isinstance(j.get("vs_team_avg_k", 0), (int, float))
        assert isinstance(j.get("vs_team_recent", []), list)


def test_pitcher_h2h_non_strikeout_returns_404(session, auth_headers):
    """Find any non-strikeout pick and ensure endpoint returns 404."""
    r = session.get(f"{BASE_URL}/api/picks/today?sport=Soccer", headers=auth_headers, timeout=20)
    if r.status_code != 200:
        pytest.skip(f"/api/picks/today not accessible: {r.status_code}")
    picks_resp = r.json()
    picks = picks_resp if isinstance(picks_resp, list) else picks_resp.get("picks") or picks_resp.get("items") or []
    non_k = None
    for p in picks:
        sport = (p.get("sport") or "")
        market = (p.get("market") or "").lower()
        if sport != "MLB" or "strikeout" not in market:
            non_k = p
            break
    if not non_k:
        pytest.skip("No non-MLB/non-strikeout pick available in /api/picks")
    pid = non_k.get("id")
    r2 = session.get(
        f"{BASE_URL}/api/picks/{pid}/pitcher-h2h",
        headers=auth_headers,
        timeout=15,
    )
    assert r2.status_code == 404, (
        f"expected 404 for non-strikeout pick (sport={non_k.get('sport')!r} "
        f"market={non_k.get('market')!r}) got {r2.status_code}: {r2.text[:200]}"
    )


def test_pitcher_h2h_missing_pick_returns_404(session, auth_headers):
    r = session.get(
        f"{BASE_URL}/api/picks/nonexistent-pick-id-zzz/pitcher-h2h",
        headers=auth_headers,
        timeout=15,
    )
    assert r.status_code == 404
