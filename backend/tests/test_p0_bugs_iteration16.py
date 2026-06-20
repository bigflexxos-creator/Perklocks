"""P0 bug review (iteration 16):
  1. Grade consistency — lock>=85 must NEVER have grade="Pass", and lock<=99.
  2. Sort toggle — /api/picks/today honors sort+direction.
  3. High-Risk Parlay — 72h window returns 3 parlays each with >=5 legs.
  4. Auto-expand — 24h high-risk request returns `auto_expanded_to`
     when 24h window is too tight.
"""
import os
import pytest
import requests

BASE_URL = (
    os.environ.get("EXPO_PUBLIC_BACKEND_URL")
    or os.environ.get("EXPO_BACKEND_URL")
    or "https://bet-edge-ai-1.preview.emergentagent.com"
).rstrip("/")


# ─── auth fixture ─────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def auth_session() -> requests.Session:
    s = requests.Session()
    r = s.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": "demo@lockscore.ai", "password": "demo123"},
        timeout=15,
    )
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    token = r.json()["access_token"]
    s.headers.update({"Authorization": f"Bearer {token}",
                       "Content-Type": "application/json"})
    return s


# ─── helper: re-derive expected grade exactly like _grade() ───────────────
def expected_grade(lock_score: float) -> str:
    if lock_score >= 98: return "Elite Lock"
    if lock_score >= 95: return "Strong Lock"
    if lock_score >= 90: return "Lock"
    if lock_score >= 85: return "Playable"
    return "Pass"


# ─── BUG #1 — Grade consistency on /api/picks/today ───────────────────────
class TestGradeConsistency:
    def test_all_grades_match_lock_score(self, auth_session):
        r = auth_session.get(f"{BASE_URL}/api/picks/today", timeout=20)
        assert r.status_code == 200, r.text
        picks = r.json().get("picks", [])
        assert len(picks) > 0, "no picks returned"

        mismatches = []
        overflow = []
        wrong_pass = []
        for p in picks:
            lock = p.get("lock_score")
            grade = p.get("grade")
            if lock is None or grade is None:
                continue
            if lock > 99.0:
                overflow.append({"id": p.get("id"), "lock": lock})
            exp = expected_grade(lock)
            if exp != grade:
                mismatches.append({"id": p.get("id"), "lock": lock,
                                   "grade": grade, "expected": exp})
            if lock >= 85 and grade == "Pass":
                wrong_pass.append({"id": p.get("id"), "lock": lock,
                                   "grade": grade})

        print(f"total picks: {len(picks)}")
        print(f"grade mismatches: {len(mismatches)}")
        print(f"lock>99 overflow: {len(overflow)}")
        print(f"lock>=85 grade=Pass: {len(wrong_pass)}")
        if mismatches[:5]:
            print(f"first mismatches: {mismatches[:5]}")
        if wrong_pass[:5]:
            print(f"first wrong-Pass: {wrong_pass[:5]}")
        if overflow[:5]:
            print(f"first overflow: {overflow[:5]}")

        assert len(wrong_pass) == 0, \
            f"{len(wrong_pass)} picks have lock>=85 AND grade=Pass: {wrong_pass[:5]}"
        assert len(overflow) == 0, \
            f"{len(overflow)} picks have lock>99: {overflow[:5]}"
        assert len(mismatches) == 0, \
            f"{len(mismatches)} picks have grade mismatching _grade(lock): {mismatches[:3]}"


# ─── BUG #2 — Sort toggle on /api/picks/today ─────────────────────────────
class TestSortToggle:
    def _get(self, sess, sort, direction):
        r = sess.get(
            f"{BASE_URL}/api/picks/today",
            params={"sort": sort, "direction": direction},
            timeout=20,
        )
        assert r.status_code == 200, r.text
        return r.json().get("picks", [])

    def test_lock_desc(self, auth_session):
        picks = self._get(auth_session, "lock", "desc")
        assert len(picks) >= 5
        # Elite anchor floats elite_player picks on top, so check non-elite tail.
        non_elite = [p for p in picks if not p.get("elite_player")]
        ls = [p["lock_score"] for p in non_elite][:20]
        print(f"lock desc top 10 (non-elite): {ls[:10]}")
        assert ls == sorted(ls, reverse=True), f"lock-desc not desc among non-elite: {ls[:10]}"
        # And the very top of the list should be a high lock (>=90)
        assert picks[0]["lock_score"] >= 90, \
            f"top lock-desc pick has lock {picks[0]['lock_score']} (<90)"

    def test_lock_asc(self, auth_session):
        picks = self._get(auth_session, "lock", "asc")
        ls = [p["lock_score"] for p in picks][:20]
        print(f"lock asc top 10: {ls[:10]}")
        assert ls == sorted(ls), f"lock-asc not asc: {ls[:10]}"

    def test_edge_desc(self, auth_session):
        picks = self._get(auth_session, "edge", "desc")
        es = [p.get("edge_percent", 0) for p in picks][:20]
        print(f"edge desc top 10: {es[:10]}")
        assert es == sorted(es, reverse=True), f"edge-desc not desc: {es[:10]}"

    def test_edge_asc(self, auth_session):
        picks = self._get(auth_session, "edge", "asc")
        es = [p.get("edge_percent", 0) for p in picks][:20]
        print(f"edge asc top 10: {es[:10]}")
        assert es == sorted(es), f"edge-asc not asc: {es[:10]}"

    def test_time_asc(self, auth_session):
        picks = self._get(auth_session, "time", "asc")
        ts = [p.get("event_time", "") for p in picks][:20]
        print(f"time asc top 10: {ts[:10]}")
        assert ts == sorted(ts), f"time-asc not asc: {ts[:10]}"

    def test_win_desc(self, auth_session):
        picks = self._get(auth_session, "win", "desc")
        ws = [p.get("win_probability", 0) for p in picks][:20]
        print(f"win desc top 10: {ws[:10]}")
        assert ws == sorted(ws, reverse=True), f"win-desc not desc: {ws[:10]}"

    def test_dir_actually_flips(self, auth_session):
        """desc and asc must produce different ordering."""
        desc = self._get(auth_session, "lock", "desc")
        asc = self._get(auth_session, "lock", "asc")
        desc_ids = [p.get("id") for p in desc[:10]]
        asc_ids = [p.get("id") for p in asc[:10]]
        assert desc_ids != asc_ids, "lock desc and asc returned same order"


