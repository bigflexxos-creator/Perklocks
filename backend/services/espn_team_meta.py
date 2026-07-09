"""ESPN team metadata registry.

Why: every pick card on our board looks the same today (generic circles,
no visual hierarchy). Linemate wins the \"UX polish\" comparison in
significant part because they show team crests + team colors on every
fixture.

This module pre-loads every ESPN team's canonical logo URL, primary
and alternate colors, and normalized name variants. Written to
`espn_team_meta` collection once at startup then read via a small
cache lookup on pick fetch.

Sports covered by ESPN's `/teams` endpoint (all with logo + colors):
  • football/nfl
  • football/college-football (FBS)
  • basketball/nba
  • basketball/mens-college-basketball
  • basketball/wnba
  • baseball/mlb
  • hockey/nhl
  • soccer/{competition_slug} — Champions/EPL/La Liga/etc.
  • mma/ufc (fighter meta — no team)

Deliberately no TTL — logos rarely change. Refresh via admin endpoint
whenever a re-brand happens.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any

import httpx

from .espn_common import espn_client

logger = logging.getLogger("lockscore.services.espn_team_meta")

# ESPN team slugs to hydrate. Add new sports here — the loader is
# generic. Format: (slug, sport_label_stored_in_db)
_TEAM_SLUGS: list[tuple[str, str]] = [
    ("football/nfl",                       "NFL"),
    ("football/college-football",          "CFB"),
    ("basketball/nba",                     "NBA"),
    ("basketball/mens-college-basketball", "NCAAB"),
    ("basketball/wnba",                    "WNBA"),
    ("baseball/mlb",                       "MLB"),
    ("hockey/nhl",                         "NHL"),
    # Soccer — we hit each major league separately. ESPN's slug format
    # for soccer leagues is well-known.
    ("soccer/eng.1",                       "Soccer"),   # Premier League
    ("soccer/esp.1",                       "Soccer"),   # La Liga
    ("soccer/ita.1",                       "Soccer"),   # Serie A
    ("soccer/ger.1",                       "Soccer"),   # Bundesliga
    ("soccer/fra.1",                       "Soccer"),   # Ligue 1
    ("soccer/uefa.champions",              "Soccer"),
    ("soccer/uefa.europa",                 "Soccer"),
    ("soccer/uefa.europa.conf",            "Soccer"),
    ("soccer/uefa.champions_qual",         "Soccer"),
    ("soccer/uefa.europa_qual",            "Soccer"),
    ("soccer/uefa.europa.conf_qual",       "Soccer"),
    ("soccer/fifa.world",                  "Soccer"),
    ("soccer/usa.1",                       "Soccer"),   # MLS
    ("soccer/mex.1",                       "Soccer"),   # Liga MX
    ("soccer/bra.1",                       "Soccer"),   # Brasileiro
]


def normalize_name(name: str) -> str:
    """Lowercase, strip accents, strip punctuation/spaces. Used to
    match team names in pick docs against ESPN's canonical names.
    'Côte d Ivoire' → 'cotedivoire'; 'F.C. Bayern' → 'fcbayern'."""
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "", s.lower())
    return s


_MEM_CACHE: dict[str, dict[str, Any]] = {}


async def refresh_all_teams(db) -> dict:
    """Pull every ESPN team registry we care about and upsert into
    `espn_team_meta` collection. Also refreshes the in-process cache.

    Idempotent — safe to run on every boot.
    """
    started = datetime.now(timezone.utc)
    total = 0
    per_slug: dict[str, int] = {}
    async with httpx.AsyncClient(headers={"User-Agent": "PerkLocks/1.0"}) as cx:
        for slug, sport in _TEAM_SLUGS:
            try:
                teams = await espn_client.teams(cx, slug)
            except Exception as e:
                logger.warning("teams fetch %s failed: %s", slug, e)
                teams = []
            per_slug[slug] = len(teams)
            for t in teams:
                names = [t.get("displayName"), t.get("name"),
                         t.get("shortDisplayName"), t.get("nickname"),
                         t.get("location"), t.get("abbreviation")]
                names = [n for n in names if n]
                key = normalize_name(t.get("displayName") or t.get("name") or "")
                if not key:
                    continue
                doc = {
                    "norm_name":     key,
                    "display_name":  t.get("displayName"),
                    "nickname":      t.get("nickname"),
                    "abbreviation":  t.get("abbreviation"),
                    "team_id":       t.get("id"),
                    "slug":          slug,
                    "sport":         sport,
                    "logo":          t.get("logos", [{}])[0].get("href")
                                     if t.get("logos") else None,
                    "color":         t.get("color"),
                    "alt_color":     t.get("alternateColor"),
                    "aliases":       list({normalize_name(n) for n in names if n}),
                    "updated_at":    datetime.now(timezone.utc).isoformat(),
                }
                await db.espn_team_meta.update_one(
                    {"norm_name": key, "sport": sport},
                    {"$set": doc},
                    upsert=True,
                )
                # Cache every alias too so lookups are O(1).
                for alias in doc["aliases"]:
                    _MEM_CACHE[(alias, sport)] = doc  # type: ignore
                total += 1
    finished = datetime.now(timezone.utc)
    summary = {
        "started_at":  started.isoformat(),
        "finished_at": finished.isoformat(),
        "elapsed_ms":  int((finished - started).total_seconds() * 1000),
        "teams_upserted": total,
        "per_slug":    per_slug,
    }
    logger.info("ESPN team meta refresh: %s", summary)
    return summary


async def lookup(db, name: str, sport: str) -> dict[str, Any] | None:
    """O(1) mem-cache hit → falls through to DB.
    Sport is required for disambiguation (e.g. 'Bayern' resolves
    differently in Bundesliga vs Bundesliga Frauen)."""
    key = normalize_name(name)
    if not key:
        return None
    cached = _MEM_CACHE.get((key, sport))
    if cached:
        return cached
    doc = await db.espn_team_meta.find_one(
        {"$or": [{"norm_name": key, "sport": sport},
                 {"aliases": key, "sport": sport}]},
        {"_id": 0},
    )
    if doc:
        _MEM_CACHE[(key, sport)] = doc
    return doc


async def enrich_pick(db, pick: dict) -> dict:
    """Attach `home_meta` / `away_meta` (logo, color) to a pick doc
    based on `event` (\"Away Team @ Home Team\") or team names on the
    pick itself.

    Safe to call on already-enriched picks — no-ops when meta absent.
    """
    sport = pick.get("sport")
    if not sport:
        return pick
    event = pick.get("event") or ""
    home_name = away_name = None
    if " @ " in event:
        away_name, home_name = event.split(" @ ", 1)
    elif " vs " in event:
        home_name, away_name = event.split(" vs ", 1)
    if home_name:
        home_meta = await lookup(db, home_name.strip(), sport)
        if home_meta:
            pick["home_meta"] = {
                "logo": home_meta.get("logo"),
                "color": home_meta.get("color"),
                "alt_color": home_meta.get("alt_color"),
                "abbrev": home_meta.get("abbreviation"),
            }
    if away_name:
        away_meta = await lookup(db, away_name.strip(), sport)
        if away_meta:
            pick["away_meta"] = {
                "logo": away_meta.get("logo"),
                "color": away_meta.get("color"),
                "alt_color": away_meta.get("alt_color"),
                "abbrev": away_meta.get("abbreviation"),
            }
    return pick


async def enrich_picks(db, picks: list[dict]) -> list[dict]:
    for p in picks:
        await enrich_pick(db, p)
    return picks
