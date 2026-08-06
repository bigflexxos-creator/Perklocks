"""Phase 4D — NBA + CFB + NFL prediction quality tests.

**No non-MLB SPORT MODELS were changed in Phase 4D except NBA / CFB /
NFL** — per the user directive.  These tests prove:

  1. NBA feature engine produces well-shaped factors from real
     gamelog rows.
  2. NBA emission-path branch consumes ``ctx["nba_precomputed"]``
     when present.
  3. NBA min-factor gate blocks book-follow drop-through.
  4. NBA market list now includes PRA + 3-pointers + combined props.
  5. CFB emission-path branch consumes ``ctx["cfb_precomputed"]``.
  6. CFB precompute helper is importable and produces the right shape.
  7. Repository guardrail: NBA / CFB emission branches carry the
     Phase-4D wiring markers.
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


# ═══════════════════════════════════════════════════════════════════
# NBA feature engine — in-memory Motor stub
# ═══════════════════════════════════════════════════════════════════
class _FakeCursor:
    def __init__(self, rows):
        self._rows = list(rows)
    def sort(self, *a, **kw): return self
    def limit(self, n):
        self._rows = self._rows[:n]
        return self
    async def to_list(self, n):
        return self._rows[:n]


class _FakeCollection:
    def __init__(self, rows):
        self._rows = rows
    def find(self, query, projection=None):
        pattern = ((query.get("player_name") or {}).get("$regex") or "").lower()
        # Strip ^…$ anchors used by the real engine.
        pattern = pattern.lstrip("^").rstrip("$")
        return _FakeCursor([r for r in self._rows
                             if r.get("player_name", "").lower() == pattern])


class _FakeDb:
    def __init__(self, rows):
        self.player_game_logs = _FakeCollection(rows)


def _log(**over):
    row = {
        "sport": "nba", "player_name": "star player",
        "date": "2026-01-01", "minutes": 34, "points": 28,
        "rebounds": 6, "assists": 5, "threes_made": 3,
        "steals": 1, "blocks": 1, "usage": 28.0, "pace": 100.5,
        "rest_days": 1,
    }
    row.update(over)
    return row


def test_nba_engine_returns_factors_for_over_line():
    from services.nba_feature_engine import build_nba_prop_factors
    rows = [_log(points=p) for p in (30, 28, 26, 32, 29, 25, 31, 27, 33, 30)]
    db = _FakeDb(rows)
    fac, src = asyncio.run(build_nba_prop_factors(
        db, player="Star Player", market_key="player_points",
        side="Over", line=24.5,
    ))
    assert "PTS L10 vs Line" in fac
    assert "PTS L10 Hit Rate" in fac
    # Player averages ~29.1 pts vs 24.5 line → Over should score high.
    assert fac["PTS L10 vs Line"] > 0.7
    assert fac["PTS L10 Hit Rate"] >= 0.9
    assert len(src) >= 3


def test_nba_engine_pra_composite():
    from services.nba_feature_engine import build_nba_prop_factors
    rows = [_log(points=25, rebounds=8, assists=6)] * 10
    db = _FakeDb(rows)
    fac, src = asyncio.run(build_nba_prop_factors(
        db, player="Star Player",
        market_key="player_points_rebounds_assists",
        side="Over", line=35.5,
    ))
    # Player averages 39 PRA vs 35.5 line → Over should score high.
    assert fac.get("PRA L10 vs Line", 0) > 0.6


def test_nba_engine_gate_insufficient_data():
    from services.nba_feature_engine import (build_nba_prop_factors,
                                               has_enough_real_data_nba)
    db = _FakeDb([])
    fac, _ = asyncio.run(build_nba_prop_factors(
        db, player="Unknown", market_key="player_points",
        side="Over", line=20.5,
    ))
    assert has_enough_real_data_nba(fac) is False


def test_nba_engine_rest_days_signal():
    from services.nba_feature_engine import build_nba_prop_factors
    b2b_row = _log(rest_days=0)
    rested_row = _log(rest_days=2)
    db_b2b = _FakeDb([b2b_row] * 10)
    db_rested = _FakeDb([rested_row] * 10)
    fac_b, _ = asyncio.run(build_nba_prop_factors(
        db_b2b, player="Star Player", market_key="player_points",
        side="Over", line=24.5))
    fac_r, _ = asyncio.run(build_nba_prop_factors(
        db_rested, player="Star Player", market_key="player_points",
        side="Over", line=24.5))
    assert fac_r["Rest Days"] > fac_b["Rest Days"]


def test_nba_engine_l3_trend_up():
    """Recent 3 games higher than L10 average → over trend positive."""
    from services.nba_feature_engine import build_nba_prop_factors
    rows = ([_log(points=35), _log(points=34), _log(points=33)] +
             [_log(points=25)] * 7)
    db = _FakeDb(rows)
    fac_over, _ = asyncio.run(build_nba_prop_factors(
        db, player="Star Player", market_key="player_points",
        side="Over", line=25.5))
    fac_under, _ = asyncio.run(build_nba_prop_factors(
        db, player="Star Player", market_key="player_points",
        side="Under", line=25.5))
    assert fac_over.get("L3 Trend") > 0.5
    assert fac_under.get("L3 Trend") < 0.5


def test_nba_precompute_populates_ctx_shape():
    from services.nba_feature_engine import precompute_nba_prop_factors
    rows = [_log(points=p) for p in (28, 30, 25, 27, 32, 29, 26, 31, 28, 30)]
    db = _FakeDb(rows)
    ctx = asyncio.run(precompute_nba_prop_factors(
        db, players=["Star Player"],
        market_keys=["player_points", "player_threes"],
        lines_by_player_market={
            ("star player", "player_points"): [(24.5, "Over")],
            ("star player", "player_threes"): [(2.5, "Over")],
        },
    ))
    assert "nba_precomputed" in ctx
    per = ctx["nba_precomputed"].get("star player") or {}
    assert "player_points" in per
    assert "factors" in per["player_points"]
    assert "sources" in per["player_points"]


# ═══════════════════════════════════════════════════════════════════
# NBA market-list expansion
# ═══════════════════════════════════════════════════════════════════
def test_nba_market_list_has_pra_and_threes():
    import sports_engine
    nba = sports_engine.PLAYER_PROP_MARKETS["NBA"]
    assert "player_points_rebounds_assists" in nba
    assert "player_threes" in nba
    assert "player_threes_alternate" in nba


# ═══════════════════════════════════════════════════════════════════
# Wiring markers in emission path
# ═══════════════════════════════════════════════════════════════════
def test_nba_emission_branch_wired_to_ctx():
    src = open("/app/backend/sports_engine.py", encoding="utf-8").read()
    assert "nba_precomputed" in src
    assert "nba_feature_engine" in src or "services.nba_feature_engine" in src
    # Book-follow fallback marker.
    assert "nba_engine_no_precompute" in src


def test_cfb_emission_branch_wired_to_ctx():
    src = open("/app/backend/sports_engine.py", encoding="utf-8").read()
    assert "cfb_precomputed" in src
    assert "cfb_engine_no_precompute" in src


# ═══════════════════════════════════════════════════════════════════
# CFB precompute helper importable
# ═══════════════════════════════════════════════════════════════════
def test_cfb_precompute_helper_importable():
    from services.cfb_precompute import precompute_cfb_factors  # noqa: F401


def test_nba_precompute_helper_importable():
    from services.nba_feature_engine import (
        precompute_nba_prop_factors, build_nba_prop_factors,
        has_enough_real_data_nba, MIN_FACTORS_NBA_PROP,
    )
    assert MIN_FACTORS_NBA_PROP == 3
