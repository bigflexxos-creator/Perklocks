"""Live integration tests for Iter 76 — Phase 4 (NFL nflverse) + Phase 5
(Kelly, CLV, Steam).

Coverage matrix:
  (a) test_iter76_phase4_5.py unit tests    — run separately, 16/16 green
  (b) live nflverse parquet fetch + upsert  — TestNflverseLive
  (c) synthetic NFL pick enrichment          — TestNflEnrichment
  (d) Kelly endpoint math correctness (HTTP)  — TestKellyEndpoint
  (e) Steam endpoint empty-response shape (HTTP) — TestSteamEndpoint
  (f) signal engine NFL nudge on target_share=0.28 — TestSignalNudge

Design note (from iter75 postmortem): every Motor-touching test wraps its
full flow inside a single asyncio.run() call — new event loop + fresh
Motor client per test to avoid the "Event loop is closed" bug that
occurs when Motor's per-loop executors outlive their loop.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

import pytest
import requests

# Backend on the path so we can import services & analytics directly.
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(BACKEND_DIR / ".env")
# Frontend .env holds the public preview URL (EXPO_PACKAGER_HOSTNAME is the
# public preview host; EXPO_BACKEND_URL may not be defined explicitly).
load_dotenv(BACKEND_DIR.parent / "frontend" / ".env")

BASE_URL = (
    os.environ.get("EXPO_BACKEND_URL")
    or os.environ.get("EXPO_PACKAGER_HOSTNAME")
    or ""
).rstrip("/")
MONGO_URL = os.environ.get("MONGO_URL")
DB_NAME = os.environ.get("DB_NAME") or "lockscore_db"

TEST_EMAIL = "demo@lockscore.ai"
TEST_PASSWORD = "demo123"


# ── Auth helper ─────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def auth_headers() -> dict:
    if not BASE_URL:
        pytest.skip("EXPO_BACKEND_URL not set")
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": TEST_EMAIL, "password": TEST_PASSWORD},
        timeout=15,
    )
    if r.status_code != 200:
        pytest.skip(f"Login failed ({r.status_code}): {r.text[:200]}")
    tok = r.json().get("access_token")
    assert tok, "No access token"
    return {"Authorization": f"Bearer {tok}"}


# ── (d) Kelly endpoint ──────────────────────────────────────────────
class TestKellyEndpoint:
    def test_kelly_positive_edge(self, auth_headers):
        r = requests.get(
            f"{BASE_URL}/api/analytics/kelly",
            params={
                "win_probability": 60, "american_odds": -110,
                "bankroll": 1000, "fraction": 0.25,
            },
            headers=auth_headers, timeout=10,
        )
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["stake"] > 0, f"expected positive stake, got {d}"
        assert d["edge_pp"] > 0, f"expected positive edge, got {d}"
        # cap at 5%
        assert d["stake_pct"] <= 5.001, f"cap breached: {d['stake_pct']}"
        assert d["kelly_f"] > 0

    def test_kelly_no_edge(self, auth_headers):
        r = requests.get(
            f"{BASE_URL}/api/analytics/kelly",
            params={
                "win_probability": 50, "american_odds": -110,
                "bankroll": 100,
            },
            headers=auth_headers, timeout=10,
        )
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["stake"] == 0.0
        assert "Negative" in d["note"]

    def test_kelly_probability_as_fraction(self, auth_headers):
        """Accepts both 0..1 and 0..100."""
        r_pct = requests.get(
            f"{BASE_URL}/api/analytics/kelly",
            params={"win_probability": 60, "american_odds": -110, "bankroll": 1000},
            headers=auth_headers, timeout=10,
        ).json()
        r_frac = requests.get(
            f"{BASE_URL}/api/analytics/kelly",
            params={"win_probability": 0.60, "american_odds": -110, "bankroll": 1000},
            headers=auth_headers, timeout=10,
        ).json()
        assert abs(r_pct["stake"] - r_frac["stake"]) < 0.01

    def test_kelly_for_pick_not_found(self, auth_headers):
        r = requests.get(
            f"{BASE_URL}/api/analytics/kelly/for-pick",
            params={"pick_id": f"nonexistent-{uuid.uuid4().hex[:8]}", "bankroll": 100},
            headers=auth_headers, timeout=10,
        )
        assert r.status_code == 200
        assert r.json().get("error") == "pick_not_found"


# ── (e) Steam endpoint ──────────────────────────────────────────────
class TestSteamEndpoint:
    def test_steam_empty_shape(self, auth_headers):
        r = requests.get(
            f"{BASE_URL}/api/analytics/steam",
            params={"hours": 6, "direction": "toward", "limit": 50},
            headers=auth_headers, timeout=10,
        )
        assert r.status_code == 200, r.text
        d = r.json()
        assert "count" in d
        assert "picks" in d
        assert isinstance(d["picks"], list)
        assert d["count"] == len(d["picks"])
        assert d["hours"] == 6
        assert d["direction_filter"] == "toward"

    def test_steam_default_direction_any(self, auth_headers):
        r = requests.get(
            f"{BASE_URL}/api/analytics/steam",
            headers=auth_headers, timeout=10,
        )
        assert r.status_code == 200
        assert r.json()["direction_filter"] == "any"


# ── (f) Signal engine NFL nudge ─────────────────────────────────────
class TestSignalNudge:
    def test_volume_signal_elite_target_share(self):
        from services.signal_engine.calculators import volume_signal
        pick = {
            "sport": "NFL",
            "market": "Receiving Yards Over 65.5",
            "selection": "Ja'Marr Chase",
            "nfl_usage": {
                "target_share": 0.28, "wopr": 0.72, "adot": 14.1,
                "snap_pct": 0.85,
            },
        }
        r = volume_signal(pick)
        # Elite target share (+1.8) + WOPR≥0.7 (+1.2) + aDOT≥13 (+0.8) = +3.8
        # Clamped to VOLUME_MAX. Just assert positive & found.
        assert r["found"] is True
        assert r["points"] > 0, f"expected positive nudge, got {r}"
        joined = " ".join(r["details"]).lower()
        assert "target share" in joined

    def test_volume_signal_low_target_share(self):
        from services.signal_engine.calculators import volume_signal
        pick = {
            "sport": "NFL",
            "market": "Receiving Yards Over 45.5",
            "selection": "Rotational WR",
            "nfl_usage": {"target_share": 0.10},
        }
        r = volume_signal(pick)
        assert r["found"] is True
        assert r["points"] < 0, f"expected negative nudge, got {r}"

    def test_volume_signal_bellcow_rushing(self):
        from services.signal_engine.calculators import volume_signal
        pick = {
            "sport": "NFL",
            "market": "Rushing Yards Over 75.5",
            "selection": "Bijan Robinson",
            "nfl_usage": {"snap_pct": 0.80},
        }
        r = volume_signal(pick)
        assert r["found"] is True
        assert r["points"] > 0

    def test_volume_signal_non_nfl_no_op(self):
        from services.signal_engine.calculators import volume_signal
        pick = {
            "sport": "MLB",
            "market": "Home Runs Over 0.5",
            "nfl_usage": {"target_share": 0.28},  # should be ignored
        }
        r = volume_signal(pick)
        # No nfl_usage nudges applied for non-NFL. May still find other volume
        # signals — we only assert nfl-specific details are absent.
        joined = " ".join(r["details"]).lower()
        assert "target share" not in joined


# ── (c) Synthetic pick enrichment (in-memory / mock db) ────────────
class TestNflEnrichmentSynthetic:
    def test_enrichment_attaches_block_receiver_pick(self):
        """Verify enrich_picks_with_nfl_usage_bulk attaches nfl_usage
        block when a matching cached doc exists. Uses a fake Motor-like
        db to avoid real DB coupling here."""
        from services.nfl_nflfastr import enrich_picks_with_nfl_usage_bulk

        cached_doc = {
            "player": "ja'marr chase", "season": 2024,
            "position": "WR", "team": "CIN", "snap_pct_avg": 0.88,
            "games_recorded": 17, "games": 17,
            "receiving": {
                "target_share": 0.29, "air_yards_share": 0.35,
                "wopr": 0.72, "adot": 12.4, "yprr_est": 2.35,
                "receiving_yards": 1708.0, "receiving_epa": 55.1,
            },
            "rushing": {"rushing_yards": 0, "carries": 0, "rushing_epa": 0},
        }

        class _Coll:
            async def find_one(self, q, projection=None, sort=None):
                # Match on normalized player+season or any player fallback.
                if q.get("player") == "ja'marr chase":
                    return cached_doc
                return None

        class _DB:
            nfl_player_usage = _Coll()

        picks = [
            {"sport": "NFL", "market": "Receiving Yards Over 82.5",
             "selection": "Ja'Marr Chase"},
            # Non-NFL pick must be skipped
            {"sport": "MLB", "market": "Home Runs Over 0.5",
             "selection": "Aaron Judge"},
            # Team-total NFL pick must be skipped
            {"sport": "NFL", "market": "Team Total Over 24.5",
             "selection": "Over"},
        ]

        async def _run():
            return await enrich_picks_with_nfl_usage_bulk(_DB(), picks)

        touched = asyncio.run(_run())
        assert touched == 1, f"expected 1 pick enriched, got {touched}"
        assert "nfl_usage" in picks[0]
        assert picks[0]["nfl_usage"]["target_share"] == 0.29
        assert picks[0]["nfl_usage"]["snap_pct"] == 0.88
        assert "nfl_usage" not in picks[1]
        assert "nfl_usage" not in picks[2]


# ── (b) Live nflverse parquet fetch + Mongo upsert ─────────────────
# Marked slow: downloads ~10MB of parquet from nflverse GitHub Releases.
@pytest.mark.slow
class TestNflverseLive:
    def test_refresh_and_lookup_2024(self):
        """Downloads snap_counts_2024.parquet + player_stats_season.parquet
        via nflverse GitHub Releases, upserts to nfl_player_usage, and
        verifies a well-known player (Ja'Marr Chase) is queryable
        case-insensitively."""
        if not MONGO_URL:
            pytest.skip("MONGO_URL not set")

        async def _run():
            from motor.motor_asyncio import AsyncIOMotorClient
            from services.nfl_nflfastr import (
                refresh_nfl_seasons, get_nfl_player_usage,
            )
            cli = AsyncIOMotorClient(MONGO_URL)
            db = cli[DB_NAME]
            try:
                result = await refresh_nfl_seasons(db, seasons=(2024,))
                assert isinstance(result, dict)
                # Should get many docs
                assert result.get("snap_count_docs", 0) > 500, \
                    f"expected >500 snap docs, got {result}"
                assert result.get("stat_docs", 0) > 500, \
                    f"expected >500 stat docs, got {result}"

                # Case-insensitive lookup
                doc = await get_nfl_player_usage(db, "Ja'Marr Chase", 2024)
                assert doc is not None, "Ja'Marr Chase not found"
                assert doc["season"] == 2024
                # Structure sanity checks
                assert "receiving" in doc
                assert doc["receiving"].get("targets", 0) > 0
                return result
            finally:
                cli.close()

        result = asyncio.run(_run())
        print(f"[nflverse refresh 2024] {result}")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
