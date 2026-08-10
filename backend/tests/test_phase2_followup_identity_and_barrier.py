"""Phase 2 Follow-up (2026-08-11) — canonical player identity +
final publication barrier.
"""
from __future__ import annotations

import pathlib


_BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]


# ── 1. Canonical player identity ────────────────────────────────────
def test_identity_new_player_mints_canonical_id():
    from services.player_identity import (
        upsert_player, reset_registry_for_tests,
    )
    reset_registry_for_tests()
    p = upsert_player(name="Leo Walta", sport="Soccer", league="Veikkausliiga",
                       provider="espn", provider_id="ESPN-11",
                       current_team="Inter Turku",
                       observed_at="2026-08-01T00:00:00+00:00",
                       source="espn_roster")
    assert p.canonical_player_id.startswith("cpid_")
    assert p.name == "Leo Walta"
    assert p.current_team == "Inter Turku"
    assert p.provider_ids == {"espn": "ESPN-11"}


def test_identity_resolves_by_provider_id_across_sources():
    from services.player_identity import (
        upsert_player, resolve_player, reset_registry_for_tests,
    )
    reset_registry_for_tests()
    a = upsert_player(name="Kylian Mbappé", sport="Soccer", league="LaLiga",
                       provider="espn", provider_id="ESPN-42",
                       current_team="Real Madrid",
                       observed_at="2026-08-01T00:00:00+00:00")
    # Second call with SAME provider id but slightly different spelling
    # returns the SAME identity (not a new one).
    b = upsert_player(name="Kylian Mbappe", sport="Soccer", league="LaLiga",
                       provider="espn", provider_id="ESPN-42",
                       current_team="Real Madrid",
                       observed_at="2026-08-01T01:00:00+00:00")
    assert a.canonical_player_id == b.canonical_player_id


def test_identity_similar_names_never_merge():
    """Two DIFFERENT players named "Luis Fernandez" (different DOBs
    OR different provider ids) MUST get different canonical ids."""
    from services.player_identity import (
        upsert_player, reset_registry_for_tests,
    )
    reset_registry_for_tests()
    a = upsert_player(name="Luis Fernandez", sport="Soccer",
                       league="LigaMX", provider="espn",
                       provider_id="ESPN-777", dob="1998-01-01",
                       current_team="Club A")
    b = upsert_player(name="Luis Fernandez", sport="Soccer",
                       league="LigaMX", provider="espn",
                       provider_id="ESPN-888", dob="1993-06-15",
                       current_team="Club B")
    assert a.canonical_player_id != b.canonical_player_id


def test_identity_transfer_preserves_history():
    """A player transferring between teams keeps historical_teams
    with date ranges — historical teams NEVER become current-team
    truth."""
    from services.player_identity import (
        upsert_player, reset_registry_for_tests,
    )
    reset_registry_for_tests()
    p = upsert_player(name="Victor Lind", sport="Soccer",
                       league="Veikkausliiga",
                       provider="espn", provider_id="V1",
                       current_team="HJK",
                       observed_at="2025-06-01T00:00:00+00:00",
                       source="espn_2025")
    # Transfer to Ilves.
    p = upsert_player(name="Victor Lind", sport="Soccer",
                       league="Veikkausliiga",
                       provider="espn", provider_id="V1",
                       current_team="Ilves",
                       observed_at="2026-01-15T00:00:00+00:00",
                       source="espn_2026")
    assert p.current_team == "Ilves"
    assert len(p.historical_teams) >= 2
    # First entry closes at the transfer time; second entry is open.
    entries = {e["team"]: e for e in p.historical_teams}
    assert "HJK" in entries and entries["HJK"]["to"] is not None
    assert "Ilves" in entries and entries["Ilves"]["to"] is None


def test_identity_freshness_gate_flags_stale_current_team():
    from services.player_identity import (
        upsert_player, reset_registry_for_tests,
    )
    reset_registry_for_tests()
    p = upsert_player(name="Tokmac Nguen", sport="Soccer",
                       league="Eliteserien",
                       provider="espn", provider_id="TN1",
                       current_team="Aalesund",
                       observed_at="2024-01-01T00:00:00+00:00")
    # 30-day window default → 2024 observation is stale.
    assert p.is_current_team_fresh(staleness_days=30) is False
    # A generous window keeps it fresh.
    assert p.is_current_team_fresh(staleness_days=99999) is True


