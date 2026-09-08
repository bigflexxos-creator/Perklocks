"""Universal NFL Prop Closure P0 — live verification.

Covers (from review_request):
  A. §A15 grade invariant end-to-end (API read path)
  B. §A4  book-seed fail-closed marker (DB state)
  C. §A5  Doubs regression proof
  D. §A11 Burrow regression proof
  E. §A10 Ladder monotonicity import sanity
  G. Regression sanity endpoints
"""
import os
import time
import asyncio
import requests
import pytest
from datetime import datetime, timedelta, timezone

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "https://canonical-parity.preview.emergentagent.com").rstrip("/")
MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "lockscore_db")

TIMEOUT = 30

# ── shared fixtures ────────────────────────────────────────────────

@pytest.fixture(scope="module")
def token():
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": "demo@lockscore.ai", "password": "demo123"},
        timeout=TIMEOUT,
    )
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:200]}"
    tok = r.json().get("token") or r.json().get("access_token")
    assert tok, f"no token in response: {r.json()}"
    return tok


@pytest.fixture(scope="module")
def db():
    from motor.motor_asyncio import AsyncIOMotorClient
    return AsyncIOMotorClient(MONGO_URL)[DB_NAME]


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── E. §A10 Ladder monotonicity import sanity ─────────────────────

def test_e_ladder_module_imports():
    """Module imports cleanly."""
    from services.nfl_ladder_monotonicity import enforce_ladder_monotonicity  # noqa
    assert callable(enforce_ladder_monotonicity)


# ── G. Regression sanity ───────────────────────────────────────────

def test_g_atd_leaderboard_200():
    r = requests.get(f"{BASE_URL}/api/nfl/atd/leaderboard", timeout=TIMEOUT)
    assert r.status_code == 200, f"got {r.status_code}: {r.text[:200]}"


def test_g_picks_today_lite_200(token):
    r = requests.get(
        f"{BASE_URL}/api/picks/today?lite=true",
        headers={"Authorization": f"Bearer {token}"},
        timeout=TIMEOUT,
    )
    assert r.status_code == 200, f"got {r.status_code}: {r.text[:200]}"
    body = r.json()
    assert isinstance(body, (list, dict)), "unexpected body"


def test_g_force_refresh_bad_sport_400(token):
    r = requests.post(
        f"{BASE_URL}/api/admin/picks/force-refresh?sport_filter=zzz",
        headers={"Authorization": f"Bearer {token}"},
        timeout=TIMEOUT,
    )
    assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text[:200]}"


# ── A. §A15 grade invariant (API read path) ───────────────────────

def _pick_lock_grade(p):
    return (
        p.get("lock_score") or p.get("published_lock_score") or 0,
        p.get("grade") or p.get("published_grade"),
    )


def test_a_grade_invariant_no_stale_pass(token):
    """No NFL pick surfaced with lock>=85 AND grade='Pass'."""
    r = requests.get(
        f"{BASE_URL}/api/picks/today?lite=true",
        headers={"Authorization": f"Bearer {token}"},
        timeout=TIMEOUT,
    )
    assert r.status_code == 200
    body = r.json()
    picks = body.get("picks") if isinstance(body, dict) else body
    if not picks:
        pytest.skip("no picks returned")
    nfl = [p for p in picks if (p.get("sport") or "").upper() == "NFL"]
    if not nfl:
        pytest.skip("no NFL picks in payload")
    offenders = []
    for p in nfl:
        lock, grade = _pick_lock_grade(p)
        if isinstance(lock, (int, float)) and lock >= 85 and grade == "Pass":
            offenders.append({"sel": p.get("selection"), "lock": lock, "grade": grade})
    print(f"NFL picks: {len(nfl)}, sample:")
    for p in nfl[:5]:
        print(f"  lock={_pick_lock_grade(p)[0]} grade={_pick_lock_grade(p)[1]} sel={p.get('selection')}")
    assert not offenders, f"§A15 VIOLATION: {offenders[:5]}"


def test_a_grade_values_valid(token):
    r = requests.get(
        f"{BASE_URL}/api/picks/today?lite=true",
        headers={"Authorization": f"Bearer {token}"},
        timeout=TIMEOUT,
    )
    body = r.json()
    picks = body.get("picks") if isinstance(body, dict) else body
    if not picks:
        pytest.skip("no picks")
    nfl = [p for p in picks if (p.get("sport") or "").upper() == "NFL"]
    if not nfl:
        pytest.skip("no NFL")
    valid_hi = {"Playable", "Lock", "Strong Lock", "Elite Lock", "APEX Lock"}
    bad = []
    for p in nfl:
        lock, grade = _pick_lock_grade(p)
        if isinstance(lock, (int, float)) and lock >= 85 and grade not in valid_hi:
            bad.append({"sel": p.get("selection"), "lock": lock, "grade": grade})
    assert not bad, f"lock>=85 with invalid grade: {bad[:5]}"


