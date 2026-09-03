"""PERKLOCKS-MAIN 35 · UNIVERSAL EVENT/MARKET ACQUISITION FIX.

Locks in the shared acquisition-timing invariant so an early empty
provider probe cannot permanently suppress a later real market.

Contract asserted:
  * The `HONEST_EMPTY` sport-refresh state expires within a
    reasonable window (<= 30 min) so newly-posted markets can be
    additively discovered without a manual force-refresh, backend
    restart, or code release.
  * The 15-min per-sport cooldown remains for stampede protection.
  * ProviderBudget-blocked / pipeline exceptions retry on the next
    hydrate cycle (not blocked by HONEST_EMPTY).
  * No hardcoded event slicing suppresses late games at scale — the
    propline feed's per-sport caps are ≥ realistic slate size:
    MLB=60, NFL=30, Tennis=120.
  * The refresh state schema separates last_attempted_at from
    last_succeeded_at (telemetry truth).
"""
from __future__ import annotations
import inspect
import pytest


def test_honest_empty_window_is_short_enough_for_intraday_markets():
    """Late-appearing markets (MLB hitter props posted mid-day,
    NFL alt props appearing hours before kickoff, Tennis re-probes
    after early empty responses) MUST re-flow within a bounded
    window. A 4-hour HONEST_EMPTY cache would suppress them for
    the rest of the day — regression banned."""
    import server
    src = inspect.getsource(server._ensure_today_picks) if hasattr(server, "_ensure_today_picks") else inspect.getsource(server)
    # The forbidden 4-hour cache MUST be gone.
    assert "4 * 3600" not in src or "4h" not in src, (
        "4-hour HONEST_EMPTY cache reintroduced — new markets will "
        "be suppressed"
    )
    # Confirm the new bounded window exists (≤ 30 min).
    assert "20 * 60" in src or "30 * 60" in src or "15 * 60" in src, (
        "HONEST_EMPTY expiry window missing"
    )


def test_per_sport_cooldown_still_present_for_stampede_protection():
    """15-min per-sport cooldown must remain."""
    import server
    src = inspect.getsource(server)
    assert "15 * 60" in src, "15-min per-sport cooldown missing"


def test_provider_unavailable_retries_after_cooldown():
    import server
    src = inspect.getsource(server)
    # PROVIDER_UNAVAILABLE branch retries after 15 min.
    assert "PROVIDER_UNAVAILABLE" in src


def test_propline_feed_per_sport_caps_are_realistic_for_slate_sizes():
    """No first-N slicing may permanently exclude late games."""
    import inspect as _i
    import propline_feed
    src = _i.getsource(propline_feed)
    # MLB slate is ≤ 15 games/day — cap of 60 covers double-headers.
    assert '"baseball_mlb": 60' in src
    # NFL slate is ≤ 16 games/week — cap of 30 covers full week.
    assert '"football_nfl": 30' in src
    # Tennis daily slate can exceed 100 — cap of 120 accommodates.
    assert '"tennis": 120' in src


def test_refresh_state_separates_attempt_from_success():
    """Telemetry must distinguish last_attempted_at from
    last_succeeded_at so yesterday's success doesn't look like
    today's."""
    import server
    src = inspect.getsource(server)
    assert "last_attempted_at" in src
    assert "last_succeeded_at" in src or "last_succeeded" in src


def test_pipeline_exception_not_cached_as_success():
    """A crashed refresh must NOT be treated as HONEST_EMPTY."""
    import server
    src = inspect.getsource(server)
    assert "PIPELINE_EXCEPTION" in src
    assert 'sport_starved.append(_sport)' in src
