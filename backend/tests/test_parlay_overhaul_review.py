"""PerksLocks parlay-overhaul review — backend-only test suite.

Covers the 7 scenarios in the iteration 15 review request:
  1. /api/picks/parlay returns legs where every leg has win_probability >= 60
  2. High-risk Soccer-only does NOT explode into Win-or-Draw monoculture (cap 2)
  3. refresh_nonce produces a different parlay than the default
  4. parlay_history collection gets populated (>= 2 distinct signatures)
  5. Settlement loop "Parlay Learning:" log line is present in backend.err.log
  6. Self-heal validator latest fixed_lock count is < 50 (was 106 in user complaint)
  7. /api/picks/today sort=lock direction asc/desc orders correctly
"""

import os
import re
import subprocess
import asyncio

import pytest
import requests

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "https://player-intel-engine.preview.emergentagent.com").rstrip("/")
EMAIL = "demo@lockscore.ai"
PASSWORD = "demo123"
BACKEND_ERR_LOG = "/var/log/supervisor/backend.err.log"


# ──────────────────────────────────────────────────────────── fixtures ─────


@pytest.fixture(scope="session")
def auth_headers():
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"email": EMAIL, "password": PASSWORD},
                      timeout=30)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:200]}"
    tok = r.json()["access_token"]
    return {"Authorization": f"Bearer {tok}"}


def _market_family(market: str) -> str:
    """Mirror parlay_optimizer._market_family for assertions in tests."""
    m = (market or "").lower()
    if "win or draw" in m or "double chance" in m:
        return "win_or_draw"
    if "anytime goal" in m or "anytime scorer" in m:
        return "anytime_goal"
    if "to score" in m or "first goal" in m or "last goal" in m:
        return "scorer"
    if "over" in m or "under" in m or "total" in m:
        return "total"
    if "spread" in m or "handicap" in m or "line" in m:
        return "spread"
    if "moneyline" in m or "match winner" in m or "to win" in m:
        return "moneyline"
    if "btts" in m or "both teams to score" in m:
        return "btts"
    return "other"


# ──────────────────────────────────────────────────────── 1. win_p filter ──


def test_1_parlay_legs_have_strong_win_probability(auth_headers):
    """Top parlay legs should have win_probability >= 60 (now 30% of score)."""
    r = requests.get(
        f"{BASE_URL}/api/picks/parlay",
        params={"legs": 3, "mode": "standard", "sport_mode": "auto"},
        headers=auth_headers, timeout=60,
    )
    assert r.status_code == 200, f"{r.status_code} {r.text[:200]}"
    body = r.json()
    parlays = body.get("parlays") or []
    assert parlays, f"no parlays returned: {body.get('reason')}"
    top = parlays[0]
    legs = top.get("legs") or []
    assert legs, "top parlay has no legs"

    wps = [float(L.get("win_probability") or 0) for L in legs]
    print(f"\n[T1] top parlay leg win_probabilities = {wps}")
    print(f"[T1] min={min(wps):.1f}  max={max(wps):.1f}  n={len(wps)}")

    weak = [wp for wp in wps if wp < 60]
    assert not weak, f"Found legs with win_probability < 60: {weak}"


# ─────────────────────────────────────────────── 2. market family cap ──────


def test_2_high_risk_soccer_caps_win_or_draw(auth_headers):
    """High-risk Soccer-only must NOT produce a Win-or-Draw monoculture."""
    r = requests.get(
        f"{BASE_URL}/api/picks/parlay",
        params={"legs": 5, "mode": "high_risk", "sport_mode": "single", "sport": "Soccer"},
        headers=auth_headers, timeout=60,
    )
    assert r.status_code == 200, f"{r.status_code} {r.text[:200]}"
    body = r.json()
    parlays = body.get("parlays") or []
    if not parlays:
        pytest.skip(f"no soccer parlay built: {body.get('reason')}")
    top = parlays[0]
    legs = top.get("legs") or []
    assert legs, "no legs"

    fams = {}
    wod = 0
    for L in legs:
        f = _market_family(L.get("market") or "")
        fams[f] = fams.get(f, 0) + 1
        if "win or draw" in (L.get("market") or "").lower():
            wod += 1

    print(f"\n[T2] total legs = {len(legs)}")
    print(f"[T2] market-family distribution = {fams}")
    print(f"[T2] win-or-draw count = {wod}")

    assert wod <= 2, f"Win-or-Draw exceeded cap of 2: got {wod}/{len(legs)}"


# ─────────────────────────────────────────────── 3. refresh_nonce diff ─────


def _signature(legs: list[dict]) -> tuple:
    return tuple(sorted(L.get("id") or L.get("pick_id") or str(L) for L in legs))


def test_3_refresh_nonce_changes_parlay(auth_headers):
    def fetch(nonce, rank):
        r = requests.get(
            f"{BASE_URL}/api/picks/parlay",
            params={"legs": 3, "mode": "standard", "sport_mode": "auto",
                    "refresh_nonce": nonce, "rank": rank},
            headers=auth_headers, timeout=60,
        )
        assert r.status_code == 200, f"{r.status_code} {r.text[:200]}"
        b = r.json()
        ps = b.get("parlays") or []
        assert ps, f"no parlays: {b.get('reason')}"
        return ps[0]["legs"]

    legs_a = fetch(0, 1)
    legs_b = fetch(1, 2)
    sig_a = _signature(legs_a)
    sig_b = _signature(legs_b)

    print(f"\n[T3] signature A = {sig_a}")
    print(f"[T3] signature B = {sig_b}")
    print(f"[T3] differs = {sig_a != sig_b}")

    assert sig_a != sig_b, "refresh_nonce did not produce a different parlay"