def test_identity_stats_attach_across_transfers():
    """Historical stats must be keyed by canonical_player_id so they
    follow the player through a transfer."""
    from services.player_identity import (
        upsert_player, reset_registry_for_tests,
    )
    reset_registry_for_tests()
    a = upsert_player(name="Test Striker", sport="Soccer",
                       league="X", provider="p", provider_id="1",
                       current_team="Old FC",
                       observed_at="2025-01-01T00:00:00+00:00")
    a_stats = {"canonical_player_id": a.canonical_player_id,
                "goals": 12, "team": "Old FC"}
    b = upsert_player(name="Test Striker", sport="Soccer",
                       league="X", provider="p", provider_id="1",
                       current_team="New FC",
                       observed_at="2026-02-01T00:00:00+00:00")
    # Same canonical id → stats still attached.
    assert a_stats["canonical_player_id"] == b.canonical_player_id
    # But the current team truth reflects the transfer.
    assert b.current_team == "New FC"


def test_registry_snapshot_and_hydrate_roundtrip():
    from services.player_identity import (
        upsert_player, snapshot_registry, hydrate_registry,
        reset_registry_for_tests, resolve_player,
    )
    reset_registry_for_tests()
    upsert_player(name="A", sport="Soccer", league="L",
                   provider="p", provider_id="1", current_team="T")
    docs = snapshot_registry()
    reset_registry_for_tests()
    hydrate_registry(docs)
    resolved = resolve_player(name="A", sport="Soccer", league="L")
    assert resolved is not None
    assert resolved.current_team == "T"


# ── 2. Integrity gate is INSIDE publish_batch ───────────────────────
def test_publish_batch_has_integrity_gate_inside():
    src = (_BACKEND_ROOT / "services"
           / "prediction_publication_service.py").read_text()
    idx = src.find("async def publish_batch(")
    assert idx > 0
    body_end = src.find("async def ", idx + 10)
    body = src[idx:body_end if body_end > 0 else -1]
    assert "Layer-B integrity gate" in body, (
        "publish_batch must contain the integrity gate marker"
    )
    assert "validate_player_fixture_pick" in body
    # gate must run BEFORE the publish loop.
    gate_idx = body.find("validate_player_fixture_pick")
    loop_idx = body.find("for cand in candidates_list")
    assert 0 < gate_idx < loop_idx


def test_publish_batch_returns_integrity_rejection_count():
    """The batch summary exposes `integrity_rejected` so callers can
    log/report barrier drops."""
    src = (_BACKEND_ROOT / "services"
           / "prediction_publication_service.py").read_text()
    assert '"integrity_rejected"' in src
    assert '"integrity_rejections"' in src


