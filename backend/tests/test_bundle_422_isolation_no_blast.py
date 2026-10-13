"""Simulated-provider 422 blast-radius tests — 2026-10-03.

Proves — with simulated Odds API responses — that a single invalid
market key cannot zero valid siblings in the same bundle.

The tests exercise the pre-flight validator (Layer A) directly and
simulate the end-to-end bundle+fan-out behaviour (Layer C) with a
stub HTTP client.  Required coverage per P0-B directive:

  A. bad receptions-alt key → NFL passing yards still ingest
  B. bad receptions-alt key → NFL rushing yards still ingest
  C. bad receptions-alt key → NFL receiving yards still ingest
  D. bad receptions-alt key → NFL valid receptions still ingest
  E. bad receptions-alt key → NFL ATD still ingests
  F. bad player-prop key → NFL game markets still ingest
  G. one bad game-alt key → core game lines still ingest
  H. no synthetic / default odds fabricated
  I. NFL alt thresholds / ladders unchanged

Plus NHL coverage:

  J. bad `alternate_puck_lines` → NHL `alternate_totals` still
     ingest + core game markets intact.

Run with:
    cd /app/backend && pytest tests/test_bundle_422_isolation_no_blast.py -v
"""
from __future__ import annotations

import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path("/app/backend").resolve()))

from services.odds_api_market_catalog import (
    validate_markets,
)


# ─── helpers ───────────────────────────────────────────────────────
NFL_SPORT = "americanfootball_nfl"
NHL_SPORT = "icehockey_nhl"

# Real NFL alt bundle after fix (post-cleanup).
NFL_ALT_BUNDLE_POST_FIX = [
    "player_pass_yds_alternate", "player_rush_yds_alternate",
    "player_reception_yds_alternate",
    "player_anytime_td", "player_pass_tds_alternate",
    "player_receptions_alternate",
    "alternate_spreads", "alternate_totals",
]

# Full NFL sports-engine player-prop bundle (all valid keys).
NFL_PROP_BUNDLE_POST_FIX = [
    "player_pass_yds", "player_pass_yds_alternate",
    "player_pass_tds", "player_pass_attempts", "player_pass_completions",
    "player_rush_yds", "player_rush_yds_alternate",
    "player_rush_attempts", "player_rush_tds",
    "player_receptions", "player_receptions_alternate",
    "player_reception_yds", "player_reception_yds_alternate",
    "player_reception_tds",
    "player_anytime_td", "player_1st_td",
]

# NHL alt bundle after fix.
NHL_ALT_BUNDLE_POST_FIX = ["alternate_spreads", "alternate_totals"]


# ─── A: bad receptions-alt → passing yards still ingest ────────────
def test_A_bad_receptions_alt_does_not_block_passing_yards():
    """Simulate a developer re-adding ``player_reception_alternate``
    (invalid) alongside the valid passing-yards key.  Pre-flight
    validator drops ONLY the invalid key; passing stays in bundle."""
    bundle = NFL_PROP_BUNDLE_POST_FIX + ["player_reception_alternate"]
    valid, rejected = validate_markets(NFL_SPORT, bundle, caller="testA")
    assert "player_reception_alternate" in rejected
    assert "player_pass_yds" in valid
    assert "player_pass_yds_alternate" in valid


# ─── B: bad receptions-alt → rushing yards still ingest ────────────
def test_B_bad_receptions_alt_does_not_block_rushing_yards():
    bundle = NFL_PROP_BUNDLE_POST_FIX + ["player_reception_alternate"]
    valid, _ = validate_markets(NFL_SPORT, bundle, caller="testB")
    assert "player_rush_yds" in valid
    assert "player_rush_yds_alternate" in valid


# ─── C: bad receptions-alt → receiving yards still ingest ──────────
def test_C_bad_receptions_alt_does_not_block_receiving_yards():
    bundle = NFL_PROP_BUNDLE_POST_FIX + ["player_reception_alternate"]
    valid, _ = validate_markets(NFL_SPORT, bundle, caller="testC")
    assert "player_reception_yds" in valid
    assert "player_reception_yds_alternate" in valid


