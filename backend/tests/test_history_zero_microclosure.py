"""History Zero surgical μ-fix — focused regressions.

Scope: services/published_results_truth.load_published only.

Covers all 9 required behaviors:
  1. Seed >5000 newer pending/future canonical picks.
  2. Seed one older settled WIN and one older settled LOSS inside 30 days.
  3. /picks/history (via load_published) returns BOTH.
  4. Snapshot-backed legacy canonical settled pick must appear.
  5. Legacy pick with NO publication proof must remain excluded.
  6. 30-day window preserved.
  7. WIN/LOSS/PUSH/VOID semantics unchanged.
  8. HistoryProjection remains dynamic (no `history_projection` collection created).
  9. No unbounded DB read introduced (bounded 3000 + 2000 + 500 = 5500 max).
"""
import asyncio
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MARK = "history_zero_muclosure"


def test_load_published_starvation_and_snapshot_admission():
    """One test — end-to-end proof of both root-cause fixes."""
    async def _run():
        from motor.motor_asyncio import AsyncIOMotorClient
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
        cx = AsyncIOMotorClient(os.getenv("MONGO_URL"))
        db = cx[os.getenv("DB_NAME", "test_database")]

        try:
            # Cleanup any prior test rows.
            await db.picks.delete_many({"_test_marker": MARK})
            await db.prediction_snapshots.delete_many({"_test_marker": MARK})

            now = datetime.now(timezone.utc)
            future = (now + timedelta(days=2)).isoformat()   # newer
            older  = (now - timedelta(days=12)).isoformat()  # older, inside 30d
            legacy = (now - timedelta(days=20)).isoformat()  # inside 30d
            expired = (now - timedelta(days=45)).isoformat() # OUTSIDE 30d

            docs = []
            # (1) Seed >5000 newer pending/future canonical picks.
            for i in range(5200):
                docs.append({
                    "id": f"HZM_P_{i}",
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
            # (2) One older settled WIN + one older settled LOSS
            #     inside the 30-day window.  event_time is OLDER
            #     than every pending pick above so the OLD load
            #     path would sort them off the tail of 5000.
            docs.append({
                "id": "HZM_WIN_OLD",
                "sport": "MLB", "market": "Moneyline",
                "selection": "Home", "event": "OLD@GAME",
                "event_time": older,
                "settled_at": older,
                "pick_date": older[:10],
                "book_odds": -150,
                "publication_source": "test",
                "off_board": False,
                "status": "won", "result": "won",
                "_test_marker": MARK,
            })
            docs.append({
                "id": "HZM_LOSS_OLD",
                "sport": "MLB", "market": "Moneyline",
                "selection": "Away", "event": "OLD@GAME2",
                "event_time": older,
                "settled_at": older,
                "pick_date": older[:10],
                "book_odds": +130,
                "publication_source": "test",
                "off_board": False,
                "status": "lost", "result": "lost",
                "_test_marker": MARK,
            })
            # (4) Legacy pick — HAS a canonical prediction_snapshot,
            #     but LACKS newer publication_source / on_*_at.
            docs.append({
                "id": "HZM_SNAP_LEGACY",
                "sport": "MLB", "market": "Moneyline",
                "selection": "Home", "event": "SNAP@GAME",
                "event_time": legacy,
                "settled_at": legacy,
                "pick_date": legacy[:10],
                "book_odds": -110,
                # NOTE: no publication_source, no on_*_at, no published_at.
                "off_board": False,
                "status": "won", "result": "won",
                "_test_marker": MARK,
            })
            # (5) Legacy pick with NO publication proof at all
            #     (no snapshot, no publication_source).  Must be excluded.
            docs.append({
                "id": "HZM_ORPHAN_LEGACY",
                "sport": "MLB", "market": "Moneyline",
                "selection": "Home", "event": "ORPHAN@GAME",
                "event_time": legacy,
                "settled_at": legacy,
                "pick_date": legacy[:10],
                "book_odds": -110,
                "off_board": False,
                "status": "won", "result": "won",
                "_test_marker": MARK,
            })
            # PUSH + VOID for semantics (7)
            docs.append({
                "id": "HZM_PUSH", "sport": "MLB", "market": "Run Line",
                "selection": "Home -1.5", "event": "A@B",
                "event_time": older, "settled_at": older,
                "pick_date": older[:10], "book_odds": -110, "line": 1.5,
                "publication_source": "test", "off_board": False,
                "status": "push", "result": "push",
                "_test_marker": MARK,
            })
            docs.append({
                "id": "HZM_VOID", "sport": "MLB", "market": "Moneyline",
                "selection": "Home", "event": "C@D",
                "event_time": older, "settled_at": older,
                "pick_date": older[:10], "book_odds": -150,
                "publication_source": "test", "off_board": False,
                "status": "void", "result": "void",
                "_test_marker": MARK,
            })
            # (6) Outside 30-day window — must NOT appear.
            docs.append({
                "id": "HZM_EXPIRED", "sport": "MLB", "market": "Moneyline",
                "selection": "Home", "event": "OLD@GAME",
                "event_time": expired, "settled_at": expired,
                "pick_date": expired[:10], "book_odds": -150,
                "publication_source": "test", "off_board": False,
                "status": "won", "result": "won",
                "_test_marker": MARK,
            })

            await db.picks.insert_many(docs)
            # Seed the snapshot for HZM_SNAP_LEGACY only.
            await db.prediction_snapshots.insert_one({
                "pick_id":            "HZM_SNAP_LEGACY",
                "prediction_id":      "HZM_SNAP_LEGACY",
                "snapshot_created_at": legacy,
                "created_at":          legacy,
                "_test_marker":        MARK,
            })

            # Exercise the FIXED loader.
            from services.published_results_truth import (
                load_published, summarise)
            loaded = await load_published(db, days=30,
                                           exclude_ambiguous_legacy=True,
                                           include_pending=True)
            mine = {p.get("id"): p for p in loaded
                    if p.get("_test_marker") == MARK}

            # (3) BOTH older settled WIN and LOSS surface despite 5200
            #     newer pending picks.  This is the starvation fix.
            assert "HZM_WIN_OLD"  in mine, (
                "starvation regression — older settled WIN dropped")
            assert "HZM_LOSS_OLD" in mine, (
                "starvation regression — older settled LOSS dropped")

            # (4) Snapshot-backed legacy pick is admitted.
            assert "HZM_SNAP_LEGACY" in mine, (
                "snapshot-admission regression — legacy w/ snapshot excluded")
            assert mine["HZM_SNAP_LEGACY"].get("_has_prediction_snapshot") is True
            assert mine["HZM_SNAP_LEGACY"].get("_classification") == "PROVEN_PUBLISHED"

            # (5) Legacy WITHOUT publication proof stays excluded.
            assert "HZM_ORPHAN_LEGACY" not in mine, (
                "canonical leak — orphan legacy pick without publication " 
                "proof was incorrectly admitted")

            # (6) 30-day window preserved.
            assert "HZM_EXPIRED" not in mine, (
                "window regression — pick outside 30-day window admitted")

            # (7) WIN/LOSS/PUSH/VOID semantics preserved.
            assert mine["HZM_WIN_OLD"].get("status")  == "won"
            assert mine["HZM_LOSS_OLD"].get("status") == "lost"
            assert mine["HZM_PUSH"].get("status")     == "push"
            assert mine["HZM_VOID"].get("status")     == "void"
            summary = summarise(mine.values())
            assert summary["won"]  >= 2   # HZM_WIN_OLD + HZM_SNAP_LEGACY
            assert summary["lost"] >= 1
            assert summary["push"] >= 1
            assert summary["void"] >= 1

            # (8) HistoryProjection remains dynamic — no
            #     `history_projection` collection exists.
            colls = await db.list_collection_names()
            assert "history_projection" not in colls, (
                "spec violation — dynamic HistoryProjection replaced "
                "by a materialized collection")

            # (9) No unbounded read — total load <= 5500 (3000+2000+500).
            assert len(loaded) <= 5500, (
                f"bounded-read violation — load returned {len(loaded)} "
                "records (ceiling 5500)")

        finally:
            await db.picks.delete_many({"_test_marker": MARK})
            await db.prediction_snapshots.delete_many({"_test_marker": MARK})
            cx.close()
    asyncio.run(_run())
    print("test_load_published_starvation_and_snapshot_admission OK")


if __name__ == "__main__":
    test_load_published_starvation_and_snapshot_admission()
    print("\nHISTORY_ZERO_MUCLOSURE_TESTS_ALL_PASSED")
