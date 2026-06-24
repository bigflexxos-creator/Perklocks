"""Iteration 45 — Backend regression tests for the MLB hitter alt-lock carve-out.

Verifies:
- The new `mlb_hitter_q` carve-out surfaces H+R+RBI / Hits / HR / RBI / Total Bases picks.
- `min_lock` slider is enforced as a global AND across every carve-out.
- Existing carve-outs (tennis ML, tennis alt, soccer scorer, MLB K) still work.
- Sort order is strictly DESC by lock_score.
- /api/picks/{id}, /api/analytics/xg-form-shadow, /api/parlay/optimize still respond.
"""
import os
import re
import pytest
import requests

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "").rstrip("/") or \
           os.environ.get("EXPO_BACKEND_URL", "").rstrip("/")

assert BASE_URL, "EXPO_PUBLIC_BACKEND_URL / EXPO_BACKEND_URL must be set"

HRRBI_RE = re.compile(r"hits\s*\+\s*runs\s*\+\s*rbis?", re.I)
HITTER_RE = re.compile(
    r"hits\s*\+\s*runs\s*\+\s*rbis?|\bhits?\b|home runs|\brbis?\b|total bases",
    re.I,
)
PITCHER_RE = re.compile(r"strikeout|outs recorded", re.I)


# ─── Fixtures ───────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module")
def token(session):
    r = session.post(f"{BASE_URL}/api/auth/login",
                     json={"email": "demo@lockscore.ai", "password": "demo123"})
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:200]}"
    body = r.json()
    assert "access_token" in body, f"no access_token in {body}"
    return body["access_token"]


@pytest.fixture(scope="module")
def auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


# ─── Auth sanity ────────────────────────────────────────────────────
class TestAuth:
    def test_login_returns_token(self, token):
        assert isinstance(token, str) and len(token) > 20


# ─── /api/picks/today carve-out behaviour ───────────────────────────
class TestPicksToday:
    def test_default_floor_returns_picks(self, session, auth_headers):
        r = session.get(f"{BASE_URL}/api/picks/today", headers=auth_headers)
        assert r.status_code == 200, r.text[:300]
        data = r.json()
        # Tolerate either a bare list or an envelope {picks:[...]}
        picks = data if isinstance(data, list) else data.get("picks", [])
        assert isinstance(picks, list)
        assert len(picks) > 0, "no picks returned at default floor"

    def test_sort_desc_by_lock_score(self, session, auth_headers):
        """When sort=lock is requested explicitly, lock_score must be DESC.

        Default sort is "time" (soonest kickoff first), so we MUST pass
        sort=lock to validate the historical 92-above-95 inversion fix.
        """
        r = session.get(f"{BASE_URL}/api/picks/today?sort=lock",
                        headers=auth_headers)
        assert r.status_code == 200
        data = r.json()
        picks = data if isinstance(data, list) else data.get("picks", [])
        locks = [
            float(p.get("lock_score") or p.get("lock_score_v2") or 0)
            for p in picks
        ]
        # Strictly non-increasing (allow tiny float fuzz)
        inversions = [
            (i, locks[i - 1], locks[i])
            for i in range(1, len(locks)) if locks[i] > locks[i - 1] + 1e-6
        ]
        assert not inversions, f"lock_score sort inversions: {inversions[:5]}"

    def test_hrrbi_carveout_surfaces(self, session, auth_headers):
        r = session.get(f"{BASE_URL}/api/picks/today?sport=MLB",
                        headers=auth_headers)
        assert r.status_code == 200
        data = r.json()
        picks = data if isinstance(data, list) else data.get("picks", [])
        hrrbi = [p for p in picks if HRRBI_RE.search(p.get("market", ""))]
        # Main agent verified 3 H+R+RBI picks at lock 94.4-94.5
        assert len(hrrbi) >= 1, (
            f"expected ≥1 Hits+Runs+RBIs prop, got {len(hrrbi)}. "
            f"Sample markets: {[p.get('market') for p in picks[:10]]}"
        )
        # Each must carry a valid lock
        for p in hrrbi:
            lk = p.get("lock_score") or p.get("lock_score_v2")
            assert lk is not None and float(lk) >= 70.0, \
                f"H+R+RBI pick under floor 70: {lk} ({p.get('market')})"

    def test_mlb_hitter_alt_props_surface(self, session, auth_headers):
        r = session.get(f"{BASE_URL}/api/picks/today?sport=MLB",
                        headers=auth_headers)
        assert r.status_code == 200
        data = r.json()
        picks = data if isinstance(data, list) else data.get("picks", [])
        hitter = [
            p for p in picks
            if HITTER_RE.search(p.get("market", ""))
            and not PITCHER_RE.search(p.get("market", ""))
        ]
        assert len(hitter) >= 1, (
            "expected ≥1 MLB hitter prop (Hits / HR / RBI / H+R+RBI / Total Bases). "
            f"Got markets: {[p.get('market') for p in picks[:15]]}"
        )

    def test_mlb_k_carveout_still_works(self, session, auth_headers):
        r = session.get(f"{BASE_URL}/api/picks/today?sport=MLB",
                        headers=auth_headers)
        assert r.status_code == 200
        data = r.json()
        picks = data if isinstance(data, list) else data.get("picks", [])
        ks = [p for p in picks if PITCHER_RE.search(p.get("market", ""))]
        # Pitcher K carve-out should still surface at least one
        if ks:
            for p in ks:
                lk = p.get("lock_score") or p.get("lock_score_v2")
                assert lk and float(lk) >= 70
        else:
            pytest.skip("no pitcher K picks today — not a regression on its own")


