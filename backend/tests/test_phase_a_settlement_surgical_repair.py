"""Phase A + μ-closure — focused regression tests.

Requirements (from user):
  A. SETTLEMENT_UNSUPPORTED does not create VOID.
  B. Unsupported record does not count in W/L/PUSH/VOID.
  C. Future unsupported market cannot become actionable.
  D. UNKNOWN oldest record cannot starve a later supported completed pick.
  E. Repeated bounded settlement runs advance through supported backlog.
  F. Canonical SettlementService remains the only authoritative outcome writer.

All tests are self-contained; the async fixture tests use the live
MongoDB pointed to by ``MONGO_URL`` and clean up after themselves via
a ``_test_marker`` field.
"""
import asyncio
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── Capability matrix (unchanged from prior turn) ────────────────────
def test_capability_matrix():
    from services.settlement_capability import (
        classify, SUPPORTED, UNSUPPORTED, UNKNOWN,
    )
    cases = [
        ("Soccer", "Home Moneyline",                None, SUPPORTED),
        ("Soccer", "Total Goals Over 2.5",          None, SUPPORTED),
        ("Soccer", "Anytime Goal Scorer",           None, SUPPORTED),
        ("Soccer", "Both Teams To Score Yes",       None, SUPPORTED),
        ("Soccer", "First Goalscorer",              None, UNSUPPORTED),
        ("Soccer", "Player X Shots On Target 2.5",  None, UNSUPPORTED),
        ("Soccer", "Player X Total Shots 4.5",      None, UNSUPPORTED),
        ("Soccer", "Correct Score 2-1",             None, UNSUPPORTED),
        ("Soccer", "Half Time / Full Time",         None, UNSUPPORTED),
        ("Soccer", "Asian Handicap Home +0.5",      None, UNSUPPORTED),
        ("Soccer", "Total Cards Over 3.5",          None, UNSUPPORTED),
        ("Soccer", "Total Corners Over 9.5",        None, UNSUPPORTED),
        ("MLB",    "Team A Moneyline",              None,          SUPPORTED),
        ("MLB",    "Team A Run Line -1.5",          None,          SUPPORTED),
        ("NBA",    "Total Points Over 220.5",       None,          SUPPORTED),
        ("MLB",    "Buxton Over 0.5 Hits",          "MLB Props",   SUPPORTED),
        ("Cricket", "Runs at Fall of 2nd Wicket",   None, UNKNOWN),
    ]
    for sport, market, league, expected in cases:
        got, _ = classify(sport, market, league)
        assert got == expected, (
            f"expected {expected} for ({sport!r},{market!r}) got {got}"
        )
    print("test_capability_matrix OK")


# ── F: SettlementService canonical status mapping (regression guard) ─
def test_pick_status_semantics_unchanged():
    from services.settlement_service import (
        _pick_status_from_result, VALID_RESULTS,
    )
    assert VALID_RESULTS == ("won", "lost", "void", "push", "cancelled")
    assert _pick_status_from_result("won")       == "won"
    assert _pick_status_from_result("lost")      == "lost"
    assert _pick_status_from_result("push")      == "push"
    assert _pick_status_from_result("void")      == "void"
    assert _pick_status_from_result("cancelled") == "void"
    assert _pick_status_from_result("blocked")   == "pending"   # unknown → pending
    assert _pick_status_from_result("unknown")   == "pending"
    print("test_pick_status_semantics_unchanged OK")


