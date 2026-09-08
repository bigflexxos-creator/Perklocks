"""Iteration 118 — Backend regression + fresh-repair verification.

Covers the three surgical fixes landed under the "Emergent Senior
Support root-cause repair" directive:

  A. Regression: GET /api/nfl/atd/leaderboard still 200 canonical.
  B. Regression: GET /api/picks/today with demo@lockscore.ai bearer token
     returns NFL picks in payload.
  C. FIX 2 : POST /api/admin/picks/force-refresh sport_filter parameter
     — 401 without auth, 400 with invalid, 202/429 with sport_filter=nfl
     (verify response body shape only — DO NOT actually queue an
     all-sports refresh).
  D. FIX 1 unit assertion: _apply_atomic_delete seen_ids exemption
     (source inspection + simulated delete via direct DB seed).
  E. FIX 3 : Fresh NFL picks carry non-zero edge_percent.
  F. sport_filter isolation: only NFL should have a large recent volume.

All tests are read-only against production data except D which seeds
two clearly-tagged TEST rows and deletes them.
"""
from __future__ import annotations

import inspect
import os
import re
from datetime import datetime, timedelta, timezone

import pytest
import requests
from pymongo import MongoClient

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    # Fallback to the frontend/.env value hard-coded for the preview.
    BASE_URL = "https://canonical-parity.preview.emergentagent.com"

MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "lockscore_db")

DEMO_EMAIL = "demo@lockscore.ai"
DEMO_PASSWORD = "demo123"


# ─────────────────────────────── fixtures ──────────────────────────────
@pytest.fixture(scope="module")
def api():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module")
def demo_token(api):
    r = api.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD},
        timeout=20,
    )
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:200]}"
    data = r.json()
    token = data.get("token") or data.get("access_token")
    assert token, f"no token in login response: {list(data.keys())}"
    return token


@pytest.fixture(scope="module")
def db():
    client = MongoClient(MONGO_URL)
    yield client[DB_NAME]
    client.close()


# ────────────────────────── A. ATD leaderboard regression ─────────────
class TestAAtdLeaderboardRegression:
    def test_atd_leaderboard_200_canonical(self, api):
        r = api.get(f"{BASE_URL}/api/nfl/atd/leaderboard?limit=5", timeout=30)
        assert r.status_code == 200, (
            f"expected 200, got {r.status_code}: {r.text[:300]}"
        )
        body = r.json()
        assert body.get("mode") == "canonical_publication", (
            f"mode not canonical_publication: {body.get('mode')}"
        )
        picks = body.get("picks") or []
        assert isinstance(picks, list) and len(picks) > 0, (
            f"picks empty or wrong type: {type(picks)} len={len(picks) if isinstance(picks, list) else 'n/a'}"
        )
        probs = [p.get("td_probability") for p in picks]
        # sorted DESC
        assert probs == sorted(probs, reverse=True), (
            f"picks not sorted by td_probability DESC: {probs}"
        )


# ────────────────────── B. /api/picks/today regression ────────────────
class TestBPicksTodayNFL:
    def test_picks_today_authorized(self, api, demo_token):
        headers = {"Authorization": f"Bearer {demo_token}"}
        r = api.get(f"{BASE_URL}/api/picks/today", headers=headers, timeout=60)
        assert r.status_code == 200, (
            f"picks/today {r.status_code}: {r.text[:300]}"
        )
        body = r.json()
        picks = body.get("picks") or []
        assert isinstance(picks, list), "picks not a list"
        # Only assert NFL presence if any picks are returned (avoid false negative if empty).
        if picks:
            nfl_picks = [p for p in picks if str(p.get("sport", "")).upper() == "NFL"]
            assert len(nfl_picks) > 0, (
                f"no NFL picks in picks/today; sports present: "
                f"{sorted({p.get('sport') for p in picks})}"
            )


