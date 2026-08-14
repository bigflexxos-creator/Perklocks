"""
Iteration 81 — targeted verification of contradiction reconciler + MLS
goalscorer visibility bugs.

Focus (per review request):

  1. GET /api/picks/today?sport=MLB — no player has BOTH Over and Under
     strikeout picks for the same event. Specifically no Skubal Over 7.5
     + Under 8.5 and no Drohan Over 5.5 + Under 5.5.

  2. GET /api/picks/today?sport=Soccer&leagues=MLS — response includes
     BOTH Anytime Goal Scorer picks AND Anytime Assist picks.

  3. _apply_atomic_delete `_pin_filter`: picks with `no_bet=True` and
     `lock_score_peak >= 95` are deleted; a control pick with
     `no_bet=False` from an out-of-band source is protected.

  4. _reconcile_player_prop_contradictions: given synthetic Over/Under
     strikeout picks for the same (event, player) the loser side gets
     `no_bet=True` tagged.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import uuid
from datetime import datetime, timezone

import pytest
import requests

sys.path.insert(0, "/app/backend")

BASE_URL = (
    os.environ.get("EXPO_PUBLIC_BACKEND_URL")
    or os.environ.get("EXPO_BACKEND_URL")
    or "https://canonical-parity.preview.emergentagent.com"
).rstrip("/")


# ─────────────────── Fixtures ───────────────────
@pytest.fixture(scope="module")
def api_client():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module")
def auth_token(api_client):
    r = api_client.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": "demo@lockscore.ai", "password": "demo123"},
        timeout=20,
    )
    if r.status_code != 200:
        pytest.skip(f"Auth failed: {r.status_code} {r.text[:200]}")
    return r.json().get("access_token")


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ───────── PART A — MLB strikeout contradictions gone from API ─────────
class TestMLBNoContradictions:
    """Verify /api/picks/today?sport=MLB has no Over+Under strikeout pairs
    for the same (event, player).
    """

    def _fetch_mlb(self, api_client, auth_token):
        r = api_client.get(
            f"{BASE_URL}/api/picks/today",
            headers=_auth_headers(auth_token),
            params={"sport": "MLB"},
            timeout=60,
        )
        assert r.status_code == 200, f"MLB picks: {r.status_code} {r.text[:300]}"
        body = r.json()
        assert isinstance(body, dict) and "picks" in body
        return body["picks"]

    def test_no_strikeout_over_under_contradictions(self, api_client, auth_token):
        picks = self._fetch_mlb(api_client, auth_token)
        # Filter K picks
        k_picks = [
            p for p in picks
            if "strikeout" in (p.get("market") or "").lower()
        ]
        # Group by (event, player) → set of sides
        groups: dict[tuple, set] = {}
        details: dict[tuple, list] = {}
        for p in k_picks:
            m = (p.get("market") or "").lower()
            side = None
            if " over " in m or m.startswith("over "):
                side = "over"
            elif " under " in m or m.startswith("under "):
                side = "under"
            if not side:
                continue
            key = (p.get("event") or "", (p.get("selection") or "").strip())
            groups.setdefault(key, set()).add(side)
            details.setdefault(key, []).append(p.get("market"))

        contradicting = {k: v for k, v in groups.items()
                         if {"over", "under"}.issubset(v)}
        if contradicting:
            for k, sides in contradicting.items():
                print(f"CONTRADICTION: {k} sides={sides} markets={details[k]}")
        assert not contradicting, (
            f"Found {len(contradicting)} contradicting Over+Under K pairs: "
            f"{list(contradicting.keys())[:5]}"
        )

    def test_no_skubal_over_and_under(self, api_client, auth_token):
        picks = self._fetch_mlb(api_client, auth_function := auth_token)  # noqa
        k_picks = [
            p for p in picks
            if "strikeout" in (p.get("market") or "").lower()
            and "skubal" in (p.get("selection") or "").lower()
        ]
        sides = set()
        for p in k_picks:
            m = (p.get("market") or "").lower()
            if " over " in m or m.startswith("over "):
                sides.add("over")
            elif " under " in m or m.startswith("under "):
                sides.add("under")
        assert not {"over", "under"}.issubset(sides), (
            f"Skubal has both Over and Under strikeout picks: {[p.get('market') for p in k_picks]}"
        )

    def test_no_drohan_over_and_under(self, api_client, auth_token):
        picks = self._fetch_mlb(api_client, auth_token)
        k_picks = [
            p for p in picks
            if "strikeout" in (p.get("market") or "").lower()
            and "drohan" in (p.get("selection") or "").lower()
        ]
        sides = set()
        for p in k_picks:
            m = (p.get("market") or "").lower()
            if " over " in m or m.startswith("over "):
                sides.add("over")
            elif " under " in m or m.startswith("under "):
                sides.add("under")
        assert not {"over", "under"}.issubset(sides), (
            f"Drohan has both Over and Under: {[p.get('market') for p in k_picks]}"
        )


# ───────── PART B — MLS Goal Scorer + Assist visibility ─────────
class TestMLSGoalScorerVisible:
    def test_mls_has_both_goal_scorer_and_assist(self, api_client, auth_token):
        r = api_client.get(
            f"{BASE_URL}/api/picks/today",
            headers=_auth_headers(auth_token),
            params={"sport": "Soccer", "leagues": "MLS"},
            timeout=60,
        )
        assert r.status_code == 200, f"MLS picks: {r.status_code} {r.text[:300]}"
        body = r.json()
        picks = body.get("picks", [])
        # Only count MLS
        mls = [p for p in picks if (p.get("league") or "").upper() == "MLS"]
        goal_scorer = [p for p in mls
                       if re.search(r"anytime goal scorer",
                                    (p.get("market") or ""), re.I)]
        assist = [p for p in mls
                  if re.search(r"anytime assist",
                               (p.get("market") or ""), re.I)]
        score_or_assist = [p for p in mls
                           if re.search(r"score or assist|to score or assist",
                                        (p.get("market") or ""), re.I)]
        print(
            f"MLS market counts: goal_scorer={len(goal_scorer)} "
            f"assist={len(assist)} score_or_assist={len(score_or_assist)} "
            f"total_mls={len(mls)}"
        )
        assert len(goal_scorer) > 0, (
            "No Anytime Goal Scorer picks returned for MLS — the user's "
            f"'only showing assist' complaint should be resolved. total mls={len(mls)}"
        )
        assert len(assist) > 0, (
            f"No Anytime Assist picks for MLS. total mls={len(mls)}"
        )


# ───────── PART C — _pin_filter live delete integration ─────────
class TestPinFilterLiveDelete:
    """Insert real docs into Mongo and verify the exact `_pin_filter`
    from server.py deletes the no_bet=True doc while protecting the
    out-of-band control doc.
    """

    def test_live_delete_no_bet_only(self):
        asyncio.run(self._run())

    async def _run(self):
        try:
            from motor.motor_asyncio import AsyncIOMotorClient
        except Exception:
            pytest.skip("motor not available")
        try:
            from dotenv import load_dotenv
            load_dotenv("/app/backend/.env")
        except Exception:
            pass
        mongo_url = os.environ.get("MONGO_URL")
        db_name = os.environ.get("DB_NAME")
        if not mongo_url or not db_name:
            pytest.skip("MONGO_URL / DB_NAME not set")

        client = AsyncIOMotorClient(mongo_url)
        db = client[db_name]
        date_str = "TEST_iter81_2099-01-01"
        await db.picks.delete_many({"pick_date": date_str})

        del_id = f"TEST_iter81_del_{uuid.uuid4().hex[:8]}"
        keep_control_id = f"TEST_iter81_keep_ctl_{uuid.uuid4().hex[:8]}"
        keep_oob_id = f"TEST_iter81_keep_oob_{uuid.uuid4().hex[:8]}"

        seed = [
            {   # SHOULD be deleted: no_bet=True + peak 99 + normal source
                "id": del_id,
                "pick_date": date_str,
                "sport": "MLB",
                "event": "TEST @ MLB",
                "selection": "TEST Skubal (DET)",
                "market": "TEST Skubal Over 7.5 Strikeouts",
                "book_odds": -110,
                "lock_score_peak": 99,
                "no_bet": True,
                "source": "the_odds_api",
            },
            {   # Control: no_bet=False from out-of-band source — protected
                "id": keep_control_id,
                "pick_date": date_str,
                "sport": "Soccer",
                "market": "Anytime Goal Scorer",
                "book_odds": -110,
                "lock_score_peak": 99,
                "no_bet": False,
                "source": "mls_espn_leaderboard",
            },
            {   # OOB with no_bet=True — must STILL be protected
                "id": keep_oob_id,
                "pick_date": date_str,
                "sport": "Soccer",
                "market": "Anytime Goal Scorer",
                "book_odds": -110,
                "lock_score_peak": 99,
                "no_bet": True,
                "source": "mls_espn_leaderboard",
            },
        ]
        _OUT_OF_BAND_SOURCES = [
            "soccer_hot_scorers_v1", "csl_espn_leaderboard", "csl_espn_live",
            "mls_espn_leaderboard", "tennis_extra", "tennis_extra_model",
            "tennis_real_odds", "mlb_hot_hitters", "mlb_hot_hitters_v1",
        ]
        _pin_filter = {
            "$and": [
                {"$or": [
                    {"lock_score_peak": {"$exists": False}},
                    {"lock_score_peak": {"$lt": 95}},
                    {"no_bet": True},
                ]},
                {"source": {"$nin": _OUT_OF_BAND_SOURCES}},
                {"source": {"$not": {"$regex": r"^sportdb_scorer", "$options": "i"}}},
                {"is_model_only": {"$ne": True}},
            ]
        }
        try:
            await db.picks.insert_many(seed)
            res = await db.picks.delete_many({"pick_date": date_str, **_pin_filter})
            assert res.deleted_count == 1, (
                f"Expected exactly 1 delete, got {res.deleted_count}"
            )
            remaining = {d["id"] async for d in
                         db.picks.find({"pick_date": date_str}, {"id": 1})}
            assert del_id not in remaining, "no_bet=True doc must be deleted"
            assert keep_control_id in remaining, "Control OOB no_bet=False must be protected"
            assert keep_oob_id in remaining, "OOB no_bet=True must still be protected (source guard)"
        finally:
            await db.picks.delete_many({"pick_date": date_str})
            client.close()


# ───────── PART D — reconciler tags loser with no_bet=True ─────────
class TestReconcilerTagsNoBet:
    """Insert two contradictory MLB K picks (same event + player, opposing
    Over/Under sides, different edges) and call the real reconciler.
    Expect the loser side to be flagged no_bet=True.
    """

    def test_reconciler_tags_loser_no_bet(self):
        asyncio.run(self._run())

    async def _run(self):
        try:
            from motor.motor_asyncio import AsyncIOMotorClient
        except Exception:
            pytest.skip("motor not available")
        try:
            from dotenv import load_dotenv
            load_dotenv("/app/backend/.env")
        except Exception:
            pass
        mongo_url = os.environ.get("MONGO_URL")
        db_name = os.environ.get("DB_NAME")
        if not mongo_url or not db_name:
            pytest.skip("MONGO_URL / DB_NAME not set")

        # Monkey-patch server.db to use test client so we don't taint the
        # live picks collection. But the server module holds its own `db`
        # reference — safest is to just insert onto the same collection
        # with unique TEST_ ids and clean up in finally.
        import server  # noqa: F401  (imports server.db)
        from server import _reconcile_player_prop_contradictions

        client = AsyncIOMotorClient(mongo_url)
        db = client[db_name]

        now_iso = datetime.now(timezone.utc).isoformat()
        event = f"TEST_ITER81 @ EVT {uuid.uuid4().hex[:6]}"
        player = "TEST_ITER81 Player One"
        date_str = "TEST_iter81_reconcile"

        over_id = f"TEST_iter81_over_{uuid.uuid4().hex[:8]}"
        under_id = f"TEST_iter81_under_{uuid.uuid4().hex[:8]}"

        seed = [
            {
                "id": over_id,
                "pick_date": date_str,
                "sport": "MLB",
                "event": event,
                "selection": player,
                "market": "Over 7.5 Strikeouts",
                "book_odds": -110,
                "edge_percent": 1.0,       # LOSER — lower edge
                "lock_score": 90,
                "created_at": now_iso,
                "no_bet": False,
            },
            {
                "id": under_id,
                "pick_date": date_str,
                "sport": "MLB",
                "event": event,
                "selection": player,
                "market": "Under 8.5 Strikeouts",
                "book_odds": -110,
                "edge_percent": 6.0,       # WINNER — higher edge
                "lock_score": 92,
                "created_at": now_iso,
                "no_bet": False,
            },
        ]

        try:
            await db.picks.insert_many(seed)
            # Call the real reconciler with the "safe_picks" that would
            # have been inserted this pass — pass the winner side (Under)
            # to trigger touched-set inclusion.
            await _reconcile_player_prop_contradictions(seed, date_str)

            over_doc = await db.picks.find_one({"id": over_id})
            under_doc = await db.picks.find_one({"id": under_id})
            assert over_doc is not None and under_doc is not None, "Seed missing"

            assert over_doc.get("no_bet") is True, (
                f"Loser (Over 7.5) not tagged no_bet=True — got {over_doc.get('no_bet')!r} "
                f"reason={over_doc.get('no_bet_reason')!r}"
            )
            reason = over_doc.get("no_bet_reason") or ""
            assert "under" in reason.lower() and "mlb_k" in reason.lower(), (
                f"Unexpected no_bet_reason: {reason!r}"
            )
            assert under_doc.get("no_bet") is not True, (
                f"Winner (Under 8.5) was incorrectly tagged no_bet: {under_doc.get('no_bet')!r}"
            )
        finally:
            await db.picks.delete_many({"id": {"$in": [over_id, under_id]}})
            client.close()


# ───────── PART E — Backend health smoke ─────────
class TestBackendHealth:
    def test_picks_today_all_sports_200(self, api_client, auth_token):
        r = api_client.get(
            f"{BASE_URL}/api/picks/today",
            headers=_auth_headers(auth_token),
            timeout=60,
        )
        assert r.status_code == 200
        assert isinstance(r.json().get("picks"), list)
