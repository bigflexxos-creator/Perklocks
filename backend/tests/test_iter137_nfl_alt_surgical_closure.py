"""
Iteration 137 · NFL alt-line Surgical Closure Pass regression check.

Backend-only verification of:
1. GET /api/nfl/atd/leaderboard returns HTTP 200 with mode=canonical_publication
   and non-empty picks[]  (regression from prior fix).
2. GET /api/picks/today?sport=NFL&lite=true returns HTTP 200 with Bearer.
3. No NFL alt pick with edge_percent<=0 has lock_score>=98 or apex_lock=true.
4. Trap-chalk NFL alt picks (edge_percent<=0) carry:
     alt_edge_cap_applied=True
     apex_status="NOT_APEX"
     apex_reason="nfl_alt_no_positive_edge_no_elite_authority"
5. Positive-edge NFL alt picks are UNTOUCHED (no alt_edge_cap_applied).
6. Import sanity for the four touched modules.
7. Ladder integrity spot-check: monotone implied_probability / win_probability
   as line increases for at least one NFL alt-ladder in DB.

DO NOT trigger POST /api/admin/picks/force-refresh — audit backfill is
already complete; a refresh would consume Odds API credit for no gain.
"""

import os
import asyncio
import pytest
import requests
from motor.motor_asyncio import AsyncIOMotorClient

BASE_URL = os.environ["EXPO_PUBLIC_BACKEND_URL"].rstrip("/")
DEMO_EMAIL = "demo@lockscore.ai"
DEMO_PASSWORD = "demo123"
MONGO_URL = "mongodb://localhost:27017"
DB_NAME = "lockscore_db"


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) \
        if False else asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture(scope="module")
def db():
    client = AsyncIOMotorClient(MONGO_URL)
    return client[DB_NAME]


# ─── shared fixtures ─────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def api_client():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module")
def bearer_token(api_client):
    r = api_client.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD},
        timeout=30,
    )
    if r.status_code != 200:
        pytest.skip(f"demo login failed: {r.status_code} {r.text[:200]}")
    data = r.json()
    token = (
        data.get("access_token")
        or data.get("token")
        or data.get("data", {}).get("access_token")
    )
    if not token:
        pytest.skip(f"no bearer in login response: {list(data.keys())}")
    return token


@pytest.fixture(scope="module")
def nfl_picks(api_client, bearer_token):
    r = api_client.get(
        f"{BASE_URL}/api/picks/today?sport=NFL&lite=true",
        headers={"Authorization": f"Bearer {bearer_token}"},
        timeout=60,
    )
    assert r.status_code == 200, f"HTTP {r.status_code} {r.text[:300]}"
    body = r.json()
    # response may be list, or {picks:[...]}, or {data:{picks:[...]}}
    if isinstance(body, list):
        picks = body
    elif isinstance(body, dict):
        picks = (
            body.get("picks")
            or body.get("data", {}).get("picks")
            or body.get("items")
            or []
        )
    else:
        picks = []
    return picks


def _is_alt_line(p: dict) -> bool:
    mkt = (p.get("market") or "")
    return (
        "ALT LOCK" in mkt
        or p.get("alt_line") is True
        or p.get("is_alt_line") is True
    )


# ─── 1 · ATD leaderboard regression ──────────────────────────────────────
class TestATDLeaderboard:
    def test_atd_leaderboard_200_canonical_publication_non_empty(self, api_client):
        r = api_client.get(f"{BASE_URL}/api/nfl/atd/leaderboard", timeout=45)
        assert r.status_code == 200, f"HTTP {r.status_code} {r.text[:300]}"
        body = r.json()
        assert body.get("mode") == "canonical_publication", (
            f"expected mode=canonical_publication, got {body.get('mode')}"
        )
        picks = body.get("picks") or []
        assert isinstance(picks, list) and len(picks) > 0, (
            f"picks[] must be non-empty, got {len(picks)}"
        )


# ─── 2/3 · picks/today NFL lite + value-floor guarantees ─────────────────
class TestNFLPicksLiteValueFloor:
    def test_endpoint_returns_200_with_bearer(self, nfl_picks):
        # Fixture already asserts 200. Ensure we got a list.
        assert isinstance(nfl_picks, list), (
            f"picks response should be list, got {type(nfl_picks)}"
        )

    def test_no_nfl_alt_with_nonpositive_edge_reaches_elite_or_apex(self, nfl_picks):
        offenders = []
        for p in nfl_picks:
            if not _is_alt_line(p):
                continue
            try:
                edge = float(p.get("edge_percent") or 0.0)
            except (TypeError, ValueError):
                edge = 0.0
            try:
                ls = float(p.get("lock_score") or 0.0)
            except (TypeError, ValueError):
                ls = 0.0
            apex = bool(p.get("apex_lock"))
            if edge <= 0.0 and (ls >= 98.0 or apex):
                offenders.append({
                    "id": p.get("id") or p.get("pick_id"),
                    "market": p.get("market"),
                    "edge_percent": edge,
                    "lock_score": ls,
                    "apex_lock": apex,
                })
        assert not offenders, (
            f"NFL alt picks with edge<=0 reached Elite/APEX: {offenders[:5]}"
        )


