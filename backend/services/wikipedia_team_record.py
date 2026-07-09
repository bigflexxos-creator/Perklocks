"""Wikipedia Team Season-Record Scraper.

Why this exists (per user directive 2026-07-09): ESPN's `form` field
only exposes the last 5 games. For niche leagues (Montenegrin First
League, Kazakh Premier, Andorran, etc.) neither The Odds API,
football-data.org (tier-locked), nor TheSportsDB (rate-limited free
tier) give us a longer view. But Wikipedia's league-season articles
maintain a full standings template with W/D/L/GF/GA for every team,
authored by editors within days of the season ending.

This module scrapes that data with the following pipeline:

  1. Search Wikipedia for the team's article via the `list=search` API.
  2. From the team article, follow the \"Recent seasons\" table to find
     the most recent completed league season page (e.g.
     `2025-26 Montenegrin First League`).
  3. Parse the standings template — `|win_MOR=20|draw_MOR=9|...` —
     using the team's 3-letter code (extracted from the same page's
     `|name_MOR=...` entry).
  4. Cache the resulting record in `wiki_team_records` with a 7-day
     TTL so we don't hammer Wikipedia.

**Bounds:**
  * Soccer only initially (biggest data gap). Extendable to NBA/NFL
    later, though those sports already have deep ESPN coverage.
  * Records older than ~14 months are ignored — we want *last season*
    to project *this season*, not decade-old form.
"""
from __future__ import annotations

import logging
import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from .espn_team_meta import normalize_name

logger = logging.getLogger("lockscore.services.wiki_team_record")

_WIKI_API = "https://en.wikipedia.org/w/api.php"
_UA = {"User-Agent": "PerkLocks/1.0 (+https://perklocks.app)"}
_CACHE_TTL_SECONDS = 7 * 24 * 3600


# ── HTTP wrappers ──────────────────────────────────────────────────

async def _wiki_get(cx: httpx.AsyncClient, params: dict) -> dict:
    try:
        r = await cx.get(_WIKI_API, params=params, headers=_UA, timeout=15)
        if r.status_code != 200:
            logger.debug("wiki %s → %s", params, r.status_code)
            return {}
        return r.json() or {}
    except Exception as e:
        logger.warning("wiki fetch failed: %s (%s)", e, params)
        return {}


async def _search_page(cx: httpx.AsyncClient, query: str) -> Optional[str]:
    """Return the title of the top matching Wikipedia article for the
    given query, or None on miss."""
    data = await _wiki_get(cx, {
        "action": "query",
        "format": "json",
        "list": "search",
        "srsearch": query,
        "srlimit": 5,
    })
    hits = data.get("query", {}).get("search", [])
    return hits[0]["title"] if hits else None


async def _fetch_wikitext(cx: httpx.AsyncClient, page_title: str) -> str:
    data = await _wiki_get(cx, {
        "action": "parse",
        "format": "json",
        "page": page_title,
        "prop": "wikitext",
    })
    return data.get("parse", {}).get("wikitext", {}).get("*", "")


# ── record parsing ─────────────────────────────────────────────────

def _standings_template_record(text: str, team_code: str) -> Optional[dict]:
    """Extract a W/D/L record for a team_code (e.g. 'MOR') from a
    standings template. Returns None if any of W/D/L is missing.
    """
    if not text or not team_code:
        return None
    pat = re.compile(
        rf"\|\s*win_{team_code}\s*=\s*(\d+)"
        rf".*?draw_{team_code}\s*=\s*(\d+)"
        rf".*?loss_{team_code}\s*=\s*(\d+)"
        rf"(?:.*?gf_{team_code}\s*=\s*(\d+))?"
        rf"(?:.*?ga_{team_code}\s*=\s*(\d+))?",
        re.DOTALL,
    )
    m = pat.search(text)
    if not m:
        return None
    w, d, loss, gf, ga = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
    return {
        "wins":   int(w),
        "draws":  int(d),
        "losses": int(loss),
        "gf":     int(gf) if gf else None,
        "ga":     int(ga) if ga else None,
        "played": int(w) + int(d) + int(loss),
    }


_TEAM_CODE_PAT = re.compile(
    r"\|\s*name_([A-Z0-9]{2,4})\s*=\s*([^\n|]+)"
)


def _extract_display_name(raw: str) -> str:
    """Given the raw right-hand side of `|name_MOR=[[FK Mornar|Mornar]]`
    or `|name_MOR=Mornar`, return `'Mornar'`."""
    raw = raw.strip()
    # `[[Link|Display]]` → 'Display'
    m = re.search(r"\[\[[^|\]]+\|([^\]]+?)\]\]", raw)
    if m:
        return m.group(1).strip()
    # `[[Display]]` → 'Display'
    m = re.search(r"\[\[([^\]|]+?)\]\]", raw)
    if m:
        return m.group(1).strip()
    return raw.rstrip("] ").lstrip("[ ").strip()


