"""TennisExplorer.com scraper.

Polite by design:
  • Single GET per day (cached for 30 min in process).
  • User-Agent identifies us as Mozilla (no spoofing tricks).
  • Soft fail on 4xx/5xx — we just return [] so the pipeline continues.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger("lockscore.tennis_extra.scraper")

_BASE = "https://www.tennisexplorer.com/matches/"
_TIMEOUT = 20.0
_CACHE_TTL = 1800  # 30 min
_cache: dict[str, tuple[float, list[dict]]] = {}

# Tournaments we DO want to surface (ATP/WTA main tour + Challengers).
# Filtered against the tournament header name. Anything containing these
# tokens is considered "tour-grade".
_TOUR_KEYWORDS = (
    "atp", "wta", "challenger", "masters", "grand slam",
    # Main-tour 250/500 tournament cities that don't include ATP/WTA in name:
    "halle", "queen", "berlin", "mallorca", "homburg", "eastbourne",
    "nottingham", "hertogenbosch", "rosmalen", "stuttgart", "birmingham",
    "newport", "atlanta", "los cabos", "washington", "winston-salem",
    "kitzbuhel", "umag", "gstaad", "bastad",
)

# Tournaments we EXPLICITLY SKIP (too low-level / exhibition / settlement risk).
_SKIP_KEYWORDS = (
    "utr", "exhibition", "futures", "itf", "lower level", "main tournaments",
)


def _decimal_to_american(dec: float) -> Optional[int]:
    """Decimal odds → American moneyline odds (rounded)."""
    if dec is None or dec <= 1.0:
        return None
    if dec >= 2.0:
        return round((dec - 1) * 100)
    return -round(100 / (dec - 1))


def _decimal_to_implied(dec: float) -> float:
    """Decimal odds → implied probability (0..1)."""
    if not dec or dec <= 1.0:
        return 0.0
    return 1.0 / dec


def _parse_odds(cell_text: str) -> Optional[float]:
    """TennisExplorer odds cells contain "1.51" or "2.32" or "-" (no odds)."""
    txt = (cell_text or "").strip()
    if not txt or txt == "-":
        return None
    try:
        val = float(txt)
        if 1.01 <= val <= 50.0:
            return val
    except ValueError:
        pass
    return None


def _normalize_player_name(raw: str) -> str:
    """TennisExplorer formats names as 'Lastname F.' — we keep that format
    so it matches what the UI shows, but trim whitespace/dots."""
    if not raw:
        return ""
    n = raw.strip()
    # Drop ranking suffixes like "(WC)" or "(Q)" but keep the name.
    n = re.sub(r"\s*\((WC|Q|LL|PR|SE|ALT)\)\s*$", "", n, flags=re.IGNORECASE)
    return n.strip()


def _is_tour_grade(tournament: str) -> bool:
    """True if this tournament should produce picks."""
    if not tournament:
        return False
    t = tournament.lower()
    if any(skip in t for skip in _SKIP_KEYWORDS):
        return False
    return any(kw in t for kw in _TOUR_KEYWORDS)


def _qualification_mark(tournament: str) -> str:
    """Tag qualifying-round matches separately (we still surface them but
    settle differently — qualifiers can be over in <60 min)."""
    t = (tournament or "").lower()
    if "qual" in t:
        return "(Q)"
    return ""


async def fetch_today_matches(now: Optional[datetime] = None, target_date: Optional[datetime] = None) -> list[dict]:
    """Returns a list of match dicts for `target_date` (default = today's UTC date).

    Pass `target_date` to scrape a future date (e.g. tomorrow) for night-before
    visibility — TennisExplorer's URL supports `?year=...&month=...&day=...`
    natively, so this is one extra HTTP call per day with no API credits used.

    Each match dict:
      {
        "tournament": "Mallorca ATP",
        "tournament_tier": "ATP 250" | "Challenger" | "Qualifier" | "Unknown",
        "round": "R1" | None,
        "commence_time": ISO 8601 UTC,
        "player1": "Borges N.",
        "player2": "Mannarino A.",
        "odds_dec_p1": 1.51 or None,
        "odds_dec_p2": 2.32 or None,
        "odds_american_p1": -196,
        "odds_american_p2": +132,
        "implied_p1": 0.662,
        "implied_p2": 0.431,
      }
    """
    now = now or datetime.now(timezone.utc)
    target = target_date or now
    cache_key = target.strftime("%Y-%m-%d")
    cached = _cache.get(cache_key)
    if cached and (time.time() - cached[0]) < _CACHE_TTL:
        return cached[1]

    # NOTE: Do NOT pass `type=all` — TennisExplorer's default endpoint
    # returns the MAIN tour tournaments (Mallorca, Bad Homburg, Halle,
    # Queen's etc.). `type=all` perversely filters those out and only
    # returns UTR exhibitions + ITFs.
    url = f"{_BASE}?year={target.year}&month={target.month:02d}&day={target.day:02d}"
    headers = {
        "User-Agent": "Mozilla/5.0 PerksLocks/1.0 (https://perkslocks.com)",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=headers) as cx:
            r = await cx.get(url, follow_redirects=True)
    except Exception as e:
        logger.warning("tennis-extra scrape failed for %s: %s", cache_key, e)
        return []
    if r.status_code != 200:
        logger.warning("tennis-extra HTTP %s for %s", r.status_code, cache_key)
        return []

    # Pass `target` so commence times resolve relative to the target date,
    # not the wall-clock now. Critical for tomorrow's matches — without this,
    # a "14:00 CEST" tomorrow match would render with today's date stamp.
    matches = _parse_html(r.text, target)
    _cache[cache_key] = (time.time(), matches)
    logger.info("tennis-extra scrape (%s): %d tour-grade matches parsed", cache_key, len(matches))
    return matches


def _parse_html(html: str, now: datetime) -> list[dict]:
    """Walk every match row under tour-grade tournament tables.

    TennisExplorer puts MULTIPLE tournaments inside a single
    <table class="result">, separating them with `<tr class="head">`
    rows. We must track the current tournament context as we walk.
    """
    soup = BeautifulSoup(html, "lxml")
    out: list[dict] = []

    for tbl in soup.find_all("table", class_="result"):
        rows = tbl.find_all("tr")
        current_tournament: Optional[str] = None
        current_is_tour: bool = False
        i = 0
        while i < len(rows):
            row = rows[i]
            classes = row.get("class") or []
            # Tournament header row → update context.
            if "head" in classes:
                cell = row.find("td")
                if cell:
                    current_tournament = cell.get_text(" ", strip=True)
                    current_is_tour = _is_tour_grade(current_tournament or "")
                i += 1
                continue

            if not current_is_tour or current_tournament is None:
                i += 1
                continue

            # Match row pair: row1 has time + p1 + odds; row2 has p2.
            if i + 1 >= len(rows):
                break
            row1 = row
            row2 = rows[i + 1]
            # If the next row is another head, this orphan row is malformed.
            if "head" in (row2.get("class") or []):
                i += 1
                continue

            tds1 = [td.get_text(" ", strip=True) for td in row1.find_all("td")]
            tds2 = [td.get_text(" ", strip=True) for td in row2.find_all("td")]
            if len(tds1) < 11 or len(tds2) < 2:
                # Row schema is: [0]=time+books, [1]=player1, [2..8]=score
                # cells, [9]=odds_p1, [10]=odds_p2, [11]=blank, [12]=info.
                # Anything shorter is a match still loading or malformed.
                i += 1
                continue

            # [0] is "HH:MM Live streams 1xBet BetVictor ..." — extract
            # just the leading time.
            time_str = None
            m_time = re.match(r"^(\d{1,2}:\d{2})", tds1[0])
            if m_time:
                time_str = m_time.group(1)
            player1 = _normalize_player_name(tds1[1])
            player2 = _normalize_player_name(tds2[0])
            if not player1 or not player2 or player2 == "-":
                i += 2
                continue

            # Odds columns at indices 9 & 10 (TennisExplorer schema).
            odds_p1 = _parse_odds(tds1[9]) if len(tds1) > 9 else None
            odds_p2 = _parse_odds(tds1[10]) if len(tds1) > 10 else None

            commence_iso = _resolve_commence_time(time_str, now)
            tier = _infer_tier(current_tournament)

            out.append({
                "tournament": current_tournament,
                "tournament_tier": tier,
                "round": None,
                "commence_time": commence_iso,
                "player1": player1,
                "player2": player2,
                "odds_dec_p1": odds_p1,
                "odds_dec_p2": odds_p2,
                "odds_american_p1": _decimal_to_american(odds_p1) if odds_p1 else None,
                "odds_american_p2": _decimal_to_american(odds_p2) if odds_p2 else None,
                "implied_p1": _decimal_to_implied(odds_p1) if odds_p1 else 0.0,
                "implied_p2": _decimal_to_implied(odds_p2) if odds_p2 else 0.0,
            })
            i += 2
    return out


def _resolve_commence_time(time_str: Optional[str], now: datetime) -> str:
    """TennisExplorer times appear as 'HH:MM' in CEST (their server clock,
    Europe/Prague). Convert to UTC ISO 8601 for today's date.

    If parsing fails, fall back to "today at noon UTC".
    """
    if not time_str:
        return now.replace(hour=12, minute=0, second=0, microsecond=0).isoformat()
    m = re.match(r"^(\d{1,2}):(\d{2})$", time_str.strip())
    if not m:
        return now.replace(hour=12, minute=0, second=0, microsecond=0).isoformat()
    hh, mm = int(m.group(1)), int(m.group(2))
    # CEST is UTC+2 (summer). Subtract 2h to get UTC.
    dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    dt = dt.fromtimestamp(dt.timestamp() - 2 * 3600, tz=timezone.utc)
    return dt.isoformat()


def _infer_tier(tournament: str) -> str:
    t = (tournament or "").lower()
    if "qual" in t:
        return "Qualifier"
    if "challenger" in t:
        return "Challenger"
    if "wta" in t:
        return "WTA 250"
    if "atp" in t:
        return "ATP 250"
    # Bare city names — typically 250-level grass tune-ups.
    if any(city in t for city in ("halle", "queen", "mallorca", "homburg",
                                   "eastbourne", "nottingham", "berlin")):
        if "wta" in t or "homburg" in t or "nottingham" in t:
            return "WTA 250"
        return "ATP 250"
    return "Unknown"


async def _selftest() -> None:
    """Quick callable for ad-hoc verification."""
    rows = await fetch_today_matches()
    print(f"Scraped {len(rows)} matches")
    for r in rows[:5]:
        print(r)


if __name__ == "__main__":
    asyncio.run(_selftest())
