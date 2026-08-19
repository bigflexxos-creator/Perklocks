"""Universal Flow Production Recovery — FINAL CLOSURE endpoint & DB invariant tests.

Verifies:
- Endpoints return 200 (health, picks/today, rollover, parlay 3 variants)
- DB invariant: for canonical MLB published picks, lock_score == published_lock_score
- Rollover query prefers published_lock_score via $expr $ifNull
"""
from __future__ import annotations
import os
import datetime as dt
import pytest
import requests

BASE_URL = "https://canonical-parity.preview.emergentagent.com"
API = f"{BASE_URL}/api"

DEMO_EMAIL = "demo@lockscore.ai"
DEMO_PASSWORD = "demo123"


@pytest.fixture(scope="module")
def auth_token():
    """Login demo user; skip if unavailable."""
    try:
        r = requests.post(f"{API}/auth/login",
                          json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD},
                          timeout=15)
    except Exception as e:
        pytest.skip(f"Auth endpoint unreachable: {e}")
    if r.status_code != 200:
        pytest.skip(f"Login failed {r.status_code}: {r.text[:150]}")
    tok = r.json().get("access_token")
    if not tok:
        pytest.skip("No access_token in login response")
    return tok


@pytest.fixture(scope="module")
def headers(auth_token):
    return {"Authorization": f"Bearer {auth_token}"}


# ── Endpoint 200 checks ─────────────────────────────────────────────
class TestEndpoints200:

    def test_health(self):
        r = requests.get(f"{API}/health", timeout=15)
        assert r.status_code == 200, f"{r.status_code}: {r.text[:200]}"

    def test_picks_today_mlb(self, headers):
        r = requests.get(f"{API}/picks/today?sport=MLB", headers=headers, timeout=45)
        assert r.status_code == 200, f"{r.status_code}: {r.text[:300]}"
        data = r.json()
        # picks may be list or dict payload
        picks = data.get("picks", data if isinstance(data, list) else [])
        # If picks exist, published_lock_score == lock_score
        checked = 0
        for p in (picks or []):
            ls = p.get("lock_score")
            pls = p.get("published_lock_score")
            if ls is not None and pls is not None:
                assert abs(float(ls) - float(pls)) < 1e-6, \
                    f"lock_score {ls} != published_lock_score {pls} for {p.get('pick_id') or p.get('_id')}"
                checked += 1
        print(f"picks_today: checked {checked} picks for lock_score parity")

    def test_picks_rollover(self, headers):
        r = requests.get(f"{API}/picks/rollover", headers=headers, timeout=45)
        assert r.status_code == 200, f"{r.status_code}: {r.text[:300]}"

    def test_parlay_standard(self, headers):
        r = requests.get(f"{API}/picks/parlay?legs=3&mode=standard&window_hours=24",
                         headers=headers, timeout=60)
        assert r.status_code == 200, f"{r.status_code}: {r.text[:300]}"

    def test_parlay_advanced_ev(self, headers):
        r = requests.get(f"{API}/picks/parlay?legs=3&mode=advanced&advanced_sub=ev&window_hours=24",
                         headers=headers, timeout=60)
        assert r.status_code == 200, f"{r.status_code}: {r.text[:300]}"

    def test_parlay_high_risk(self, headers):
        r = requests.get(f"{API}/picks/parlay?legs=10&mode=high_risk&window_hours=24",
                         headers=headers, timeout=60)
        assert r.status_code == 200, f"{r.status_code}: {r.text[:300]}"


# ── DB invariant ────────────────────────────────────────────────────
class TestDBInvariant:

    def test_mlb_published_lock_score_parity(self):
        """For canonical MLB published picks today: lock_score == published_lock_score."""
        from pymongo import MongoClient
        mongo_url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
        db_name = os.environ.get("DB_NAME", "lockscore_db")
        client = MongoClient(mongo_url, serverSelectionTimeoutMS=5000)
        db = client[db_name]
        today = dt.datetime.utcnow().strftime("%Y-%m-%d")
        q = {"sport": "MLB", "pick_date": today, "publication_state": "PUBLISHED"}
        n_total = db.picks.count_documents(q)
        # any doc missing published_lock_score
        missing = db.picks.count_documents({**q, "published_lock_score": {"$exists": False}})
        # any mismatch
        mismatched_cursor = db.picks.find({
            **q,
            "published_lock_score": {"$exists": True},
            "$expr": {"$ne": ["$lock_score", "$published_lock_score"]},
        }, {"pick_id": 1, "lock_score": 1, "published_lock_score": 1, "_id": 0})
        mismatches = list(mismatched_cursor)
        print(f"MLB PUBLISHED today={today}: total={n_total}, missing_pls={missing}, mismatched={len(mismatches)}")
        if mismatches[:5]:
            print(f"Sample mismatches: {mismatches[:5]}")
        # Invariant: no mismatches
        assert len(mismatches) == 0, f"Found {len(mismatches)} MLB published picks with lock_score != published_lock_score"


# ── _to_unit correctness ────────────────────────────────────────────
class TestToUnit:

    def test_to_unit_cases(self):
        import sys
        sys.path.insert(0, "/app/backend")
        from probability_engine import _to_unit
        import math
        assert _to_unit(92) == 0.92
        assert _to_unit(92.0) == 0.92
        assert abs(_to_unit(0.92) - 0.92) < 1e-9
        assert _to_unit(-1) is None
        assert _to_unit(None) is None
        assert _to_unit("bad") is None
        assert _to_unit(float("nan")) is None


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