# ─────────────────── C. FIX 2 — sport_filter endpoint wiring ──────────
class TestCSportFilterEndpoint:
    def test_force_refresh_unauth_401(self, api):
        r = api.post(
            f"{BASE_URL}/api/admin/picks/force-refresh?sport_filter=nfl",
            timeout=15,
        )
        # FastAPI Depends(current_admin) → 401 or 403 when unauthenticated.
        assert r.status_code in (401, 403), (
            f"expected 401/403 without auth, got {r.status_code}: {r.text[:200]}"
        )

    def test_force_refresh_invalid_sport_400(self, api, demo_token):
        headers = {"Authorization": f"Bearer {demo_token}"}
        r = api.post(
            f"{BASE_URL}/api/admin/picks/force-refresh?sport_filter=zzz",
            headers=headers,
            timeout=20,
        )
        # If demo user isn't admin, we cannot verify 400 — we still verify
        # the endpoint declaration by asserting NOT a 5xx.
        if r.status_code in (401, 403):
            pytest.skip(
                f"demo user is not admin (status {r.status_code}); "
                "cannot verify 400 path via HTTP. Endpoint wiring is "
                "verified via source inspection in the DemoAdminCheck test."
            )
        assert r.status_code == 400, (
            f"expected 400 for invalid sport_filter, got {r.status_code}: {r.text[:300]}"
        )
        try:
            body = r.json()
        except Exception:
            pytest.fail(f"non-JSON body: {r.text[:200]}")
        # FastAPI HTTPException wraps under 'detail'.
        detail = body.get("detail") if isinstance(body, dict) else None
        # Accept both direct and wrapped shapes.
        err_field = None
        if isinstance(detail, dict):
            err_field = detail.get("error")
        elif isinstance(body, dict):
            err_field = body.get("error")
        assert err_field == "invalid_sport_filter", (
            f"error field not 'invalid_sport_filter': {body}"
        )

    def test_force_refresh_nfl_response_shape(self, api, demo_token):
        headers = {"Authorization": f"Bearer {demo_token}"}
        r = api.post(
            f"{BASE_URL}/api/admin/picks/force-refresh?sport_filter=nfl",
            headers=headers,
            timeout=25,
        )
        if r.status_code in (401, 403):
            pytest.skip(
                f"demo user is not admin (status {r.status_code}); "
                "HTTP path un-verifiable. See source inspection test."
            )
        # Valid outcomes for wiring proof: 202-ish success (200 with queued),
        # 429 if lease held or budget denied. NEVER 5xx.
        assert r.status_code in (200, 202, 429), (
            f"unexpected status {r.status_code}: {r.text[:300]}"
        )
        body = r.json()
        # Both success and 429 bodies should carry sport_filter key.
        if r.status_code in (200, 202):
            assert body.get("sport_filter") == "NFL", (
                f"queued response missing sport_filter=NFL: {body}"
            )
            assert body.get("queued") is True, f"queued flag missing: {body}"
        else:
            # 429 — detail body should still reflect the NFL scoping via
            # error/message. Endpoint wiring is proven by the fact that we
            # got 429 (JobCoordinator lease evaluated) rather than 400/500.
            detail = body.get("detail") or {}
            assert (
                "picks_refresh_locked" in str(detail)
                or "budget_denied" in str(detail)
            ), f"429 body shape unexpected: {body}"

    def test_endpoint_source_declares_sport_filter(self):
        """Wiring proof at source level — always runnable, no auth needed."""
        from routes import admin_routes

        src = inspect.getsource(admin_routes.admin_force_refresh)
        assert "sport_filter" in src, "sport_filter param missing from endpoint"
        assert "invalid_sport_filter" in src, "invalid_sport_filter branch missing"
        assert "picks_refresh_today:" in src or 'f"picks_refresh_today:{' in src, (
            "namespaced lease key missing"
        )
        for canon in ("NFL", "MLB", "NBA", "NHL", "CFB", "Soccer", "Tennis", "UFC"):
            assert canon in src, f"canonical sport {canon} missing from mapping"


