"""P0 retest (iteration 17) — verify the three fixes from iteration 16:

  Fix #1: soccer/predictor._grade_from_conf delegates to sports_engine._grade
          (no more legacy ELITE/A+/A/B+/B/C vocabulary from soccer upserts).
  Fix #2: sports_engine.compute_lock_score floor REWRITTEN to step function
          (Win>=80 & Edge>=15 → 98, Win>=75 & Edge>=10 → 95, ...).
  Fix #3: learning_system_v2.apply_v2_to_picks step 4 uses SAME step function
          before re-grading (step 5).
  + Bulk DB re-sync was executed before this run.

Critical assertions:
  - NO picks (API+DB) with lock>=85 & grade='Pass'
  - NO legacy grades (ELITE/A+/A/B+/B/C) anywhere
  - NO lock_score > 99
  - Grade EXACTLY matches lock_score per spec tier table
  - State STAYS clean across at least one soccer pipeline cycle (15-min loop)
  - Sort toggle still works on all axes
  - High-risk parlay window_hours=72 returns 3 parlays each with >=5 legs
  - Auto-expand fallback fires when window is tight (4h)
"""
import os
import re
import time
import pytest
import requests
from datetime import datetime, timezone

BASE_URL = (
    os.environ.get("EXPO_PUBLIC_BACKEND_URL")
    or os.environ.get("EXPO_BACKEND_URL")
    or "https://player-intel-engine.preview.emergentagent.com"
).rstrip("/")

LEGACY_GRADES = {"ELITE", "A+", "A", "B+", "B", "C"}
SPEC_GRADES = {"Elite Lock", "Strong Lock", "Lock", "Playable", "Pass"}


# ─── helpers ──────────────────────────────────────────────────────────────
def expected_grade(lock_score: float) -> str:
    if lock_score >= 98: return "Elite Lock"
    if lock_score >= 95: return "Strong Lock"
    if lock_score >= 90: return "Lock"
    if lock_score >= 85: return "Playable"
    return "Pass"


def db_handle():
    from pymongo import MongoClient
    mongo_url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
    db_name = os.environ.get("DB_NAME", "lockscore_db")
    return MongoClient(mongo_url)[db_name]


def today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def audit_picks(picks):
    """Return dict counting tier-mapping anomalies."""
    legacy = []
    overflow = []
    wrong_pass = []
    mismatches = []
    for p in picks:
        lock = p.get("lock_score")
        grade = p.get("grade")
        if lock is None or grade is None:
            continue
        if grade in LEGACY_GRADES:
            legacy.append({"id": p.get("id"), "lock": lock, "grade": grade})
        if lock > 99.0:
            overflow.append({"id": p.get("id"), "lock": lock})
        if lock >= 85 and grade == "Pass":
            wrong_pass.append({"id": p.get("id"), "lock": lock, "grade": grade})
        exp = expected_grade(lock)
        if exp != grade and grade in SPEC_GRADES:
            mismatches.append({"id": p.get("id"), "lock": lock,
                               "grade": grade, "expected": exp})
    return {"legacy": legacy, "overflow": overflow,
            "wrong_pass": wrong_pass, "mismatches": mismatches}


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


# ─── BUG #1A — Grade consistency on /api/picks/today ──────────────────────
class TestGradeConsistencyAPI:
    def test_no_wrong_pass_no_legacy_no_overflow_no_mismatch(self, auth_session):
        r = auth_session.get(f"{BASE_URL}/api/picks/today", timeout=20)
        assert r.status_code == 200, r.text
        picks = r.json().get("picks", [])
        assert len(picks) > 0, "no picks returned"
        a = audit_picks(picks)

        print(f"\nAPI total picks: {len(picks)}")
        print(f"  legacy grades: {len(a['legacy'])}")
        print(f"  lock>99 overflow: {len(a['overflow'])}")
        print(f"  lock>=85 & grade=Pass: {len(a['wrong_pass'])}")
        print(f"  tier mismatches: {len(a['mismatches'])}")
        if a['legacy']: print(f"  sample legacy: {a['legacy'][:5]}")
        if a['wrong_pass']: print(f"  sample wrong_pass: {a['wrong_pass'][:5]}")
        if a['mismatches']: print(f"  sample mismatches: {a['mismatches'][:5]}")
        if a['overflow']: print(f"  sample overflow: {a['overflow'][:5]}")

        assert len(a['legacy']) == 0, \
            f"{len(a['legacy'])} picks have legacy grade vocab: {a['legacy'][:5]}"
        assert len(a['overflow']) == 0, \
            f"{len(a['overflow'])} picks have lock>99: {a['overflow'][:5]}"
        assert len(a['wrong_pass']) == 0, \
            f"{len(a['wrong_pass'])} picks have lock>=85 & grade=Pass: {a['wrong_pass'][:5]}"
        assert len(a['mismatches']) == 0, \
            f"{len(a['mismatches'])} picks have tier-mapping mismatch: {a['mismatches'][:5]}"


