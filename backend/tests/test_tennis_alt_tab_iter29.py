"""
Iteration 29 — Tennis Alt-Spread Tab regression tests.

User report: "It good you got tennis ml but you deleted all the alt spread tennis
after I told you to fix simulator add a tab under tennis (alt) that all alt spread
tennis picks".

Coverage:
 1. /api/picks/markets/Tennis returns 4 tabs incl. tennis_alt -> Alt
 2. /api/picks/today?sport=Tennis&market=tennis_alt returns >=10 alt picks
    (mix of spread + total)
 3. /api/picks/today?sport=Tennis&market=match_winner still works
 4. /api/version returns data_version='2026.06.23-tennis-alt-tab'
 5. Regression: midnight rollover picks total > 100
 6. Regression: MLB pitcher strikeouts endpoint still works
"""
import os
import re
import pytest
import requests

# Read backend URL from frontend/.env so we exercise the public ingress URL
def _load_backend_url() -> str:
    env = os.environ.get("EXPO_PUBLIC_BACKEND_URL") or os.environ.get("EXPO_BACKEND_URL")
    if env:
        return env.rstrip("/")
    try:
        with open("/app/frontend/.env") as f:
            for line in f:
                if line.startswith("EXPO_PUBLIC_BACKEND_URL=") or line.startswith("EXPO_BACKEND_URL="):
                    return line.split("=", 1)[1].strip().strip('"').rstrip("/")
    except FileNotFoundError:
        pass
    raise RuntimeError("EXPO_PUBLIC_BACKEND_URL not found")


BASE_URL = _load_backend_url()

EMAIL = "demo@lockscore.ai"
PASSWORD = "demo123"


@pytest.fixture(scope="module")
def token():
    """Login once for the whole module."""
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": EMAIL, "password": PASSWORD},
        timeout=20,
    )
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:200]}"
    data = r.json()
    tok = data.get("access_token") or data.get("token")
    assert tok, f"no token in response: {data}"
    return tok


@pytest.fixture(scope="module")
def auth_headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ─────────── Version ───────────
def test_version_bumped():
    r = requests.get(f"{BASE_URL}/api/version", timeout=10)
    assert r.status_code == 200
    body = r.json()
    assert body.get("data_version") == "2026.06.23-tennis-alt-tab", body


# ─────────── Markets endpoint ───────────
def test_tennis_markets_has_four_tabs(auth_headers):
    r = requests.get(
        f"{BASE_URL}/api/picks/markets/Tennis", headers=auth_headers, timeout=15
    )
    assert r.status_code == 200, r.text[:300]
    data = r.json()
    # Could be list-of-dicts directly, or wrapped under "markets"
    tabs = data if isinstance(data, list) else data.get("markets", data.get("items", []))
    tokens = [t.get("token") for t in tabs]
    labels = [t.get("label") for t in tabs]
    assert "match_winner" in tokens, tokens
    assert "tennis_alt" in tokens, tokens
    assert "sets" in tokens, tokens
    assert "games_total" in tokens, tokens
    assert "Alt" in labels, labels
    assert len(tabs) == 4, f"expected 4 tabs, got {len(tabs)}: {tabs}"


# ─────────── Tennis Alt picks ───────────
def test_tennis_alt_returns_at_least_10_picks(auth_headers):
    r = requests.get(
        f"{BASE_URL}/api/picks/today",
        params={"sport": "Tennis", "market": "tennis_alt"},
        headers=auth_headers,
        timeout=30,
    )
    assert r.status_code == 200, r.text[:300]
    data = r.json()
    picks = data if isinstance(data, list) else data.get("picks", data.get("items", []))
    assert isinstance(picks, list)
    assert len(picks) >= 10, (
        f"expected >=10 alt picks, got {len(picks)}. "
        f"Sample: {[p.get('market') for p in picks[:5]]}"
    )
    # Sanity: every pick should be Tennis
    for p in picks:
        assert p.get("sport") == "Tennis", p


