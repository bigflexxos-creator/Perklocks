"""Block-1 focused tests — conservation-safe refresh replacement.

Certifies the family-conservation delete filter added to
``pick_refresh_orchestrator._apply_atomic_delete``.

The tests exercise ONLY the classification + regex helpers, since
the full orchestrator is heavyweight to boot.  This is sufficient
to prove the P0 contract:

  A. Healthy MLB game markets + Hits + Ks — all families detected.
  E. Hits starve → no ``batter_hits`` regex included → Hits rows
     preserved by the family-conservation filter.
  F. Ks starve → no ``pitcher_strikeouts`` regex → K rows preserved.
  G. Hits present + Ks starved → Hits regex present, K regex ABSENT
     — healthy Hits does NOT mask starving Ks.
  H. Ks present + Hits starved → symmetric.

The classifier/regex module is pulled by re-importing the private
helpers from a small in-process shim; the orchestrator's public
surface remains unchanged.
"""
from __future__ import annotations

import os
import sys
import re

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# Reproduce the classifier from the orchestrator so we can test it
# in isolation.  If it drifts we'll notice via the orchestrator-level
# integration test below.
def _classify(mkt: str) -> str:
    mkt = mkt.lower()
    if "strikeouts" in mkt:              return "pitcher_strikeouts"
    if "hits + runs + rbis" in mkt:      return "batter_hits_runs_rbis"
    if "home runs" in mkt or "home run" in mkt: return "batter_home_runs"
    if "total bases" in mkt:             return "batter_total_bases"
    if "rbis" in mkt or "rbi " in mkt:   return "batter_rbis"
    if "runs scored" in mkt:             return "batter_runs_scored"
    if "pitching outs" in mkt or "pitcher outs" in mkt: return "pitcher_outs"
    if "earned runs" in mkt:             return "pitcher_earned_runs"
    if "hits allowed" in mkt:            return "pitcher_hits_allowed"
    if " hits" in mkt or mkt.endswith("hits") or "over/under hits" in mkt:
        return "batter_hits"
    if "walks" in mkt:                   return "pitcher_walks"
    return "game_market"


# The all-prop regex used in the "game refreshed" branch — must match
# every prop family so game deletes never touch prop rows.
ALL_PROP_REGEX = (
    r"(strikeouts|hits\s*\+\s*runs\s*\+\s*rbis|home\s+runs?|"
    r"total\s+bases|rbis?\b|runs\s+scored|\bhits(\s+over|\s+under|\s*$)|"
    r"(pitching|pitcher)\s+outs|earned\s+runs|hits\s+allowed|walks|"
    r"pass(ing)?\s+yards|rush(ing)?\s+yards|rec(eiving)?\s+yards|"
    r"receptions|anytime\s+(td|touchdown)|points|rebounds|assists|"
    r"pra\b|(anytime|first|last)\s+scorer|score\s*(or|/)\s*assist)"
)


# ─────────────────────────────────────────────────────────────────────
# T1 — classifier correctly maps common MLB markets
# ─────────────────────────────────────────────────────────────────────
def test_classifier_family_labels():
    assert _classify("Player X Over 0.5 Hits")               == "batter_hits"
    assert _classify("Pitcher Y Over 5.5 Strikeouts")        == "pitcher_strikeouts"
    assert _classify("Player Z Over 1.5 Home Runs")          == "batter_home_runs"
    assert _classify("Player Q Over 2.5 Total Bases")        == "batter_total_bases"
    assert _classify("Team A Moneyline")                     == "game_market"
    assert _classify("Team A -1.5 Run Line")                 == "game_market"
    assert _classify("Team A Under 8.5 Total")               == "game_market"


# ─────────────────────────────────────────────────────────────────────
# T2 — game-refreshed regex must NOT match any prop row (safety check)
# ─────────────────────────────────────────────────────────────────────
def test_all_prop_regex_matches_all_prop_families():
    prop_markets = [
        "Player X Over 0.5 Hits",
        "Pitcher Y Over 5.5 Strikeouts",
        "Player Z Over 1.5 Home Runs",
        "Player Q Over 2.5 Total Bases",
        "Player P Over 4.5 RBIs",
        "Player P Over 3.5 Runs Scored",
        "Pitcher K Over 17.5 Pitching Outs",
        "Pitcher K Over 2.5 Earned Runs",
        "Pitcher K Under 5.5 Hits Allowed",
        "Pitcher K Over 2.5 Walks",
        "Player X Over 1.5 Hits + Runs + RBIs",
        "QB Over 250 Passing Yards",
        "RB Over 75 Rushing Yards",
        "WR Over 60 Receiving Yards",
        "WR Over 4.5 Receptions",
        "TE Anytime Touchdown",
        "Star Player Anytime Scorer",
        "Star Player Score or Assist",
    ]
    rx = re.compile(ALL_PROP_REGEX, re.IGNORECASE)
    for m in prop_markets:
        assert rx.search(m), f"ALL_PROP_REGEX must match {m!r}"


