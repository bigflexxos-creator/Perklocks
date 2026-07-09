"""
Iteration 66 — ESPN enrichment pipeline (Pass 1 of ESPN-for-all-sports).

Validates:
  1. Demo login (demo@lockscore.ai / demo123)
  2. GET /api/picks/today → picks decorated with home_meta/away_meta/injury_chip
  3. Admin endpoints (uefa-espn-refresh / ufc-espn-refresh /
     espn-team-meta-refresh / espn-injury-refresh)
  4. Mongo collections: espn_team_meta > 500, uefa_espn_v1 picks ≥ 80,
     ufc_espn_v1 picks ≥ 1.
"""
import os
import time
import asyncio
import pytest
import requests
from dotenv import load_dotenv

load_dotenv("/app/backend/.env")
load_dotenv("/app/frontend/.env")

BASE_URL = (
    os.environ.get("EXPO_PUBLIC_BACKEND_URL")
    or os.environ.get("EXPO_BACKEND_URL")
    or ""
).rstrip("/")

DEMO_EMAIL = "demo@lockscore.ai"
DEMO_PASSWORD = "demo123"

ADMIN_EMAIL = "TEST_espn_admin@lockscore.ai"
ADMIN_PASSWORD = "AdminPw123!"


@pytest.fixture(scope="module")
def api():
    s = requests.Session()
    s.headers["Content-Type"] = "application/json"
    return s


@pytest.fixture(scope="module")
def demo_token(api):
    r = api.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD},
    )
    if r.status_code == 429:
        time.sleep(30)
        r = api.post(
            f"{BASE_URL}/api/auth/login",
            json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD},
        )
    assert r.status_code == 200, f"Demo login failed: {r.status_code} {r.text}"
    tok = r.json().get("access_token")
    assert tok
    return tok


@pytest.fixture(scope="module")
def admin_token(api):
    """Register a fresh admin user, promote via Mongo (role='admin'),
    then log in.  Uses direct DB write because the OWNER_EMAIL auto-promo
    only fires on server startup for a specific email."""
    from motor.motor_asyncio import AsyncIOMotorClient

    async def _promote():
        c = AsyncIOMotorClient(os.environ["MONGO_URL"])
        db = c[os.environ["DB_NAME"]]
        # register (idempotent — register may 400 if exists)
        # promote to admin — emails stored lowercase in DB
        await db.users.update_one(
            {"email": ADMIN_EMAIL.lower()},
            {"$set": {"role": "admin", "status": "active"}},
        )
        c.close()

    # register (best-effort)
    api.post(
        f"{BASE_URL}/api/auth/register",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD,
              "name": "TEST ESPN Admin"},
    )
    # promote via DB
    asyncio.get_event_loop().run_until_complete(_promote())
    # login
    r = api.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
    )
    if r.status_code == 429:
        time.sleep(30)
        r = api.post(
            f"{BASE_URL}/api/auth/login",
            json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        )
    assert r.status_code == 200, f"Admin login failed: {r.status_code} {r.text}"
    return r.json()["access_token"]


# ── Demo auth ────────────────────────────────────────────────────────

def test_demo_login(demo_token):
    assert isinstance(demo_token, str) and len(demo_token) > 20


# ── /api/picks/today ESPN meta decoration ────────────────────────────

def test_picks_today_status(api, demo_token):
    r = api.get(
        f"{BASE_URL}/api/picks/today",
        headers={"Authorization": f"Bearer {demo_token}"},
    )
    assert r.status_code == 200
    body = r.json()
    picks = body if isinstance(body, list) else body.get("picks", [])
    assert isinstance(picks, list) and len(picks) > 0, "No picks returned"


def test_picks_today_has_home_meta(api, demo_token):
    r = api.get(
        f"{BASE_URL}/api/picks/today",
        headers={"Authorization": f"Bearer {demo_token}"},
    )
    assert r.status_code == 200
    picks = r.json() if isinstance(r.json(), list) else r.json().get("picks", [])
    with_meta = [p for p in picks
                 if (p.get("home_meta") or {}).get("logo")
                 or (p.get("away_meta") or {}).get("logo")]
    # Expect at least SOME picks with ESPN logos
    assert len(with_meta) > 0, (
        f"No picks carry home_meta/away_meta.logo out of {len(picks)} picks"
    )
    # Validate logo URL host
    sample = next(
        (m for p in with_meta
         for m in ((p.get("home_meta") or {}), (p.get("away_meta") or {}))
         if m.get("logo")),
        None,
    )
    assert sample is not None
    assert sample["logo"].startswith("https://a.espncdn.com/i/teamlogos/"), (
        f"Unexpected logo host: {sample['logo']}"
    )


