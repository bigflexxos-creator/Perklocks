"""Event matcher — builds structured sportsbook deep-link IDs for each pick.

Why this exists:
================
Sportsbook event IDs (FanDuel, DraftKings, BetMGM, Caesars) are PROPRIETARY
and only available via paid partner APIs. The Odds API gives us its OWN
UUIDs but those are NOT compatible with sportsbook deep links.

What this module does:
======================
Build DETERMINISTIC SLUGS from the public data we already have
(sport + home team + away team + commence date) that work with sportsbook
universal-link search redirects.

Example slug: ``nba_lakers_warriors_20260618``

The frontend uses these slugs to build search-redirect URLs like:
``https://sportsbook.fanduel.com/search?q=Lakers+Warriors`` which
FanDuel automatically redirects to the specific game page when there's a
single match (~85 % of cases for major-market games).

How team names are normalised:
==============================
The Odds API returns canonical team names ("Los Angeles Lakers"). We
strip the city/qualifier and keep the team nickname so the slug is
short and stable: "Los Angeles Lakers" → "lakers".

Public API:
===========
* ``enrich_pick_with_event_ids(pick)`` — mutates the pick to add:
    - ``home_team``      (e.g. "Warriors")
    - ``away_team``      (e.g. "Lakers")
    - ``pick``           (human-readable bet description, e.g. "Lakers ML")
    - ``fanduel_event_id``      (slug)
    - ``draftkings_event_id``   (slug — same as FanDuel for now)
    - ``betmgm_event_id``       (slug)
    - ``caesars_event_id``      (slug)
* ``extract_teams(event_str)`` — best-effort parse of stored
  ``event`` field (formats like "Lakers @ Warriors", "Lakers vs Warriors").
* ``match_event_time_proximity(a, b, max_minutes=30)`` — utility for
  matching events against an external directory.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any

# ──────────────────────────────────────────────────────────────────────────
# Team-name normalization
# ──────────────────────────────────────────────────────────────────────────

# Strip these city / qualifier tokens to get the bare team nickname.
# Multi-word cities ("New York", "Los Angeles", "San Francisco", etc.)
# MUST come before single-word cities in the list because we apply them
# in order.
_CITY_PREFIXES_MULTI = [
    "los angeles", "san francisco", "san antonio", "san diego",
    "new york", "new orleans", "new england", "new jersey",
    "kansas city", "oklahoma city", "salt lake", "tampa bay",
    "golden state", "north carolina", "south carolina",
    "saint louis", "st louis", "st. louis",
    "washington dc", "washington d.c.",
    # International soccer prefixes
    "manchester ", "real ", "atletico ", "bayern ", "borussia ",
    "fc ", "ac ", "as ", "ss ", "ssc ",
    "inter ", "olympique ", "paris ", "rb ",
]
_CITY_PREFIXES_SINGLE = [
    "atlanta", "boston", "brooklyn", "charlotte", "chicago", "cleveland",
    "dallas", "denver", "detroit", "houston", "indiana", "memphis", "miami",
    "milwaukee", "minnesota", "orlando", "philadelphia", "phoenix",
    "portland", "sacramento", "toronto", "utah", "washington",
    # MLB extras
    "arizona", "baltimore", "cincinnati", "colorado", "pittsburgh",
    "seattle", "texas", "wisconsin",
    # NFL extras
    "buffalo", "carolina", "jacksonville", "tennessee",
]


def _strip_accents(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def normalize_team(name: str) -> str:
    """Return a short, lowercase, slug-safe team nickname.

    Examples
    --------
    >>> normalize_team("Los Angeles Lakers")
    'lakers'
    >>> normalize_team("Manchester City")
    'city'              # 'manchester ' stripped as a club prefix
    >>> normalize_team("FC Barcelona")
    'barcelona'
    >>> normalize_team("Argentina")        # national team — pass through
    'argentina'
    >>> normalize_team("Erling Braut Haaland")  # player name (props)
    'erling-braut-haaland'
    """
    if not name:
        return ""
    s = _strip_accents(name).lower().strip()
    # Strip multi-word city/club prefixes first
    for pref in _CITY_PREFIXES_MULTI:
        if s.startswith(pref):
            s = s[len(pref):].strip()
            break
    else:
        # Then try single-word city prefixes (only if multi didn't match)
        for pref in _CITY_PREFIXES_SINGLE:
            if s.startswith(pref + " "):
                s = s[len(pref) + 1:].strip()
                break
    # Final slug: keep letters/numbers, hyphenate spaces.
    s = re.sub(r"[^a-z0-9 ]+", "", s).strip()
    s = re.sub(r"\s+", "-", s)
    return s or _strip_accents(name).lower().strip().replace(" ", "-")


# ──────────────────────────────────────────────────────────────────────────
# Event-string parser
# ──────────────────────────────────────────────────────────────────────────

# Picks store the matchup as ``event`` field in formats like:
#   "Lakers @ Warriors"       (away @ home)
#   "Croatia @ England"
#   "Iraq vs France"          (away vs home, less common)
#   "France vs Iraq"          (home vs away)
# Convention used by sports_engine.py is AWAY @ HOME.

_AT_RE = re.compile(r"\s+@\s+")
_VS_RE = re.compile(r"\s+vs\.?\s+", re.IGNORECASE)


def extract_teams(event_str: str) -> tuple[str, str]:
    """Parse a stored ``event`` string into (home_team, away_team).

    Returns empty strings if parse fails.
    """
    if not event_str or not isinstance(event_str, str):
        return "", ""
    s = event_str.strip()
    # Pattern 1: "AWAY @ HOME" (canonical)
    parts = _AT_RE.split(s, maxsplit=1)
    if len(parts) == 2:
        away, home = parts[0].strip(), parts[1].strip()
        return home, away
    # Pattern 2: "HOME vs AWAY"  (sports_engine sometimes uses this for tennis)
    parts = _VS_RE.split(s, maxsplit=1)
    if len(parts) == 2:
        # We can't reliably tell home/away from "vs" — assume HOME first.
        return parts[0].strip(), parts[1].strip()
    return "", ""


# ──────────────────────────────────────────────────────────────────────────
# Slug builders (deterministic across all sportsbooks)
# ──────────────────────────────────────────────────────────────────────────

# Maps our sport label → URL-segment used by sportsbooks.
_SPORT_URL_SEGMENT = {
    "NBA": "nba",
    "MLB": "mlb",
    "NFL": "nfl",
    "NHL": "nhl",
    "Tennis": "tennis",
    "UFC": "mma",
    "Soccer": "soccer",
    "KBO": "baseball",
    "CFL": "cfl",
    "WNBA": "wnba",
}


def _event_date(event_time: Any) -> str:
    """Extract YYYYMMDD from an ISO datetime string (or empty if absent)."""
    if not event_time:
        return ""
    try:
        if isinstance(event_time, str):
            # Common formats: "2026-06-19T19:00:00Z", "2026-06-19T19:00:00+00:00"
            base = event_time.split("T")[0]  # "2026-06-19"
            return base.replace("-", "")
        if isinstance(event_time, datetime):
            return event_time.strftime("%Y%m%d")
    except Exception:
        pass
    return ""


def build_event_slug(sport: str, home_team: str, away_team: str,
                     event_time: Any) -> str:
    """Build a deterministic, sportsbook-compatible event slug.

    Example: build_event_slug("NBA", "Warriors", "Lakers", "2026-06-18T...")
    →        "nba_lakers_warriors_20260618"

    Slug parts: ``{sport_segment}_{away_norm}_{home_norm}_{yyyymmdd}``
    """
    sport_seg = _SPORT_URL_SEGMENT.get(sport, (sport or "sports").lower())
    home_norm = normalize_team(home_team).replace("-", "")
    away_norm = normalize_team(away_team).replace("-", "")
    date_seg = _event_date(event_time)
    parts = [sport_seg, away_norm, home_norm, date_seg]
    return "_".join(p for p in parts if p)


# ──────────────────────────────────────────────────────────────────────────
# Pick → human-readable "pick" string
# ──────────────────────────────────────────────────────────────────────────

def _format_pick_label(pick: dict) -> str:
    """Human-readable label for the bet itself.

    Pulls from existing fields:
      • ``market``    e.g. "Lakers Moneyline", "Anytime Goal Scorer"
      • ``selection`` e.g. "Yes", "Over 8.5"
    """
    market = (pick.get("market") or "").strip()
    selection = (pick.get("selection") or "").strip()
    # Common case: market already encodes the side ("Lakers Moneyline",
    # "Harry Kane Anytime Goal Scorer") — selection is just "Yes".
    if selection and selection.lower() not in ("yes", "no", "n/a", ""):
        # Selection adds info (e.g. "Over 8.5")
        if selection.lower() not in market.lower():
            return f"{market} {selection}".strip()
    return market


# ──────────────────────────────────────────────────────────────────────────
# Time-proximity matcher (utility for external directory matching)
# ──────────────────────────────────────────────────────────────────────────

def match_event_time_proximity(a_iso: str, b_iso: str,
                              max_minutes: int = 30) -> bool:
    """Return True if two ISO times are within ``max_minutes`` of each other.

    Used when matching our picks against a fetched FanDuel / DraftKings
    event directory (when partner API access becomes available).
    """
    try:
        a = datetime.fromisoformat(a_iso.replace("Z", "+00:00"))
        b = datetime.fromisoformat(b_iso.replace("Z", "+00:00"))
        if a.tzinfo is None:
            a = a.replace(tzinfo=timezone.utc)
        if b.tzinfo is None:
            b = b.replace(tzinfo=timezone.utc)
        return abs((a - b).total_seconds()) <= max_minutes * 60
    except Exception:
        return False


# ──────────────────────────────────────────────────────────────────────────
# PUBLIC: enrich a pick with sportsbook deep-link fields
# ──────────────────────────────────────────────────────────────────────────

def enrich_pick_with_event_ids(pick: dict) -> dict:
    """Mutate ``pick`` to add structured deep-link fields.

    Fields added (idempotent — re-running is safe):
      • ``home_team``           short team name (e.g. "Warriors")
      • ``away_team``           short team name (e.g. "Lakers")
      • ``pick``                human-readable bet description
      • ``fanduel_event_id``    deterministic slug
      • ``draftkings_event_id`` deterministic slug
      • ``betmgm_event_id``     deterministic slug
      • ``caesars_event_id``    deterministic slug

    The same slug is reused across all four books — they all consume it via
    universal-link search redirects, which auto-resolve to the matching
    game page for major-market events.
    """
    sport = pick.get("sport") or ""
    event_time = pick.get("event_time") or pick.get("commence_time") or ""
    home_raw = pick.get("home_team") or ""
    away_raw = pick.get("away_team") or ""

    # Parse from event string if not already populated
    if not home_raw or not away_raw:
        home_raw, away_raw = extract_teams(pick.get("event") or "")

    pick["home_team"] = home_raw
    pick["away_team"] = away_raw
    pick["pick"] = _format_pick_label(pick)

    slug = build_event_slug(sport, home_raw, away_raw, event_time)
    if slug:
        pick["fanduel_event_id"] = slug
        pick["draftkings_event_id"] = slug
        pick["betmgm_event_id"] = slug
        pick["caesars_event_id"] = slug
    return pick


def enrich_picks_with_event_ids(picks: list[dict]) -> list[dict]:
    """Apply :func:`enrich_pick_with_event_ids` over a list."""
    for p in picks:
        enrich_pick_with_event_ids(p)
    return picks
