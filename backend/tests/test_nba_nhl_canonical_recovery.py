"""Regression — NBA + NHL canonical history runtime recovery (2026-10-20).

Three invariants:

1. BALLDONTLIE is NOT a required Perklocks dependency.
   * ``universal_historical_authority._client_for('nba')`` returns None,
     so no scoring path can require ``BALLDONTLIE_KEY``.
   * ``universal_historical_authority._client_for('nhl')`` returns None,
     so the stale ``historical/nhl.py`` legacy writer is no longer
     invoked by the authority loop.
2. NHL ``_team_recent_stats`` consults the canonical
   ``team_game_actuals`` collection (first) with the legacy
   ``db.games`` collection as fallback — never only legacy.
3. The NHL team-actuals aggregator in
   ``live_gamelog_ingestor/nhl.py`` is defined, importable, and
   referenced from the scheduled ``refresh`` entrypoint.

These guards prevent recurrence of:
  * NBA board burn under missing BALLDONTLIE_KEY, and
  * NHL stale-reader zeroing (support traced Oct '26 incident).
"""
from __future__ import annotations
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_universal_authority_does_not_require_balldontlie_for_nba():
    from services.universal_historical_authority import _client_for
    assert _client_for("nba") is None, (
        "NBA adapter must be None so BALLDONTLIE_KEY is never required. "
        "Canonical NBA history is maintained by live_gamelog_ingestor."
    )


def test_universal_authority_does_not_dispatch_legacy_nhl_adapter():
    from services.universal_historical_authority import _client_for
    assert _client_for("nhl") is None, (
        "Legacy historical/nhl.py adapter must be unwired. Canonical "
        "NHL history is maintained by live_gamelog_ingestor.nhl."
    )


def test_other_sports_still_dispatch():
    from services.universal_historical_authority import _client_for
    for sp in ("mlb", "nfl", "cfb", "soccer", "tennis"):
        assert _client_for(sp) is not None, f"{sp} adapter must still dispatch."


def test_nhl_team_recent_stats_prefers_canonical_collection():
    """Source-level invariant — the function body must query
    ``team_game_actuals`` BEFORE ``db.games`` (canonical-first)."""
    from services import nhl_feature_engine
    src = inspect.getsource(nhl_feature_engine._team_recent_stats)
    tga = src.find("team_game_actuals")
    games = src.find("db.games.find")
    assert tga != -1, "_team_recent_stats must query team_game_actuals."
    assert games != -1, "_team_recent_stats must retain legacy db.games fallback."
    assert tga < games, (
        "team_game_actuals must be queried BEFORE legacy db.games "
        "(canonical-first with legacy fallback)."
    )


def test_nhl_live_gamelog_has_team_actuals_aggregator():
    from services.live_gamelog_ingestor import nhl as nhl_live
    assert hasattr(nhl_live, "_aggregate_team_actuals"), (
        "NHL live gamelog ingestor must expose _aggregate_team_actuals "
        "so team_game_actuals stays fresh automatically."
    )
    # And the scheduled refresh must call it.
    src = inspect.getsource(nhl_live.refresh)
    assert "_aggregate_team_actuals" in src, (
        "refresh() must invoke _aggregate_team_actuals so the 6h "
        "scheduled loop keeps NHL team canonical rows current."
    )


def test_balldontlie_env_var_absent_import_safety():
    """Repo must import cleanly with BALLDONTLIE_KEY completely absent."""
    import os
    original = os.environ.pop("BALLDONTLIE_KEY", None)
    try:
        # Reimport the authority — must not crash.
        from importlib import reload
        from services import universal_historical_authority as mod
        reload(mod)
        assert mod._client_for("nba") is None
    finally:
        if original is not None:
            os.environ["BALLDONTLIE_KEY"] = original


if __name__ == "__main__":
    test_universal_authority_does_not_require_balldontlie_for_nba()
    test_universal_authority_does_not_dispatch_legacy_nhl_adapter()
    test_other_sports_still_dispatch()
    test_nhl_team_recent_stats_prefers_canonical_collection()
    test_nhl_live_gamelog_has_team_actuals_aggregator()
    test_balldontlie_env_var_absent_import_safety()
    print("OK — NBA/NHL canonical-recovery invariants hold.")
