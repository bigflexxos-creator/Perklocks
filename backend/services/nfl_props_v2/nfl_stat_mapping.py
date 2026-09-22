"""Canonical NFL sportsbook-market → historical-actuals-key mapping.

One authoritative registry consumed by the distribution builder.  Adding a
market later is a data-only edit.

Sportsbook market strings observed on the current PerkLocks board look like:
  "Matthew Stafford Over 261.5 Player Pass Yds - Alternative"
  "Kyren Williams Over 12.5 Player Reception Yds"
  "Bijan Robinson Over 75.5 Player Rush Yds"
  "Malik Nabers Over 50.5 Player Reception Yds"

The mapping resolves those free-form strings onto the compact keys used by
``player_game_actuals.actuals`` — which are:

  pass_yds, pass_tds, interceptions, completions, attempts,
  rush_yds, rush_attempts, rush_tds,
  rec_yds, receptions, rec_tds, targets.
"""
from __future__ import annotations

from typing import Optional


# canonical key → list of (substr) admission patterns (case-insensitive
# substring match on the market string).  Order matters — first hit wins.
_MARKET_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("pass_yds",       ("pass yds", "pass yards", "passing yards")),
    ("pass_tds",       ("pass tds", "pass tds", "passing tds", "pass touchdowns")),
    ("attempts",       ("pass attempts", "passing attempts", "pass att")),
    ("completions",    ("pass completions", "passing completions", "completions")),
    ("interceptions",  ("interception", "int thrown", "picks thrown")),
    ("rush_yds",       ("rush yds", "rush yards", "rushing yards")),
    ("rush_attempts",  ("rush attempts", "carries", "rushing attempts")),
    ("rush_tds",       ("rush tds", "rushing tds", "rush touchdowns")),
    ("rec_yds",        ("reception yds", "receiving yards", "rec yards", "rec yds")),
    ("receptions",     ("receptions", "player reception", "catches")),
    ("rec_tds",        ("receiving tds", "rec tds", "receiving touchdowns")),
    ("targets",        ("targets",)),
    # Anytime TD combines rushing + receiving TDs.  Handled specially in
    # the distribution builder (sum of rush_tds + rec_tds).
    ("anytime_td",     ("anytime td", "anytime touchdown", "atd")),
]


ACTUALS_STAT_KEYS = {
    "pass_yds", "pass_tds", "attempts", "completions", "interceptions",
    "rush_yds", "rush_attempts", "rush_tds",
    "rec_yds", "receptions", "rec_tds", "targets",
}


def resolve_market_to_stat(market_string: str) -> Optional[str]:
    """Map a free-form sportsbook market string onto a canonical stat key.

    Returns None when no pattern matches — callers should skip that
    market (never fabricate an actuals key).
    """
    if not market_string:
        return None
    s = market_string.strip().lower()
    for canonical, patterns in _MARKET_PATTERNS:
        for pat in patterns:
            if pat in s:
                return canonical
    return None


def actuals_value(actuals: dict, market_key: str) -> Optional[float]:
    """Return the numeric value for ``market_key`` from an ``actuals``
    dict.  Handles the ``anytime_td`` composite as rush_tds + rec_tds.

    Returns ``None`` when the required underlying key is absent.  Zero
    is a valid value and is returned as ``0.0``.
    """
    if not isinstance(actuals, dict):
        return None
    if market_key == "anytime_td":
        rt = actuals.get("rush_tds")
        rr = actuals.get("rec_tds")
        if rt is None and rr is None:
            return None
        try:
            return float(rt or 0) + float(rr or 0)
        except (TypeError, ValueError):
            return None
    v = actuals.get(market_key)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


__all__ = ["resolve_market_to_stat", "actuals_value", "ACTUALS_STAT_KEYS"]
