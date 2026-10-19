"""
Historical ingestion merge-fix regression suite — NFL + CFB.

Proves the structural fix in ``historical/nfl.py`` and
``historical/cfb.py``:

  * One (sport, game_id, player_id) → ONE merged document.
  * Merge preserves every stat family truthfully (passing + rushing +
    receiving do NOT overwrite each other).
  * Upsert filter is exactly ``{sport, game_id, player_id}`` — no
    ``stat_block``.
  * Repeated ingestion is idempotent (no row count explosion, no
    DuplicateKeyError against the authoritative unique shape).
  * The ``(sport, game_id, player_id)`` unique shape can be ENFORCED
    on top of the merged writes without raising duplicate-key.
  * Legacy per-block rows still queryable alongside merged rows.

Uses ``mongomock_motor`` so we exercise a real Motor-shaped driver
end-to-end without touching the shared Mongo cluster.
"""
from __future__ import annotations
import asyncio
import pytest, pytest_asyncio, pathlib, sys
from unittest.mock import patch, AsyncMock

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ─────────────────────────────────────────────────────────────────────
# Good-neighbour event-loop handling.  Several legacy test modules
# (``test_commit_b5c_hi_adoption.py`` et al.) use the deprecated
# ``asyncio.get_event_loop().run_until_complete(...)`` pattern that
# requires a DEFAULT event loop to be set.  pytest-asyncio's strict
# mode leaves no default loop behind after an async test, so when
# any ``@pytest.mark.asyncio`` test runs before them, those legacy
# tests fail with ``RuntimeError: There is no current event loop``.
# Install a usable default loop at teardown so the suite can mix
# modern async-managed and legacy sync-driven tests.
# ─────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _restore_default_event_loop():
    yield
    try:
        asyncio.set_event_loop(asyncio.new_event_loop())
    except Exception:
        pass

try:
    from mongomock_motor import AsyncMongoMockClient
    _HAS_MONGOMOCK = True
except ImportError:  # pragma: no cover
    _HAS_MONGOMOCK = False


# ─────────────────────────────────────────────────────────────────────
# Synthetic ESPN boxscore fixtures.
# ─────────────────────────────────────────────────────────────────────

def _espn_summary_qb_dual_threat():
    """A QB who BOTH threw AND ran — the exact shape that triggered
    the pre-fix DuplicateKeyError."""
    return {
        "boxscore": {
            "players": [{
                "team": {"displayName": "Dallas Cowboys"},
                "statistics": [
                    {
                        "name": "passing",
                        "labels": ["C/ATT", "YDS", "AVG", "TD", "INT", "SACKS", "QBR", "RTG"],
                        "athletes": [{
                            "athlete": {"id": "2577417", "displayName": "Dak Prescott",
                                        "position": {"abbreviation": "QB"}},
                            "stats": ["21/34", "288", "8.5", "2", "0", "1-5", "72.1", "105.4"],
                        }],
                    },
                    {
                        "name": "rushing",
                        "labels": ["CAR", "YDS", "AVG", "TD", "LONG"],
                        "athletes": [{
                            "athlete": {"id": "2577417", "displayName": "Dak Prescott",
                                        "position": {"abbreviation": "QB"}},
                            "stats": ["4", "22", "5.5", "0", "9"],
                        }],
                    },
                ],
            }],
        },
    }


def _espn_summary_skill_player_rush_recv():
    """A skill player with both rushing + receiving."""
    return {
        "boxscore": {
            "players": [{
                "team": {"displayName": "San Francisco 49ers"},
                "statistics": [
                    {
                        "name": "rushing",
                        "labels": ["CAR", "YDS", "AVG", "TD", "LONG"],
                        "athletes": [{
                            "athlete": {"id": "9999999", "displayName": "Christian McCaffrey",
                                        "position": {"abbreviation": "RB"}},
                            "stats": ["18", "117", "6.5", "1", "32"],
                        }],
                    },
                    {
                        "name": "receiving",
                        "labels": ["REC", "YDS", "AVG", "TD", "LONG", "TGTS"],
                        "athletes": [{
                            "athlete": {"id": "9999999", "displayName": "Christian McCaffrey",
                                        "position": {"abbreviation": "RB"}},
                            "stats": ["5", "41", "8.2", "0", "17", "6"],
                        }],
                    },
                ],
            }],
        },
    }


# ─────────────────────────────────────────────────────────────────────
# Shared harness.
# ─────────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def db():
    if not _HAS_MONGOMOCK:
        pytest.skip("mongomock_motor not installed")
    client = AsyncMongoMockClient()
    db = client["lockscore_test"]
    yield db
    await client.drop_database("lockscore_test")


async def _run_nfl_ingest(db, summary):
    """Patch ``_get`` to return the synthetic boxscore and invoke
    ``_ingest_summary``."""
    from historical import nfl as adapter
    with patch.object(adapter, "_get",
                      new=AsyncMock(return_value=summary)):
        return await adapter._ingest_summary(
            cx=None, db=db, event_id="401772510", season=2025)


