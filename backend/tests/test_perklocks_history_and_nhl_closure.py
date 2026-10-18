"""Perklocks — current-season history + NHL board surface closure.

Covers two P0 regressions discovered during the 2026-10-03 surgical fix:

  (A) NFL current-season history was stale (UI showed 2025 L10 for a
      current-2026 pick) because `player_game_actuals` stores NFL rows
      under two different canonical_player_id schemes (gsis + ESPN)
      and the HI adapter only queried one.  Fix: dual-pipeline
      identity bridge inside `NFLPlayerHistoricalAdapter`.

  (B) NHL was missing from `/api/picks/today` when the frontend sent
      the new `sports=NHL` CSV multi-select param, because the
      eligibility-union RESCUE block only honoured the legacy singular
      `sport=` param.  The rescue re-injected the full published
      Soccer slate (46k rows) which swamped the NHL rows.  Fix:
      multi-select parity in the rescue block.

Both tests are DB-independent — they inject a lightweight fake db that
captures the queries the production code builds, and asserts on the
query shape.  No Prod writes, no live network.
"""
from __future__ import annotations

import re
import pytest
from unittest.mock import patch


# =====================================================================
# (A) NFL dual-pipeline identity bridge
# =====================================================================

class _FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, *_a, **_kw):  # chainable
        return self

    def limit(self, _n):
        return self

    def __aiter__(self):
        self._i = 0
        return self

    async def __anext__(self):
        if self._i >= len(self._docs):
            raise StopAsyncIteration
        d = self._docs[self._i]
        self._i += 1
        return d


class _FakePlayerIdentityCursor(_FakeCursor):
    pass


class _FakeActuals:
    """Captures the final `.find()` filter used by the adapter."""

    def __init__(self, docs, distinct_values):
        self._docs = docs
        self._distinct_values = distinct_values
        self.last_filter: dict | None = None

    def find(self, filter_, *_a, **_kw):
        self.last_filter = filter_
        # Return any matching docs by canonical_player_id ∈ filter set.
        if (isinstance(filter_.get("canonical_player_id"), dict)
                and "$in" in filter_["canonical_player_id"]):
            ids = set(filter_["canonical_player_id"]["$in"])
            matched = [d for d in self._docs
                       if d.get("canonical_player_id") in ids]
        else:
            cid = filter_.get("canonical_player_id")
            matched = [d for d in self._docs
                       if d.get("canonical_player_id") == cid]
        return _FakeCursor(matched)

    async def distinct(self, _field, _filter):
        return list(self._distinct_values)


class _FakeIdentities:
    def __init__(self, docs):
        self._docs = docs

    def find(self, _filter, *_a, **_kw):
        return _FakeCursor(self._docs)

    async def find_one(self, _filter, *_a, **_kw):
        return self._docs[0] if self._docs else None


class _FakeTeamActuals:
    def find(self, *_a, **_kw):
        return _FakeCursor([])


class _FakeDB:
    def __init__(self, actuals_docs, identity_docs, distinct_ids):
        self.player_game_actuals = _FakeActuals(actuals_docs, distinct_ids)
        self.player_identities = _FakeIdentities(identity_docs)
        self.team_game_actuals = _FakeTeamActuals()


def _mk_actual_row(pid, event_time, season, actuals, player_name):
    return {
        "sport": "nfl",
        "canonical_player_id": pid,
        "player_name": player_name,
        "event_time": event_time,
        "season": season,
        "week": 1,
        "opponent": "ARI",
        "actuals": actuals,
        "source": ("nfl_player_weekly" if str(pid).startswith("00-")
                   else "live_gamelog_nfl_v1"),
    }


@pytest.mark.asyncio
async def test_nfl_hi_bridges_dual_pipeline_player_ids():
    """NFL HI adapter must UNION rows across both canonical ID schemes
    (gsis + ESPN) so a current-2026 pick stamped with gsis still sees
    the 2026 ESPN rows."""
    from services.historical_intelligence import (
        NFLPlayerHistoricalAdapter, HistoricalQuery,
    )

    gsis_rows = [
        _mk_actual_row("00-0036244", "2026-01-25T18:30:00", 2025,
                       {"receptions": 3}, "Colby Parkinson"),
        _mk_actual_row("00-0036244", "2026-01-18T18:30:00", 2025,
                       {"receptions": 3}, "Colby Parkinson"),
    ]
    espn_rows = [
        _mk_actual_row("4242557", "2026-09-28T00:20:00.000+00:00", None,
                       {"receptions": 0}, "Colby Parkinson"),
        _mk_actual_row("4242557", "2026-09-22T00:15:00.000+00:00", None,
                       {"receptions": 2}, "Colby Parkinson"),
    ]
    all_rows = gsis_rows + espn_rows
    identity_docs = [{"sport": "NFL", "name": "Colby Parkinson",
                      "canonical_player_id": "cpid_x",
                      "provider_ids": {"espn": "4242557"}}]
    db = _FakeDB(all_rows, identity_docs, ["00-0036244", "4242557"])

    adapter = NFLPlayerHistoricalAdapter()
    q = HistoricalQuery(sport="NFL", entity_type="player",
                        market_family="receptions",
                        entity_name="Colby Parkinson",
                        entity_id="00-0036244")
    obs = await adapter.fetch_observations(db, q)

    # Must see BOTH pipelines (2026 ESPN rows + 2025 gsis rows).
    assert len(obs) == 4, (
        f"NFL dual-pipeline bridge must union both ID schemes; got {len(obs)}"
    )
    # The adapter must have fired a $in query (not single-id).
    f = db.player_game_actuals.last_filter
    assert isinstance(f.get("canonical_player_id"), dict), (
        "NFL adapter must query canonical_player_id via $in across both ids"
    )
    assert "$in" in f["canonical_player_id"]
    ids_queried = set(f["canonical_player_id"]["$in"])
    assert "00-0036244" in ids_queried
    assert "4242557" in ids_queried