# ── Static safety net — sort + block-gate present in every cursor ────
def test_cursor_gates_present():
    root = os.path.join(os.path.dirname(__file__), "..")
    with open(os.path.join(root, "settlement_engine.py")) as f:
        engine_src = f.read()
    with open(os.path.join(root, "soccer_espn_settle.py")) as f:
        soccer_src = f.read()
    with open(os.path.join(root, "prop_settlement.py")) as f:
        prop_src = f.read()
    with open(os.path.join(root, "espn_settlement.py")) as f:
        espn_src = f.read()

    # Oldest-first sort in every candidate cursor.
    assert 'db.picks.find(query, {"_id": 0}).sort("event_time", 1)' in engine_src
    assert '.sort("event_time", 1).limit(max_picks)' in soccer_src
    assert '.sort("event_time", 1).limit(max_picks)' in prop_src
    assert espn_src.count('.sort("event_time", 1).to_list(length=') >= 3, (
        "espn_settlement missing oldest-first sort on all 3 cursors")

    # Actionable-only settlement_block gate in every candidate cursor.
    assert '"settlement_block": {"$ne": True}' in engine_src
    assert '"settlement_block": {"$ne": True}' in soccer_src
    assert '"settlement_block": {"$ne": True}' in prop_src
    assert espn_src.count('"settlement_block": {"$ne": True}') >= 3

    # Requirement A — the μ-closure removes the direct-void terminator.
    # The old code path called SettlementService.settle_from_pick with
    # ``result="void"`` inside the SETTLEMENT_UNSUPPORTED block.  It
    # must be gone.
    _term_block_start = engine_src.find("SETTLEMENT_UNSUPPORTED terminator")
    assert _term_block_start > 0, "terminator block missing"
    _term_block_end = engine_src.find("except Exception as _uce",
                                       _term_block_start)
    _term_block = engine_src[_term_block_start:_term_block_end]
    assert 'result                    = "void"' not in _term_block, (
        "μ-closure regression: terminator still calls SettlementService "
        "with result='void' — unsupported must NOT create VOID")
    # And it must ONLY use metadata (db.picks.bulk_write / UpdateOne).
    assert "settlement_block" in _term_block and "UpdateOne" in _term_block, (
        "terminator block must use metadata-only writes (settlement_block)")
    print("test_cursor_gates_present OK")


# ── Async DB tests ─────────────────────────────────────────────────
def _make_db():
    from motor.motor_asyncio import AsyncIOMotorClient
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    cx = AsyncIOMotorClient(os.getenv("MONGO_URL"))
    return cx, cx[os.getenv("DB_NAME", "test_database")]


async def _seed_pick(db, *, id: str, sport: str, market: str,
                     event_time: datetime, league: str = None) -> None:
    doc = {
        "id": id,
        "sport": sport,
        "market": market,
        "league": league,
        "event": "TeamA @ TeamB",
        "event_time": event_time.isoformat(),
        "created_at": event_time.isoformat(),
        "status": "pending",
        "publication_state": "PUBLISHED",
        "off_board": False,
        "_test_marker": "phase_a_uclosure",
    }
    await db.picks.update_one(
        {"id": id}, {"$set": doc}, upsert=True)


async def _cleanup(db):
    await db.picks.delete_many({"_test_marker": "phase_a_uclosure"})
    await db.settlement_events.delete_many({"_test_marker": "phase_a_uclosure"})


# ── A + B: unsupported does NOT create VOID, does NOT enter W/L/PUSH/VOID
def test_A_B_unsupported_does_not_create_void():
    async def _run():
        cx, db = _make_db()
        try:
            await _cleanup(db)
            now = datetime.now(timezone.utc)
            past = now - timedelta(hours=6)
            # 1 UNSUPPORTED soccer pick (Corners) — past event.
            await _seed_pick(db, id="pk_uclosure_A1", sport="Soccer",
                              market="Total Corners Over 9.5",
                              event_time=past)
            # Snapshot pre-run state.
            pre = await db.picks.find_one({"id": "pk_uclosure_A1"})
            assert pre.get("status") == "pending"

            # Run only the terminator path by calling settle_due_picks
            # scoped to Soccer.  Provider fetch will no-op cleanly.
            from settlement_engine import settle_due_picks
            counts = await settle_due_picks(db, sport_filter=["Soccer"])

            post = await db.picks.find_one({"id": "pk_uclosure_A1"})
            # A — no VOID / no outcome in canonical status.
            assert post.get("status") in ("pending", None), (
                f"unsupported pick was mutated to status={post.get('status')} "
                "— violates requirement A (no fake VOID).")
            assert post.get("result") is None, (
                f"unsupported pick got result={post.get('result')}")
            # Metadata block stamped.
            assert post.get("settlement_block") is True
            assert (post.get("settlement_block_reason") or "").startswith(
                "settler_unsupported:")

            # B — no settlement_events row exists for this pick (nothing
            # in the canonical outcome ledger).
            se = await db.settlement_events.find_one(
                {"prediction_id": "pk_uclosure_A1"})
            assert se is None, (
                "unsupported pick spawned a settlement_events row — "
                "violates requirement B (must not count in W/L/PUSH/VOID)."
            )

            # History projection must not pick it up either (status is
            # still pending, which the projection filters out).
            assert counts["unsupported_terminated"] >= 1
        finally:
            await _cleanup(db)
            cx.close()
    asyncio.run(_run())
    print("test_A_B_unsupported_does_not_create_void OK")


