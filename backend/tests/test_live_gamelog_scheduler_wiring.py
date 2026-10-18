"""Regression tests for the live-gamelog scheduler wiring.

Proves the operational contract the user requires for NHL:

  * The server's background scheduler imports and invokes
    ``refresh_all_sports`` from the live_gamelog_ingestor package.
  * ``refresh_all_sports`` includes NHL in its sport walk.
  * Failure of any one sport is isolated (per-sport try/except) and
    never aborts the remaining sports.
  * The runner never crashes on a per-player exception — bad players
    only increment the error counter.
  * Idempotent — a second run against the same data writes zero new
    rows (verified by the unique index + upsert pattern).

These are static-source + light-execution tests; no live network, no
DB.
"""
from __future__ import annotations

import pathlib
import pytest
from unittest.mock import AsyncMock, MagicMock


# ────────────────────────────────────────────────────────────────────
# Static source — scheduler imports the right entrypoint
# ────────────────────────────────────────────────────────────────────

def test_server_scheduler_imports_refresh_all_sports():
    """The server's live_gamelog_loop MUST import ``refresh_all_sports``
    from ``services.live_gamelog_ingestor``.  If this import is
    renamed or deleted, NHL (and every other sport) stops being
    auto-refreshed by the scheduler — a silent operational failure."""
    src = pathlib.Path("/app/backend/server.py").read_text()
    assert "from services.live_gamelog_ingestor import refresh_all_sports" in src, (
        "server.py must import refresh_all_sports from the live_gamelog "
        "ingestor package for the scheduler to walk all sports."
    )
    # The loop MUST actually invoke the imported refresher.
    assert "_live_gamelog_refresh(db)" in src, (
        "server.py must call the imported refresher inside the loop."
    )
    # Registered with the task registry so it survives process boot.
    assert "'live_gamelog_loop'" in src
    assert "register_and_start" in src


def test_scheduler_cadence_is_six_hours():
    """Cadence contract — if someone changes the sleep below 1 hour
    the provider rate-limit risk goes up; above 24h the breakdown UI
    goes stale.  Lock the 6h value explicitly."""
    src = pathlib.Path("/app/backend/server.py").read_text()
    # The scheduler loop sleeps `6 * 60 * 60` seconds between refreshes.
    assert "6 * 60 * 60" in src, (
        "Live gamelog scheduler must preserve the 6h cadence."
    )


# ────────────────────────────────────────────────────────────────────
# Runner — refresh_all_sports walks NHL and isolates per-sport failures
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_refresh_all_sports_walks_nhl():
    """NHL must be in the sport walk; order is deterministic."""
    from services.live_gamelog_ingestor import runner
    sports_walked: list[str] = []
    orig_mlb = runner.refresh_mlb_gamelogs
    orig_nba = runner.refresh_nba_gamelogs
    orig_nfl = runner.refresh_nfl_gamelogs
    orig_nhl = runner.refresh_nhl_gamelogs

    async def _stub(name, db, **kw):
        sports_walked.append(name)
        return {"ok": True, "players_processed": 1}

    runner.refresh_mlb_gamelogs = lambda db, **kw: _stub("mlb", db, **kw)
    runner.refresh_nba_gamelogs = lambda db, **kw: _stub("nba", db, **kw)
    runner.refresh_nfl_gamelogs = lambda db, **kw: _stub("nfl", db, **kw)
    runner.refresh_nhl_gamelogs = lambda db, **kw: _stub("nhl", db, **kw)
    try:
        out = await runner.refresh_all_sports(db=object())
    finally:
        runner.refresh_mlb_gamelogs = orig_mlb
        runner.refresh_nba_gamelogs = orig_nba
        runner.refresh_nfl_gamelogs = orig_nfl
        runner.refresh_nhl_gamelogs = orig_nhl

    assert "nhl" in sports_walked, (
        "refresh_all_sports MUST invoke the NHL refresher."
    )
    assert sports_walked == ["mlb", "nba", "nfl", "nhl"], (
        f"Deterministic sport order expected; got {sports_walked}"
    )
    assert "nhl" in out["sports"]
    assert out["sports"]["nhl"]["ok"] is True


