"""
Iteration 82 — line-specific contradiction reconciler.

Focus (per review request): the reconciler must ONLY tag no_bet=True
for Over/Under pairs on the SAME numeric line. Over 7.5 vs Under 8.5
are NOT contradictions (both can win at K=8).

Tests:
  1. Different lines (Over 7.5 edge 13.77, Under 8.5 edge 16.6) — NEITHER
     is tagged no_bet=True.
  2. Same line (Over 6.5 vs Under 6.5) — the lower-edge side is tagged.
  3. Multi-line: (Over 6.5 + Under 6.5 + Over 7.5 + Under 8.5) — only
     the same-line 6.5 pair triggers no_bet; Over 7.5 & Under 8.5 survive.
  4. no_bet_reason format includes the numeric line.
  5. Line-less markets (e.g. Anytime Goal Scorer) are skipped safely.
  6. Regression: /api/picks/today?sport=MLB responds 200 with no
     duplicate SAME-line Over+Under contradictions.
"""
from __future__ import annotations

import asyncio
import os
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


# ─────────────── Helpers ───────────────
def _get_db():
    """Return (client, db) bound to the CURRENT asyncio event loop and
    also rebind `server.db` so the reconciler queries the same DB from
    the same loop. Without this rebind, `server.db` (created at module
    import) stays bound to the first loop and later `asyncio.run()`
    invocations trip 'Event loop is closed' in motor."""
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
    import asyncio as _asyncio
    loop = _asyncio.get_event_loop()
    client = AsyncIOMotorClient(mongo_url, io_loop=loop)
    db = client[db_name]
    # Rebind server.db so reconciler uses this loop's client
    import server as _server
    _server.db = db
    _server.client = client
    return client, db


# ─────── PART A — Different lines are NOT reconciled (bug fix) ───────
class TestDifferentLinesNotContradiction:
    """Skubal-style scenario: Over 7.5 + Under 8.5 are DIFFERENT lines,
    both can win at K=8. Reconciler must NOT tag either.
    """

    def test_over_7_5_vs_under_8_5_both_survive(self):
        asyncio.run(self._run())

    async def _run(self):
        client, db = _get_db()
        import server  # noqa
        from server import _reconcile_player_prop_contradictions

        # Bind reconciler's `db` module-global to the same live DB so the
        # collection it queries matches where we seed. (server.db already
        # points to this DB; we still use the same URL.)
        now_iso = datetime.now(timezone.utc).isoformat()
        event = f"TEST_iter82 @ EVT {uuid.uuid4().hex[:6]}"
        player = "TEST_iter82 Skubal (DET)"
        date_str = "TEST_iter82_diff_lines"

        over_id = f"TEST_iter82_diffL_over_{uuid.uuid4().hex[:8]}"
        under_id = f"TEST_iter82_diffL_under_{uuid.uuid4().hex[:8]}"

        seed = [
            {
                "id": over_id,
                "pick_date": date_str,
                "sport": "MLB",
                "event": event,
                "selection": player,
                "market": f"{player} Over 7.5 Strikeouts",
                "book_odds": -110,
                "edge_percent": 13.77,
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
                "market": f"{player} Under 8.5 Strikeouts",
                "book_odds": -110,
                "edge_percent": 16.6,
                "lock_score": 92,
                "created_at": now_iso,
                "no_bet": False,
            },
        ]
        try:
            await server.db.picks.insert_many(seed)
            await _reconcile_player_prop_contradictions(seed, date_str)

            over_doc = await server.db.picks.find_one({"id": over_id})
            under_doc = await server.db.picks.find_one({"id": under_id})
            assert over_doc and under_doc, "Seed docs missing"

            assert over_doc.get("no_bet") is not True, (
                f"Over 7.5 was incorrectly tagged no_bet=True (different line "
                f"from Under 8.5). reason={over_doc.get('no_bet_reason')!r}"
            )
            assert under_doc.get("no_bet") is not True, (
                f"Under 8.5 was incorrectly tagged no_bet=True. "
                f"reason={under_doc.get('no_bet_reason')!r}"
            )
        finally:
            await server.db.picks.delete_many(
                {"id": {"$in": [over_id, under_id]}}
            )
            client.close()


