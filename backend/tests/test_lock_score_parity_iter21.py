"""
PerksLocks — Lock Score parity (iteration 21) + Bet Killer purge verification.

Verifies the backend-side canonicalisation fix:
  • Every pick across every endpoint returns `lock_score >= lock_score_v2`
    (V2 promoted into the canonical field at READ time).
  • /api/picks/today and /api/picks/{id} return identical lock_score for same id.
  • /api/picks/bet-killer is deprecated and returns an empty payload.
  • /api/picks/under-of-the-day, parlay, rollover all canonicalised.
"""
import os
import random
import requests
import pytest

from pathlib import Path
def _load_env():
    p = Path(__file__).resolve().parents[2] / "frontend" / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))
_load_env()
BASE_URL = os.environ["EXPO_PUBLIC_BACKEND_URL"].rstrip("/")
EMAIL = "demo@lockscore.ai"
PASSWORD = "demo123"


@pytest.fixture(scope="module")
def headers():
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"email": EMAIL, "password": PASSWORD}, timeout=30)
    assert r.status_code == 200, r.text
    tok = r.json()["access_token"]
    return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}


@pytest.fixture(scope="module")
def today_picks(headers):
    r = requests.get(f"{BASE_URL}/api/picks/today", headers=headers, timeout=60)
    assert r.status_code == 200, r.text
    data = r.json()
    picks = data if isinstance(data, list) else data.get("picks", [])
    assert picks, "No picks returned"
    return picks


# ─── Auth ─────────────────────────────────────────────────────────────────
class TestAuth:
    def test_login(self, headers):
        assert headers["Authorization"].startswith("Bearer ")

    def test_me(self, headers):
        r = requests.get(f"{BASE_URL}/api/auth/me", headers=headers, timeout=30)
        assert r.status_code == 200
        assert r.json().get("email") == EMAIL


# ─── P0 — Lock Score parity ───────────────────────────────────────────────
class TestLockScoreParity:
    def test_today_canonicalised(self, today_picks):
        """lock_score must be >= lock_score_v2 for every pick (within rounding)."""
        bad = []
        for p in today_picks:
            try:
                v1 = float(p.get("lock_score") or 0)
                v2 = float(p.get("lock_score_v2") or 0)
            except Exception:
                continue
            # tolerance because backend stores v1 rounded to 1 decimal
            if v2 - v1 > 0.5:
                bad.append({"id": p.get("id"), "lock_score": v1, "lock_score_v2": v2,
                            "delta": round(v2 - v1, 2)})
        if bad:
            print(f"❌ {len(bad)}/{len(today_picks)} picks where v2 > v1 (canonicalisation leak):")
            for b in bad[:10]:
                print(f"   {b}")
        assert not bad, f"{len(bad)} picks leak v2 above canonical lock_score: {bad[:5]}"

    def test_today_vs_detail_parity_5_random(self, headers, today_picks):
        """For 5 random picks: GET /picks/{id} must return same lock_score as /picks/today."""
        sample = random.sample(today_picks, min(5, len(today_picks)))
        mismatches = []
        for pick in sample:
            pid = pick["id"]
            r = requests.get(f"{BASE_URL}/api/picks/{pid}", headers=headers, timeout=60)
            assert r.status_code == 200, f"{pid} -> {r.status_code}"
            detail = r.json()
            feed_ls = round(float(pick.get("lock_score") or 0))
            detail_ls = round(float(detail.get("lock_score") or 0))
            print(f"  {pid} [{pick.get('sport')}]: today={feed_ls} detail={detail_ls}")
            if feed_ls != detail_ls:
                mismatches.append({"id": pid, "today": feed_ls, "detail": detail_ls,
                                   "today_v2": pick.get("lock_score_v2"),
                                   "detail_v2": detail.get("lock_score_v2")})
        assert not mismatches, f"Parity mismatch: {mismatches}"

    def test_parlay_legs_canonicalised(self, headers):
        r = requests.get(f"{BASE_URL}/api/picks/parlay?legs=3", headers=headers, timeout=60)
        assert r.status_code == 200
        body = r.json()
        # response shape: {"parlay": {"legs": [...]}, "parlays": [...]}
        legs = (body.get("parlay") or {}).get("legs") or body.get("legs") or []
        if not legs:
            pytest.skip("no parlay legs returned")
        bad = []
        for leg in legs:
            v1 = float(leg.get("lock_score") or 0)
            v2 = float(leg.get("lock_score_v2") or 0)
            if v2 - v1 > 0.5:
                bad.append({"id": leg.get("id"), "v1": v1, "v2": v2})
        assert not bad, f"Parlay legs not canonicalised: {bad}"

    def test_rollover_canonicalised(self, headers):
        r = requests.get(f"{BASE_URL}/api/picks/rollover", headers=headers, timeout=60)
        assert r.status_code == 200
        body = r.json()
        picks = body.get("picks") or []
        for p in picks:
            v1 = float(p.get("lock_score") or 0)
            v2 = float(p.get("lock_score_v2") or 0)
            assert v2 - v1 <= 0.5, f"Rollover leak: {p.get('id')} v1={v1} v2={v2}"

    def test_under_of_the_day_canonicalised(self, headers):
        r = requests.get(f"{BASE_URL}/api/picks/under-of-the-day", headers=headers, timeout=60)
        assert r.status_code == 200
        body = r.json()
        # may be {"pick": {...}, "alternates": [...]} or empty state
        picks = []
        if body.get("pick"):
            picks.append(body["pick"])
        picks.extend(body.get("alternates") or [])
        for p in picks:
            v1 = float(p.get("lock_score") or 0)
            v2 = float(p.get("lock_score_v2") or 0)
            assert v2 - v1 <= 0.5, f"Under leak: {p.get('id')} v1={v1} v2={v2}"


# ─── P1 — Bet Killer purge ────────────────────────────────────────────────
class TestBetKillerPurge:
    def test_bet_killer_returns_empty(self, headers):
        r = requests.get(f"{BASE_URL}/api/picks/bet-killer", headers=headers, timeout=30)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body == {"picks": []} or body.get("picks") == [], (
            f"bet-killer should be deprecated/empty, got: {body}"
        )

    def test_under_endpoint_returns_200(self, headers):
        r = requests.get(f"{BASE_URL}/api/picks/under-of-the-day", headers=headers, timeout=60)
        assert r.status_code == 200


# ─── P2 — Smoke ───────────────────────────────────────────────────────────
class TestSmoke:
    def test_picks_all(self, headers):
        r = requests.get(f"{BASE_URL}/api/picks/all", headers=headers, timeout=60)
        assert r.status_code == 200

    def test_pick_detail(self, headers, today_picks):
        pid = today_picks[0]["id"]
        r = requests.get(f"{BASE_URL}/api/picks/{pid}", headers=headers, timeout=30)
        assert r.status_code == 200
        assert r.json().get("id") == pid
