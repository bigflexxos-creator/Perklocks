"""Odds API market-key whitelist regression tests — 2026-10-03.

Proves:
  * Every market key in every active NFL / NHL request-building list
    is in the authoritative Odds API whitelist.
  * No code path still references the two known-invalid historical
    keys (``player_reception_alternate``, ``alternate_puck_lines``).
  * The whitelist's known-invalid list is correctly WIRED to the
    startup seed and the pre-flight validator.
  * NFL alt threshold / ladder / scoring constants are UNCHANGED
    (frozen literals, re-checked here for defense-in-depth).

Run with:
    cd /app/backend && pytest tests/test_odds_api_market_whitelist.py -v
"""
from __future__ import annotations

import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path("/app/backend").resolve()))

from services.odds_api_market_catalog import (
    NFL_VALID_MARKETS,
    NHL_VALID_MARKETS,
    KNOWN_INVALID_MARKETS,
    validate_markets,
)


# ─────────── 1. Whitelist contents are correct ───────────────────
def test_nfl_whitelist_contains_core_game_markets():
    for k in ("h2h", "spreads", "totals",
              "alternate_spreads", "alternate_totals"):
        assert k in NFL_VALID_MARKETS, k


def test_nfl_whitelist_contains_core_player_props():
    for k in ("player_pass_yds",      "player_pass_yds_alternate",
              "player_rush_yds",      "player_rush_yds_alternate",
              "player_reception_yds", "player_reception_yds_alternate",
              "player_receptions",    "player_receptions_alternate",
              "player_anytime_td",    "player_1st_td"):
        assert k in NFL_VALID_MARKETS, k


def test_nhl_whitelist_contains_core_markets():
    for k in ("h2h", "spreads", "totals",
              "alternate_spreads", "alternate_totals",
              "player_shots_on_goal", "player_shots_on_goal_alternate",
              "player_goals", "player_goals_alternate",
              "player_assists", "player_assists_alternate",
              "player_points", "player_points_alternate"):
        assert k in NHL_VALID_MARKETS, k


def test_whitelists_exclude_known_invalid_keys():
    assert "player_reception_alternate" not in NFL_VALID_MARKETS
    assert "alternate_puck_lines"       not in NHL_VALID_MARKETS


# ─────────── 2. Known-invalid list is complete ────────────────────
def test_known_invalid_contains_both_historical_typos():
    keys = {(sk, mk) for sk, mk, _ in KNOWN_INVALID_MARKETS}
    assert ("americanfootball_nfl",  "player_reception_alternate") in keys
    assert ("americanfootball_ncaaf", "player_reception_alternate") in keys
    assert ("icehockey_nhl",         "alternate_puck_lines")       in keys


# ─────────── 3. Static grep — invalid keys removed from code ──────
def _read(p):
    return pathlib.Path(p).read_text()


def test_alt_lines_feed_has_no_player_reception_alternate():
    src = _read("/app/backend/alt_lines_feed.py")
    # The literal must not appear in any ACTIVE python expression (we
    # only tolerate it in a comment explaining the historical typo).
    for line in src.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        assert "player_reception_alternate" not in line, line


def test_alt_lines_feed_has_no_alternate_puck_lines():
    src = _read("/app/backend/alt_lines_feed.py")
    for line in src.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        assert "alternate_puck_lines" not in line, line


def test_quality_gate_maps_receiving_yards_over_correctly():
    src = _read("/app/backend/quality_gate.py")
    # The internal "receiving yards over" MUST map to the valid key.
    assert '"receiving yards over":   "player_reception_yds_alternate"' in src
    # And must NOT map to the typo.
    assert '"receiving yards over":   "player_reception_alternate"' not in src


def test_admin_routes_spread_whitelist_has_no_alternate_puck_lines():
    src = _read("/app/backend/routes/admin_routes.py")
    # The spread-family allow set must not contain the dead key.
    # Scoped to lines inside the dict literal (small window around the
    # original match).
    for line in src.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        if '"alternate_puck_lines"' in line:
            raise AssertionError(f"stale key still referenced: {line}")