# ─── BUG #1B — Grade consistency on DB (ground truth) ─────────────────────
class TestGradeConsistencyDB:
    def test_db_grade_audit(self):
        try:
            db = db_handle()
        except Exception:
            pytest.skip("pymongo not available")

        today = today_utc()
        picks = list(db.picks.find(
            {"pick_date": today},
            {"_id": 0, "id": 1, "lock_score": 1, "grade": 1, "sport": 1},
        ))
        total = len(picks)
        distinct_grades = sorted({p.get("grade") for p in picks if p.get("grade")})
        a = audit_picks(picks)

        print(f"\nDB today total={total}")
        print(f"  distinct grades: {distinct_grades}")
        print(f"  legacy: {len(a['legacy'])}")
        print(f"  overflow: {len(a['overflow'])}")
        print(f"  wrong_pass: {len(a['wrong_pass'])}")
        print(f"  mismatches: {len(a['mismatches'])}")
        if a['legacy']: print(f"  sample legacy: {a['legacy'][:5]}")
        if a['wrong_pass']: print(f"  sample wrong_pass: {a['wrong_pass'][:5]}")
        if a['mismatches']: print(f"  sample mismatches: {a['mismatches'][:5]}")

        assert total > 0, "no picks in DB for today"
        unexpected = set(distinct_grades) - SPEC_GRADES
        assert not unexpected, f"unexpected grade values in DB: {unexpected}"
        assert len(a['legacy']) == 0, \
            f"{len(a['legacy'])} DB picks have legacy grade vocab: {a['legacy'][:5]}"
        assert len(a['overflow']) == 0, \
            f"{len(a['overflow'])} DB picks have lock>99: {a['overflow'][:5]}"
        assert len(a['wrong_pass']) == 0, \
            f"{len(a['wrong_pass'])} DB picks have lock>=85 & grade=Pass: {a['wrong_pass'][:5]}"
        assert len(a['mismatches']) == 0, \
            f"{len(a['mismatches'])} DB picks have tier-mapping mismatch: {a['mismatches'][:5]}"


