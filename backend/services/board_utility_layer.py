"""Board Utility Layer — PHASE 0 §9-§11 (2026-06).

Read-time-only projection layer that prevents two categories of picks
from flooding the main Locks board:

  §9  Extreme-juice utility
      A pick with sportsbook odds worse than -1000 (e.g. -1500, -2500,
      -10000) is mathematically publishable — the model likes it AND
      the book carries a real line — but at that price the return per
      unit risked is so poor that showing it as a headline Lock hurts
      the product.  Extreme-juice picks are demoted with
      ``consumer_disposition="EXTREME_JUICE"`` and hidden from the
      main board via ``hide_from_main_board=True``.  They REMAIN
      canonically eligible so Parlay 2.0 (Phase 7) can still use them
      as legs where mathematically useful.

  §10-§11  Alt-line ladder flooding
      Alternate totals typically arrive as a ladder — e.g. Under 3.5,
      Under 4.5, Under 5.5 for a single event.  Without a utility
      layer the board fills with the same "Under" thesis at four
      different lines from the same event.  We collapse the ladder
      per (event, market_family, side) keeping ONLY the highest-
      utility rung and mark the others
      ``consumer_disposition="DISPLAY_LADDER_SUPERSEDED"``.

  Utility ranking within a ladder
  -------------------------------
  Utility = 0.60 * lock_score  +  0.40 * edge_percent_scaled
  where edge_percent_scaled = max(0, min(edge_percent, 25)) * (100/25)
  (edge above 25% is capped so a wild outlier doesn't dominate).

STRICT CONTRACTS
----------------
* Read-only.  Does NOT touch ``published_lock_score`` /
  ``lock_score`` / model probability / canonical identity.
* Cannot silently drop a pick.  Every demoted pick keeps its
  original fields AND receives ``consumer_disposition`` +
  ``disposition_reason`` + ``disposition_stage`` so the
  transparency contract from Phase 1E is preserved.
* Cannot promote a previously-hidden pick.  This layer only
  ever tightens visibility.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("lockscore.board_utility")


# ─────────────────────────────────────────────────────────────────────
# §9 — Extreme-juice utility
# ─────────────────────────────────────────────────────────────────────
# Threshold: anything at -1000 or worse is EXTREME_JUICE.
# At -1000 you risk 10u to win 1u — a $0.10 return per $1 risked
# regardless of hit rate.  These picks may be genuine model favourites
# but they aren't publishable Locks; they belong in Parlay 2.0.
EXTREME_JUICE_MAX_AMERICAN: int = -1000


def _american_worse_than(american: Any, threshold: int) -> bool:
    """Return True iff `american` is a negative price at or beyond
    `threshold` (both must be negative; -2000 is worse than -1000)."""
    try:
        v = int(american)
    except (TypeError, ValueError):
        return False
    if threshold >= 0 or v >= 0:
        return False
    return v <= threshold


def apply_extreme_juice_utility(picks: list[dict]) -> tuple[list[dict], int]:
    """Tag picks with book_odds <= -1000 as EXTREME_JUICE.

    Returns ``(picks, count_tagged)``.  Picks are MUTATED in place —
    ``consumer_disposition``, ``disposition_reason``,
    ``disposition_stage`` are added, and ``hide_from_main_board``
    is set to True so the main-board queries drop them.  The pick
    itself remains in the response list (transparency contract).
    """
    tagged = 0
    for p in picks:
        if not isinstance(p, dict):
            continue
        # Already tagged with a stronger disposition — respect earlier
        # decisions (Phase 1E — never overwrite an upstream tag).
        if p.get("consumer_disposition") in (
            "DISPLAY_HIDDEN_BY_QUALITY_GATE",
            "DISPLAY_HIDDEN_BY_MATCHUP",
            "EXTREME_JUICE",
            "DISPLAY_LADDER_SUPERSEDED",
        ):
            continue
        if _american_worse_than(p.get("book_odds"),
                                EXTREME_JUICE_MAX_AMERICAN):
            p["consumer_disposition"] = "EXTREME_JUICE"
            p["disposition_reason"]   = (
                f"book_odds <= {EXTREME_JUICE_MAX_AMERICAN}"
            )
            p["disposition_stage"]    = "board_utility_layer"
            p["hide_from_main_board"] = True
            tagged += 1
    if tagged:
        logger.info("BoardUtility: tagged %d picks as EXTREME_JUICE", tagged)
    return picks, tagged


# ─────────────────────────────────────────────────────────────────────
# §10-§11 — Alt-line ladder flooding
# ─────────────────────────────────────────────────────────────────────
# Markets that participate in ladder collapse.  Match on the
# lower-cased "market" string.
_LADDER_MARKET_FAMILIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    # (family_key, substrings that identify the family in `market`)
    ("totals_over",  ("over ",)),
    ("totals_under", ("under ",)),
)
# Player-prop ladders — e.g. "Over 1.5 Shots on Target",
# "Over 2.5 Shots on Target".  Family key includes the metric name so
# we don't collapse Shots-Over-1.5 with Corners-Over-1.5.
_PROP_METRICS: tuple[str, ...] = (
    "shots on target", "shots", "hits", "strikeouts",
    "passing yards", "rushing yards", "receiving yards",
    "points", "rebounds", "assists", "corners",
)


def _ladder_group_key(p: dict) -> tuple[str, str, str] | None:
    """Return a hashable ladder-group key for a pick, or None if the
    pick is not part of a ladder-collapsible family.

    Key shape: ``(event, family, side_or_player)`` so ladders collapse
    per event, per market family, per side (e.g. Home team totals
    ladder is separate from Away team totals ladder).
    """
    event = (p.get("event") or "").strip()
    if not event:
        return None
    market = (p.get("market") or "").lower().strip()
    if not market:
        return None
    # ── Team totals ladder ──
    for family, needles in _LADDER_MARKET_FAMILIES:
        if any(n in market for n in needles):
            # Family key includes the metric (e.g. "totals_over_goals",
            # "totals_over_corners") so goal ladders and corner ladders
            # don't collapse together.
            metric = "goals"
            for mm in _PROP_METRICS:
                if mm in market:
                    metric = mm
                    break
            return (event, f"{family}_{metric}", "")
    # ── Player-prop ladders (over/under N.5 <metric>) ──
    #    Only if the market string contains one of the known prop metrics
    #    AND an Over/Under keyword.  This safely excludes moneylines,
    #    spreads, ATG, BTTS.
    if "over" in market or "under" in market:
        sel = (p.get("selection") or "").strip().lower()
        if sel:
            for mm in _PROP_METRICS:
                if mm in market:
                    side = "over" if "over" in market else "under"
                    return (event, f"prop_{side}_{mm}", sel)
    return None


def _utility_score(p: dict) -> float:
    """Combined Lock Score + Edge utility for ladder ranking."""
    try:
        ls = float(p.get("published_lock_score")
                   or p.get("lock_score") or 0)
    except (TypeError, ValueError):
        ls = 0.0
    try:
        edge = float(p.get("edge_percent") or 0)
    except (TypeError, ValueError):
        edge = 0.0
    edge_scaled = max(0.0, min(edge, 25.0)) * (100.0 / 25.0)
    return 0.60 * ls + 0.40 * edge_scaled


def apply_ladder_collapse(picks: list[dict]) -> tuple[list[dict], int]:
    """Collapse alt-line ladders per (event, family, side).

    Only the highest-utility pick per ladder group survives on the
    main board.  Others receive
    ``consumer_disposition="DISPLAY_LADDER_SUPERSEDED"`` and
    ``hide_from_main_board=True``.

    Returns ``(picks, count_superseded)``.
    """
    # Group picks by ladder key.  Non-ladder picks are untouched.
    groups: dict[tuple, list[dict]] = {}
    for p in picks:
        if not isinstance(p, dict):
            continue
        # Respect prior dispositions.
        if p.get("consumer_disposition") in (
            "DISPLAY_HIDDEN_BY_QUALITY_GATE",
            "DISPLAY_HIDDEN_BY_MATCHUP",
            "EXTREME_JUICE",
            "DISPLAY_LADDER_SUPERSEDED",
        ):
            continue
        # Also skip picks already hidden by an earlier stage.
        if p.get("hide_from_main_board") is True:
            continue
        key = _ladder_group_key(p)
        if key is None:
            continue
        groups.setdefault(key, []).append(p)

    superseded = 0
    for key, members in groups.items():
        if len(members) < 2:
            continue
        # Rank members by utility, best first.
        ranked = sorted(members, key=_utility_score, reverse=True)
        winner = ranked[0]
        for loser in ranked[1:]:
            loser["consumer_disposition"] = "DISPLAY_LADDER_SUPERSEDED"
            loser["disposition_reason"]   = (
                f"superseded by {winner.get('market')} @ line "
                f"{winner.get('line')} (utility "
                f"{_utility_score(winner):.1f} > "
                f"{_utility_score(loser):.1f})"
            )
            loser["disposition_stage"] = "board_utility_layer"
            loser["hide_from_main_board"] = True
            superseded += 1
    if superseded:
        logger.info(
            "BoardUtility: %d ladder rungs marked DISPLAY_LADDER_SUPERSEDED",
            superseded,
        )
    return picks, superseded


# ─────────────────────────────────────────────────────────────────────
# Public entry point — run both passes in a single call.
# ─────────────────────────────────────────────────────────────────────
def apply_board_utility_layer(picks: list[dict]) -> dict:
    """Apply extreme-juice + ladder collapse in the intended order.

    Extreme juice first — a -2500 favourite that's ALSO the top of a
    ladder must be filtered out BEFORE the ladder pass so the ladder
    winner is chosen from the SUB-1000 rungs, not the extreme-juice
    rung.

    Returns a diagnostic dict:
    ``{extreme_juice_tagged, ladder_superseded, picks_hidden_total}``.
    """
    picks, ej = apply_extreme_juice_utility(picks)
    picks, ls = apply_ladder_collapse(picks)
    return {
        "extreme_juice_tagged":  ej,
        "ladder_superseded":     ls,
        "picks_hidden_total":    ej + ls,
    }


__all__ = [
    "EXTREME_JUICE_MAX_AMERICAN",
    "apply_extreme_juice_utility",
    "apply_ladder_collapse",
    "apply_board_utility_layer",
]