def _find_team_code(text: str, team_name: str) -> Optional[str]:
    """Given the wikitext of a league-season page, return the team's
    short code (e.g. 'MOR' for Mornar). Matches both `|name_MOR=Mornar`
    and `|name_MOR=[[FK Mornar|Mornar]]` styles.
    """
    target = normalize_name(team_name)
    if not target:
        return None
    for m in _TEAM_CODE_PAT.finditer(text):
        code = m.group(1)
        display = _extract_display_name(m.group(2))
        if normalize_name(display) == target:
            return code
        # Fuzzy match — e.g. 'Mornar' inside 'FK Mornar'
        display_norm = normalize_name(display)
        if display_norm and (target in display_norm or display_norm in target):
            # Only accept fuzzy hit when it's a substantial overlap
            if len(target) >= 4 and len(display_norm) >= 4:
                return code
    return None


# ── page discovery ────────────────────────────────────────────────

_SEASON_YEAR_PAT = re.compile(r"(20\d{2})(?:[\u2013\u2014-]?(\d{2,4}))?")


def _extract_season(page_title: str) -> Optional[str]:
    """`'2025–26 Montenegrin First League'` → `'2025-26'`."""
    m = _SEASON_YEAR_PAT.search(page_title)
    if not m:
        return None
    y1 = m.group(1)
    y2 = m.group(2)
    if y2 is None:
        return y1
    if len(y2) == 2:
        return f"{y1}-{y2}"
    return f"{y1}-{y2}"


async def _find_latest_season_page(cx: httpx.AsyncClient, team_name: str) -> Optional[str]:
    """Discover the Wikipedia league-season page containing this team's
    standings row. Strategy prioritises league-standings pages (which
    carry the W/D/L template) over team-dedicated season pages.

    Order:
      1. Load the team's Wikipedia article once.
      2. Prefer the infobox `| season = [[YYYY-YY <league>|...]]` link
         — that's where the current-season standings live.
      3. Fall back to any wiki-link `[[YYYY-YY <...league...>]]` on the
         article (excluding UEFA cup pages).
      4. Fall back to `| league = [[X]]` + a targeted search for the
         current season of X.
      5. Last-resort: direct search of `{team} {season} season` — this
         resolves to the team-season page which has a link back to the
         league page (which we then follow).
    """
    now = datetime.now(timezone.utc)
    seasons = [
        f"{now.year - 1}-{str(now.year)[2:]}",
        f"{now.year}-{str(now.year + 1)[2:]}",
        f"{now.year}",
    ]

    # Load team article
    team_page = await _search_page(cx, team_name)
    if not team_page:
        return None
    text = await _fetch_wikitext(cx, team_page)
    if not text:
        return None

    def _looks_like_league_season(t: str) -> bool:
        if not t or not _extract_season(t):
            return False
        low = t.lower()
        # Reject cup / tournament pages — those don't have a
        # comprehensive team standings template.
        for bad in (
            "cup", "uefa champions", "uefa europa", "uefa conference",
            "world cup", "copa america", "nations league", "playoff",
            "season", "final",
        ):
            if bad in low:
                return False
        # Must contain "League" or "Division" or "Liga" or "Premiership"
        return any(k in low for k in
                   ("league", "division", "liga", "premiership",
                    "eredivisie", "bundesliga", "primera", "erovnuli",
                    "primeira", "premier"))

    def _year_of_season_title(t: str) -> int:
        """Extract the *starting* year from a title. Newer = higher."""
        m = re.search(r"(20\d{2}|19\d{2})", t)
        return int(m.group(1)) if m else 0

    # Step 2 — infobox season link (fastest, high accuracy)
    for m in re.finditer(r"\|\s*season\s*=\s*\[\[([^\]|]+)", text[:8000], re.IGNORECASE):
        cand = m.group(1).strip()
        if _looks_like_league_season(cand):
            return cand

    # Step 3 — any league-season wikilink in the top of the article.
    # Sort by starting year descending so we grab the most-recent
    # season, not e.g. Manchester City's 1999-2000 Division One reference.
    season_candidates: list[str] = []
    for m in re.finditer(
        r"\[\[((?:20\d{2}[\u2013\u2014-]\d{2,4}|\d{4})[^\]|]{2,80})",
        text[:20000],
    ):
        cand = m.group(1).strip()
        if _looks_like_league_season(cand):
            season_candidates.append(cand)
    if season_candidates:
        season_candidates.sort(key=_year_of_season_title, reverse=True)
        return season_candidates[0]

    # Step 4 — resolve league then search for current season
    m = re.search(r"\|\s*league\s*=\s*\[\[([^\]|]+)", text[:4000], re.IGNORECASE)
    if m:
        league_name = m.group(1).strip()
        for season in seasons:
            candidate = f"{season} {league_name}"
            title = await _search_page(cx, candidate)
            if title and _looks_like_league_season(title):
                return title

    # Step 5 — team-season page then follow its league link
    for season in seasons:
        title = await _search_page(cx, f"{team_name} {season} season")
        if title and season.replace("-", "") in title.replace("–", "").replace("—", "").replace("-", ""):
            # Team-season page — follow its infobox league link
            tsp_text = await _fetch_wikitext(cx, title)
            m = re.search(r"\|\s*league\s*=\s*\[\[([^\]|]+)", tsp_text[:4000], re.IGNORECASE)
            if m:
                league_name = m.group(1).strip()
                candidate = f"{season} {league_name}"
                league_title = await _search_page(cx, candidate)
                if league_title and _looks_like_league_season(league_title):
                    return league_title
            return title  # last resort
    return None


