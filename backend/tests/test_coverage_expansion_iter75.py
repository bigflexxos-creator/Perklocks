"""Unit tests for Phase 3b — ATP Challenger + Qualifying (tml_stats)
and Phase 2 Liga MX canonical code.

Ensures the URL format, canonical league keys, and parser flow all
line up. Uses inline CSV bodies to avoid network calls."""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest


# ── Liga MX canonical code ──────────────────────────────────────────
class TestLigaMxCoverage:
    def test_liga_mx_in_league_codes(self):
        from services.soccer.models import LEAGUE_CODES
        assert "LigaMX" in LEAGUE_CODES
        assert LEAGUE_CODES["LigaMX"]["country"] == "Mexico"
        assert LEAGUE_CODES["LigaMX"]["tier"] == 1

    def test_liga_mx_in_extra_leagues_file(self):
        from services.soccer.sources.football_data_co_uk import (
            _EXTRA_LEAGUES_FILES, _EXTRA_LEAGUE_MAP,
        )
        assert _EXTRA_LEAGUES_FILES["LigaMX"] == "MEX.csv"
        # Multiple aliases resolve to LigaMX
        assert _EXTRA_LEAGUE_MAP["Mexico Liga MX"] == "LigaMX"
        assert _EXTRA_LEAGUE_MAP["Mexican Liga MX"] == "LigaMX"

    def test_liga_mx_in_thesportsdb(self):
        from services.soccer.sources.thesportsdb import _LEAGUE_ID_MAP
        assert _LEAGUE_ID_MAP["LigaMX"] == "4350"

    def test_extra_leagues_parser_produces_liga_mx(self):
        from services.soccer.sources.football_data_co_uk import (
            _parse_extra_leagues_row,
        )
        row = {
            "League":  "Mexico Liga MX",
            "Season":  "2024/2025",
            "Date":    "01/08/25",
            "Home":    "Club America",
            "Away":    "Chivas",
            "HG":      "2",
            "AG":      "1",
            "AvgH":    "1.85",
            "AvgD":    "3.60",
            "AvgA":    "4.20",
        }
        out = _parse_extra_leagues_row(row)
        assert out is not None
        assert out["league"] == "LigaMX"
        assert out["home_team"] == "Club America"
        assert out["home_score"] == 2
        assert out["away_score"] == 1
        assert out["home_odds_open"] == 1.85


# ── ATP Challenger + Qualifying (Phase 3b) ─────────────────────────
_SAMPLE_CH_CSV = (
    "tourney_id,tourney_name,surface,draw_size,tourney_level,indoor,"
    "tourney_date,match_num,winner_id,winner_seed,winner_entry,winner_name,"
    "winner_hand,winner_ht,winner_ioc,winner_age,winner_rank,"
    "winner_rank_points,loser_id,loser_seed,loser_entry,loser_name,"
    "loser_hand,loser_ht,loser_ioc,loser_age,loser_rank,loser_rank_points,"
    "score,best_of,round,minutes,w_ace,w_df,w_svpt,w_1stIn,w_1stWon,"
    "w_2ndWon,w_SvGms,w_bpSaved,w_bpFaced,l_ace,l_df,l_svpt,l_1stIn,"
    "l_1stWon,l_2ndWon,l_SvGms,l_bpSaved,l_bpFaced\n"
    "2024-2205,Noumea,Hard,63,C,,20240108,1,B752,,,Stephane Bohli,R,185,"
    "SUI,24.4,219,187,D469,1,,Nicolas Devilder,L,173,FRA,27.7,115,388,"
    "6-2 7-6(3),3,R32,90,4,2,60,40,25,10,10,3,5,3,1,55,30,20,9,9,2,7\n"
)


class TestTmlStatsSource:
    def test_challenger_url_format(self):
        from services.tennis.sources import tml_stats
        assert "stats.tennismylife.org" in tml_stats._BASE

    def test_fetch_challenger_year_parses_rows(self):
        from services.tennis.sources import tml_stats

        async def _mock_fetch(url):
            assert "2024_challenger.csv" in url
            return _SAMPLE_CH_CSV

        with patch.object(tml_stats, "_fetch_csv", new=_mock_fetch):
            matches = asyncio.run(tml_stats.fetch_challenger_year(2024))
        assert len(matches) == 1
        m = matches[0]
        assert m["winner_name"] == "Stephane Bohli"
        assert m["loser_name"] == "Nicolas Devilder"
        assert m["surface"] == "Hard"
        assert m["date"] == "2024-01-08"
        assert m["circuit"] == "challenger"
        assert m["source"] == "tml_stats"
        assert m["w_ace"] == 4

    def test_fetch_atp_quali_url_correct(self):
        from services.tennis.sources import tml_stats

        captured_url = []

        async def _mock_fetch(url):
            captured_url.append(url)
            return _SAMPLE_CH_CSV

        with patch.object(tml_stats, "_fetch_csv", new=_mock_fetch):
            matches = asyncio.run(tml_stats.fetch_atp_quali_year(2025))
        assert "atp_quali/2025_atp_quali.csv" in captured_url[0]
        assert len(matches) == 1
        assert matches[0]["circuit"] == "atp_quali"

    def test_fetch_all_years_combines(self):
        from services.tennis.sources import tml_stats

        async def _mock_fetch(url):
            return _SAMPLE_CH_CSV

        with patch.object(tml_stats, "_fetch_csv", new=_mock_fetch):
            out = asyncio.run(tml_stats.fetch_all_years([2023, 2024]))
        # 2 years × 2 flavors × 1 row each = 4 matches
        assert len(out) == 4
        circuits = {m["circuit"] for m in out}
        assert circuits == {"challenger", "atp_quali"}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
