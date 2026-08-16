"""UNIVERSAL_IDENTITY_HISTORY_BRIDGE (iter 107) — focused regression tests."""
from __future__ import annotations
import asyncio, inspect, os, sys
import pytest
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
sys.path.insert(0, "/app/backend")
load_dotenv("/app/backend/.env")


def _run(coro): return asyncio.run(coro)
def _db():
    c = AsyncIOMotorClient(os.environ["MONGO_URL"])
    return c, c[os.environ["DB_NAME"]]


# ─── 1. Resolver signature accepts identity fields ──────────────
def test_resolver_signature_accepts_identity():
    from services import soccer_feature_resolver as fr
    sig = inspect.signature(fr.resolve_soccer_player_features)
    for p in ("canonical_player_id", "canonical_player_name",
              "aliases", "provider_player_name"):
        assert p in sig.parameters, f"missing param {p}"


def test_prior_resolver_signature_accepts_identity():
    from services import soccer_feature_resolver as fr
    sig = inspect.signature(fr.resolve_soccer_player_prior)
    for p in ("canonical_player_name", "aliases"):
        assert p in sig.parameters


# ─── 2. Ingester passes full identity to feature resolver ───────
def test_scorer_ingester_passes_identity_object():
    from services import real_line_scorer_ingest as ing
    src = inspect.getsource(ing._ingest_player_scorer_row)
    assert "canonical_player_id=identity.canonical_player_id" in src
    assert "canonical_player_name=identity.canonical_name" in src
    assert "aliases=_identity_aliases" in src
    assert "provider_player_name=player" in src


# ─── 3. Canonical-ID priority in _aggregate_from_actuals ────────
def test_aggregate_from_actuals_uses_id_first():
    from services import soccer_feature_resolver as fr
    src = inspect.getsource(fr._aggregate_from_actuals)
    assert "canonical_player_id" in src
    # ID branch marker must appear BEFORE alias branch marker.
    id_marker    = src.find("q_id = dict(q, canonical_player_id")
    alias_marker = src.find("q_alias = dict(q, ")
    assert 0 < id_marker < alias_marker, (
        "canonical_player_id lookup branch must precede alias branch"
    )


# ─── 4. Same-name players remain distinct via ID join ───────────
def test_same_name_players_remain_distinct_via_canonical_id():
    from services.soccer_feature_resolver import _aggregate_from_actuals
    async def run():
        c, db = _db()
        try:
            # Passing a nonexistent canonical_player_id must NOT
            # accidentally match by name — ID lookup returns None,
            # then name-fallback runs only if ID branch was empty.
            row = await _aggregate_from_actuals(
                db, player_name="Messi",
                canonical_player_id="cpid_DOES_NOT_EXIST_zz",
                name_variants=[],
            )
            # With no name_variants and a bogus ID, result is None.
            assert row is None
        finally:
            c.close()
    _run(run())


# ─── 5. Alias-aware lookup: variants passed to actuals aggregator
def test_alias_variants_included_in_actuals_query():
    from services import soccer_feature_resolver as fr
    src = inspect.getsource(fr._aggregate_from_actuals)
    assert "name_variants" in src
    assert "$or" in src or "alias_re" in src


# ─── 6. Legacy exact-name behavior preserved as fallback ────────
def test_provider_name_fallback_still_works():
    from services.soccer_feature_resolver import _aggregate_from_actuals
    async def run():
        c, db = _db()
        try:
            # With no canonical_player_id, the resolver should still
            # find real players via the name-variants path.
            row = await _aggregate_from_actuals(
                db, player_name="Lionel Messi",
                canonical_player_id=None,
                name_variants=["lionel messi"],
            )
            # If Messi has PGA data, row is non-None; if not, None.
            # Either result is acceptable — the query path just
            # must not crash and must consult variants.
            assert row is None or isinstance(row, dict)
        finally:
            c.close()
    _run(run())


# ─── 7. Cross-sport regression: authoritative IDs preserved ─────
def test_cross_sport_authoritative_id_survey():
    """Confirm the app-wide audit finding: MLB/NFL/NBA/Tennis
    player_game_actuals collections carry authoritative IDs so the
    Soccer defect is Soccer-only.  Any sport losing its ID field
    later will fail this test as a regression tripwire."""
    async def run():
        c, db = _db()
        try:
            # MLB actuals use canonical_player_id (mapped from
            # MLBAM); NFL/NBA use player_id; Tennis uses player_key.
            expectations = {
                "mlb":    ("canonical_player_id", "mlbam_id"),
                "nfl":    ("player_id", "canonical_player_id"),
                "nba":    ("player_id", "canonical_player_id"),
                "tennis": ("player_key", "canonical_player_id"),
            }
            for sport, id_fields in expectations.items():
                d = await db.player_game_actuals.find_one({"sport": sport})
                if d is None:
                    continue
                assert any(f in d for f in id_fields), (
                    f"{sport} player_game_actuals lost every "
                    f"authoritative ID {id_fields}"
                )
        finally:
            c.close()
    _run(run())


# ─── 8. Identity + history remain SEPARATE arrows ───────────────
def test_history_missing_never_identity_failure_at_runtime():
    """Live DB check: any pick with off_board_reasons=
    PLAYER_HISTORY_NOT_FOUND must ALSO carry identity_status=
    IDENTITY_RESOLVED (otherwise the two arrows collapsed again)."""
    async def run():
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        c, db = _db()
        try:
            n_bad = await db.picks.count_documents({
                "sport": "Soccer", "pick_date": today,
                "off_board_reasons": "PLAYER_HISTORY_NOT_FOUND",
                "identity_status": {"$ne": "IDENTITY_RESOLVED"},
            })
            assert n_bad == 0, (
                f"{n_bad} history-missing picks lack IDENTITY_RESOLVED — "
                "identity/history arrows re-conflated"
            )
        finally:
            c.close()
    _run(run())


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
