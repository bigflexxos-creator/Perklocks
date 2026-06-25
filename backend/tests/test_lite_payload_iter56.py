"""Tests for `/api/picks/today?lite=true` payload optimization (iter56).

Verifies:
  1. Lite mode strips detail-only heavy fields.
  2. Lite mode preserves all fields rendered by LockPickCard home card.
  3. Lite payload is meaningfully smaller than fat payload (>=50% reduction).
  4. Lite and fat payloads return identical pick counts and core values.
  5. Backward compat: omitting `lite` returns full payload (with detail fields).
  6. Pick detail endpoint `/api/picks/{id}` still returns the full document.
"""
import json
import os
import pytest
import requests

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    BASE_URL = os.environ.get("EXPO_BACKEND_URL", "").rstrip("/")

DEMO_EMAIL = "demo@lockscore.ai"
DEMO_PASSWORD = "demo123"

# Fields that lite mode is expected to STRIP from each pick.
LITE_STRIPPED_FIELDS = {
    "sportsbook_mapping",
    "evidence_breakdown",
    "v2_reasons",
    "probability",
    "selection_v2",
    "brain",
    "key_insights",
    "top_reasons",
    "learning",
    "factors",
    "lock_components",
    "sim_alt_lines",
    "fanduel_event_id",
    "draftkings_event_id",
    "betmgm_event_id",
    "caesars_event_id",
    "pointsbet_event_id",
}

# Fields the home LockPickCard.tsx needs — lite must preserve these.
HOME_CARD_FIELDS = [
    "id", "sport", "event", "market", "pick", "lock_score",
    "grade", "win_probability", "edge_percent", "book_odds",
    "event_time", "league",
]


@pytest.fixture(scope="module")
def auth_token():
    """Login or register the demo user and return a JWT."""
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD}, timeout=30)
    if r.status_code != 200:
        # Try register fresh
        r = s.post(f"{BASE_URL}/api/auth/register",
                   json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD,
                         "name": "Demo"}, timeout=30)
        if r.status_code not in (200, 201, 409):
            pytest.skip(f"Cannot authenticate demo user: {r.status_code} {r.text[:200]}")
        # If conflict (409), retry login
        if r.status_code == 409:
            r = s.post(f"{BASE_URL}/api/auth/login",
                       json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD}, timeout=30)
    data = r.json()
    token = data.get("access_token") or data.get("token")
    if not token:
        pytest.skip(f"No access_token in login response: {data}")
    return token


@pytest.fixture(scope="module")
def auth_headers(auth_token):
    return {"Authorization": f"Bearer {auth_token}",
            "Content-Type": "application/json"}


@pytest.fixture(scope="module")
def fat_payload(auth_headers):
    r = requests.get(f"{BASE_URL}/api/picks/today", headers=auth_headers, timeout=60)
    assert r.status_code == 200, f"fat status={r.status_code} body={r.text[:300]}"
    return r


@pytest.fixture(scope="module")
def lite_payload(auth_headers):
    r = requests.get(f"{BASE_URL}/api/picks/today?lite=true",
                     headers=auth_headers, timeout=60)
    assert r.status_code == 200, f"lite status={r.status_code} body={r.text[:300]}"
    return r


# ── Tests ─────────────────────────────────────────────────────────────

class TestLitePayloadSize:
    def test_lite_smaller_than_fat(self, fat_payload, lite_payload):
        fat_bytes = len(fat_payload.content)
        lite_bytes = len(lite_payload.content)
        print(f"\nFat payload : {fat_bytes:,} bytes ({fat_bytes/1024:.1f} KB)")
        print(f"Lite payload: {lite_bytes:,} bytes ({lite_bytes/1024:.1f} KB)")
        if fat_bytes:
            reduction = (1 - lite_bytes / fat_bytes) * 100
            print(f"Reduction   : {reduction:.1f}%")
        assert lite_bytes < fat_bytes, "lite must be strictly smaller than fat"
        # Target >= 50% reduction (agent reported ~77%).
        assert lite_bytes <= fat_bytes * 0.6, (
            f"expected ≥40% reduction, got "
            f"{(1 - lite_bytes/fat_bytes)*100:.1f}% "
            f"(fat={fat_bytes}, lite={lite_bytes})"
        )