def test_direct_publish_batch_call_rejects_invalid_pick():
    """The critical acceptance test: a caller that skips
    `publication_helpers` and the orchestrator gate and invokes
    `PredictionPublicationService.publish_batch` DIRECTLY must still
    not publish an invalid Soccer player prop."""
    import asyncio, os, uuid
    from datetime import datetime, timezone, timedelta
    from dotenv import load_dotenv
    load_dotenv("/app/backend/.env")
    from motor.motor_asyncio import AsyncIOMotorClient

    async def go():
        db = AsyncIOMotorClient(os.environ["MONGO_URL"])[
            os.environ.get("DB_NAME", "lockscore_db")]
        from services.prediction_publication_service import (
            PredictionPublicationService,
        )
        pub = PredictionPublicationService(db)
        pid = "phase2fu_" + uuid.uuid4().hex[:12]
        # Deliberately invalid: player_current_team is NOT on the fixture.
        pick = {
            "id": pid, "sport": "Soccer", "league": "Veikkausliiga",
            "event": "Inter Turku vs KuPS",
            "market": "Victor Lind - Anytime Goal Scorer",
            "player_name": "Victor Lind",
            "player_current_team": "Ilves",   # NOT on the fixture
            "event_time": (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat(),
            "win_probability": 62.0, "lock_score": 92.0,
            "grade": "Elite Lock", "confidence": "Very High",
            "book_odds": None, "edge_percent": None,
            "pick_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }
        await db.picks.delete_many({"id": pid})
        await db.prediction_snapshots.delete_many({"prediction_id": pid})
        await db.picks.insert_one(pick)
        try:
            summary = await pub.publish_batch([pick])
            assert summary["integrity_rejected"] == 1
            assert summary["new_snapshots"] == 0
            # No snapshot was created.
            snap = await db.prediction_snapshots.find_one(
                {"prediction_id": pid}, {"_id": 0})
            assert snap is None
            # Pick doc has NO publication_source stamped by the barrier.
            after = await db.picks.find_one({"id": pid}, {"_id": 0})
            assert after.get("publication_source") in (None, "")
            # The barrier reports the rejection via the summary so callers
            # can persist the off_board flag; the in-memory pick dict is
            # tagged (verified below via the summary payload).
            rej = summary["integrity_rejections"][0]
            assert rej["prediction_id"] == pid
            assert rej["reason"] == "player_team_mismatch"
        finally:
            await db.picks.delete_many({"id": pid})
            await db.prediction_snapshots.delete_many({"prediction_id": pid})
    asyncio.run(go())


def test_direct_publish_batch_accepts_valid_pick():
    """Valid picks still publish through the direct barrier."""
    import asyncio, os, uuid
    from datetime import datetime, timezone, timedelta
    from dotenv import load_dotenv
    load_dotenv("/app/backend/.env")
    from motor.motor_asyncio import AsyncIOMotorClient

    async def go():
        db = AsyncIOMotorClient(os.environ["MONGO_URL"])[
            os.environ.get("DB_NAME", "lockscore_db")]
        from services.prediction_publication_service import (
            PredictionPublicationService,
        )
        pub = PredictionPublicationService(db)
        pid = "phase2fu_v_" + uuid.uuid4().hex[:12]
        pick = {
            "id": pid, "sport": "Soccer", "league": "Veikkausliiga",
            "event": "Inter Turku vs KuPS",
            "market": "Leo Walta - Anytime Goal Scorer",
            "player_name": "Leo Walta",
            "player_current_team": "Inter Turku",
            "event_time": (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat(),
            "win_probability": 62.0, "lock_score": 92.0,
            "grade": "Elite Lock", "confidence": "Very High",
            "book_odds": None, "edge_percent": None,
            "pick_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }
        await db.picks.delete_many({"id": pid})
        await db.prediction_snapshots.delete_many({"prediction_id": pid})
        await db.picks.insert_one(pick)
        try:
            summary = await pub.publish_batch([pick])
            assert summary["integrity_rejected"] == 0
            assert summary["new_snapshots"] == 1
        finally:
            await db.picks.delete_many({"id": pid})
            await db.prediction_snapshots.delete_many({"prediction_id": pid})
    asyncio.run(go())


# ── 3. Dry-run scanner exists and returns a shape ──────────────────
def test_dryrun_scanner_shape():
    import asyncio, os
    from dotenv import load_dotenv
    load_dotenv("/app/backend/.env")
    from motor.motor_asyncio import AsyncIOMotorClient

    async def go():
        db = AsyncIOMotorClient(os.environ["MONGO_URL"])[
            os.environ.get("DB_NAME", "lockscore_db")]
        from scripts.dryrun_player_team_integrity import dryrun_scan
        stats = await dryrun_scan(db)
        for k in ("scanned", "valid", "team_mismatch",
                   "roster_unverified", "would_be_ineligible",
                   "over_85_ineligible", "already_off_board"):
            assert k in stats
    asyncio.run(go())


# ── 4. Regression sanity ───────────────────────────────────────────
def test_locks_contract_still_strict_gt_85():
    from services.main_board_eligibility import is_main_board_eligible
    assert is_main_board_eligible({"lock_score": 85.0}) is False
    assert is_main_board_eligible({"lock_score": 85.001}) is True
