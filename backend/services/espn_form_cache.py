"""ESPN Form Cache — team recent-form strings by (sport, team_norm).

ESPN's scoreboards include a per-team `form` field on competitor blocks
(e.g. \"LLLWL\") for team sports. We already parse this in
`services.espn_common.parse_scoreboard_event` for UEFA/UFC ingest;
this module extends it to *every* covered sport so the Signal Engine
can factor recent form into non-UEFA picks too.

How it works:
  1. `refresh_all_forms(db)` walks a curated slug list, pulls the
     scoreboard for today/tomorrow, and upserts one doc per team into
     `espn_form_cache` collection.
  2. `attach_form_to_pick(db, pick)` reads home/away team_norm from
     the pick's `home_meta`/`away_meta` and stamps `.form` on them
     so the Signal Engine's `_form_signal` fires.

The cache is refreshed every 6 hours alongside team meta + injuries.
Storage is trivial (~30 sports × ~30 teams × <200 bytes/doc).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx

from .espn_common import espn_client, parse_scoreboard_event
from .espn_team_meta import normalize_name

logger = logging.getLogger("lockscore.services.espn_form_cache")

# (slug, sport_label_stored_in_pick_docs)
_FORM_SLUGS: list[tuple[str, str]] = [
    ("baseball/mlb",                       "MLB"),
    ("football/nfl",                       "NFL"),
    ("football/college-football",          "CFB"),
    ("basketball/nba",                     "NBA"),
    ("basketball/mens-college-basketball", "NCAAB"),
    ("basketball/wnba",                    "WNBA"),
    ("hockey/nhl",                         "NHL"),
    # Soccer — hit each major league so team_norm keys match across
    # our pick pipeline.
    ("soccer/eng.1",                       "Soccer"),
    ("soccer/esp.1",                       "Soccer"),
    ("soccer/ita.1",                       "Soccer"),
    ("soccer/ger.1",                       "Soccer"),
    ("soccer/fra.1",                       "Soccer"),
    ("soccer/uefa.champions",              "Soccer"),
    ("soccer/uefa.europa",                 "Soccer"),
    ("soccer/uefa.europa.conf",            "Soccer"),
    ("soccer/uefa.champions_qual",         "Soccer"),
    ("soccer/uefa.europa_qual",            "Soccer"),
    ("soccer/uefa.europa.conf_qual",       "Soccer"),
    ("soccer/fifa.world",                  "Soccer"),
    ("soccer/usa.1",                       "Soccer"),
    ("soccer/mex.1",                       "Soccer"),
    ("soccer/bra.1",                       "Soccer"),
]


async def refresh_all_forms(db) -> dict:
    """Fetch scoreboards for today + tomorrow for every slug and cache
    the `form` string per team. Bulk-safe: one upsert per team.
    """
    started = datetime.now(timezone.utc)
    today = datetime.now(timezone.utc).date()
    dates = [(today + timedelta(days=i)).strftime("%Y%m%d") for i in range(3)]
    per_slug: dict[str, int] = {}
    total_teams = 0
    seen_keys: set[tuple[str, str]] = set()   # dedup across dates

    async with httpx.AsyncClient(headers={"User-Agent": "PerkLocks/1.0"}) as cx:
        for slug, sport in _FORM_SLUGS:
            slug_count = 0
            for d in dates:
                try:
                    events = await espn_client.scoreboard(cx, slug, d)
                except Exception as e:
                    logger.warning("scoreboard %s/%s failed: %s", slug, d, e)
                    events = []
                for ev in events or []:
                    pe = parse_scoreboard_event(ev, sport, slug)
                    if not pe:
                        continue
                    for side_info in (pe.home, pe.away):
                        name = side_info.get("name") or ""
                        form = side_info.get("form") or ""
                        if not name or not form:
                            continue
                        key = (normalize_name(name), sport)
                        if key in seen_keys:
                            continue
                        seen_keys.add(key)
                        await db.espn_form_cache.update_one(
                            {"sport": sport, "team_norm": key[0]},
                            {"$set": {
                                "sport":      sport,
                                "team_norm":  key[0],
                                "team_name":  name,
                                "form":       form,
                                "record":     side_info.get("record"),
                                "updated_at": datetime.now(timezone.utc).isoformat(),
                            }},
                            upsert=True,
                        )
                        slug_count += 1
                        total_teams += 1
            per_slug[slug] = slug_count

    finished = datetime.now(timezone.utc)
    summary = {
        "started_at":   started.isoformat(),
        "finished_at":  finished.isoformat(),
        "elapsed_ms":   int((finished - started).total_seconds() * 1000),
        "teams_cached": total_teams,
        "per_slug":     per_slug,
    }
    logger.info("ESPN form cache refresh: %s", summary)
    return summary


async def get_team_form(db, sport: str, team_name: str) -> str | None:
    key = normalize_name(team_name)
    if not key:
        return None
    doc = await db.espn_form_cache.find_one(
        {"sport": sport, "team_norm": key},
        {"_id": 0, "form": 1},
    )
    return doc.get("form") if doc else None


async def attach_form_to_pick(db, pick: dict) -> dict:
    """Populate `pick.home_meta.form` and `.away_meta.form` from the
    cache so the Signal Engine's `_form_signal` can act on them.

    No-op when meta blocks aren't yet on the pick.
    """
    sport = pick.get("sport")
    event = pick.get("event") or ""
    if not sport or not event:
        return pick
    home = away = ""
    if " @ " in event:
        away, home = event.split(" @ ", 1)
    elif " vs " in event:
        home, away = event.split(" vs ", 1)
    if home:
        f = await get_team_form(db, sport, home.strip())
        if f:
            pick.setdefault("home_meta", {})["form"] = f
    if away:
        f = await get_team_form(db, sport, away.strip())
        if f:
            pick.setdefault("away_meta", {})["form"] = f
    return pick
