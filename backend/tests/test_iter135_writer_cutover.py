"""Phase 3G Step 6 — canonical writer cutover tests (test_iter135).

Approach: call the route-handler *functions* directly (not through
FastAPI's HTTP layer) inside a per-test ``asyncio.run`` loop, so each
test uses a freshly-scoped Motor client bound to that loop.  This
avoids the "Event loop is closed" cross-loop conflict that plagues
TestClient + Motor override.

The static checks that don't need I/O are kept as top-level tests.
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


# ── Static assertions (no DB required) ────────────────────────────────
def test_track_route_imports_user_bet_ledger():
    src = Path("/app/backend/routes/user_bets_routes.py").read_text(encoding="utf-8")
    assert "from services import user_bet_ledger" in src
    assert "UBL.create_bet" in src
    assert "UBL.create_parlay" in src


def test_parlay_save_route_imports_user_bet_ledger():
    src = Path("/app/backend/routes/parlay_history_routes.py").read_text(encoding="utf-8")
    assert "user_bet_ledger" in src
    assert "_UBL.create_parlay" in src
    # Phase 3G Step 7 sunset the parlay_history mirror — the source
    # must no longer reference the mirror ``source`` marker.  See
    # ``test_iter136_reader_settlement_cutover.py::test_parlay_save_mirror_is_sunset``
    # for the canonical Step 7 invariant.
    assert "user_bet_ledger_mirror" not in src


def test_no_direct_user_bets_inserts_in_user_routes():
    for f in Path("/app/backend/routes").glob("*_routes.py"):
        src = f.read_text(encoding="utf-8")
        for bad in ("db.user_bets.insert_one(", "db.user_bets.insert_many(",
                    'db["user_bets"].insert_one(', 'db["user_bets"].insert_many('):
            assert bad not in src, f"{f.name} still contains direct write: {bad}"


def test_track_bet_request_accepts_client_bet_id():
    from routes.user_bets_routes import TrackBetRequest
    assert TrackBetRequest(pick_id="p1").client_bet_id is None
    assert TrackBetRequest(pick_id="p1", client_bet_id="x").client_bet_id == "x"


def test_save_parlay_request_accepts_client_bet_id():
    from routes.parlay_history_routes import SaveParlayRequest
    assert SaveParlayRequest(legs=[{}, {}]).client_bet_id is None
    assert SaveParlayRequest(legs=[{}, {}], client_bet_id="y").client_bet_id == "y"


def test_settlement_code_untouched_in_step_6():
    src = Path("/app/backend/routes/user_bets_routes.py").read_text(encoding="utf-8")
    assert "propagate_pick_settlement" in src


# ── Runtime harness — direct handler invocation ──────────────────────
def _run(coro):
    async def wrapper():
        c = AsyncIOMotorClient(MONGO_URL)
        db_name = f"step6_test_{uuid.uuid4().hex[:12]}"
        db = c[db_name]
        from services.database import (
            override_database_for_testing, reset_database_override,
        )
        override_database_for_testing(c, db)
        # Ensure partial-unique indexes exist so the concurrent-race
        # test observes the same protection the production DB has.
        from services import index_registry as IR
        try:
            await IR.ensure_all_indexes(db)
        except Exception:
            pass
        # Route modules captured a `db` reference at import time via
        # `from deps import ... db`.  Monkey-patch those bindings so
        # they use the freshly-overridden test DB.
        import deps
        deps.db = db
        try:
            from routes import user_bets_routes, parlay_history_routes
            user_bets_routes.db      = db
            parlay_history_routes.db = db
        except Exception:
            pass
        try:
            await coro(db)
        finally:
            try: await c.drop_database(db_name)
            except Exception: pass
            reset_database_override()
            c.close()
    asyncio.run(wrapper())


def _fake_user(uid=None, email="u@example.com"):
    return SimpleNamespace(
        id=(uid or str(uuid.uuid4())),
        email=email,
        role="user",
        status="active",
    )


async def _seed_pick(db, pick_id="pick-1", sport="MLB", odds=-110):
    await db.picks.insert_one({
        "id": pick_id, "sport": sport, "market": "Moneyline",
        "selection": "X", "event": "X vs Y", "book_odds": int(odds),
    })


# ── 4/5. Optional client_bet_id + same-user idempotent ───────────────
def test_track_bet_client_bet_id_idempotency():
    async def body(db):
        from routes import user_bets_routes as UBR
        await _seed_pick(db, "pick-idem")
        user = _fake_user()
        req  = UBR.TrackBetRequest(pick_id="pick-idem", bet_type="straight",
                                   stake_units=1.0, client_bet_id="tap-abc")
        r1 = await UBR.track_bet(req, user)
        r2 = await UBR.track_bet(req, user)
        assert r1["id"] == r2["id"]
        for k in ("id","user_id","pick_id","bet_type","stake_units",
                  "odds_at_bet","status","pnl_units","sport","market",
                  "event","selection","created_at","notes"):
            assert k in r1, f"missing legacy key: {k}"
        assert await db.user_bets.count_documents({"user_id": user.id}) == 1
    _run(body)


# ── 6. Different users may reuse client_bet_id ───────────────────────
def test_client_bet_id_is_user_scoped():
    async def body(db):
        from routes import user_bets_routes as UBR
        await _seed_pick(db, "pick-scoped")
        u1 = _fake_user()
        u2 = _fake_user()
        req = UBR.TrackBetRequest(pick_id="pick-scoped", stake_units=1.0,
                                  client_bet_id="same")
        r1 = await UBR.track_bet(req, u1)
        r2 = await UBR.track_bet(req, u2)
        assert r1["id"] != r2["id"]
        assert r1["user_id"] != r2["user_id"]
    _run(body)


# ── 7. Concurrent duplicates: gather 5 identical calls ───────────────
def test_concurrent_duplicate_track_creates_one_wager():
    async def body(db):
        from routes import user_bets_routes as UBR
        await _seed_pick(db, "pick-conc")
        user = _fake_user()
        req = UBR.TrackBetRequest(pick_id="pick-conc", stake_units=1.0,
                                  client_bet_id="conc-tap")
        results = await asyncio.gather(*[UBR.track_bet(req, user) for _ in range(5)])
        assert len({r["id"] for r in results}) == 1
        assert await db.user_bets.count_documents({"user_id": user.id}) == 1
    _run(body)


# ── 8. Fallback idempotency without client_bet_id ────────────────────
def test_fallback_idempotency_without_client_bet_id():
    async def body(db):
        from routes import user_bets_routes as UBR
        await _seed_pick(db, "pick-fbid")
        user = _fake_user()
        req = UBR.TrackBetRequest(pick_id="pick-fbid", stake_units=1.0)
        r1 = await UBR.track_bet(req, user)
        r2 = await UBR.track_bet(req, user)
        assert r1["id"] == r2["id"]
        assert await db.user_bets.count_documents({"user_id": user.id}) == 1
    _run(body)


# ── 10. Different bet-time odds → distinct wagers ────────────────────
def test_different_odds_remain_distinct_wagers():
    async def body(db):
        from routes import user_bets_routes as UBR
        await _seed_pick(db, "pick-o1", odds=-110)
        await _seed_pick(db, "pick-o2", odds=+150)
        user = _fake_user()
        r1 = await UBR.track_bet(UBR.TrackBetRequest(pick_id="pick-o1", stake_units=1.0), user)
        r2 = await UBR.track_bet(UBR.TrackBetRequest(pick_id="pick-o2", stake_units=1.0), user)
        assert r1["id"] != r2["id"]
        assert await db.user_bets.count_documents({"user_id": user.id}) == 2
    _run(body)


# ── 11. plearn_* rows never enter user_bets via the route ────────────
def test_plearn_rows_never_enter_user_bets_via_writer():
    async def body(db):
        from routes import user_bets_routes as UBR
        await db.parlay_history.insert_one({
            "id": "plearn_x1", "signature": "s", "leg_count": 2,
            "legs": [{"pick_id": "a"}, {"pick_id": "b"}],
            "status": "pending", "shown_at": "2026-06-01T00:00:00+00:00",
        })
        await _seed_pick(db, "pick-pl")
        user = _fake_user()
        await UBR.track_bet(UBR.TrackBetRequest(pick_id="pick-pl", stake_units=1.0), user)
        assert await db.user_bets.count_documents(
            {"migration_source_id": {"$regex": "^plearn_"}}) == 0
        pl = await db.parlay_history.find_one({"id": "plearn_x1"}, {"_id": 0})
        assert pl["status"] == "pending"
    _run(body)


# ── 12. Old-shape request still accepted ─────────────────────────────
def test_old_track_bet_request_still_works():
    async def body(db):
        from routes import user_bets_routes as UBR
        await _seed_pick(db, "pick-old")
        user = _fake_user()
        r = await UBR.track_bet(
            UBR.TrackBetRequest(pick_id="pick-old", bet_type="straight",
                                 stake_units=1.5),
            user,
        )
        for k in ("id","user_id","pick_id","bet_type","stake_units",
                  "odds_at_bet","status","pnl_units","sport","market",
                  "event","selection","created_at"):
            assert k in r
    _run(body)


# ── 13. Response shape parity ────────────────────────────────────────
def test_track_bet_response_shape_stable():
    async def body(db):
        from routes import user_bets_routes as UBR
        await _seed_pick(db, "pick-shape")
        user = _fake_user()
        r = await UBR.track_bet(
            UBR.TrackBetRequest(pick_id="pick-shape", stake_units=1.0),
            user,
        )
        assert r["status"] == "pending" and r["pnl_units"] == 0.0
        assert r["bet_type"] == "straight" and r["stake_units"] == 1.0
        assert r["pick_id"] == "pick-shape" and r["odds_at_bet"] == -110
        assert r.get("wager_type") == "straight"
        assert r.get("is_legacy") is False
    _run(body)


# ── 17. Compatibility mirror was sunset in Step 7 ────────────────────
def test_parlay_save_mirror_is_idempotent():
    """Phase 3G Step 7 sunset the parlay_history mirror.  Prior
    (Step 6) semantics required the second call to insert exactly one
    mirror row; the current (Step 7) invariant is that **zero** mirror
    rows are ever inserted, and the canonical ledger row remains
    idempotent by ``client_bet_id``."""
    async def body(db):
        from routes import parlay_history_routes as PHR
        user = _fake_user()
        legs = [
            {"id": "leg-1", "sport": "MLB", "market": "ML", "selection": "X",
             "book_odds": -110, "event": "X vs Y"},
            {"id": "leg-2", "sport": "MLB", "market": "ML", "selection": "Z",
             "book_odds": +100, "event": "Z vs W"},
        ]
        req = PHR.SaveParlayRequest(legs=legs, mode="standard", stake=1.0,
                                     client_bet_id="mirror-tap")
        r1 = await PHR.parlay_save(req, user)
        r2 = await PHR.parlay_save(req, user)
        for k in ("id","user_id","legs","leg_ids","combined_odds","stake","status"):
            assert k in r1
        # Canonical ledger idempotency preserved.
        assert await db.user_bets.count_documents({"user_id": user.id}) == 1
        # Mirror sunset — NO parlay_history rows for this user.
        n_mirror = await db.parlay_history.count_documents(
            {"user_id": user.id, "source": "user_bet_ledger_mirror"})
        assert n_mirror == 0
        n_any_ph = await db.parlay_history.count_documents({"user_id": user.id})
        assert n_any_ph == 0
    _run(body)


# ── 14/15. Existing native + migrated rows unchanged ─────────────────
def test_existing_native_and_migrated_rows_unchanged_after_track():
    async def body(db):
        from routes import user_bets_routes as UBR
        await db.user_bets.insert_many([
            {"id": "native-A", "user_bet_id": "native-A",
             "user_id": "u-existing-native", "wager_type": "straight",
             "is_legacy": False, "notes": "keep-me"},
            {"id": "mig-B", "user_bet_id": "mig-B",
             "user_id": "u-migrated", "wager_type": "parlay",
             "is_legacy": True, "migration_source": "parlay_history",
             "migration_source_id": "p_x1", "migration_version": 1,
             "notes": "keep-me-too"},
        ])
        await _seed_pick(db, "pick-un")
        user = _fake_user()
        await UBR.track_bet(UBR.TrackBetRequest(pick_id="pick-un", stake_units=1.0), user)
        native = await db.user_bets.find_one({"user_bet_id": "native-A"}, {"_id": 0})
        mig    = await db.user_bets.find_one({"user_bet_id": "mig-B"}, {"_id": 0})
        assert native["notes"] == "keep-me"
        assert mig["notes"] == "keep-me-too"
        assert mig["migration_source"] == "parlay_history"
    _run(body)


# ── 16. Learning rows unchanged after new parlay save ────────────────
def test_learning_rows_unchanged_after_writes():
    async def body(db):
        from routes import parlay_history_routes as PHR
        await db.parlay_history.insert_one({
            "id": "plearn_untouched", "signature": "s",
            "legs": [{"pick_id": "a"}, {"pick_id": "b"}],
            "status": "pending", "shown_at": "2026-06-01T00:00:00+00:00",
        })
        user = _fake_user()
        legs = [
            {"id": "leg-a", "sport": "MLB", "market": "ML", "selection": "X",
             "book_odds": -110, "event": "X"},
            {"id": "leg-b", "sport": "MLB", "market": "ML", "selection": "Y",
             "book_odds": +100, "event": "Y"},
        ]
        await PHR.parlay_save(
            PHR.SaveParlayRequest(legs=legs, mode="standard", stake=1.0),
            user,
        )
        pl = await db.parlay_history.find_one({"id": "plearn_untouched"}, {"_id": 0})
        assert pl["status"] == "pending"
        assert pl.get("source") != "user_bet_ledger_mirror"
        assert "user_bet_id" not in pl
    _run(body)


# ── 18. Prediction snapshots never written by Step 6 ─────────────────
def test_prediction_snapshots_never_written_by_step_6():
    async def body(db):
        from routes import user_bets_routes as UBR
        await db.prediction_snapshots.insert_one(
            {"prediction_id": "p1", "snapshot_version": 1})
        await _seed_pick(db, "pick-snap")
        user = _fake_user()
        before = await db.prediction_snapshots.count_documents({})
        await UBR.track_bet(UBR.TrackBetRequest(pick_id="pick-snap", stake_units=1.0), user)
        after = await db.prediction_snapshots.count_documents({})
        assert before == after
    _run(body)
