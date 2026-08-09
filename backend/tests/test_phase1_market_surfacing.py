"""Phase 1 — Market surfacing chip integrity tests.

Verifies the corrected chip set per user directive (2026-08-08):
  * MLB: adds Total Bases, RBIs.  NO Home Run, NO NRFI/YRFI chip.
  * NFL / CFB: adds 1st TD, Pass TDs, Pass Att, Pass Comp, Rush Att,
    Rush TDs, Receptions, Rec TDs.  NO Anytime TD chip (kept on
    dedicated ATD experience).
  * NBA: adds PRA, 3-Pointers.
  * Soccer: adds Handicap (spread token).  NO Corners/Cards/Shots
    chips (Phase 3).
  * Tennis: adds Spread.
  * Every new token maps to a valid regex.
  * Every existing intended chip is preserved.
  * Locks >85 contract stays untouched.
"""
from __future__ import annotations

import re
import pathlib

from fastapi.testclient import TestClient


_BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]


# ─────────────────────────────────────────────────────────────────────
#  Tests
# ─────────────────────────────────────────────────────────────────────
def _get_chips(sport: str):
    # Read the source of truth directly to avoid auth requirements
    # on the /api endpoint.  The route just returns
    # `SPORT_MARKETS[sport]` (see server.py:1449).
    import server as srv
    markets = srv.SPORT_MARKETS.get(sport, [])
    return markets, {"markets": markets}


def _tokens(sport: str) -> set[str]:
    markets, _ = _get_chips(sport)
    return {m["token"] for m in markets}


def _labels(sport: str) -> set[str]:
    markets, _ = _get_chips(sport)
    return {m["label"] for m in markets}


# 1. Total Bases appears for MLB
def test_mlb_has_total_bases_chip():
    tokens = _tokens("MLB")
    assert "batter_total_bases" in tokens
    assert "Total Bases" in _labels("MLB")


# 2. RBIs appears for MLB
def test_mlb_has_rbis_chip():
    assert "batter_rbis" in _tokens("MLB")
    assert "RBIs" in _labels("MLB")


# 3. Home Run was NOT duplicated into Locks
def test_mlb_has_no_home_run_chip():
    labels = _labels("MLB")
    for bad in ("Home Run", "Home Runs", "HR", "Homers"):
        assert bad not in labels, (
            f"MLB Locks must NOT show a Home Run chip; found {bad!r}"
        )
    tokens = _tokens("MLB")
    assert "batter_home_runs" not in tokens
    assert "home_run" not in tokens


# 4. NRFI/YRFI does NOT appear
def test_mlb_has_no_nrfi_yrfi_chip():
    tokens = _tokens("MLB")
    labels = _labels("MLB")
    for bad in ("nrfi_yrfi", "1st_inning_runs", "nrfi", "yrfi",
                "first_inning_runs"):
        assert bad not in tokens
    for bad in ("NRFI/YRFI", "NRFI", "YRFI", "1st Inning Runs",
                "First Inning Runs"):
        assert bad not in labels


# 5. Existing HR dedicated experience remains intact
def test_hr_dedicated_experience_still_present():
    # The HR-tab route/module must still exist even though we skipped
    # adding a Locks chip for it.
    hr_paths = [
        "/app/frontend/app/(tabs)/hrs.tsx",
        "/app/frontend/app/(tabs)/hr.tsx",
        "/app/frontend/app/hrs.tsx",
        "/app/frontend/app/hr.tsx",
        "/app/frontend/app/(tabs)/home-runs.tsx",
    ]
    hr_exists = any(pathlib.Path(p).exists() for p in hr_paths)
    # Fall back: any file in app/ that references HR pipeline.
    if not hr_exists:
        for f in pathlib.Path("/app/frontend/app").rglob("*.tsx"):
            src = f.read_text()
            if "home_run" in src.lower() or "home runs" in src.lower():
                hr_exists = True
                break
    assert hr_exists, (
        "expected the dedicated Home Runs experience to still exist "
        "somewhere in /app/frontend/app/"
    )