# ─── 4 · trap-chalk alt picks carry surgical-closure flags ───────────────
class TestTrapChalkFlags:
    """The 49 audit-backfilled trap-chalk alt picks now live in DB under a
    prior pick_date (2026-09-07). They will NOT appear on today's board
    (`/api/picks/today`) because today's board is a fresh 2026-09-08 slate
    with zero NFL alt-line emissions. Verify them directly in the picks
    collection."""

    def test_49_trap_chalk_alts_carry_expected_flags(self, db):
        async def _run():
            n_total = await db.picks.count_documents({
                "sport": "NFL",
                "alt_edge_cap_applied": True,
            })
            n_apex = await db.picks.count_documents({
                "sport": "NFL",
                "alt_edge_cap_applied": True,
                "apex_status": "NOT_APEX",
                "apex_lock": False,
                "apex_reason": "nfl_alt_no_positive_edge_no_elite_authority",
            })
            # Sample verification
            sample = await db.picks.find_one({
                "sport": "NFL",
                "alt_edge_cap_applied": True,
            })
            return n_total, n_apex, sample

        n_total, n_apex, sample = asyncio.new_event_loop().run_until_complete(_run())
        print(f"\n[trap-chalk] total={n_total}, all_flags_correct={n_apex}")
        if sample:
            print(f"[trap-chalk] sample: market={sample.get('market')!r} "
                  f"line={sample.get('line')} edge={sample.get('edge_percent')} "
                  f"pre_elite={sample.get('pre_elite_lock_score')} "
                  f"lock_score={sample.get('lock_score')} "
                  f"apex={sample.get('apex_status')} "
                  f"cap_reason={sample.get('alt_edge_cap_reason')}")

        assert n_total >= 49, (
            f"expected >=49 backfilled trap-chalk alt picks, got {n_total}"
        )
        assert n_apex == n_total, (
            f"only {n_apex}/{n_total} trap-chalk alts carry all 4 required "
            f"flags (alt_edge_cap_applied + apex_status=NOT_APEX + "
            f"apex_lock=False + correct apex_reason)"
        )

    def test_trap_chalk_all_have_nonpositive_edge_and_capped_lock_score(self, db):
        async def _run():
            offenders = []
            cursor = db.picks.find({
                "sport": "NFL",
                "alt_edge_cap_applied": True,
            })
            async for p in cursor:
                try:
                    edge = float(p.get("edge_percent") or 0.0)
                except (TypeError, ValueError):
                    edge = 0.0
                try:
                    ls = float(p.get("lock_score") or 0.0)
                except (TypeError, ValueError):
                    ls = 0.0
                if edge > 0.0 or ls >= 98.0 or p.get("apex_lock") is True:
                    offenders.append({
                        "id": p.get("id") or str(p.get("_id")),
                        "market": p.get("market"),
                        "edge_percent": edge,
                        "lock_score": ls,
                        "apex_lock": p.get("apex_lock"),
                    })
            return offenders

        offenders = asyncio.new_event_loop().run_until_complete(_run())
        assert not offenders, (
            f"trap-chalk alts violate value-floor invariant: {offenders[:5]}"
        )


# ─── 5 · positive-edge alts untouched ────────────────────────────────────
class TestPositiveEdgeAltsUntouched:
    """Positive-edge NFL alt picks must NOT carry alt_edge_cap_applied.
    Check today's board first; fall back to DB if board is empty for alts."""

    def test_positive_edge_nfl_alts_have_no_cap_flag(self, nfl_picks, db):
        # Board-side check (today's picks)
        board_pos = [p for p in nfl_picks
                     if _is_alt_line(p) and float(p.get("edge_percent") or 0) > 0]
        # DB-side check (all published NFL alt picks with positive edge)
        async def _fetch():
            out = []
            cursor = db.picks.find({
                "sport": "NFL",
                "is_alt": True,
                "edge_percent": {"$gt": 0},
                "publication_state": "PUBLISHED",
            })
            async for p in cursor:
                out.append(p)
            return out

        db_pos = asyncio.new_event_loop().run_until_complete(_fetch())

        pool = board_pos + db_pos
        if not pool:
            pytest.skip("no positive-edge NFL alts on today's board or in DB")

        contaminated = [
            {
                "id": p.get("id") or str(p.get("_id")),
                "market": p.get("market"),
                "edge_percent": p.get("edge_percent"),
            }
            for p in pool if p.get("alt_edge_cap_applied") is True
        ]
        print(f"\n[positive-edge] board={len(board_pos)} db={len(db_pos)} "
              f"contaminated={len(contaminated)}")
        assert not contaminated, (
            f"positive-edge NFL alts should NOT have alt_edge_cap_applied: "
            f"{contaminated[:5]}"
        )


