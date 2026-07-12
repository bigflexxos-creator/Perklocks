"""MLB Deep Signals — Signal Engine Phase B.1.

Adds MLB-specific evidence layers to hitter and pitcher prop signals
WITHOUT making any external API calls at request time. Public data is
baked in as lookup tables so the enrichment is deterministic and
adds ~0ms latency to /picks/today.

What we add per MLB pick:
  • `park_hr_factor`      — 100 = league average, >105 = HR-boosting,
                             <95 = HR-suppressing (Coors 118, Petco 92, etc.)
  • `park_hits_factor`    — same scale for base hits
  • `park_run_factor`     — total-runs multiplier
  • `is_hitter_friendly`  — bool derived from HR/hits/runs consensus
  • `is_pitcher_friendly` — bool (inverse of above)
  • `market_family`       — inferred from market string (hr / hits / k / etc.)

Everything else (weather, umpire tendency, platoon splits) can be
layered on later without breaking existing consumers — this module is
strictly additive.

Data source (public, updated seasonally):
  • Baseball Savant park factors
    https://baseballsavant.mlb.com/leaderboard/statcast-park-factors
  • ESPN park factor reports (cross-check)

Version note (2026-07): 3-year rolling park factors, hitter-side.
Refresh cadence: hand-updated once per season — the underlying park
dimensions don't change unless a team moves stadiums.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger("lockscore.services.signal_engine.mlb_deep")


# ──────────────────────────────────────────────────────────────────────
# Park factors (2023-2025 rolling, hitter-side, HR / hits / runs)
# 100 = league average. Higher = more offense.
# Source: Baseball Savant Statcast park factors, cross-checked with ESPN.
# ──────────────────────────────────────────────────────────────────────
_PARK_FACTORS: dict[str, dict[str, int]] = {
    # ── Extreme hitter havens ──
    "Colorado Rockies":       {"hr": 112, "hits": 114, "runs": 118},
    "Cincinnati Reds":        {"hr": 116, "hits": 103, "runs": 108},
    "Boston Red Sox":         {"hr": 100, "hits": 108, "runs": 108},
    # ── Above average ──
    "Baltimore Orioles":      {"hr": 108, "hits": 101, "runs": 103},
    "Texas Rangers":          {"hr": 106, "hits": 102, "runs": 104},
    "Philadelphia Phillies":  {"hr": 108, "hits":  99, "runs": 103},
    "Atlanta Braves":         {"hr": 105, "hits": 101, "runs": 103},
    "Chicago Cubs":           {"hr": 103, "hits": 103, "runs": 102},
    "Toronto Blue Jays":      {"hr": 106, "hits":  99, "runs": 102},
    "Kansas City Royals":     {"hr":  96, "hits": 108, "runs": 102},
    # ── Neutral ──
    "Milwaukee Brewers":      {"hr": 102, "hits":  99, "runs": 100},
    "St. Louis Cardinals":    {"hr":  98, "hits": 102, "runs": 100},
    "Arizona Diamondbacks":   {"hr": 101, "hits":  99, "runs": 100},
    "Chicago White Sox":      {"hr": 102, "hits":  98, "runs":  99},
    "Houston Astros":         {"hr": 101, "hits":  99, "runs":  99},
    "Washington Nationals":   {"hr":  99, "hits": 100, "runs":  99},
    "New York Yankees":       {"hr": 108, "hits":  95, "runs":  99},
    "Minnesota Twins":        {"hr":  98, "hits":  99, "runs":  98},
    "Los Angeles Angels":     {"hr":  99, "hits":  97, "runs":  98},
    "Cleveland Guardians":    {"hr":  96, "hits":  98, "runs":  97},
    "Athletics":              {"hr":  96, "hits":  99, "runs":  97},  # Sacramento 2025+
    "Oakland Athletics":      {"hr":  92, "hits":  98, "runs":  95},  # historical
    # ── Below average ──
    "New York Mets":          {"hr":  94, "hits":  99, "runs":  96},
    "Los Angeles Dodgers":    {"hr":  96, "hits":  97, "runs":  95},
    "Detroit Tigers":         {"hr":  93, "hits":  98, "runs":  94},
    "Tampa Bay Rays":         {"hr":  92, "hits": 100, "runs":  95},
    "Pittsburgh Pirates":     {"hr":  92, "hits":  99, "runs":  94},
    "Seattle Mariners":       {"hr":  90, "hits":  97, "runs":  92},
    "San Francisco Giants":   {"hr":  90, "hits":  96, "runs":  92},
    "Miami Marlins":          {"hr":  88, "hits":  98, "runs":  91},
    # ── Extreme pitcher havens ──
    "San Diego Padres":       {"hr":  87, "hits":  95, "runs":  90},
}


# Map common team-name variants to canonical park-factor key.
_TEAM_ALIASES = {
    "d-backs": "Arizona Diamondbacks",
    "diamondbacks": "Arizona Diamondbacks",
    "dodgers": "Los Angeles Dodgers",
    "yankees": "New York Yankees",
    "mets": "New York Mets",
    "red sox": "Boston Red Sox",
    "cubs": "Chicago Cubs",
    "white sox": "Chicago White Sox",
    "giants": "San Francisco Giants",
    "padres": "San Diego Padres",
    "orioles": "Baltimore Orioles",
    "rockies": "Colorado Rockies",
    "rays": "Tampa Bay Rays",
    "phillies": "Philadelphia Phillies",
    "braves": "Atlanta Braves",
    "mariners": "Seattle Mariners",
    "twins": "Minnesota Twins",
    "royals": "Kansas City Royals",
    "brewers": "Milwaukee Brewers",
    "reds": "Cincinnati Reds",
    "pirates": "Pittsburgh Pirates",
    "cardinals": "St. Louis Cardinals",
    "guardians": "Cleveland Guardians",
    "tigers": "Detroit Tigers",
    "marlins": "Miami Marlins",
    "nationals": "Washington Nationals",
    "angels": "Los Angeles Angels",
    "astros": "Houston Astros",
    "rangers": "Texas Rangers",
    "blue jays": "Toronto Blue Jays",
    "athletics": "Athletics",
    "a's": "Athletics",
}


def _canonical_home_team(team: str) -> Optional[str]:
    """Normalise home-team string → key into _PARK_FACTORS."""
    if not team:
        return None
    t = team.strip()
    if t in _PARK_FACTORS:
        return t
    tl = t.lower()
    if tl in _TEAM_ALIASES:
        return _TEAM_ALIASES[tl]
    for alias, canonical in _TEAM_ALIASES.items():
        if alias in tl:
            return canonical
    for full in _PARK_FACTORS:
        if full.lower() in tl or tl in full.lower():
            return full
    return None


# ──────────────────────────────────────────────────────────────────────
# Market family classification (hitter vs pitcher vs game)
# ──────────────────────────────────────────────────────────────────────
_MARKET_FAMILIES: list[tuple[str, str]] = [
    # Compound markets FIRST — otherwise a substring like "RBIs" fires
    # before the "Hits+Runs+RBIs" HRR pattern gets a chance to match.
    (r"\bhits?\s*\+\s*runs?\s*\+\s*rbis?\b",   "hrr"),
    (r"home\s*run|\bhr\b|dinger",              "hr"),
    (r"total\s*bases|tot\s*bases|\btb\b",      "total_bases"),
    (r"rbis?|runs?\s*batted",                  "rbi"),
    (r"\bhits?\b",                             "hits"),
    (r"strikeouts?|\bks?\b",                   "pitcher_k"),
    (r"outs?\s*recorded|innings?\s*pitched",   "pitcher_ip"),
    (r"earned\s*runs?|\bera\b",                "pitcher_er"),
    (r"nrfi|yrfi|first\s*inning",              "nrfi"),
    (r"total\s*runs?|game\s*total|over\s*\d",  "game_total"),
]


def _market_family(market: str) -> Optional[str]:
    """Return canonical market-family token or None."""
    if not market:
        return None
    m = market.lower()
    for pat, family in _MARKET_FAMILIES:
        if re.search(pat, m):
            return family
    return None


# ──────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────
def _extract_home_team(pick: dict) -> Optional[str]:
    """Resolve the home team from a pick, falling back to parsing the
    event string "Away @ Home" when the explicit field is missing.
    Older MLB pick documents don't carry `home_team`/`away_team`."""
    home = pick.get("home_team")
    if isinstance(home, str) and home.strip():
        return home.strip()
    event = pick.get("event")
    if isinstance(event, str) and " @ " in event:
        # "Away @ Home" — home team comes after the " @ ".
        parts = event.split(" @ ", 1)
        if len(parts) == 2 and parts[1].strip():
            return parts[1].strip()
    return None