@pytest.mark.asyncio
async def test_nfl_hi_current_season_outranks_prior_season():
    """2026 rows (newer event_time) must be returned first by the
    adapter's sort — the ordering is the L5/L10 contract."""
    from services.historical_intelligence import (
        NFLPlayerHistoricalAdapter, HistoricalQuery,
    )

    rows = [
        # Prior season (dates earlier in year 2026)
        _mk_actual_row("00-0033077", "2026-01-04T13:00:00", 2025,
                       {"pass_yds": 70}, "Dak Prescott"),
        _mk_actual_row("00-0033077", "2025-12-25T20:15:00", 2025,
                       {"pass_yds": 307}, "Dak Prescott"),
        # Current season (dates later in year 2026)
        _mk_actual_row("2577417", "2026-10-04T17:00:00.000+00:00", None,
                       {"pass_yds": 335}, "Dak Prescott"),
        _mk_actual_row("2577417", "2026-09-27T17:00:00.000+00:00", None,
                       {"pass_yds": 276}, "Dak Prescott"),
    ]
    identity_docs = [{"sport": "NFL", "name": "Dak Prescott",
                      "canonical_player_id": "cpid_dp",
                      "provider_ids": {"espn": "2577417"}}]
    db = _FakeDB(rows, identity_docs, ["00-0033077", "2577417"])

    adapter = NFLPlayerHistoricalAdapter()
    q = HistoricalQuery(sport="NFL", entity_type="player",
                        market_family="pass_yds",
                        entity_name="Dak Prescott",
                        entity_id="00-0033077")
    obs = await adapter.fetch_observations(db, q)

    assert len(obs) == 4
    # Sorted by event_time desc — the two current-2026 games (October)
    # MUST come first; the two prior-season games follow.
    assert obs[0].date.startswith("2026-10"), f"L5[0] stale: {obs[0].date}"
    assert obs[1].date.startswith("2026-09"), f"L5[1] stale: {obs[1].date}"
    # Season context must be honestly derived even when source row is null.
    assert obs[0].context.get("season") == 2026, (
        f"Current-2026 row must report season=2026, got "
        f"{obs[0].context.get('season')}"
    )
    assert obs[2].context.get("season") == 2025


@pytest.mark.asyncio
async def test_nfl_hi_bridge_soft_fail_preserves_single_id_query():
    """If the identity bridge raises, the adapter must still return
    results from the original single-id query (no silent zero)."""
    from services.historical_intelligence import (
        NFLPlayerHistoricalAdapter, HistoricalQuery,
    )

    rows = [
        _mk_actual_row("00-0036244", "2026-01-25T18:30:00", 2025,
                       {"receptions": 3}, "Colby Parkinson"),
    ]

    class _ExplodingDistinct(_FakeActuals):
        async def distinct(self, *_a, **_kw):
            raise RuntimeError("simulated bridge failure")

    db = _FakeDB(rows, [], [])
    db.player_game_actuals = _ExplodingDistinct(rows, [])

    adapter = NFLPlayerHistoricalAdapter()
    q = HistoricalQuery(sport="NFL", entity_type="player",
                        market_family="receptions",
                        entity_name="Colby Parkinson",
                        entity_id="00-0036244")
    obs = await adapter.fetch_observations(db, q)
    assert len(obs) == 1, (
        "Bridge failure must not drop pre-fix single-id results"
    )


# =====================================================================
# (B) NHL board rescue parity for sports= multi-select
# =====================================================================

def test_rescue_sport_parity_matches_both_singular_and_plural_params():
    """Static-source regression — the picks_routes rescue block must
    scope the rescue query by `sport_list` (multi-select) in addition
    to the legacy singular `sport=` param.  Prior to the fix the
    rescue only honoured `sport`, so `?sports=NHL` silently fell back
    to the full published slate (NHL buried under 46k Soccer rows).
    """
    import pathlib
    src = pathlib.Path(
        "/app/backend/routes/picks_routes.py"
    ).read_text()
    # Find the rescue block (between the first _rescue_query dict and
    # the rescue_missing_eligible call).  Must mention sport_list.
    m = re.search(
        r"_rescue_query\s*=\s*\{.*?rescue_missing_eligible",
        src, re.S,
    )
    assert m is not None, "rescue block not found in picks_routes.py"
    block = m.group(0)
    # Legacy singular path still present.
    assert "if sport and sport.lower() not in" in block
    # NEW: multi-select parity path.
    assert "elif sport_list:" in block, (
        "Rescue block must honour `sports=` CSV via sport_list — "
        "without this, NHL / any sport multi-select is swamped by "
        "the full published slate."
    )
    # And the multi-select path must stamp `sport` on the rescue query.
    scoped_section = block[block.index("elif sport_list:"):]
    assert "_rescue_query[\"sport\"]" in scoped_section


def test_rescue_market_parity_still_intact_regression_guard():
    """Protect the earlier P0-A canonical market filter parity — the
    fix for NHL must not regress market/league parity in the rescue
    block (both must coexist)."""
    import pathlib
    src = pathlib.Path(
        "/app/backend/routes/picks_routes.py"
    ).read_text()
    m = re.search(
        r"_rescue_query\s*=\s*\{.*?rescue_missing_eligible",
        src, re.S,
    )
    assert m is not None
    block = m.group(0)
    # Earlier parity blocks must still exist.
    assert "if market_list:" in block
    assert "if league_list:" in block
    assert "if game_id_list:" in block
    assert "if event_list:" in block