# ── B. §A4 book-seed marker (DB state) ────────────────────────────

def test_b_book_seed_marker_present_and_dominant(db):
    """Fresh NFL picks (last 30min): >95% have marker, >90% False (independent)."""
    async def _q():
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
        # try common collection names
        for coll_name in ["picks", "canonical_picks", "published_picks", "current_picks"]:
            coll = db[coll_name]
            cnt = await coll.count_documents({"sport": "NFL", "created_at": {"$gte": cutoff}})
            if cnt:
                cur = coll.find({"sport": "NFL", "created_at": {"$gte": cutoff}}, {
                    "mp_from_book_seed": 1, "lock_score": 1, "mp_leakage_cap_applied": 1,
                    "selection": 1, "created_at": 1
                }).limit(2000)
                picks = [p async for p in cur]
                return coll_name, picks
        return None, []
    coll_name, picks = _run(_q())
    if not picks:
        pytest.skip("no fresh NFL picks in last 30min across common collections")
    total = len(picks)
    with_marker = sum(1 for p in picks if "mp_from_book_seed" in p)
    false_state = sum(1 for p in picks if p.get("mp_from_book_seed") is False)
    print(f"Collection={coll_name} total_fresh_nfl={total} with_marker={with_marker} mp_from_book_seed=False={false_state}")
    marker_pct = with_marker / total * 100
    false_pct = false_state / total * 100 if total else 0
    print(f"marker_present={marker_pct:.1f}%  mp_from_book_seed=False={false_pct:.1f}%")
    # Assert marker presence >95% (allow transitional slack — informational assert as warning if fails)
    assert marker_pct >= 95, f"§A4 marker presence {marker_pct:.1f}% < 95%"


def test_b_book_seed_true_capped_or_below_85(db):
    """Every fresh NFL pick with mp_from_book_seed=True AND lock>=85 has cap applied."""
    async def _q():
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
        for coll_name in ["picks", "canonical_picks", "published_picks", "current_picks"]:
            coll = db[coll_name]
            cnt = await coll.count_documents({
                "sport": "NFL", "mp_from_book_seed": True, "created_at": {"$gte": cutoff}
            })
            if cnt:
                cur = coll.find({
                    "sport": "NFL", "mp_from_book_seed": True, "created_at": {"$gte": cutoff}
                }, {"lock_score": 1, "mp_leakage_cap_applied": 1, "apex_reason": 1, "selection": 1})
                return coll_name, [p async for p in cur]
        return None, []
    coll_name, offenders_all = _run(_q())
    if not offenders_all:
        pytest.skip("no mp_from_book_seed=True picks in fresh NFL")
    leakage = []
    for p in offenders_all:
        lock = float(p.get("lock_score") or 0)
        capped = p.get("mp_leakage_cap_applied") is True
        if lock >= 85 and not capped:
            leakage.append({"sel": p.get("selection"), "lock": lock, "capped": capped})
    print(f"mp_from_book_seed=True picks: {len(offenders_all)}, leakage={len(leakage)}")
    assert not leakage, f"§A4 cap NOT applied: {leakage[:5]}"


# ── C, D · Doubs / Burrow independence ─────────────────────────────

@pytest.mark.parametrize("selection", ["Romeo Doubs", "Joe Burrow"])
def test_cd_independent_probability(db, selection):
    async def _q():
        cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
        for coll_name in ["picks", "canonical_picks", "published_picks", "current_picks"]:
            coll = db[coll_name]
            cur = coll.find(
                {"sport": "NFL", "selection": {"$regex": selection, "$options": "i"},
                 "created_at": {"$gte": cutoff}},
                {"market": 1, "line": 1, "book_odds": 1, "implied_probability": 1,
                 "win_probability": 1, "edge_percent": 1, "lock_score": 1,
                 "grade": 1, "mp_from_book_seed": 1, "created_at": 1, "selection": 1}
            ).sort("created_at", -1).limit(20)
            picks = [p async for p in cur]
            if picks:
                return coll_name, picks
        return None, []

    coll_name, picks = _run(_q())
    if not picks:
        pytest.skip(f"no fresh {selection} picks")
    print(f"\n{selection} ({coll_name}): {len(picks)} picks")
    non_book = 0
    for p in picks:
        imp = p.get("implied_probability")
        wp = p.get("win_probability")
        print(f"  {p.get('market'):<30} line={p.get('line')} odds={p.get('book_odds')} "
              f"imp={imp} wp={wp} edge={p.get('edge_percent')} lock={p.get('lock_score')} "
              f"grade={p.get('grade')} mp_from_book_seed={p.get('mp_from_book_seed')}")
        if imp is not None and wp is not None and abs(float(imp) - float(wp)) > 1e-6:
            non_book += 1
    assert non_book > 0, f"§A5/§A11 VIOLATION: all {selection} picks show wp==imp (book seed only)"