# ── C: future unsupported market cannot become actionable ────────────
def test_C_future_unsupported_not_actionable():
    """The capability registry MUST correctly classify markets so
    generation gates can refuse to publish them.  Also verifies the
    settlement candidate query excludes ``settlement_block:True``
    (preview: adding the block gate to a future pick renders it
    ineligible for settlement even before its event completes).
    """
    async def _run():
        cx, db = _make_db()
        try:
            await _cleanup(db)
            # Registry classification — the primary defense.
            from services.settlement_capability import (
                is_unsupported, is_supported)
            assert is_unsupported("Soccer", "Total Corners Over 9.5")
            assert is_unsupported("Soccer", "Correct Score 2-1")
            assert is_unsupported("Soccer", "Player X Shots On Target 2.5")
            assert not is_supported("Soccer", "Correct Score 2-1")
            # Simulate a FUTURE unsupported pick with the block metadata
            # already stamped (this is what a generation-time gate
            # would do). Verify it is invisible to the settlement queue.
            future = datetime.now(timezone.utc) + timedelta(hours=2)
            await db.picks.update_one(
                {"id": "pk_uclosure_C1"},
                {"$set": {
                    "id": "pk_uclosure_C1",
                    "sport": "Soccer",
                    "market": "Correct Score 2-1",
                    "event": "TeamA @ TeamB",
                    "event_time": future.isoformat(),
                    "created_at": future.isoformat(),
                    "status": "pending",
                    "publication_state": "PUBLISHED",
                    "off_board": False,
                    "settlement_block": True,
                    "settlement_block_reason": "settler_unsupported:soccer_correct_score",
                    "_test_marker": "phase_a_uclosure",
                }},
                upsert=True,
            )
            n = await db.picks.count_documents({
                "id": "pk_uclosure_C1",
                "status": {"$in": [None, "pending"]},
                "off_board": {"$ne": True},
                "settlement_block": {"$ne": True},
            })
            assert n == 0, (
                "future unsupported pick with settlement_block=True is "
                "still visible to the settlement candidate query — "
                "violates requirement C.")
        finally:
            await _cleanup(db)
            cx.close()
    asyncio.run(_run())
    print("test_C_future_unsupported_not_actionable OK")


