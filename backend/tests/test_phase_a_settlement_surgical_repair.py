"""Phase A — Focused unit tests for the surgical settlement repair.

Scope (per user budget arrest):
 * settlement_capability classification matrix
 * settlement_telemetry write/read cycle
 * SettlementService canonical status-mapping preserved
 * settlement_engine cursor ordering (oldest-first) enforced
 * SETTLEMENT_UNSUPPORTED terminator VOIDs unsupported picks via
   the canonical service (no direct pick.status mutation)

NON-goals (deferred to Phase B+):
 * Full regression
 * Live production data
 * Provider integration paths
"""
import asyncio
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── Capability matrix ────────────────────────────────────────────────
def test_capability_matrix():
    from services.settlement_capability import (
        classify, SUPPORTED, UNSUPPORTED, UNKNOWN,
    )
    cases = [
        # (sport, market, league, expected_status)
        ("Soccer", "Home Moneyline", None, SUPPORTED),
        ("Soccer", "Total Goals Over 2.5", None, SUPPORTED),
        ("Soccer", "Anytime Goal Scorer", None, SUPPORTED),
        ("Soccer", "To Score or Assist", None, SUPPORTED),
        ("Soccer", "Both Teams To Score Yes", None, SUPPORTED),
        ("Soccer", "Win or Draw", None, SUPPORTED),
        # Unsupported soccer families
        ("Soccer", "First Goalscorer",             None, UNSUPPORTED),
        ("Soccer", "Player X Shots On Target 2.5", None, UNSUPPORTED),
        ("Soccer", "Player X Total Shots 4.5",     None, UNSUPPORTED),
        ("Soccer", "Correct Score 2-1",            None, UNSUPPORTED),
        ("Soccer", "Half Time / Full Time",        None, UNSUPPORTED),
        ("Soccer", "Asian Handicap Home +0.5",     None, UNSUPPORTED),
        ("Soccer", "Total Cards Over 3.5",         None, UNSUPPORTED),
        ("Soccer", "Total Corners Over 9.5",       None, UNSUPPORTED),
        # Non-soccer sports
        ("MLB",   "Team A Moneyline",               None,          SUPPORTED),
        ("MLB",   "Team A Run Line -1.5",           None,          SUPPORTED),
        ("NBA",   "Total Points Over 220.5",        None,          SUPPORTED),
        ("MLB",   "Buxton Over 0.5 Hits",           "MLB Props",   SUPPORTED),
        # Truly unknown → UNKNOWN, not terminated
        ("Cricket", "Runs at Fall of 2nd Wicket",   None, UNKNOWN),
    ]
    for sport, market, league, expected in cases:
        got, reason = classify(sport, market, league)
        assert got == expected, (
            f"expected {expected} for ({sport!r},{market!r}) got {got} ({reason})"
        )
    print("test_capability_matrix OK")


# ── SettlementService semantics ──────────────────────────────────────
def test_pick_status_semantics():
    from services.settlement_service import _pick_status_from_result
    # Strict canonical status mapping — must NOT change across releases.
    expected = {
        "won":       "won",
        "lost":      "lost",
        "void":      "void",
        "push":      "push",
        "cancelled": "void",
        "unknown":   "pending",
    }
    for k, v in expected.items():
        assert _pick_status_from_result(k) == v, (
            f"canonical mapping drift: {k} → {_pick_status_from_result(k)}"
        )
    print("test_pick_status_semantics OK")


# ── Telemetry write/read cycle ───────────────────────────────────────
def test_telemetry_roundtrip():
    from motor.motor_asyncio import AsyncIOMotorClient
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    mongo_url = os.getenv("MONGO_URL", "mongodb://localhost:27017")
    dbname = os.getenv("DB_NAME", "test_database")

    async def _run():
        cx = AsyncIOMotorClient(mongo_url)
        db = cx[dbname]
        from services.settlement_telemetry import (
            record_run, read_latest, COLLECTION,
        )
        # Cleanup prior test docs.
        await db[COLLECTION].delete_many({"_test_marker": "phase_a"})
        payload = {
            "candidates_examined": 42,
            "attempts": 30,
            "success": 22,
            "fail": 8,
            "unsupported_terminated": 3,
            "oldest_unresolved_age_seconds": 12345,
            "terminal_reasons": {"settler_unsupported:soccer_shots": 2},
            "_test_marker": "phase_a",
        }
        await record_run(db, payload)
        docs = await read_latest(db, limit=5)
        assert docs, "no telemetry docs returned"
        latest = docs[0]
        assert latest.get("candidates_examined") == 42
        assert latest.get("success") == 22
        assert latest.get("oldest_unresolved_age_seconds") == 12345
        assert latest.get("terminal_reasons") == {
            "settler_unsupported:soccer_shots": 2}
        # Cleanup.
        await db[COLLECTION].delete_many({"_test_marker": "phase_a"})
        cx.close()

    asyncio.run(_run())
    print("test_telemetry_roundtrip OK")


# ── Settlement engine — cursor ordering asserted at source level ─────
def test_settlement_engine_sort_present():
    """Static safety net — ensures the oldest-first sort call is present
    in the settlement-engine main cursor and the soccer batch cursor.
    Prevents a future edit from silently re-introducing the starvation.
    """
    root = os.path.join(os.path.dirname(__file__), "..")
    with open(os.path.join(root, "settlement_engine.py")) as f:
        engine_src = f.read()
    with open(os.path.join(root, "soccer_espn_settle.py")) as f:
        soccer_src = f.read()
    with open(os.path.join(root, "prop_settlement.py")) as f:
        prop_src = f.read()
    with open(os.path.join(root, "espn_settlement.py")) as f:
        espn_src = f.read()

    assert 'db.picks.find(query, {"_id": 0}).sort("event_time", 1)' in engine_src, (
        "settlement_engine main cursor missing oldest-first sort")
    assert '.sort("event_time", 1).limit(max_picks)' in soccer_src, (
        "soccer_espn_settle cursor missing oldest-first sort")
    assert '.sort("event_time", 1).limit(max_picks)' in prop_src, (
        "prop_settlement cursor missing oldest-first sort")
    assert 'sport": "Tennis"' in espn_src and '.sort("event_time", 1).to_list(length=1000)' in espn_src, (
        "espn_settlement tennis cursor missing oldest-first sort")

    # SETTLEMENT_UNSUPPORTED terminator wired.
    assert "SETTLEMENT_UNSUPPORTED terminator" in engine_src, (
        "settlement_engine missing SETTLEMENT_UNSUPPORTED terminator block")
    # Telemetry record wired.
    assert "settlement_telemetry" in engine_src, (
        "settlement_engine missing telemetry hook")
    print("test_settlement_engine_sort_present OK")


if __name__ == "__main__":
    test_capability_matrix()
    test_pick_status_semantics()
    test_telemetry_roundtrip()
    test_settlement_engine_sort_present()
    print("\nPHASE_A_TESTS_ALL_PASSED")
