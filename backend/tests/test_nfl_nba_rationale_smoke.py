"""Smoke test for NFL & NBA rationale builders against LIVE DB data.

Picks a well-known player (Dak Prescott, CJ McCollum) with real
season stats + game logs in Mongo and verifies:
  1. Dispatcher routes NFL / NBA correctly.
  2. Builders produce non-empty evidence for common markets.
  3. Builders gracefully return empty on unknown players.
"""
from __future__ import annotations

import asyncio
import os
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
load_dotenv()

from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402
from services import sport_rationale, nfl_rationale, nba_rationale  # noqa: E402


async def _db():
    c = AsyncIOMotorClient(os.environ["MONGO_URL"])
    return c[os.environ["DB_NAME"]]


async def test_nfl_passing_yards():
    db = await _db()
    pick = {"market": "Dak Prescott Passing Yards Over 249.5"}
    r = await nfl_rationale.build_nfl_rationale(db, pick, "Dak Prescott")
    print(f"  NFL Prescott pass-yds: evidence={r['evidence']}  concerns={r['concerns']}")
    # Should surface at least one bullet from either evidence or concerns
    total = len(r["evidence"]) + len(r["concerns"])
    assert total >= 1, f"expected ≥1 bullet, got {r}"


async def test_nfl_receiving_yards():
    db = await _db()
    # Pick a WR with realistic season stats
    pick = {"market": "Kendrick Bourne Receiving Yards Over 39.5"}
    r = await nfl_rationale.build_nfl_rationale(db, pick, "Kendrick Bourne")
    print(f"  NFL Bourne rec-yds: evidence={r['evidence']}  concerns={r['concerns']}")


async def test_nfl_anytime_td():
    db = await _db()
    pick = {"market": "Devin Duvernay Anytime Touchdown"}
    r = await nfl_rationale.build_nfl_rationale(db, pick, "Devin Duvernay")
    print(f"  NFL Duvernay ATD: evidence={r['evidence']}  concerns={r['concerns']}")


async def test_nba_pts():
    db = await _db()
    pick = {"market": "CJ McCollum Points Over 17.5"}
    r = await nba_rationale.build_nba_rationale(db, pick, "CJ McCollum")
    print(f"  NBA McCollum PTS: evidence={r['evidence']}  concerns={r['concerns']}")
    total = len(r["evidence"]) + len(r["concerns"])
    assert total >= 1, f"expected ≥1 bullet, got {r}"


async def test_nba_pra():
    db = await _db()
    pick = {"market": "CJ McCollum Points + Rebounds + Assists Over 24.5"}
    r = await nba_rationale.build_nba_rationale(db, pick, "CJ McCollum")
    print(f"  NBA McCollum PRA: evidence={r['evidence']}  concerns={r['concerns']}")


async def test_dispatcher_routes():
    db = await _db()
    # NFL
    r = await sport_rationale.build_sport_specific(
        db, {"market": "Dak Prescott Passing Yards Over 249.5"}, "nfl", "Dak Prescott"
    )
    print(f"  Dispatcher NFL: {len(r['evidence'])}ev/{len(r['concerns'])}con")
    assert len(r["evidence"]) + len(r["concerns"]) >= 1
    # NBA
    r = await sport_rationale.build_sport_specific(
        db, {"market": "CJ McCollum Points Over 17.5"}, "nba", "CJ McCollum"
    )
    print(f"  Dispatcher NBA: {len(r['evidence'])}ev/{len(r['concerns'])}con")
    assert len(r["evidence"]) + len(r["concerns"]) >= 1


async def test_unknown_player_graceful():
    db = await _db()
    r = await nfl_rationale.build_nfl_rationale(
        db, {"market": "Fake Player Passing Yards Over 250.5"}, "Nobody Xyzq"
    )
    print(f"  NFL unknown: {r}")
    assert r == {"evidence": [], "concerns": []}
    r = await nba_rationale.build_nba_rationale(
        db, {"market": "Fake Player Points Over 20.5"}, "Nobody Xyzq"
    )
    print(f"  NBA unknown: {r}")
    assert r == {"evidence": [], "concerns": []}


if __name__ == "__main__":
    async def run_all():
        failed = 0
        for name, fn in list(globals().items()):
            if name.startswith("test_") and asyncio.iscoroutinefunction(fn):
                try:
                    print(f"▶ {name}")
                    await fn()
                except Exception as e:
                    failed += 1
                    print(f"  ✗ {name}: {e}")
                    traceback.print_exc()
        print(f"\n{'PASS' if failed == 0 else 'FAIL'} — {failed} failures")
        return failed
    rc = asyncio.run(run_all())
    sys.exit(1 if rc else 0)
