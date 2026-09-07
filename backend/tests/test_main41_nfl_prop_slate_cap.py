"""MAIN 41 — NFL prop full-slate acquisition guarantee.

Root defect (2026-06-06): ``_PROPS_PER_KEY_CAP`` was missing an
``americanfootball_nfl`` entry, so NFL fell back to
``_DEFAULT_PROPS_PER_KEY = 3``.  On a Sunday slate with 14-16 NFL
games only 3 events ever received prop-acquisition — the other
13+ games were silently starved before the model ever saw them.

Runtime evidence: FanDuel published rich player-prop ladders for
NE @ SEA (event id ``8c94552d022acec4a0458d70c19d3da9``) yet
Perklocks generated zero NFL player props for over 24 hours.

Surgical fix: pin ``americanfootball_nfl → 16`` so EVERY eligible
active-slate NFL event receives one fair prop-acquisition
opportunity.  The model / integrity gates still decide whether a
candidate becomes a Lock — this cap only guarantees the model
receives them.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def test_nfl_prop_cap_covers_full_sunday_slate():
    from sports_engine import _PROPS_PER_KEY_CAP, _DEFAULT_PROPS_PER_KEY
    assert "americanfootball_nfl" in _PROPS_PER_KEY_CAP, (
        "NFL missing from _PROPS_PER_KEY_CAP — will fall back to "
        f"_DEFAULT_PROPS_PER_KEY={_DEFAULT_PROPS_PER_KEY}, starving "
        "the Sunday slate to 3 events."
    )
    cap = _PROPS_PER_KEY_CAP["americanfootball_nfl"]
    assert cap >= 16, (
        f"NFL cap={cap} does not cover a full Sunday slate. Sunday "
        "carries up to 14 games; primetime adds 2 more. Cap must be "
        ">= 16 or fair-slate scheduling will drop legitimate events."
    )


def test_cfb_prop_cap_covers_typical_slate():
    from sports_engine import _PROPS_PER_KEY_CAP
    # CFB has 40-100+ games per weekend, but Locks-eligible power-
    # conference matchups are typically 8-10.  Pinned to 8 in the
    # closure so we don't burn credits on non-Locks games.
    assert _PROPS_PER_KEY_CAP.get("americanfootball_ncaaf", 0) >= 8


def test_nba_and_nhl_prop_caps_cover_full_nightly_slate():
    from sports_engine import _PROPS_PER_KEY_CAP
    assert _PROPS_PER_KEY_CAP.get("basketball_nba", 0) >= 12
    assert _PROPS_PER_KEY_CAP.get("icehockey_nhl", 0) >= 14


def test_nfl_prop_market_list_still_complete():
    """Prop-acquisition wiring reaches every real FanDuel NFL family
    the runtime evidence documented.  Missing any of these silently
    kills that family end-to-end."""
    from sports_engine import PLAYER_PROP_MARKETS
    required = {
        # Standard O/U ladders
        "player_pass_yds", "player_rush_yds",
        "player_reception_yds", "player_receptions",
        "player_pass_tds", "player_pass_completions",
        "player_pass_attempts",
        "player_rush_attempts", "player_rush_tds",
        "player_reception_tds",
        # Real observed alternate ladders (FanDuel screenshot proof)
        "player_pass_yds_alternate",
        "player_rush_yds_alternate",
        "player_reception_yds_alternate",
        "player_receptions_alternate",
        # Anytime TD & First TD
        "player_anytime_td", "player_1st_td",
    }
    nfl = set(PLAYER_PROP_MARKETS.get("NFL") or [])
    missing = required - nfl
    assert not missing, f"NFL PLAYER_PROP_MARKETS missing: {missing}"


if __name__ == "__main__":
    test_nfl_prop_cap_covers_full_sunday_slate()
    test_cfb_prop_cap_covers_typical_slate()
    test_nba_and_nhl_prop_caps_cover_full_nightly_slate()
    test_nfl_prop_market_list_still_complete()
    print("OK — NFL prop full-slate cap protection verified.")
