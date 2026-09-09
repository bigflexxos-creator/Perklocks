"""Regression guard for the stale player_game_logs unique index.

The pre-fix Production DB carried a 2-field UNIQUE index
`player_id_1_game_id_1` on `player_game_logs`, which crashed NFL
historical ingestion because NFL players legitimately have MULTIPLE
stat-block documents (passing / rushing / receiving) per
`(player_id, game_id)`.  The fix drops the stale index at startup
and installs a NON-UNIQUE 3-field composite (player_id, game_id,
stat_block).

This test proves the 3-field index still allows multi-stat-block
rows to coexist for the same (player_id, game_id) pair.
"""
from __future__ import annotations
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402


async def _asyncmain():
    c = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = c[os.environ.get("DB_NAME", "test_database") + "_pgl_index_probe"]

    coll = db.player_game_logs

    # Fresh probe — drop any previous state from earlier runs.
    await coll.drop()

    # Deliberately install the stale unique index that Production had.
    await coll.create_index(
        [("player_id", 1), ("game_id", 1)],
        unique=True, name="player_id_1_game_id_1",
    )

    # Attempting to insert two stat-block rows for one (player,game) must
    # fail while the stale index is present — this reproduces the bug.
    row_passing = {"player_id": "espn:1234", "game_id": "nfl:abc",
                    "stat_block": "passing", "yards": 285}
    row_rushing = {"player_id": "espn:1234", "game_id": "nfl:abc",
                    "stat_block": "rushing", "yards": 42}
    await coll.insert_one(dict(row_passing))
    try:
        await coll.insert_one(dict(row_rushing))
        pre_fix_reproduced = False
    except Exception:
        pre_fix_reproduced = True
    assert pre_fix_reproduced, (
        "expected stale unique index to reject the 2nd stat-block row — "
        "reproduction of the pre-fix bug failed"
    )
    print("[stale-index] pre-fix behaviour reproduced ✓ "
          "(unique index rejected 2nd stat-block row)")

    # Now apply the fix: drop stale, add correct non-unique 3-field index.
    await coll.drop_index("player_id_1_game_id_1")
    await coll.create_index(
        [("player_id", 1), ("game_id", 1), ("stat_block", 1)],
        name="player_id_1_game_id_1_stat_block_1",
    )
    ix_names = [ix.get("name") async for ix in coll.list_indexes()]
    assert "player_id_1_game_id_1" not in ix_names, (
        f"stale index still present after drop: {ix_names}"
    )
    assert "player_id_1_game_id_1_stat_block_1" in ix_names, (
        f"correct 3-field index missing: {ix_names}"
    )

    # Post-fix: multiple stat-block rows for one (player,game) must coexist.
    await coll.insert_one(dict(row_rushing))
    await coll.insert_one({"player_id": "espn:1234", "game_id": "nfl:abc",
                            "stat_block": "receiving", "yards": 18})
    n_pair = await coll.count_documents({
        "player_id": "espn:1234", "game_id": "nfl:abc",
    })
    assert n_pair == 3, f"expected 3 stat-block rows, got {n_pair}"
    print(f"[stale-index] post-fix: 3 stat-block rows coexist for the "
          f"same (player_id, game_id) ✓")

    # And a second player in a different game still works, of course.
    await coll.insert_one({"player_id": "espn:5678", "game_id": "nfl:abc",
                            "stat_block": "passing", "yards": 210})
    n_all = await coll.count_documents({})
    assert n_all == 4, f"expected 4 total rows, got {n_all}"

    # Cleanup — drop the probe DB so it doesn't linger.
    await c.drop_database(db.name)


def test_player_game_logs_stat_block_coexistence():
    asyncio.run(_asyncmain())


if __name__ == "__main__":
    test_player_game_logs_stat_block_coexistence()
    print("=" * 60)
    print("PLAYER_GAME_LOGS STALE-INDEX REGRESSION · PASS")