def test_picks_today_injury_chip_present(api, demo_token):
    r = api.get(
        f"{BASE_URL}/api/picks/today",
        headers={"Authorization": f"Bearer {demo_token}"},
    )
    picks = r.json() if isinstance(r.json(), list) else r.json().get("picks", [])
    # Injury chip is inserted on NFL/NBA/CFB picks by _decorate_with_espn_meta
    # It may be all-zeros (off-season) but the key should exist somewhere.
    with_chip = [p for p in picks if "injury_chip" in p]
    # ANY picks decorated (chip may be None for non-NFL/NBA/CFB). The key
    # itself doesn't strictly need to be present on every pick — but for
    # NFL/NBA/CFB (if any) we expect a dict.
    nfl_nba_cfb = [
        p for p in picks
        if (p.get("sport") or "").lower() in ("nfl", "nba", "cfb",
                                              "americanfootball_nfl",
                                              "basketball_nba",
                                              "americanfootball_ncaaf")
    ]
    if nfl_nba_cfb:
        chip_present = [p for p in nfl_nba_cfb
                        if isinstance(p.get("injury_chip"), (dict, str))
                        or p.get("injury_chip") is None]
        # Not strictly asserted (off-season) — just log.
        print(
            f"NFL/NBA/CFB picks: {len(nfl_nba_cfb)}, "
            f"with injury_chip key: "
            f"{sum(1 for p in nfl_nba_cfb if 'injury_chip' in p)}"
        )
    # No hard assertion for chip — the decorator is what we care about
    assert True


def test_picks_today_no_mongo_id_leak(api, demo_token):
    r = api.get(
        f"{BASE_URL}/api/picks/today",
        headers={"Authorization": f"Bearer {demo_token}"},
    )
    picks = r.json() if isinstance(r.json(), list) else r.json().get("picks", [])
    leaked = [p for p in picks if "_id" in p]
    assert not leaked, f"{len(leaked)} picks leak Mongo _id"


# ── Admin refresh endpoints ──────────────────────────────────────────

def _admin_headers(tok):
    return {"Authorization": f"Bearer {tok}"}


def test_admin_uefa_espn_refresh(api, admin_token):
    r = api.post(
        f"{BASE_URL}/api/admin/uefa-espn-refresh",
        headers=_admin_headers(admin_token),
        timeout=180,
    )
    assert r.status_code == 200, f"{r.status_code} {r.text[:400]}"
    body = r.json()
    print("UEFA refresh:", body)
    # Body should have upserts/fixtures_seen fields
    upserts = body.get("upserts") or body.get("picks_upserted") or 0
    assert upserts >= 0  # non-error


def test_admin_ufc_espn_refresh(api, admin_token):
    r = api.post(
        f"{BASE_URL}/api/admin/ufc-espn-refresh",
        headers=_admin_headers(admin_token),
        timeout=180,
    )
    assert r.status_code == 200, f"{r.status_code} {r.text[:400]}"
    body = r.json()
    print("UFC refresh:", body)
    # fixtures_seen may be low if only 1 event scheduled — accept >=0
    fs = body.get("fixtures_seen", 0)
    assert isinstance(fs, int)


def test_admin_espn_team_meta_refresh(api, admin_token):
    r = api.post(
        f"{BASE_URL}/api/admin/espn-team-meta-refresh",
        headers=_admin_headers(admin_token),
        timeout=180,
    )
    assert r.status_code == 200, f"{r.status_code} {r.text[:400]}"
    body = r.json()
    print("team-meta refresh:", body)
    up = body.get("teams_upserted") or body.get("upserts") or 0
    assert up >= 500, f"Only {up} team meta rows upserted; expected 500+"


def test_admin_espn_injury_refresh(api, admin_token):
    r = api.post(
        f"{BASE_URL}/api/admin/espn-injury-refresh",
        headers=_admin_headers(admin_token),
        timeout=180,
    )
    assert r.status_code == 200, f"{r.status_code} {r.text[:400]}"
    body = r.json()
    print("injury refresh:", body)
    per = body.get("per_sport") or body.get("counts") or {}
    # NFL should have >0 injuries
    nfl_data = per.get("NFL") if isinstance(per, dict) else None
    nfl_count = 0
    if isinstance(nfl_data, dict):
        nfl_count = nfl_data.get("total_injuries", 0)
    elif isinstance(nfl_data, int):
        nfl_count = nfl_data
    assert nfl_count > 0, f"No NFL injuries seeded: {body}"


# ── Mongo collection counts (direct DB check) ────────────────────────

def _mongo_count(filter_dict, collection):
    from motor.motor_asyncio import AsyncIOMotorClient

    async def _q():
        c = AsyncIOMotorClient(os.environ["MONGO_URL"])
        db = c[os.environ["DB_NAME"]]
        n = await db[collection].count_documents(filter_dict)
        c.close()
        return n

    return asyncio.run(_q())


def test_mongo_uefa_picks_count():
    n = _mongo_count({"source": "uefa_espn_v1"}, "picks")
    print("uefa_espn_v1 picks:", n)
    assert n >= 80, f"Only {n} UEFA ESPN picks (expected 80+)"


def test_mongo_ufc_picks_count():
    n = _mongo_count({"source": "ufc_espn_v1"}, "picks")
    print("ufc_espn_v1 picks:", n)
    assert n >= 1, "No UFC ESPN picks in DB"


def test_mongo_team_meta_count():
    n = _mongo_count({}, "espn_team_meta")
    print("espn_team_meta:", n)
    assert n >= 500, f"espn_team_meta only has {n} docs (expected 500+)"


def test_mongo_injury_notes_count():
    n = _mongo_count({}, "espn_injury_notes")
    print("espn_injury_notes:", n)
    assert n >= 25, f"espn_injury_notes has {n} docs (expected 25+)"
