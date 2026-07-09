"""Tests for rollover_history_tagger — the settlement-time tagger
that stamps `on_rollover_at` on the true V4 top-3 rollover slate.

Regression tests for the 2026-07-08 bug: History → Rollover was
showing MLB alt-line team totals that were never on the live Rollover
board.  Root cause: no picks were tagged (a lock_score_v2 bug in
board_validator) and the fallback threshold matched everything ≥89.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from rollover_history_tagger import (  # noqa: E402
    _passes_v4,
    _composite_score,
    _top_three_for_slate,
)


def _pick(**over):
    """Build a pick that satisfies V4 by default; override any field."""
    base = {
        "id": "p1",
        "sport": "MLB",
        "event": "A @ B",
        "market": "Team Total Over 4.5",
        "book_odds": -105,      # outside -140/-110 dead zone
        "win_probability": 0.72,
        "edge_percent": 3.5,
        "lock_score": 92,
    }
    base.update(over)
    return base


class TestPassesV4:
    def test_baseline_pick_passes(self):
        assert _passes_v4(_pick()) is True

    def test_lock_below_floor_fails(self):
        assert _passes_v4(_pick(lock_score=85)) is False

    def test_lock_dead_zone_fails(self):
        # 80-84 band is a known inverted-calibration zone per V4 audit.
        assert _passes_v4(_pick(lock_score=83)) is False

    def test_wp_below_floor_fails(self):
        assert _passes_v4(_pick(win_probability=0.55)) is False

    def test_edge_negative_fails(self):
        assert _passes_v4(_pick(edge_percent=-2.0)) is False

    def test_edge_over_12_pct_fails_inverted_signal(self):
        # Per audit >12% edge is an inverted signal hitting only 51%.
        assert _passes_v4(_pick(edge_percent=15)) is False

    def test_odds_dead_zone_fails(self):
        # -140 to -110 is 48% hit, band excluded from Rollover.
        assert _passes_v4(_pick(book_odds=-125)) is False

    def test_super_chalk_fails(self):
        # Below -350 is capped.
        assert _passes_v4(_pick(book_odds=-400)) is False

    def test_soccer_goal_scorer_market_blacklisted(self):
        p = _pick(sport="Soccer", market="Harry Kane Anytime Goal Scorer",
                  win_probability=0.85, edge_percent=5.0, lock_score=95)
        assert _passes_v4(p) is False

    def test_nrfi_blacklisted(self):
        p = _pick(market="NRFI",
                  win_probability=0.70, edge_percent=3.0, lock_score=95)
        assert _passes_v4(p) is False


class TestCompositeScore:
    def test_higher_win_prob_ranks_higher(self):
        a = _composite_score(_pick(win_probability=0.85))
        b = _composite_score(_pick(win_probability=0.65))
        assert a > b

    def test_win_or_draw_gets_market_boost(self):
        """Whitelist bonus: Win-or-Draw historically hits 80.0% → 1.15×"""
        base = _composite_score(_pick(market="Moneyline"))
        wod = _composite_score(_pick(market="Belgium Win or Draw"))
        assert wod > base

    def test_strikeouts_gets_boost(self):
        base = _composite_score(_pick(market="Team Total"))
        ks = _composite_score(_pick(market="Wheeler Over 6.5 Strikeouts"))
        assert ks > base


class TestTopThreeSelection:
    def test_returns_exactly_top_three(self):
        picks = [
            _pick(id=str(i), lock_score=90 + i, event=f"E{i}",
                  win_probability=0.65 + i * 0.02)
            for i in range(6)
        ]
        top = _top_three_for_slate(picks)
        assert len(top) == 3
        # Highest wp/lock should be first
        assert top[0]["id"] == "5"

    def test_one_pick_per_event(self):
        """Two team-total legs on the same MLB game must not both
        make top-3 — the Rollover board is `one per game`."""
        picks = [
            _pick(id="a", event="Yankees @ Red Sox",
                  market="Team Total Over 4.5", lock_score=95),
            _pick(id="b", event="Yankees @ Red Sox",
                  market="Team Total Under 5.5", lock_score=94),
            _pick(id="c", event="Cubs @ Cardinals",
                  market="Cubs +1.5", lock_score=93),
            _pick(id="d", event="Mets @ Phillies",
                  market="Total Runs Over 8.5", lock_score=92),
            _pick(id="e", event="Braves @ Nationals",
                  market="Braves Moneyline", lock_score=91),
        ]
        top = _top_three_for_slate(picks)
        events = {p["event"] for p in top}
        assert len(events) == 3, \
            f"Top-3 must span 3 unique events, got {events}"
        # Yankees game may only contribute ONE pick
        yanks = [p for p in top if "Yankees" in p["event"]]
        assert len(yanks) == 1

    def test_blacklisted_markets_excluded(self):
        """AGS / NRFI / HRR markets never make top-3 even when they'd
        otherwise rank #1."""
        picks = [
            _pick(id="ags", event="G1", market="Harry Kane Anytime Goal Scorer",
                  win_probability=0.90, lock_score=95),
            _pick(id="ml", event="G2", market="Moneyline",
                  win_probability=0.72, lock_score=91),
        ]
        top = _top_three_for_slate(picks)
        assert not any(p["id"] == "ags" for p in top), \
            "Blacklisted AGS market must not enter Rollover top-3"

    def test_empty_slate_returns_empty(self):
        assert _top_three_for_slate([]) == []

    def test_all_failing_v4_returns_empty(self):
        picks = [
            _pick(lock_score=70),          # fails lock floor
            _pick(edge_percent=-5),         # fails edge
            _pick(book_odds=-120),          # dead zone
        ]
        assert _top_three_for_slate(picks) == []
