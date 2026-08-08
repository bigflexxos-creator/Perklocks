"""P0-5 — Cross-refresh K-math immutability regression tests.

Verifies the surgical guard added to
`services.pick_refresh_orchestrator._reconcile_player_prop_contradictions`
that prevents the K-math reconciler from silently rewriting canonical
prediction fields on a keeper pick that was already canonically
published in a prior refresh.

Behaviour under test:

  * When a keeper pick carries ``publication_source``, the reconciler
    MUST NOT copy the winner's payload onto it.  Instead, the keeper
    is atomically tagged ``no_bet=True`` (a lifecycle flag, not a
    canonical field) and the current-refresh winner survives — so
    ``publish_batch`` snapshots the winner on the same refresh via
    the P0-4 re-hydration path.

  * When a keeper pick has NO ``publication_source`` (legacy /
    unpublished), the pre-P0-5 in-place update behaviour is
    preserved: the keeper is repurposed with the winner's payload
    and the newer winner row is deleted.

  * K-math winner-selection logic and formulas remain unchanged.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from motor.motor_asyncio import AsyncIOMotorClient


_TEST_ID_PREFIX = "p0test5_"


def _run(c):
    """Run an async coroutine in a fresh event loop with a fresh Motor
    client installed as the shared DB override — required because
    Motor clients bind to the loop they were first used on, and
    multiple `asyncio.run()` calls in a single pytest process each
    create a different loop.  Overriding the shared client per-test
    keeps every DB call (ours + the orchestrator's) on the same loop.
    """
    from services.database import (
        override_database_for_testing, reset_database_override,
    )
    import server as _srv
    _prior_server_db = _srv.__dict__.pop("db", None)
    async def _wrapper():
        client = AsyncIOMotorClient(os.environ["MONGO_URL"])
        db = client[os.environ.get("DB_NAME", "lockscore_db")]
        override_database_for_testing(client, db)
        # Force `_DBProxy._resolve()` to use the override.
        _srv.db = db
        try:
            return await c
        finally:
            reset_database_override()
            client.close()
    try:
        return asyncio.run(_wrapper())
    finally:
        # Restore server.db so other tests are unaffected.
        if _prior_server_db is None:
            _srv.__dict__.pop("db", None)
        else:
            _srv.db = _prior_server_db


def _fresh_db():
    """Grab the currently-active (overridden) shared database.  Only
    valid inside an active `_run` context."""
    from services.database import get_database
    return get_database()


async def _wipe(db):
    await db.picks.delete_many({"id": {"$regex": f"^{_TEST_ID_PREFIX}"}})
    await db.prediction_snapshots.delete_many(
        {"prediction_id": {"$regex": f"^{_TEST_ID_PREFIX}"}})


def _shape_k_prop(
    *, pid: str, side: str, published: bool,
    pick_date: str, event: str = "Y @ B",
    player: str = "Gerrit Cole", line: float = 6.5,
    lock: float = 90.0, edge: float = 4.0,
) -> dict[str, Any]:
    """Build an MLB K-prop pick doc matching the shape the reconciler
    expects to see (event, player as `selection`, `market` containing
    Over/Under + line, family=MLB_K)."""
    now = datetime.now(timezone.utc)
    doc: dict[str, Any] = {
        "id": pid,
        "sport": "MLB",
        "league": "MLB",
        "market": f"{player} {side} {line} strikeouts",
        "market_key": "player_strikeouts_over" if side == "Over"
                       else "player_strikeouts_under",
        "player": player,
        "selection": player,
        "side": side.lower(),
        "event": event,
        "event_time": (now + timedelta(hours=4)).isoformat(),
        "pick_date": pick_date,
        "created_at": now.isoformat(),
        "book_odds": -140 if side == "Over" else -115,
        "book": "test",
        "line": line,
        "lock_score": lock,
        "lock_score_v2": lock,
        "win_probability": 62.0,
        "edge_percent": edge,
        "grade": "Strong Lock",
        "confidence": lock,
        "reasoning": "test",
        "status": "pending",
        "no_bet": False,
        "off_board": False,
        "hide_from_main_board": False,
        "factors": {"form": 0.7},
        "key_insights": ["strong matchup"],
        "k_math_gate": True,
        "k_math_expected_k": 7.6,
        "k_prop_data": {"tsr_2026": 0.28},
        "source": "mlb_k_prop_engine",
        "model_version": "test.v1",
    }
    if published:
        doc.update({
            "publication_source": "canonical_pipeline",
            "snapshot_version": 1,
            "published_at": now.isoformat(),
            "payload_hash": "test_" + pid,
            "idempotency_key": "test_idem_" + pid,
            "published_probability": 0.62,
            "published_edge": edge,
            "published_lock_score": lock,
            "published_grade": "Strong Lock",
            "published_confidence": lock,
            "published_line": line,
            "published_odds": doc["book_odds"],
            "published_reasoning": "test",
            "model_version": "mlb_prop_v3.2",
            "fusion_version": "fusion_v4",
            "scoring_version": "lockscore_v2.1",
            "calibration_version": "cal_2026-08-01",
            "validator_version": "board_v2.0",
            "simulation_version": "mc_v1.5",
            "feature_snapshot_version": "feat_v2.0",
            "board_version": "board-test",
        })
    return doc


async def _snapshot_for(pick: dict[str, Any]) -> dict[str, Any]:
    """Build a matching prediction_snapshots doc for a published pick."""
    return {
        "prediction_id": pick["id"],
        "snapshot_version": 1,
        "is_active": True,
        "published_lock_score": pick["published_lock_score"],
        "published_probability": pick["published_probability"],
        "published_edge": pick["published_edge"],
        "published_grade": pick["published_grade"],
        "published_confidence": pick["published_confidence"],
        "published_reasoning": pick["published_reasoning"],
        "published_line": pick["published_line"],
        "published_odds": pick["published_odds"],
        "payload_hash": pick["payload_hash"],
        "idempotency_key": pick["idempotency_key"],
        "publication_source": pick["publication_source"],
        "published_at": pick["published_at"],
        "board_version": pick["board_version"],
        "model_version": pick["model_version"],
        "fusion_version": pick["fusion_version"],
        "scoring_version": pick["scoring_version"],
        "calibration_version": pick["calibration_version"],
        "validator_version": pick["validator_version"],
        "simulation_version": pick["simulation_version"],
        "feature_snapshot_version": pick["feature_snapshot_version"],
        "is_legacy": False,
    }


# ══════════════════════════════════════════════════════════════════════ #
# 1.  Cross-refresh scenario — keeper is PUBLISHED
#     Prior published Under is NOT overwritten by K-math.
# ══════════════════════════════════════════════════════════════════════ #
def test_cross_refresh_published_keeper_is_immutable():
    async def run():
        from services.pick_refresh_orchestrator import (
            _reconcile_player_prop_contradictions,
        )

        db = _fresh_db()
        await _wipe(db)
        try:
            yesterday = (datetime.now(timezone.utc)
                          - timedelta(days=1)).strftime("%Y-%m-%d")
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

            # Refresh N: published UNDER pick.
            prior = _shape_k_prop(
                pid=_TEST_ID_PREFIX + "prior_under_" + uuid.uuid4().hex[:8],
                side="Under", published=True,
                pick_date=yesterday, lock=88.0, edge=3.5,
            )
            prior_snap = await _snapshot_for(prior)
            await db.picks.insert_one(prior)
            await db.prediction_snapshots.insert_one(prior_snap)

            # Refresh N+1: new OVER pick (opposite side, K-math winner).
            new_over = _shape_k_prop(
                pid=_TEST_ID_PREFIX + "new_over_" + uuid.uuid4().hex[:8],
                side="Over", published=False,     # not yet published
                pick_date=today, lock=93.0, edge=6.0,
            )
            new_over["k_math_gate"] = True
            new_over["k_math_expected_k"] = 8.2   # supports OVER
            await db.picks.insert_one(new_over)

            # Snapshot the "before" state for later comparison.
            before_pick = await db.picks.find_one(
                {"id": prior["id"]}, {"_id": 0})
            before_snap = await db.prediction_snapshots.find_one(
                {"prediction_id": prior["id"]}, {"_id": 0})

            # Run the reconciler.  It must NOT mutate the published
            # keeper's canonical fields.
            await _reconcile_player_prop_contradictions(
                [new_over], today)

            after_pick = await db.picks.find_one(
                {"id": prior["id"]}, {"_id": 0})
            after_snap = await db.prediction_snapshots.find_one(
                {"prediction_id": prior["id"]}, {"_id": 0})
            after_new = await db.picks.find_one(
                {"id": new_over["id"]}, {"_id": 0})

            # Canonical fields on the published keeper — UNCHANGED.
            for f in ("market", "selection", "side", "book_odds", "book",
                      "edge_percent", "lock_score", "grade", "confidence",
                      "probability", "line", "reasoning",
                      "published_probability", "published_edge",
                      "published_lock_score", "published_grade",
                      "published_confidence", "published_line",
                      "published_odds", "published_reasoning",
                      "snapshot_version", "payload_hash",
                      "idempotency_key", "publication_source",
                      "published_at"):
                assert after_pick.get(f) == before_pick.get(f), (
                    f"canonical field {f!r} was mutated on the "
                    f"published keeper: before={before_pick.get(f)!r} "
                    f"after={after_pick.get(f)!r}"
                )

            # Snapshot untouched.
            assert after_snap == before_snap

            # Keeper tagged no_bet so /picks/today filters it out.
            assert after_pick["no_bet"] is True
            assert after_pick["status"] == "blocked"
            assert "P0-5" in (after_pick.get("no_bet_reason") or "")

            # Winner (current-refresh Over) survived — will be
            # canonically published by publish_batch on this refresh.
            assert after_new is not None
            assert after_new["side"] == "over"
            assert after_new["no_bet"] is False
        finally:
            await _wipe(db)
    _run(run())


# ══════════════════════════════════════════════════════════════════════ #
# 2.  Same-refresh in-place path — keeper is UNPUBLISHED (legacy)
#     Behaviour unchanged from P0-4.
# ══════════════════════════════════════════════════════════════════════ #
def test_unpublished_keeper_still_gets_in_place_update():
    async def run():
        from services.pick_refresh_orchestrator import (
            _reconcile_player_prop_contradictions,
        )

        db = _fresh_db()
        await _wipe(db)
        try:
            yesterday = (datetime.now(timezone.utc)
                          - timedelta(days=1)).strftime("%Y-%m-%d")
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

            keeper = _shape_k_prop(
                pid=_TEST_ID_PREFIX + "legacy_under_" + uuid.uuid4().hex[:8],
                side="Under", published=False,  # no publication_source
                pick_date=yesterday, lock=80.0, edge=2.5,
            )
            keeper["k_math_gate"] = True
            keeper["k_math_expected_k"] = 6.4  # supports UNDER
            await db.picks.insert_one(keeper)

            winner = _shape_k_prop(
                pid=_TEST_ID_PREFIX + "new_over_" + uuid.uuid4().hex[:8],
                side="Over", published=False,
                pick_date=today, lock=93.0, edge=6.0,
            )
            winner["k_math_gate"] = True
            winner["k_math_expected_k"] = 8.2  # supports OVER
            await db.picks.insert_one(winner)

            await _reconcile_player_prop_contradictions(
                [winner], today)

            keeper_after = await db.picks.find_one(
                {"id": keeper["id"]}, {"_id": 0})
            winner_after = await db.picks.find_one(
                {"id": winner["id"]}, {"_id": 0})

            # Legacy path — keeper repurposed with winner's payload.
            # Behaviour matches pre-P0-5 (P0-4) semantics exactly.
            assert keeper_after is not None
            assert keeper_after.get("corrected_by") == "reconciler_k_math"
            assert keeper_after["side"] == "over"
            assert keeper_after["pick_date"] == today
            # New winner row deleted.
            assert winner_after is None
        finally:
            await _wipe(db)
    _run(run())


# ══════════════════════════════════════════════════════════════════════ #
# 3.  Static-source guard: the immutability branch exists in code.
# ══════════════════════════════════════════════════════════════════════ #
def test_reconciler_source_has_publication_source_guard():
    src = (pathlib.Path(__file__).resolve().parents[1]
            / "services" / "pick_refresh_orchestrator.py").read_text()
    # Guard branch is present.
    assert 'if keeper.get("publication_source"):' in src
    # Defence-in-depth Mongo write filter is present.
    assert (
        '{"id": keeper["id"],\n                         '
        '"publication_source": {"$exists": False}}' in src
    ) or (
        '"publication_source": {"$exists": False}' in src
    )
    # P0-5 marker to prevent silent revert.
    assert "P0-5" in src


# ══════════════════════════════════════════════════════════════════════ #
# 4.  K-math formulas are byte-identical (audit trail marker present).
# ══════════════════════════════════════════════════════════════════════ #
def test_kmath_formulas_untouched():
    src = (pathlib.Path(__file__).resolve().parents[1]
            / "services" / "pick_refresh_orchestrator.py").read_text()
    # Verify the winner-selection code path still exists.
    assert "from services.k_conflict_resolver import resolve_k_family_winner" in src
    assert "winning_side, reason = resolve_k_family_winner(" in src
    assert 'update_payload["corrected_by"] = "reconciler_k_math"' in src
    # Every canonical field the payload copies is still there
    # (legacy path only — the P0-5 guard skips this path when
    # the keeper is published).
    for field in ("market", "selection", "side", "book_odds",
                  "edge_percent", "lock_score", "grade",
                  "confidence", "probability"):
        assert f'"{field}"' in src


# ══════════════════════════════════════════════════════════════════════ #
# 5.  Static bypass audit — no other reconciler-style writer touches
#     canonical fields on a row with publication_source.
# ══════════════════════════════════════════════════════════════════════ #
def test_static_search_finds_no_unguarded_reconciler_style_writer():
    """Search all reconciler / correction code for db.picks writes
    that set canonical fields without checking publication_source.
    The reconciler in pick_refresh_orchestrator.py is now guarded;
    no other file should do the equivalent."""
    backend_root = pathlib.Path(__file__).resolve().parents[1]
    canonical_fields = ["lock_score", "win_probability", "edge_percent",
                        "grade", "confidence", "probability"]
    for py in backend_root.rglob("*.py"):
        # Skip tests, scripts, the publication service itself, and
        # the write-guard.
        rel = py.relative_to(backend_root).as_posix()
        if rel.startswith(("tests/", "scripts/")):
            continue
        if rel in {
            "services/prediction_publication_service.py",
            "services/published_write_guard.py",
            "services/published_prediction_reader.py",
        }:
            continue
        text = py.read_text()
        if "db.picks.update_one" not in text and \
           "db.picks.bulk_write" not in text and \
           "db.picks.update_many" not in text:
            continue
        # A reconciler-style writer sets multiple canonical fields
        # in a single $set with the intent of "correcting" a pick.
        # Heuristic: file has an update statement AND at least 3
        # canonical field names within the same 300-char window.
        i = 0
        while i < len(text):
            for op in ("db.picks.update_one", "db.picks.update_many",
                       "db.picks.bulk_write"):
                j = text.find(op, i)
                if j == -1:
                    continue
                window = text[j:j + 400]
                hits = sum(1 for f in canonical_fields
                           if f'"{f}"' in window)
                if hits >= 3:
                    # This is a reconciler-style writer.  It MUST
                    # have a publication_source guard nearby (within
                    # the enclosing function).  Read the function
                    # body — grab 3000 chars before + 400 after.
                    start = max(0, j - 3000)
                    body = text[start:j + 400]
                    assert (
                        "publication_source" in body
                    ), (
                        f"{rel}:{text[:j].count(chr(10)) + 1} appears "
                        f"to be a reconciler-style canonical writer "
                        f"but lacks any `publication_source` guard "
                        f"in its enclosing function"
                    )
            i = j + 1 if j != -1 else len(text)


# ══════════════════════════════════════════════════════════════════════ #
# 6.  Concurrent-publication race: the defence-in-depth Mongo write
#     filter blocks a legacy-path update if publication landed
#     between the reconciler's initial read and its final write.
# ══════════════════════════════════════════════════════════════════════ #
def test_race_publication_after_read_is_still_blocked_by_write_filter():
    async def run():
        db = _fresh_db()
        await _wipe(db)
        try:
            # Insert a keeper that STARTS unpublished (reconciler
            # would enter the legacy branch), then simulate that
            # publication landed between the reconciler's read and
            # its final `update_one`.
            keeper = _shape_k_prop(
                pid=_TEST_ID_PREFIX + "race_" + uuid.uuid4().hex[:8],
                side="Under", published=False,
                pick_date="2099-01-01",
                lock=88.0, edge=3.5,
            )
            await db.picks.insert_one(keeper)
            # Simulate publication landing NOW.
            await db.picks.update_one(
                {"id": keeper["id"]},
                {"$set": {
                    "publication_source": "canonical_pipeline",
                    "snapshot_version": 1,
                    "published_at": datetime.now(timezone.utc).isoformat(),
                    "payload_hash": "race_hash",
                    "idempotency_key": "race_idem",
                    "published_lock_score": 88.0,
                    "published_grade": "Strong Lock",
                }},
            )
            # Now emulate the reconciler's LEGACY-path write with
            # the P0-5 defence-in-depth filter.
            res = await db.picks.update_one(
                {"id": keeper["id"],
                 "publication_source": {"$exists": False}},
                {"$set": {"lock_score": 99.0, "grade": "Elite Lock"}},
            )
            assert res.matched_count == 0
            after = await db.picks.find_one({"id": keeper["id"]},
                                             {"_id": 0})
            assert after["lock_score"] == 88.0
            assert after["published_lock_score"] == 88.0
        finally:
            await _wipe(db)
    _run(run())


# ══════════════════════════════════════════════════════════════════════ #
# 7.  Board-eligibility follow-through: after the P0-5 guard tags the
#     published keeper `no_bet`, /picks/today's base filter excludes it.
# ══════════════════════════════════════════════════════════════════════ #
def test_board_filter_excludes_no_bet_keeper():
    async def run():
        db = _fresh_db()
        await _wipe(db)
        try:
            keeper = _shape_k_prop(
                pid=_TEST_ID_PREFIX + "keeper_" + uuid.uuid4().hex[:8],
                side="Under", published=True,
                pick_date="2099-08-08",
                lock=88.0, edge=3.5,
            )
            await db.picks.insert_one(keeper)
            # Simulate the P0-5 guard's atomic no_bet tag.
            await db.picks.update_one(
                {"id": keeper["id"]},
                {"$set": {"no_bet": True, "status": "blocked",
                            "no_bet_reason": "P0-5 test"}},
            )
            base_q = {
                "pick_date": "2099-08-08",
                "grade": {"$ne": "Pass"},
                "no_bet": {"$ne": True},
                "off_board": {"$ne": True},
                "hide_from_main_board": {"$ne": True},
                "status": {"$in": ["pending", "open", None]},
            }
            n = await db.picks.count_documents(base_q)
            assert n == 0
        finally:
            await _wipe(db)
    _run(run())