# ─────── PART B — Same line IS reconciled (regression) ───────
class TestSameLineIsContradiction:
    def test_over_6_5_vs_under_6_5_lower_edge_tagged(self):
        asyncio.run(self._run())

    async def _run(self):
        client, db = _get_db()
        import server
        from server import _reconcile_player_prop_contradictions

        now_iso = datetime.now(timezone.utc).isoformat()
        event = f"TEST_iter82 @ SAME {uuid.uuid4().hex[:6]}"
        player = "TEST_iter82 Same Line Pitcher"
        date_str = "TEST_iter82_same_line"

        over_id = f"TEST_iter82_sameL_over_{uuid.uuid4().hex[:8]}"
        under_id = f"TEST_iter82_sameL_under_{uuid.uuid4().hex[:8]}"

        seed = [
            {
                "id": over_id, "pick_date": date_str, "sport": "MLB",
                "event": event, "selection": player,
                "market": f"{player} Over 6.5 Strikeouts",
                "book_odds": -110, "edge_percent": 2.5, "lock_score": 80,
                "created_at": now_iso, "no_bet": False,
            },
            {
                "id": under_id, "pick_date": date_str, "sport": "MLB",
                "event": event, "selection": player,
                "market": f"{player} Under 6.5 Strikeouts",
                "book_odds": -110, "edge_percent": 9.9, "lock_score": 88,
                "created_at": now_iso, "no_bet": False,
            },
        ]
        try:
            await server.db.picks.insert_many(seed)
            await _reconcile_player_prop_contradictions(seed, date_str)

            over_doc = await server.db.picks.find_one({"id": over_id})
            under_doc = await server.db.picks.find_one({"id": under_id})
            assert over_doc.get("no_bet") is True, (
                f"Loser (Over 6.5, lower edge) should be tagged. "
                f"got no_bet={over_doc.get('no_bet')!r}"
            )
            assert under_doc.get("no_bet") is not True, (
                f"Winner (Under 6.5, higher edge) should survive."
            )
            reason = (over_doc.get("no_bet_reason") or "").lower()
            # PART D — no_bet_reason format includes numeric line
            assert "6.5" in reason, (
                f"no_bet_reason should mention numeric line 6.5. got={reason!r}"
            )
            assert "under" in reason, (
                f"no_bet_reason should reference winner side 'under'. got={reason!r}"
            )
            assert "mlb_k" in reason, (
                f"no_bet_reason should include family MLB_K. got={reason!r}"
            )
        finally:
            await server.db.picks.delete_many(
                {"id": {"$in": [over_id, under_id]}}
            )
            client.close()


# ─────── PART C — Multi-line: only same-line pair reconciled ───────
class TestMultiLineOnlySamePairTagged:
    def test_multi_line_only_matching_pair(self):
        asyncio.run(self._run())

    async def _run(self):
        client, db = _get_db()
        import server
        from server import _reconcile_player_prop_contradictions

        now_iso = datetime.now(timezone.utc).isoformat()
        event = f"TEST_iter82 @ MULTI {uuid.uuid4().hex[:6]}"
        player = "TEST_iter82 Multi Line Pitcher"
        date_str = "TEST_iter82_multi_line"

        ids = {
            "over_6_5":  f"TEST_iter82_ml_o65_{uuid.uuid4().hex[:8]}",
            "under_6_5": f"TEST_iter82_ml_u65_{uuid.uuid4().hex[:8]}",
            "over_7_5":  f"TEST_iter82_ml_o75_{uuid.uuid4().hex[:8]}",
            "under_8_5": f"TEST_iter82_ml_u85_{uuid.uuid4().hex[:8]}",
        }

        def _row(pid, mkt, edge, lock):
            return {
                "id": pid, "pick_date": date_str, "sport": "MLB",
                "event": event, "selection": player,
                "market": f"{player} {mkt}",
                "book_odds": -110, "edge_percent": edge, "lock_score": lock,
                "created_at": now_iso, "no_bet": False,
            }

        seed = [
            _row(ids["over_6_5"],  "Over 6.5 Strikeouts",  2.0, 80),   # SAME-LINE LOSER
            _row(ids["under_6_5"], "Under 6.5 Strikeouts", 8.0, 88),   # SAME-LINE WINNER
            _row(ids["over_7_5"],  "Over 7.5 Strikeouts", 13.77, 90),  # DIFF LINE — survives
            _row(ids["under_8_5"], "Under 8.5 Strikeouts", 16.6, 92),  # DIFF LINE — survives
        ]
        try:
            await server.db.picks.insert_many(seed)
            await _reconcile_player_prop_contradictions(seed, date_str)

            docs = {}
            async for d in server.db.picks.find(
                {"id": {"$in": list(ids.values())}}
            ):
                docs[d["id"]] = d

            # Same-line pair: Over 6.5 tagged, Under 6.5 kept
            assert docs[ids["over_6_5"]].get("no_bet") is True, (
                "Over 6.5 (lower edge, SAME line as Under 6.5) must be tagged."
            )
            assert docs[ids["under_6_5"]].get("no_bet") is not True, (
                "Under 6.5 (winner of the 6.5 pair) must survive."
            )
            # Different-line pair: both survive
            assert docs[ids["over_7_5"]].get("no_bet") is not True, (
                f"Over 7.5 must survive (different line from Under 8.5). "
                f"reason={docs[ids['over_7_5']].get('no_bet_reason')!r}"
            )
            assert docs[ids["under_8_5"]].get("no_bet") is not True, (
                f"Under 8.5 must survive (different line from Over 7.5). "
                f"reason={docs[ids['under_8_5']].get('no_bet_reason')!r}"
            )
            # no_bet_reason for 6.5 loser must include '6.5' NOT '7.5' or '8.5'
            reason = (docs[ids["over_6_5"]].get("no_bet_reason") or "").lower()
            assert "6.5" in reason, f"reason must include 6.5, got={reason!r}"
            assert "7.5" not in reason and "8.5" not in reason, (
                f"reason must reference the SAME line only. got={reason!r}"
            )
        finally:
            await server.db.picks.delete_many({"id": {"$in": list(ids.values())}})
            client.close()