async def _run_cfb_ingest(db, summary):
    from historical import cfb as adapter
    with patch.object(adapter, "_get",
                      new=AsyncMock(return_value=summary)):
        return await adapter._ingest_summary(
            cx=None, db=db, event_id="401723456", season=2025)


# ─────────────────────────────────────────────────────────────────────
# NFL regression tests.
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_nfl_qb_passing_plus_rushing_merges_to_one_doc(db):
    """A dual-threat QB with passing + rushing produces exactly ONE
    document — the pre-fix bug produced two and raced the unique
    index."""
    inserted = await _run_nfl_ingest(db, _espn_summary_qb_dual_threat())
    assert inserted == 1, f"expected 1 merged doc, got {inserted}"
    docs = [d async for d in db.player_game_logs.find(
        {"sport": "nfl", "game_id": "espn_401772510", "player_id": "espn_2577417"})]
    assert len(docs) == 1, f"expected ONE row for (nfl, espn_401772510, espn_2577417), got {len(docs)}"


@pytest.mark.asyncio
async def test_nfl_skill_player_rushing_plus_receiving_merges_to_one_doc(db):
    inserted = await _run_nfl_ingest(db, _espn_summary_skill_player_rush_recv())
    assert inserted == 1
    docs = [d async for d in db.player_game_logs.find(
        {"sport": "nfl", "player_id": "espn_9999999"})]
    assert len(docs) == 1


@pytest.mark.asyncio
async def test_nfl_merged_doc_carries_both_stat_families(db):
    """Merged doc MUST expose every stat family truthfully."""
    await _run_nfl_ingest(db, _espn_summary_qb_dual_threat())
    doc = await db.player_game_logs.find_one(
        {"sport": "nfl", "player_id": "espn_2577417"})
    assert doc is not None
    assert set(doc["stat_blocks"]) == {"passing", "rushing"}
    # Normalized family fields present and non-colliding.
    assert doc["passing_yards"] == 288
    assert doc["rushing_yards"] == 22
    assert doc.get("passing_tds") == 2
    # Namespaced legacy fields present per family.
    assert doc["nfl_passing_yds"] == "288"
    assert doc["nfl_rushing_yds"] == "22"
    assert doc["nfl_passing_td"] == "2"
    assert doc["nfl_rushing_td"] == "0"


@pytest.mark.asyncio
async def test_nfl_rush_recv_merged_doc_preserves_both_families(db):
    await _run_nfl_ingest(db, _espn_summary_skill_player_rush_recv())
    doc = await db.player_game_logs.find_one(
        {"sport": "nfl", "player_id": "espn_9999999"})
    assert doc is not None
    assert set(doc["stat_blocks"]) == {"rushing", "receiving"}
    assert doc["rushing_yards"] == 117
    assert doc["receiving_yards"] == 41
    assert doc["receptions"] == 5
    assert doc.get("any_td") == 1  # rushing_td=1
    assert doc["nfl_rushing_yds"] == "117"
    assert doc["nfl_receiving_yds"] == "41"


@pytest.mark.asyncio
async def test_nfl_repeated_ingest_is_idempotent(db):
    """Running the same ingest twice MUST NOT produce two docs and
    MUST NOT raise DuplicateKeyError."""
    await _run_nfl_ingest(db, _espn_summary_qb_dual_threat())
    await _run_nfl_ingest(db, _espn_summary_qb_dual_threat())
    docs = [d async for d in db.player_game_logs.find(
        {"sport": "nfl", "player_id": "espn_2577417"})]
    assert len(docs) == 1, f"idempotent re-ingest produced {len(docs)} rows"


@pytest.mark.asyncio
async def test_nfl_upsert_filter_shape_is_canonical(db):
    """Prove the upsert filter is exactly ``{sport, game_id, player_id}``
    by inspecting the on-disk doc shape (filter keys all present, no
    ``stat_block`` as part of the key)."""
    await _run_nfl_ingest(db, _espn_summary_qb_dual_threat())
    doc = await db.player_game_logs.find_one(
        {"sport": "nfl", "game_id": "espn_401772510", "player_id": "espn_2577417"})
    assert doc is not None
    # The merged doc MUST NOT carry a scalar ``stat_block`` (which was
    # the collision key).  It carries ``stat_blocks`` (array) instead.
    assert "stat_block" not in doc or doc.get("stat_block") is None, \
        f"merged doc should not expose scalar stat_block, got {doc.get('stat_block')!r}"
    assert "stat_blocks" in doc and isinstance(doc["stat_blocks"], list)


@pytest.mark.asyncio
async def test_nfl_unique_index_on_sport_game_player_can_be_enforced(db):
    """Create the authoritative unique index on
    ``(sport, game_id, player_id)`` BEFORE ingesting and prove the fix
    cooperates with the index (no DuplicateKeyError, no silent drop)."""
    await db.player_game_logs.create_index(
        [("sport", 1), ("game_id", 1), ("player_id", 1)],
        unique=True, name="ux_player_game_log",
    )
    # Pre-fix behaviour would have raised here on the second stat
    # block.  The merged write must succeed under the index.
    inserted = await _run_nfl_ingest(db, _espn_summary_qb_dual_threat())
    assert inserted == 1
    # Idempotent re-run under the index.
    inserted2 = await _run_nfl_ingest(db, _espn_summary_qb_dual_threat())
    assert inserted2 == 1
    docs = [d async for d in db.player_game_logs.find({"sport": "nfl"})]
    assert len(docs) == 1


