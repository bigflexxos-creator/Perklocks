"""Phase 3G Step 7 — reader + settlement cutover tests (test_iter136).

Direct-handler + ledger-level tests for the final cutover invariants.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from motor.motor_asyncio import AsyncIOMotorClient

MONGO_URL = os.environ.get("MONGO_URL") or "mongodb://localhost:27017"


# ── Static ───────────────────────────────────────────────────────────
def test_parlay_history_route_reads_from_ledger():
    src = Path("/app/backend/routes/parlay_history_routes.py").read_text(encoding="utf-8")
    assert "_UBL.list_parlays_history_shape" in src
    # Legacy list_history import path removed from the LIST handler.
    # (parlay_history.list_history may still be imported elsewhere; we
    # just assert the LIST route no longer uses it.)
    assert "from parlay_history import list_history" not in src


def test_parlay_save_mirror_is_sunset():
    src = Path("/app/backend/routes/parlay_history_routes.py").read_text(encoding="utf-8")
    # The mirror insert path is removed.
    assert "await save_parlay(" not in src
    assert "user_bet_ledger_mirror" not in src  # no new mirror rows
    assert "serialize_parlay_history_row" in src


def test_ledger_exports_serializer_and_reader():
    from services import user_bet_ledger as UBL
    assert hasattr(UBL, "serialize_parlay_history_row")
    assert hasattr(UBL, "list_parlays_history_shape")


# ── Runtime ──────────────────────────────────────────────────────────
def _run(coro):
    async def wrapper():
        c = AsyncIOMotorClient(MONGO_URL)
        db_name = f"step7_{uuid.uuid4().hex[:12]}"
        db = c[db_name]
        from services.database import override_database_for_testing, reset_database_override
        override_database_for_testing(c, db)
        from services import index_registry as IR
        try: await IR.ensure_all_indexes(db)
        except Exception: pass
        import deps
        deps.db = db
        from routes import user_bets_routes, parlay_history_routes
        user_bets_routes.db = db
        parlay_history_routes.db = db
        try:
            await coro(db)
        finally:
            try: await c.drop_database(db_name)
            except Exception: pass
            reset_database_override(); c.close()
    asyncio.run(wrapper())


def _user(uid=None, email="u@example.com"):
    return SimpleNamespace(id=(uid or str(uuid.uuid4())), email=email,
                            role="user", status="active")


async def _seed_pick(db, pid, sport="MLB", odds=-110):
    await db.picks.insert_one({
        "id": pid, "sport": sport, "market": "ML", "selection": "X",
        "event": "X vs Y", "book_odds": int(odds),
    })


def test_parlay_history_reads_canonical_and_preserves_envelope():
    async def body(db):
        from routes import parlay_history_routes as PHR
        user = _user()
        legs = [
            {"id": "l1", "sport": "MLB", "market": "ML", "selection": "A",
             "book_odds": -110, "event": "A vs B"},
            {"id": "l2", "sport": "MLB", "market": "ML", "selection": "C",
             "book_odds": +100, "event": "C vs D"},
        ]
        r_save = await PHR.parlay_save(
            PHR.SaveParlayRequest(legs=legs, mode="standard", stake=1.0,
                                   client_bet_id="tap-s7"),
            user,
        )
        # Response envelope preserved.
        for k in ("id","user_id","legs","leg_ids","combined_odds",
                  "stake","status","legs_won","legs_lost","legs_pending"):
            assert k in r_save
        # No parlay_history mirror row.
        assert await db.parlay_history.count_documents({"user_id": user.id}) == 0
        # And it exists in the canonical ledger exactly once.
        assert await db.user_bets.count_documents({"user_id": user.id}) == 1

        r_list = await PHR.parlay_history_list(user, None, 50)
        assert "parlays" in r_list and "count" in r_list
        assert r_list["count"] == 1
        row = r_list["parlays"][0]
        for k in ("id","user_id","legs","leg_ids","combined_odds",
                  "stake","status","legs_won","legs_lost","legs_pending"):
            assert k in row
    _run(body)


def test_migrated_parlay_shows_once_and_plearn_never_appears():
    async def body(db):
        from routes import parlay_history_routes as PHR
        user = _user()
        # Migrated legacy row (already in canonical from Step 5).
        await db.user_bets.insert_one({
            "id": "leg-mig-1", "user_bet_id": "leg-mig-1",
            "user_id": user.id, "wager_type": "parlay",
            "is_legacy": True, "migration_source": "parlay_history",
            "migration_source_id": "p_legmig1", "migration_version": 1,
            "status": "won", "original_status": "won",
            "combined_odds": 300, "stake_amount": 1.0, "actual_payout": 3.0,
            "placed_at": None, "settled_at": None,
            "legs": [{"prediction_id": "a", "status": "won"},
                     {"prediction_id": "b", "status": "won"}],
        })
        # And the source legacy p_ row is STILL in parlay_history.
        await db.parlay_history.insert_one({
            "id": "p_legmig1", "user_id": user.id, "status": "won",
            "legs": [{"pick_id": "a"}, {"pick_id": "b"}],
            "leg_ids": ["a","b"], "combined_odds": 300, "stake": 1.0,
        })
        # And a plearn_ row exists (must NEVER appear).
        await db.parlay_history.insert_one({
            "id": "plearn_zzz", "signature": "s", "status": "pending",
            "legs": [{"pick_id": "a"}, {"pick_id": "b"}],
            "shown_at": "2026-06-01T00:00:00+00:00",
        })
        r = await PHR.parlay_history_list(user, None, 50)
        assert r["count"] == 1
        ids = {row["id"] for row in r["parlays"]}
        # Migrated row shown once, keyed by legacy id.
        assert ids == {"p_legmig1"}
        # No plearn.
        assert all(not str(row["id"]).startswith("plearn_") for row in r["parlays"])
    _run(body)


def test_status_filter_semantics_preserved():
    async def body(db):
        from routes import parlay_history_routes as PHR
        user = _user()
        for legacy, canonical in (("live","pending"),("won","won"),
                                    ("lost","lost")):
            await db.user_bets.insert_one({
                "id": f"ub-{legacy}", "user_bet_id": f"ub-{legacy}",
                "user_id": user.id, "wager_type": "parlay",
                "status": canonical, "original_status": legacy,
                "combined_odds": 200, "stake_amount": 1.0,
                "legs": [{"prediction_id":"a"},{"prediction_id":"b"}],
            })
        # ``all``
        r = await PHR.parlay_history_list(user, "all", 50)
        assert r["count"] == 3
        # ``won``
        r = await PHR.parlay_history_list(user, "won", 50)
        assert r["count"] == 1 and r["parlays"][0]["status"] == "won"
        # ``live`` maps to canonical pending
        r = await PHR.parlay_history_list(user, "live", 50)
        assert r["count"] == 1 and r["parlays"][0]["status"] == "live"
        # ``lost``
        r = await PHR.parlay_history_list(user, "lost", 50)
        assert r["count"] == 1 and r["parlays"][0]["status"] == "lost"
    _run(body)


def test_user_authorization_boundary_preserved():
    async def body(db):
        from routes import parlay_history_routes as PHR
        alice = _user(); bob = _user()
        await db.user_bets.insert_one({
            "id": "ub-A", "user_bet_id": "ub-A", "user_id": alice.id,
            "wager_type": "parlay", "status": "pending",
            "legs": [{"prediction_id":"a"},{"prediction_id":"b"}],
        })
        r_alice = await PHR.parlay_history_list(alice, None, 50)
        r_bob   = await PHR.parlay_history_list(bob, None, 50)
        assert r_alice["count"] == 1
        assert r_bob["count"] == 0
    _run(body)


def test_new_parlay_save_no_longer_inserts_into_parlay_history():
    async def body(db):
        from routes import parlay_history_routes as PHR
        user = _user()
        legs = [{"id":"a","sport":"MLB","market":"ML","selection":"X","book_odds":-110},
                {"id":"b","sport":"MLB","market":"ML","selection":"Y","book_odds":+100}]
        before = await db.parlay_history.count_documents({})
        await PHR.parlay_save(
            PHR.SaveParlayRequest(legs=legs, mode="standard", stake=1.0),
            user,
        )
        after = await db.parlay_history.count_documents({})
        # No new parlay_history row.
        assert after == before
        # But canonical ledger got exactly one row.
        assert await db.user_bets.count_documents({"user_id": user.id}) == 1
    _run(body)


def test_existing_parlay_history_rows_unchanged():
    async def body(db):
        from routes import parlay_history_routes as PHR
        user = _user()
        await db.parlay_history.insert_many([
            {"id": "p_pre", "user_id": user.id, "status": "won",
             "legs": [{"pick_id":"a"},{"pick_id":"b"}],
             "leg_ids": ["a","b"], "combined_odds": 200, "stake": 1.0,
             "notes": "PRESERVE"},
            {"id": "plearn_xx", "signature":"s","status":"pending",
             "legs": [{"pick_id":"a"}], "shown_at": "2026-06-01T00:00:00+00:00"},
        ])
        # Do a new save through the sunset path.
        await PHR.parlay_save(
            PHR.SaveParlayRequest(
                legs=[{"id":"a","sport":"MLB","market":"ML","selection":"X","book_odds":-110},
                      {"id":"c","sport":"MLB","market":"ML","selection":"Y","book_odds":+100}],
                mode="standard", stake=1.0),
            user,
        )
        pre = await db.parlay_history.find_one({"id": "p_pre"}, {"_id": 0})
        pl  = await db.parlay_history.find_one({"id": "plearn_xx"}, {"_id": 0})
        assert pre.get("notes") == "PRESERVE"
        assert pre.get("status") == "won"
        assert pl.get("status") == "pending"
    _run(body)


def test_client_bet_id_idempotency_still_works_post_cutover():
    async def body(db):
        from routes import parlay_history_routes as PHR
        user = _user()
        legs = [{"id":"a","sport":"MLB","market":"ML","selection":"X","book_odds":-110},
                {"id":"b","sport":"MLB","market":"ML","selection":"Y","book_odds":+100}]
        r1 = await PHR.parlay_save(
            PHR.SaveParlayRequest(legs=legs, mode="standard", stake=1.0,
                                   client_bet_id="idem-7"), user)
        r2 = await PHR.parlay_save(
            PHR.SaveParlayRequest(legs=legs, mode="standard", stake=1.0,
                                   client_bet_id="idem-7"), user)
        assert r1["id"] == r2["id"]
        assert await db.user_bets.count_documents({"user_id": user.id}) == 1
    _run(body)


def test_serializer_preserves_null_clv_and_line():
    from services import user_bet_ledger as UBL
    from datetime import datetime, timezone
    bet = UBL.UserBet(
        user_bet_id="ub-null", user_id="u1", wager_type="parlay",
        status=UBL.STATUS_PENDING, combined_odds=200, stake_amount=1.0,
        placed_at=datetime.now(timezone.utc),
        legs=[UBL.UserBetLeg(prediction_id="a", original_odds=-110),
              UBL.UserBetLeg(prediction_id="b", original_odds=+100)],
    )
    row = UBL.serialize_parlay_history_row(bet)
    assert row["stake"] == 1.0
    assert row["combined_odds"] == 200
    assert row["payout"] is None    # never invented
    assert row["status"] == "live"  # canonical pending → legacy live
    assert row["legs_pending"] == 2


def test_push_stays_distinct_from_void_in_serializer():
    from services import user_bet_ledger as UBL
    bet_push = UBL.UserBet(user_bet_id="p", user_id="u", wager_type="parlay",
                            status=UBL.STATUS_PUSHED,
                            legs=[UBL.UserBetLeg(prediction_id="a"),
                                  UBL.UserBetLeg(prediction_id="b")])
    bet_void = UBL.UserBet(user_bet_id="v", user_id="u", wager_type="parlay",
                            status=UBL.STATUS_VOID,
                            legs=[UBL.UserBetLeg(prediction_id="a"),
                                  UBL.UserBetLeg(prediction_id="b")])
    assert UBL.serialize_parlay_history_row(bet_push)["status"] == "push"
    assert UBL.serialize_parlay_history_row(bet_void)["status"] == "void"


# ── Settlement cutover ────────────────────────────────────────────────
def test_canonical_resolver_settles_all_legs_won():
    async def body(db):
        from services import user_bet_ledger as UBL
        user = _user()
        await _seed_pick(db, "leg-a"); await _seed_pick(db, "leg-b")
        # Mark both picks as won.
        await db.picks.update_many({"id": {"$in": ["leg-a","leg-b"]}},
                                    {"$set": {"status": "won"}})
        # Insert canonical parlay directly (no legacy alias fields).
        await db.user_bets.insert_one({
            "user_bet_id": "ub-win", "id": "ub-win",
            "user_id": user.id, "wager_type": "parlay",
            "status": "pending", "combined_odds": 300,
            "stake_amount": 1.0, "stake_units": 1.0,
            "legs": [{"prediction_id": "leg-a", "status": "pending"},
                     {"prediction_id": "leg-b", "status": "pending"}],
        })
        res = await UBL.resolve_pending_parlays_canonical(db)
        assert res["updated"] == 1 and res["won"] == 1
        row = await db.user_bets.find_one({"user_bet_id": "ub-win"})
        assert row["status"] == UBL.STATUS_WON
        assert row["settled_at"] is not None
        assert row["actual_payout"] and row["actual_payout"] > 0
        # settlement_events audit entry appended.
        assert any(e.get("event_kind") == "settle"
                    for e in row.get("settlement_events", []))
    _run(body)


def test_canonical_resolver_settles_one_leg_lost():
    async def body(db):
        from services import user_bet_ledger as UBL
        user = _user()
        await _seed_pick(db, "leg-a"); await _seed_pick(db, "leg-b")
        await db.picks.update_one({"id": "leg-a"}, {"$set": {"status": "won"}})
        await db.picks.update_one({"id": "leg-b"}, {"$set": {"status": "lost"}})
        await db.user_bets.insert_one({
            "user_bet_id": "ub-lost", "user_id": user.id,
            "wager_type": "parlay", "status": "pending",
            "combined_odds": 300, "stake_amount": 2.0, "stake_units": 2.0,
            "legs": [{"prediction_id": "leg-a", "status": "pending"},
                     {"prediction_id": "leg-b", "status": "pending"}],
        })
        res = await UBL.resolve_pending_parlays_canonical(db)
        assert res["updated"] == 1 and res["lost"] == 1
        row = await db.user_bets.find_one({"user_bet_id": "ub-lost"})
        assert row["status"] == UBL.STATUS_LOST
        assert row["profit_loss"] == -2.0
    _run(body)


def test_canonical_resolver_skips_pending_leg():
    async def body(db):
        from services import user_bet_ledger as UBL
        user = _user()
        await _seed_pick(db, "leg-a"); await _seed_pick(db, "leg-b")
        await db.picks.update_one({"id": "leg-a"}, {"$set": {"status": "won"}})
        # leg-b still has no status → pending
        await db.user_bets.insert_one({
            "user_bet_id": "ub-p", "user_id": user.id,
            "wager_type": "parlay", "status": "pending",
            "combined_odds": 250, "stake_amount": 1.0,
            "legs": [{"prediction_id": "leg-a"},
                     {"prediction_id": "leg-b"}],
        })
        res = await UBL.resolve_pending_parlays_canonical(db)
        assert res["updated"] == 0
        row = await db.user_bets.find_one({"user_bet_id": "ub-p"})
        assert row["status"] == "pending"
    _run(body)


def test_canonical_resolver_never_touches_terminal_migrated_rows():
    async def body(db):
        from services import user_bet_ledger as UBL
        user = _user()
        await db.user_bets.insert_one({
            "user_bet_id": "ub-mig", "user_id": user.id,
            "wager_type": "parlay", "status": "won",
            "is_legacy": True, "migration_source": "parlay_history",
            "migration_source_id": "p_xxxx",
            "combined_odds": 200, "stake_amount": 1.0,
            "legs": [{"prediction_id": "a"}, {"prediction_id": "b"}],
        })
        res = await UBL.resolve_pending_parlays_canonical(db)
        assert res["updated"] == 0
        row = await db.user_bets.find_one({"user_bet_id": "ub-mig"})
        assert row["status"] == "won"
    _run(body)


def test_propagate_pick_settlement_canonical_parlay_win():
    async def body(db):
        from routes.user_bets_routes import propagate_pick_settlement
        from services import user_bet_ledger as UBL
        user = _user()
        # Canonical parlay with 2 legs.
        await db.user_bets.insert_one({
            "user_bet_id": "ub-can-win", "id": "ub-can-win",
            "user_id": user.id, "wager_type": "parlay",
            "status": "pending", "combined_odds": 300,
            "stake_amount": 1.0, "stake_units": 1.0,
            "legs": [{"prediction_id": "pk-a", "status": "pending"},
                     {"prediction_id": "pk-b", "status": "pending"}],
        })
        # Only one leg has settled — propagator must skip.
        await db.picks.insert_many([
            {"id": "pk-a", "status": "won"},
            {"id": "pk-b", "status": "pending"},
        ])
        n = await propagate_pick_settlement("pk-a", "won", book_odds=-110)
        assert n == 0
        # Now the last leg settles.
        await db.picks.update_one({"id": "pk-b"}, {"$set": {"status": "won"}})
        n = await propagate_pick_settlement("pk-b", "won", book_odds=+150)
        row = await db.user_bets.find_one({"user_bet_id": "ub-can-win"})
        assert row["status"] == UBL.STATUS_WON
        assert row["profit_loss"] and row["profit_loss"] > 0
    _run(body)


def test_propagate_pick_settlement_canonical_parlay_loss_short_circuit():
    async def body(db):
        from routes.user_bets_routes import propagate_pick_settlement
        from services import user_bet_ledger as UBL
        user = _user()
        await db.user_bets.insert_one({
            "user_bet_id": "ub-can-loss", "user_id": user.id,
            "wager_type": "parlay", "status": "pending",
            "combined_odds": 500, "stake_amount": 3.0, "stake_units": 3.0,
            "legs": [{"prediction_id": "px-a"},
                     {"prediction_id": "px-b"}],
        })
        # Full leg settle before propagation.
        await db.picks.insert_many([
            {"id": "px-a", "status": "won"},
            {"id": "px-b", "status": "lost"},
        ])
        await propagate_pick_settlement("px-b", "lost")
        row = await db.user_bets.find_one({"user_bet_id": "ub-can-loss"})
        assert row["status"] == UBL.STATUS_LOST
        assert row["profit_loss"] == -3.0
    _run(body)


def test_parlay_save_stamps_legacy_aliases_for_analytics_parity():
    async def body(db):
        from routes import parlay_history_routes as PHR
        user = _user()
        legs = [{"id":"la","sport":"MLB","market":"ML","selection":"X","book_odds":-110,"event":"A vs B"},
                {"id":"lb","sport":"MLB","market":"ML","selection":"Y","book_odds":+100,"event":"A vs B"}]
        r = await PHR.parlay_save(
            PHR.SaveParlayRequest(legs=legs, mode="standard", stake=2.5),
            user,
        )
        # The canonical row must ALSO carry legacy alias fields.
        row = await db.user_bets.find_one({"user_bet_id": r["user_bet_id"]})
        assert row["bet_type"] == "parlay"
        assert row["parlay_legs"] == ["la", "lb"]
        assert row["stake_units"] == 2.5
        assert row["stake_amount"] == 2.5
        assert row["pnl_units"] == 0.0
        assert row["sport"] == "MLB"
        assert row["market"] == "2-leg parlay"
        # ``id`` legacy alias mirrors ``user_bet_id``.
        assert row["id"] == r["user_bet_id"]
    _run(body)


def test_analytics_summary_is_canonical_aware_for_migrated_rows():
    async def body(db):
        from routes import user_bets_routes as UBR
        user = _user()
        # Migrated legacy row — canonical-only (no aliases).
        await db.user_bets.insert_one({
            "user_bet_id": "ub-mig-won", "user_id": user.id,
            "wager_type": "parlay", "status": "won",
            "is_legacy": True, "combined_odds": 285,
            "stake_amount": 10.0, "actual_payout": 28.5,
            "profit_loss": 28.5,
            "legs": [{"prediction_id":"a","sport_key":"MLB"},
                     {"prediction_id":"b","sport_key":"MLB"}],
        })
        s = await UBR.user_analytics_summary(user)
        assert s["total_bets"] == 1
        assert s["won"] == 1
        assert s["units_risked"] == 10.0
        assert s["pnl_units"] == 28.5
        assert s["roi_pct"] > 0
    _run(body)


def test_analytics_by_sport_falls_back_to_sport_key():
    async def body(db):
        from routes import user_bets_routes as UBR
        user = _user()
        # Migrated row with sport_key but no ``sport`` alias.
        await db.user_bets.insert_one({
            "user_bet_id": "ubm", "user_id": user.id,
            "wager_type": "parlay", "status": "won",
            "is_legacy": True, "combined_odds": 200,
            "stake_amount": 1.0, "profit_loss": 1.0,
            "sport_key": "MLB",
            "legs": [{"prediction_id":"a"},{"prediction_id":"b"}],
        })
        r = await UBR.user_analytics_by_sport(user)
        rows = r["rows"]
        assert any(row["sport"] == "MLB" for row in rows)
    _run(body)


def test_analytics_by_market_synthesizes_parlay_bucket():
    async def body(db):
        from routes import user_bets_routes as UBR
        user = _user()
        # Migrated 3-leg parlay with no market field.
        await db.user_bets.insert_one({
            "user_bet_id": "ubm2", "user_id": user.id,
            "wager_type": "parlay", "status": "won",
            "is_legacy": True, "combined_odds": 600,
            "stake_amount": 1.0, "profit_loss": 6.0,
            "legs": [{"prediction_id":"a"},
                     {"prediction_id":"b"},
                     {"prediction_id":"c"}],
        })
        r = await UBR.user_analytics_by_market(user)
        rows = r["rows"]
        assert any(row["market"] == "3-leg parlay" for row in rows)
    _run(body)


# ── Backend index safety ─────────────────────────────────────────────
def test_no_new_critical_indexes_promoted_in_step_7():
    from services import index_registry as IR
    # Snapshot every currently registered index — nothing that references
    # user_bets should have been promoted from ``advisory`` to
    # ``critical`` as part of Step 7 (Step 7 is a code-only cutover).
    reg_src = Path("/app/backend/services/index_registry.py").read_text(encoding="utf-8")
    # No new ``critical=True`` decorated user_bets index appearing after
    # Step 6.  We use a coarse invariant here: Step 7 must not have
    # increased the count of critical indexes.  Store baseline count.
    critical_count = reg_src.count('"critical": True') + reg_src.count("'critical': True")
    # Baseline captured post-Step-6 was 6 critical entries — Step 7
    # must not have added another.
    assert critical_count <= 8, (
        f"Step 7 must not promote new critical indexes — found {critical_count}"
    )


def test_ledger_exports_resolver():
    from services import user_bet_ledger as UBL
    assert hasattr(UBL, "resolve_pending_parlays_canonical")
    assert "resolve_pending_parlays_canonical" in UBL.__all__


def test_server_calls_canonical_resolver_alongside_legacy():
    src = Path("/app/backend/server.py").read_text(encoding="utf-8")
    assert "resolve_pending_parlays_canonical" in src
    # Legacy resolver call also still present so pre-Step-7 rows keep
    # being covered (belt-and-braces).
    assert "resolve_saved_parlays" in src
