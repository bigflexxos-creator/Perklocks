"""
Iter108 §15 invariant proof — no GF/GA mirroring in _mls_form_adapter.

Uses asyncio.run() to avoid pytest-asyncio dependency (matches iter103+ style).
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

from services.soccer_game_model import _mls_form_adapter, _MLS_FORM_CACHE


def _reset_cache():
    _MLS_FORM_CACHE.clear()


def _make_db(espn_docs, opp_names, agg_rows):
    db = MagicMock()

    espn_cursor = MagicMock()
    espn_cursor.to_list = AsyncMock(return_value=espn_docs)
    db.espn_mls_stats = MagicMock()
    db.espn_mls_stats.find = MagicMock(return_value=espn_cursor)

    db.player_game_actuals = MagicMock()
    db.player_game_actuals.distinct = AsyncMock(return_value=opp_names)

    agg_cursor = MagicMock()
    agg_cursor.to_list = AsyncMock(return_value=agg_rows)
    db.player_game_actuals.aggregate = MagicMock(return_value=agg_cursor)

    return db


def test_only_gf_present_ga_remains_none():
    """Only ESPN data → gf populated, ga MUST remain None (no mirror)."""
    _reset_cache()
    espn_docs = [
        {"team": "Test Alpha FC", "goals": 10, "games": 20},
        {"team": "Test Alpha FC", "goals": 6, "games": 18},
    ]
    db = _make_db(espn_docs, opp_names=[], agg_rows=[])
    row = asyncio.run(_mls_form_adapter(db, "Test Alpha FC"))
    assert row is not None, "row must be present when GF is available"
    assert row["gf_avg"] is not None, f"gf_avg populated: {row}"
    assert row["ga_avg"] is None, (
        f"§15 VIOLATION: ga_avg mirrored from gf_avg. Row: {row}"
    )


def test_only_ga_present_gf_remains_none():
    """Only PGA opponent data → ga populated, gf MUST remain None (no mirror)."""
    _reset_cache()
    opp_names = ["Test Beta FC"]
    agg_rows = [
        {"_id": {"opp": "Test Beta FC", "date": "2026-01-01"}, "conceded": 2},
        {"_id": {"opp": "Test Beta FC", "date": "2026-01-08"}, "conceded": 1},
        {"_id": {"opp": "Test Beta FC", "date": "2026-01-15"}, "conceded": 3},
    ]
    db = _make_db(espn_docs=[], opp_names=opp_names, agg_rows=agg_rows)
    row = asyncio.run(_mls_form_adapter(db, "Test Beta FC"))
    assert row is not None
    assert row["ga_avg"] is not None, f"ga_avg populated: {row}"
    assert row["gf_avg"] is None, (
        f"§15 VIOLATION: gf_avg mirrored from ga_avg. Row: {row}"
    )


def test_neither_present_returns_none():
    """No signals → adapter returns None (no fabrication)."""
    _reset_cache()
    db = _make_db(espn_docs=[], opp_names=[], agg_rows=[])
    row = asyncio.run(_mls_form_adapter(db, "Test Gamma FC"))
    assert row is None, f"expected None, got {row}"


def test_both_present_both_populated():
    """Both sources → both values populated & independently derived."""
    _reset_cache()
    espn_docs = [{"team": "Test Delta FC", "goals": 8, "games": 20}]
    opp_names = ["Test Delta FC"]
    agg_rows = [
        {"_id": {"opp": "Test Delta FC", "date": "2026-01-01"}, "conceded": 1},
        {"_id": {"opp": "Test Delta FC", "date": "2026-01-08"}, "conceded": 2},
    ]
    db = _make_db(espn_docs, opp_names, agg_rows)
    row = asyncio.run(_mls_form_adapter(db, "Test Delta FC"))
    assert row is not None
    assert abs(row["gf_avg"] - 0.4) < 1e-6, row
    assert abs(row["ga_avg"] - 1.5) < 1e-6, row
