"""SOCCER_REGRESSION_RUNTIME closure — live backend verification.

Validates 4 targeted regressions against a live backend:
  §4 CROSS-BOOK DEDUPE
  §6 EVENT DATE/TIME
  §7 H2H TRUTHFUL STATUS
  §8 BREAKDOWN SELF-COMPARISON
  REGRESSION — >=20 soccer picks with proper fields.
"""
import os
import pytest
import requests

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL") or os.environ.get(
    "EXPO_BACKEND_URL", "https://canonical-parity.preview.emergentagent.com"
)
BASE_URL = BASE_URL.rstrip("/")

VALID_H2H_STATUSES = {
    "H2H_AVAILABLE",
    "H2H_INSUFFICIENT_SAMPLE",
    "H2H_IDENTITY_FAILURE",
    "H2H_SOURCE_UNAVAILABLE",
    "H2H_NOT_INGESTED",
}


@pytest.fixture(scope="module")
def token():
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": "demo@lockscore.ai", "password": "demo123"},
        timeout=30,
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


@pytest.fixture(scope="module")
def auth_headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


@pytest.fixture(scope="module")
def soccer_picks(auth_headers):
    r = requests.get(
        f"{BASE_URL}/api/picks/today?sport=Soccer",
        headers=auth_headers,
        timeout=60,
    )
    assert r.status_code == 200, r.text
    data = r.json()
    return data.get("picks", []) if isinstance(data, dict) else data


# --- Login smoke ---
def test_login_returns_token(token):
    assert isinstance(token, str) and len(token) > 20


# --- REGRESSION: minimum soccer pick count with valid fields ---
def test_soccer_picks_min_count(soccer_picks):
    assert len(soccer_picks) >= 20, (
        f"Soccer regression: got {len(soccer_picks)} picks, expected >=20"
    )


def test_soccer_picks_have_required_fields(soccer_picks):
    if not soccer_picks:
        pytest.skip("no soccer picks")
    problems = []
    for p in soccer_picks:
        pid = p.get("id") or p.get("pick_id")
        wp = p.get("win_probability")
        bo = p.get("book_odds")
        bm = p.get("bookmaker")
        osource = p.get("odds_source")
        edge = p.get("edge_percent")
        if not (isinstance(wp, (int, float)) and 0 <= wp <= 100):
            problems.append(f"{pid}: bad win_probability={wp}")
        if not isinstance(bo, (int, float)):
            problems.append(f"{pid}: bad book_odds={bo}")
        if not (isinstance(bm, str) and bm):
            problems.append(f"{pid}: bad bookmaker={bm}")
        if not osource:
            problems.append(f"{pid}: missing odds_source")
        if edge is None:
            problems.append(f"{pid}: missing edge_percent")
        else:
            mkt = (p.get("market") or "").lower()
            is_prop = any(k in mkt for k in ["goalscorer", "shots", "cards", "corner", "assist"])
            if not is_prop and isinstance(edge, (int, float)) and edge < -5:
                problems.append(f"{pid}: game-market edge_percent={edge} < -5")
    assert not problems, "; ".join(problems[:10])


# --- §4 CROSS-BOOK DEDUPE ---
def test_cross_book_dedupe_no_duplicates(soccer_picks):
    if not soccer_picks:
        pytest.skip("no soccer picks")
    groups = {}
    for p in soccer_picks:
        key = (
            p.get("event"),
            p.get("market_key"),
            p.get("selection"),
            p.get("line"),
        )
        groups.setdefault(key, []).append(p.get("id") or p.get("pick_id"))
    dupes = {k: v for k, v in groups.items() if len(v) > 1}
    assert not dupes, f"Found duplicate wagers across books: {dupes}"


def test_bookmaker_quotes_array_present_when_multiple_books(soccer_picks):
    if not soccer_picks:
        pytest.skip("no soccer picks")
    # At least one card with 2+ quotes should be present (5 books collapsed → array)
    with_quotes = [p for p in soccer_picks if isinstance(p.get("bookmaker_quotes"), list) and len(p["bookmaker_quotes"]) >= 2]
    assert with_quotes, "Expected at least one pick with bookmaker_quotes array of len >=2"


# --- §6 EVENT DATE/TIME ---
def test_all_picks_have_commence_time(soccer_picks):
    if not soccer_picks:
        pytest.skip("no soccer picks")
    missing = []
    for p in soccer_picks:
        ct = p.get("commence_time_utc") or p.get("commence_time")
        if ct in (None, "", "null", "undefined"):
            missing.append(p.get("id") or p.get("pick_id"))
    assert not missing, f"Picks missing commence_time: {missing}"


# --- §7 H2H TRUTHFUL STATUS ---
def test_h2h_endpoint_returns_valid_status(soccer_picks, auth_headers):
    if not soccer_picks:
        pytest.skip("no soccer picks")
    checked = 0
    problems = []
    for p in soccer_picks[:8]:
        pid = p.get("id") or p.get("pick_id")
        r = requests.get(
            f"{BASE_URL}/api/picks/{pid}/h2h", headers=auth_headers, timeout=30
        )
        if r.status_code == 404:
            continue
        assert r.status_code == 200, f"{pid}: {r.status_code} {r.text[:300]}"
        body = r.json()
        status = body.get("status")
        if status not in VALID_H2H_STATUSES:
            problems.append(f"{pid}: invalid status={status!r}")
        checked += 1
    assert checked > 0, "no soccer picks had /h2h endpoint reachable"
    assert not problems, "; ".join(problems)


# --- §8 BREAKDOWN SELF-COMPARISON ---
def test_market_rank_excludes_self(soccer_picks, auth_headers):
    if not soccer_picks:
        pytest.skip("no soccer picks")
    checked = 0
    problems = []
    # Broadened: request runtime slate rarely surfaces "Total Goals Over" cards,
    # so we verify canonical self-exclusion on every reachable soccer pick.
    for p in soccer_picks:
        pid = p.get("id") or p.get("pick_id")
        r = requests.get(
            f"{BASE_URL}/api/picks/{pid}/market-rank",
            headers=auth_headers,
            timeout=30,
        )
        if r.status_code == 404:
            continue
        if r.status_code != 200:
            problems.append(f"{pid}: {r.status_code} {r.text[:200]}")
            continue
        body = r.json()
        alts = body.get("alternatives", []) or []
        cur_key = (
            (p.get("short_market") or p.get("market") or "").strip().lower(),
            (p.get("selection") or "").strip().lower(),
            p.get("line"),
        )
        for a in alts:
            akey = (
                (a.get("short_market") or a.get("market") or "").strip().lower(),
                (a.get("selection") or "").strip().lower(),
                a.get("line"),
            )
            if akey == cur_key:
                problems.append(f"{pid}: alternatives contain self {akey}")
        checked += 1
        if checked >= 5:
            break
    if checked == 0:
        pytest.skip("no 'over' soccer picks with market-rank endpoint reachable")
    assert not problems, "; ".join(problems)
