"""Session D — Soccer + Tennis identity remediation tests.

Deterministic (no live provider dependency).  Uses the real Mongo
instance but with a `sess_d_` id prefix and isolated registry docs.
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
        os.environ.get("DB_NAME", "lockscore_db")]


def _run(c):
    return asyncio.run(c)


async def _wipe(db):
    await db.picks.delete_many({"id": {"$regex": "^sess_d_"}})
    await db.player_identities.delete_many(
        {"canonical_player_id": {"$regex": "^sess_d_"}})
    # Tennis_players test rows use "sess d ..." (space-normalized).
    await db.tennis_players.delete_many({"name_norm": {"$regex": "^sess d "}})
    await db.tennis_players.delete_many({"name_norm": "same name"})


# ══════════════════════════════════════════════════════════════════
# Utilities
# ══════════════════════════════════════════════════════════════════
def test_normalize_name_strips_accents_and_case():
    from services.pick_identity_remediation_soccer_tennis import (
        normalize_name,
    )
    assert normalize_name("Vinícius Júnior")   == "vinicius junior"
    assert normalize_name("  ZLATAN  Ibrahimović ") == "zlatan ibrahimovic"
    assert normalize_name("O'Neal")             == "o'neal"
    assert normalize_name(None)                 == ""    # type: ignore[arg-type]


# ══════════════════════════════════════════════════════════════════
# Soccer
# ══════════════════════════════════════════════════════════════════
def test_soccer_unique_name_promotes_to_MAPPED():
    async def run():
        db = _db(); await _wipe(db)
        # Seed authoritative registry entry.
        await db.player_identities.insert_one({
            "sport":  "Soccer",
            "canonical_player_id": "sess_d_cpid_messi",
            "name":         "SessDTest MessiUnique",
            "name_norm":    "sessdtest messiunique",
            "league":       "MLS",
            "current_team": "Inter Miami CF",
            "historical_teams": [{"team": "Inter Miami CF"}],
            "source": "espn_mls_leaders",
        })
        # PROVISIONAL pick with player + team hint matching registry.
        await db.picks.insert_one({
            "id":              "sess_d_soccer_ok",
            "sport":           "Soccer",
            "league":          "MLS",
            "market":          "SessDTest MessiUnique Anytime Goal Scorer",
            "player_name":     "SessDTest MessiUnique",
            "team":            "Inter Miami CF",
            "identity_class":  "PROVISIONAL",
            "canonical_player_id": "fallback:aaa",
        })
        from services.pick_identity_remediation_soccer_tennis import (
            remediate_soccer,
        )
        s = await remediate_soccer(db, limit=50, dry_run=False, id_prefix="sess_d_")
        assert s.promoted == 1
        got = await db.picks.find_one({"id": "sess_d_soccer_ok"},
                                          projection={"_id": 0})
        assert got["identity_class"] == "MAPPED"
        assert got["canonical_player_id"] == "sess_d_cpid_messi"
        assert got["identity_evidence"]["source"] == "player_identities"
        await _wipe(db)
    _run(run())


def test_soccer_ambiguous_name_stays_provisional():
    async def run():
        db = _db(); await _wipe(db)
        await db.player_identities.insert_many([
            {"sport": "Soccer", "canonical_player_id": "sess_d_cpid_A",
             "name": "John Smith", "name_norm": "john smith",
             "league": "EPL", "current_team": "Team A", "source": "test"},
            {"sport": "Soccer", "canonical_player_id": "sess_d_cpid_B",
             "name": "John Smith", "name_norm": "john smith",
             "league": "Serie A", "current_team": "Team B", "source": "test"},
        ])
        await db.picks.insert_one({
            "id": "sess_d_soccer_ambig", "sport": "Soccer",
            "market": "John Smith Anytime Goal Scorer",
            "player_name": "John Smith",
            "team": "Team X",       # matches NEITHER
            "identity_class": "PROVISIONAL",
        })
        from services.pick_identity_remediation_soccer_tennis import (
            remediate_soccer,
        )
        s = await remediate_soccer(db, limit=50, dry_run=False, id_prefix="sess_d_")
        assert s.promoted == 0
        assert s.unchanged_ambiguous == 1
        got = await db.picks.find_one({"id": "sess_d_soccer_ambig"},
                                          projection={"_id": 0})
        assert got["identity_class"] == "PROVISIONAL"
        await _wipe(db)
    _run(run())


def test_soccer_transfer_retains_same_canonical_id():
    async def run():
        db = _db(); await _wipe(db)
        # Registry entry: player currently at Team B, historically Team A.
        await db.player_identities.insert_one({
            "sport": "Soccer",
            "canonical_player_id": "sess_d_cpid_transfer",
            "name": "SessDTest MbappeUnique", "name_norm": "sessdtest mbappeunique",
            "league": "La Liga", "current_team": "Real Madrid",
            "historical_teams": [
                {"team": "Paris Saint-Germain FC",
                    "from": "2017-01-01", "to": "2024-06-30"},
                {"team": "Real Madrid",
                    "from": "2024-07-01", "to": None},
            ],
            "source": "test",
        })
        # OLD pick when player was at PSG.
        await db.picks.insert_one({
            "id": "sess_d_soccer_transfer_old", "sport": "Soccer",
            "league": "Ligue 1", "market": "SessDTest MbappeUnique Anytime Goal Scorer",
            "player_name": "SessDTest MbappeUnique", "team": "Paris Saint-Germain FC",
            "identity_class": "PROVISIONAL",
        })
        # NEW pick at Real Madrid.
        await db.picks.insert_one({
            "id": "sess_d_soccer_transfer_new", "sport": "Soccer",
            "league": "La Liga", "market": "SessDTest MbappeUnique Anytime Goal Scorer",
            "player_name": "SessDTest MbappeUnique", "team": "Real Madrid",
            "identity_class": "PROVISIONAL",
        })
        from services.pick_identity_remediation_soccer_tennis import (
            remediate_soccer,
        )
        s = await remediate_soccer(db, limit=50, dry_run=False, id_prefix="sess_d_")
        assert s.promoted == 2
        old = await db.picks.find_one({"id": "sess_d_soccer_transfer_old"},
                                          projection={"_id": 0})
        new = await db.picks.find_one({"id": "sess_d_soccer_transfer_new"},
                                          projection={"_id": 0})
        # SAME canonical id across the transfer — team differs.
        assert old["canonical_player_id"] == "sess_d_cpid_transfer"
        assert new["canonical_player_id"] == "sess_d_cpid_transfer"
        await _wipe(db)
    _run(run())


def test_soccer_ghost_team_rejected():
    async def run():
        db = _db(); await _wipe(db)
        # Registry: player at Team A.  Pick claims Team B (not in
        # historical_teams).  Ghost-team rejection kicks in.
        await db.player_identities.insert_one({
            "sport": "Soccer",
            "canonical_player_id": "sess_d_cpid_ghost",
            "name": "Some Player", "name_norm": "some player",
            "league": "MLS", "current_team": "LA Galaxy",
            "historical_teams": [{"team": "LA Galaxy"}],
            "source": "test",
        })
        await db.picks.insert_one({
            "id": "sess_d_soccer_ghost", "sport": "Soccer",
            "league": "MLS",
            "market": "Some Player Anytime Goal Scorer",
            "player_name": "Some Player", "team": "AC Milan",
            "identity_class": "PROVISIONAL",
        })
        from services.pick_identity_remediation_soccer_tennis import (
            remediate_soccer,
        )
        s = await remediate_soccer(db, limit=50, dry_run=False, id_prefix="sess_d_")
        assert s.promoted == 0
        assert s.rejected_ghost_team == 1
        got = await db.picks.find_one({"id": "sess_d_soccer_ghost"},
                                          projection={"_id": 0})
        assert got["identity_class"] == "PROVISIONAL"
        await _wipe(db)
    _run(run())


def test_soccer_team_market_untouched():
    async def run():
        db = _db(); await _wipe(db)
        # PROVISIONAL team market — no player_name and market string
        # doesn't include a player suffix.
        await db.picks.insert_one({
            "id": "sess_d_soccer_team_mkt", "sport": "Soccer",
            "league": "EPL", "market": "Total Goals Over 2.5",
            "identity_class": "PROVISIONAL",
        })
        from services.pick_identity_remediation_soccer_tennis import (
            remediate_soccer,
        )
        s = await remediate_soccer(db, limit=50, dry_run=False, id_prefix="sess_d_")
        assert s.promoted == 0
        assert s.unchanged_team_market == 1
        got = await db.picks.find_one({"id": "sess_d_soccer_team_mkt"},
                                          projection={"_id": 0})
        assert got["identity_class"] == "PROVISIONAL"
        await _wipe(db)
    _run(run())


def test_soccer_no_candidate_stays_provisional():
    async def run():
        db = _db(); await _wipe(db)
        await db.picks.insert_one({
            "id": "sess_d_soccer_no_cand", "sport": "Soccer",
            "league": "EPL",
            "market": "Nonexistent Player Anytime Goal Scorer",
            "player_name": "Nonexistent Player", "team": "EPL Club",
            "identity_class": "PROVISIONAL",
        })
        from services.pick_identity_remediation_soccer_tennis import (
            remediate_soccer,
        )
        s = await remediate_soccer(db, limit=50, dry_run=False, id_prefix="sess_d_")
        assert s.unchanged_no_candidate == 1
        got = await db.picks.find_one({"id": "sess_d_soccer_no_cand"},
                                          projection={"_id": 0})
        assert got["identity_class"] == "PROVISIONAL"
        await _wipe(db)
    _run(run())


# ══════════════════════════════════════════════════════════════════
# Tennis
# ══════════════════════════════════════════════════════════════════
def test_tennis_unique_name_promotes_with_opponent():
    async def run():
        db = _db(); await _wipe(db)
        await db.tennis_players.insert_many([
            {"name": "Sess D Player One", "name_norm": "sess d player one"},
            {"name": "Sess D Player Two", "name_norm": "sess d player two"},
        ])
        await db.picks.insert_one({
            "id": "sess_d_tennis_ok", "sport": "Tennis",
            "league": "ATP", "market": "Sess D Player One Moneyline",
            "player_name": "Sess D Player One",
            "opponent_team": "Sess D Player Two",
            "identity_class": "PROVISIONAL",
        })
        from services.pick_identity_remediation_soccer_tennis import (
            remediate_tennis,
        )
        s = await remediate_tennis(db, limit=50, dry_run=False, id_prefix="sess_d_")
        assert s.promoted == 1
        got = await db.picks.find_one({"id": "sess_d_tennis_ok"},
                                          projection={"_id": 0})
        assert got["identity_class"] == "MAPPED"
        assert got["canonical_player_id"] == "tp:sess d player one"
        assert got.get("canonical_opponent_id") == "tp:sess d player two"
        await _wipe(db)
    _run(run())


def test_tennis_ambiguous_name_stays_provisional():
    async def run():
        db = _db(); await _wipe(db)
        # Create 2 rows sharing the SAME normalized key via a
        # temporary collection-side workaround: since name_norm has
        # a unique index in production, we simulate ambiguity by
        # dropping the index on the test-only prefix.  Instead of
        # bypassing the index, we test the module's collision
        # protection via ``_tennis_candidate_lookup`` directly.
        from services.pick_identity_remediation_soccer_tennis import (
            _tennis_candidate_lookup, remediate_one_tennis,
        )
        # Monkey-patch the lookup to return 2 candidates.
        import services.pick_identity_remediation_soccer_tennis as mod
        original = mod._tennis_candidate_lookup
        async def two_cands(db, *, name_norm):  # noqa: ARG001
            return [
                {"name": "Same Name A", "name_norm": "same name"},
                {"name": "Same Name B", "name_norm": "same name"},
            ]
        mod._tennis_candidate_lookup = two_cands   # type: ignore[assignment]
        try:
            await db.picks.insert_one({
                "id": "sess_d_tennis_collision", "sport": "Tennis",
                "market": "Same Name Moneyline",
                "player_name": "Same Name",
                "identity_class": "PROVISIONAL",
            })
            from services.pick_identity_remediation_soccer_tennis import (
                remediate_tennis,
            )
            s = await remediate_tennis(db, limit=50, dry_run=False,
                                          id_prefix="sess_d_")
            assert s.promoted == 0
            assert s.unchanged_ambiguous == 1
            got = await db.picks.find_one({"id": "sess_d_tennis_collision"},
                                              projection={"_id": 0})
            assert got["identity_class"] == "PROVISIONAL"
        finally:
            mod._tennis_candidate_lookup = original   # type: ignore[assignment]
            await _wipe(db)
    _run(run())


def test_tennis_total_games_market_untouched():
    async def run():
        db = _db(); await _wipe(db)
        await db.picks.insert_one({
            "id": "sess_d_tennis_totals", "sport": "Tennis",
            "market": "Total Games Over 22.5",
            "selection": "Over",  # not a player name — market gate keeps it
            "identity_class": "PROVISIONAL",
        })
        from services.pick_identity_remediation_soccer_tennis import (
            remediate_tennis,
        )
        s = await remediate_tennis(db, limit=50, dry_run=False, id_prefix="sess_d_")
        got = await db.picks.find_one({"id": "sess_d_tennis_totals"},
                                          projection={"_id": 0})
        # 'Over' is not in tennis_players → no_candidate (safe).
        assert got["identity_class"] == "PROVISIONAL"
        await _wipe(db)
    _run(run())


def test_tennis_provisional_hash_cannot_consume_authority():
    """A pick that fails promotion (ambiguous / no candidate)
    retains its fallback ``canonical_player_id`` — NEVER promoted
    to AUTHORITATIVE — so downstream history consumption remains
    safely gated."""
    async def run():
        db = _db(); await _wipe(db)
        await db.picks.insert_one({
            "id": "sess_d_tennis_hash_safety", "sport": "Tennis",
            "market": "Unknown Player Moneyline",
            "player_name": "Unknown Player",
            "canonical_player_id": "fallback:deadbeef",
            "identity_class": "PROVISIONAL",
        })
        from services.pick_identity_remediation_soccer_tennis import (
            remediate_tennis,
        )
        await remediate_tennis(db, limit=50, dry_run=False, id_prefix="sess_d_")
        got = await db.picks.find_one({"id": "sess_d_tennis_hash_safety"},
                                          projection={"_id": 0})
        # Still PROVISIONAL, still fallback: hash never became authority.
        assert got["identity_class"] == "PROVISIONAL"
        assert got["canonical_player_id"] == "fallback:deadbeef"
        await _wipe(db)
    _run(run())