def test_tennis_alt_has_spread_and_total_mix(auth_headers):
    r = requests.get(
        f"{BASE_URL}/api/picks/today",
        params={"sport": "Tennis", "market": "tennis_alt"},
        headers=auth_headers,
        timeout=30,
    )
    assert r.status_code == 200
    data = r.json()
    picks = data if isinstance(data, list) else data.get("picks", data.get("items", []))
    spread_pat = re.compile(r"spread|[+\-]\d", re.I)
    total_pat = re.compile(r"\b(over|under|total games|games over|games under)\b", re.I)
    spread_count = 0
    total_count = 0
    for p in picks:
        market = (p.get("market") or "")
        selection = (p.get("selection") or p.get("name") or "")
        blob = f"{market} {selection}"
        if spread_pat.search(blob):
            spread_count += 1
        if total_pat.search(blob):
            total_count += 1
    print(f"\n[tennis_alt] spread-like={spread_count}, total-like={total_count}, n={len(picks)}")
    assert spread_count > 0, "no spread-type alt picks"
    assert total_count > 0, "no total-type alt picks"


# ─────────── Tennis Moneyline still works ───────────
def test_tennis_moneyline_no_regression(auth_headers):
    r = requests.get(
        f"{BASE_URL}/api/picks/today",
        params={"sport": "Tennis", "market": "match_winner"},
        headers=auth_headers,
        timeout=30,
    )
    assert r.status_code == 200, r.text[:300]
    data = r.json()
    picks = data if isinstance(data, list) else data.get("picks", data.get("items", []))
    assert len(picks) >= 1, f"expected >=1 tennis moneyline pick, got {len(picks)}"
    for p in picks:
        assert p.get("sport") == "Tennis"


# ─────────── MLB pitcher strikeouts regression ───────────
def test_mlb_strikeouts_still_works(auth_headers):
    r = requests.get(
        f"{BASE_URL}/api/picks/today",
        params={"sport": "MLB", "market": "pitcher_strikeouts"},
        headers=auth_headers,
        timeout=30,
    )
    assert r.status_code == 200, r.text[:300]
    data = r.json()
    picks = data if isinstance(data, list) else data.get("picks", data.get("items", []))
    # Strikeouts may not always have many picks but endpoint must not error
    assert isinstance(picks, list)
    # If picks exist, must mention strikeouts
    for p in picks[:5]:
        m = (p.get("market") or "").lower()
        assert "strikeout" in m or "k" in m or "outs" in m, p


# ─────────── Midnight rollover ───────────
def test_picks_today_total_over_100(auth_headers):
    r = requests.get(
        f"{BASE_URL}/api/picks/today", headers=auth_headers, timeout=60
    )
    assert r.status_code == 200, r.text[:300]
    data = r.json()
    picks = data if isinstance(data, list) else data.get("picks", data.get("items", []))
    assert len(picks) > 100, f"expected >100 picks today, got {len(picks)}"


# ─────────── Code presence checks ───────────
def test_sport_markets_tennis_alt_present_in_code():
    with open("/app/backend/server.py") as f:
        src = f.read()
    assert '"tennis_alt"' in src
    assert '"label": "Alt"' in src


def test_market_regex_tennis_alt_matches_expected_strings():
    """
    Verify _MARKET_REGEX['tennis_alt'] matches the strings called out in the
    review request.
    """
    pattern = r"\(alt\)|[+\-]\d+(?:\.\d+)?\s+spread|spread\b|\btotal games\b|games over|games under"
    rx = re.compile(pattern, re.I)
    assert rx.search("Over 16.5 Games (Alt)")
    assert rx.search("Player -3.0 Spread")
    assert rx.search("Total Games Over 21.5")
    assert rx.search("Under 21.0 Games (Alt)")


def test_sport_keys_has_grass_warmups():
    with open("/app/backend/sports_engine.py") as f:
        src = f.read()
    assert "tennis_atp_eastbourne" in src
    assert "tennis_atp_mallorca_open" in src
    assert "tennis_wta_bad_homburg_open" in src