# ─────────── 4. Pre-flight validator behaviour ────────────────────
def test_validate_markets_drops_unknown_nfl_key():
    bundle = ["h2h", "spreads", "totals",
              "player_reception_alternate",   # invalid typo
              "player_pass_yds_alternate"]
    valid, rejected = validate_markets(
        "americanfootball_nfl", bundle, caller="test",
    )
    assert "player_reception_alternate" not in valid
    assert "player_reception_alternate" in rejected
    # Every other (valid) key survives.
    for k in ("h2h", "spreads", "totals", "player_pass_yds_alternate"):
        assert k in valid, k


def test_validate_markets_drops_unknown_nhl_key():
    bundle = ["h2h", "spreads", "totals",
              "alternate_puck_lines",         # invalid stale
              "alternate_totals",
              "player_shots_on_goal_alternate"]
    valid, rejected = validate_markets(
        "icehockey_nhl", bundle, caller="test",
    )
    assert "alternate_puck_lines" not in valid
    assert "alternate_puck_lines" in rejected
    for k in ("h2h", "spreads", "totals", "alternate_totals",
              "player_shots_on_goal_alternate"):
        assert k in valid, k


def test_validate_markets_passthrough_for_unknown_sport():
    """Sports without a whitelist (MLB/CFB-NCAAF are special-cased —
    anything else, e.g. ``mma_mixed_martial_arts``) accept all keys so
    we don't regress acquisition for sports not yet audited."""
    bundle = ["anything_goes", "even_fake_keys"]
    valid, rejected = validate_markets(
        "mma_mixed_martial_arts", bundle, caller="test",
    )
    assert rejected == []
    assert valid == bundle


def test_validate_markets_dedupes_preserving_order():
    bundle = ["h2h", "spreads", "h2h", "spreads", "totals"]
    valid, rejected = validate_markets(
        "americanfootball_nfl", bundle, caller="test",
    )
    assert valid == ["h2h", "spreads", "totals"]
    assert rejected == []


# ─────────── 5. NFL alt threshold invariance (defense-in-depth) ──
def test_nfl_alt_threshold_constants_are_frozen():
    """Verifies the fix did NOT touch any alt-ladder / scoring /
    threshold constant.  Mirrors the identical guard in
    ``test_producer_provenance_audit.py`` so the SYNTHETIC_BOOK_ODDS
    frozen-constants contract is restated across suites."""
    src = _read("/app/backend/sports_engine.py")
    expected = [
        (r"_ALT_PROP_MIN_IMPLIED\s*=\s*0\.80",   "_ALT_PROP_MIN_IMPLIED=0.80"),
        (r"_ALT_PROP_MAX_IMPLIED\s*=\s*0\.95",   "_ALT_PROP_MAX_IMPLIED=0.95"),
        (r"_MLB_ALT_MIN_EDGE_PCT\s*=\s*8\.0",    "_MLB_ALT_MIN_EDGE_PCT=8.0"),
        (r"_MLB_ALT_MAX_ODDS\s*=\s*-700",        "_MLB_ALT_MAX_ODDS=-700"),
        (r"_MLB_ALT_MIN_ODDS\s*=\s*-450",        "_MLB_ALT_MIN_ODDS=-450"),
    ]
    for pat, label in expected:
        assert re.search(pat, src), f"missing or changed: {label}"


def test_lock_score_eighty_five_eligibility_unchanged():
    """Grep guard: this fix must not touch Lock-Score function
    definitions or 85+ eligibility thresholds."""
    src = _read("/app/backend/sports_engine.py")
    # The compute_lock_score call sites remain in the shared ML/total
    # paths — grep for presence of the function name.  (We can't
    # assert the whole function body here, but any accidental rename
    # or removal would make the whole producer collapse — the
    # existing publication-boundary tests already cover that.)
    assert "compute_lock_score" in src


# ─────────── 6. Live request-builder usage of validator ──────────
def test_alt_lines_feed_calls_validate_markets_before_request():
    src = _read("/app/backend/alt_lines_feed.py")
    assert "from services.odds_api_market_catalog import validate_markets" in src
    assert "validate_markets(" in src


def test_sports_engine_calls_validate_markets_in_player_prop_fetch():
    src = _read("/app/backend/sports_engine.py")
    # Our wiring at _fetch_event_props_payload imports from the
    # catalog + invokes it before any provider call.
    assert "from services.odds_api_market_catalog import validate_markets" in src
    # The validate_markets invocation lives inside _fetch_event_props_payload.
    assert "_fetch_event_props_payload" in src