# ─── 6 · import sanity ───────────────────────────────────────────────────
class TestImportSanity:
    def test_touched_modules_import_cleanly(self):
        import importlib
        for name in (
            "sports_engine",
            "services.magic.lock_score_integrator",
            "services.elite_evidence_gate",
            "services.pick_refresh_orchestrator",
        ):
            importlib.import_module(name)


# ─── 7 · ladder integrity spot-check ─────────────────────────────────────
class TestLadderIntegrity:
    """Spot-check monotonicity of an alt-ladder in DB (e.g. Trevor Lawrence
    Pass Yds 149.5 / 159.5 / 169.5). Prefer OVER side ladders where implied
    probability should be monotone decreasing as line increases."""

    def test_nfl_alt_ladder_monotone_and_odds_present(self, db):
        from collections import defaultdict

        async def _fetch():
            cursor = db.picks.find({
                "sport": "NFL",
                "is_alt": True,
                "publication_state": "PUBLISHED",
            })
            groups = defaultdict(list)
            async for p in cursor:
                market = (p.get("market") or "")
                line = p.get("line")
                side = (p.get("side") or "").upper()
                if line is None:
                    continue
                try:
                    line_f = float(line)
                except (TypeError, ValueError):
                    continue
                # Family key = market string without the line number
                # (e.g. "Trevor Lawrence Over 149.5 Player Pass Yds · ALT LOCK")
                # Group by (player+market family, side).
                # Simpler: derive family = base market before line + statType
                # by using canonical_player_id + normalized stat.
                key = (
                    p.get("canonical_player_id") or p.get("player_id")
                    or p.get("elite_player_name"),
                    "".join(ch for ch in market
                            if not ch.isdigit() and ch not in ".").strip(),
                    side,
                )
                groups[key].append({
                    "line": line_f,
                    "implied_probability": p.get("implied_probability"),
                    "win_probability": p.get("win_probability"),
                    "book_odds": p.get("book_odds"),
                    "edge_percent": p.get("edge_percent"),
                    "market": market,
                    "id": p.get("id") or str(p.get("_id")),
                })
            return groups

        groups = asyncio.new_event_loop().run_until_complete(_fetch())
        ladders = {k: sorted(v, key=lambda r: r["line"])
                   for k, v in groups.items() if len(v) >= 2}
        if not ladders:
            pytest.skip("no NFL alt ladder with >=2 rungs in DB")

        # Prefer OVER ladders (unambiguous monotone-decreasing expectation)
        over_ladders = {k: v for k, v in ladders.items() if "OVER" in k[2]}
        target = over_ladders or ladders
        # Grab up to 3 ladders to spot-check
        sample_keys = list(target.keys())[:3]

        failures = []
        for key in sample_keys:
            rungs = target[key]
            player, family, side = key
            print(f"\n[ladder] {player} | {family} | {side} → "
                  f"{len(rungs)} rungs")
            for r in rungs:
                print(f"   line={r['line']:>6}  implied={r['implied_probability']}"
                      f"  win={r['win_probability']}  odds={r['book_odds']}"
                      f"  edge={r['edge_percent']}")

            direction = "decreasing" if "OVER" in side else \
                        "increasing" if "UNDER" in side else "decreasing"

            def _mono(seq, d):
                xs = [x for x in seq if x is not None]
                if len(xs) < 2:
                    return True
                if d == "decreasing":
                    return all(xs[i] >= xs[i + 1] for i in range(len(xs) - 1))
                return all(xs[i] <= xs[i + 1] for i in range(len(xs) - 1))

            imp_ok = _mono([r["implied_probability"] for r in rungs], direction)
            win_ok = _mono([r["win_probability"] for r in rungs], direction)
            odds_ok = all(r["book_odds"] not in (None, 0, "") for r in rungs)
            if not (imp_ok and win_ok and odds_ok):
                failures.append({
                    "key": key,
                    "implied_ok": imp_ok,
                    "win_ok": win_ok,
                    "odds_ok": odds_ok,
                })

        assert not failures, (
            f"ladder monotonicity / odds integrity failures: {failures}"
        )
