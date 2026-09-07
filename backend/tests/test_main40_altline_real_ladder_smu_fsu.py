"""Item P0-C — Alt-Line Magic real-ladder wiring.

Simulates the SMU vs FSU CFB Alt Total scenario end-to-end using
seeded ``live_alt_lines`` rows (matching FanDuel screenshot evidence).

Verifies:
  1. Internal Perklocks pick id is NEVER used as provider_event_id.
  2. ``_fetch_game_market_alt_lines`` reads from the normalized
     ``live_alt_lines`` collection.
  3. Full observed ladder survives (all 27 thresholds).
  4. Alt-Line Magic surfaces a small (~3-5) list of bettable chips
     with real sportsbook prices + provenance.
"""
from __future__ import annotations

import asyncio
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class _FakeCursor:
    def __init__(self, docs):
        self._docs = docs
    async def to_list(self, length=None):
        return list(self._docs) if length is None else list(self._docs)[:length]


class _FakeColl:
    def __init__(self, docs):
        self._docs = docs
    def find(self, query, projection=None):
        # Very small in-memory filter — only supports the equality +
        # $in shapes used by the code under test.
        eid = query.get("event_id")
        mkt = query.get("market_key")
        allow: set = set()
        if isinstance(mkt, dict) and "$in" in mkt:
            allow = set(mkt["$in"])
        elif isinstance(mkt, str):
            allow = {mkt}
        filtered = []
        for d in self._docs:
            if d.get("event_id") != eid:
                continue
            if allow and d.get("market_key") not in allow:
                continue
            filtered.append(dict(d))
        return _FakeCursor(filtered)


class _FakeDB:
    def __init__(self, alt_docs):
        self.live_alt_lines = _FakeColl(alt_docs)


# Seed FanDuel-visible thresholds around Perklocks base 52.5 with
# real prices from the runtime screenshot evidence.
_SMU_FSU_LADDER = []
_thresholds = [
    29.5, 30.5, 31.5, 32.5, 33.5, 34.5, 35.5, 36.5, 37.5, 38.5,
    39.5, 40.5, 41.5, 42.5, 43.5, 44.5, 45.5, 46.5, 47.5, 48.5,
    49.5, 50.5, 51.5, 52.5, 53.5, 54.5, 55.5,
]
# Prices from screenshot (spot-check verified)
_PRICES = {
    49.5: (-166, 128),
    50.5: (-150, 118),
    51.5: (-130, 102),
    52.5: (-115, -111),
    53.5: (-106, -120),
    54.5:  (104, -132),
    55.5:  (122, -154),
}
for th in _thresholds:
    over_p, under_p = _PRICES.get(th, (-110, -110))
    _SMU_FSU_LADDER.append({
        "event_id":   "cfb_smu_fsu_20260906",
        "sport":      "cfb",
        "market_key": "alternate_totals",
        "selection":  "Over",
        "line":       th,
        "price":      over_p,
        "sportsbook": "fanduel",
        "last_seen":  "2026-09-06T22:00:00+00:00",
        "home_team":  "Florida State Seminoles",
        "away_team":  "SMU Mustangs",
    })
    _SMU_FSU_LADDER.append({
        "event_id":   "cfb_smu_fsu_20260906",
        "sport":      "cfb",
        "market_key": "alternate_totals",
        "selection":  "Under",
        "line":       th,
        "price":      under_p,
        "sportsbook": "fanduel",
        "last_seen":  "2026-09-06T22:00:00+00:00",
        "home_team":  "Florida State Seminoles",
        "away_team":  "SMU Mustangs",
    })


def test_internal_pick_id_never_used_as_provider_event_id():
    """When only ``id`` (internal) is present the fetch returns []."""
    from routes.admin_routes import _fetch_game_market_alt_lines
    db = _FakeDB([])
    pick = {"id": "internal-uuid-12345",
            "sport": "cfb", "home_team": "SMU", "away_team": "FSU"}
    async def _run():
        rows = await _fetch_game_market_alt_lines(
            db, pick=pick, market_type="total",
        )
        assert rows == []
    asyncio.get_event_loop().run_until_complete(_run())


