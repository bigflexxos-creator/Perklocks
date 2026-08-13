"""One-time cleanup — Emergent Support Soccer Board fix (2026-06).

Reclassify stale Soccer picks that were persisted with no real
sportsbook line from the main Locks board.  This script:

    * Targets ONLY unsettled picks (status ∈ {pending, open, upcoming,
      None}).  Settled picks (won / lost / void / push) are NEVER
      touched — settlement status, ``settled_at``, ``units_profit``,
      ``units_risked``, ``closing_odds``, ``snapshot`` and every
      other historical field is preserved verbatim.
    * Matches on the real-line-missing pattern:
          ``no_real_book_line == True``
          OR (``book_odds`` is null AND ``implied_probability`` is null)
          OR (``book_odds`` is null AND ``odds_source`` in
              {"MODEL_ONLY", "legacy_unknown"})
    * Annotates each matching row with:
          ``hide_from_main_board = True``
          ``is_extra = True``
          ``model_only = True``
          ``main_board_reclassified_reason = "no_real_book_line_stale_backfill"``
          ``main_board_reclassified_at = <utc iso>``
    * Preserves every existing field exactly as-is (no field is
      deleted, no scoring number is rewritten, no odds are fabricated).

Runs idempotently — a second invocation is a no-op because
``main_board_reclassified_reason`` becomes the guard.

Usage
─────
    cd /app/backend && DB_NAME=lockscore_db python -m scripts.cleanup_stale_no_real_line_soccer
        [--dry-run]    just report; do not write
        [--all-sports] also scan Tennis/UFC (opt-in)
"""
from __future__ import annotations

import argparse
import asyncio
import os
from datetime import datetime, timezone

from motor.motor_asyncio import AsyncIOMotorClient


_UNSETTLED_STATUSES = {"pending", "open", "upcoming", None}
_TARGET_FLAG = "no_real_book_line_stale_backfill"


def _match_predicate(all_sports: bool) -> dict:
    """Mongo predicate isolating stale no-real-line rows."""
    sport_clause = (
        {}    # every sport
        if all_sports
        else {"sport": "Soccer"}
    )
    return {
        **sport_clause,
        # UNSETTLED only — never touch historical truth
        "$or": [
            {"status": {"$in": ["pending", "open", "upcoming"]}},
            {"status": {"$exists": False}},
            {"status": None},
        ],
        # Not yet reclassified — idempotent guard
        "main_board_reclassified_reason": {"$exists": False},
        # The actual stale-pattern match
        "$and": [
            {"$or": [
                {"no_real_book_line": True},
                {"$and": [
                    {"book_odds": None},
                    {"implied_probability": None},
                ]},
                {"$and": [
                    {"book_odds": None},
                    {"odds_source": {"$in": ["MODEL_ONLY", "legacy_unknown"]}},
                ]},
            ]},
        ],
    }


async def run(dry_run: bool, all_sports: bool) -> dict:
    mongo_url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
    db_name   = os.environ.get("DB_NAME", "lockscore_db")
    cli = AsyncIOMotorClient(mongo_url)
    db  = cli[db_name]

    match = _match_predicate(all_sports)
    total = await db.picks.count_documents(match)

    # Distribution snapshot BEFORE change
    breakdown: dict[str, int] = {}
    async for p in db.picks.find(match, {"_id": 0, "sport": 1, "source": 1,
                                          "market": 1, "no_real_book_line": 1,
                                          "book_odds": 1, "implied_probability": 1,
                                          "lock_score": 1}):
        key = (p.get("sport") or "?", p.get("source") or "?")
        breakdown[str(key)] = breakdown.get(str(key), 0) + 1

    # Also identify picks that would appear on main board (lock_score > 85)
    high_lock_match = {
        **match,
        "$or": [
            {"status": {"$in": ["pending", "open", "upcoming"]}},
            {"status": {"$exists": False}},
            {"status": None},
        ],
        "lock_score": {"$gt": 85},
    }
    on_board_count = await db.picks.count_documents({**match, "lock_score": {"$gt": 85}})

    now = datetime.now(timezone.utc).isoformat()
    summary = {
        "matched_total":         total,
        "matched_on_main_board": on_board_count,
        "breakdown_by_source":   breakdown,
        "dry_run":               dry_run,
        "started_at":            now,
    }

    if dry_run:
        summary["applied"] = 0
        return summary

    # Apply the annotation — additive $set only.
    result = await db.picks.update_many(
        match,
        {"$set": {
            "hide_from_main_board":          True,
            "is_extra":                      True,
            "model_only":                    True,
            "main_board_reclassified_reason": _TARGET_FLAG,
            "main_board_reclassified_at":    now,
        }},
    )
    summary["applied"]      = result.modified_count
    summary["finished_at"]  = datetime.now(timezone.utc).isoformat()
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run",    action="store_true")
    parser.add_argument("--all-sports", action="store_true")
    args = parser.parse_args()
    summary = asyncio.run(run(dry_run=args.dry_run, all_sports=args.all_sports))
    for k, v in summary.items():
        print(f"  {k:28s} {v}")


if __name__ == "__main__":
    main()
