"""Wikipedia Top Scorer scraper for soccer leagues.

**Why this exists**

Sportsbook coverage for niche leagues (Allsvenskan, Eliteserien,
Veikkausliiga, Ekstraklasa, etc.) is patchy — The Odds API often has
NO Anytime-Goal-Scorer market for a match even when Linemate does.
Meanwhile Wikipedia's league-season articles maintain a fully-curated
top-scorer table updated weekly by editors.

This scraper reads that table so we can:
  1. Emit stats-driven goalscorer picks (analogous to the MLB Hot
     Hitters module) regardless of sportsbook coverage.
  2. Enrich existing goalscorer picks with a `season_goals_per_game`
     field for the Signal Engine.

Cached in `wiki_top_scorers` collection with a 24h TTL. Wikipedia
uses a well-known template pattern for these tables so the parser is
brittle but well-defined.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger("lockscore.services.wiki_top_scorers")

_WIKI_API = "https://en.wikipedia.org/w/api.php"
_UA = {"User-Agent": "PerkLocks/1.0 (+https://perklocks.app)"}
_CACHE_TTL_SECONDS = 24 * 3600

# League label → Wikipedia page slug (for current season).
# The scraper falls back to search when the direct slug misses.
_LEAGUE_PAGES: dict[str, list[str]] = {
    "Allsvenskan":                ["2026 Allsvenskan", "2025 Allsvenskan"],
    "Eliteserien":                ["2026 Eliteserien", "2025 Eliteserien"],
    "Veikkausliiga":              ["2026 Veikkausliiga", "2025 Veikkausliiga"],
    "Norwegian Eliteserien":      ["2026 Eliteserien", "2025 Eliteserien"],
    "Swedish Allsvenskan":        ["2026 Allsvenskan"],
    "Ekstraklasa":                ["2025–26 Ekstraklasa"],
    "Superliga":                  ["2025–26 Danish Superliga"],
    "Eredivisie":                 ["2025–26 Eredivisie"],
    "Premier League":             ["2025–26 Premier League"],
    "La Liga":                    ["2025–26 La Liga"],
    "Serie A":                    ["2025–26 Serie A"],
    "Bundesliga":                 ["2025–26 Bundesliga"],
    "Ligue 1":                    ["2025–26 Ligue 1"],
    "MLS":                        ["2026 Major League Soccer season"],
    "Brasileirão Série A":        ["2026 Campeonato Brasileiro Série A"],
    "Brasileirão Série B":        ["2026 Campeonato Brasileiro Série B"],
    "China Super League":         ["2026 Chinese Super League"],
    "K League 1":                 ["2026 K League 1"],
    "J1 League":                  ["2026 J1 League"],
    "League of Ireland":          ["2026 League of Ireland Premier Division"],
    "Argentine Primera División": ["2026 Argentine Primera División"],
}


# ── HTTP ────────────────────────────────────────────────────────────

async def _wiki_get(cx: httpx.AsyncClient, params: dict) -> dict:
    try:
        r = await cx.get(_WIKI_API, params=params, headers=_UA, timeout=15)
        if r.status_code != 200:
            return {}
        return r.json() or {}
    except Exception as e:
        logger.warning("wiki fetch failed: %s (%s)", e, params)
        return {}


async def _fetch_wikitext(cx: httpx.AsyncClient, page: str) -> str:
    data = await _wiki_get(cx, {
        "action": "parse", "format": "json",
        "page": page, "prop": "wikitext",
    })
    return data.get("parse", {}).get("wikitext", {}).get("*", "")


# ── parsing ────────────────────────────────────────────────────────

_ROW_PLAYER_PAT = re.compile(
    # Optional flag templates (both `{{flagicon|X}}` and `{{#invoke:flag|icon|X}}`).
    r"align=\"left\"[^\{]*?(?:\{\{(?:#invoke:)?flag(?:icon)?\|[^}]{1,60}\}\}\s*)?"
    r"\[\[([^|\]]+?)(?:\|[^\]]+?)?\]\]"
)

# Match a club cell that may be a wikilink OR bare text
_CLUB_CELL_PAT = re.compile(
    r"align=\"left\"[^\n]*?(?:\{\{(?:#invoke:)?flag(?:icon)?\|[^}]{1,60}\}\}\s*)?"
    r"(?:\[\[([^|\]]+?)(?:\|[^\]]+?)?\]\]|([A-Za-zÀ-ÿ0-9/.\-øÅåÄäÖöÜü ]{3,40}))"
)


def _parse_top_scorers(text: str, max_players: int = 25) -> list[dict]:
    """Parse the Top scorers section of a league-season wikitext page.
    Returns a list of `{name, club, goals}` dicts. Handles multiple
    Wikipedia template variants:
      • `{{flagicon|X}}` vs `{{#invoke:flag|icon|X}}`
      • Wikilinked clubs `[[Malmö FF]]` vs bare text `Bodø/Glimt`
      • Rowspan-shared goal counts
    """
    idx = text.lower().find("top scorer")
    if idx < 0:
        return []
    section = text[idx:idx + 14000]
    for stopper in ("==Clean sheets==", "==Awards==", "==Discipline==",
                    "==Attendances==", "==Hat-trick", "==See also==",
                    "==References==", "==External links=="):
        s_idx = section.find(stopper)
        if s_idx > 0:
            section = section[:s_idx]
            break

    out: list[dict] = []
    current_goals: Optional[int] = None
    current_goals_remaining = 0
    rows = re.split(r"\|-", section)
    for row in rows:
        # rowspan goals — a row can have TWO rowspan cells (rank at
        # start, goals at end). The goals column is always the last one.
        rowspan_hits = re.findall(
            r"rowspan=\"?(\d+)\"?\s*\|\s*(\d{1,3})\s*(?:$|\n)", row)
        if rowspan_hits:
            # The LAST rowspan match in a row is the goals column
            # (rank is always first, goals last). Small integer sanity.
            span, val = rowspan_hits[-1]
            current_goals = int(val)
            current_goals_remaining = int(span)

        # Extract player wikilink (must be a full name link, no template)
        pm = re.search(
            r"align=\"left\"[^{]*?(?:\{\{(?:#invoke:)?flag(?:icon)?\|[^}]{1,60}\}\}\s*)?"
            r"\[\[([^|\]]+?)(?:\|([^\]]+?))?\]\]",
            row,
        )
        if not pm:
            continue
        player_display = (pm.group(2) or pm.group(1)).strip()
        # Clean disambiguation "Player Name (footballer)" → "Player Name"
        player = re.sub(r"\s*\((?:footballer|born \d{4})[^)]*\)\s*$", "", player_display).strip()

        # Extract club cell — starts AFTER the player wikilink
        after_player = row[pm.end():]
        club = None
        cm = re.search(
            r"align=\"left\"[^\n]*?(?:\{\{(?:#invoke:)?flag(?:icon)?\|[^}]{1,60}\}\}\s*)?"
            r"(?:\[\[([^|\]]+?)(?:\|([^\]]+?))?\]\]|([^|\n]+?))\s*(?:$|\n|\|)",
            after_player,
        )
        if cm:
            club = (cm.group(2) or cm.group(1) or cm.group(3) or "").strip()
            # Strip leftover wiki markers and common suffixes
            club = re.sub(r"^\[\[|\]\]$", "", club).strip()
            club = re.sub(r"\s+Fotboll$", "", club).strip()

        # Goals — use rowspan carry when active; otherwise search this
        # row's non-rank cells for a bare integer >2 (rank is usually 1-15).
        g = None
        if current_goals_remaining > 0:
            g = current_goals
            current_goals_remaining -= 1
        else:
            # Look for a bare integer on its own line that's NOT the rank
            # (first cell). Scan cells in reverse and take the last one.
            rest_lines = [ln.strip() for ln in row.strip().splitlines()]
            for ln in reversed(rest_lines):
                m = re.match(r"\|?\s*(\d{1,3})\s*$", ln)
                if m:
                    candidate = int(m.group(1))
                    # Sanity-check the candidate isn't the rank (which
                    # appears first in the row). Ranks are typically
                    # <= 20 for top-scorer tables.
                    g = candidate
                    break

        if player and g is not None and 0 < g < 100:
            out.append({
                "name":  player,
                "club":  club or "?",
                "goals": g,
            })
            if len(out) >= max_players:
                break
    return out


# ── public API ─────────────────────────────────────────────────────

async def fetch_league_top_scorers(cx: httpx.AsyncClient, league: str) -> list[dict]:
    """Fetch the top-scorer list for a league. Returns [] on miss."""
    candidates = _LEAGUE_PAGES.get(league) or []
    if not candidates:
        # Try a generic search fallback
        r = await _wiki_get(cx, {
            "action": "query", "format": "json",
            "list": "search", "srsearch": f"{league} 2026",
            "srlimit": 3,
        })
        hits = r.get("query", {}).get("search", [])
        candidates = [h["title"] for h in hits]

    for page in candidates:
        text = await _fetch_wikitext(cx, page)
        if not text:
            continue
        rows = _parse_top_scorers(text)
        if rows:
            return [{
                **r, "league": league, "source_page": page,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            } for r in rows]
    return []


async def refresh_top_scorers(db, leagues: Optional[list[str]] = None) -> dict:
    """Refresh the top-scorer cache for the given leagues (or all
    known leagues)."""
    if leagues is None:
        leagues = list(_LEAGUE_PAGES.keys())
    started = datetime.now(timezone.utc)
    per_league: dict[str, int] = {}
    async with httpx.AsyncClient(headers=_UA) as cx:
        for league in leagues:
            try:
                rows = await fetch_league_top_scorers(cx, league)
            except Exception as e:
                logger.warning("top-scorer fetch %s failed: %s", league, e)
                rows = []
            per_league[league] = len(rows)
            await db.wiki_top_scorers.update_one(
                {"league": league},
                {"$set": {
                    "league":     league,
                    "scorers":    rows,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }},
                upsert=True,
            )
    finished = datetime.now(timezone.utc)
    return {
        "started_at":  started.isoformat(),
        "finished_at": finished.isoformat(),
        "elapsed_ms":  int((finished - started).total_seconds() * 1000),
        "leagues":     len(leagues),
        "per_league":  per_league,
        "total_scorers": sum(per_league.values()),
    }


async def get_top_scorers(db, league: str) -> list[dict]:
    """Cache-first accessor."""
    doc = await db.wiki_top_scorers.find_one(
        {"league": league},
        {"_id": 0, "scorers": 1, "updated_at": 1},
    )
    if doc:
        try:
            stale = datetime.fromisoformat(
                doc.get("updated_at", "").replace("Z", "+00:00"))
            if (datetime.now(timezone.utc) - stale).total_seconds() < _CACHE_TTL_SECONDS:
                return doc.get("scorers") or []
        except Exception:
            pass
    # Cache stale/missing — do a fresh fetch (no client passed, one-off)
    async with httpx.AsyncClient(headers=_UA) as cx:
        rows = await fetch_league_top_scorers(cx, league)
    if rows:
        await db.wiki_top_scorers.update_one(
            {"league": league},
            {"$set": {
                "league": league, "scorers": rows,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }},
            upsert=True,
        )
    return rows