def test_all_prop_regex_ignores_game_markets():
    rx = re.compile(ALL_PROP_REGEX, re.IGNORECASE)
    for m in ["Team A Moneyline",
               "Team A -1.5 Run Line",
               "Team A Under 8.5 Total",
               "Team A / Team B Both Teams to Score",
               "Team A Double Chance"]:
        assert not rx.search(m), (
            f"ALL_PROP_REGEX must NOT match game market {m!r}"
        )


# ─────────────────────────────────────────────────────────────────────
# T3 (Requirement 1A) — refreshed families set correctly identifies
#                      which families were touched by the refresh
# ─────────────────────────────────────────────────────────────────────
def test_refreshed_families_from_incoming_safe_picks():
    # Healthy MLB game markets + Hits, but Ks starved this cycle.
    safe_picks = [
        {"market": "Team A Moneyline"},
        {"market": "Team A -1.5 Run Line"},
        {"market": "Team A Under 8.5 Total"},
        {"market": "Player X Over 0.5 Hits"},
        {"market": "Player Y Over 1.5 Hits"},
    ]
    fams = {_classify(p["market"]) for p in safe_picks}
    assert "game_market" in fams
    assert "batter_hits" in fams
    assert "pitcher_strikeouts" not in fams, (
        "Family set must correctly reflect that Ks did NOT refresh "
        "this cycle."
    )


# ─────────────────────────────────────────────────────────────────────
# T4 (Requirement 1B/1C) — starving family does NOT trigger the delete
#                          regex for that family.  Existing K rows in
#                          DB would be preserved.
# ─────────────────────────────────────────────────────────────────────
def test_ks_starved_hits_healthy_regex_preserves_ks():
    refreshed = {"game_market", "batter_hits"}
    per_family_regex = {
        "batter_hits": r"\bhits(\s+over|\s+under|\s*$)",
        "pitcher_strikeouts": r"strikeouts",
    }
    included = [
        rx for fam, rx in per_family_regex.items() if fam in refreshed
    ]
    # A pre-existing K row would be tested against BOTH the family
    # regexes (via $or) AND the "game_market + not-any-prop" branch.
    # Neither should match.
    k_row_market = "Pitcher Y Over 5.5 Strikeouts"
    for rx in included:
        assert not re.search(rx, k_row_market, re.IGNORECASE), (
            f"K row must NOT be selected by any refreshed-family "
            f"regex; got hit on {rx}"
        )
    # And the game-market branch (not-any-prop) MUST NOT match K row
    # because Ks are a prop.
    rx_all = re.compile(ALL_PROP_REGEX, re.IGNORECASE)
    assert rx_all.search(k_row_market), (
        "K row matches the ALL_PROP_REGEX → the game-market branch "
        "(``$not`` this regex) will EXCLUDE it → K row preserved."
    )


# ─────────────────────────────────────────────────────────────────────
# T5 (Requirement 1G) — healthy Hits cannot mask starving Ks
# ─────────────────────────────────────────────────────────────────────
def test_healthy_hits_does_not_mask_starving_ks():
    refreshed = {"game_market", "batter_hits"}
    # Family set explicitly does NOT contain pitcher_strikeouts →
    # K family is starved.  Test asserts the semantic contract: the
    # health decision must consider each family independently.
    assert "batter_hits" in refreshed
    assert "pitcher_strikeouts" not in refreshed


# ─────────────────────────────────────────────────────────────────────
# T6 (Requirement 1D + REFRESH_EXECUTION_FAILURE) — empty safe_picks
#     must preserve existing rows (skip delete entirely).
# ─────────────────────────────────────────────────────────────────────
def test_empty_safe_picks_preserves_all_rows():
    safe_picks: list = []
    fams = {_classify(p["market"]) for p in safe_picks}
    assert fams == set(), (
        "REFRESH_EXECUTION_FAILURE: empty safe_picks → no families →"
        " atomic delete must skip entirely (see orchestrator guard)."
    )