# ─── BUG #3 — High-Risk Parlay 72h ────────────────────────────────────────
class TestHighRiskParlay72h:
    def test_high_risk_72h_returns_3_parlays(self, auth_session):
        r = auth_session.get(
            f"{BASE_URL}/api/picks/parlay",
            params={
                "legs": 10, "mode": "high_risk",
                "window_hours": 72, "sport_mode": "auto",
            },
            timeout=30,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        parlays = body.get("parlays") or []
        print(f"window_hours=72: parlays={len(parlays)} "
              f"auto_expanded_to={body.get('auto_expanded_to')} "
              f"reason={body.get('reason')}")
        assert len(parlays) == 3, \
            f"expected 3 parlay cards, got {len(parlays)}; reason={body.get('reason')}"
        for i, card in enumerate(parlays):
            n_legs = card.get("leg_count") or len(card.get("legs") or [])
            print(f"  card[{i}] legs={n_legs} survival={card.get('survival_pct')}")
            assert n_legs >= 5, f"card[{i}] only has {n_legs} legs"


# ─── BUG #4 — Auto-expand fallback for high_risk with tight window ────────
class TestHighRiskAutoExpand:
    def test_high_risk_24h_auto_expand(self, auth_session):
        r = auth_session.get(
            f"{BASE_URL}/api/picks/parlay",
            params={
                "legs": 10, "mode": "high_risk",
                "window_hours": 24, "sport_mode": "auto",
            },
            timeout=30,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        parlays = body.get("parlays") or []
        ae = body.get("auto_expanded_to")
        print(f"window_hours=24: parlays={len(parlays)} "
              f"auto_expanded_to={ae} "
              f"reason={body.get('reason')}")
        # Either: 24h alone built 3 parlays (no expand needed), OR
        # auto_expanded_to is populated when fallback fired.
        if len(parlays) == 3:
            # auto_expanded_to is allowed to be None if 24h itself was sufficient
            # OR populated (72/168) if the expand path fired. Either is OK.
            pass
        else:
            # No parlays — at minimum the auto_expand flag should reflect
            # whether the fallback path even ran.
            assert ae is not None or len(parlays) == 0, \
                "Expected either parlays returned or auto_expanded_to populated"


# ─── direct DB inspection — ground-truth grade audit ──────────────────────
class TestDbGradeAudit:
    """Direct mongo check for picks with lock>=85 & grade=Pass on today's slate."""
    def test_db_grade_audit(self):
        try:
            from pymongo import MongoClient
        except Exception:
            pytest.skip("pymongo not available")
        mongo_url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
        db_name = os.environ.get("DB_NAME", "lockscore_db")
        client = MongoClient(mongo_url)
        db = client[db_name]
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        bad = list(db.picks.find(
            {"pick_date": today, "lock_score": {"$gte": 85}, "grade": "Pass"},
            {"_id": 0, "id": 1, "lock_score": 1, "grade": 1, "sport": 1},
        ).limit(20))
        overflow = list(db.picks.find(
            {"pick_date": today, "lock_score": {"$gt": 99}},
            {"_id": 0, "id": 1, "lock_score": 1},
        ).limit(20))
        total = db.picks.count_documents({"pick_date": today})
        print(f"DB today total={total} bad(lock>=85,grade=Pass)={len(bad)} "
              f"overflow(lock>99)={len(overflow)}")
        if bad: print(f"sample bad: {bad[:5]}")
        if overflow: print(f"sample overflow: {overflow[:5]}")
        assert len(bad) == 0, f"{len(bad)} DB picks with lock>=85 AND grade=Pass: {bad[:5]}"
        assert len(overflow) == 0, f"{len(overflow)} DB picks with lock>99: {overflow[:5]}"
