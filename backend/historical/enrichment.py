"""Lock Engine ↔ Historical Engine bridge.

`enrich_pick_with_form(pick)` is the single entry point used by the
learning_system_v2 per-pick loop. It:

  1. Detects whether the pick is a player-prop (skips team markets).
  2. Pulls the player's historical form from MongoDB via lookup.py.
  3. Adds the form payload to the pick (for UI transparency).
  4. Applies a SOFT ±1.5 nudge to lock_score based on hot/cold + consistency.

IMPORTANT — per user spec ("don't remove elite players"):
  • This runs ALONGSIDE `elite_players.py` / `auto_elite.py`, never replacing
    them. Marquee scorers still get their hardcoded 99 lock.
  • Hot/cold nudge is capped at ±1.5 so it can't single-handedly demote a
    pick out of the 85+ Lock tier or push a pick over 99.
  • If form data is missing (player not yet backfilled), pick is unchanged.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("lockscore.historical.enrichment")


# Player-prop markets (case-insensitive substrings). If the pick's market
# string contains ANY of these, we treat it as a player-prop and attempt
# a form lookup.
_PROP_MARKETS = (
    # MLB
    "hit", "home run", "rbi", "total base", "strikeout", "pitcher outs",
    "stolen base", "single", "double", "triple", "walk",
    # NBA
    "point", "rebound", "assist", "three", "3pt", "steal", "block",
    # NFL
    "pass yard", "rush yard", "rec yard", "passing", "rushing", "receiving",
    "touchdown", "td", "reception", "completion",
    # NHL
    "shots on goal", "save", "goal",
    # Soccer
    "goal scorer", "score or assist", "first goal", "shots on target",
)


_BOOST_HOT = 1.5
_PENALTY_COLD = -1.5
_MIN_GAMES_FOR_SIGNAL = 3      # need at least 3 logged games to react
_CONSISTENCY_HOT = 0.70
_CONSISTENCY_COLD = 0.30


def _is_player_prop(pick: dict) -> bool:
    market = (pick.get("market") or "").lower()
    return any(tok in market for tok in _PROP_MARKETS)


def _player_name(pick: dict) -> Optional[str]:
    """Best-effort player name extraction.

    For player props, `pick.selection` is the player (set by sports_engine
    _build_pick `pick_side=player`). For other markets (moneyline, totals),
    `selection` is a team or "Over"/"Under" → return None.
    """
    sel = (pick.get("selection") or "").strip()
    if not sel:
        return None
    s_low = sel.lower()
    # Skip non-player selections.
    if s_low in ("over", "under", "yes", "no", "draw", "tie"):
        return None
    # Skip if selection looks like a team / spread / line.
    if any(c in sel for c in ("+", "-")) and any(ch.isdigit() for ch in sel):
        return None
    # Strip team abbr suffix that sports_engine sometimes appends e.g.
    # "Aaron Judge (NYY)" → "Aaron Judge".
    if "(" in sel and sel.endswith(")"):
        sel = sel.split("(")[0].strip()
    # Must have at least two words to look like a real player name.
    if len(sel.split()) < 2:
        return None
    return sel


def _sport_key(sport: str) -> Optional[str]:
    s = (sport or "").lower()
    if s == "mlb":
        return "mlb"
    if s in ("soccer", "football"):
        return "soccer"
    if s == "nba":
        return "nba"
    if s == "nfl":
        return "nfl"
    if s == "nhl":
        return "nhl"
    return None


async def enrich_pick_with_form(pick: dict) -> None:
    """Mutates `pick` in place. No-op if data is missing.

    Adds:
      • `player_form`         – compact form summary (UI-friendly)
      • `historical_signal`   – {"label": "hot"|"cold"|"steady", "delta": float}

    Modifies (capped):
      • `lock_score`          – ±1.5 nudge from baseline
    """
    if not _is_player_prop(pick):
        return
    sport = _sport_key(pick.get("sport") or "")
    if not sport:
        return
    name = _player_name(pick)
    if not name:
        return

    try:
        from .lookup import get_player_form
        form = await get_player_form(sport, name, market_hint=pick.get("market"))
    except Exception as e:
        logger.debug("form lookup failed for %s/%s: %s", sport, name, e)
        return
    if not form:
        return

    pick["player_form"] = form
    games = int(form.get("games_logged") or 0)
    if games < _MIN_GAMES_FOR_SIGNAL:
        # Not enough sample yet — surface the data but don't move the score.
        pick["historical_signal"] = {"label": "insufficient", "delta": 0.0,
                                      "games": games}
        return

    trend = form.get("trend") or "steady"
    consistency = float(form.get("consistency") or 0.0)
    delta = 0.0
    label = "steady"
    if trend == "hot" and consistency >= _CONSISTENCY_HOT:
        delta = _BOOST_HOT
        label = "hot"
    elif trend == "cold" and consistency <= _CONSISTENCY_COLD:
        delta = _PENALTY_COLD
        label = "cold"

    if delta != 0.0:
        cur = float(pick.get("lock_score") or 0)
        # Clamp 0..99 — Lock Score band per spec.
        new_score = round(max(0.0, min(99.0, cur + delta)), 1)
        pick["lock_score"] = new_score
        pick["historical_signal_applied"] = True

    pick["historical_signal"] = {
        "label": label,
        "delta": delta,
        "games": games,
        "consistency": consistency,
    }