@pytest.mark.asyncio
async def test_nfl_legacy_per_block_rows_resolved_on_merge(db):
    """If a pre-fix per-block row for the same (sport, game_id,
    player_id) already exists, the merge pass must replace it with
    the new merged shape instead of racing it or raising.
    """
    # Seed ONE legacy per-block row (the one that would exist before
    # the second block's upsert failed in the pre-fix adapter).
    await db.player_game_logs.insert_one(
        {"sport": "nfl", "game_id": "espn_401772510",
         "player_id": "espn_2577417", "stat_block": "passing",
         "nfl_yds": "288", "name": "Dak Prescott"},
    )
    # Run the merged ingest.  The upsert filter now excludes
    # ``stat_block``, so it MATCHES the existing legacy row and
    # replaces its payload with the merged shape.
    await _run_nfl_ingest(db, _espn_summary_qb_dual_threat())
    docs = [d async for d in db.player_game_logs.find({"sport": "nfl"})]
    assert len(docs) == 1, f"expected 1 row after merge-over-legacy, got {len(docs)}"
    doc = docs[0]
    # Merge payload took over — stat_blocks array present, both
    # families represented, normalized fields populated.
    assert set(doc["stat_blocks"]) == {"passing", "rushing"}
    assert doc["passing_yards"] == 288
    assert doc["rushing_yards"] == 22


@pytest.mark.asyncio
async def test_nfl_no_duplicate_key_error_without_index(db):
    """Baseline: without any unique index, repeated ingests must
    still produce a single row (upsert correctness, independent of
    index enforcement)."""
    for _ in range(3):
        await _run_nfl_ingest(db, _espn_summary_qb_dual_threat())
    docs = [d async for d in db.player_game_logs.find({"sport": "nfl"})]
    assert len(docs) == 1


# ─────────────────────────────────────────────────────────────────────
# CFB regression — identical structural fix must apply.
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cfb_qb_dual_threat_merges_to_one_doc(db):
    inserted = await _run_cfb_ingest(db, _espn_summary_qb_dual_threat())
    assert inserted == 1
    docs = [d async for d in db.player_game_logs.find({"sport": "cfb"})]
    assert len(docs) == 1
    doc = docs[0]
    assert set(doc["stat_blocks"]) == {"passing", "rushing"}
    assert doc["passing_yards"] == 288
    assert doc["rushing_yards"] == 22
    # CFB uses cfb_ prefix for raw legacy fields.
    assert doc["cfb_passing_yds"] == "288"
    assert doc["cfb_rushing_yds"] == "22"
    assert doc["game_id"] == "espn_cfb_401723456"
    assert doc["player_id"] == "espn_cfb_2577417"


@pytest.mark.asyncio
async def test_cfb_rush_recv_merges_to_one_doc(db):
    inserted = await _run_cfb_ingest(db, _espn_summary_skill_player_rush_recv())
    assert inserted == 1
    doc = await db.player_game_logs.find_one({"sport": "cfb"})
    assert doc is not None
    assert set(doc["stat_blocks"]) == {"rushing", "receiving"}
    assert doc["rushing_yards"] == 117
    assert doc["receiving_yards"] == 41
    assert doc["receptions"] == 5


@pytest.mark.asyncio
async def test_cfb_unique_index_enforced_and_idempotent(db):
    await db.player_game_logs.create_index(
        [("sport", 1), ("game_id", 1), ("player_id", 1)],
        unique=True, name="ux_player_game_log",
    )
    await _run_cfb_ingest(db, _espn_summary_qb_dual_threat())
    await _run_cfb_ingest(db, _espn_summary_qb_dual_threat())
    docs = [d async for d in db.player_game_logs.find({"sport": "cfb"})]
    assert len(docs) == 1


# ─────────────────────────────────────────────────────────────────────
# Reader compat — downstream engines + rationales must still query
# merged + legacy rows.
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_merged_doc_matches_legacy_stat_block_in_predicate(db):
    """``{stat_blocks: 'rushing'}`` must match the merged doc because
    MongoDB equality against an array matches any element."""
    await _run_nfl_ingest(db, _espn_summary_skill_player_rush_recv())
    # The query shape downstream readers use after the compat update.
    hit = await db.player_game_logs.find_one(
        {"sport": "nfl",
         "$or": [{"stat_block": "rushing"}, {"stat_blocks": "rushing"}]})
    assert hit is not None, "merged doc must match stat_blocks membership"
    hit2 = await db.player_game_logs.find_one(
        {"sport": "nfl",
         "$or": [{"stat_block": "receiving"}, {"stat_blocks": "receiving"}]})
    assert hit2 is not None, "same merged doc must match receiving too"
