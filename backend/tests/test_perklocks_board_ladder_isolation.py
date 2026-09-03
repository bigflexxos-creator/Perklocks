"""PERKLOCKS ROOT FIX (2026-09-03) — Board Utility Ladder Isolation.

Regression: ``_ladder_group_key`` in ``board_utility_layer`` was
matching the "totals_over"/"totals_under" family on ANY market string
containing "over "/"under " — including EVERY player-prop with the
Over/Under keyword.  That meant "Brett Bateman (TOR) Over 0.5 Hits",
"Vladimir Guerrero Jr. Over 1.5 H+R+RBIs", "Rafael Devers Over 0.5
Hits" — all Elite/Strong-Lock hitter props on the same MLB game —
collapsed into a single ``(event, "totals_over_hits", "")`` ladder
group and only ONE survivor per game reached the main board.

These tests pin the correct isolation contract:

  * Team totals (selection == "Over" / "Under") still ladder-collapse.

  * Player props (selection == "<player name>") ARE NOT collapsed
    under the team-totals family.  They ladder INDEPENDENTLY per
    player via the player-prop branch (line ~155 of the module),
    keyed on (event, family, player_name).
"""
from __future__ import annotations

from services.board_utility_layer import (
    _ladder_group_key,
    apply_ladder_collapse,
)


def _mk_team_total(event: str, side: str, line: float, lock: float) -> dict:
    return {
        "event":     event,
        "market":    f"Total Runs {side.capitalize()} {line}",
        "selection": side,
        "line":      line,
        "lock_score": lock,
        "published_lock_score": lock,
        "book_odds": -110,
    }


def _mk_player_prop(event: str, player: str, market: str, lock: float) -> dict:
    return {
        "event":     event,
        "market":    market,
        "selection": player,
        "lock_score": lock,
        "published_lock_score": lock,
        "book_odds": -180,
    }


def test_team_total_still_groups_into_totals_family():
    """Explicit team totals (selection=Over/Under) MUST still collapse
    onto the same ladder key so alt-line rungs are deduped."""
    a = _mk_team_total("A @ B", "Over", 8.5, 92.0)
    b = _mk_team_total("A @ B", "Over", 9.5, 88.0)
    ka = _ladder_group_key(a)
    kb = _ladder_group_key(b)
    assert ka == kb, f"expected same ladder key, got {ka} vs {kb}"
    assert ka[1].startswith("totals_over"), ka


def test_player_prop_is_isolated_from_team_totals_ladder():
    """PERKLOCKS ROOT FIX §4 — player-prop rows must NOT match the
    team-totals family just because their market string contains the
    substring 'Over '.  They must fall through to the player-prop
    branch keyed on the player selection.
    """
    devers  = _mk_player_prop(
        "Giants @ Pirates", "Rafael Devers",
        "Rafael Devers (SF) Over 0.5 Hits", 98.0,
    )
    reynolds = _mk_player_prop(
        "Giants @ Pirates", "Bryan Reynolds",
        "Bryan Reynolds (PIT) Over 1.5 Hits + Runs + RBIs", 99.0,
    )
    cruz = _mk_player_prop(
        "Giants @ Pirates", "Oneil Cruz",
        "Oneil Cruz (PIT) Over 0.5 Hits", 98.0,
    )
    k_d = _ladder_group_key(devers)
    k_r = _ladder_group_key(reynolds)
    k_c = _ladder_group_key(cruz)
    # Distinct players → distinct ladder groups.
    assert k_d != k_r != k_c, (k_d, k_r, k_c)
    # None of the player-prop keys claim the team-totals family.
    for k in (k_d, k_r, k_c):
        if k is not None:
            assert not k[1].startswith("totals_"), k


def test_multi_player_prop_slate_survives_ladder_collapse():
    """End-to-end: apply_ladder_collapse across many player-prop rows
    on the same event must NOT hide 90%+ of the rows behind a single
    winner.  Each unique player is its own ladder.
    """
    ev = "Giants @ Pirates"
    picks = [
        _mk_player_prop(ev, p, m, ls)
        for (p, m, ls) in [
            ("Rafael Devers",     "Rafael Devers (SF) Over 0.5 Hits", 98.0),
            ("Bryan Reynolds",    "Bryan Reynolds (PIT) Over 1.5 Hits + Runs + RBIs", 99.0),
            ("Oneil Cruz",        "Oneil Cruz (PIT) Over 0.5 Hits", 98.0),
            ("Bryce Eldridge",    "Bryce Eldridge (SF) Over 0.5 Hits", 98.0),
            ("Turner Hill",       "Turner Hill (SF) Over 0.5 Hits", 98.0),
            ("Andrew Knizner",    "Andrew Knizner (SF) Over 0.5 Hits", 98.0),
        ]
    ]
    _out, superseded = apply_ladder_collapse(list(picks))
    # Zero supersession because every ladder group has ONE member.
    assert superseded == 0
    # None of the picks should be flagged hide_from_main_board.
    hidden = [p for p in picks if p.get("hide_from_main_board") is True]
    assert not hidden, hidden


def test_same_player_multiple_rungs_still_collapse():
    """A single player with two rungs (Over 0.5 Hits + Over 1.5 Hits)
    IS a legit alt-line ladder → still collapses to the best rung.
    """
    ev = "Giants @ Pirates"
    hi = _mk_player_prop(ev, "Rafael Devers",
                          "Rafael Devers (SF) Over 0.5 Hits", 98.0)
    lo = _mk_player_prop(ev, "Rafael Devers",
                          "Rafael Devers (SF) Over 1.5 Hits", 82.0)
    k_hi = _ladder_group_key(hi)
    k_lo = _ladder_group_key(lo)
    assert k_hi == k_lo, (k_hi, k_lo)
    _out, superseded = apply_ladder_collapse([hi, lo])
    assert superseded == 1
    # lo is the loser, hi survives.
    assert lo.get("hide_from_main_board") is True
    assert not hi.get("hide_from_main_board")