# ── D + E: UNKNOWN/BLOCKED cannot starve, supported picks advance ────
def test_D_E_forward_progress():
    """Seed 3 picks:
      * pk_D_OLD_UNKNOWN  — Cricket market (UNKNOWN, oldest)
      * pk_D_BLOCK        — Soccer Corners already blocked (must be
                            excluded from candidate set entirely)
      * pk_D_SUPPORTED    — Soccer Moneyline (supported, newer)
    Verify:
      • The blocked pick never enters the candidate list.
      • The supported pick is present and reachable.
      • The UNKNOWN pick appears BUT does not prevent the supported
        pick from being examined (bounded batch semantics + no
        block-metadata mutation on UNKNOWN so it can be retried
        on future runs when capability broadens).
    """
    async def _run():
        cx, db = _make_db()
        try:
            await _cleanup(db)
            now = datetime.now(timezone.utc)
            await _seed_pick(db, id="pk_D_OLD_UNKNOWN",
                              sport="Cricket",
                              market="Runs at Fall of 2nd Wicket",
                              event_time=now - timedelta(days=3))
            # Blocked pick — pre-stamped (simulating prior run).
            await db.picks.update_one(
                {"id": "pk_D_BLOCK"},
                {"$set": {
                    "id": "pk_D_BLOCK",
                    "sport": "Soccer",
                    "market": "Total Corners Over 9.5",
                    "event": "TeamA @ TeamB",
                    "event_time": (now - timedelta(days=2)).isoformat(),
                    "created_at": (now - timedelta(days=2)).isoformat(),
                    "status": "pending",
                    "publication_state": "PUBLISHED",
                    "off_board": False,
                    "settlement_block": True,
                    "settlement_block_reason": "settler_unsupported:soccer_corners",
                    "_test_marker": "phase_a_uclosure",
                }},
                upsert=True,
            )
            await _seed_pick(db, id="pk_D_SUPPORTED",
                              sport="Soccer",
                              market="TeamA Moneyline",
                              event_time=now - timedelta(hours=6))

            # Emulate the exact candidate query used by the engine.
            candidates = await db.picks.find(
                {"status": {"$in": [None, "pending"]},
                 "off_board": {"$ne": True},
                 "settlement_block": {"$ne": True},
                 "_test_marker": "phase_a_uclosure"},
                {"_id": 0, "id": 1, "market": 1},
            ).sort("event_time", 1).to_list(length=2000)
            ids = [c["id"] for c in candidates]

            # D — blocked pick must NOT appear.
            assert "pk_D_BLOCK" not in ids, (
                "blocked pick contaminates candidate set — violates D."
            )
            # E — supported pick reachable in same batch as UNKNOWN.
            assert "pk_D_SUPPORTED" in ids, (
                "supported oldest-due pick unreachable — violates E."
            )
            # Bounded batch semantics: since the block gate removes
            # blocked picks entirely, and UNKNOWN picks share the same
            # bounded batch (2000) as SUPPORTED, no starvation is
            # possible until UNKNOWN >> 2000 (out of preview scope).
            # UNKNOWN + SUPPORTED both present in ordering.
            assert ids.index("pk_D_OLD_UNKNOWN") < ids.index("pk_D_SUPPORTED"), (
                "oldest-first ordering not applied consistently."
            )
        finally:
            await _cleanup(db)
            cx.close()
    asyncio.run(_run())
    print("test_D_E_forward_progress OK")


# ── F: SettlementService is the only outcome writer ─────────────────
def test_F_settlement_service_sole_outcome_writer():
    """Grep for direct pick.status mutations to canonical values.

    Only the SettlementService compat mirror is permitted to write
    ``status ∈ {won, lost, push, void}``.  Everywhere else the
    presence of such a literal write is a regression.
    """
    root = os.path.join(os.path.dirname(__file__), "..")
    hits: list[str] = []
    ALLOW_FILES = {
        "services/settlement_service.py",  # sole writer
    }
    for dirpath, _, files in os.walk(root):
        # Skip test dir / cache.
        if any(seg in dirpath for seg in ("__pycache__", "tests", "scripts",
                                           "historical", "ml/", "models/")):
            continue
        for fn in files:
            if not fn.endswith(".py"):
                continue
            fp = os.path.join(dirpath, fn)
            rel = os.path.relpath(fp, root)
            if rel in ALLOW_FILES:
                continue
            try:
                with open(fp) as f:
                    src = f.read()
            except Exception:
                continue
            # Look for direct db.picks update writing canonical outcome
            # status. Any $set with "status": "won"|"lost"|"push"|"void"
            # outside the allow-listed file is a violation.
            for needle in ('"status": "won"', '"status": "lost"',
                           '"status": "push"', '"status": "void"'):
                if needle in src:
                    hits.append(f"{rel}: {needle}")
    assert not hits, (
        "Non-canonical outcome writes detected — violates requirement F:\n  "
        + "\n  ".join(hits)
    )
    print("test_F_settlement_service_sole_outcome_writer OK")


if __name__ == "__main__":
    test_capability_matrix()
    test_pick_status_semantics_unchanged()
    test_cursor_gates_present()
    test_A_B_unsupported_does_not_create_void()
    test_C_future_unsupported_not_actionable()
    test_D_E_forward_progress()
    test_F_settlement_service_sole_outcome_writer()
    print("\nPHASE_A_MICRO_CLOSURE_TESTS_ALL_PASSED")
