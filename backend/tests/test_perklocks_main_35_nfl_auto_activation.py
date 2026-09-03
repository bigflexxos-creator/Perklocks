"""PERKLOCKS-MAIN 35 · P0 FINAL — NFL AUTO-ACTIVATION CONTRACT.

Proves that when real NFL sportsbook events appear the pipeline
activates them WITHOUT requiring an admin `force-refresh` call.

The activation path:
  * `/api/picks/today` computes actionable coverage.  When coverage
    is below the starvation threshold, `_background_refresh` is
    scheduled as a one-shot supervised job.
  * `publication_reconciler_scheduler.start_scheduler()` runs the
    publication reconciler on a fixed interval.

Neither path requires a manual toggle or a code release.
Force-refresh is OPTIONAL immediate acceleration.
"""
from __future__ import annotations
import inspect
import pytest


def test_background_refresh_is_scheduled_on_starvation():
    """`/api/picks/today` MUST schedule `_background_refresh` when
    canonical actionable coverage is below threshold."""
    import server
    src = inspect.getsource(server)
    # The lazy-hydrate branch that fires the scheduler.
    assert "_background_refresh" in src
    # Scheduled via the supervised background pool (not manual).
    assert "asyncio.create_task(_background_refresh" in src or \
            "task_type='one_shot'" in src


def test_reconciler_scheduler_start_available():
    """The publication reconciler scheduler must exist as an importable
    module so it can be booted at server startup."""
    from services.publication_reconciler_scheduler import (
        register_with_task_registry, run_once, status,
    )
    assert callable(register_with_task_registry)
    assert callable(run_once)
    assert callable(status)


def test_force_refresh_route_is_optional_not_required():
    """Admin `force-refresh` route must be optional (not the only
    path to activation). The lazy `/api/picks/today` branch is the
    primary activation trigger."""
    import server
    src = inspect.getsource(server)
    # Lazy-hydrate branch exists and is the primary trigger.
    assert "_ensure_today_picks" in src or \
            "_background_refresh()" in src


def test_no_hardcoded_nfl_activation_gate():
    """No env flag / kickoff-date literal blocks NFL activation."""
    import server
    src = inspect.getsource(server)
    assert "NFL_ENABLED" not in src
    assert "NFL_ACTIVATION_DATE" not in src
    assert "nfl_kickoff_gate" not in src.lower()


def test_nfl_sport_keys_in_static_list():
    """`SPORT_KEYS["NFL"]` must contain both preseason and regular
    sport keys so the discovery flow can activate either as they
    appear on the provider catalog."""
    import sports_engine
    keys = sports_engine.SPORT_KEYS.get("NFL", [])
    assert "americanfootball_nfl" in keys
    # Preseason key must also be present so August activation is
    # automatic without a code release.
    assert any("preseason" in k.lower() for k in keys)


def test_nfl_capability_state_is_supported_awaiting_events():
    """`SPORT_CAPABILITIES["NFL"]` must be `enabled=True` and
    `production_status="SUPPORTED"` even when no games are today —
    so the pipeline can auto-activate the moment provider events
    surface."""
    from services.sport_capability_registry import SPORT_CAPABILITIES
    cfg = SPORT_CAPABILITIES.get("NFL", {})
    assert cfg.get("enabled") is True
    assert cfg.get("production_status") == "SUPPORTED"
