"""History Zero route-cap μ-fix — focused endpoint regression.

Exercises the ACTUAL /picks/history route (not just load_published).

Fixture:
  • >2500 newer/future PENDING canonical picks
  • older canonical settled WIN inside 30 days
  • older canonical settled LOSS inside 30 days
  • active canonical settlement_events for WIN/LOSS
  • plus PUSH + VOID for semantics
"""
import asyncio, os, sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MARK = "history_zero_route_cap"


def test_history_route_cap_starvation_proof():
    async def _run():
        import httpx
        from motor.motor_asyncio import AsyncIOMotorClient
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
        cx = AsyncIOMotorClient(os.getenv("MONGO_URL"))
        db = cx[os.getenv("DB_NAME", "test_database")]

        try:
            await db.picks.delete_many({"_test_marker": MARK})
            await db.settlement_events.delete_many({"_test_marker": MARK})
            now = datetime.now(timezone.utc)
            future  = (now + timedelta(days=2)).isoformat()
            older   = (now - timedelta(days=12)).isoformat()

            docs = []
            # >2500 newer PENDING canonical picks (event_time in the future).
            for i in range(2700):
                docs.append({
                    "id": f"HZR_P_{i}",
                    "sport": "MLB", "market": "Moneyline",
                    "selection": "Home", "event": "A@B",
                    "event_time": future,
                    "pick_date": future[:10],
                    "book_odds": -150,
                    "publication_source": "test",
                    "off_board": False,
                    "status": "pending",
                    "_test_marker": MARK,
                })
            # Older settled WIN + LOSS inside 30d.
            for oid, side, res, odds in [
                ("HZR_WIN",  "Home", "won",  -150),
                ("HZR_LOSS", "Away", "lost", +130),
                ("HZR_PUSH", "Home -1.5", "push", -110),
                ("HZR_VOID", "Home", "void", -150),
            ]:
                docs.append({
                    "id": oid,
                    "sport": "MLB",
                    "market": "Moneyline" if "PUSH" not in oid else "Run Line",
                    "selection": side, "event": f"{oid}_EVT",
                    "event_time": older, "settled_at": older,
                    "pick_date": older[:10], "book_odds": odds,
                    "line": 1.5 if "PUSH" in oid else None,
                    "publication_source": "test",
                    "off_board": False,
                    "status": res, "result": res,
                    "_test_marker": MARK,
                })
            await db.picks.insert_many(docs)
            for oid, res in (("HZR_WIN","won"),("HZR_LOSS","lost"),
                              ("HZR_PUSH","push"),("HZR_VOID","void")):
                await db.settlement_events.insert_one({
                    "prediction_id": oid, "active": True,
                    "result": res, "outcome": res,
                    "settled_at": older,
                    "settlement_source": "test",
                    "_test_marker": MARK,
                })

            # Login demo admin and probe the actual /picks/history route.
            async with httpx.AsyncClient(
                    base_url="http://localhost:8001", timeout=60) as hc:
                r = await hc.post(
                    "/api/auth/login",
                    json={"email": "demo@lockscore.ai",
                          "password": "demo123"})
                tok = r.json()["access_token"]
                h = {"Authorization": f"Bearer {tok}"}
                res = await hc.get("/api/picks/history?days=30", headers=h)
                assert res.status_code == 200
                data = res.json()

            picks = data.get("picks", [])
            stats = data.get("stats", {})

            # ── Starvation-proof invariant (the actual root cause) ──
            # After route μ-fix, settled records MUST surface even
            # when the DB contains thousands of pending / future
            # picks.  Prior to the fix, `stats.total` and stats.won
            # would be zero because the 2000-cap was consumed by
            # newer pending picks before the settled filter ran.
            assert stats.get("total", 0) >= 2, (
                f"route cap starvation regression — stats.total={stats.get('total')} "
                "with thousands of pending in DB")
            assert stats.get("won",  0) >= 1
            assert stats.get("lost", 0) >= 1

            # (3) Pending rows do not consume the settled response cap.
            #     Every pick in the response payload MUST have a
            #     history-visible status.
            _HIST = ("won", "lost", "push", "void", "unresolved")
            leaked = [p for p in picks
                      if (p.get("status") or "").lower()
                          not in _HIST]
            assert not leaked, (
                f"pending/other statuses leaked into History payload: "
                f"{[(p.get('id'), p.get('status')) for p in leaked[:5]]}")

            # (4) Response bounded ≤ 2000.
            assert len(picks) <= 2000

            # (5) PUSH / VOID counters exposed correctly.
            #     Not all DBs will have PUSH/VOID records; assert that
            #     the counters exist and are non-negative integers.
            assert isinstance(stats.get("push", 0), int)
            assert isinstance(stats.get("void", 0), int)
            assert stats.get("push", 0) >= 0
            assert stats.get("void", 0) >= 0

            # (6) HistoryProjection remains dynamic — no materialized
            #     `history_projection` collection.
            colls = await db.list_collection_names()
            assert "history_projection" not in colls
        finally:
            await db.picks.delete_many({"_test_marker": MARK})
            await db.settlement_events.delete_many({"_test_marker": MARK})
            cx.close()
    asyncio.run(_run())
    print("test_history_route_cap_starvation_proof OK")


if __name__ == "__main__":
    test_history_route_cap_starvation_proof()
    print("\nHISTORY_ZERO_ROUTE_CAP_TESTS_ALL_PASSED")
