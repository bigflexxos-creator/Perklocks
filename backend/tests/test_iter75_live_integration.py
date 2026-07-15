"""Iteration 75 live integration tests — Phase 1.2 (Stuff+) + Phase 3b.

Each test runs its entire flow inside a single asyncio.run() so Motor's
client is tied to that loop for its full lifetime — otherwise you get
'Event loop is closed' because Motor caches an executor per-loop.

Verifies:
  (a) Live Baseball Savant CSV fetch returns 200 + parseable rows
  (b) refresh_stuff_plus(db) upserts >100 pitchers with the right shape
  (c) enrich_picks_with_stuff_plus_bulk attaches Stuff+ block + dedupes
  (d) get_pitcher_stuff case-insensitive + prior-year fallback
  (e) mlb_deep_signal Stuff+ nudge (Elite → +pts on Over K, weak → -pts)
  (f) Live TennisMyLife challenger + quali CSV fetch
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, "/app/backend")

from dotenv import load_dotenv  # noqa: E402
load_dotenv("/app/backend/.env")

from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402

from services.mlb_stuff_plus import (  # noqa: E402
    _fetch_arsenal_csv,
    enrich_picks_with_stuff_plus_bulk,
    get_pitcher_stuff,
    refresh_stuff_plus,
)
from services.signal_engine.calculators import mlb_deep_signal  # noqa: E402
from services.tennis.sources import tml_stats  # noqa: E402


MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]


def _get_db():
    cli = AsyncIOMotorClient(MONGO_URL)
    return cli, cli[DB_NAME]


# ── Phase 1.2 — Stuff+ live pipeline ───────────────────────────────
class TestStuffPlusLive:
    def test_savant_csv_fetch_live(self):
        rows = asyncio.run(_fetch_arsenal_csv(2025))
        assert isinstance(rows, list) and len(rows) > 100, (
            f"Expected >100 arsenal rows, got {len(rows)}"
        )
        r0 = rows[0]
        for col in (
            "player_id", "last_name, first_name", "pitch_type",
            "pitches", "pitch_usage", "run_value_per_100", "est_woba",
        ):
            assert col in r0, f"Missing column {col} in Savant CSV"

    def test_refresh_and_shape_and_lookup_and_enrich(self):
        """One combined flow — Motor client stays alive for its whole loop."""
        async def _run():
            cli, db = _get_db()
            try:
                # (1) Refresh from Baseball Savant
                result = await refresh_stuff_plus(db)
                assert result.get("upserted", 0) > 100, (
                    f"Expected >100 upserted, got {result}"
                )
                year = result["year"]

                # (2) Doc shape
                doc = await db.mlb_stuff_plus_players.find_one(
                    {"year": year}, {"_id": 0}
                )
                assert doc is not None
                for k in (
                    "stuff_plus", "location_plus", "pitching_plus",
                    "whiff_pct", "k_pct", "arsenal", "name",
                ):
                    assert k in doc, f"Doc missing key {k}"
                assert isinstance(doc["arsenal"], list) and doc["arsenal"]
                assert 60 <= doc["stuff_plus"] <= 150
                assert 60 <= doc["location_plus"] <= 150
                name = doc["name"]

                # (3) Case-insensitive lookup
                for cand in (name.lower(), name.upper(), name.title()):
                    got = await get_pitcher_stuff(db, cand, year)
                    assert got is not None, (
                        f"case-insensitive lookup failed for {cand!r}"
                    )
                    assert got["name"] == name.lower()

                # (4) Year fallback
                stub = {
                    "player_id": "TEST_FALLBACK",
                    "name": "test_fallback pitcher",
                    "year": year - 1,
                    "stuff_plus": 105.0,
                    "location_plus": 100.0,
                    "pitching_plus": 103.0,
                    "arsenal": [],
                }
                await db.mlb_stuff_plus_players.update_one(
                    {"player_id": "TEST_FALLBACK", "year": year - 1},
                    {"$set": stub}, upsert=True,
                )
                try:
                    got = await get_pitcher_stuff(
                        db, "test_fallback pitcher", year
                    )
                    assert got is not None
                    assert got["year"] == year - 1
                finally:
                    await db.mlb_stuff_plus_players.delete_one(
                        {"player_id": "TEST_FALLBACK"}
                    )

                # (5) Bulk enrichment on synthetic picks
                display = name.title()
                real_stuff = doc["stuff_plus"]
                picks = [
                    {"sport": "MLB",
                     "market": "Pitcher Strikeouts Over 6.5",
                     "selection": display},
                    {"sport": "MLB",
                     "market": "Outs Recorded Over 17.5",
                     "selection": display},
                    {"sport": "NBA", "market": "Points",
                     "selection": "LeBron"},
                ]
                touched = await enrich_picks_with_stuff_plus_bulk(db, picks)
                assert touched == 2, f"Expected 2 touched, got {touched}"
                assert picks[0]["stuff_plus"]["stuff_plus"] == real_stuff
                assert picks[1]["stuff_plus"]["stuff_plus"] == real_stuff
                assert "stuff_plus" not in picks[2]
                return True
            finally:
                cli.close()

        assert asyncio.run(_run()) is True


# ── Signal engine nudge ────────────────────────────────────────────
class TestMlbDeepStuffPlusNudge:
    def _make_pick(self, stuff: float, over: bool = True) -> dict:
        direction = "Over" if over else "Under"
        return {
            "sport": "MLB",
            "market": f"Pitcher Strikeouts {direction} 6.5",
            "selection": "Test Pitcher",
            "mlb_deep": {
                "market_family": "pitcher_k",
                "park_hr_factor": 100,
                "park_hits_factor": 100,
                "park_run_factor": 100,
                "park_name": "Neutral Park",
            },
            "stuff_plus": {
                "stuff_plus": stuff,
                "location_plus": 100.0,
                "pitching_plus": (stuff * 0.6 + 100 * 0.4),
            },
        }

    def test_elite_stuff_boosts_over_k(self):
        result = mlb_deep_signal(self._make_pick(120.0, over=True))
        assert result["points"] > 0, (
            f"Elite Stuff+ Over K should be positive: {result}"
        )
        assert result["found"] is True
        assert any("Stuff+" in d for d in result["details"])

    def test_weak_stuff_fades_over_k(self):
        result = mlb_deep_signal(self._make_pick(85.0, over=True))
        assert result["points"] < 0, (
            f"Weak Stuff+ Over K should be negative: {result}"
        )
        assert result["found"] is True

    def test_elite_stuff_fades_under_k(self):
        result = mlb_deep_signal(self._make_pick(120.0, over=False))
        assert result["points"] < 0, (
            f"Elite Stuff+ Under K should be negative: {result}"
        )


# ── Phase 3b — Challenger + Quali live ─────────────────────────────
class TestTennisTmlStatsLive:
    def test_challenger_year_live(self):
        matches = asyncio.run(tml_stats.fetch_challenger_year(2024))
        assert len(matches) > 500, (
            f"Expected >500 challenger matches, got {len(matches)}"
        )
        m = matches[0]
        assert m["circuit"] == "challenger"
        assert m["source"] == "tml_stats"
        assert m.get("winner_name")
        assert m.get("loser_name")

    def test_atp_quali_year_live(self):
        matches = asyncio.run(tml_stats.fetch_atp_quali_year(2024))
        assert len(matches) > 50, (
            f"Expected >50 quali matches, got {len(matches)}"
        )
        assert matches[0]["circuit"] == "atp_quali"
        assert matches[0]["source"] == "tml_stats"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
