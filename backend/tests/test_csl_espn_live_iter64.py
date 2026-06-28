"""Backend-only tests for the CSL ESPN Live retired-player filter (iter 64).

Covers:
  1. /api/picks/today returns 200 + payload structure (authenticated).
  2. /api/picks/rollover has NO goal-scorer markets (authenticated).
  3. CSL ESPN refresh log line was emitted within 30s of startup.
  4. Direct module test: hydrate_from_db + is_player_currently_active assertions
     for Guy Mbenza, Crysan, Cedric Bakambu, NotARealPlayer XYZ.
  5. db.picks with is_synthetic_scorer=True AND CSL league must have a
     player_name that is NOT explicitly returned active=False by csl_espn_live.
  6. /api/version returns 200 (no startup crash).
"""
from __future__ import annotations

import asyncio
import os
import re
import sys

import pytest
import requests

# Ensure /app/backend is importable so we can run direct module tests.
sys.path.insert(0, "/app/backend")

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    raise RuntimeError("EXPO_PUBLIC_BACKEND_URL not set in env")

MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "lockscore_db")

LOG_PATHS = [
    "/var/log/supervisor/backend.err.log",
    "/var/log/supervisor/backend.out.log",
]

DEMO_EMAIL = "demo@lockscore.ai"
DEMO_PASSWORD = "demo123"


@pytest.fixture(scope="module")
def api():
    """Authenticated requests session using the seeded demo user."""
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    # Login & attach bearer.
    r = s.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD},
        timeout=20,
    )
    if r.status_code != 200:
        pytest.skip(f"demo login failed ({r.status_code}): {r.text[:200]}")
    tok = r.json().get("access_token")
    if not tok:
        pytest.skip(f"no access_token in login response: {r.text[:200]}")
    s.headers["Authorization"] = f"Bearer {tok}"
    return s


def _run_async(coro):
    """Tiny shim to run async coroutines without pytest-asyncio."""
    return asyncio.run(coro)


# ─────────────────────────── 6. Smoke: /api/version ───────────────────────────
def test_version_endpoint_returns_200():
    r = requests.get(f"{BASE_URL}/api/version", timeout=20)
    assert r.status_code == 200, f"GET /api/version returned {r.status_code}: {r.text[:200]}"
    body = r.json()
    assert isinstance(body, dict)


# ─────────────────────────── 1. /api/picks/today ───────────────────────────
def test_picks_today_returns_200_and_payload(api):
    r = api.get(f"{BASE_URL}/api/picks/today", timeout=90)
    assert r.status_code == 200, f"/api/picks/today returned {r.status_code}: {r.text[:300]}"
    body = r.json()
    assert isinstance(body, dict), f"expected dict, got {type(body)}"
    assert "picks" in body, f"missing 'picks' key. keys={list(body.keys())[:10]}"
    assert isinstance(body["picks"], list)
    if body["picks"]:
        p = body["picks"][0]
        # Sanity: every pick should at minimum have an id-like key + sport-like key.
        assert any(k in p for k in ("id", "pick_id", "_id"))
        assert any(k in p for k in ("sport", "sport_key", "sport_title"))


# ─────────────────────────── 2. /api/picks/rollover ───────────────────────────
def test_picks_rollover_has_no_goal_scorer_markets(api):
    r = api.get(f"{BASE_URL}/api/picks/rollover", timeout=90)
    assert r.status_code == 200, f"/api/picks/rollover returned {r.status_code}: {r.text[:300]}"
    body = r.json()
    if isinstance(body, dict):
        picks = body.get("picks", body.get("results", []))
    else:
        picks = body
    assert isinstance(picks, list)
    bad = []
    for p in picks:
        market = (p.get("market") or p.get("market_key") or "").lower()
        prop = (p.get("prop") or p.get("prop_type") or "").lower()
        synth = p.get("is_synthetic_scorer")
        if (
            "anytime_goal_scorer" in market
            or "goal_scorer" in market
            or "anytime_scorer" in market
            or "scorer" in prop
            or synth is True
        ):
            bad.append({
                "id": p.get("id") or p.get("pick_id"),
                "market": market,
                "prop": prop,
                "synth": synth,
            })
    assert not bad, f"rollover unexpectedly contained goal-scorer markets: {bad[:5]}"