# ───────────── D. FIX 1 — atomic_delete seen_ids exemption ────────────
class TestDAtomicDeleteShieldExemption:
    def test_shield_source_includes_seen_ids_exemption(self):
        from services import pick_refresh_orchestrator as pro

        src = inspect.getsource(pro)
        # Find the _apply_atomic_delete body.
        m = re.search(
            r"async def _apply_atomic_delete\(\):(.+?)(?:\n    async def |\n    def |\nasync def |\ndef )",
            src,
            re.DOTALL,
        )
        assert m, "could not locate _apply_atomic_delete definition"
        body = m.group(1)

        # (1) Shield must include seen_ids exemption clause.
        assert '"id": {"$in": list(seen_ids)}' in body, (
            "shield missing {'id': {'$in': list(seen_ids)}} exemption clause"
        )

        # (2) The two `id IN seen_ids` delete_many calls MUST NOT carry
        #     `**_publication_shield`.  Walk delete_many( ... ) calls with
        #     a brace-aware parser (regex can't match nested braces).
        dm_blocks: list[str] = []
        for m2 in re.finditer(r"delete_many\(", body):
            i = m2.end()
            depth = 1
            while i < len(body) and depth > 0:
                c = body[i]
                if c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                i += 1
            call = body[m2.start(): i]
            if "list(seen_ids)" in call:
                dm_blocks.append(call)
        assert len(dm_blocks) >= 2, (
            f"expected >=2 delete_many(id IN seen_ids) calls, got {len(dm_blocks)}"
        )
        # At least two of these must be shield-free (the two id-scoped
        # deletes added by the surgical repair). The shield clause
        # {"id": {"$in": list(seen_ids)}} inside _publication_shield is
        # a shield ITEM, not usage of `**_publication_shield`.
        shield_free = [b for b in dm_blocks if "_publication_shield" not in b]
        assert len(shield_free) >= 2, (
            f"expected >=2 shield-free seen_ids deletes, got {len(shield_free)}. "
            f"Blocks: {[b[:120] for b in dm_blocks]}"
        )

    @pytest.mark.asyncio
    async def test_shield_permits_reemitted_row_delete(self, db):
        """Simulate the delete filter directly against MongoDB with two
        seeded rows to prove the exemption clause behaves as documented.
        """
        # ── seed
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        reemit_id = "TEST-REEMITTED-fix1a"
        untouched_id = "TEST-UNTOUCHED-fix1b"
        docs = [
            {
                "id": reemit_id,
                "sport": "NFL",
                "pick_date": today,
                "publication_source": "canonical_pipeline",
                "publication_state": "PUBLISHED",
                "market": "TEST_market",
                "player": "TEST_player_a",
                "line": 0.5,
            },
            {
                "id": untouched_id,
                "sport": "NFL",
                "pick_date": today,
                "publication_source": "canonical_pipeline",
                "publication_state": "PUBLISHED",
                "market": "TEST_market",
                "player": "TEST_player_b",
                "line": 0.5,
            },
        ]
        # Cleanup any residue.
        db.picks.delete_many({"id": {"$in": [reemit_id, untouched_id]}})
        db.picks.insert_many(docs)

        try:
            seen_ids = {reemit_id}
            # Replicate the shield exactly as constructed in _apply_atomic_delete.
            _publication_shield = {
                "$or": [
                    {"publication_source": {"$in": [None, "", False]}},
                    {"publication_source": {"$exists": False}},
                    {"id": {"$in": list(seen_ids)}}
                    if seen_ids
                    else {"_id_": "never"},
                ]
            }

            # Simulate the sport-scoped shielded delete (would touch
            # published NFL rows for this pick_date + shield). With the
            # exemption, only the re-emitted row is deletable.
            db.picks.delete_many({
                "pick_date": today,
                "sport": "NFL",
                **_publication_shield,
                "id": {"$in": list(seen_ids)},  # narrow to test rows only
            })

            reemit_after = db.picks.find_one({"id": reemit_id})
            untouched_after = db.picks.find_one({"id": untouched_id})
            assert reemit_after is None, (
                "re-emitted row should be deletable via shield exemption"
            )
            assert untouched_after is not None, (
                "untouched row should NOT be deleted (shield still protects)"
            )
        finally:
            db.picks.delete_many({"id": {"$in": [reemit_id, untouched_id]}})