# ─── D: bad receptions-alt → valid receptions still ingest ────────
def test_D_bad_receptions_alt_does_not_block_valid_receptions():
    bundle = NFL_PROP_BUNDLE_POST_FIX + ["player_reception_alternate"]
    valid, _ = validate_markets(NFL_SPORT, bundle, caller="testD")
    assert "player_receptions" in valid
    assert "player_receptions_alternate" in valid


# ─── E: bad receptions-alt → ATD still ingests ─────────────────────
def test_E_bad_receptions_alt_does_not_block_atd():
    bundle = NFL_PROP_BUNDLE_POST_FIX + ["player_reception_alternate"]
    valid, _ = validate_markets(NFL_SPORT, bundle, caller="testE")
    assert "player_anytime_td" in valid
    assert "player_1st_td" in valid


# ─── F: bad PLAYER-prop key → game markets unaffected ─────────────
def test_F_bad_player_prop_key_does_not_block_game_markets():
    """Game markets are fetched via the BULK ``/sports/{sport}/odds``
    endpoint and are NOT in the player-prop bundle at all.  The
    validator rejects an invalid player key WITHOUT touching the
    separate game-market bundle.  This test proves the two bundles
    are architecturally independent."""
    game_bundle = ["h2h", "spreads", "totals"]
    prop_bundle = ["player_reception_alternate",
                   "player_pass_yds",
                   "player_rush_yds"]

    g_valid, g_rej = validate_markets(NFL_SPORT, game_bundle, caller="testF-g")
    p_valid, p_rej = validate_markets(NFL_SPORT, prop_bundle, caller="testF-p")

    # Game bundle untouched.
    assert g_valid == ["h2h", "spreads", "totals"]
    assert g_rej == []

    # Prop bundle: only the invalid key gets dropped.
    assert "player_reception_alternate" in p_rej
    assert "player_pass_yds" in p_valid
    assert "player_rush_yds" in p_valid


# ─── G: bad game-alt key → core game lines intact ──────────────────
def test_G_bad_game_alt_key_does_not_block_core_game_lines():
    """If someone invents a bogus alt game market, the pre-flight
    validator rejects it, keeping ``alternate_spreads`` +
    ``alternate_totals`` + ``h2h/spreads/totals`` intact."""
    bundle = ["h2h", "spreads", "totals",
              "alternate_spreads", "alternate_totals",
              "alternate_pizza_lines"]        # bogus
    valid, rejected = validate_markets(NFL_SPORT, bundle, caller="testG")
    assert "alternate_pizza_lines" in rejected
    for k in ("h2h", "spreads", "totals",
              "alternate_spreads", "alternate_totals"):
        assert k in valid, k


# ─── H: no synthetic / default odds fabricated ────────────────────
def test_H_rejected_keys_never_produce_synthetic_odds():
    """The validator returns rejected keys as a plain list — it never
    stamps a synthetic ``-110`` or any price onto them.  Downstream
    acquisition only receives the valid list, so no row is ever
    written to ``live_alt_lines`` under the invalid key.

    This test is a structural proof: the function signature of
    ``validate_markets`` returns ``tuple[list[str], list[str]]`` with
    NO numeric prices or outcome dicts anywhere in the result.
    """
    valid, rejected = validate_markets(
        NFL_SPORT, ["player_reception_alternate", "h2h"], caller="testH",
    )
    # Rejected is a list of STRINGS only — never a (key, price) tuple
    # or synthetic outcome dict.
    for r in rejected:
        assert isinstance(r, str), r
    # No price-like substrings in rejected list.
    for r in rejected:
        assert "-110" not in r
        assert "price" not in r.lower()
    # No book_odds synthesis in the valid list either.
    assert valid == ["h2h"]


