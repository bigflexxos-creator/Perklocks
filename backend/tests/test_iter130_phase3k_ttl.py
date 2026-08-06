"""Phase 3K — publication_mismatch_report TTL migration tests."""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

import pytest
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from motor.motor_asyncio import AsyncIOMotorClient
from services.database import (
    override_database_for_testing, reset_database_override,
)
from services.index_registry import get_specs_for_collection


COLL = "publication_mismatch_report"


def _with_fresh_client(coro):
    async def wrapper():
        c = AsyncIOMotorClient(os.environ["MONGO_URL"])
        db = c[os.environ.get("DB_NAME", "lockscore_db")]
        override_database_for_testing(c, db)
        try:
            await coro(db)
        finally:
            reset_database_override()
            c.close()
    asyncio.run(wrapper())


# ── 1. Registry declares TTL on logged_at_dt exactly 30 days ─────
def test_registry_declares_30_day_ttl_on_logged_at_dt():
    specs = get_specs_for_collection(COLL)
    ttl_specs = [s for s in specs if s.expire_after_seconds is not None]
    assert len(ttl_specs) == 1, "exactly one TTL spec expected"
    s = ttl_specs[0]
    assert s.keys == (("logged_at_dt", 1),)
    assert s.expire_after_seconds == 2_592_000     # 30 days


# ── 2. Live index actually exists with correct settings ──────────
def test_live_ttl_index_present():
    async def body(db):
        idx = await db[COLL].index_information()
        assert "mismatch_logged_at_dt_ttl" in idx
        assert idx["mismatch_logged_at_dt_ttl"]["expireAfterSeconds"] == 2_592_000
        assert idx["mismatch_logged_at_dt_ttl"]["key"] == [("logged_at_dt", 1)]
    _with_fresh_client(body)


# ── 3. New writer emits BSON Date logged_at_dt ───────────────────
def test_writer_emits_logged_at_dt():
    """Assert the code path that inserts a mismatch row includes
    a BSON-Date logged_at_dt beside the ISO string logged_at."""
    from pathlib import Path
    src = Path("/app/backend/services/prediction_publication_service.py").read_text()
    assert '"logged_at_dt":  _now,' in src or '"logged_at_dt": _now' in src
    assert 'insert_one({' in src


# ── 4. logged_at ISO string still written (compat) ───────────────
def test_writer_preserves_legacy_logged_at():
    from pathlib import Path
    src = Path("/app/backend/services/prediction_publication_service.py").read_text()
    assert '"logged_at":     _now.isoformat()' in src


# ── 5. Live coverage ≥ 99% after backfill ────────────────────────
def test_live_coverage_after_backfill():
    async def body(db):
        total = await db[COLL].count_documents({})
        with_dt = await db[COLL].count_documents(
            {"logged_at_dt": {"$type": "date"}})
        if total == 0:
            pytest.skip("collection empty")
        assert with_dt / total >= 0.99, (with_dt, total)
    _with_fresh_client(body)


# ── 6. Backfill script parses timezone offsets correctly ─────────
def test_backfill_parser_normalises_to_utc():
    from scripts.backfills.backfill_publication_mismatch_logged_at_dt import _parse_iso
    a = _parse_iso("2026-08-06T07:09:07.463417+00:00")
    b = _parse_iso("2026-08-06T00:09:07.463417-07:00")
    c = _parse_iso("2026-08-06T07:09:07.463417Z")
    # All three represent the same instant.
    assert a == b == c
    assert a.tzinfo == timezone.utc


# ── 7. Backfill parser raises on invalid input ───────────────────
def test_backfill_parser_raises_on_invalid():
    from scripts.backfills.backfill_publication_mismatch_logged_at_dt import _parse_iso
    with pytest.raises(Exception):
        _parse_iso("not-a-date")


# ── 8. Rerun is idempotent (no pending rows after full backfill) ─
def test_backfill_is_idempotent_after_completion():
    async def body(db):
        pending = await db[COLL].count_documents({
            "logged_at":    {"$type": "string"},
            "logged_at_dt": {"$exists": False},
        })
        assert pending == 0, f"{pending} rows still missing logged_at_dt"
    _with_fresh_client(body)


# ── 9. Immutability — no snapshots were mutated ─────────────────
def test_prediction_snapshots_unchanged():
    """Phase 1 invariant: snapshot documents are never rewritten.
    Our backfill only touched publication_mismatch_report; assert
    it did not accidentally add logged_at_dt to snapshots."""
    async def body(db):
        n = await db.prediction_snapshots.count_documents(
            {"logged_at_dt": {"$exists": True}})
        assert n == 0
    _with_fresh_client(body)


# ── 10. Old TTL block-note is gone from the registry declaration ─
def test_old_block_note_removed():
    from pathlib import Path
    src = Path("/app/backend/services/index_registry.py").read_text()
    assert "PHASE3C: TTL declined here" not in src
    assert "PHASE3K (2026-08): TTL now applied" in src
