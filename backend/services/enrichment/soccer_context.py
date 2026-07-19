"""Soccer Context — Set-piece specialists, Manager tactics, High-pressure
fixture heuristics.  Phase 2 (2026-07-19).

Three lightweight (static-table + heuristic) signal boosters that
don't require any live API calls:

  1. Set-piece specialists (``set_piece_bonus``)
     Reference table of confirmed penalty takers, direct free-kick
     specialists, and dead-ball corner threats. A striker who's the
     primary penalty taker AND on set-piece duty gets +2 signal on
     Anytime Goalscorer markets — extra scoring paths on top of
     open-play xG.

  2. Manager tactics (``manager_style``)
     High-tempo/attacking managers (Klopp, De Zerbi, Amorim,
     Bielsa, Enzo Maresca (pre-2025)) produce more shots + more xG.
     Ultra-defensive managers (Simeone, Mourinho late-career,
     Ancelotti when protecting a lead) suppress totals.
     Small ±0.5 goal adjustment on team totals.

  3. High-pressure fixture (``high_pressure_context``)
     Cup finals, relegation 6-pointers, top-of-table battles → more
     variance, more penalties, more red cards. Not a directional
     signal but a variance flag for the correlation guard.

All three are additive to the existing ``soccer_deep`` block and
return 0 points when data is missing.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("lockscore.soccer_context")


# ── 1. Set-piece specialists (mid-2026 rosters) ─────────────────────
# Key = normalised player name (lowercase, no diacritics stripped).
# Value = list of set-piece duties: 'PK' (penalty), 'FK' (direct FK),
# 'CK' (corner kicks — for header goal probability).
_SET_PIECE_TAKERS: dict[str, list[str]] = {
    # Elite scorers with penalty + FK duty
    "harry kane":         ["PK", "FK"],
    "kylian mbappé":      ["PK", "FK"],
    "kylian mbappe":      ["PK", "FK"],
    "cristiano ronaldo":  ["PK", "FK"],
    "lionel messi":       ["PK", "FK"],
    "erling haaland":     ["PK"],
    "robert lewandowski": ["PK"],
    "mohamed salah":      ["PK"],
    "bruno fernandes":    ["PK", "FK"],
    "jamal musiala":      ["FK"],
    "trent alexander-arnold": ["FK", "CK"],
    "kevin de bruyne":    ["FK", "CK"],
    "james ward-prowse":  ["FK", "CK"],
    "lorenzo pellegrini": ["PK", "FK"],
    "paulo dybala":       ["FK"],
    "rodrigo de paul":    ["FK"],
    "martin ødegaard":    ["CK", "FK"],
    "martin odegaard":    ["CK", "FK"],
    "declan rice":        ["CK"],
    "bukayo saka":        ["PK", "CK"],
    "phil foden":         ["CK"],
    "cole palmer":        ["PK", "FK"],
    "vinicius jr":        ["FK"],
    "vinicius junior":    ["FK"],
    "jude bellingham":    ["FK"],
    "federico valverde":  ["FK"],
    "julian alvarez":     ["FK"],
    "lautaro martinez":   ["PK"],
    "nicolò barella":     ["CK"],
    "nicolo barella":     ["CK"],
    "khvicha kvaratskhelia": ["FK", "CK"],
    "rafael leao":        ["FK"],
    "rafael leão":        ["FK"],
    "heung-min son":      ["FK"],
    "son heung-min":      ["FK"],
    "marcus rashford":    ["FK"],
    "mason mount":        ["CK"],
    "kai havertz":        ["PK"],
    "gabriel jesus":      ["PK"],
    "darwin nunez":       ["PK"],
    "alexis mac allister": ["PK"],
    "exequiel palacios":  ["PK", "FK"],
    "riccardo orsolini":  ["FK", "CK"],
}


def _norm_name(name: str) -> str:
    return (name or "").strip().lower()


def set_piece_duties(player: str) -> list[str]:
    return _SET_PIECE_TAKERS.get(_norm_name(player)) or []


# ── 2. Manager tactical fingerprint ──────────────────────────────────
# 'attacking' = high pressing + high xG-For, boosts team-total Overs
# 'defensive' = low block + pragmatic, boosts Unders
# 'balanced' = neutral (default when unknown)
_MANAGER_STYLE: dict[str, str] = {
    # Attacking (+overs bias)
    "jürgen klopp":       "attacking",
    "jurgen klopp":       "attacking",
    "pep guardiola":      "attacking",
    "mikel arteta":       "attacking",
    "arne slot":          "attacking",
    "xabi alonso":        "attacking",
    "roberto de zerbi":   "attacking",
    "marcelo bielsa":     "attacking",
    "ruben amorim":       "attacking",
    "rúben amorim":       "attacking",
    "vincent kompany":    "attacking",
    "luciano spalletti":  "attacking",
    "gian piero gasperini": "attacking",
    "antonio conte":      "attacking",
    "julen lopetegui":    "attacking",
    "unai emery":         "balanced",
    "thomas frank":       "balanced",
    "eddie howe":         "balanced",
    # Defensive (+unders bias)
    "diego simeone":      "defensive",
    "jose mourinho":      "defensive",
    "josé mourinho":      "defensive",
    "sean dyche":         "defensive",
    "nuno espirito santo": "defensive",
    "nuno espírito santo": "defensive",
    "carlo ancelotti":    "balanced",   # switches by fixture
    "stefano pioli":      "balanced",
    "simone inzaghi":     "balanced",
    "massimiliano allegri": "defensive",
    "gary o'neil":        "defensive",
    "marco rose":         "balanced",
    "julian nagelsmann":  "attacking",
}


def manager_style(manager: str) -> str:
    return _MANAGER_STYLE.get(_norm_name(manager)) or "balanced"


# ── 3. High-pressure fixture detector ────────────────────────────────
# Reads league + round context from the pick document. Returns
# ('high' | 'medium' | 'normal', reason:str).
#
# Rules (heuristic — no live table scrape):
#   - Cup final / semifinal / knockout round tags → high
#   - Derby markers ("El Clásico", "Merseyside", "Manchester", etc.) → high
#   - Late-season (Apr-May) league fixture where at least one side is
#     in the relegation zone (via factors['relegation_flag']) → high
#   - Late-season title race (top-4 both sides) → high
_KNOCKOUT_TAGS = {
    "final", "semi-final", "semifinal", "quarter-final",
    "quarterfinal", "round of 16", "last 16", "knockout",
    "playoff", "play-off", "relegation playoff", "promotion final",
}
_DERBY_TOKENS = (
    "el clasico", "clásico", "clasico", "derby", "merseyside",
    "north london", "manchester derby", "north west derby",
    "milan derby", "derby della madonnina", "rome derby",
    "superclásico", "superclasico", "le classique", "old firm",
)


def high_pressure_context(pick: dict) -> tuple[str, str]:
    """Classify the fixture stakes. Non-throwing."""
    if (pick.get("sport") or "").lower() != "soccer":
        return "normal", ""
    round_str = (pick.get("round") or pick.get("stage") or "").lower()
    for tag in _KNOCKOUT_TAGS:
        if tag in round_str:
            return "high", f"{round_str} — knockout stakes"
    event = (pick.get("event") or "").lower()
    league = (pick.get("league") or "").lower()
    combined = f"{event} {league}"
    for tok in _DERBY_TOKENS:
        if tok in combined:
            return "high", f"Rivalry fixture ({tok})"
    factors = pick.get("factors") or {}
    if isinstance(factors, dict):
        if factors.get("relegation_flag") or factors.get("title_race_flag"):
            return "high", "Stakes-heavy league position"
    return "normal", ""


def enrich_pick_with_context(pick: dict) -> dict:
    """Attach ``pick['soccer_context']`` = {
        set_piece_duties: list[str],   # ['PK','FK'] etc
        manager_style_home: str,
        manager_style_away: str,
        pressure: 'high' | 'normal',
        pressure_reason: str,
    }
    Idempotent, non-throwing.
    """
    if (pick.get("sport") or "").lower() != "soccer":
        return pick
    if pick.get("soccer_context"):
        return pick

    # Player set-piece duties (for goalscorer markets)
    player_name = pick.get("player_name") or pick.get("selection") or ""
    duties = set_piece_duties(str(player_name))

    # Manager styles — home/away picked up from pick fields when present.
    style_home = manager_style(pick.get("home_manager") or "")
    style_away = manager_style(pick.get("away_manager") or "")

    pressure, reason = high_pressure_context(pick)

    if not duties and style_home == "balanced" and style_away == "balanced" and pressure == "normal":
        # Nothing interesting — skip attaching an empty block.
        return pick

    pick["soccer_context"] = {
        "set_piece_duties":   duties,
        "manager_style_home": style_home,
        "manager_style_away": style_away,
        "pressure":           pressure,
        "pressure_reason":    reason,
    }
    return pick