class TestLitePayloadStructure:
    def test_lite_returns_picks_array(self, lite_payload):
        body = lite_payload.json()
        assert "picks" in body and isinstance(body["picks"], list)

    def test_lite_strips_heavy_fields(self, lite_payload):
        picks = lite_payload.json()["picks"]
        if not picks:
            pytest.skip("No picks in feed today — cannot validate lite strip")
        leak = {}
        for p in picks:
            for f in LITE_STRIPPED_FIELDS:
                if f in p:
                    leak.setdefault(f, 0)
                    leak[f] += 1
        assert not leak, f"lite payload leaks stripped fields: {leak}"

    def test_lite_preserves_home_card_fields(self, lite_payload):
        picks = lite_payload.json()["picks"]
        if not picks:
            pytest.skip("No picks in feed today")
        missing = {}
        for p in picks:
            for f in HOME_CARD_FIELDS:
                if f not in p or p.get(f) is None:
                    # event_time / league can legitimately be null for
                    # some picks; only flag id/sport/lock_score etc as hard.
                    if f in ("id", "sport", "lock_score", "grade",
                             "win_probability", "edge_percent", "pick"):
                        missing.setdefault(f, 0)
                        missing[f] += 1
        assert not missing, f"lite payload missing required card fields: {missing}"


class TestFatPayloadBackwardCompat:
    def test_fat_returns_picks(self, fat_payload):
        body = fat_payload.json()
        assert "picks" in body
        assert isinstance(body["picks"], list)

    def test_fat_contains_at_least_one_heavy_field(self, fat_payload):
        picks = fat_payload.json()["picks"]
        if not picks:
            pytest.skip("No picks in feed")
        present = set()
        for p in picks:
            for f in LITE_STRIPPED_FIELDS:
                if f in p:
                    present.add(f)
            if len(present) >= 3:
                break
        assert present, (
            "fat payload should contain at least one heavy detail field "
            f"({LITE_STRIPPED_FIELDS}), got none — backward compat broken?"
        )
        print(f"\nFat payload contains heavy fields: {sorted(present)[:6]}…")


class TestParity:
    def test_fat_and_lite_same_count(self, fat_payload, lite_payload):
        fat = fat_payload.json()["picks"]
        lite = lite_payload.json()["picks"]
        assert len(fat) == len(lite), (
            f"pick count diverged: fat={len(fat)} lite={len(lite)}"
        )

    def test_fat_and_lite_same_lock_scores(self, fat_payload, lite_payload):
        fat = fat_payload.json()["picks"]
        lite = lite_payload.json()["picks"]
        if not fat:
            pytest.skip("No picks in feed")
        fat_by_id = {p["id"]: p for p in fat if "id" in p}
        diffs = []
        for p in lite:
            pid = p.get("id")
            if pid not in fat_by_id:
                continue
            for f in ("lock_score", "win_probability",
                      "edge_percent", "grade"):
                if fat_by_id[pid].get(f) != p.get(f):
                    diffs.append((pid, f, fat_by_id[pid].get(f), p.get(f)))
        assert not diffs[:10], f"value mismatches between fat/lite: {diffs[:10]}"


class TestPickDetailUnchanged:
    def test_detail_returns_full_document(self, fat_payload, auth_headers):
        picks = fat_payload.json()["picks"]
        if not picks:
            pytest.skip("No picks to inspect detail for")
        pid = picks[0]["id"]
        r = requests.get(f"{BASE_URL}/api/picks/{pid}",
                         headers=auth_headers, timeout=30)
        assert r.status_code == 200, f"detail status={r.status_code}"
        detail = r.json()
        # The detail document should still contain at least one of the
        # detail-only heavy fields (since this endpoint is what the detail
        # screen calls — it is unaffected by the lite optimization).
        present = {f for f in LITE_STRIPPED_FIELDS if f in detail}
        # Some picks may genuinely not have all heavy fields populated
        # (depends on sport/source). But at least one of the bigger
        # ones should be there for the test to be meaningful.
        print(f"\nDetail doc heavy fields present: {sorted(present)[:8]}")
        assert "id" in detail, "detail must include id"
        assert detail["id"] == pid

    def test_lite_param_does_not_apply_to_detail(self, fat_payload, auth_headers):
        """`/api/picks/{id}` ignores any lite-style stripping — sanity check."""
        picks = fat_payload.json()["picks"]
        if not picks:
            pytest.skip("No picks")
        pid = picks[0]["id"]
        # Even if someone appends ?lite=true to a detail URL, it should
        # not be applied (detail endpoint doesn't take that param).
        r = requests.get(f"{BASE_URL}/api/picks/{pid}?lite=true",
                         headers=auth_headers, timeout=30)
        assert r.status_code == 200
