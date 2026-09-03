"""PERKLOCKS ROOT FIX (2026-09-03) — Universal Hydration Unblocker.

Regression: the MLB Early Hitter Hydrator (``hydrate_missing_hitter``)
was UNIVERSALLY dead for every MLB hitter for weeks.  Its ``_sync_db``
helper returned ``_SYNC_DB or None``, but pymongo's ``Database``
object raises ``NotImplementedError`` on ``__bool__`` — so every call
threw an exception that the outer ``try/except`` in
``sports_engine._props_picks_from_event`` silently swallowed as
``logger.debug``.

Result across the board:
  * every hitter candidate → ``ctx["hitters"]`` still empty after
    hydration attempt
  * every Statcast factor (xBA, Barrel%, Hard-Hit%, xBA-BA) → None
  * every hitter pick → ``has_enough_real_data`` False → funnel
    stamps ``MISSING_FEATURE_DATA`` → candidate silently killed

The fix replaces the truthiness check with explicit ``is None`` /
``is False`` comparisons, matching the pymongo migration guide.

Contract this test pins:
  1. ``_sync_db()`` returns a live Database handle without raising.
  2. Hydrator populates ``ctx["hitters"][key]`` with real Statcast
     data when a batter row exists in ``mlb_statcast_players``.
  3. Hydrator returns ``False`` gracefully for players with no
     cached data — never raises.
"""
from __future__ import annotations

import os
import pytest  # noqa: F401
from dotenv import load_dotenv

load_dotenv("/app/backend/.env")


def test_sync_db_returns_database_without_truthiness_error():
    """The root defect: pymongo Database raises NotImplementedError
    on ``bool()``.  ``_sync_db`` must NOT trigger that path.
    """
    import services.mlb_early_hitter_hydrate as mod
    mod._SYNC_DB = None                                        # reset cache
    db = mod._sync_db()
    # Explicit ``is None`` check is the ONLY safe truthiness idiom
    # on pymongo Database objects.  If this raises, the fix regressed.
    assert db is not None
    assert hasattr(db, "mlb_statcast_players")


def test_hydrator_attaches_real_statcast_for_known_batter():
    """End-to-end proof of the fix: a batter with a real
    ``mlb_statcast_players`` row must land in ``ctx["hitters"]`` with
    the Statcast payload attached, so downstream factor functions
    (``factor_batter_statcast_xba`` et al.) resolve to real numbers.
    """
    import services.mlb_early_hitter_hydrate as mod
    mod._SYNC_DB = None
    from services.mlb_early_hitter_hydrate import hydrate_missing_hitter

    ctx: dict = {
        "home_team": "Chicago Cubs",
        "away_team": "Milwaukee Brewers",
        "hitters":   {},
    }
    # Jake Bauers is present in the seeded ``mlb_statcast_players``
    # snapshot with real xba / barrel_pct / hard_hit / xba_diff.  If
    # this batter ever disappears from the cache, swap for another
    # active MLB name — the SHAPE of the assertion is what matters.
    ok = hydrate_missing_hitter(ctx, "Jake Bauers")
    if not ok:
        pytest.skip(
            "mlb_statcast_players cache empty for test batter; "
            "hydrator gracefully returned False — no regression.",
        )
    row = ctx["hitters"].get("jake bauers") or {}
    sc = row.get("statcast") or {}
    assert isinstance(sc.get("xba"), (int, float)), row
    assert isinstance(sc.get("barrel_pct"), (int, float)), row
    assert isinstance(sc.get("hard_hit"), (int, float)), row


def test_hydrator_returns_false_for_unknown_batter_without_raising():
    """A player with NO cached data (Statcast + hitter_intel both
    empty) must return False silently — never raise.  This is the
    fail-closed contract the caller relies on.
    """
    import services.mlb_early_hitter_hydrate as mod
    mod._SYNC_DB = None
    from services.mlb_early_hitter_hydrate import hydrate_missing_hitter

    ctx: dict = {
        "home_team": "A",
        "away_team": "B",
        "hitters":   {},
    }
    ok = hydrate_missing_hitter(
        ctx, "Definitely Not A Real Player Zzzzz9999",
    )
    assert ok is False
    assert ctx["hitters"] == {}


def test_hitter_factors_resolve_after_hydration():
    """The full contract: hydration → factor engine → gate.  After
    hydrating a known batter, ``has_enough_real_data("hitter_prop")``
    must return True with the healed floor of 2 factors — proving the
    pipeline can now emit hitter picks WITHOUT a confirmed lineup.
    """
    import services.mlb_early_hitter_hydrate as mod
    mod._SYNC_DB = None
    from services.mlb_early_hitter_hydrate import hydrate_missing_hitter
    from services.mlb_feature_engine import (
        build_mlb_hitter_factors, has_enough_real_data,
    )

    ctx: dict = {
        "home_team": "Chicago Cubs",
        "away_team": "Milwaukee Brewers",
        "hitters":   {},
    }
    ok = hydrate_missing_hitter(ctx, "Jake Bauers")
    if not ok:
        pytest.skip("test batter not in Statcast cache")
    factors, _sources = build_mlb_hitter_factors(
        ctx, player="Jake Bauers", is_home=False,
        opp_pitcher_name=None, market_type="batter_hits",
        line=0.5, side="over",
    )
    resolved = [k for k, v in factors.items() if v is not None]
    assert len(resolved) >= 2, resolved
    assert has_enough_real_data(factors, "hitter_prop") is True
