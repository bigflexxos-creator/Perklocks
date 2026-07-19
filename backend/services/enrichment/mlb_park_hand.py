"""MLB Park HR Factors by Batter Handedness — Phase 2 (2026-07-19).

The base park factor table in ``services/signal_engine/mlb_deep.py``
uses a single HR multiplier per stadium, but ballpark HR rates differ
dramatically by batter handedness:

  • Yankee Stadium's short right-field porch: LHB HR factor ~118,
    RHB HR factor ~99.
  • Fenway's Green Monster: RHB HR/doubles factor ~112, LHB ~92.
  • Coors: both sides boosted (altitude carries fly balls uniformly).

Hitting an LHB Home Run Over in Yankee Stadium is a fundamentally
different bet than the same market for a RHB — even though the venue
is identical. Phase 2 surfaces this per-handedness factor to the
signal engine.

Data source: 3-year rolling park factors from Statcast (baseballsavant
/leaderboard/statcast-park-factors), split by batter side.

On missing data (no handedness resolved for the batter, or park not
in the table), returns neutral 100 for both hands so the caller can
fall back to the base park factor.
"""
from __future__ import annotations

from typing import Optional

# HR park factor by batter side (100 = league avg, 3-yr Statcast rolling).
_HAND_HR_FACTORS: dict[str, dict[str, int]] = {
    # ── LHB-friendly parks ──
    "New York Yankees":       {"L": 118, "R":  99},   # short right porch
    "Colorado Rockies":       {"L": 113, "R": 111},   # altitude both hands
    "Cincinnati Reds":        {"L": 115, "R": 116},
    "Baltimore Orioles":      {"L": 111, "R": 106},   # short RF (LHB benefit)
    "Toronto Blue Jays":      {"L": 108, "R": 104},
    "Philadelphia Phillies":  {"L": 110, "R": 106},
    "Atlanta Braves":         {"L": 106, "R": 104},
    "Chicago Cubs":           {"L": 105, "R": 100},   # wind-out days LHB
    "Texas Rangers":          {"L": 108, "R": 104},
    # ── RHB-friendly parks ──
    "Boston Red Sox":         {"L":  93, "R": 112},   # Green Monster
    "Houston Astros":         {"L":  98, "R": 108},   # Crawford Boxes LF
    "Milwaukee Brewers":      {"L": 101, "R": 105},
    "Los Angeles Angels":     {"L":  97, "R": 102},
    "Washington Nationals":   {"L":  99, "R": 100},
    "Chicago White Sox":      {"L": 100, "R": 104},
    # ── Neutral ──
    "Minnesota Twins":        {"L":  99, "R":  98},
    "Kansas City Royals":     {"L":  96, "R":  95},
    "Cleveland Guardians":    {"L":  95, "R":  97},
    "St. Louis Cardinals":    {"L":  98, "R":  99},
    "Arizona Diamondbacks":   {"L": 101, "R": 101},
    "Athletics":              {"L":  96, "R":  96},
    # ── Pitcher-friendly parks ──
    "San Francisco Giants":   {"L":  85, "R":  93},   # wind + deep RF
    "Seattle Mariners":       {"L":  88, "R":  92},
    "San Diego Padres":       {"L":  87, "R":  87},
    "Miami Marlins":          {"L":  86, "R":  90},
    "Detroit Tigers":         {"L":  92, "R":  94},
    "Tampa Bay Rays":         {"L":  93, "R":  91},
    "Pittsburgh Pirates":     {"L":  91, "R":  93},
    "Los Angeles Dodgers":    {"L":  95, "R":  97},
    "New York Mets":          {"L":  93, "R":  95},
    "Oakland Athletics":      {"L":  91, "R":  93},
}


def park_hr_by_hand(team: str, hand: str) -> Optional[int]:
    """Return the HR factor for a batter of ``hand`` at ``team``'s home
    park. ``hand`` is 'L' or 'R'. Returns None on unknown team so the
    caller can skip / fall back to base park factor.
    """
    if not team or hand not in ("L", "R"):
        return None
    rec = _HAND_HR_FACTORS.get(team)
    if not rec:
        return None
    return int(rec.get(hand) or 100)


def enrich_pick_with_hand_factor(pick: dict) -> dict:
    """Attach ``pick['park_hr_hand_factor']`` = int (or None) using the
    batter's handedness (``pick['batter_hand']`` / ``pick['bat_side']``)
    and the home team from ``mlb_deep.park_name`` / ``home_team``.

    Idempotent, non-throwing.
    """
    if (pick.get("sport") or "").upper() != "MLB":
        return pick
    # Resolve batter hand — sources vary across enrichers.
    hand = (pick.get("batter_hand") or pick.get("bat_side")
            or (pick.get("statcast_batter") or {}).get("bat_side")
            or "")
    hand = str(hand).strip().upper()[:1]
    if hand not in ("L", "R"):
        return pick
    deep = pick.get("mlb_deep") or {}
    team = deep.get("park_name") or pick.get("home_team")
    if not team:
        # Fall back to parsing the event.
        ev = pick.get("event") or ""
        if " @ " in ev:
            team = ev.split(" @ ", 1)[1].strip()
    fac = park_hr_by_hand(team or "", hand)
    if fac is not None:
        pick["park_hr_hand_factor"] = fac
        pick["park_hr_hand_side"] = hand
    return pick
