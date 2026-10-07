"""test_push_driver_batch_content_hash — byte-for-byte parity between
the resume driver's local hash function and the server's
``services.canonical_cutover.batch_content_hash``.

Why this test exists
────────────────────
R3 Resume #10 failed because the driver repartitioned existing
session batches and produced different ``content_hash`` values than
what Production had stored.  The surgical fix in Resume #11 adds a
preflight hash-match guard in the driver.  The guard is useless if
the driver's local hash function disagrees with the server even by
one byte.

What this test proves
─────────────────────
For a representative sample of each of the 21 reconciled
collections, the driver's ``_server_batch_content_hash`` returns the
EXACT same hex digest as the server's ``batch_content_hash`` on the
SAME accepted-subset docs.

The test uses the real server function (imported from the running
backend package) as the reference oracle.  Any drift here would be
caught before Prod sees a single ALTERED_REPLAY_REJECTED.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys


# Load the server-side hash function directly from the backend package.
sys.path.insert(0, "/app/backend")
from services.canonical_cutover import (  # noqa: E402
    batch_content_hash as server_batch_content_hash,
    is_excluded as server_is_excluded,
    extract_logical_key as server_extract_logical_key,
    logical_key_fields as server_logical_key_fields,
)

# Load the driver module.  It lives outside sys.path so load via spec.
_driver_path = pathlib.Path("/app/reconcile_workspace/scripts/push_canonical_accelerated.py")
_spec = importlib.util.spec_from_file_location("push_canonical_accelerated", _driver_path)
_mod = importlib.util.module_from_spec(_spec)
# Force the module to load without executing its __main__ (importable at module scope).
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]


# ─── Representative sample docs covering all 21 collections ──────────
# Each entry crafts a minimal doc that carries the collection's
# authoritative logical-key fields.  The actual values are irrelevant
# — the only requirement is that both sides see the identical dict.
_SAMPLE_DOCS: dict[str, list[dict]] = {
    "games": [
        {"sport": "nfl", "game_id": "g1", "home_team": "KC", "away_team": "BUF"},
        {"sport": "mlb", "game_id": "g2", "home_team": "NYY", "away_team": "BOS"},
    ],
    "historical_ingestion_state": [{"_id": "nfl_ingest_state", "value": 7, "nested": {"a": 1}}],
    "nfl_ingest_meta":            [{"_id": "nfl_ingest_meta_v1", "last_run_at": "2025-10-01"}],
    "nfl_player_weekly": [
        {"player_id": "p1", "season": 2024, "week": 1, "stats": {"yds": 100}},
        {"player_id": "p2", "season": 2024, "week": 1, "stats": {"yds": 85}},
    ],
    "parlay_history": [{"_id": "pl1", "legs": [{"a": 1}, {"a": 2}]}],
    "picks":          [{"id": "pk1", "sport": "nfl", "status": "pending"}],
    "player_game_actuals": [
        {"sport": "nfl", "event_id": "e1", "player_id": "p1", "pts": 24.5},
    ],
    "player_game_logs": [
        {"sport": "mlb", "game_id": "g2", "player_id": "p3", "ab": 4},
    ],
    "player_identities": [
        {"canonical_player_id": "cp1", "name": "A", "league": "nfl"},
    ],
    "prediction_snapshots": [
        {"prediction_id": "pr1", "snapshot_version": 2, "value": 0.55},
    ],
    "pregame_snapshots": [{"snapshot_hash": "sh1", "generated_at": "2025-01-01T00:00:00Z"}],
    "publication_events": [{"payload_hash": "ph1", "event": "published"}],
    "rollover_slate_events": [
        {"slate_date": "2025-10-01", "event": "generated", "at": "2025-10-01T12:00:00Z"},
    ],
    "rollover_slates": [{"slate_id": "sl1", "sport": "nfl"}],
    "settlement_events": [{"settlement_id": "se1", "status": "settled"}],
    "soccer_matches": [
        {"league": "epl", "season": 2024, "home_team": "ARS",
         "away_team": "CHE", "date": "2025-02-01", "score": "2-1"},
    ],
    "soccer_player_game_logs": [
        {"match_id": "m1", "player_id": "sp1", "minutes": 90},
    ],
    "team_game_actuals": [
        {"sport": "nba", "event_id": "e9", "canonical_team_id": "ct1", "pts": 110},
    ],
    "tennis_matches_history": [
        {"tourney_id": "wim-2024", "winner_id": "atp101", "loser_id": "atp202",
         "rounds": [1, 2, 3]},
    ],
    "user_bets": [{"id": "ub1", "user_id": "u1", "stake": 10.0}],
    "users":     [{"id": "u1", "email": "a@b.c", "role": "admin"}],
}


def test_hash_parity_all_21_collections():
    """Driver's local hash must equal server's hash for every one of the
    21 reconciled collections."""
    diverged = []
    for coll, docs in _SAMPLE_DOCS.items():
        server_hex = server_batch_content_hash(coll, docs)
        driver_hex = _mod._server_batch_content_hash(coll, docs)
        if server_hex != driver_hex:
            diverged.append((coll, server_hex, driver_hex))
    assert not diverged, (
        f"driver↔server content-hash DIVERGED on {len(diverged)} collection(s): "
        + "; ".join(f"{c}: server={s!r} driver={d!r}" for c, s, d in diverged)
    )


def test_hash_order_independence_matches_server():
    """Server sorts by logical-key before hashing — driver must too."""
    coll = "player_identities"
    docs_fwd = [
        {"canonical_player_id": "cp1", "n": 1},
        {"canonical_player_id": "cp2", "n": 2},
        {"canonical_player_id": "cp3", "n": 3},
    ]
    docs_rev = list(reversed(docs_fwd))
    assert server_batch_content_hash(coll, docs_fwd) \
        == server_batch_content_hash(coll, docs_rev)
    assert _mod._server_batch_content_hash(coll, docs_fwd) \
        == _mod._server_batch_content_hash(coll, docs_rev)
    assert _mod._server_batch_content_hash(coll, docs_fwd) \
        == server_batch_content_hash(coll, docs_rev)


def test_exclusion_filter_parity():
    """Driver's _is_excluded must agree with server's is_excluded on
    every doc shape used by the Phase-5 export."""
    cases = [
        ({"excluded_from_canonical_runtime": True}, True),
        ({"status": "UNRESOLVED_IMMUTABLE_CONFLICT"}, True),
        ({"status": "ok"}, False),
        ({}, False),
        ({"excluded_from_canonical_runtime": False}, False),
    ]
    for doc, want in cases:
        server_bool, _reason = server_is_excluded(doc)
        driver_bool = _mod._server_is_excluded(doc)
        assert server_bool == want, f"server disagrees on {doc}"
        assert driver_bool == want, f"driver disagrees on {doc}"
        assert server_bool == driver_bool, f"server/driver disagree on {doc}"


def test_logical_key_parity():
    """Driver's _logical_key_fields must equal server's logical_key_fields
    for every reconciled collection."""
    for coll in _SAMPLE_DOCS.keys():
        s = tuple(server_logical_key_fields(coll))
        d = _mod._logical_key_fields(coll)
        assert s == d, f"{coll}: server={s} driver={d}"


def test_extract_logical_key_parity():
    """``_extract_logical_key`` output must match the server's
    ``extract_logical_key`` on every sample doc."""
    for coll, docs in _SAMPLE_DOCS.items():
        for doc in docs:
            s = tuple(server_extract_logical_key(coll, doc))
            d = tuple(_mod._extract_logical_key(coll, doc))
            assert s == d, f"{coll}: server={s} driver={d}"


if __name__ == "__main__":
    # Direct-invoke runner for pre-commit / local dev convenience.
    test_hash_parity_all_21_collections()
    test_hash_order_independence_matches_server()
    test_exclusion_filter_parity()
    test_logical_key_parity()
    test_extract_logical_key_parity()
    print("OK — driver hash + exclusion + logical-key parity with server across 21 collections")
