"""Focused regression — Settlement `timedelta` NameError closure.

Before the fix, ``settle_due_picks`` carried two local
``from datetime import timedelta`` statements inside a ``try`` block
around lines 703 and 728.  Python treats ``timedelta`` as a local name
throughout the enclosing function scope from the moment such an assignment
exists, so earlier uses (line 395's ``now_utc_for_void - timedelta(days=14)``
and line 603's ``timedelta(minutes=…)``) crashed with:

    UnboundLocalError: cannot access local variable 'timedelta'
    where it is not associated with a value

The fix deletes both redundant local imports.  This test proves the
function no longer raises ``UnboundLocalError`` when it reaches the
auto-void code path (line 395) with a non-empty pick list — the empty-queue
early return at line 371 would have masked the defect.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta

import pytest

from settlement_engine import settle_due_picks


class _FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, *_a, **_k):
        return self

    async def to_list(self, length=None):
        return list(self._docs)


class _FakePicks:
    def __init__(self, docs):
        self._docs = list(docs)
        self.updates = []

    def find(self, *_a, **_k):
        return _FakeCursor(self._docs)

    async def find_one(self, *_a, **_k):
        return self._docs[0] if self._docs else None

    async def update_one(self, filt, update, upsert=False):
        self.updates.append((filt, update))

        class _R:
            matched_count = 1
            modified_count = 1
        return _R()

    async def update_many(self, filt, update):
        for d in self._docs:
            d.setdefault("_updates", []).append(update)

        class _R:
            matched_count = len(self._docs)
            modified_count = len(self._docs)
        return _R()

    async def distinct(self, *_a, **_k):
        return []


class _FakeTelemetryCollection:
    async def insert_one(self, *_a, **_k):
        class _R:
            inserted_id = "x"
        return _R()

    def find(self, *_a, **_k):
        return _FakeCursor([])

    async def find_one(self, *_a, **_k):
        return None

    async def create_index(self, *_a, **_k):
        return None


class _FakeDB:
    def __init__(self, picks_docs):
        self.picks = _FakePicks(picks_docs)
        self.settlement_runs = _FakeTelemetryCollection()

    def __getattr__(self, name):
        # Any other collection touched by settlement (telemetry, audit,
        # etc.) — hand back a no-op collection.
        return _FakeTelemetryCollection()


def _stale_pick(days_ago: int, sport: str = "MLB") -> dict:
    et = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    return {
        "id": f"test-stale-{days_ago}d",
        "sport": sport,
        "market": "Total Runs Over 8.5",
        "selection": "Over 8.5",
        "status": "pending",
        "publication_state": "PUBLISHED",
        "settlement_block": False,
        "event_time": et,
        "book_odds": -110,
        "home_team": "Test Home",
        "away_team": "Test Away",
    }


def test_settle_due_picks_no_timedelta_unbound_local():
    """The auto-void path (line ~395) must run without hitting an
    UnboundLocalError on `timedelta`. We feed one pick well past the
    14-day auto-void horizon so the function traverses beyond the
    empty-queue early return and executes the `timedelta(days=14)`
    line that used to crash."""
    picks_docs = [_stale_pick(days_ago=20)]
    db = _FakeDB(picks_docs)

    # If the previous scope bug is present, this call raises
    # UnboundLocalError before it can return counts.
    counts = asyncio.get_event_loop().run_until_complete(
        settle_due_picks(db, sport_filter=None),
    )

    # The function must return a counts dict (proving no NameError /
    # UnboundLocalError escaped) — grading correctness is out of
    # scope for this regression.
    assert isinstance(counts, dict), (
        "settle_due_picks did not return a counts dict — likely the "
        "previous UnboundLocalError('timedelta') has resurfaced."
    )
    assert "candidates_examined" in counts
    assert counts["candidates_examined"] >= 1, counts


if __name__ == "__main__":
    test_settle_due_picks_no_timedelta_unbound_local()
    print("PASS: settlement timedelta scope regression")
