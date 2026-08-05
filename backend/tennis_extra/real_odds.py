"""Real-odds resolver for tennis picks.

Mirrors `soccer/real_odds.py` — for each tennis_extra pick built from
the TennisExplorer scrape, try to find the matching event in The Odds
API and promote it to the main board with real FanDuel / DraftKings /
BetMGM lines instead of the model's Elo-derived fair odds.

Two key differences vs soccer:

1. **Sport key discovery is DYNAMIC.** Tennis sport_keys rotate weekly
   (this week = `tennis_wta_bad_homburg_open`, next week = a Wimbledon
   key, etc). We can't hardcode a competition-code → sport_key map like
   soccer. Instead, we call `/v4/sports` once per pipeline run, grab
   every active `tennis_*` key, and fetch them all in parallel.

2. **Player-name fuzzy matching.** The scrape gives names like
   "Davidovich Fokina A." while The Odds API uses "Alejandro Davidovich
   Fokina". We normalize both sides (strip accents, lowercase,
   keep-letters-only) and also match on LAST NAME alone as a fallback,
   since TennisExplorer sometimes truncates first names to an initial.
"""
from __future__ import annotations

import logging
import os
import re
import unicodedata
from typing import Optional

import httpx

logger = logging.getLogger("lockscore.tennis_extra.real_odds")

_ODDS_API_BASE = "https://api.the-odds-api.com/v4"

# Preferred-book order — same as soccer (user is on FanDuel).
_PREFERRED_BOOKS = ("fanduel", "draftkings", "betmgm", "caesars", "pointsbet")


def _normalize_player(name: str) -> str:
    """Strip accents, casing, punctuation. 'Davidovich Fokina A.' →
    'davidovichfokinaa'."""
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "", s.lower())
    return s


def _last_name(name: str) -> str:
    """Best-effort last-name extraction for fuzzy fallback matching.
    'Davidovich Fokina A.' → 'fokina', 'Alejandro Davidovich Fokina' →
    'fokina'. For two-word lastnames (Davidovich Fokina) this returns
    the trailing word — fine for fuzzy matching because both sides
    have the same trailing word."""
    if not name:
        return ""
    # Strip trailing initials like "A." or "J."
    parts = [p for p in re.split(r"\s+", name.strip()) if p and not re.fullmatch(r"[A-Z]\.", p)]
    if not parts:
        return ""
    return _normalize_player(parts[-1])


def _decimal_to_american(price: float) -> int:
    if price <= 1.0:
        return -10000
    if price >= 2.0:
        return round((price - 1.0) * 100)
    return -round(100 / (price - 1.0))


def _decimal_to_implied_pct(price: float) -> float:
    if price <= 1.0:
        return 99.0
    return round(100.0 / price, 2)


async def _list_active_tennis_sport_keys(client: httpx.AsyncClient, api_key: str) -> list[str]:
    """Cached through `services.odds_cache` — hits network at most
    once per 24 h."""
    try:
        from services.odds_cache import cached_httpx_get
        data = await cached_httpx_get(
            f"{_ODDS_API_BASE}/sports",
            {},
            api_key=api_key,
            endpoint_type="sports_list",
            caller="tennis_extra.real_odds._list_active_tennis_sport_keys",
        )
        return [
            s.get("key") for s in (data or [])
            if s.get("active") and (s.get("key") or "").startswith("tennis_")
        ]
    except Exception as e:
        logger.warning("Failed to list tennis sport_keys: %s", e)
        return []


async def _fetch_odds_for_sport(
    client: httpx.AsyncClient, api_key: str, sport_key: str,
) -> list[dict]:
    """Fetch all live h2h events + odds for a tennis sport_key.
    Cached; returns raw event list or [] on any error."""
    try:
        from services.odds_cache import cached_httpx_get
        data = await cached_httpx_get(
            f"{_ODDS_API_BASE}/sports/{sport_key}/odds",
            {"regions": "us", "markets": "h2h",
              "bookmakers": ",".join(_PREFERRED_BOOKS),
              "oddsFormat": "decimal"},
            api_key=api_key,
            endpoint_type="bulk_odds",
            caller="tennis_extra.real_odds._fetch_odds_for_sport",
            sport_key=sport_key,
            markets="h2h",
            skip_completed=True,
        )
        return data or []
    except Exception as e:
        logger.warning("Tennis odds fetch failed for %s: %s", sport_key, e)
        return []