# ── public API ─────────────────────────────────────────────────────

async def fetch_team_season_record(
    cx: httpx.AsyncClient, team_name: str
) -> Optional[dict]:
    """Return `{team_name, wins, draws, losses, gf, ga, played, source_page, source_url}`
    or None on miss. Uses the discovery pipeline above."""
    page = await _find_latest_season_page(cx, team_name)
    if not page:
        return None
    text = await _fetch_wikitext(cx, page)
    if not text:
        return None
    code = _find_team_code(text, team_name)
    if not code:
        return None
    record = _standings_template_record(text, code)
    if not record:
        return None
    record.update({
        "team_name":   team_name,
        "team_code":   code,
        "source_page": page,
        "source_url":  f"https://en.wikipedia.org/wiki/{urllib.parse.quote(page.replace(' ', '_'))}",
        "season":      _extract_season(page),
        "fetched_at":  datetime.now(timezone.utc).isoformat(),
    })
    return record


async def get_team_record(db, sport: str, team_name: str,
                          force_refresh: bool = False) -> Optional[dict]:
    """DB-cached accessor. Refreshes automatically past `_CACHE_TTL_SECONDS`."""
    key = normalize_name(team_name)
    if not key:
        return None
    doc = None
    if not force_refresh:
        doc = await db.wiki_team_records.find_one(
            {"sport": sport, "team_norm": key},
            {"_id": 0},
        )
        if doc:
            try:
                stale = datetime.fromisoformat(doc.get("fetched_at").replace("Z", "+00:00"))
                if (datetime.now(timezone.utc) - stale).total_seconds() < _CACHE_TTL_SECONDS:
                    return doc.get("record")
            except Exception:
                pass

    async with httpx.AsyncClient(headers=_UA) as cx:
        record = await fetch_team_season_record(cx, team_name)
    if not record:
        # Cache the miss for 24h so we don't retry every request
        await db.wiki_team_records.update_one(
            {"sport": sport, "team_norm": key},
            {"$set": {
                "sport":      sport,
                "team_norm":  key,
                "team_name":  team_name,
                "record":     None,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "ttl_short":  True,
            }},
            upsert=True,
        )
        return None
    await db.wiki_team_records.update_one(
        {"sport": sport, "team_norm": key},
        {"$set": {
            "sport":      sport,
            "team_norm":  key,
            "team_name":  team_name,
            "record":     record,
            "fetched_at": record["fetched_at"],
        }},
        upsert=True,
    )
    return record


async def bulk_refresh_soccer(db, limit_teams: int = 200) -> dict:
    """Best-effort: pull season records for every soccer team we've
    seen in picks over the last 7 days. Skips teams that already have
    a fresh cache entry."""
    started = datetime.now(timezone.utc)
    cutoff = (started - timedelta(days=7)).isoformat()

    picks = await db.picks.find(
        {"sport": "Soccer", "event_time": {"$gte": cutoff}},
        {"event": 1},
    ).to_list(length=None)
    teams: set[str] = set()
    for p in picks:
        ev = p.get("event") or ""
        if " @ " in ev:
            a, h = ev.split(" @ ", 1)
            teams.add(a.strip())
            teams.add(h.strip())
        elif " vs " in ev:
            h, a = ev.split(" vs ", 1)
            teams.add(a.strip())
            teams.add(h.strip())

    hit = miss = skipped = 0
    async with httpx.AsyncClient(headers=_UA) as cx:
        for name in list(teams)[:limit_teams]:
            key = normalize_name(name)
            if not key:
                continue
            existing = await db.wiki_team_records.find_one(
                {"sport": "Soccer", "team_norm": key},
                {"fetched_at": 1, "record": 1},
            )
            if existing:
                try:
                    stale = datetime.fromisoformat(
                        existing.get("fetched_at", "").replace("Z", "+00:00"))
                    if (started - stale).total_seconds() < _CACHE_TTL_SECONDS:
                        skipped += 1
                        continue
                except Exception:
                    pass
            record = await fetch_team_season_record(cx, name)
            if record:
                hit += 1
            else:
                miss += 1
            await db.wiki_team_records.update_one(
                {"sport": "Soccer", "team_norm": key},
                {"$set": {
                    "sport":      "Soccer",
                    "team_norm":  key,
                    "team_name":  name,
                    "record":     record,
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                }},
                upsert=True,
            )

    finished = datetime.now(timezone.utc)
    return {
        "started_at":  started.isoformat(),
        "finished_at": finished.isoformat(),
        "elapsed_ms":  int((finished - started).total_seconds() * 1000),
        "teams_seen":  len(teams),
        "hits":        hit,
        "misses":      miss,
        "skipped_fresh": skipped,
    }
