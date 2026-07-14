"""Sanity tests for the soccer multi-source ingest package.

Pure logic tests only — the HTTP fetchers are covered by manual
smoke-tests in the module docstrings. These verify:
  1. Season slug parser
  2. CSV row parsing (main leagues + extra leagues)
  3. Provider trust ranking
"""
from __future__ import annotations

import pytest

from services.soccer.cache import PROVIDER_TRUST
from services.soccer.sources.football_data_co_uk import (
    _parse_date,
    _parse_row,
    _season_to_slug,
    _to_float,
    _to_int,
)


class TestSeasonSlug:
    def test_dash_full(self):
        assert _season_to_slug("2024-2025") == "2425"

    def test_dash_short(self):
        assert _season_to_slug("2024-25") == "2425"

    def test_slash(self):
        assert _season_to_slug("2024/25") == "2425"

    def test_bare_slug(self):
        assert _season_to_slug("2425") == "2425"

    def test_2023_24(self):
        assert _season_to_slug("2023-24") == "2324"


class TestParseDate:
    def test_slash_2digit(self):
        assert _parse_date("16/08/24") == "2024-08-16"

    def test_slash_4digit(self):
        assert _parse_date("16/08/2024") == "2024-08-16"

    def test_bad(self):
        assert _parse_date("") is None
        assert _parse_date("garbage") is None


class TestParseRow:
    def test_full_row(self):
        row = {
            "Date": "16/08/24",
            "HomeTeam": "Arsenal",
            "AwayTeam": "Wolves",
            "FTHG": "2",
            "FTAG": "0",
            "FTR": "H",
            "AvgH": "1.30",
            "AvgD": "5.75",
            "AvgA": "10.50",
            "AvgCH": "1.32",
            "AvgCD": "5.50",
            "AvgCA": "10.00",
        }
        parsed = _parse_row(row, "EPL", "2024-25")
        assert parsed is not None
        assert parsed["home_team"] == "Arsenal"
        assert parsed["away_team"] == "Wolves"
        assert parsed["date"] == "2024-08-16"
        assert parsed["home_score"] == 2
        assert parsed["away_score"] == 0
        assert parsed["home_odds_open"] == 1.30
        assert parsed["home_odds_close"] == 1.32
        assert parsed["source"] == "football_data_co_uk"

    def test_missing_teams(self):
        row = {"Date": "16/08/24", "HomeTeam": "", "AwayTeam": "Wolves"}
        assert _parse_row(row, "EPL", "2024-25") is None

    def test_legacy_bb_columns(self):
        row = {
            "Date": "10/05/16",
            "HomeTeam": "Man City",
            "AwayTeam": "West Brom",
            "FTHG": "3",
            "FTAG": "0",
            "BbAvH": "1.60",
            "BbAvD": "4.20",
            "BbAvA": "6.00",
        }
        parsed = _parse_row(row, "EPL", "2015-16")
        assert parsed["home_odds_open"] == 1.60


class TestProviderTrust:
    def test_ranking(self):
        # football-data.co.uk is the industry-standard historical source
        # → highest trust for match-level data (scores + closing odds).
        assert PROVIDER_TRUST["football_data_co_uk"] > PROVIDER_TRUST["thesportsdb"]
        assert PROVIDER_TRUST["football_data_org"] > PROVIDER_TRUST["thesportsdb"]

    def test_unknown_fallback(self):
        assert PROVIDER_TRUST["unknown"] == 0


class TestNumericParsers:
    def test_to_int(self):
        assert _to_int("3") == 3
        assert _to_int("3.0") == 3
        assert _to_int("") is None
        assert _to_int("garbage") is None

    def test_to_float(self):
        assert _to_float("1.30") == 1.30
        assert _to_float("0") is None    # 0 = missing in football-data.co.uk
        assert _to_float("") is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