# ───────────────────────────────────────── 4. parlay_history populated ────


def test_4_parlay_history_collection_populated(auth_headers):
    # Hit endpoint 3x with different nonces
    for n in range(3):
        r = requests.get(
            f"{BASE_URL}/api/picks/parlay",
            params={"legs": 3, "mode": "standard", "sport_mode": "auto",
                    "refresh_nonce": n, "rank": n + 1},
            headers=auth_headers, timeout=60,
        )
        assert r.status_code == 200, f"hit {n}: {r.status_code} {r.text[:200]}"

    # Query Mongo directly
    async def count_history():
        from motor.motor_asyncio import AsyncIOMotorClient
        client = AsyncIOMotorClient(os.environ.get("MONGO_URL", "mongodb://localhost:27017"))
        db = client[os.environ.get("DB_NAME", "lockscore_db")]
        total = await db.parlay_history.count_documents({})
        distinct = await db.parlay_history.distinct("signature")
        client.close()
        return total, distinct

    # Backend uses its own env — try common defaults
    os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
    os.environ.setdefault("DB_NAME", "lockscore_db")

    total, distinct_sigs = asyncio.get_event_loop().run_until_complete(count_history())
    print(f"\n[T4] parlay_history total docs = {total}")
    print(f"[T4] distinct signatures = {len(distinct_sigs)}")

    assert total >= 2, f"parlay_history not populated: total={total}"
    assert len(distinct_sigs) >= 2, (
        f"only {len(distinct_sigs)} distinct signature(s) recorded"
    )


# ───────────────────────────────────────── 5. parlay learning log line ────


def test_5_parlay_learning_log_line_present():
    if not os.path.exists(BACKEND_ERR_LOG):
        pytest.skip(f"{BACKEND_ERR_LOG} not present")
    # Use grep — fast on multi-MB file
    out = subprocess.run(
        ["grep", "-c", "Parlay Learning:", BACKEND_ERR_LOG],
        capture_output=True, text=True,
    )
    count = int((out.stdout or "0").strip() or 0)
    print(f"\n[T5] 'Parlay Learning:' occurrences in backend.err.log = {count}")
    # Show last occurrence for visibility
    tail = subprocess.run(
        ["grep", "Parlay Learning:", BACKEND_ERR_LOG],
        capture_output=True, text=True,
    )
    lines = [l for l in (tail.stdout or "").splitlines() if l.strip()]
    if lines:
        print(f"[T5] last line: {lines[-1][-220:]}")
    assert count >= 1, "settlement loop never logged 'Parlay Learning:'"


# ───────────────────────────────────────── 6. validator fixed_lock churn ──


def test_6_validator_fixed_lock_below_threshold():
    if not os.path.exists(BACKEND_ERR_LOG):
        pytest.skip(f"{BACKEND_ERR_LOG} not present")
    out = subprocess.run(
        ["grep", "Self-heal validator:", BACKEND_ERR_LOG],
        capture_output=True, text=True,
    )
    lines = [l for l in (out.stdout or "").splitlines() if l.strip()]
    if not lines:
        pytest.skip("no Self-heal validator lines yet")
    last = lines[-1]
    print(f"\n[T6] last validator line: {last[-260:]}")
    # Extract fixed_lock=NN
    m = re.search(r"'fixed_lock':\s*(\d+)", last)
    if not m:
        m = re.search(r"fixed_lock['\":=\s]+(\d+)", last)
    assert m, f"could not parse fixed_lock from: {last}"
    fixed = int(m.group(1))
    print(f"[T6] latest fixed_lock = {fixed}")
    assert fixed < 50, f"fixed_lock churn still high: {fixed} (expected <50)"


# ───────────────────────────────────────── 7. sort direction respected ────


def _scores(picks):
    return [float(p.get("lock_score") or 0) for p in picks]


def test_7a_lock_desc_top_ge_fifth(auth_headers):
    r = requests.get(
        f"{BASE_URL}/api/picks/today",
        params={"sport": "Soccer", "line_type": "both",
                "sort": "lock", "direction": "desc", "limit": 10},
        headers=auth_headers, timeout=60,
    )
    assert r.status_code == 200, f"{r.status_code} {r.text[:200]}"
    picks = r.json().get("picks", [])
    if len(picks) < 5:
        pytest.skip(f"only {len(picks)} picks returned")
    scores = _scores(picks[:5])
    print(f"\n[T7-DESC] lock scores [0..4] = {scores}")
    assert scores[0] >= scores[4], f"desc broken: {scores[0]} < {scores[4]}"


def test_7b_lock_asc_top_le_fifth(auth_headers):
    r = requests.get(
        f"{BASE_URL}/api/picks/today",
        params={"sport": "Soccer", "line_type": "both",
                "sort": "lock", "direction": "asc", "limit": 10},
        headers=auth_headers, timeout=60,
    )
    assert r.status_code == 200, f"{r.status_code} {r.text[:200]}"
    picks = r.json().get("picks", [])
    if len(picks) < 5:
        pytest.skip(f"only {len(picks)} picks returned")
    scores = _scores(picks[:5])
    print(f"\n[T7-ASC] lock scores [0..4] = {scores}")
    assert scores[0] <= scores[4], f"asc broken: {scores[0]} > {scores[4]}"
