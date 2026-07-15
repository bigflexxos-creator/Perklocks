"""Unit tests for Phase 4 + Phase 5 (Iter 76).

Covers:
  - services.nfl_nflfastr math helpers + market classification
  - analytics.kelly_stake — full math + edge cases
  - steam_detector._american_to_implied_pp + _detect_steam_for_pick

Live network + DB tests are handled by the testing agent's
integration file — this file is pure logic, offline, fast."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest


# ── Kelly staking ───────────────────────────────────────────────────
class TestKellyStake:
    def test_kelly_neutral_no_edge(self):
        from analytics import kelly_stake
        # Book -110 = 52.4% implied. Bettor claims 50% (below implied) →
        # negative edge → no stake.
        r = kelly_stake(0.50, -110, bankroll=100)
        assert r["stake"] == 0.0
        assert r["kelly_f"] <= 0
        assert "Negative" in r["note"]

    def test_kelly_positive_edge(self):
        from analytics import kelly_stake
        # Book -110 implied 52.38%. Bettor claims 60% → +7.6pp edge.
        # Full Kelly f = (0.909*0.60 - 0.40) / 0.909 ≈ 0.16
        # ¼-Kelly on $1000 → ~$40 → capped at 5% ($50).
        r = kelly_stake(0.60, -110, bankroll=1000, fraction=0.25)
        assert r["stake"] > 0
        assert r["kelly_f"] > 0
        assert r["fractional_kelly"] <= 0.05  # max cap
        assert r["edge_pp"] > 0

    def test_kelly_prob_percentage_input(self):
        from analytics import kelly_stake
        # Same result for 60 (percent) and 0.60 (fraction).
        r_pct = kelly_stake(60, -110, bankroll=1000)
        r_frac = kelly_stake(0.60, -110, bankroll=1000)
        assert abs(r_pct["stake"] - r_frac["stake"]) < 0.01

    def test_kelly_fraction_scales(self):
        from analytics import kelly_stake
        # ¼-Kelly should be half of ½-Kelly for same edge (before cap).
        r_quarter = kelly_stake(0.60, +200, bankroll=1000, fraction=0.25, max_stake_pct=1.0)
        r_half = kelly_stake(0.60, +200, bankroll=1000, fraction=0.5, max_stake_pct=1.0)
        assert abs(r_half["stake"] - 2 * r_quarter["stake"]) < 0.5

    def test_kelly_zero_bankroll(self):
        from analytics import kelly_stake
        r = kelly_stake(0.60, -110, bankroll=0)
        assert r["stake"] == 0.0

    def test_kelly_zero_odds(self):
        from analytics import kelly_stake
        r = kelly_stake(0.60, 0, bankroll=100)
        assert r["stake"] == 0.0

    def test_kelly_max_stake_cap(self):
        from analytics import kelly_stake
        # 90% win prob at +200 → huge Kelly → cap kicks in.
        r = kelly_stake(0.90, +200, bankroll=1000, fraction=1.0, max_stake_pct=0.05)
        assert r["fractional_kelly"] <= 0.05
        assert r["stake"] <= 50.01


# ── Steam detector math ────────────────────────────────────────────
class TestSteamMath:
    def test_implied_pp_favorite(self):
        from steam_detector import _american_to_implied_pp
        # -110 = 52.38%
        assert abs(_american_to_implied_pp(-110) - 52.38) < 0.01

    def test_implied_pp_underdog(self):
        from steam_detector import _american_to_implied_pp
        # +200 = 33.33%
        assert abs(_american_to_implied_pp(+200) - 33.33) < 0.01

    def test_implied_pp_zero(self):
        from steam_detector import _american_to_implied_pp
        assert _american_to_implied_pp(0) == 0.0


class TestSteamDetection:
    def test_steam_toward_detected(self):
        """5pp move within 5 minutes → toward steam flagged."""
        from steam_detector import _detect_steam_for_pick, _STEAM_THRESHOLD_PP
        now = datetime.now(timezone.utc)

        class FakeCursor:
            def __init__(self, obs): self.obs = obs
            def sort(self, *a, **kw): return self
            async def to_list(self, length): return self.obs[:length]

        # +100 → -150 = 50% → 60% implied, +10pp move (well above 3pp threshold).
        obs = [
            {"american": +100, "observed_at": now - timedelta(minutes=4)},
            {"american": -110, "observed_at": now - timedelta(minutes=2)},
            {"american": -150, "observed_at": now - timedelta(minutes=1)},
        ]

        class FakeDB:
            class pick_line_history:
                @staticmethod
                def find(*a, **kw):
                    return FakeCursor(obs)
        db = FakeDB()
        steam = asyncio.run(_detect_steam_for_pick(db, "pick_1", -110, now))
        assert steam is not None
        assert steam["direction"] == "toward"
        assert steam["magnitude_pp"] >= _STEAM_THRESHOLD_PP
        assert steam["american_start"] == 100
        assert steam["american_end"] == -150

    def test_no_steam_when_small_move(self):
        """<3pp move → no steam flag."""
        from steam_detector import _detect_steam_for_pick
        now = datetime.now(timezone.utc)

        class FakeCursor:
            def __init__(self, obs): self.obs = obs
            def sort(self, *a, **kw): return self
            async def to_list(self, length): return self.obs[:length]

        # -110 → -115: implied 52.38% → 53.49% = +1.11pp (below threshold)
        obs = [
            {"american": -110, "observed_at": now - timedelta(minutes=4)},
            {"american": -115, "observed_at": now - timedelta(minutes=1)},
        ]

        class FakeDB:
            class pick_line_history:
                @staticmethod
                def find(*a, **kw):
                    return FakeCursor(obs)
        steam = asyncio.run(_detect_steam_for_pick(FakeDB(), "pick_2", -110, now))
        assert steam is None


# ── NFL nflfastR ────────────────────────────────────────────────────
class TestNflNflfastr:
    def test_market_classification_wr(self):
        from services.nfl_nflfastr import _is_nfl_skill_prop
        assert _is_nfl_skill_prop({
            "sport": "NFL", "market": "Receiving Yards Over 65.5",
            "selection": "Ja'Marr Chase",
        })
        assert _is_nfl_skill_prop({
            "sport": "NFL", "market": "Rushing Yards Over 75.5",
            "selection": "Bijan Robinson",
        })
        assert _is_nfl_skill_prop({
            "sport": "NFL", "market": "Passing Yards Over 249.5",
            "selection": "Patrick Mahomes",
        })

    def test_non_nfl_ignored(self):
        from services.nfl_nflfastr import _is_nfl_skill_prop
        assert not _is_nfl_skill_prop({
            "sport": "MLB", "market": "Home Runs Over 0.5",
            "selection": "Aaron Judge",
        })
        assert not _is_nfl_skill_prop({
            "sport": "NFL", "market": "Team Total Over 24.5",
            "selection": "Over",
        })

    def test_pick_player_extraction(self):
        from services.nfl_nflfastr import _pick_player
        assert _pick_player({"selection": "CeeDee Lamb"}) == "CeeDee Lamb"
        assert _pick_player({"selection": "Over"}) is None
        assert _pick_player({"selection": ""}) is None

    def test_safe_div(self):
        from services.nfl_nflfastr import _safe_div
        assert _safe_div(100, 4) == 25.0
        assert _safe_div(100, 0) is None
        assert _safe_div(None, 4) is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
