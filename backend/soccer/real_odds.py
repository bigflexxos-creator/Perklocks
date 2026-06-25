"""Real-odds resolver for the soccer pipeline.

Bridges the gap between our football-data.org-backed model (which knows
fixtures + team form) and The Odds API (which knows what real
sportsbooks are quoting). For each prediction, looks up the matching
event in The Odds API and returns FanDuel / DraftKings / BetMGM lines.

When a real line is available, the pipeline uses it instead of the
synthetic "Fair Odds (Model)" estimate — so users see -909 (FanDuel's
actual quote for Netherlands @ Tunisia) instead of -2400 (a fabricated
number derived from the model's own 96% probability).

Falls back gracefully:
  • The Odds API key missing → skip, return empty cache
  • Sport key not active → skip
  • Fixture not in API → leave prediction as fair-odds-model
  • Team names don't match → leave prediction as fair-odds-model

This module deliberately stays SYNC-friendly internally and exposes
one async resolver. We cache the full event list per sport_key for the
pipeline run so we hit The Odds API at most ~10 times per refresh
(one per soccer competition).
"""
from __future__ import annotations

import logging
import os
import re
import unicodedata
from typing import Optional

import httpx

logger = logging.getLogger("lockscore.soccer.real_odds")

_ODDS_API_BASE = "https://api.the-odds-api.com/v4"

# football-data.org competition codes → The Odds API sport_keys.
# Add to this map as we expand coverage. Anything not in this map
# falls through to the synthetic fair-odds path.
_FD_TO_ODDS_KEY = {
    # Major club leagues
    "PL":  "soccer_epl",
    "BL1": "soccer_germany_bundesliga",
    "PD":  "soccer_spain_la_liga",
    "SA":  "soccer_italy_serie_a",
    "FL1": "soccer_france_ligue_one",
    "DED": "soccer_netherlands_eredivisie",
    "PPL": "soccer_portugal_primeira_liga",
    # International
    "WC":  "soccer_fifa_world_cup",
    "EC":  "soccer_uefa_european_championship",
    "CL":  "soccer_uefa_champs_league",
    "EL":  "soccer_uefa_europa_league",
    "CLI": "soccer_conmebol_copa_libertadores",
    "CA":  "soccer_conmebol_copa_america",
}

# Bookmakers we surface in the lite payload + use for `book_odds`.
# Order matters — first match wins. FanDuel first because the US-based
# user that asked for this feature uses FanDuel.
_PREFERRED_BOOKS = ("fanduel", "draftkings", "betmgm", "caesars", "pointsbet")


def _normalize_team(name: str) -> str:
    """Strip accents, casing, spaces, punctuation for fuzzy team matching.
    'Côte d'Ivoire' → 'cotedivoire', 'Bosnia & Herzegovina' → 'bosniaherzegovina'."""
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "", s.lower())
    return s


def _decimal_to_american(price: float) -> int:
    """Convert decimal odds (FanDuel returns 1.11) to American (-909)."""
    if price <= 1.0:
        return -10000  # degenerate, shouldn't happen
    if price >= 2.0:
        return round((price - 1.0) * 100)
    return -round(100 / (price - 1.0))


def _decimal_to_implied_pct(price: float) -> float:
    """Decimal odds → implied probability (0..100)."""
    if price <= 1.0:
        return 99.0
    return round(100.0 / price, 2)


async def fetch_odds_for_sport(sport_key: str, timeout: float = 6.0) -> list[dict]:
    """One-shot fetch of all h2h events + odds for a sport_key.

    Returns the raw Odds-API event list, or an empty list on any error
    (rate limit, network, missing key). Logs at WARN so failures are
    visible in supervisord output but never crash the pipeline.
    """
    api_key = (os.environ.get("THE_ODDS_API_KEY") or "").strip()
    if not api_key:
        return []
    try:
        async with httpx.AsyncClient(timeout=timeout) as cx:
            r = await cx.get(
                f"{_ODDS_API_BASE}/sports/{sport_key}/odds",
                params={
                    "apiKey": api_key,
                    "regions": "us",
                    "markets": "h2h",
                    "bookmakers": ",".join(_PREFERRED_BOOKS),
                    "oddsFormat": "decimal",
                },
            )
            if r.status_code != 200:
                logger.warning(
                    "Odds-API %s returned %d (body=%s)",
                    sport_key, r.status_code, r.text[:200],
                )
                return []
            return r.json() or []
    except Exception as e:
        logger.warning("Odds-API fetch failed for %s: %s", sport_key, e)
        return []


def _index_events_by_team_pair(events: list[dict]) -> dict[frozenset, dict]:
    """Build a {frozenset({home_norm, away_norm}) → event} lookup.

    Using frozenset so we match regardless of which team is "home" in
    the Odds API (sometimes flipped vs football-data.org — e.g. the
    Tunisia/Netherlands match is Tunisia-home there, Netherlands-home
    on football-data).
    """
    idx: dict[frozenset, dict] = {}
    for ev in events:
        home = _normalize_team(ev.get("home_team") or "")
        away = _normalize_team(ev.get("away_team") or "")
        if home and away:
            idx[frozenset({home, away})] = ev
    return idx


def lookup_real_odds(
    event_index: dict[frozenset, dict],
    home_team_name: str,
    away_team_name: str,
    selection_team_name: str,
) -> Optional[dict]:
    """Return the best real-book quote for the selection team, or None.

    Output schema:
        {
          "book_odds":           int,    # American (e.g. -909)
          "implied_probability": float,  # 0..100 percent
          "bookmaker":           str,    # e.g. "FanDuel"
          "decimal":             float,  # raw decimal (e.g. 1.11)
          "all_books":           {book_key: american_int, ...},
        }

    The "all_books" dict lets the sportsbook_mapper enrich the pick with
    multi-book deep-links downstream.
    """
    key = frozenset({_normalize_team(home_team_name), _normalize_team(away_team_name)})
    ev = event_index.get(key)
    if not ev:
        return None

    sel_norm = _normalize_team(selection_team_name)
    sel_label = (selection_team_name or "").strip()
    is_draw = sel_label.lower() in ("draw", "tie")

    best: Optional[dict] = None
    all_books: dict[str, int] = {}

    for bookmaker in ev.get("bookmakers") or []:
        bk_key = (bookmaker.get("key") or "").lower()
        bk_title = bookmaker.get("title") or bk_key.title()
        for mkt in bookmaker.get("markets") or []:
            if mkt.get("key") != "h2h":
                continue
            for outcome in mkt.get("outcomes") or []:
                name = (outcome.get("name") or "").strip()
                price = outcome.get("price")
                if price is None:
                    continue
                try:
                    price_f = float(price)
                except (TypeError, ValueError):
                    continue
                match = False
                if is_draw and name.lower() in ("draw", "tie"):
                    match = True
                elif _normalize_team(name) == sel_norm:
                    match = True
                if not match:
                    continue
                american = _decimal_to_american(price_f)
                all_books[bk_key] = american
                # First preferred-book match wins. _PREFERRED_BOOKS is
                # already in priority order so we exit on the first
                # hit from the highest-priority book.
                if best is None and bk_key in _PREFERRED_BOOKS:
                    best = {
                        "book_odds":           american,
                        "implied_probability": _decimal_to_implied_pct(price_f),
                        "bookmaker":           bk_title,
                        "bookmaker_key":       bk_key,
                        "decimal":             price_f,
                    }

    if best is None:
        return None
    best["all_books"] = all_books
    return best
