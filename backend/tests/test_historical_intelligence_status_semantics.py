"""P0 (2026-09-18) — Historical Intelligence honest availability semantics.

AVAILABLE_WITH_DATA  : adapter consulted, ≥1 observation
AVAILABLE_EMPTY      : adapter consulted, genuinely nothing
SOURCE_UNAVAILABLE   : no adapter registered for the sport
QUERY_FAILED         : adapter crashed → HistoricalQueryFailed (route → 503)

Regression: a crashed adapter must NEVER be returned as a 200 with
0 observations (the false "NO RECENT HISTORY" the Expo surface showed).
"""
import asyncio

import pytest

from services import historical_intelligence as hi


def _q(sport="ZZZ"):
    return hi.HistoricalQuery(
        sport=sport, entity_type="player", entity_id="x", entity_name="X Y",
        opponent_id=None, opponent_name=None, market_family="pass_yds",
        current_threshold=150.0, sample_scope="L10", venue_scope="ALL",
        context_scope=None, side="over",
    )


class _Obs(hi.HistoricalAdapter):
    def __init__(self, obs): self._obs = obs
    async def fetch_observations(self, db, q): return self._obs


class _Boom(hi.HistoricalAdapter):
    async def fetch_observations(self, db, q): raise RuntimeError("mongo down")


@pytest.fixture
def registry():
    saved = dict(hi._SPORT_ADAPTERS)
    yield hi._SPORT_ADAPTERS
    hi._SPORT_ADAPTERS.clear(); hi._SPORT_ADAPTERS.update(saved)


def test_source_unavailable_when_no_adapter(registry):
    registry.pop("ZZZ", None)
    r = asyncio.run(hi.query_historical(None, _q("ZZZ")))
    assert r.status == hi.HI_STATUS_SOURCE_UNAVAILABLE
    assert r.sample_size == 0 and r.data_coverage["total_observations"] == 0
    assert r.to_dict()["status"] == "SOURCE_UNAVAILABLE"


def test_available_empty_when_adapter_returns_nothing(registry):
    hi.register_adapter("ZZZ", _Obs([]))
    r = asyncio.run(hi.query_historical(None, _q("ZZZ")))
    assert r.status == hi.HI_STATUS_EMPTY
    assert r.sample_size == 0


def test_available_with_data(registry):
    obs = [hi.HistoricalObservation(date=f"2025-09-{d:02d}", opponent_id=None,
                                    opponent_name="OPP", home_away="home",
                                    actual=200.0 + d, context={}, provenance="t",
                                    event_id=None) for d in range(1, 11)]
    hi.register_adapter("ZZZ", _Obs(obs))
    r = asyncio.run(hi.query_historical(None, _q("ZZZ")))
    assert r.status == hi.HI_STATUS_WITH_DATA
    assert r.sample_size == 10 and r.hits == 10
    assert r.data_coverage["total_observations"] == 10


def test_query_failed_raises_never_false_empty(registry):
    hi.register_adapter("ZZZ", _Boom())
    with pytest.raises(hi.HistoricalQueryFailed) as ei:
        asyncio.run(hi.query_historical(None, _q("ZZZ")))
    assert ei.value.sport == "ZZZ"


def test_route_maps_query_failed_to_503(registry, monkeypatch):
    """Route contract: 503 + detail.status == QUERY_FAILED."""
    from fastapi import HTTPException
    from routes import historical_intelligence_routes as routes

    class _Picks:
        async def find_one(self, *_a, **_k):
            return {"id": "p1", "sport": "ZZZ", "market": "X Y Over 150.5 Player Pass Yds",
                    "player_name": "X Y", "line": 150.5}

    class _DB:
        name = "test_db"
        picks = _Picks()

    class _Req:
        headers = {"host": "unit.test"}

    hi.register_adapter("ZZZ", _Boom())
    monkeypatch.setattr(routes, "_get_db", lambda: _DB())
    monkeypatch.setattr(routes, "resolve_market_family", lambda s, m: "pass_yds")
    with pytest.raises(HTTPException) as ei:
        asyncio.run(routes.historical_intelligence(
            _Req(), None, "p1", sample_scope="L10", venue_scope="ALL", context_scope=None))
    assert ei.value.status_code == 503
    assert ei.value.detail["status"] == "QUERY_FAILED"
    assert ei.value.detail["served_by"]["host"] == "unit.test"