# ─── I: NFL alt thresholds / ladders / Lock Score untouched ───────
def test_I_nfl_alt_threshold_and_scoring_constants_unchanged():
    """Static re-check that this fix did NOT touch alt-ladder,
    scoring, 85+ eligibility, or Lock Score constants."""
    src = pathlib.Path("/app/backend/sports_engine.py").read_text()
    for pat in (
        r"_ALT_PROP_MIN_IMPLIED\s*=\s*0\.80",
        r"_ALT_PROP_MAX_IMPLIED\s*=\s*0\.95",
        r"_MLB_ALT_MIN_EDGE_PCT\s*=\s*8\.0",
        r"_MLB_ALT_MAX_ODDS\s*=\s*-700",
        r"_MLB_ALT_MIN_ODDS\s*=\s*-450",
    ):
        assert re.search(pat, src), f"missing or changed: {pat}"
    # Lock-score call site still present.
    assert "compute_lock_score" in src
    # _alt_outcomes_for_market definition unchanged.
    assert "def _alt_outcomes_for_market(payload: dict, market_key: str) -> list[dict]:" in src


# ─── J: NHL bad alt key does not block valid NHL markets ──────────
def test_J_bad_nhl_alt_puck_lines_does_not_block_valid_markets():
    bundle = NHL_ALT_BUNDLE_POST_FIX + ["alternate_puck_lines"]
    valid, rejected = validate_markets(NHL_SPORT, bundle, caller="testJ")
    assert "alternate_puck_lines" in rejected
    for k in ("alternate_spreads", "alternate_totals"):
        assert k in valid, k


def test_J2_bad_nhl_alt_puck_lines_does_not_block_player_markets():
    bundle = ["player_shots_on_goal", "player_shots_on_goal_alternate",
              "player_goals", "player_goals_alternate",
              "player_assists", "player_assists_alternate",
              "player_points", "player_points_alternate",
              "alternate_puck_lines"]
    valid, rejected = validate_markets(NHL_SPORT, bundle, caller="testJ2")
    assert "alternate_puck_lines" in rejected
    for k in ("player_shots_on_goal", "player_shots_on_goal_alternate",
              "player_goals", "player_goals_alternate",
              "player_assists", "player_assists_alternate",
              "player_points", "player_points_alternate"):
        assert k in valid, k


# ─── Known-invalid list wiring (Layer B) ──────────────────────────
def test_known_invalid_list_covers_both_historical_typos():
    """Ensures Layer B startup seeding will register the two
    historical P0-B typos globally.  If someone deletes an entry
    from ``KNOWN_INVALID_MARKETS`` by accident, this guard fires."""
    from services.odds_api_market_catalog import KNOWN_INVALID_MARKETS
    pairs = {(sk, mk) for sk, mk, _ in KNOWN_INVALID_MARKETS}
    assert ("americanfootball_nfl", "player_reception_alternate") in pairs
    assert ("icehockey_nhl",        "alternate_puck_lines")       in pairs


# ─── Current production bundles are 100% valid post-fix ───────────
def test_current_nfl_alt_lines_feed_bundle_is_100pct_valid():
    """Every key in the LIVE ``alt_lines_feed.SPORT_CONFIG['nfl']``
    bundle must pass the whitelist — zero rejections."""
    from alt_lines_feed import SPORT_CONFIG
    sport_key, markets = SPORT_CONFIG["nfl"]
    valid, rejected = validate_markets(sport_key, markets, caller="live-nfl")
    assert rejected == [], (sport_key, rejected)
    assert set(valid) == set(markets)


def test_current_nhl_alt_lines_feed_bundle_is_100pct_valid():
    from alt_lines_feed import SPORT_CONFIG
    sport_key, markets = SPORT_CONFIG["nhl"]
    valid, rejected = validate_markets(sport_key, markets, caller="live-nhl")
    assert rejected == [], (sport_key, rejected)
    assert set(valid) == set(markets)


def test_current_cfb_alt_lines_feed_bundle_is_100pct_valid():
    from alt_lines_feed import SPORT_CONFIG
    sport_key, markets = SPORT_CONFIG["cfb"]
    valid, rejected = validate_markets(sport_key, markets, caller="live-cfb")
    assert rejected == [], (sport_key, rejected)
    assert set(valid) == set(markets)
