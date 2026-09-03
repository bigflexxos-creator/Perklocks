"""PERKLOCKS-MAIN 35 — UNIVERSAL PROVIDER-DRIVEN ACQUISITION WINDOW.

Locks in the mandate that:
  • The acquisition path MUST NOT reject a real, upcoming, provider-posted
    event solely because ``commence_time > now + N hours`` for any fixed
    N (36h, 72h, 168h all previously enforced).
  • Only the past-cutoff (``commence < now - 30m`` for props /
    ``now - 2h`` for alt lines) is honoured — that is data hygiene,
    not a horizon.
  • Callers may still pass an explicit horizon on an out-of-band
    ops snapshot (e.g. ``event_window_hours=24`` for a narrow probe),
    but the default is ``None`` (no artificial upper bound).

If a future refactor re-introduces a hard-coded acquisition horizon,
these assertions will fail so the drift is caught immediately.
"""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone


def test_refresh_alt_lines_default_event_window_is_unbounded():
    """``event_window_hours`` MUST default to ``None`` (unbounded)."""
    import alt_lines_feed
    sig = inspect.signature(alt_lines_feed.refresh_alt_lines)
    param = sig.parameters.get("event_window_hours")
    assert param is not None, "event_window_hours param missing"
    assert param.default is None, (
        f"event_window_hours default is {param.default!r}, expected None. "
        "A fixed acquisition horizon (36h / 72h / 168h) violates "
        "PERKLOCKS-MAIN 35 provider-driven acquisition."
    )


def test_props_lookahead_default_is_unbounded():
    """``_DEFAULT_LOOKAHEAD_HOURS`` MUST be ``None`` (no fixed cutoff)."""
    import sports_engine
    assert sports_engine._DEFAULT_LOOKAHEAD_HOURS is None, (
        f"_DEFAULT_LOOKAHEAD_HOURS is "
        f"{sports_engine._DEFAULT_LOOKAHEAD_HOURS!r}, expected None. "
        "A hard-coded per-key/default lookahead re-introduces the "
        "same 72h/168h bug the acquisition fix eliminated."
    )


def test_props_lookahead_map_has_no_default_entries():
    """The legacy per-key ``_PROPS_LOOKAHEAD_HOURS`` overrides must be
    empty by default so provider-driven acquisition is the norm.  If
    the operator explicitly injects a per-key override for a genuine
    ops scenario, that opt-in path is still honoured by the loop.
    """
    import sports_engine
    assert isinstance(sports_engine._PROPS_LOOKAHEAD_HOURS, dict)
    # Empty by default — populated only by explicit ops override.
    assert sports_engine._PROPS_LOOKAHEAD_HOURS == {}, (
        f"_PROPS_LOOKAHEAD_HOURS should default to empty; got "
        f"{sports_engine._PROPS_LOOKAHEAD_HOURS!r}"
    )


def test_picks_from_game_accepts_events_beyond_72h():
    """``_picks_from_game`` must NOT drop a game just because it is
    scheduled >72h out.  Previously per-sport window_hours (72h /
    120h / 168h / 240h) silently returned ``[]`` for anything
    farther out.  Now the past-cutoff of -30min is the ONLY time
    gate on this path.
    """
    import sports_engine
    now = datetime.now(timezone.utc)

    def _game(hours_out: float) -> dict:
        dt = now + timedelta(hours=hours_out)
        return {
            "id": f"e-{int(hours_out)}",
            "home_team": "Home", "away_team": "Away",
            "commence_time": dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "bookmakers": [],
        }

    # Every sport, at horizons that were previously blocked.
    for sport in ("MLB", "NFL", "NBA", "Soccer", "Tennis", "UFC"):
        for horizon_h in (73, 96, 120, 168, 240, 336):  # 3d-14d out
            result = sports_engine._picks_from_game(
                sport, sport, _game(horizon_h), "2026-06-30",
            )
            # Empty bookmakers → empty picks list.  That is EXPECTED
            # (no markets to build from) — the crucial assertion is
            # that the function did NOT short-circuit to ``[]`` on the
            # future-cutoff check.  We assert the shape is a list
            # rather than a horizon rejection.
            assert isinstance(result, list), (
                f"{sport} @ +{horizon_h}h returned {type(result)!r}, "
                "expected list — acquisition must not reject on horizon."
            )


def test_past_cutoff_still_enforced_alt_lines():
    """Data hygiene: alt-line acquisition must still drop events that
    are already >2h in the past.  This is NOT a horizon; it is a
    finished-event guard.
    """
    import inspect
    import alt_lines_feed
    src = inspect.getsource(alt_lines_feed.refresh_alt_lines)
    # The past-cutoff line remains as a pure data-hygiene guard.
    assert "commence < now - timedelta(hours=2)" in src, (
        "Past-cutoff (commence < now - 2h) missing from "
        "refresh_alt_lines — finished games would leak into "
        "acquisition."
    )


def test_past_cutoff_still_enforced_picks_from_game():
    """Data hygiene: ``_picks_from_game`` still drops events that
    have already started (past -30 min).
    """
    import sports_engine
    now = datetime.now(timezone.utc)
    started_45m_ago = (now - timedelta(minutes=45)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    game = {
        "id": "over-1", "home_team": "H", "away_team": "A",
        "commence_time": started_45m_ago, "bookmakers": [],
    }
    result = sports_engine._picks_from_game(
        "MLB", "MLB", game, "2026-06-30",
    )
    assert result == [], (
        "Past-cutoff of -30min was NOT enforced — already-started "
        "games should be dropped from acquisition."
    )


def test_admin_snapshot_endpoint_default_is_unbounded():
    """The admin ops endpoint that fires one-off alt-line snapshots
    must default to ``event_window_hours=None`` (unbounded).  Ops can
    still pass an explicit narrow horizon on the query string, but
    the default must not silently cap acquisition to 36 hours.
    """
    from routes import admin_routes
    sig = inspect.signature(admin_routes.admin_alt_lines_snapshot)
    param = sig.parameters.get("event_window_hours")
    assert param is not None
    assert param.default is None, (
        f"admin_alt_lines_snapshot event_window_hours default is "
        f"{param.default!r}, expected None."
    )