async def fetch_all_tennis_events(timeout: float = 8.0) -> list[dict]:
    """One-shot helper: returns the COMBINED list of every active
    tennis tournament's h2h events. Each event keeps its original
    Odds-API shape so downstream code can read `home_team`,
    `away_team`, `bookmakers`, etc.

    Falls back to empty list silently if THE_ODDS_API_KEY is missing
    or any network call fails.
    """
    api_key = (os.environ.get("THE_ODDS_API_KEY") or "").strip()
    if not api_key:
        return []
    async with httpx.AsyncClient(timeout=timeout) as cx:
        keys = await _list_active_tennis_sport_keys(cx, api_key)
        if not keys:
            return []
        import asyncio
        results = await asyncio.gather(
            *[_fetch_odds_for_sport(cx, api_key, k) for k in keys],
            return_exceptions=True,
        )
        merged: list[dict] = []
        for k, res in zip(keys, results):
            if isinstance(res, Exception):
                continue
            for ev in res or []:
                # Tag with sport_key so downstream can show the tournament label
                ev["_sport_key"] = k
                merged.append(ev)
        logger.info(
            "Tennis real-odds prefetch: %d sport_keys (%s) → %d events",
            len(keys), ",".join(keys), len(merged),
        )
        return merged


def _player_match_score(scrape_name: str, odds_name: str) -> float:
    """0..1 score for how well two player names align.
    1.0 = full normalized match, 0.7 = last-name match, 0 otherwise."""
    s = _normalize_player(scrape_name)
    o = _normalize_player(odds_name)
    if not s or not o:
        return 0.0
    # Strong: scrape name is a prefix or substring of odds (or vice versa)
    if s == o or s in o or o in s:
        return 1.0
    # Weak fallback: same last name
    if _last_name(scrape_name) and _last_name(scrape_name) == _last_name(odds_name):
        return 0.7
    return 0.0


def lookup_real_odds_for_match(
    events: list[dict],
    player_a: str,
    player_b: str,
    selection_player: str,
) -> Optional[dict]:
    """Search `events` (from fetch_all_tennis_events) for a match
    between `player_a` and `player_b`, then return the real book odds
    for `selection_player`.

    Returns same shape as `soccer/real_odds.lookup_real_odds` so the
    downstream pick-builder treats both identically.
    """
    sel_norm = _normalize_player(selection_player)
    sel_last = _last_name(selection_player)
    if not sel_norm:
        return None

    # Find candidate events where both player_a AND player_b have at
    # least last-name matches with the event's home/away teams.
    best_event: Optional[dict] = None
    best_score = 0.0
    for ev in events:
        home = ev.get("home_team") or ""
        away = ev.get("away_team") or ""
        # Try both orderings — Odds API home/away mapping is unreliable.
        score_ab = (
            _player_match_score(player_a, home)
            + _player_match_score(player_b, away)
        )
        score_ba = (
            _player_match_score(player_a, away)
            + _player_match_score(player_b, home)
        )
        score = max(score_ab, score_ba)
        if score >= 1.4 and score > best_score:  # both names must match decently
            best_score = score
            best_event = ev

    if not best_event:
        return None

    # Resolve the selection player's price.
    all_books: dict[str, int] = {}
    best_quote: Optional[dict] = None
    for bk in best_event.get("bookmakers") or []:
        bk_key = (bk.get("key") or "").lower()
        bk_title = bk.get("title") or bk_key.title()
        for mkt in bk.get("markets") or []:
            if mkt.get("key") != "h2h":
                continue
            for outcome in mkt.get("outcomes") or []:
                name = outcome.get("name") or ""
                if _player_match_score(selection_player, name) < 0.7:
                    continue
                price = outcome.get("price")
                if price is None:
                    continue
                try:
                    price_f = float(price)
                except (TypeError, ValueError):
                    continue
                american = _decimal_to_american(price_f)
                all_books[bk_key] = american
                # Highest-priority book wins.
                if best_quote is None and bk_key in _PREFERRED_BOOKS:
                    best_quote = {
                        "book_odds":           american,
                        "implied_probability": _decimal_to_implied_pct(price_f),
                        "bookmaker":           bk_title,
                        "bookmaker_key":       bk_key,
                        "decimal":             price_f,
                    }

    if best_quote is None:
        return None
    best_quote["all_books"] = all_books
    best_quote["sport_key"] = best_event.get("_sport_key")
    return best_quote
