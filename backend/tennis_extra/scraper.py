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
from datetime import datetime, timezone, timedelta as _td
from typing import Optional

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger("lockscore.tennis_extra.scraper")

_BASE = "https://www.tennisexplorer.com/matches/"
_TIMEOUT = 20.0
_CACHE_TTL = 1800  # 30 min
_cache: dict[str, tuple[float, list[dict]]] = {}

# Tournaments we EXPLICITLY SKIP. Anything else that TennisExplorer's
# `?type=all` endpoint surfaces is considered fair game (see filter
# below). This is a BLOCKLIST-only design so tournaments rotating in
# and out of the tour calendar week-to-week (Umag → Hamburg → Kitzbühel
# WTA → Prague, etc.) are picked up AUTOMATICALLY without code changes.
# User mandate 2026-07-12: "Right tennis tournaments always change I
# want app to be able to pick them up".
_SKIP_KEYWORDS = (
    # Non-tour categories — settlement is unreliable and lines are weak.
    "utr", "exhibition", "futures", " itf",
    # Section headers TennisExplorer emits as fake tournament rows.
    "main tournaments", "lower level tournaments",
    # ITF variants (with/without space).
    "itf w",     # ITF W15/W25/W35/W60/W75/W100 women
    "itf m",     # ITF M15/M25 men
)

# (Legacy list — retained but no longer consulted. See _is_tour_grade.)
_TOUR_KEYWORDS = (
    "atp", "wta", "challenger", "masters", "grand slam",
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
    """Permissive tour-grade filter (2026-07-12 rewrite).

    Design: accept ANY tournament that TennisExplorer's ?type=all endpoint
    surfaces UNLESS it matches a known-bad category (UTR / ITF /
    exhibition / futures / section header). This is a BLOCKLIST-only
    design — the rationale is that ATP/WTA rotate tournaments weekly
    (Umag → Hamburg → Kitzbühel WTA → Athens WTA → Iasi WTA → …) and
    hardcoding city names means we miss the new week's slate until
    someone updates the list. User mandate: "Right tennis tournaments
    always change I want app to be able to pick them up".

    We still require a non-empty tournament header. Sanity limit: 100
    chars so we don't accidentally pick up a stray HTML block.
    """
    if not tournament:
        return False
    t = tournament.lower().strip()
    if len(t) == 0 or len(t) > 100:
        return False
    if any(skip in t for skip in _SKIP_KEYWORDS):
        return False
    return True


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

    # 2026-07-12: switch to `?type=all` — TennisExplorer's default
    # endpoint used to return the main tour (Bastad/Gstaad/Umag) and
    # `type=all` used to add ITFs. But their frontend now emits WTA
    # 250s (Iasi WTA, Athens WTA, Kitzbühel WTA, Rome 2 WTA) ONLY on
    # `?type=all`. Since our `_is_tour_grade()` filter is now
    # blocklist-only (skips UTR/ITF/exhibition), we can safely use
    # `type=all` and the SKIP_KEYWORDS list keeps the noise out.
    # User mandate: "Right tennis tournaments always change I want app
    # to be able to pick them up" — this + the blocklist filter means
    # newly-scheduled tournaments auto-appear without code changes.
    url = f"{_BASE}?type=all&year={target.year}&month={target.month:02d}&day={target.day:02d}"
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
    """TennisExplorer publishes match times in **Europe/Prague** local time
    (their server clock). The site shows CEST (UTC+2) in summer and CET
    (UTC+1) in winter — DST switches the last Sunday of March / October.

    Previously this function hard-coded a UTC+2 offset, which silently
    drifted by 1 hour every winter and produced wrong commence_time
    stamps for ~5 months a year. We now use `zoneinfo("Europe/Prague")`
    so DST transitions are handled by the standard tzdata, and we also
    interpret `now` correctly even when the host clock isn't UTC (the
    old `dt.fromtimestamp(dt.timestamp() - 2*3600)` path was timezone-
    sensitive in subtle ways).

    Fallback: "today at noon UTC" if parsing fails.
    """
    if not time_str:
        return now.replace(hour=12, minute=0, second=0, microsecond=0).isoformat()
    m = re.match(r"^(\d{1,2}):(\d{2})$", time_str.strip())
    if not m:
        return now.replace(hour=12, minute=0, second=0, microsecond=0).isoformat()
    hh, mm = int(m.group(1)), int(m.group(2))

    # `now` is the date anchor (today or a target future date). We attach
    # Europe/Prague TZ so the local-time → UTC conversion is DST-aware.
    try:
        from zoneinfo import ZoneInfo
        prague_tz = ZoneInfo("Europe/Prague")
        local_dt = datetime(
            now.year, now.month, now.day,
            hh, mm, 0, 0,
            tzinfo=prague_tz,
        )
        utc_dt = local_dt.astimezone(timezone.utc)
        return utc_dt.isoformat()
    except Exception:
        # Last-resort fallback if the tzdata isn't available on the host.
        # Approximate CEST (UTC+2) — only used on a misconfigured box.
        dt_naive = now.replace(hour=hh, minute=mm, second=0, microsecond=0, tzinfo=timezone.utc)
        utc_dt = dt_naive - _td(hours=2)
        return utc_dt.isoformat()


def _infer_tier(tournament: str) -> str:
    t = (tournament or "").lower()
    if "qual" in t:
        return "Qualifier"
    if "challenger" in t:
        return "Challenger"
    # Explicit WTA / ATP tags anywhere in the name (Athens WTA, Iasi WTA,
    # Kitzbühel WTA, Bastad ATP, Rome 2 WTA, Halle ATP, etc.).
    if "wta" in t:
        return "WTA 250"
    if "atp" in t:
        return "ATP 250"
    # Bare city names (default TennisExplorer schema for the main tour) —
    # treat as ATP 250 unless the city is a known WTA-only tune-up.
    if any(city in t for city in ("homburg", "nottingham")):
        return "WTA 250"
    return "ATP 250"


async def _selftest() -> None:
    """Quick callable for ad-hoc verification."""
    rows = await fetch_today_matches()
    print(f"Scraped {len(rows)} matches")
    for r in rows[:5]:
        print(r)


if __name__ == "__main__":
    asyncio.run(_selftest())