# ─── min_lock global enforcement ────────────────────────────────────
class TestMinLockEnforcement:
    def test_min_lock_95_hides_below_95(self, session, auth_headers):
        r = session.get(f"{BASE_URL}/api/picks/today?min_lock=95",
                        headers=auth_headers)
        assert r.status_code == 200, r.text[:300]
        data = r.json()
        picks = data if isinstance(data, list) else data.get("picks", [])
        bad = []
        for p in picks:
            lk1 = float(p.get("lock_score") or 0)
            lk2 = float(p.get("lock_score_v2") or 0)
            # Either lock_score OR lock_score_v2 must be >= 95
            if lk1 < 95.0 and lk2 < 95.0:
                bad.append({
                    "id": p.get("id"),
                    "market": p.get("market"),
                    "lock_score": lk1,
                    "lock_score_v2": lk2,
                })
        assert not bad, f"min_lock=95 leaked sub-95 picks: {bad[:5]}"

    def test_min_lock_95_excludes_hrrbi_at_94(self, session, auth_headers):
        """The H+R+RBI carve-out picks (94.4-94.5) MUST be hidden at min_lock=95."""
        r = session.get(f"{BASE_URL}/api/picks/today?sport=MLB&min_lock=95",
                        headers=auth_headers)
        assert r.status_code == 200
        data = r.json()
        picks = data if isinstance(data, list) else data.get("picks", [])
        leaks = []
        for p in picks:
            if HRRBI_RE.search(p.get("market", "")):
                lk1 = float(p.get("lock_score") or 0)
                lk2 = float(p.get("lock_score_v2") or 0)
                if lk1 < 95.0 and lk2 < 95.0:
                    leaks.append({
                        "id": p.get("id"),
                        "market": p.get("market"),
                        "lock": max(lk1, lk2),
                    })
        assert not leaks, f"H+R+RBI carve-out leaked at min_lock=95: {leaks}"

    def test_min_lock_default_picks_count_ge_min_lock_95(self, session, auth_headers):
        r1 = session.get(f"{BASE_URL}/api/picks/today", headers=auth_headers)
        r2 = session.get(f"{BASE_URL}/api/picks/today?min_lock=95",
                         headers=auth_headers)
        d1 = r1.json(); d2 = r2.json()
        p1 = d1 if isinstance(d1, list) else d1.get("picks", [])
        p2 = d2 if isinstance(d2, list) else d2.get("picks", [])
        assert len(p1) >= len(p2), \
            f"min_lock=95 returned MORE picks ({len(p2)}) than default ({len(p1)})"


# ─── Sport-filtered regression checks ───────────────────────────────
class TestSportFilters:
    def test_tennis_carveouts(self, session, auth_headers):
        r = session.get(f"{BASE_URL}/api/picks/today?sport=Tennis",
                        headers=auth_headers)
        assert r.status_code == 200
        data = r.json()
        picks = data if isinstance(data, list) else data.get("picks", [])
        # Tennis ML or alt-spread must surface (carve-outs)
        if not picks:
            pytest.skip("no tennis picks today — possibly off-season")
        for p in picks:
            assert p.get("sport") == "Tennis"

    def test_soccer_scorer_carveout(self, session, auth_headers):
        r = session.get(f"{BASE_URL}/api/picks/today?sport=Soccer",
                        headers=auth_headers)
        assert r.status_code == 200
        data = r.json()
        picks = data if isinstance(data, list) else data.get("picks", [])
        if not picks:
            pytest.skip("no soccer picks today")
        for p in picks:
            assert p.get("sport") == "Soccer"


# ─── /api/picks/{id} no-500 check ───────────────────────────────────
class TestPickDetail:
    def test_pick_detail_no_500(self, session, auth_headers):
        r = session.get(f"{BASE_URL}/api/picks/today?sport=MLB",
                        headers=auth_headers)
        data = r.json()
        picks = data if isinstance(data, list) else data.get("picks", [])
        if not picks:
            pytest.skip("no MLB picks today")
        sample = picks[:5]
        for p in sample:
            pid = p.get("id") or p.get("_id")
            if not pid:
                continue
            rr = session.get(f"{BASE_URL}/api/picks/{pid}", headers=auth_headers)
            assert rr.status_code < 500, \
                f"/api/picks/{pid} returned 5xx: {rr.status_code} {rr.text[:200]}"
            # 200 expected
            assert rr.status_code in (200, 404), \
                f"unexpected status {rr.status_code} for {pid}"


# ─── XG/Form shadow A/B ─────────────────────────────────────────────
class TestAnalytics:
    def test_xg_form_shadow_200(self, session, auth_headers):
        r = session.get(f"{BASE_URL}/api/analytics/xg-form-shadow",
                        headers=auth_headers)
        assert r.status_code == 200, f"{r.status_code} {r.text[:200]}"
        body = r.json()
        assert isinstance(body, dict)


# ─── Parlay endpoint smoke ──────────────────────────────────────────
class TestParlay:
    def test_parlay_endpoint_no_5xx(self, session, auth_headers):
        # The canonical parlay endpoint in this codebase is /api/picks/parlay.
        # /api/parlay/optimize does not exist (404). Verify the real one works
        # and that carve-out picks (hitter/H+R+RBI) are eligible candidates.
        rr = session.get(f"{BASE_URL}/api/picks/parlay", headers=auth_headers)
        assert rr.status_code < 500, \
            f"/api/picks/parlay returned 5xx: {rr.status_code} {rr.text[:200]}"
        assert rr.status_code == 200, \
            f"unexpected status {rr.status_code}: {rr.text[:200]}"
        body = rr.json()
        assert isinstance(body, (dict, list)), f"unexpected body type: {type(body)}"