# ─────── PART E — Line-less markets skipped safely ───────
class TestLineLessMarketsSafe:
    """Markets without a numeric line (e.g. 'Anytime Goal Scorer') must
    not raise and must NOT be grouped or tagged."""

    def test_no_raise_and_no_tag_on_lineless(self):
        asyncio.run(self._run())

    async def _run(self):
        client, db = _get_db()
        import server
        from server import _reconcile_player_prop_contradictions

        now_iso = datetime.now(timezone.utc).isoformat()
        # Use a MLB market ("Home Run") which has family MLB_HR but no
        # numeric line in the label — must be skipped safely.
        event = f"TEST_iter82 @ LINELESS {uuid.uuid4().hex[:6]}"
        player = "TEST_iter82 Lineless Player"
        date_str = "TEST_iter82_lineless"

        # Use a market label that DOES contain "Over" but no numeric line
        # after it — the extractor regex must return "" and the row is
        # skipped, not crashed.
        over_id = f"TEST_iter82_ll_o_{uuid.uuid4().hex[:8]}"
        under_id = f"TEST_iter82_ll_u_{uuid.uuid4().hex[:8]}"
        seed = [
            {
                "id": over_id, "pick_date": date_str, "sport": "MLB",
                "event": event, "selection": player,
                # "Over" appears but no numeric line following it
                "market": f"{player} Over Home Run",
                "book_odds": -110, "edge_percent": 5.0, "lock_score": 80,
                "created_at": now_iso, "no_bet": False,
            },
            {
                "id": under_id, "pick_date": date_str, "sport": "MLB",
                "event": event, "selection": player,
                "market": f"{player} Under Home Run",
                "book_odds": -110, "edge_percent": 7.0, "lock_score": 82,
                "created_at": now_iso, "no_bet": False,
            },
        ]
        try:
            await server.db.picks.insert_many(seed)
            # Should complete without raising
            await _reconcile_player_prop_contradictions(seed, date_str)
            over_doc = await server.db.picks.find_one({"id": over_id})
            under_doc = await server.db.picks.find_one({"id": under_id})
            assert over_doc and under_doc
            # Neither should be tagged since line is missing
            assert over_doc.get("no_bet") is not True, (
                f"Lineless over should be skipped (no numeric line). "
                f"reason={over_doc.get('no_bet_reason')!r}"
            )
            assert under_doc.get("no_bet") is not True, (
                f"Lineless under should be skipped. "
                f"reason={under_doc.get('no_bet_reason')!r}"
            )
        finally:
            await server.db.picks.delete_many({"id": {"$in": [over_id, under_id]}})
            client.close()


# ─────── PART F — Regression: /api/picks/today MLB works & no same-line dupes ───────
class TestMLBEndpointRegression:
    def test_mlb_endpoint_200(self, api_client, auth_token):
        r = api_client.get(
            f"{BASE_URL}/api/picks/today",
            headers=_auth_headers(auth_token),
            params={"sport": "MLB"},
            timeout=60,
        )
        assert r.status_code == 200, f"MLB picks: {r.status_code} {r.text[:300]}"
        body = r.json()
        assert isinstance(body, dict) and "picks" in body

    def test_no_same_line_over_under_contradictions_live(self, api_client, auth_token):
        """The LIVE /api/picks/today?sport=MLB must not include a Over N +
        Under N pair on the SAME numeric line for the same (event, player)."""
        import re as _re
        _line_re = _re.compile(r"(?i)(?:over|under)\s+(-?\d+(?:\.\d+)?)")
        r = api_client.get(
            f"{BASE_URL}/api/picks/today",
            headers=_auth_headers(auth_token),
            params={"sport": "MLB"},
            timeout=60,
        )
        assert r.status_code == 200
        picks = r.json().get("picks", [])
        # Group by (event, player, line) → set of sides
        groups: dict[tuple, set] = {}
        for p in picks:
            m = (p.get("market") or "")
            ml = m.lower()
            if "strikeout" not in ml:
                continue
            side = None
            if " over " in ml or ml.startswith("over "):
                side = "over"
            elif " under " in ml or ml.startswith("under "):
                side = "under"
            if not side:
                continue
            match = _line_re.search(m)
            if not match:
                continue
            line = match.group(1)
            key = (
                p.get("event") or "",
                (p.get("selection") or "").strip(),
                line,
            )
            groups.setdefault(key, set()).add(side)
        contradicting = {k: v for k, v in groups.items()
                         if {"over", "under"}.issubset(v)}
        if contradicting:
            for k in list(contradicting)[:5]:
                print(f"SAME-LINE CONTRADICTION: {k}")
        assert not contradicting, (
            f"Found {len(contradicting)} SAME-LINE Over+Under K contradictions"
        )
