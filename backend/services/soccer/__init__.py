"""Soccer data package — multi-source ingest with automatic fallback.

The design goal is simple: **never leave a soccer pick without team-level
context because one upstream provider is blocked**. Historically our
soccer signal was Understat-only; when Understat stopped serving inline
JSON in off-season, the entire soccer signal layer went dark.

Package layout:
    sources/                     one file per data provider
        football_data_co_uk.py   CSV — historical results + closing odds
        football_data_org.py     JSON — needs free API key
        thesportsdb.py           JSON — no key, teams + fixtures
        openligadb.py            JSON — no key, German leagues
        espn.py                  wraps existing ESPN scoreboard code

    fallback.py       provider chain orchestration
    cache.py          mongo-backed cache with source tracking
    models.py         normalized dataclasses (Match, Team, Standing, Fixture)

Public API is exposed via the top-level package:
    from services.soccer import (
        get_fixtures, get_standings, get_team,
        get_historical_results, refresh_all_leagues,
    )

Every cached document carries the `source` field — so we can audit which
provider gave us which data point and rank source reliability over time.
"""
from services.soccer.fallback import (
    get_fixtures,
    get_historical_results,
    get_standings,
    get_team,
    refresh_all_leagues,
)

__all__ = [
    "get_fixtures",
    "get_standings",
    "get_team",
    "get_historical_results",
    "refresh_all_leagues",
]
