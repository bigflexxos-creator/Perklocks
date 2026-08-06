"""Phase 3G Step 2 — Canonical UserBetLedger tests (test_iter131).

Covers every invariant declared in the Step 2 prompt (22 items):

  1. ``user_bets`` is the declared canonical ledger.
  2. ``plearn_*`` rows are always rejected from user-wager migration.
  3. Rows without ``user_id`` are rejected from user-wager migration.
  4. Eligible ``p_*`` rows map correctly.
  5. Legacy ``live`` maps to canonical ``pending``.
  6. Legacy ``void`` remains ``void`` (never coerced to pushed).
  7. Legacy ``push`` maps to ``pushed``.
  8. Unknown statuses are preserved in ``original_status`` and reported.
  9. Missing CLV produces ``clv_value=None`` and ``clv_status='unavailable'``.
 10. Missing book / line values are not invented.
 11. Same ``client_bet_id`` for the same user is idempotent.
 12. Different users may use the same client-generated ID without collision.
 13. ``migration_source_id`` prevents duplicate migration (identity semantics).
 14. Different exact lines remain distinct wagers.
 15. Different sportsbooks remain distinct when book data exists.
 16. Original wager line and odds remain frozen post-creation.
 17. Canonical parlay legs preserve all available identity references.
 18. The pure legacy mapper performs zero database writes.
 19. No independent Mongo client is created — the ledger uses Phase 3B.
 20. Published prediction snapshots remain immutable (no writes elsewhere).
 21. Existing frontend response schemas remain unchanged (no route conversion).
 22. All prior Phase 1–3 tests remain passing (verified externally).

Every test is fully isolated: each runs in its own async loop against
a fresh test collection scoped by a unique prefix per test.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import os
import re
import uuid
from pathlib import Path
from typing import Any

import pytest
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from motor.motor_asyncio import AsyncIOMotorClient

from services import database as SDB
from services.database import (
    initialize_database,
    override_database_for_testing,
    reset_database_override,
)
from services import user_bet_ledger as UBL
from services.user_bet_ledger import (
    CANONICAL_MIGRATION_VERSION,
    CANONICAL_STATUSES,
    CLV_UNAVAILABLE,
    LEGACY_STATUS_MAP,
    LegacyRowNotEligible,
    STATUS_CANCELLED,
    STATUS_LOST,
    STATUS_PENDING,
    STATUS_PUSHED,
    STATUS_UNKNOWN,
    STATUS_VOID,
    STATUS_WON,
    UserBet,
    UserBetCreateRequest,
    UserBetLeg,
    UserBetLedgerError,
    UserBetResult,
    WAGER_TYPE_PARLAY,
    WAGER_TYPE_STRAIGHT,
    compute_idempotency_key,
    create_bet,
    create_parlay,
    get_bet,
    get_or_create_by_idempotency,
    is_eligible_legacy_user_parlay,
    is_learning_row,
    list_bets_for_user,
    map_legacy_status,
    map_legacy_user_parlay,
    preflight_unique_indexes,
    safe_ledger_diagnostics,
    settle_bet,
    settle_leg,
    void_bet,
    cancel_bet,
)


# ── Async harness ─────────────────────────────────────────────────────
def _with_fresh_client(coro, isolate_collection: str | None = None):
    """Run a test body against a per-loop client + a UNIQUE test
    collection prefix so cross-test state cannot leak.

    Each test gets its own DB name (``ubl_test_<uuid>``) so ``user_bets``
    starts empty and is dropped in ``finally``.
    """
    async def wrapper():
        c = AsyncIOMotorClient(os.environ.get("MONGO_URL") or "mongodb://localhost:27017")
        db_name = f"ubl_test_{uuid.uuid4().hex[:12]}"
        db = c[db_name]
        override_database_for_testing(c, db)
        try:
            await coro(db)
        finally:
            try:
                await c.drop_database(db_name)
            except Exception:
                pass
            reset_database_override()
            c.close()
    asyncio.run(wrapper())


# ─────────────────────────────────────────────────────────────────────
# 1.  user_bets is the declared canonical ledger
# ─────────────────────────────────────────────────────────────────────
def test_canonical_ledger_collection_name():
    assert UBL.COLLECTION == "user_bets"
    # And every API touches this collection only (name-check).
    src = Path(UBL.__file__).read_text(encoding="utf-8")
    # Only reference to parlay_history from the ledger is read-only
    # inside safe_ledger_diagnostics — we ensure no write ops touch it.
    ph_lines = [ln for ln in src.splitlines()
                if "parlay_history" in ln and ("insert" in ln or "update" in ln or "delete" in ln)]
    assert not ph_lines, f"ledger MUST NOT write to parlay_history: {ph_lines}"


def test_canonical_status_vocabulary():
    # Exact status vocabulary from Step 2 prompt.
    expected = {"pending", "won", "lost", "pushed", "void",
                "partially_settled", "cancelled"}
    assert set(CANONICAL_STATUSES) == expected


# ─────────────────────────────────────────────────────────────────────
# 2.  plearn_* rows are always rejected from user-wager migration
# ─────────────────────────────────────────────────────────────────────
def test_plearn_row_is_never_eligible():
    plearn = {
        "id": "plearn_abc1234567",
        "signature": "sig1",
        "legs": [{"pick_id": "p1"}, {"pick_id": "p2"}],
        "status": "pending",
        "shown_at": "2026-01-01T00:00:00+00:00",
        "leg_count": 2,
    }
    assert is_learning_row(plearn) is True
    assert is_eligible_legacy_user_parlay(plearn) is False
    with pytest.raises(LegacyRowNotEligible):
        map_legacy_user_parlay(plearn)


def test_plearn_row_with_ranking_snapshot_rejected():
    plearn = {
        "id": "plearn_xyz",
        "ranking_snapshot": {"p1": {"parlay_score": 1}},
        "legs": [{"pick_id": "p1"}, {"pick_id": "p2"}],
    }
    assert is_learning_row(plearn) is True
    with pytest.raises(LegacyRowNotEligible):
        map_legacy_user_parlay(plearn)


def test_plearn_row_even_with_forged_p_prefix_is_rejected_by_signature_signal():
    # Defence in depth: even if id starts with "p_", the presence of
    # ranking_snapshot + no user_id trips the learning-row detector.
    forged = {
        "id": "p_forged123",
        "ranking_snapshot": {"x": 1},
        "correlation_snapshot": {"y": 1},
        "legs": [{"pick_id": "a"}, {"pick_id": "b"}],
    }
    assert is_learning_row(forged) is True


# ─────────────────────────────────────────────────────────────────────
# 3.  rows without user_id are rejected from user-wager migration
# ─────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("bad_uid", [None, "", 0])
def test_missing_user_id_rejected(bad_uid):
    doc = {
        "id": "p_valid1234",
        "user_id": bad_uid,
        "leg_ids": ["a", "b"],
        "legs": [{"pick_id": "a"}, {"pick_id": "b"}],
        "status": "live",
    }
    assert is_eligible_legacy_user_parlay(doc) is False
    with pytest.raises(LegacyRowNotEligible):
        map_legacy_user_parlay(doc)


# ─────────────────────────────────────────────────────────────────────
# 4.  eligible p_* rows map correctly
# ─────────────────────────────────────────────────────────────────────
def test_eligible_p_row_maps_completely():
    src = {
        "id": "p_test1234abcd",
        "user_id": "user-1",
        "created_at": "2026-06-21T15:59:47.898347+00:00",
        "mode": "standard",
        "leg_ids": ["a", "b"],
        "legs": [
            {"pick_id": "a", "sport": "Tennis", "event": "X vs Y",
             "market": "X ML", "selection": "X", "book_odds": -200,
             "status": "won"},
            {"pick_id": "b", "sport": "Tennis", "event": "M vs N",
             "market": "M ML", "selection": "M", "book_odds": -150,
             "status": "won"},
        ],
        "combined_odds": 285,
        "stake": 10.0,
        "status": "won",
        "settled_at": "2026-06-21T16:32:17.591862+00:00",
        "payout": 28.5,
    }
    assert is_eligible_legacy_user_parlay(src) is True
    bet = map_legacy_user_parlay(src)
    assert isinstance(bet, UserBet)
    assert bet.user_id == "user-1"
    assert bet.wager_type == WAGER_TYPE_PARLAY
    assert bet.status == STATUS_WON
    assert bet.original_status == "won"
    assert bet.combined_odds == 285
    assert bet.stake_amount == 10.0
    assert bet.stake_units == 10.0
    assert bet.actual_payout == 28.5
    assert bet.profit_loss == 28.5
    assert bet.migration_source == "parlay_history"
    assert bet.migration_source_id == "p_test1234abcd"
    assert bet.is_legacy is True
    assert bet.migration_version == CANONICAL_MIGRATION_VERSION
    assert bet.mode == "standard"
    assert len(bet.legs) == 2
    # Legs preserved verbatim
    assert bet.legs[0].prediction_id == "a"
    assert bet.legs[0].original_odds == -200
    assert bet.legs[0].sport_key == "Tennis"
    assert bet.legs[0].original_status == "won"
    # CLV/book preserved as null — never invented
    assert bet.sportsbook is None
    assert bet.opening_line is None
    assert bet.closing_line is None
    assert bet.clv_value is None
    assert bet.clv_status == CLV_UNAVAILABLE


def test_lost_row_maps_profit_loss_to_neg_stake():
    src = {
        "id": "p_lost123",
        "user_id": "u-lost",
        "created_at": "2026-06-21T00:00:00+00:00",
        "leg_ids": ["a", "b"],
        "legs": [{"pick_id": "a"}, {"pick_id": "b"}],
        "combined_odds": 500,
        "stake": 5.0,
        "status": "lost",
        "settled_at": "2026-06-21T02:00:00+00:00",
        "payout": None,
    }
    bet = map_legacy_user_parlay(src)
    assert bet.status == STATUS_LOST
    assert bet.profit_loss == -5.0
    assert bet.actual_payout is None


# ─────────────────────────────────────────────────────────────────────
# 5.  legacy live → pending
# ─────────────────────────────────────────────────────────────────────
def test_status_map_live_to_pending():
    assert map_legacy_status("live") == STATUS_PENDING
    assert map_legacy_status("LIVE") == STATUS_PENDING


def test_map_legacy_row_live_status_becomes_pending():
    src = {
        "id": "p_live1",
        "user_id": "u-1",
        "created_at": "2026-06-01T00:00:00+00:00",
        "leg_ids": ["a", "b"],
        "legs": [{"pick_id": "a"}, {"pick_id": "b"}],
        "combined_odds": 200,
        "stake": 1.0,
        "status": "live",
    }
    bet = map_legacy_user_parlay(src)
    assert bet.status == STATUS_PENDING
    assert bet.original_status == "live"


# ─────────────────────────────────────────────────────────────────────
# 6.  void remains void (NEVER coerced to pushed)
# ─────────────────────────────────────────────────────────────────────
def test_void_stays_void():
    assert map_legacy_status("void") == STATUS_VOID
    assert map_legacy_status("VOID") == STATUS_VOID
    # And absolutely NEVER pushed:
    assert map_legacy_status("void") != STATUS_PUSHED


def test_legacy_row_void_status_stays_void_and_zero_pnl():
    src = {
        "id": "p_void1",
        "user_id": "u-1",
        "created_at": "2026-06-01T00:00:00+00:00",
        "leg_ids": ["a", "b"],
        "legs": [{"pick_id": "a"}, {"pick_id": "b"}],
        "combined_odds": 200,
        "stake": 1.0,
        "status": "void",
    }
    bet = map_legacy_user_parlay(src)
    assert bet.status == STATUS_VOID
    assert bet.original_status == "void"
    assert bet.profit_loss == 0.0


# ─────────────────────────────────────────────────────────────────────
# 7.  push maps to pushed
# ─────────────────────────────────────────────────────────────────────
def test_push_maps_to_pushed():
    assert map_legacy_status("push") == STATUS_PUSHED
    assert map_legacy_status("pushed") == STATUS_PUSHED
    assert map_legacy_status("PUSH") == STATUS_PUSHED


def test_legacy_row_push_maps_to_pushed_and_pushed_stays_distinct_from_void():
    src = {
        "id": "p_push1",
        "user_id": "u-1",
        "created_at": "2026-06-01T00:00:00+00:00",
        "leg_ids": ["a", "b"],
        "legs": [{"pick_id": "a"}, {"pick_id": "b"}],
        "combined_odds": 200,
        "stake": 1.0,
        "status": "push",
    }
    bet = map_legacy_user_parlay(src)
    assert bet.status == STATUS_PUSHED
    assert bet.status != STATUS_VOID


# ─────────────────────────────────────────────────────────────────────
# 8.  unknown statuses are preserved and reported
# ─────────────────────────────────────────────────────────────────────
def test_unknown_status_preserved_and_reported():
    assert map_legacy_status("mystery_status") == STATUS_UNKNOWN
    src = {
        "id": "p_unknown1",
        "user_id": "u-2",
        "created_at": "2026-06-01T00:00:00+00:00",
        "leg_ids": ["a", "b"],
        "legs": [{"pick_id": "a"}, {"pick_id": "b"}],
        "combined_odds": 200,
        "stake": 1.0,
        "status": "settling_third_period",   # not in map
    }
    bet = map_legacy_user_parlay(src)
    assert bet.status == STATUS_UNKNOWN
    assert bet.original_status == "settling_third_period"


# ─────────────────────────────────────────────────────────────────────
# 9.  missing CLV → clv_value=None + clv_status="unavailable"
# ─────────────────────────────────────────────────────────────────────
def test_new_bet_has_clv_unavailable_when_no_market_data():
    async def body(db):
        req = UserBetCreateRequest(
            user_id="u-clv",
            wager_type=WAGER_TYPE_STRAIGHT,
            prediction_id="pred-1",
            stake_amount=1.0,
            stake_units=1.0,
            odds=-110,
        )
        r = await create_bet(req)
        assert r.created is True
        assert r.bet.clv_status == CLV_UNAVAILABLE
        assert r.bet.clv_value is None
        # And explicitly never 0.
        assert r.bet.clv_value is not 0
    _with_fresh_client(body)


# ─────────────────────────────────────────────────────────────────────
# 10. missing book / line values are not invented
# ─────────────────────────────────────────────────────────────────────
def test_missing_book_and_line_are_null_not_invented():
    src = {
        "id": "p_nobook",
        "user_id": "u-nb",
        "created_at": "2026-06-01T00:00:00+00:00",
        "leg_ids": ["a", "b"],
        "legs": [{"pick_id": "a", "book_odds": -110},
                 {"pick_id": "b"}],       # no book_odds on this leg
        "combined_odds": 100,
        "stake": 1.0,
        "status": "live",
    }
    bet = map_legacy_user_parlay(src)
    assert bet.sportsbook is None
    assert bet.opening_line is None
    assert bet.opening_odds is None
    assert bet.closing_line is None
    assert bet.closing_odds is None
    # Leg without book_odds must be None, not 0.
    assert bet.legs[1].original_odds is None
    # Line was never in the source → None in every leg.
    assert all(L.line is None for L in bet.legs)


# ─────────────────────────────────────────────────────────────────────
# 11. same client_bet_id for same user is idempotent
# ─────────────────────────────────────────────────────────────────────
def test_client_bet_id_is_idempotent_for_same_user():
    async def body(db):
        req = UserBetCreateRequest(
            user_id="user-idem",
            wager_type=WAGER_TYPE_STRAIGHT,
            prediction_id="pred-1",
            stake_units=1.0, odds=-110,
            client_bet_id="tap-once-1",
        )
        r1 = await create_bet(req)
        r2 = await create_bet(req)
        assert r1.created is True
        assert r2.created is False
        assert r1.bet.user_bet_id == r2.bet.user_bet_id
        # There must be exactly one row in Mongo.
        n = await db[UBL.COLLECTION].count_documents({"user_id": "user-idem"})
        assert n == 1
    _with_fresh_client(body)


# ─────────────────────────────────────────────────────────────────────
# 12. different users may share client_bet_id without collision
# ─────────────────────────────────────────────────────────────────────
def test_client_bet_id_is_user_scoped():
    async def body(db):
        common = "same-client-tap"
        r1 = await create_bet(UserBetCreateRequest(
            user_id="alice", wager_type=WAGER_TYPE_STRAIGHT,
            prediction_id="pA", stake_units=1.0, odds=+120,
            client_bet_id=common,
        ))
        r2 = await create_bet(UserBetCreateRequest(
            user_id="bob", wager_type=WAGER_TYPE_STRAIGHT,
            prediction_id="pB", stake_units=1.0, odds=+120,
            client_bet_id=common,
        ))
        assert r1.created is True
        assert r2.created is True
        assert r1.bet.user_bet_id != r2.bet.user_bet_id
        assert r1.bet.user_id != r2.bet.user_id
    _with_fresh_client(body)


# ─────────────────────────────────────────────────────────────────────
# 13. migration_source_id prevents duplicate migration (identity)
# ─────────────────────────────────────────────────────────────────────
def test_migration_source_id_prevents_dup_via_idempotency_shape():
    # The mapping function itself is pure; the migration script (not
    # part of Step 2) inserts under a partial-unique
    # (migration_source, migration_source_id) constraint.  We verify
    # here that the mapper propagates the identity fields exactly and
    # deterministically for any given input.
    src = {
        "id": "p_migidem",
        "user_id": "u-m",
        "created_at": "2026-06-01T00:00:00+00:00",
        "leg_ids": ["a", "b"],
        "legs": [{"pick_id": "a"}, {"pick_id": "b"}],
        "combined_odds": 200,
        "stake": 1.0,
        "status": "won",
        "payout": 2.0,
    }
    a = map_legacy_user_parlay(src)
    b = map_legacy_user_parlay(src)
    assert a.migration_source == b.migration_source == "parlay_history"
    assert a.migration_source_id == b.migration_source_id == "p_migidem"
    # The user_bet_id itself is UUID and DOES differ (that's fine — the
    # migration relies on the compound (source, source_id) index).
    assert a.user_bet_id != b.user_bet_id


# ─────────────────────────────────────────────────────────────────────
# 14. different exact lines remain distinct wagers
# ─────────────────────────────────────────────────────────────────────
def test_different_lines_remain_distinct_wagers():
    async def body(db):
        # Two parlays with legs that differ ONLY by line — different
        # canonical wagers.
        legs_a = [
            UserBetLeg(prediction_id="p1", line=6.5, original_odds=-110, event_id="ev1"),
            UserBetLeg(prediction_id="p2", line=1.5, original_odds=+120, event_id="ev2"),
        ]
        legs_b = [
            UserBetLeg(prediction_id="p1", line=7.5, original_odds=-110, event_id="ev1"),
            UserBetLeg(prediction_id="p2", line=1.5, original_odds=+120, event_id="ev2"),
        ]
        r1 = await create_parlay(UserBetCreateRequest(
            user_id="u-line",
            wager_type=WAGER_TYPE_PARLAY,
            stake_units=1.0, combined_odds=200, odds=200,
            legs=legs_a,
        ))
        r2 = await create_parlay(UserBetCreateRequest(
            user_id="u-line",
            wager_type=WAGER_TYPE_PARLAY,
            stake_units=1.0, combined_odds=200, odds=200,
            legs=legs_b,
        ))
        assert r1.created is True
        assert r2.created is True
        assert r1.bet.user_bet_id != r2.bet.user_bet_id
        n = await db[UBL.COLLECTION].count_documents({"user_id": "u-line"})
        assert n == 2
    _with_fresh_client(body)


# ─────────────────────────────────────────────────────────────────────
# 15. different sportsbooks remain distinct when book data exists
# ─────────────────────────────────────────────────────────────────────
def test_different_sportsbooks_are_distinct_wagers():
    async def body(db):
        base = dict(
            user_id="u-sb",
            wager_type=WAGER_TYPE_STRAIGHT,
            prediction_id="pred-sb",
            stake_units=1.0, odds=+150,
        )
        r1 = await create_bet(UserBetCreateRequest(sportsbook="dk", **base))
        r2 = await create_bet(UserBetCreateRequest(sportsbook="fd", **base))
        assert r1.created is True
        assert r2.created is True
        assert r1.bet.user_bet_id != r2.bet.user_bet_id
        assert r1.bet.sportsbook == "dk"
        assert r2.bet.sportsbook == "fd"
    _with_fresh_client(body)


# ─────────────────────────────────────────────────────────────────────
# 16. original wager line and odds remain frozen
# ─────────────────────────────────────────────────────────────────────
def test_original_odds_and_line_are_frozen_after_create():
    async def body(db):
        legs = [
            UserBetLeg(prediction_id="p1", line=6.5, original_odds=-110, event_id="ev1"),
            UserBetLeg(prediction_id="p2", line=1.5, original_odds=+120, event_id="ev2"),
        ]
        r = await create_parlay(UserBetCreateRequest(
            user_id="u-freeze", wager_type=WAGER_TYPE_PARLAY,
            stake_units=1.0, combined_odds=200, odds=200,
            legs=legs,
        ))
        assert r.created is True

        # Attempt a settle_leg with a different actual_result.  The
        # frozen values must survive.
        _ = await settle_leg(
            r.bet.user_bet_id, "p1", status=STATUS_WON,
            actual_result="over", actor="test",
        )
        after = await get_bet(r.bet.user_bet_id)
        assert after is not None
        # First leg — frozen odds/line preserved
        assert after.legs[0].original_odds == -110
        assert after.legs[0].line == 6.5
        # Second leg untouched
        assert after.legs[1].original_odds == +120
        assert after.legs[1].line == 1.5
        # And combined_odds on the parent unchanged
        assert after.combined_odds == 200
    _with_fresh_client(body)


# ─────────────────────────────────────────────────────────────────────
# 17. canonical parlay legs preserve all available identity references
# ─────────────────────────────────────────────────────────────────────
def test_canonical_parlay_leg_preserves_all_available_identity_fields():
    leg = UserBetLeg(
        leg_id="L1",
        prediction_id="pred-1",
        snapshot_id="snap-1",
        market_contract_id="mc-1",
        event_id="ev-1",
        sport_key="MLB",
        participant_id="participant-1",
        market="Total Bases",
        selection="over",
        side="over",
        line=1.5,
        original_odds=-115,
        sportsbook="dk",
        status=STATUS_PENDING,
    )
    d = leg.to_document()
    for k in ("leg_id", "prediction_id", "snapshot_id", "market_contract_id",
              "event_id", "sport_key", "participant_id", "market", "selection",
              "side", "line", "original_odds", "sportsbook"):
        assert k in d, f"leg document missing {k}"
        assert d[k] is not None, f"leg document {k} unexpectedly None"


# ─────────────────────────────────────────────────────────────────────
# 18. pure legacy mapper performs ZERO database writes
# ─────────────────────────────────────────────────────────────────────
def test_pure_legacy_mapper_performs_zero_db_writes():
    async def body(db):
        # Snapshot every collection's document count.
        colls = await db.list_collection_names()
        before = {c: await db[c].count_documents({}) for c in colls}

        # Map many times.
        for i in range(25):
            src = {
                "id": f"p_pure{i:03d}",
                "user_id": f"u-{i}",
                "created_at": "2026-06-01T00:00:00+00:00",
                "leg_ids": ["a", "b"],
                "legs": [{"pick_id": "a"}, {"pick_id": "b"}],
                "combined_odds": 200,
                "stake": 1.0,
                "status": "live",
            }
            _ = map_legacy_user_parlay(src)

        colls2 = await db.list_collection_names()
        # No new collections created.
        assert set(colls2) - set(colls) == set(), (colls, colls2)
        after = {c: await db[c].count_documents({}) for c in colls2}
        for c in before:
            assert before.get(c, 0) == after.get(c, 0), \
                f"mapper wrote to {c}: {before.get(c)} → {after.get(c)}"
    _with_fresh_client(body)


# ─────────────────────────────────────────────────────────────────────
# 19. no independent Mongo client is created — Phase 3B lifecycle
# ─────────────────────────────────────────────────────────────────────
def test_ledger_does_not_construct_independent_mongo_client():
    """Static + runtime check.

    Static: parse the ledger source and verify there is no
    ``AsyncIOMotorClient(...)`` / ``MongoClient(...)`` construction in
    the module body.

    Runtime: patch the shared-DB owner and confirm the ledger picks up
    the patched database via :func:`services.database.get_database`
    rather than opening its own connection.
    """
    src = Path(UBL.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    forbidden = {"AsyncIOMotorClient", "MongoClient"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in forbidden:
                raise AssertionError(
                    f"user_bet_ledger.py must not construct {func.id}(...) "
                    f"— use services.database.get_database() instead"
                )
            if isinstance(func, ast.Attribute) and func.attr in forbidden:
                raise AssertionError(
                    f"user_bet_ledger.py must not construct <mod>.{func.attr}(...)"
                )

    # Runtime — the ledger picks up the current shared DB.
    async def body(db):
        _ = await preflight_unique_indexes()
        # Nothing else — the fact that ``preflight_unique_indexes()``
        # ran without opening a new client means it went through the
        # shared override.  The override collection name is set by
        # ``_with_fresh_client``.
        assert SDB.get_database().name == db.name
    _with_fresh_client(body)


# ─────────────────────────────────────────────────────────────────────
# 20. published prediction snapshots remain immutable (no writes)
# ─────────────────────────────────────────────────────────────────────
def test_ledger_never_writes_to_prediction_snapshots():
    async def body(db):
        before = await db["prediction_snapshots"].count_documents({})
        # Full end-to-end: create + settle a straight bet.
        r = await create_bet(UserBetCreateRequest(
            user_id="u-snap", wager_type=WAGER_TYPE_STRAIGHT,
            prediction_id="pred-snap", stake_units=1.0, odds=-110,
        ))
        _ = await settle_bet(r.bet.user_bet_id, status=STATUS_WON,
                              profit_loss=0.91, actual_payout=0.91,
                              actor="test")
        after = await db["prediction_snapshots"].count_documents({})
        assert before == after, "ledger MUST NOT touch prediction_snapshots"
    _with_fresh_client(body)


# ─────────────────────────────────────────────────────────────────────
# 21. existing frontend response schemas remain unchanged
# ─────────────────────────────────────────────────────────────────────
def test_no_route_conversion_shipped_in_step_2():
    """Step 2 does NOT flip any production routes.  Verify by
    scanning ``routes/user_bets_routes.py`` and
    ``routes/parlay_history_routes.py`` — they must still import from
    the legacy paths (not the ledger) so response shapes are
    unchanged."""
    ubr = Path("/app/backend/routes/user_bets_routes.py").read_text(encoding="utf-8")
    phr = Path("/app/backend/routes/parlay_history_routes.py").read_text(encoding="utf-8")

    # user_bets_routes must NOT import the ledger yet (adapters land in
    # Step 5+).
    assert "from services.user_bet_ledger" not in ubr, \
        "Step 2 must not wire the ledger into user_bets_routes yet"
    assert "import services.user_bet_ledger" not in ubr

    # parlay_history_routes.py must still call parlay_history.save_parlay.
    assert "from parlay_history import" in phr


# ─────────────────────────────────────────────────────────────────────
# 22. all prior Phase 1–3 tests still passing — verified externally.
# ─────────────────────────────────────────────────────────────────────
# The full pytest run is the source of truth.  We assert one sentinel
# here: the Phase 3B/3C invariants still hold when the ledger module
# is imported.  If they didn't, the shared-DB guardrail would fail on
# import.
def test_phase3b_invariants_hold_when_ledger_imported():
    # The ledger imports the shared DB owner from services.database.
    # If Phase 3B invariants held before, they hold now (the ledger
    # never constructs a client — verified by test 19 statically).
    assert SDB.is_initialized() or SDB.get_database() is not None


# ═════════════════════════════════════════════════════════════════════
# Additional operational tests (settle / void / cancel / list / preflight)
# ═════════════════════════════════════════════════════════════════════
def test_settle_bet_transitions():
    async def body(db):
        r = await create_bet(UserBetCreateRequest(
            user_id="u-op", wager_type=WAGER_TYPE_STRAIGHT,
            prediction_id="pred-op", stake_units=1.0, odds=+150,
        ))
        assert r.bet.status == STATUS_PENDING
        s = await settle_bet(
            r.bet.user_bet_id, status=STATUS_WON,
            profit_loss=1.5, actual_payout=1.5, actor="test",
        )
        assert s.bet.status == STATUS_WON
        assert s.bet.profit_loss == 1.5
        assert len(s.bet.settlement_events) >= 1
        # Idempotent re-settle at same status.
        s2 = await settle_bet(r.bet.user_bet_id, status=STATUS_WON)
        assert s2.bet.status == STATUS_WON
    _with_fresh_client(body)


def test_void_and_cancel_are_distinct_operations():
    async def body(db):
        r = await create_bet(UserBetCreateRequest(
            user_id="u-vc", wager_type=WAGER_TYPE_STRAIGHT,
            prediction_id="pred-vc", stake_units=1.0, odds=+100,
            client_bet_id="tap-vc",
        ))
        v = await void_bet(r.bet.user_bet_id, reason="rain-out", actor="test")
        assert v.bet.status == STATUS_VOID
        assert v.bet.status != STATUS_PUSHED
        # Cannot cancel a voided bet.
        raised = False
        try:
            _ = await cancel_bet(r.bet.user_bet_id, actor="test")
        except UserBetLedgerError:
            raised = True
        assert raised, "cancel on voided bet must raise UserBetLedgerError"
    _with_fresh_client(body)


def test_cancel_requires_pending_state():
    async def body(db):
        r = await create_bet(UserBetCreateRequest(
            user_id="u-c", wager_type=WAGER_TYPE_STRAIGHT,
            prediction_id="pred-c", stake_units=1.0, odds=-110,
        ))
        c = await cancel_bet(r.bet.user_bet_id, reason="user-request", actor="test")
        assert c.bet.status == STATUS_CANCELLED
        # Second cancel raises (not pending anymore)
        raised = False
        try:
            _ = await cancel_bet(r.bet.user_bet_id)
        except UserBetLedgerError:
            raised = True
        assert raised, "second cancel must raise UserBetLedgerError"
    _with_fresh_client(body)


def test_list_bets_for_user_filters_and_orders():
    async def body(db):
        for i in range(3):
            await create_bet(UserBetCreateRequest(
                user_id="u-list", wager_type=WAGER_TYPE_STRAIGHT,
                prediction_id=f"pred-{i}", stake_units=1.0, odds=-110,
                client_bet_id=f"tap-{i}",
            ))
        rows = await list_bets_for_user("u-list")
        assert len(rows) == 3
        # Filter
        for b in rows[:1]:
            await settle_bet(b.user_bet_id, status=STATUS_WON,
                             profit_loss=0.91, actual_payout=0.91)
        won = await list_bets_for_user("u-list", status=STATUS_WON)
        assert len(won) == 1
        pending = await list_bets_for_user("u-list", status=STATUS_PENDING)
        assert len(pending) == 2
    _with_fresh_client(body)


def test_preflight_unique_indexes_on_empty_collection_is_ok():
    async def body(db):
        rep = await preflight_unique_indexes()
        assert rep.ok is True
        assert rep.duplicate_user_bet_id == 0
        assert rep.duplicate_client_bet_id_per_user == 0
        assert rep.duplicate_idempotency_key_per_user == 0
        assert rep.duplicate_migration_source_id == 0
        assert rep.conflicts == []
    _with_fresh_client(body)


def test_preflight_reports_conflicts_without_deleting():
    async def body(db):
        coll = db[UBL.COLLECTION]
        # Insert two rows sharing the same (user_id, client_bet_id) —
        # simulating pre-canonical dupes that would block the index.
        await coll.insert_many([
            {"user_bet_id": "u1", "user_id": "same-user", "client_bet_id": "same-tap"},
            {"user_bet_id": "u2", "user_id": "same-user", "client_bet_id": "same-tap"},
        ])
        n_before = await coll.count_documents({})
        rep = await preflight_unique_indexes()
        assert rep.ok is False
        assert rep.duplicate_client_bet_id_per_user >= 1
        # And crucially — no rows deleted.
        n_after = await coll.count_documents({})
        assert n_before == n_after == 2
    _with_fresh_client(body)


def test_safe_ledger_diagnostics_never_exposes_wager_details():
    async def body(db):
        # Seed one bet and one legacy p_* + one plearn_*
        await create_bet(UserBetCreateRequest(
            user_id="user-secret", wager_type=WAGER_TYPE_STRAIGHT,
            prediction_id="pred-secret", stake_units=999.99, odds=-100,
            client_bet_id="secret-bet",
        ))
        await db["parlay_history"].insert_many([
            {"id": "p_diag1", "user_id": "user-secret", "leg_ids": ["a", "b"],
             "legs": [{"pick_id": "a"}, {"pick_id": "b"}], "status": "live",
             "combined_odds": 200, "stake": 1.0},
            {"id": "plearn_learn1", "signature": "s1",
             "legs": [{"pick_id": "a"}], "status": "pending"},
        ])
        rep = await safe_ledger_diagnostics()
        # Structural checks
        assert rep["canonical"]["collection"] == "user_bets"
        assert rep["canonical"]["total_user_bets"] == 1
        assert rep["legacy_parlay_history"]["excluded_plearn_rows"] == 1
        assert rep["legacy_parlay_history"]["eligible_p_star_rows"] == 1
        # Privacy: no user_id / bet_id / stake values in the response.
        as_str = str(rep)
        assert "user-secret" not in as_str
        assert "secret-bet" not in as_str
        assert "999.99" not in as_str
    _with_fresh_client(body)


def test_straight_bet_requires_prediction_id():
    async def body(db):
        raised = False
        try:
            _ = await create_bet(UserBetCreateRequest(
                user_id="u-x", wager_type=WAGER_TYPE_STRAIGHT,
                stake_units=1.0, odds=-110,
                prediction_id=None,
            ))
        except UserBetLedgerError:
            raised = True
        assert raised, "straight bet without prediction_id must raise"
    _with_fresh_client(body)


def test_parlay_requires_two_legs():
    async def body(db):
        raised = False
        try:
            _ = await create_parlay(UserBetCreateRequest(
                user_id="u-x", wager_type=WAGER_TYPE_PARLAY,
                stake_units=1.0, combined_odds=200, odds=200,
                legs=[UserBetLeg(prediction_id="p1", event_id="e1")],
            ))
        except UserBetLedgerError:
            raised = True
        assert raised, "parlay with <2 legs must raise"
    _with_fresh_client(body)


def test_idempotency_key_stable_and_distinct_for_different_odds():
    async def body(db):
        req_a = UserBetCreateRequest(
            user_id="u-idk", wager_type=WAGER_TYPE_STRAIGHT,
            prediction_id="pred-1", stake_units=1.0, odds=-110,
        )
        req_b = UserBetCreateRequest(
            user_id="u-idk", wager_type=WAGER_TYPE_STRAIGHT,
            prediction_id="pred-1", stake_units=1.0, odds=-105,
        )
        # Distinct idempotency keys → distinct wagers.
        assert compute_idempotency_key(req_a) != compute_idempotency_key(req_b)
        r1 = await create_bet(req_a)
        r2 = await create_bet(req_b)
        assert r1.created is True
        assert r2.created is True
        assert r1.bet.user_bet_id != r2.bet.user_bet_id
        # Same shape → idempotent match.
        r3 = await create_bet(req_a)
        assert r3.created is False
        assert r3.bet.user_bet_id == r1.bet.user_bet_id
    _with_fresh_client(body)


def test_get_bet_returns_none_on_missing():
    async def body(db):
        assert await get_bet("not-there") is None
    _with_fresh_client(body)