def enrich_mlb_pick(pick: dict) -> dict:
    """Add `mlb_deep` block to a single MLB pick. Idempotent. Returns
    the pick (mutated). No-op if the pick is not MLB or the home team
    is not in the park-factor table.

    Attaches:
      pick['mlb_deep'] = {
        'park_hr_factor':      int,   # 100 = neutral
        'park_hits_factor':    int,
        'park_run_factor':     int,
        'park_name':           str,   # canonical home-team name
        'is_hitter_friendly':  bool,
        'is_pitcher_friendly': bool,
        'market_family':       str,   # hr / hits / pitcher_k / ...
      }
    """
    if (pick.get("sport") or "").upper() != "MLB":
        return pick
    home = _canonical_home_team(_extract_home_team(pick) or "")
    if not home:
        return pick
    pf = _PARK_FACTORS.get(home)
    if not pf:
        return pick
    family = _market_family(pick.get("market") or "")
    # Overall "hitter-friendly" if HR and hits BOTH lean above average
    # (or HR alone is +6 above neutral — Coors / Baltimore effect).
    hit_score = (pf["hr"] + pf["hits"] + pf["runs"]) / 3.0
    is_hitter_friendly = hit_score >= 103 or pf["hr"] >= 106
    is_pitcher_friendly = hit_score <= 97 or pf["hr"] <= 92
    pick["mlb_deep"] = {
        "park_hr_factor":      pf["hr"],
        "park_hits_factor":    pf["hits"],
        "park_run_factor":     pf["runs"],
        "park_name":           home,
        "is_hitter_friendly":  is_hitter_friendly,
        "is_pitcher_friendly": is_pitcher_friendly,
        "market_family":       family,
    }
    return pick


def enrich_mlb_picks_bulk(picks: list[dict]) -> int:
    """Bulk variant. Returns the number of picks enriched."""
    if not picks:
        return 0
    enriched = 0
    for p in picks:
        try:
            before = "mlb_deep" in p
            enrich_mlb_pick(p)
            if "mlb_deep" in p and not before:
                enriched += 1
        except Exception as e:
            logger.debug("mlb_deep enrich failed for %s: %s", p.get("id"), e)
    return enriched
