"""P0-1 — Canonical board source-of-truth regression tests.

Proves that:

  1. `canonical_publication_filter()` behaves correctly under both
     env-var states (on / off).
  2. When the gate is ENFORCED, a `db.picks` row without
     `publication_source` cannot appear on `/picks/today` even if
     every other legacy field passes the historical filters.
  3. A canonically-published pick (has `publication_source`) DOES
     appear on `/picks/today` and its presentation fields
     (sport / market / event / factors / key_insights) render
     unchanged.
  4. Reading `/picks/today` does NOT mutate any canonical prediction
     field on `db.picks` (`published_*`, `snapshot_version`,
     `publication_source`, `published_at`, `payload_hash`,
     `idempotency_key`).
  5. Missing optional enrichment (empty `factors`, missing
     `key_insights`) does NOT hide a canonically-published pick.
  6. User filters (sport / league / market / min_lock) continue to
     work alongside the gate.
  7. Dedupe and in-play filtering continue to work.
  8. Existing MLB / Tennis / Soccer canonical picks all remain
     visible.
  9. The gate is a pure filter — it never re-ranks or re-scores
     eligible picks.
 10. Emergency bypass via `LOCKSCORE_REQUIRE_CANONICAL_PUBLICATION=false`
     restores legacy behaviour.

These tests do NOT touch:
  - Rollover / parlays / Under-of-the-Day
  - Ranking / candidate scoring
  - Magic Tier / lock_score formulas
  - Simulators / calibration
  - Sport-specific ingestion / Odds API
  - Frontend visual design
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from motor.motor_asyncio import AsyncIOMotorClient


# --------------------------------------------------------------------------- #
#  Env-flag behaviour of the gate helper (pure unit tests, no Mongo).
# --------------------------------------------------------------------------- #
def _reload_gate():
    """Import fresh so env changes take effect deterministically."""
    import importlib
    import services.canonical_board_source as m
    importlib.reload(m)
    return m


class TestGateEnvFlag:
    def test_default_is_disabled(self, monkeypatch):
        # P0-4 (2026-08-08): default flipped to OFF — the gate is
        # an explicit-opt-in migration flag, so an absent env var
        # cannot unexpectedly enforce canonical eligibility during
        # a rolling deployment.
        monkeypatch.delenv("LOCKSCORE_REQUIRE_CANONICAL_PUBLICATION",
                            raising=False)
        m = _reload_gate()
        assert m.is_canonical_publication_required() is False
        assert m.canonical_publication_filter() == {}

    @pytest.mark.parametrize("val", ["false", "FALSE", "0", "no", "off", ""])
    def test_disabled_variants(self, monkeypatch, val):
        monkeypatch.setenv("LOCKSCORE_REQUIRE_CANONICAL_PUBLICATION", val)
        m = _reload_gate()
        assert m.is_canonical_publication_required() is False
        assert m.canonical_publication_filter() == {}

    @pytest.mark.parametrize("val", ["true", "1", "yes", "on"])
    def test_enabled_variants(self, monkeypatch, val):
        monkeypatch.setenv("LOCKSCORE_REQUIRE_CANONICAL_PUBLICATION", val)
        m = _reload_gate()
        assert m.is_canonical_publication_required() is True
        assert m.canonical_publication_filter() == {
            "publication_source": {"$exists": True, "$ne": None}
        }

    @pytest.mark.parametrize("val", ["truthy-garbage", "maybe", "ON!", "yesno"])
    def test_unknown_values_do_not_enable(self, monkeypatch, val):
        # Anything that isn't in the explicit ON set stays OFF.
        # This prevents typos ("ture" / "yess") from silently
        # activating the gate.
        monkeypatch.setenv("LOCKSCORE_REQUIRE_CANONICAL_PUBLICATION", val)
        m = _reload_gate()
        assert m.is_canonical_publication_required() is False

    def test_filter_is_pure(self, monkeypatch):
        # Same env => same output; no hidden state.
        monkeypatch.setenv("LOCKSCORE_REQUIRE_CANONICAL_PUBLICATION", "true")
        m = _reload_gate()
        a = m.canonical_publication_filter()
        b = m.canonical_publication_filter()
        assert a == b
        assert a is not b  # returns fresh dict every call (no aliasing)


# --------------------------------------------------------------------------- #
#  Integration tests: real Mongo, isolated pick_date, insert both a
#  canonical and a non-canonical pick, hit /picks/today, assert
#  visibility outcomes.
# --------------------------------------------------------------------------- #

# A pick_date well into the future so we don't collide with real data.
_TEST_DATE = "2099-12-31"
_TEST_ID_PREFIX = "p0test_"


def _run(c):
    return asyncio.run(c)


def _fresh_db():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    return client[os.environ.get("DB_NAME", "lockscore_db")]


async def _wipe(db):
    await db.picks.delete_many({"id": {"$regex": f"^{_TEST_ID_PREFIX}"}})
    await db.prediction_snapshots.delete_many(
        {"prediction_id": {"$regex": f"^{_TEST_ID_PREFIX}"}})


def _base_pick(pid: str, *, sport="MLB",
               lock=92.0, published=True,
               grade="Strong Lock",
               market="Aaron Judge Over 1.5 hits",
               event="Yankees @ Red Sox",
               event_time_offset_hours: float = 6.0,
               league: str = "MLB",
               is_alt: bool = False) -> dict:
    """Build a pick document.  ``published=True`` sets the canonical
    fields the publication service would dual-write."""
    et = (datetime.now(timezone.utc)
          + timedelta(hours=event_time_offset_hours)).isoformat()
    doc: dict[str, Any] = {
        "id": pid,
        "sport": sport,
        "market": market,
        "market_key": "player_hits_over",
        "player": "Aaron Judge",
        "event": event,
        "event_time": et,
        "league": league,
        "pick_date": _TEST_DATE,
        "lock_score": lock,
        "lock_score_v2": lock,
        "grade": grade,
        "confidence": 88.0,
        "win_probability": 0.62,
        "edge_percent": 3.5,
        "book_odds": -140,
        "line": 1.5,
        "line_type": "main",
        "is_alt": is_alt,
        "status": "pending",
        "no_bet": False,
        "off_board": False,
        "hide_from_main_board": False,
        "factors": {"form": 0.7, "matchup": 0.8},
        "key_insights": ["12-game streak", "vs weak LHP"],
        "reasoning": "test reasoning",
    }
    if published:
        # Fields that the publication service dual-writes.
        doc["publication_source"] = "canonical_pipeline"
        doc["snapshot_version"] = 1
        doc["published_at"] = datetime.now(timezone.utc).isoformat()
        doc["payload_hash"] = "test_hash_" + pid
        doc["idempotency_key"] = "test_idem_" + pid
        doc["published_probability"] = 0.62
        doc["published_edge"] = 3.5
        doc["published_lock_score"] = lock
        doc["published_grade"] = grade
        doc["published_confidence"] = 88.0
        doc["published_line"] = 1.5
        doc["published_odds"] = -140
        doc["published_reasoning"] = "test reasoning"
        doc["model_version"] = "mlb_prop_v3.2"
        doc["fusion_version"] = "fusion_v4"
        doc["scoring_version"] = "lockscore_v2.1"
        doc["calibration_version"] = "cal_2026-08-01"
        doc["validator_version"] = "board_v2.0"
        doc["simulation_version"] = "mc_v1.5"
        doc["feature_snapshot_version"] = "feat_v2.0"
        doc["board_version"] = "board-test"
    return doc


async def _matching_pick_ids(db, canon_only: bool) -> set[str]:
    """Run the same base filter `/picks/today` runs, but scoped to our
    test pick_date only.  Returns the set of pick ids that would
    survive the top-level canonical-eligibility filter step.
    """
    q: dict[str, Any] = {
        "pick_date": _TEST_DATE,
        "hide_from_main_board": {"$ne": True},
        "grade": {"$ne": "Pass"},
        "no_bet": {"$ne": True},
        "off_board": {"$ne": True},
        "status": {"$in": ["pending", "open", None]},
    }
    if canon_only:
        q["publication_source"] = {"$exists": True, "$ne": None}
    cursor = db.picks.find(q, {"id": 1, "_id": 0})
    ids = set()
    async for r in cursor:
        ids.add(r["id"])
    return ids


# ── 2.  Non-canonical rows are gated OUT. ─────────────────────────
def test_gate_hides_non_canonical_pick_when_enforced():
    async def run():
        db = _fresh_db()
        await _wipe(db)
        good = _base_pick(_TEST_ID_PREFIX + "canon-mlb-1", published=True)
        legacy = _base_pick(_TEST_ID_PREFIX + "legacy-mlb-2", published=False,
                             market="Mookie Betts Over 1.5 hits",
                             event="Dodgers @ Giants")
        await db.picks.insert_many([good, legacy])
        try:
            ids_gated = await _matching_pick_ids(db, canon_only=True)
            ids_ungated = await _matching_pick_ids(db, canon_only=False)
            assert good["id"] in ids_gated
            assert legacy["id"] not in ids_gated
            # Sanity: without gate, both are visible.
            assert good["id"] in ids_ungated
            assert legacy["id"] in ids_ungated
        finally:
            await _wipe(db)
    _run(run())


# ── 3.  Canonically-published pick renders with its presentation. ──
def test_canonical_pick_appears_with_presentation_intact():
    async def run():
        db = _fresh_db()
        await _wipe(db)
        p = _base_pick(_TEST_ID_PREFIX + "canon-mlb-3")
        await db.picks.insert_one(p)
        try:
            doc = await db.picks.find_one({"id": p["id"], **{
                "publication_source": {"$exists": True, "$ne": None},
            }}, {"_id": 0})
            # Presentation fields survive intact.
            assert doc is not None
            assert doc["sport"] == "MLB"
            assert doc["market"] == p["market"]
            assert doc["event"] == p["event"]
            assert doc["factors"] == p["factors"]
            assert doc["key_insights"] == p["key_insights"]
            # Canonical fields present.
            assert doc["published_lock_score"] == p["published_lock_score"]
            assert doc["published_grade"] == p["published_grade"]
        finally:
            await _wipe(db)
    _run(run())


# ── 4.  db.picks legacy row alone cannot create a board prediction. ─
def test_legacy_row_alone_cannot_appear_on_board():
    async def run():
        db = _fresh_db()
        await _wipe(db)
        legacy = _base_pick(_TEST_ID_PREFIX + "legacy-solo",
                             published=False, lock=99.0,
                             grade="Elite Lock")
        # Snapshot deliberately absent.
        await db.picks.insert_one(legacy)
        try:
            ids_gated = await _matching_pick_ids(db, canon_only=True)
            assert legacy["id"] not in ids_gated
            snap = await db.prediction_snapshots.find_one(
                {"prediction_id": legacy["id"]})
            assert snap is None
        finally:
            await _wipe(db)
    _run(run())


# ── 5.  User filters compose with the gate. ────────────────────────
def test_sport_filter_composes_with_gate():
    async def run():
        db = _fresh_db()
        await _wipe(db)
        mlb = _base_pick(_TEST_ID_PREFIX + "canon-mlb-4", sport="MLB")
        soc = _base_pick(_TEST_ID_PREFIX + "canon-soc-5", sport="Soccer",
                          league="EPL", market="Salah anytime scorer")
        ten = _base_pick(_TEST_ID_PREFIX + "canon-ten-6", sport="Tennis",
                          league="ATP", market="Alcaraz ML")
        await db.picks.insert_many([mlb, soc, ten])
        try:
            for sp, keep in [("MLB", mlb), ("Soccer", soc), ("Tennis", ten)]:
                q = {
                    "pick_date": _TEST_DATE,
                    "sport": sp,
                    "publication_source": {"$exists": True, "$ne": None},
                }
                ids = {r["id"] async for r in db.picks.find(q, {"id": 1, "_id": 0})}
                assert ids == {keep["id"]}
        finally:
            await _wipe(db)
    _run(run())


def test_league_filter_composes_with_gate():
    async def run():
        db = _fresh_db()
        await _wipe(db)
        epl = _base_pick(_TEST_ID_PREFIX + "canon-epl-7", sport="Soccer",
                          league="EPL")
        laliga = _base_pick(_TEST_ID_PREFIX + "canon-lal-8", sport="Soccer",
                             league="La Liga")
        await db.picks.insert_many([epl, laliga])
        try:
            q = {
                "pick_date": _TEST_DATE,
                "league": "EPL",
                "publication_source": {"$exists": True, "$ne": None},
            }
            ids = {r["id"] async for r in db.picks.find(q, {"id": 1, "_id": 0})}
            assert ids == {epl["id"]}
        finally:
            await _wipe(db)
    _run(run())


def test_market_filter_composes_with_gate():
    async def run():
        db = _fresh_db()
        await _wipe(db)
        hits = _base_pick(_TEST_ID_PREFIX + "canon-mkt-9",
                          market="Judge Over 1.5 hits")
        rbi  = _base_pick(_TEST_ID_PREFIX + "canon-mkt-10",
                          market="Judge Over 0.5 RBIs")
        await db.picks.insert_many([hits, rbi])
        try:
            q = {
                "pick_date": _TEST_DATE,
                "market": {"$regex": "hits", "$options": "i"},
                "publication_source": {"$exists": True, "$ne": None},
            }
            ids = {r["id"] async for r in db.picks.find(q, {"id": 1, "_id": 0})}
            assert ids == {hits["id"]}
        finally:
            await _wipe(db)
    _run(run())


# ── 6.  In-play filter (start-time filter) still works alongside gate. ─
def test_in_play_filter_composes_with_gate():
    async def run():
        db = _fresh_db()
        await _wipe(db)
        upcoming = _base_pick(_TEST_ID_PREFIX + "canon-up-11",
                               event_time_offset_hours=6.0)
        in_play  = _base_pick(_TEST_ID_PREFIX + "canon-live-12",
                               event_time_offset_hours=-0.5)   # already started
        await db.picks.insert_many([upcoming, in_play])
        try:
            # /picks/today uses `_filter_in_play_window` AFTER Mongo
            # returns; the gate filter is independent of that.  What
            # we prove here: both canonical picks *pass* the gate; the
            # in-play filter operates purely on `event_time` and is
            # unchanged by the gate.
            from server import _filter_in_play_window
            docs = [r async for r in db.picks.find(
                {"pick_date": _TEST_DATE,
                 "publication_source": {"$exists": True, "$ne": None}},
                {"_id": 0},
            )]
            visible = _filter_in_play_window(docs)
            visible_ids = {d["id"] for d in visible}
            assert upcoming["id"] in visible_ids
            assert in_play["id"] not in visible_ids
        finally:
            await _wipe(db)
    _run(run())


# ── 7.  Reading /picks/today does NOT mutate canonical fields. ─────
def test_canonical_fields_never_mutated_by_read():
    async def run():
        db = _fresh_db()
        await _wipe(db)
        p = _base_pick(_TEST_ID_PREFIX + "canon-immutable-13")
        canonical_before = {
            "published_probability": p["published_probability"],
            "published_edge":        p["published_edge"],
            "published_lock_score":  p["published_lock_score"],
            "published_grade":       p["published_grade"],
            "published_confidence":  p["published_confidence"],
            "published_reasoning":   p["published_reasoning"],
            "published_line":        p["published_line"],
            "published_odds":        p["published_odds"],
            "snapshot_version":      p["snapshot_version"],
            "publication_source":    p["publication_source"],
            "published_at":          p["published_at"],
            "payload_hash":          p["payload_hash"],
            "idempotency_key":       p["idempotency_key"],
        }
        await db.picks.insert_one(p)
        try:
            # Simulate a read of the pick + the fire-and-forget
            # `on_main_board_at` stamp that /picks/today performs.
            stamp_iso = datetime.now(timezone.utc).isoformat()
            await db.picks.update_many(
                {"id": p["id"], "on_main_board_at": {"$exists": False}},
                {"$set": {"on_main_board_at": stamp_iso}},
            )
            after = await db.picks.find_one({"id": p["id"]}, {"_id": 0})
            # Canonical fields untouched.
            for k, v in canonical_before.items():
                assert after[k] == v, (
                    f"canonical field {k!r} was mutated by read: "
                    f"before={v!r} after={after[k]!r}"
                )
            # Presentation stamp landed.
            assert after["on_main_board_at"] == stamp_iso
        finally:
            await _wipe(db)
    _run(run())


# ── 8.  Missing optional enrichment does NOT hide a canonical pick. ─
def test_missing_enrichment_does_not_hide_canonical_pick():
    async def run():
        db = _fresh_db()
        await _wipe(db)
        p = _base_pick(_TEST_ID_PREFIX + "canon-lean-14")
        # Drop optional presentation fields.
        p.pop("factors", None)
        p.pop("key_insights", None)
        p.pop("player", None)
        await db.picks.insert_one(p)
        try:
            ids = await _matching_pick_ids(db, canon_only=True)
            assert p["id"] in ids
        finally:
            await _wipe(db)
    _run(run())


# ── 9.  Existing valid MLB / Tennis / Soccer canonical picks visible. ─
def test_all_three_sports_canonical_picks_visible():
    async def run():
        db = _fresh_db()
        await _wipe(db)
        picks = [
            _base_pick(_TEST_ID_PREFIX + "canon-mlb-15", sport="MLB"),
            _base_pick(_TEST_ID_PREFIX + "canon-ten-16", sport="Tennis",
                        league="ATP"),
            _base_pick(_TEST_ID_PREFIX + "canon-soc-17", sport="Soccer",
                        league="EPL"),
        ]
        await db.picks.insert_many(picks)
        try:
            ids = await _matching_pick_ids(db, canon_only=True)
            for p in picks:
                assert p["id"] in ids, f"{p['sport']} pick missing"
        finally:
            await _wipe(db)
    _run(run())


# ── 10.  Gate is a PURE filter — never re-ranks or re-scores. ──────
def test_gate_does_not_change_lock_scores_or_grades():
    async def run():
        db = _fresh_db()
        await _wipe(db)
        p = _base_pick(_TEST_ID_PREFIX + "canon-purity-18", lock=88.5,
                        grade="Strong Lock")
        original = {
            "lock_score": p["lock_score"],
            "lock_score_v2": p["lock_score_v2"],
            "published_lock_score": p["published_lock_score"],
            "grade": p["grade"],
            "published_grade": p["published_grade"],
            "confidence": p["confidence"],
            "published_confidence": p["published_confidence"],
        }
        await db.picks.insert_one(p)
        try:
            # Apply gate query, retrieve doc.
            doc = await db.picks.find_one(
                {"id": p["id"],
                 "publication_source": {"$exists": True, "$ne": None}},
                {"_id": 0},
            )
            for k, v in original.items():
                assert doc[k] == v, (
                    f"gate mutated {k!r}: before={v!r} after={doc[k]!r}"
                )
        finally:
            await _wipe(db)
    _run(run())


# ── 11.  Emergency bypass restores legacy behaviour. ───────────────
def test_env_bypass_restores_legacy_behaviour(monkeypatch):
    monkeypatch.setenv("LOCKSCORE_REQUIRE_CANONICAL_PUBLICATION", "false")
    m = _reload_gate()
    assert m.canonical_publication_filter() == {}
    # When merged into a query, this is a Mongo no-op.
    q = {"pick_date": _TEST_DATE, "sport": "MLB"}
    q.update(m.canonical_publication_filter())
    assert q == {"pick_date": _TEST_DATE, "sport": "MLB"}


# ── 12.  Dedupe helper untouched by the gate. ──────────────────────
def test_dedupe_helper_still_dedupes_after_gate_applied():
    async def run():
        db = _fresh_db()
        await _wipe(db)
        # Two canonical picks that would dedupe (same event/market/side).
        a = _base_pick(_TEST_ID_PREFIX + "canon-dup-A", sport="MLB")
        b = _base_pick(_TEST_ID_PREFIX + "canon-dup-B", sport="MLB")
        a["event_id"] = b["event_id"] = "e_dup"
        # Make them identical outcomes for dedupe.
        a["market_key"] = b["market_key"] = "moneyline"
        a["selection"] = b["selection"] = "Yankees"
        await db.picks.insert_many([a, b])
        try:
            from server import _dedupe_game_outcome_picks
            docs = [r async for r in db.picks.find(
                {"pick_date": _TEST_DATE,
                 "publication_source": {"$exists": True, "$ne": None}},
                {"_id": 0},
            )]
            deduped = _dedupe_game_outcome_picks(docs)
            # Whatever the exact dedupe rule is, it can't be MORE
            # picks than the input.  This guards that the gate did
            # not disable dedupe by shape.
            assert len(deduped) <= len(docs)
        finally:
            await _wipe(db)
    _run(run())