# ─────────────────────────── 3. CSL ESPN refresh log line ───────────────────
def test_csl_espn_refresh_log_line_emitted():
    pattern = re.compile(
        r"CSL ESPN refresh:\s*\{'ok':\s*True,\s*'season':\s*'\d{4}',\s*'teams':\s*\d+,\s*'players_active':\s*\d+"
    )
    found = False
    sample = ""
    for path in LOG_PATHS:
        try:
            with open(path, "r", errors="ignore") as f:
                data = f.read()
        except FileNotFoundError:
            continue
        m = pattern.search(data)
        if m:
            found = True
            sample = m.group(0)
            break
    assert found, "Did not find canonical 'CSL ESPN refresh: {...}' log line in backend logs"
    print(f"Matched refresh log line: {sample}")


# ─────────────────── 4. Direct module test ────────────────────────────────
def test_csl_espn_module_direct_assertions():
    async def _inner():
        from motor.motor_asyncio import AsyncIOMotorClient
        import csl_espn_live

        client = AsyncIOMotorClient(MONGO_URL)
        db = client[DB_NAME]
        try:
            await csl_espn_live.hydrate_from_db(db)
            snap = csl_espn_live.snapshot_state()
            if (snap.get("active_players") or 0) == 0 and (snap.get("scorer_rows") or 0) == 0:
                await csl_espn_live.refresh(db)

            results = {
                "Guy Mbenza": csl_espn_live.is_player_currently_active("Guy Mbenza"),
                "Crysan": csl_espn_live.is_player_currently_active("Crysan"),
                "Cedric Bakambu": csl_espn_live.is_player_currently_active("Cedric Bakambu"),
                "NotARealPlayer XYZ": csl_espn_live.is_player_currently_active("NotARealPlayer XYZ"),
            }
            forms = {
                k: csl_espn_live.get_live_form(k) for k in results
            }
            print(f"CSL ESPN module verdicts: {results}")
            print(f"CSL ESPN live forms: {forms}")

            assert results["Guy Mbenza"] is False, (
                f"Guy Mbenza should be inactive per ESPN, got {results['Guy Mbenza']!r}"
            )
            assert results["Crysan"] is True, (
                f"Crysan should be currently active per ESPN, got {results['Crysan']!r}"
            )
            assert results["Cedric Bakambu"] is False, (
                f"Cedric Bakambu should be inactive per ESPN, got {results['Cedric Bakambu']!r}"
            )
            assert results["NotARealPlayer XYZ"] is False, (
                f"Garbage name should be False, got {results['NotARealPlayer XYZ']!r}"
            )
        finally:
            client.close()

    _run_async(_inner())


# ────── 5. db.picks: every synthetic-scorer CSL pick must not reference an inactive player
def test_no_synth_csl_pick_references_inactive_player():
    async def _inner():
        from motor.motor_asyncio import AsyncIOMotorClient
        import csl_espn_live

        client = AsyncIOMotorClient(MONGO_URL)
        db = client[DB_NAME]
        try:
            await csl_espn_live.hydrate_from_db(db)
            snap = csl_espn_live.snapshot_state()
            if (snap.get("active_players") or 0) == 0 and (snap.get("scorer_rows") or 0) == 0:
                await csl_espn_live.refresh(db)

            query = {
                "is_synthetic_scorer": True,
                "$or": [
                    {"league": "Chinese Super League"},
                    {"sport_key": "soccer_china_superleague"},
                ],
            }
            cursor = db.picks.find(
                query,
                {"_id": 0, "player_name": 1, "league": 1, "sport_key": 1, "id": 1},
            )
            picks = await cursor.to_list(length=500)
            print(f"Found {len(picks)} synthetic CSL scorer picks in db.picks")

            violations = []
            for p in picks:
                pname = p.get("player_name")
                if not pname:
                    continue
                verdict = csl_espn_live.is_player_currently_active(pname)
                if verdict is False:
                    violations.append({
                        "player_name": pname,
                        "id": p.get("id"),
                        "league": p.get("league"),
                    })

            assert not violations, (
                f"Found {len(violations)} synth CSL scorer pick(s) referencing INACTIVE players: "
                f"{violations[:10]}"
            )
        finally:
            client.close()

    _run_async(_inner())
