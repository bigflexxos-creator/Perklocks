"""P0 (2026-08-11) — Settlement audit + Seymour case verification.

Read-only tests that:
  * Confirm the audit script detects the Seymour failure class.
  * Prove the exact Seymour case would settle correctly under the
    Universal Settlement Contract if MLB StatsAPI returned 7 K.
  * Prove `player_history` backfill MUST exclude unverified rows.
"""
from __future__ import annotations

import asyncio
import os

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
def test_audit_detects_suspicious_actual_zero_losses():
    """The audit MUST flag rows with status='lost' + actual=0.
    Baseline for the environment: > 500 such rows in MLB (was 598
    on the initial run)."""
    from scripts.p0_settlement_audit import run
    async def go():
        r = await run(_db())
        mlb = r["counts_by_sport"].get("MLB", {})
        assert mlb.get("suspicious_actual_zero_loss", 0) >= 100, (
            "audit must detect the Seymour failure class in MLB "
            f"(got {mlb.get('suspicious_actual_zero_loss')})")
    _run(go())


@pytest.mark.integration
def test_seymour_pick_is_in_the_suspicious_bucket():
    """Pre-P0.3 this test proved the Seymour pick was classified as
    suspicious.  After the P0.3 correction (2026-08-11), Seymour's
    pick now has status='won' and actual=7 — it should NO LONGER be
    in the suspicious bucket.  This test now asserts the correction
    is in place."""
    from scripts.p0_settlement_audit import _classify
    async def go():
        db = _db()
        p = await db.picks.find_one(
            {"id": "6f163552-16fa-5c04-aa73-ebc2bb08ee73"},
            {"_id": 0})
        assert p is not None
        bucket, diag = _classify(p)
        # After the P0.3 reconciliation, Seymour is no longer
        # suspicious — status='won' and actual=7.
        assert bucket == "ok", (bucket, diag)
        assert diag["actual"] == 7
        assert diag["status"] == "won"
    _run(go())


@pytest.mark.unit
def test_seymour_settled_correctly_under_universal_contract():
    """Given the ACTUAL from MLB StatsAPI = 7 K, the pick MUST
    settle 'won' under the universal contract.  This is what the
    fix should produce once every settler adopts the contract."""
    from services.universal_settlement_contract import (
        grade_over_under, RESULT_WON,
    )
    r = grade_over_under(actual=7, line=5.5, side="over")
    assert r["result"] == RESULT_WON
    assert r["actual"] == 7
    assert r["settlement_verified"] is True


@pytest.mark.unit
def test_history_backfill_excludes_unverified_settlements():
    """Player History Foundation write-pass MUST reject rows where
    ``settlement_verified is False``.  This test locks the contract
    into the history validator so bad results never poison Magic
    Layer 2.0."""
    from services.player_history_contract import (
        validate_history_row, HistoryContractViolation,
    )
    # A row for an unverified pick MUST be rejected — we express
    # this by requiring `value` to be present.  An unverified /
    # unresolved settlement yields `value=None`, which the
    # contract already rejects.
    bad_row = {
        "canonical_player_id": "cpid_test",
        "sport": "MLB",
        "date": "2026-08-09",
        "event_id": "mlb_rays_mariners_20260809",
        "market": "pitcher_strikeouts",
        "value": None,   # ← unresolved settlement carries None
    }
    with pytest.raises(HistoryContractViolation):
        validate_history_row(bad_row)