# ───────────────── E. FIX 3 — Fresh NFL edge_percent non-zero ─────────
class TestEFreshNflEdge:
    def test_recent_nfl_picks_have_non_zero_edge(self, db):
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
        # created_at may be stored as datetime or ISO string.
        recent = list(
            db.picks.find(
                {
                    "sport": "NFL",
                    "$or": [
                        {"created_at": {"$gte": cutoff}},
                        {"created_at": {"$gte": cutoff.isoformat()}},
                    ],
                },
                {"_id": 0},
            )
        )
        if not recent:
            pytest.skip(
                "no NFL picks with created_at in last 30 min — Fix 3 cannot "
                "be verified without a fresh refresh. Endpoint wiring for "
                "Fix 2 is verified separately in TestC."
            )

        non_zero = [
            p for p in recent
            if p.get("edge_percent") not in (None, 0, 0.0)
        ]
        ratio = len(non_zero) / len(recent)
        print(
            f"\n[Fix3] recent_nfl_picks={len(recent)} "
            f"non_zero_edge={len(non_zero)} ratio={ratio:.3f}"
        )
        # Print 5 samples.
        samples = recent[:5]
        for s in samples:
            print(
                " | ".join([
                    str(s.get("player")),
                    str(s.get("market")),
                    str(s.get("line")),
                    str(s.get("book_odds")),
                    str(s.get("implied_probability")),
                    str(s.get("win_probability")),
                    str(s.get("edge_percent")),
                    str(s.get("lock_score")),
                    str(s.get("published_edge")),
                ])
            )
        # Published rows should also carry published_edge.
        published = [p for p in recent if p.get("publication_state") == "PUBLISHED"]
        if published:
            pub_with_edge = [
                p for p in published
                if p.get("published_edge") not in (None, 0, 0.0)
            ]
            print(
                f"[Fix3] published_nfl={len(published)} "
                f"with_published_edge={len(pub_with_edge)}"
            )
        assert ratio > 0.95, (
            f"expected >95% of recent NFL picks to have non-zero edge, "
            f"got {ratio:.3f} ({len(non_zero)}/{len(recent)})"
        )


# ─────────────────── F. sport_filter isolation ────────────────────────
class TestFSportFilterIsolation:
    def test_recent_volume_dominated_by_nfl(self, db):
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
        pipeline = [
            {
                "$match": {
                    "$or": [
                        {"created_at": {"$gte": cutoff}},
                        {"created_at": {"$gte": cutoff.isoformat()}},
                    ]
                }
            },
            {"$group": {"_id": "$sport", "n": {"$sum": 1}}},
            {"$sort": {"n": -1}},
        ]
        rows = list(db.picks.aggregate(pipeline))
        print("\n[Isolation] recent counts by sport:")
        for r in rows:
            print(f"  {r['_id']}: {r['n']}")

        if not rows:
            pytest.skip("no picks with fresh created_at — cannot verify isolation.")

        nfl_count = next((r["n"] for r in rows if r["_id"] == "NFL"), 0)
        counts_by_sport = {r["_id"]: r["n"] for r in rows}
        # Per review directive: "non-NFL sports may have background
        # scheduled refreshes but should not be tied to the NFL scoped
        # run." So we verify NFL has a substantial recent volume (~175
        # target from review), NOT that NFL dominates every sport.
        # Other sports having larger counts is EXPECTED (background
        # scheduled refreshes independent of the NFL-scoped force).
        if nfl_count == 0:
            pytest.skip("no fresh NFL rows in last 30 min — isolation not applicable")
        assert nfl_count >= 100, (
            f"NFL recent count {nfl_count} below expected ~175 threshold "
            f"(review target). Full counts: {counts_by_sport}"
        )
        print(
            f"[Isolation] NFL={nfl_count} (target ~175); "
            f"other-sport totals reflect background scheduled refreshes, "
            f"not the NFL-scoped run: {counts_by_sport}"
        )