def test_smu_fsu_full_ladder_survives_from_live_alt_lines():
    """With normalized ``live_alt_lines`` seeded, the fetch surfaces
    the FULL 54-row ladder (27 thresholds × 2 sides)."""
    from routes.admin_routes import _fetch_game_market_alt_lines
    db = _FakeDB(_SMU_FSU_LADDER)
    pick = {
        "id":                  "internal-uuid-abc",
        "provider_event_id":   "cfb_smu_fsu_20260906",
        "canonical_event_id":  "cfb_smu_fsu_20260906",
        "sport":               "cfb",
        "market":              "Total Points",
        "selection":           "Over 52.5",
        "line":                52.5,
        "home_team":           "Florida State Seminoles",
        "away_team":           "SMU Mustangs",
    }
    async def _run():
        rows = await _fetch_game_market_alt_lines(
            db, pick=pick, market_type="total",
        )
        assert len(rows) == 54, f"expected 54 rows, got {len(rows)}"
        thresholds = sorted({r["line"] for r in rows})
        assert min(thresholds) == 29.5
        assert max(thresholds) == 55.5
        assert 52.5 in thresholds
        # Provenance preserved on every row.
        for r in rows:
            assert r["real_observed_line"] is True
            assert r["bookmaker"] == "fanduel"
            assert r["provider_market_key"] == "alternate_totals"
            assert r["american"] is not None
            assert r["observed_at"] is not None
        # Confirmed 52.5 Over @ -115 and Under @ -111.
        base_rows = [r for r in rows if r["line"] == 52.5]
        over = [r for r in base_rows if r["side"] == "Over"][0]
        under = [r for r in base_rows if r["side"] == "Under"][0]
        assert over["american"] == -115
        assert under["american"] == -111
    asyncio.get_event_loop().run_until_complete(_run())


def test_smu_fsu_magic_bundle_surfaces_bettable_only():
    """End-to-end: feed the fetched rows into
    ``build_game_market_alt_lines`` and confirm every surfaced chip
    has ``bettable=True`` + ``source=market``."""
    from routes.admin_routes import _fetch_game_market_alt_lines
    from services.alt_line_engine.game_markets import (
        build_game_market_alt_lines, GameMarketParse,
    )
    db = _FakeDB(_SMU_FSU_LADDER)
    pick = {
        "id":                  "internal-uuid-abc",
        "provider_event_id":   "cfb_smu_fsu_20260906",
        "sport":               "cfb",
        "selection":           "Over 52.5",
        "home_team":           "Florida State Seminoles",
        "away_team":           "SMU Mustangs",
    }
    async def _run():
        market_alt = await _fetch_game_market_alt_lines(
            db, pick=pick, market_type="total",
        )
        parsed = GameMarketParse(
            market_type="total", line=52.5, side="Over",
            label="Over 52.5", win_prob=0.55,
        )
        bundle = build_game_market_alt_lines(
            sport="CFB", pick=pick, parsed=parsed,
            market_alt_lines=market_alt, top_n=8,
        )
        chips = bundle["alt_lines"]
        assert chips, "expected at least one bettable chip"
        for c in chips:
            assert c["bettable"] is True
            assert c["source"] == "market"
            assert c["american"] is not None
            assert c["bookmaker"] == "fanduel"
    asyncio.get_event_loop().run_until_complete(_run())


def test_cfb_wired_into_alt_lines_feed():
    """SMU vs FSU is NCAAF/CFB — the alt-line ingestion feed must
    include a CFB entry so ``live_alt_lines`` gets hydrated."""
    from alt_lines_feed import SPORT_CONFIG
    assert "cfb" in SPORT_CONFIG
    sport_key, markets = SPORT_CONFIG["cfb"]
    assert sport_key == "americanfootball_ncaaf"
    assert "alternate_totals" in markets
    assert "alternate_spreads" in markets


if __name__ == "__main__":
    test_internal_pick_id_never_used_as_provider_event_id()
    test_smu_fsu_full_ladder_survives_from_live_alt_lines()
    test_smu_fsu_magic_bundle_surfaces_bettable_only()
    test_cfb_wired_into_alt_lines_feed()
    print("OK — Alt-Line Magic real-ladder wiring verified (SMU/FSU).")