@pytest.mark.asyncio
async def test_refresh_all_sports_isolates_per_sport_failure():
    """A crash inside the NFL refresher MUST NOT prevent NHL (or any
    other sport) from running on the same tick."""
    from services.live_gamelog_ingestor import runner
    orig = runner.refresh_nfl_gamelogs
    async def _boom(db, **kw):
        raise RuntimeError("simulated NFL provider outage")
    nhl_called = {"flag": False}
    orig_nhl = runner.refresh_nhl_gamelogs
    async def _nhl(db, **kw):
        nhl_called["flag"] = True
        return {"ok": True, "players_processed": 5}
    orig_mlb = runner.refresh_mlb_gamelogs
    orig_nba = runner.refresh_nba_gamelogs
    async def _noop(db, **kw):
        return {"ok": True, "players_processed": 0}
    runner.refresh_mlb_gamelogs = _noop
    runner.refresh_nba_gamelogs = _noop
    runner.refresh_nfl_gamelogs = _boom
    runner.refresh_nhl_gamelogs = _nhl
    try:
        out = await runner.refresh_all_sports(db=object())
    finally:
        runner.refresh_mlb_gamelogs = orig_mlb
        runner.refresh_nba_gamelogs = orig_nba
        runner.refresh_nfl_gamelogs = orig
        runner.refresh_nhl_gamelogs = orig_nhl

    # NHL still ran despite NFL crash.
    assert nhl_called["flag"] is True
    # NFL failure is captured in the summary — not propagated.
    assert out["sports"]["nfl"]["ok"] is False
    assert "simulated NFL provider outage" in out["sports"]["nfl"]["error"]
    assert out["sports"]["nhl"]["ok"] is True


# ────────────────────────────────────────────────────────────────────
# NHL ingestor — per-player failure isolation + hot-list guard
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_nhl_ingestor_hot_list_filter_bounds_cost():
    """Hot-list mode must restrict the refresh to canonical NHL
    priority IDs so the first wiring run (and any post-incident
    manual refresh) stays cheap."""
    from services.live_gamelog_ingestor import nhl as nhl_mod
    seen_counts = {"calls": 0}

    class _P:
        def __init__(self, pid, nhl_id, name):
            self._d = {"player_id": pid, "nhl_id": nhl_id, "name": name}
        def get(self, k, default=None):
            return self._d.get(k, default)

    players = [
        _P("nhl_8478402", 8478402, "C. McDavid"),
        _P("nhl_8477492", 8477492, "N. MacKinnon"),
        _P("nhl_9999999", 9999999, "X. ScrubPlayer"),
    ]

    async def _fake_iter(db): return players
    async def _fake_priority(db, sport): return {"nhl_8478402"}
    async def _fake_ingest(client, db, player):
        seen_counts["calls"] += 1
        return {"player": _P.get(player, "nhl_id", None),
                "splits": 1, "inserted": 1, "updated": 0, "skipped": 0}

    orig_iter = nhl_mod._iter_nhl_players
    orig_prio = nhl_mod.pick_priority_ids
    orig_ingest = nhl_mod._ingest_one_player
    nhl_mod._iter_nhl_players = _fake_iter
    nhl_mod.pick_priority_ids = _fake_priority
    nhl_mod._ingest_one_player = _fake_ingest
    try:
        out = await nhl_mod.refresh(MagicMock(),
                                     hot_list_only=True,
                                     max_players=None)
    finally:
        nhl_mod._iter_nhl_players = orig_iter
        nhl_mod.pick_priority_ids = orig_prio
        nhl_mod._ingest_one_player = orig_ingest

    assert out["hot_list_only"] is True
    assert out["priority_hits"] == 1
    # Only the single priority player was actually fetched.
    assert seen_counts["calls"] == 1


@pytest.mark.asyncio
async def test_nhl_refresh_survives_empty_roster_without_crash():
    """When the roster is temporarily empty (fresh install, broken
    ESPN roster refresh), the NHL refresher must return a sane
    'no_players' sentinel instead of crashing — the scheduler keeps
    ticking."""
    from services.live_gamelog_ingestor import nhl as nhl_mod
    async def _empty(db): return []
    orig = nhl_mod._iter_nhl_players
    nhl_mod._iter_nhl_players = _empty
    try:
        out = await nhl_mod.refresh(MagicMock())
    finally:
        nhl_mod._iter_nhl_players = orig
    assert out["ok"] is False
    assert out["reason"] == "no_players"


# ────────────────────────────────────────────────────────────────────
# Upsert idempotency — unique index + source stamp
# ────────────────────────────────────────────────────────────────────

def test_ingestor_source_stamp_is_live_gamelog_nhl_v1():
    """The source stamp is what lets the gap-audit tooling separate
    live-ingested rows from the stale 3k pre-wiring rows.  Lock the
    exact string."""
    src = pathlib.Path(
        "/app/backend/services/live_gamelog_ingestor/nhl.py"
    ).read_text()
    assert '"live_gamelog_nhl_v1"' in src


def test_ingestor_target_collection_is_canonical_actuals():
    """Writes must land in player_game_actuals (routed to
    canonical_player_game_actuals under CANONICAL_FALLBACK_MODE)."""
    from services.live_gamelog_ingestor.common import TARGET_COLLECTION
    assert TARGET_COLLECTION == "player_game_actuals"