# 6. Existing ATD dedicated route/experience remains intact
def test_atd_dedicated_experience_still_present():
    atd_found = False
    for f in pathlib.Path("/app/frontend/app").rglob("*.tsx"):
        src = f.read_text().lower()
        if ("anytime td" in src or "anytime_td" in src
                or "atd" in src.split()):
            atd_found = True
            break
    if not atd_found:
        # Backend ingest side also proves the ATD pipeline is live.
        eng_src = (_BACKEND_ROOT / "sports_engine.py").read_text()
        atd_found = "player_anytime_td" in eng_src
    assert atd_found, "expected the ATD experience to remain intact"
    # And NFL Locks chips must NOT duplicate Anytime TD.
    assert "player_anytime_td" not in _tokens("NFL")
    assert "player_anytime_td" not in _tokens("CFB")
    for sport in ("NFL", "CFB"):
        for bad in ("Anytime TD", "Anytime Touchdown", "ATD"):
            assert bad not in _labels(sport), (
                f"{sport} Locks must not duplicate the ATD chip"
            )


# 7. NFL / CFB new market filters map correctly
def test_nfl_cfb_new_market_filters_present():
    expected = {
        "player_1st_td":            "1st TD",
        "player_pass_tds":          "Pass TDs",
        "player_pass_attempts":     "Pass Att",
        "player_pass_completions":  "Pass Comp",
        "player_rush_attempts":     "Rush Att",
        "player_rush_tds":          "Rush TDs",
        "player_receptions":        "Receptions",
        "player_reception_tds":     "Rec TDs",
    }
    for sport in ("NFL", "CFB"):
        markets, _ = _get_chips(sport)
        by_tok = {m["token"]: m["label"] for m in markets}
        for tok, lbl in expected.items():
            assert tok in by_tok, (
                f"{sport} missing new chip {tok!r}"
            )
            assert by_tok[tok] == lbl, (
                f"{sport} {tok} label={by_tok[tok]!r} != {lbl!r}"
            )
        # Preserved existing chips.
        for tok in ("moneyline", "spread", "totals", "passing_yards",
                    "rushing_yards", "receiving_yards"):
            assert tok in by_tok


# 8. PRA + 3-Pointers map correctly for NBA
def test_nba_pra_and_threes():
    markets, _ = _get_chips("NBA")
    by_tok = {m["token"]: m["label"] for m in markets}
    assert by_tok.get("player_points_rebounds_assists") == "PRA"
    assert by_tok.get("player_threes") == "3-Pointers"
    # Preserved existing.
    for tok in ("moneyline", "spread", "totals", "player_points",
                "player_rebounds", "player_assists"):
        assert tok in by_tok


# 9. Soccer Handicap maps correctly
def test_soccer_handicap_chip():
    markets, _ = _get_chips("Soccer")
    by_tok = {m["token"]: m["label"] for m in markets}
    assert by_tok.get("spread") == "Handicap"
    # Preserved existing.
    for tok in ("1x2", "totals", "btts", "anytime_scorer",
                "anytime_assist", "score_or_assist",
                "first_goal_scorer"):
        assert tok in by_tok
    # Phase-3 items must NOT appear.
    for phase3 in ("corners", "cards", "player_shots",
                    "player_shots_on_goal"):
        assert phase3 not in by_tok


# 10. Tennis Spread maps correctly
def test_tennis_spread_chip():
    markets, _ = _get_chips("Tennis")
    by_tok = {m["token"]: m["label"] for m in markets}
    assert by_tok.get("spread") == "Spread"
    # Preserved existing.
    for tok in ("match_winner", "tennis_game_alt", "sets",
                "tennis_totals"):
        assert tok in by_tok


# 11. No existing intended market chip was accidentally removed
def test_no_existing_intended_chip_removed():
    expected_snapshot = {
        "Soccer": {"1x2", "spread", "totals", "btts", "anytime_scorer",
                     "anytime_assist", "score_or_assist",
                     "first_goal_scorer"},
        "NBA": {"moneyline", "spread", "totals", "player_points",
                 "player_rebounds", "player_assists",
                 "player_points_rebounds_assists", "player_threes"},
        "NFL": {"moneyline", "spread", "totals", "passing_yards",
                 "rushing_yards", "receiving_yards", "player_1st_td",
                 "player_pass_tds", "player_pass_attempts",
                 "player_pass_completions", "player_rush_attempts",
                 "player_rush_tds", "player_receptions",
                 "player_reception_tds"},
        "CFB": {"moneyline", "spread", "totals", "passing_yards",
                 "rushing_yards", "receiving_yards", "player_1st_td",
                 "player_pass_tds", "player_pass_attempts",
                 "player_pass_completions", "player_rush_attempts",
                 "player_rush_tds", "player_receptions",
                 "player_reception_tds"},
        "MLB": {"moneyline", "run_line", "totals", "team_total",
                 "batter_hits", "batter_total_bases", "batter_rbis",
                 "batter_hits_runs_rbis", "pitcher_strikeouts",
                 "pitcher_outs"},
        "Tennis": {"match_winner", "spread", "tennis_game_alt", "sets",
                    "tennis_totals"},
    }
    for sport, expected in expected_snapshot.items():
        got = _tokens(sport)
        missing = expected - got
        assert not missing, (
            f"{sport} missing expected chips: {missing}"
        )