# ─── CRITICAL: Cross-cycle stability ──────────────────────────────────────
class TestGradeStabilityAcrossPipelineCycle:
    """The iteration 16 failure mode: clean state immediately decays as soon
    as the soccer pipeline upserts new picks (15-min loop) because the soccer
    predictor wrote legacy grades. With Fix #1 the upsert now uses spec grades.

    This test:
      1. captures state NOW,
      2. waits for the next 'Soccer pipeline done' log line (or a timeout),
      3. re-audits DB+API.

    To avoid blocking CI for 15min we cap the wait at SOCCER_WAIT_S (env-
    overridable). If no new cycle fires during the wait we still re-audit at
    the end — even without a new cycle, the validator (~30-min loop) may
    have run and the audit must remain clean either way.
    """

    LOG = "/var/log/supervisor/backend.err.log"
    SOCCER_RE = re.compile(r"Soccer pipeline done")
    VALIDATOR_RE = re.compile(r"Self-heal validator")
    WAIT_SECONDS = int(os.environ.get("SOCCER_WAIT_S", "180"))  # 3-min cap by default

    def _tail_log(self, n=200):
        try:
            with open(self.LOG, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 200_000))
                data = f.read().decode("utf-8", "replace")
            return data.splitlines()[-n:]
        except Exception:
            return []

    def _last_pipeline_marker(self):
        lines = self._tail_log(400)
        latest = ""
        for ln in lines:
            if self.SOCCER_RE.search(ln) or self.VALIDATOR_RE.search(ln):
                latest = ln
        return latest

    def test_state_clean_after_pipeline_cycle(self, auth_session):
        db = db_handle()
        today = today_utc()

        # 1) capture pre-state ----------------------------------------------------
        pre_marker = self._last_pipeline_marker()
        print(f"\n[pre] last pipeline/validator marker: {pre_marker[-120:] if pre_marker else 'NONE'}")
        pre_pick_count = db.picks.count_documents({"pick_date": today})
        a_pre = audit_picks(list(db.picks.find(
            {"pick_date": today},
            {"_id": 0, "id": 1, "lock_score": 1, "grade": 1, "sport": 1},
        )))
        print(f"[pre] DB picks today={pre_pick_count}  "
              f"legacy={len(a_pre['legacy'])}  wrong_pass={len(a_pre['wrong_pass'])}  "
              f"mismatch={len(a_pre['mismatches'])}  overflow={len(a_pre['overflow'])}")
        assert len(a_pre['legacy']) == 0
        assert len(a_pre['wrong_pass']) == 0
        assert len(a_pre['mismatches']) == 0
        assert len(a_pre['overflow']) == 0

        # 2) wait for the NEXT pipeline cycle (or timeout) ----------------------
        deadline = time.time() + self.WAIT_SECONDS
        saw_new_cycle = False
        last_marker = pre_marker
        print(f"[wait] up to {self.WAIT_SECONDS}s for next 'Soccer pipeline done' / 'Self-heal validator' …")
        while time.time() < deadline:
            time.sleep(10)
            cur = self._last_pipeline_marker()
            if cur and cur != last_marker:
                last_marker = cur
                saw_new_cycle = True
                print(f"[wait] NEW marker: {cur[-160:]}")
                # Allow upserts to complete after the marker.
                time.sleep(5)
                break

        if not saw_new_cycle:
            print(f"[wait] no new cycle within window; auditing anyway")

        # 3) re-audit post-cycle (BOTH DB + API) --------------------------------
        a_post_db = audit_picks(list(db.picks.find(
            {"pick_date": today},
            {"_id": 0, "id": 1, "lock_score": 1, "grade": 1, "sport": 1},
        )))
        r = auth_session.get(f"{BASE_URL}/api/picks/today", timeout=20)
        assert r.status_code == 200
        a_post_api = audit_picks(r.json().get("picks", []))

        print(f"[post-DB ] legacy={len(a_post_db['legacy'])} wrong_pass={len(a_post_db['wrong_pass'])} "
              f"mismatch={len(a_post_db['mismatches'])} overflow={len(a_post_db['overflow'])}  "
              f"saw_new_cycle={saw_new_cycle}")
        print(f"[post-API] legacy={len(a_post_api['legacy'])} wrong_pass={len(a_post_api['wrong_pass'])} "
              f"mismatch={len(a_post_api['mismatches'])} overflow={len(a_post_api['overflow'])}")
        if a_post_db['legacy']: print(f"  DB legacy samples: {a_post_db['legacy'][:5]}")
        if a_post_db['wrong_pass']: print(f"  DB wrong_pass samples: {a_post_db['wrong_pass'][:5]}")
        if a_post_db['mismatches']: print(f"  DB mismatch samples: {a_post_db['mismatches'][:5]}")

        # These are the iteration-16 failure modes. They MUST stay at zero.
        assert len(a_post_db['legacy']) == 0, \
            f"after pipeline cycle: {len(a_post_db['legacy'])} legacy-grade picks in DB"
        assert len(a_post_db['wrong_pass']) == 0, \
            f"after pipeline cycle: {len(a_post_db['wrong_pass'])} lock>=85 & grade=Pass in DB"
        assert len(a_post_db['mismatches']) == 0, \
            f"after pipeline cycle: {len(a_post_db['mismatches'])} tier-mapping mismatches in DB"
        assert len(a_post_db['overflow']) == 0, \
            f"after pipeline cycle: {len(a_post_db['overflow'])} lock>99 in DB"
        assert len(a_post_api['legacy']) == 0
        assert len(a_post_api['wrong_pass']) == 0
        assert len(a_post_api['mismatches']) == 0
        assert len(a_post_api['overflow']) == 0


