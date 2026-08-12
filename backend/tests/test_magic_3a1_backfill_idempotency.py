"""MAGIC 3A.1 backfill idempotency + immutable-field proof.

Runs the actual backfill script against the live db.picks collection
and asserts:

  1. Zero IMMUTABLE_MISMATCH across all rows.
  2. Second run performs 0 new mutations
     (RECOVERED_TEXT_APPLIED == 0, NOT_RECOVERABLE_APPLIED == 0).
  3. Settlement truth (status, settled_at, units_profit, units_risked,
     original market/selection/book_odds/closing_odds/clv_value) on
     a random sample is byte-identical before and after.
"""
import asyncio
import os
import subprocess
import sys

sys.path.insert(0, "/app/backend")


IMMUTABLE = (
    "status", "settled_at", "units_profit", "units_risked",
    "market", "selection", "book_odds", "odds_source",
    "closing_odds", "clv_value",
)


async def _sample_settled(n: int = 20) -> list[dict]:
    from motor.motor_asyncio import AsyncIOMotorClient
    from dotenv import load_dotenv
    load_dotenv("/app/backend/.env")
    db = AsyncIOMotorClient(os.getenv("MONGO_URL"))["lockscore_db"]
    out = []
    async for p in db.picks.find(
        {"status": {"$in": ["won", "lost", "push", "void"]}},
        {f: 1 for f in IMMUTABLE + ("id",)},
    ).limit(n):
        out.append({k: p.get(k) for k in IMMUTABLE + ("id",)})
    return out


async def _fetch(pid: str) -> dict:
    from motor.motor_asyncio import AsyncIOMotorClient
    from dotenv import load_dotenv
    load_dotenv("/app/backend/.env")
    db = AsyncIOMotorClient(os.getenv("MONGO_URL"))["lockscore_db"]
    return await db.picks.find_one({"id": pid}, {f: 1 for f in IMMUTABLE})


async def _counts() -> dict:
    from motor.motor_asyncio import AsyncIOMotorClient
    from dotenv import load_dotenv
    load_dotenv("/app/backend/.env")
    db = AsyncIOMotorClient(os.getenv("MONGO_URL"))["lockscore_db"]
    return {
        "orphans": await db.picks.count_documents({
            "line": {"$type": "number"}, "line_source": None,
        }),
        "historical": await db.picks.count_documents({
            "line_source": "historical_selection_parse",
        }),
    }


def test_backfill_is_idempotent_and_preserves_settlement_truth():
    sample = asyncio.run(_sample_settled(20))
    assert sample, "no settled picks available"

    # First run — may pick up newly-created rows from live producers.
    subprocess.run(
        [sys.executable,
         "/app/backend/scripts/magic_3a1_backfill.py", "--write"],
        capture_output=True, text=True, timeout=180,
    )
    # Second run — MUST be a full no-op (idempotency).
    proc = subprocess.run(
        [sys.executable,
         "/app/backend/scripts/magic_3a1_backfill.py", "--write"],
        capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    assert "LINE_PIPELINE_READY" in proc.stdout, proc.stdout
    assert "IMMUTABLE_MISMATCH                         0" in proc.stdout

    # Parse the summary counters from the SECOND run.
    def _c(k: str) -> int:
        for line in proc.stdout.splitlines():
            if k in line:
                return int(line.split()[-1])
        raise AssertionError(f"missing counter {k}")

    assert _c("RECOVERED_TEXT_APPLIED") == 0, (
        f"non-idempotent — text applied: {_c('RECOVERED_TEXT_APPLIED')}"
    )
    assert _c("NOT_RECOVERABLE_APPLIED") == 0, (
        f"non-idempotent — unrec applied: "
        f"{_c('NOT_RECOVERABLE_APPLIED')}"
    )

    # Verify immutable fields on the sample.
    for orig in sample:
        current = asyncio.run(_fetch(orig["id"]))
        for f in IMMUTABLE:
            assert current.get(f) == orig.get(f), (
                f"immutable field {f} mutated on {orig['id']}: "
                f"{orig.get(f)!r} → {current.get(f)!r}"
            )


def test_historical_backfill_provenance_tag_present():
    c = asyncio.run(_counts())
    assert c["orphans"] == 0, (
        f"{c['orphans']} rows have a numeric line but no line_source — "
        "backfill did not tag them")
    assert c["historical"] >= 1000, (
        f"expected >=1000 historical_selection_parse rows, "
        f"got {c['historical']}")
