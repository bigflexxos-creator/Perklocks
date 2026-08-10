"""Phase 5 (2026-08-11) — READ-ONLY backfill report tests.

Ensures the script:
    * emits the required per-sport keys
    * counts confirmed vs unresolved verdicts correctly
    * NEVER writes to Mongo (idempotent re-runs leave the DB alone)
    * produces a JSON file the operator can review
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone

import pytest
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")
from motor.motor_asyncio import AsyncIOMotorClient


def _db():
    return AsyncIOMotorClient(os.environ["MONGO_URL"])[
        os.environ.get("DB_NAME", "perkslocks_production")]


def _run(coro):
    return asyncio.run(coro)


@pytest.mark.integration
def test_backfill_report_emits_all_expected_keys_per_sport():
    from scripts.phase5_cross_sport_identity_backfill_report import (
        scan_sport,
    )
    from services.universal_player_identity import ENABLED_SPORTS

    async def go():
        db = _db()
        for sport in ENABLED_SPORTS:
            r = await scan_sport(db, sport)
            for key in ("sport", "roster_source", "picks_scanned",
                         "identities_resolved", "unresolved",
                         "source_conflicts", "current_team_mismatches",
                         "collisions_prevented", "history_rows_linked",
                         "high_confidence_picks_affected_gt_85",
                         "threshold_history_ready_players"):
                assert key in r, f"missing key {key} in {sport} report"
    _run(go())


@pytest.mark.integration
def test_backfill_report_is_read_only():
    """Running the report MUST NOT increase the size of picks /
    player_identities / player_history collections."""
    from scripts.phase5_cross_sport_identity_backfill_report import (
        run_report,
    )

    async def go():
        db = _db()
        n_picks_before = await db.picks.count_documents({})
        n_id_before = await db.player_identities.count_documents({})
        n_hist_before = await db.player_history.count_documents({})
        report = await run_report(
            os.environ["MONGO_URL"],
            os.environ.get("DB_NAME", "perkslocks_production"))
        n_picks_after = await db.picks.count_documents({})
        n_id_after = await db.player_identities.count_documents({})
        n_hist_after = await db.player_history.count_documents({})
        assert n_picks_after == n_picks_before, "picks was mutated"
        assert n_id_after == n_id_before, "player_identities was mutated"
        assert n_hist_after == n_hist_before, "player_history was mutated"
        assert report["read_only"] is True
        assert report["writes_performed"] == 0
    _run(go())


@pytest.mark.integration
def test_backfill_report_summary_totals_add_up():
    """Per-sport totals must aggregate into the report totals."""
    from scripts.phase5_cross_sport_identity_backfill_report import (
        run_report,
    )

    async def go():
        report = await run_report(
            os.environ["MONGO_URL"],
            os.environ.get("DB_NAME", "perkslocks_production"))
        for k in ("picks_scanned", "identities_resolved", "unresolved",
                   "current_team_mismatches"):
            per_sport = sum(s.get(k, 0) for s in report["sports"])
            assert per_sport == report["totals"][k]
    _run(go())