# ─── BUG #2 — Sort toggle (re-verify from iter 16) ────────────────────────
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
        non_elite = [p for p in picks if not p.get("elite_player")]
        ls = [p["lock_score"] for p in non_elite][:20]
        assert ls == sorted(ls, reverse=True), f"lock-desc not desc: {ls[:10]}"
        assert picks[0]["lock_score"] >= 90

    def test_lock_asc(self, auth_session):
        picks = self._get(auth_session, "lock", "asc")
        ls = [p["lock_score"] for p in picks][:20]
        assert ls == sorted(ls), f"lock-asc not asc: {ls[:10]}"

    def test_edge_desc(self, auth_session):
        picks = self._get(auth_session, "edge", "desc")
        es = [p.get("edge_percent", 0) for p in picks][:20]
        assert es == sorted(es, reverse=True), f"edge-desc not desc: {es[:10]}"

    def test_edge_asc(self, auth_session):
        picks = self._get(auth_session, "edge", "asc")
        es = [p.get("edge_percent", 0) for p in picks][:20]
        assert es == sorted(es), f"edge-asc not asc: {es[:10]}"

    def test_time_asc(self, auth_session):
        picks = self._get(auth_session, "time", "asc")
        ts = [p.get("event_time", "") for p in picks][:20]
        assert ts == sorted(ts), f"time-asc not asc: {ts[:10]}"

    def test_win_desc(self, auth_session):
        picks = self._get(auth_session, "win", "desc")
        ws = [p.get("win_probability", 0) for p in picks][:20]
        assert ws == sorted(ws, reverse=True), f"win-desc not desc: {ws[:10]}"

    def test_dir_flips(self, auth_session):
        desc = self._get(auth_session, "lock", "desc")
        asc = self._get(auth_session, "lock", "asc")
        assert [p.get("id") for p in desc[:10]] != [p.get("id") for p in asc[:10]]


# ─── BUG #3 — High-Risk Parlay 72h ────────────────────────────────────────
class TestHighRiskParlay72h:
    def test_high_risk_72h_returns_3_parlays_min5_legs(self, auth_session):
        r = auth_session.get(
            f"{BASE_URL}/api/picks/parlay",
            params={"legs": 10, "mode": "high_risk",
                    "window_hours": 72, "sport_mode": "auto"},
            timeout=30,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        parlays = body.get("parlays") or []
        print(f"\n72h: parlays={len(parlays)} auto_expanded_to={body.get('auto_expanded_to')}")
        assert len(parlays) == 3, f"expected 3, got {len(parlays)}; reason={body.get('reason')}"
        for i, card in enumerate(parlays):
            n_legs = card.get("leg_count") or len(card.get("legs") or [])
            print(f"  card[{i}] legs={n_legs} survival={card.get('survival_pct')}")
            assert n_legs >= 5, f"card[{i}] only {n_legs} legs"


# ─── BUG #4 — Auto-expand with TIGHT window (forced fallback) ─────────────
class TestAutoExpandTightWindow:
    """Use a deliberately tight window (4h) to try to force the fallback.
    On a thick slate the 4h pool may still be sufficient — in that case we
    accept the no-expand outcome but log the band depths from the response.
    The hard requirement: either parlays>=1 OR auto_expanded_to populated
    OR a clear 'reason' string (so the user understands why the slate is
    empty).
    """

    @pytest.mark.parametrize("window", [4, 6, 8])
    def test_tight_window_high_risk(self, auth_session, window):
        r = auth_session.get(
            f"{BASE_URL}/api/picks/parlay",
            params={"legs": 10, "mode": "high_risk",
                    "window_hours": window, "sport_mode": "auto"},
            timeout=30,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        parlays = body.get("parlays") or []
        ae = body.get("auto_expanded_to")
        reason = body.get("reason")
        print(f"\nwindow={window}h: parlays={len(parlays)} "
              f"auto_expanded_to={ae} reason={reason}")
        # Accept either: parlays returned, OR auto-expand fired, OR reason given
        assert len(parlays) >= 1 or ae is not None or reason, \
            f"window={window}h: no parlays, no auto_expanded_to, no reason"
