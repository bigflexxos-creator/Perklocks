"""ESPN public-API ingestor — shared base for NBA + NFL player DB.

ESPN's site.api endpoints are free, no-key, undocumented but stable
(used by espn.com itself). Returns clean JSON. We use:
  • /teams                         → list all teams in a league
  • /teams/{id}/roster             → roster per team
  • /teams/{id}/injuries           → current injury report
  • /athletes/{id}/overview        → season stats with labels

Cost: $0/month. Quota: untracked but generous (~10 req/sec sustained).

Both NBA and NFL ingestors call this module — they differ only in
the league slug and a couple of roster-shape quirks.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger("lockscore.player_db.espn")

_SITE_API = "https://site.api.espn.com/apis/site/v2/sports"
_WEB_API  = "https://site.web.api.espn.com/apis/common/v3/sports"

_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
_SEM = asyncio.Semaphore(20)

# Browser-ish UA — ESPN sometimes returns 403 on bare Python clients.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
    ),
    "Accept":     "application/json, text/plain, */*",
    "Referer":    "https://www.espn.com/",
}


def _canonical(name: str) -> str:
    return (name or "").strip().lower()


async def _get(client: httpx.AsyncClient, url: str, params: dict | None = None) -> dict | None:
    async with _SEM:
        try:
            r = await client.get(url, params=params, headers=_HEADERS, timeout=_TIMEOUT)
            if r.status_code == 200:
                return r.json()
            logger.debug("ESPN %s → HTTP %d", url, r.status_code)
        except Exception as e:
            logger.debug("ESPN %s exception: %s", url, e)
        return None


async def _fetch_teams(client: httpx.AsyncClient, sport_slug: str, league_slug: str) -> list[dict]:
    url = f"{_SITE_API}/{sport_slug}/{league_slug}/teams"
    data = await _get(client, url)
    if not data:
        return []
    sports = data.get("sports") or []
    if not sports:
        return []
    leagues = sports[0].get("leagues") or []
    if not leagues:
        return []
    # ESPN wraps each team in {"team": {...}}
    return [t.get("team") or {} for t in (leagues[0].get("teams") or [])]


def _flatten_roster(payload: dict, league_slug: str) -> list[dict]:
    """NBA returns `athletes: [player, ...]`. NFL returns
    `athletes: [{position:"offense", items:[...]}, {...}]`. Normalize
    both to a flat list of player dicts."""
    raw = payload.get("athletes") or []
    if not raw:
        return []
    # Heuristic: NFL grouped shape if items are dicts with "items" key
    if isinstance(raw[0], dict) and "items" in raw[0]:
        flat: list[dict] = []
        for group in raw:
            for ath in group.get("items") or []:
                # Tag the group bucket (e.g. "offense") so downstream
                # can use it as a coarse position class fallback.
                ath = {**ath, "_espn_group": group.get("position")}
                flat.append(ath)
        return flat
    return raw


async def _fetch_roster(
    client: httpx.AsyncClient, sport_slug: str, league_slug: str, team_id: int,
) -> list[dict]:
    url = f"{_SITE_API}/{sport_slug}/{league_slug}/teams/{team_id}/roster"
    data = await _get(client, url)
    return _flatten_roster(data or {}, league_slug)


async def _fetch_team_injuries(
    client: httpx.AsyncClient, sport_slug: str, league_slug: str, team_id: int,
) -> list[dict]:
    url = f"{_SITE_API}/{sport_slug}/{league_slug}/teams/{team_id}/injuries"
    data = await _get(client, url)
    return (data or {}).get("injuries") or []


async def _fetch_overview_stats(
    client: httpx.AsyncClient, sport_slug: str, league_slug: str, athlete_id: int,
) -> dict | None:
    """Season-aggregate stats for one athlete. Returns a normalised
    {label: value} mapping for the Regular-Season split."""
    url = f"{_WEB_API}/{sport_slug}/{league_slug}/athletes/{athlete_id}/overview"
    data = await _get(client, url)
    stats = (data or {}).get("statistics") or {}
    labels = stats.get("labels") or []
    splits = stats.get("splits") or []
    if not labels or not splits:
        return None
    # First split is the most recent regular season
    s0 = splits[0]
    values = s0.get("stats") or []
    out: dict[str, Any] = {}
    for i, lbl in enumerate(labels):
        if i < len(values):
            v = values[i]
            try:
                out[lbl] = float(v) if "." in str(v) else int(v)
            except (TypeError, ValueError):
                out[lbl] = v
    out["_split_display"] = s0.get("displayName")
    out["_season_label"]  = s0.get("season", {}).get("displayName") if isinstance(s0.get("season"), dict) else None
    return out


async def _ingest_one_player(
    client: httpx.AsyncClient,
    db: AsyncIOMotorDatabase,
    sport: str,                # "nba" / "nfl"
    sport_slug: str,           # "basketball" / "football"
    league_slug: str,          # "nba" / "nfl"
    team: dict,
    ath: dict,
    season: int,
) -> tuple[bool, str]:
    athlete_id = ath.get("id")
    name = ath.get("displayName") or ath.get("fullName") or ""
    if not athlete_id or not name:
        return False, "missing_player"
    try:
        athlete_id_int = int(athlete_id)
    except (TypeError, ValueError):
        return False, "bad_id"

    pos = ath.get("position") or {}
    pos_abbr = pos.get("abbreviation") or pos.get("displayName") or ath.get("_espn_group")

    # Headshot — ESPN serves consistent CDN URLs for athletes
    headshot = (ath.get("headshot") or {}).get("href")
    if not headshot:
        headshot = f"https://a.espncdn.com/i/headshots/{league_slug}/players/full/{athlete_id_int}.png"

    player_doc = {
        "sport": sport,
        "player_id": athlete_id_int,            # match legacy unique index
        "espn_id":   athlete_id_int,
        "name": name,
        "canonical_name": _canonical(name),
        "first_name": ath.get("firstName"),
        "last_name":  ath.get("lastName"),
        "jersey":     ath.get("jersey"),
        "team":       team.get("abbreviation") or team.get("shortDisplayName"),
        "team_id":    team.get("id"),
        "team_name":  team.get("displayName") or team.get("name"),
        "position":   pos_abbr,
        "height":     ath.get("displayHeight") or ath.get("height"),
        "weight_lb":  ath.get("weight"),
        "birth_date": ath.get("dateOfBirth"),
        "photo_url":  headshot,
        "status":     (ath.get("status") or {}).get("name", "Active"),
        "active":     (ath.get("status") or {}).get("type") in (None, "active"),
        "source":     "espn_public",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.players.update_one(
        {"sport": sport, "player_id": athlete_id_int},
        {"$set": player_doc, "$setOnInsert": {"created_at": datetime.now(timezone.utc).isoformat()}},
        upsert=True,
    )

    # Season stats — best-effort. If overview returns nothing (rookies
    # before debut, etc.) we just skip; players doc is still useful.
    stats = await _fetch_overview_stats(client, sport_slug, league_slug, athlete_id_int)
    if stats:
        await db.player_stats.update_one(
            {"sport": sport, "player_id": athlete_id_int, "season": season},
            {"$set": {
                "sport": sport,
                "player_id": athlete_id_int,
                "canonical_name": _canonical(name),
                "season": season,
                "stats": stats,
                "source": "espn_public",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }},
            upsert=True,
        )
    return True, "ok"


async def _ingest_team_injuries(
    client: httpx.AsyncClient,
    db: AsyncIOMotorDatabase,
    sport: str,
    sport_slug: str,
    league_slug: str,
    team: dict,
) -> int:
    """ESPN team injury endpoint returns a list of injury entries with
    an embedded athlete and status. Upsert into injuries collection."""
    injuries = await _fetch_team_injuries(client, sport_slug, league_slug, team["id"])
    n = 0
    for inj in injuries:
        ath = inj.get("athlete") or {}
        athlete_id = ath.get("id")
        name = ath.get("displayName") or ""
        if not athlete_id or not name:
            continue
        try:
            athlete_id_int = int(athlete_id)
        except (TypeError, ValueError):
            continue
        await db.injuries.update_one(
            {"sport": sport, "player_id": athlete_id_int},
            {"$set": {
                "sport": sport,
                "player_id": athlete_id_int,
                "canonical_name": _canonical(name),
                "name": name,
                "status": inj.get("status") or "questionable",
                "description": (inj.get("details") or {}).get("type")
                              or inj.get("shortComment")
                              or inj.get("longComment"),
                "team": team.get("abbreviation"),
                "source": "espn_public",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }},
            upsert=True,
        )
        n += 1
    return n


async def _refresh_league(
    db: AsyncIOMotorDatabase,
    *,
    sport: str,           # "nba" / "nfl"
    sport_slug: str,      # "basketball" / "football"
    league_slug: str,     # "nba" / "nfl"
    season: int | None = None,
) -> dict:
    season = season or datetime.now(timezone.utc).year
    started = time.time()
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        teams = await _fetch_teams(client, sport_slug, league_slug)
        if not teams:
            return {"ok": False, "reason": "teams_fetch_failed", "sport": sport}

        # Roster fetches in parallel
        rosters = await asyncio.gather(
            *[_fetch_roster(client, sport_slug, league_slug, int(t["id"])) for t in teams],
            return_exceptions=False,
        )

        # Player + season-stats upserts (bounded by _SEM inside _get)
        tasks = []
        for team, roster in zip(teams, rosters):
            for ath in roster:
                tasks.append(
                    _ingest_one_player(client, db, sport, sport_slug, league_slug, team, ath, season)
                )
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Injuries — separate pass per team
        inj_tasks = [
            _ingest_team_injuries(client, db, sport, sport_slug, league_slug, t)
            for t in teams
        ]
        inj_counts = await asyncio.gather(*inj_tasks, return_exceptions=True)

    ok      = sum(1 for r in results if isinstance(r, tuple) and r[0])
    errors  = sum(1 for r in results if isinstance(r, Exception))
    injured = sum(n for n in inj_counts if isinstance(n, int))

    await _ensure_indexes(db)

    summary = {
        "ok": True,
        "sport": sport,
        "season": season,
        "teams": len(teams),
        "players_upserted": ok,
        "injured": injured,
        "errors": errors,
        "elapsed_sec": round(time.time() - started, 1),
    }
    logger.info("ESPN %s player_db refresh: %s", sport.upper(), summary)
    return summary


async def _ensure_indexes(db: AsyncIOMotorDatabase) -> None:
    await db.players.create_index([("sport", 1), ("canonical_name", 1)])
    await db.players.create_index([("sport", 1), ("last_name", 1)])
    # Partial index: only enforce uniqueness when player_id is actually
    # set (the same collection is shared with legacy docs that may not
    # have one).
    await db.player_stats.create_index(
        [("sport", 1), ("player_id", 1), ("season", 1)],
        unique=True,
        partialFilterExpression={"player_id": {"$type": "number"}},
        name="sport_player_id_season_partial",
    )
    await db.injuries.create_index([("sport", 1), ("canonical_name", 1)])


# ────────────────────────────────────────────────────────────────────
# Public entry points — one per league
# ────────────────────────────────────────────────────────────────────
async def refresh_nba(db: AsyncIOMotorDatabase, season: int | None = None) -> dict:
    return await _refresh_league(
        db, sport="nba", sport_slug="basketball", league_slug="nba", season=season,
    )


async def refresh_nfl(db: AsyncIOMotorDatabase, season: int | None = None) -> dict:
    return await _refresh_league(
        db, sport="nfl", sport_slug="football", league_slug="nfl", season=season,
    )


async def refresh_cfb(db: AsyncIOMotorDatabase, season: int | None = None) -> dict:
    """College Football (FBS) via ESPN public. 130+ FBS teams, ~95
    players each. Heavier than NBA/NFL but still completes in ~3 min
    over the free endpoints."""
    return await _refresh_league(
        db,
        sport="cfb",
        sport_slug="football",
        league_slug="college-football",
        season=season,
    )


# ── WTA tennis (Phase 3.5) ─────────────────────────────────────────
# Sackmann's TML-Database mirror is ATP-only. ESPN exposes the WTA
# tour under sport=tennis, league=wta. Same /teams → /roster pattern
# does NOT apply (tennis has no teams), so we use the rankings feed
# as the player universe and enrich each via /athletes/{id}.
async def refresh_wta(db: AsyncIOMotorDatabase, season: int | None = None) -> dict:
    """WTA player DB via ESPN rankings + athlete endpoints.

    ESPN exposes:
      • /tennis/wta/rankings    → top ~150 WTA players in ranked order
      • Athletes include id, name, flag (URL with country code), DOB
    """
    started = time.time()
    season = season or datetime.now(timezone.utc).year
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        # Correct ESPN path: tennis/wta/rankings (NOT ?league=wta).
        data = await _get(
            client,
            f"{_SITE_API}/tennis/wta/rankings",
        )
        athletes: list[dict] = []
        if data:
            for rank_list in data.get("rankings", []):
                for entry in rank_list.get("ranks", []):
                    ath = entry.get("athlete") or {}
                    if ath.get("id"):
                        ath["_wta_rank"] = entry.get("current")
                        ath["_wta_points"] = entry.get("points")
                        athletes.append(ath)
        if not athletes:
            return {"ok": False, "reason": "wta_rankings_empty", "sport": "wta"}

        upserted = 0
        for ath in athletes:
            athlete_id = ath.get("id")
            name = ath.get("displayName") or ath.get("fullName") or ""
            try:
                aid = int(athlete_id)
            except (TypeError, ValueError):
                continue
            if not name:
                continue
            # Flag URL contains the 3-letter country code as the filename:
            #   .../countries/500/blr.png → BLR. Extract that for IOC.
            ioc = None
            flag_url = ath.get("flag")
            if isinstance(flag_url, str) and flag_url:
                try:
                    tail = flag_url.rsplit("/", 1)[-1].split(".")[0]
                    if len(tail) == 3 and tail.isalpha():
                        ioc = tail.upper()
                except Exception:
                    pass
            ioc = ioc or ath.get("birthCountry")
            # ESPN sometimes returns headshot as a dict {"href":"..."} and
            # other times as a plain URL string — handle both.
            _hs = ath.get("headshot")
            if isinstance(_hs, dict):
                photo = _hs.get("href")
            elif isinstance(_hs, str):
                photo = _hs
            else:
                photo = None
            photo = photo or f"https://a.espncdn.com/i/headshots/tennis/players/full/{aid}.png"
            player_doc = {
                "sport":          "wta",
                "tour":           "wta",
                "player_id":      str(aid),       # match tennis schema
                "espn_id":        aid,
                "name":           name,
                "canonical_name": _canonical(name),
                "first_name":     ath.get("firstName"),
                "last_name":      ath.get("lastName"),
                "ioc":            ioc,
                "hand":           (ath.get("hand") or {}).get("type") if isinstance(ath.get("hand"), dict) else None,
                "height":         ath.get("displayHeight") or ath.get("height"),
                "weight_lb":      ath.get("weight"),
                "birth_date":     ath.get("dateOfBirth"),
                "rank":           ath.get("_wta_rank"),
                "rank_points":    ath.get("_wta_points"),
                "photo_url":      photo,
                "source":         "espn_public",
                "updated_at":     datetime.now(timezone.utc).isoformat(),
            }
            await db.players.update_one(
                {"sport": "wta", "player_id": str(aid)},
                {"$set": player_doc, "$setOnInsert": {"created_at": datetime.now(timezone.utc).isoformat()}},
                upsert=True,
            )
            upserted += 1

    await _ensure_indexes(db)

    summary = {
        "ok": True,
        "sport": "wta",
        "season": season,
        "players_upserted": upserted,
        "elapsed_sec": round(time.time() - started, 1),
    }
    logger.info("ESPN WTA player_db refresh: %s", summary)
    return summary