# 12. Main Locks eligibility remains >85 — no fallback restored.
def test_main_locks_still_requires_lock_score_gt_85():
    src = (_BACKEND_ROOT / "routes" / "picks_routes.py").read_text()
    # Base gate still floors at 85 (with legacy alias lock_score_v2).
    assert '"lock_score": {"$gte": 85' in src or \
             '"lock_score_v2": {"$gte": 85' in src, (
        "main Locks base filter must still gate at lock_score >= 85"
    )
    # Ensure the retired 85→75→65→55 fallback has NOT been reintroduced.
    lowered = re.findall(r"\$gte['\"]?:\s*(?:55|65|75)\b", src)
    forbidden = [m for m in lowered if m in ("55", "65", "75")]
    # Only allow those values in Rollover/Under-Lock/parlay paths
    # which are separate endpoints, not the main Locks base gate.
    # We assert that on the main "/today" query block, no such
    # threshold appears within the same 2000-char window.
    today_idx = src.find('@router.get("/today")')
    if today_idx > 0:
        window = src[today_idx:today_idx + 8000]
        assert "$gte\": 55" not in window
        assert "$gte\": 65" not in window
        assert "$gte\": 75" not in window


# 13. New tokens each map to a compiling regex.
def test_new_tokens_each_have_valid_regex():
    from server import _MARKET_REGEX
    for tok in ("batter_total_bases", "batter_rbis",
                "player_points_rebounds_assists", "player_threes",
                "player_1st_td", "player_pass_tds",
                "player_pass_attempts", "player_pass_completions",
                "player_rush_attempts", "player_rush_tds",
                "player_receptions", "player_reception_tds",
                "spread"):
        assert tok in _MARKET_REGEX, f"missing regex for {tok!r}"
        # Compiles without error.
        re.compile(_MARKET_REGEX[tok])


# 14. Sample market strings match the intended tokens.
def test_sample_market_strings_match_expected_tokens():
    import re as _re
    from server import _MARKET_REGEX

    def matches(token: str, text: str) -> bool:
        return bool(_re.search(_MARKET_REGEX[token], text, _re.IGNORECASE))

    # MLB
    assert matches("batter_total_bases", "Aaron Judge Over 1.5 Total Bases")
    assert matches("batter_rbis",        "Aaron Judge Over 0.5 RBIs")
    assert not matches("batter_hits",    "Aaron Judge Over 1.5 Total Bases")
    # NBA
    assert matches("player_points_rebounds_assists",
                    "Nikola Jokic Over 44.5 Points + Rebounds + Assists")
    assert matches("player_points_rebounds_assists",
                    "Player PRA Over 40.5")
    assert matches("player_threes",
                    "Steph Curry Over 4.5 Threes")
    # NFL / CFB
    assert matches("player_1st_td",         "Josh Allen 1st TD Scorer")
    assert matches("player_pass_tds",       "Mahomes Over 2.5 Passing TDs")
    assert matches("player_pass_attempts",  "Mahomes Over 32.5 Passing Attempts")
    assert matches("player_pass_completions", "Mahomes Over 22.5 Passing Completions")
    assert matches("player_rush_attempts",  "Saquon Over 18.5 Rushing Attempts")
    assert matches("player_rush_tds",       "Saquon Over 0.5 Rushing TDs")
    assert matches("player_receptions",     "Justin Jefferson Over 6.5 Receptions")
    assert matches("player_reception_tds",  "Justin Jefferson Over 0.5 Receiving TDs")
    # Soccer / Tennis
    assert matches("spread",   "-1.5 Spread")
    # Cross-checks: none of the new regexes cross-fire into wrong buckets.
    assert not matches("player_threes",     "Aaron Judge Over 1.5 Total Bases")
    assert not matches("batter_total_bases","Steph Curry Over 4.5 Threes")
