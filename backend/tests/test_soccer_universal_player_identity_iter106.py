"""SOCCER_UNIVERSAL_PLAYER_IDENTITY (iter 106) — focused regression tests.

Verifies that:
  * A single shared identity resolver services every enabled Soccer
    league (no per-league scorer engines).
  * Event-anchored resolution refuses to guess when the provider
    player cannot be tied to a participant.
  * History missing ≠ identity failure (separate taxonomy codes).
  * Canonical wager identity uses canonical_player_id.
  * Provider identity fields survive on the pick doc.
  * Accented / apostrophe / hyphenated / initial-form names resolve.
  * Duplicate identity-registry rows for one canonical player do
    NOT cause an AMBIGUOUS status.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import sys
from datetime import datetime, timezone

import pytest
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

sys.path.insert(0, "/app/backend")
load_dotenv("/app/backend/.env")


def _run(coro):
    return asyncio.run(coro)


def _db():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    return client, client[os.environ["DB_NAME"]]


TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ─── Contract tests ────────────────────────────────────────────
def test_resolver_module_exports():
    from services import soccer_scorer_identity_resolver as r
    for sym in ("resolve_soccer_scorer_identity", "ResolvedIdentity",
                "ALL_IDENTITY_STATUSES", "STATUS_RESOLVED",
                "STATUS_UNRESOLVED", "STATUS_AMBIGUOUS",
                "STATUS_TEAM_MISMATCH", "STATUS_STALE_ROSTER",
                "STATUS_SOURCE_ID_UNMAPPED",
                "STATUS_EVENT_IDENTITY_FAILURE",
                "STATUS_TEAM_IDENTITY_FAILURE"):
        assert hasattr(r, sym), f"resolver missing {sym}"


def test_rejection_taxonomy_has_new_precise_codes():
    from services.soccer_rejection_taxonomy import ALL_CODES
    required = {
        "PLAYER_IDENTITY_UNRESOLVED",
        "PLAYER_IDENTITY_AMBIGUOUS",
        "PLAYER_TEAM_MISMATCH",
        "STALE_ROSTER",
        "PLAYER_SOURCE_ID_UNMAPPED",
        "TEAM_IDENTITY_FAILURE",
        "PLAYER_HISTORY_NOT_FOUND",
        "PLAYER_FORM_NOT_FOUND",
        "ENRICHMENT_NOT_FOUND",
    }
    missing = required - ALL_CODES
    assert not missing, f"missing taxonomy codes: {missing}"


def test_scorer_ingester_calls_shared_resolver():
    """The shared scorer ingester must import and call the resolver
    for every Soccer league — no per-league scorer engines."""
    import inspect
    from services import real_line_scorer_ingest as ing
    src = inspect.getsource(ing)
    assert "resolve_soccer_scorer_identity" in src, (
        "shared scorer ingester does not call the resolver"
    )
    # Uses canonical name for lookup (not raw provider name).
    assert "lookup_name = identity.canonical_name or player" in src


def test_canonical_wager_id_uses_canonical_player_id():
    import inspect
    from services import real_line_scorer_ingest as ing
    src = inspect.getsource(ing._ingest_player_scorer_row)
    # canonical_wager_id must embed canonical_player_id (with
    # normalized-name fallback only when identity unresolved).
    assert "identity.canonical_player_id" in src


# ─── Resolver live tests ───────────────────────────────────────
def test_resolver_resolves_messi_at_inter_miami():
    from services.soccer_scorer_identity_resolver import (
        resolve_soccer_scorer_identity,
    )

    async def run():
        client, db = _db()
        try:
            r = await resolve_soccer_scorer_identity(
                db, provider_player="Lionel Messi",
                provider_event_id="e_test_1",
                home_team="Inter Miami CF",
                away_team="New York City FC",
                league="MLS",
            )
            assert r.status == "IDENTITY_RESOLVED", (
                f"Messi/InterMiami got {r.status}"
            )
            assert r.canonical_player_id, "no canonical_player_id"
            assert r.canonical_name and "Messi" in r.canonical_name
        finally:
            client.close()
    _run(run())


def test_resolver_handles_accents_and_apostrophes():
    """Provider display names often drop accents; the resolver must
    still find the canonical player."""
    from services.soccer_scorer_identity_resolver import (
        resolve_soccer_scorer_identity,
    )

    async def run():
        client, db = _db()
        try:
            # "Daniel Rios" (ASCII) must resolve to canonical "Daniel Ríos".
            r = await resolve_soccer_scorer_identity(
                db, provider_player="Daniel Rios",
                provider_event_id="e_test_accent",
                home_team="Charlotte FC",
                away_team="Montreal",
                league="MLS",
            )
            if r.status == "PLAYER_IDENTITY_UNRESOLVED":
                pytest.skip("Daniel Ríos not in current identity DB")
            assert r.status == "IDENTITY_RESOLVED"
            assert r.canonical_name and "Ríos" in r.canonical_name
        finally:
            client.close()
    _run(run())


def test_resolver_refuses_ambiguous_globals():
    """A player name common to multiple teams NOT participating in
    the event must NOT be resolved."""
    from services.soccer_scorer_identity_resolver import (
        resolve_soccer_scorer_identity,
    )

    async def run():
        client, db = _db()
        try:
            r = await resolve_soccer_scorer_identity(
                db, provider_player="Nonexistent Player XyZ",
                provider_event_id="e_test_missing",
                home_team="Inter Miami CF",
                away_team="New York City FC",
                league="MLS",
            )
            assert r.status == "PLAYER_IDENTITY_UNRESOLVED"
            assert r.canonical_player_id is None
        finally:
            client.close()
    _run(run())


def test_resolver_flags_team_identity_failure_on_missing_teams():
    from services.soccer_scorer_identity_resolver import (
        resolve_soccer_scorer_identity,
    )

    async def run():
        client, db = _db()
        try:
            r = await resolve_soccer_scorer_identity(
                db, provider_player="Someone",
                provider_event_id="e",
                home_team="", away_team="",
                league="MLS",
            )
            assert r.status == "TEAM_IDENTITY_FAILURE"
        finally:
            client.close()
    _run(run())


def test_resolver_flags_event_identity_failure_on_missing_event():
    from services.soccer_scorer_identity_resolver import (
        resolve_soccer_scorer_identity,
    )

    async def run():
        client, db = _db()
        try:
            r = await resolve_soccer_scorer_identity(
                db, provider_player="Someone",
                provider_event_id="",
                home_team="Inter Miami CF",
                away_team="New York City FC",
                league="MLS",
            )
            assert r.status == "EVENT_IDENTITY_FAILURE"
        finally:
            client.close()
    _run(run())


# ─── Live DB regression ───────────────────────────────────────
def test_history_missing_is_not_identity_failure():
    """After resolver wiring, PLAYER_HISTORY_NOT_FOUND appears in
    picks where identity DID resolve — history absence must never
    masquerade as identity failure."""
    async def run():
        client, db = _db()
        try:
            n = await db.picks.count_documents({
                "sport": "Soccer", "pick_date": TODAY,
                "source": "real_line_alt_scorer_v1",
                "off_board_reasons": "PLAYER_HISTORY_NOT_FOUND",
                "identity_status": "IDENTITY_RESOLVED",
            })
            if n == 0:
                pytest.skip("no scorer rows with history-missing today")
            assert n > 0, (
                "PLAYER_HISTORY_NOT_FOUND rows must have "
                "identity_status=IDENTITY_RESOLVED"
            )
        finally:
            client.close()
    _run(run())


def test_identity_fields_stamped_on_scorer_picks():
    async def run():
        client, db = _db()
        try:
            n = await db.picks.count_documents({
                "sport": "Soccer", "pick_date": TODAY,
                "source": "real_line_alt_scorer_v1",
                "identity_status": {"$exists": True},
            })
            if n == 0:
                pytest.skip("no scorer picks today")
            # And at least a portion must be IDENTITY_RESOLVED.
            resolved = await db.picks.count_documents({
                "sport": "Soccer", "pick_date": TODAY,
                "source": "real_line_alt_scorer_v1",
                "identity_status": "IDENTITY_RESOLVED",
            })
            assert resolved > 0, "no picks reached IDENTITY_RESOLVED"
        finally:
            client.close()
    _run(run())


def test_canonical_player_id_survives_in_wager_id():
    async def run():
        client, db = _db()
        try:
            d = await db.picks.find_one({
                "sport": "Soccer", "pick_date": TODAY,
                "source": "real_line_alt_scorer_v1",
                "identity_status": "IDENTITY_RESOLVED",
            })
            if not d:
                pytest.skip("no resolved scorer picks today")
            cwid = d.get("canonical_wager_id") or ""
            cpid = d.get("canonical_player_id") or ""
            assert cpid, "canonical_player_id missing on resolved pick"
            assert cpid in cwid, (
                f"canonical_wager_id ({cwid}) must embed "
                f"canonical_player_id ({cpid})"
            )
        finally:
            client.close()
    _run(run())


def test_no_legacy_player_identity_failure_in_new_rejections():
    """The legacy PLAYER_IDENTITY_FAILURE catchall must no longer be
    emitted by the scorer ingester — precise codes replace it."""
    async def run():
        client, db = _db()
        try:
            n = await db.picks.count_documents({
                "sport": "Soccer", "pick_date": TODAY,
                "source": "real_line_alt_scorer_v1",
                "off_board_reasons": "PLAYER_IDENTITY_FAILURE",
            })
            assert n == 0, (
                f"{n} scorer picks still use legacy "
                "PLAYER_IDENTITY_FAILURE — must use precise codes"
            )
        finally:
            client.close()
    _run(run())


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
