"""MAGIC 3E.a — Soccer lineup persistence + event-aware injury refresh.

Provides durable storage for lineup/injury/suspension evidence with
strict provenance, freshness, and NO uncontrolled polling.

Design (per user directive):
  * DB-first read path.  Adapters ALWAYS check the persisted store first
    and only trigger a provider fetch when the cached record is stale
    for the pick's pregame window.
  * Event-aware refresh triggers only when a soccer pick is on today's
    board within a configurable pregame window (default: matches
    starting in the next 24 h).
  * Refresh cadence:
      * Normal pregame (> 6 h to KO): 6-hour DB TTL
      * Close pregame (1-6 h to KO):  2-hour DB TTL
      * Post-kickoff:                 no refresh (final snapshot preserved)
  * Failure preserves the last-known-good record; adapter tags it STALE
    rather than deleting.

Collections written:
  * ``soccer_lineups``     — one doc per (event_id, team_id) with
                              ``starters``, ``bench``, ``formation``,
                              ``source``, ``source_timestamp``,
                              ``observed_at``, ``freshness_class``.
  * ``soccer_injuries``    — one doc per (league_slug, team_id) with
                              ``injuries`` list and ``updated_at``.

Statuses (from :class:`services.magic.gold_evidence_ext.LineupStatus`):
  CONFIRMED_STARTER, CONFIRMED_BENCH, PREDICTED_STARTER,
  PREDICTED_BENCH, QUESTIONABLE, OUT, SUSPENDED, UNKNOWN.

Providers used (both already wired elsewhere — no NEW keys required):
  * football-data.org  ``/matches/{id}``  → confirmed lineup / bench
                                             ~60 min pre-KO
  * ESPN core          ``/leagues/{slug}/injuries``  → per-league
                                             injury / suspension notes

The lineup adapter runs on-demand from `services.enrichment.lineups`
and calls back into this module to persist.  The injury scheduler is a
callable an operator can wire into the existing job registry — it is
NOT auto-scheduled at import time (per hard rule "no uncontrolled
polling").
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx

from services.magic.gold_evidence_ext import LineupStatus

logger = logging.getLogger("lockscore.soccer_lineup_injury_store")


# ═══════════════════════════════════════════════════════════════════
# LINEUP PERSISTENCE
# ═══════════════════════════════════════════════════════════════════
_FD_KEY = os.environ.get("FOOTBALL_DATA_ORG_KEY", "").strip()
_FD_MATCH_URL = "https://api.football-data.org/v4/matches/{matchId}"

_DEFAULT_LINEUP_TTL_MIN = 6 * 60   # 6 h normal pregame
_CLOSE_KICKOFF_TTL_MIN  = 2 * 60   # 2 h close pregame (≤ 6 h to KO)


async def get_or_refresh_lineup(
    db, *, event_id: str,
    match_id: Optional[int] = None,
    kickoff_iso: Optional[str] = None,
    force: bool = False,
) -> Optional[dict]:
    """Return the persisted lineup doc for the event, refreshing from
    the provider only when the persisted record is stale for the
    pick's kickoff window.

    NEVER polls after kickoff.  Never fabricates.
    """
    if not event_id:
        return None
    # 1. DB-first
    try:
        doc = await db.soccer_lineups.find_one({"event_id": event_id})
    except Exception:
        doc = None

    now = datetime.now(timezone.utc)
    kickoff_dt: Optional[datetime] = None
    if kickoff_iso:
        try:
            kickoff_dt = datetime.fromisoformat(
                str(kickoff_iso).replace("Z", "+00:00"))
            if kickoff_dt.tzinfo is None:
                kickoff_dt = kickoff_dt.replace(tzinfo=timezone.utc)
        except Exception:
            kickoff_dt = None

    # Determine TTL based on kickoff proximity.
    ttl_min = _DEFAULT_LINEUP_TTL_MIN
    stop_polling = False
    if kickoff_dt is not None:
        mins_to_ko = (kickoff_dt - now).total_seconds() / 60.0
        if mins_to_ko <= 0:
            stop_polling = True
        elif mins_to_ko <= 6 * 60:
            ttl_min = _CLOSE_KICKOFF_TTL_MIN

    fresh = False
    if doc:
        ts = doc.get("observed_at")
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age_min = (now - dt).total_seconds() / 60.0
            fresh = age_min <= ttl_min
        except Exception:
            fresh = False

    if fresh and not force:
        return doc

    # After-kickoff: preserve the final snapshot rather than refetching.
    if stop_polling and doc:
        return doc

    # 2. Provider fetch (only when needed + api key present).
    if not match_id or not _FD_KEY:
        # No path to a fresh fetch — return whatever we have (possibly
        # STALE downstream).
        return doc

    try:
        headers = {"X-Auth-Token": _FD_KEY}
        timeout = httpx.Timeout(8.0)
        async with httpx.AsyncClient(timeout=timeout, headers=headers) as cx:
            r = await cx.get(_FD_MATCH_URL.format(matchId=int(match_id)))
            if r.status_code != 200:
                logger.debug("football-data lineup fetch %s → %s",
                             match_id, r.status_code)
                return doc  # preserve last-known-good
            data = r.json() or {}
    except Exception as e:
        logger.debug("football-data lineup fetch raised: %s", e)
        return doc  # preserve last-known-good

    now_iso = now.isoformat()
    src_ts = (data.get("lastUpdated")
              or data.get("utcDate")
              or now_iso)
    # Match status governs CONFIRMED vs PREDICTED.
    status = str(data.get("status") or "").upper()
    is_confirmed = status in ("IN_PLAY", "PAUSED", "FINISHED",
                                "SUSPENDED", "AWARDED", "LIVE")
    # football-data.org exposes final lineup on / after kick-off; before
    # KO it may return an empty lineup or the "starting" placeholders.
    for side_key, side in (("homeTeam", "home"), ("awayTeam", "away")):
        team = data.get(side_key) or {}
        starters = team.get("lineup") or []
        bench    = team.get("bench")  or []
        team_id  = str(team.get("id") or "") or None
        team_name = team.get("name") or team.get("shortName") or ""
        starter_status = (LineupStatus.CONFIRMED_STARTER if is_confirmed
                           else (LineupStatus.PREDICTED_STARTER
                                 if starters else LineupStatus.UNKNOWN))
        bench_status = (LineupStatus.CONFIRMED_BENCH if is_confirmed
                         else (LineupStatus.PREDICTED_BENCH
                               if bench else LineupStatus.UNKNOWN))
        lineup_doc = {
            "event_id":         event_id,
            "team_id":          team_id,
            "team_name":        team_name,
            "side":             side,
            "starters":         [
                {"player_id": str(p.get("id") or ""),
                 "player_name": p.get("name") or "",
                 "position": p.get("position"),
                 "status": starter_status}
                for p in starters if p.get("name")
            ],
            "bench":            [
                {"player_id": str(p.get("id") or ""),
                 "player_name": p.get("name") or "",
                 "position": p.get("position"),
                 "status": bench_status}
                for p in bench if p.get("name")
            ],
            "formation":        team.get("formation"),
            "match_status":     status,
            "is_confirmed":     is_confirmed,
            "source":           "football_data_org",
            "source_timestamp": src_ts,
            "observed_at":      now_iso,
            "freshness_class":  "AVAILABLE" if is_confirmed else "PARTIAL",
        }
        try:
            await db.soccer_lineups.update_one(
                {"event_id": event_id, "team_id": team_id},
                {"$set": lineup_doc},
                upsert=True,
            )
        except Exception as e:
            logger.debug("lineup upsert failed for %s/%s: %s",
                         event_id, team_id, e)

    # Return the updated home-side doc.
    try:
        return await db.soccer_lineups.find_one({"event_id": event_id})
    except Exception:
        return doc


# ═══════════════════════════════════════════════════════════════════
# INJURY EVENT-AWARE REFRESH (per-league scoped, DB-first)
# ═══════════════════════════════════════════════════════════════════
_ESPN_LEAGUE_INJURY_URL = (
    "https://sports.core.api.espn.com/v2/sports/soccer/leagues/"
    "{slug}/teams/{team_id}/injuries?lang=en&region=us"
)
_ESPN_TEAMS_URL = (
    "https://sports.core.api.espn.com/v2/sports/soccer/leagues/"
    "{slug}/seasons/{year}/teams?limit=200"
)

# Injury refresh cadence — event-aware.
_INJURY_TTL_NORMAL_MIN = 12 * 60   # 12 h if no near-term event
_INJURY_TTL_MATCHDAY_MIN = 2 * 60  # 2  h if a team plays within 24 h


async def _leagues_with_upcoming_picks(db, *, hours_ahead: int = 24) -> list[str]:
    """Return the distinct league labels that have a soccer pick with
    an ``event_time`` within the next ``hours_ahead`` hours.
    """
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=hours_ahead)
    lo = now.isoformat()
    hi = horizon.isoformat()
    try:
        out: set[str] = set()
        async for r in db.picks.aggregate([
            {"$match": {"sport": "Soccer",
                         "event_time": {"$gte": lo, "$lt": hi}}},
            {"$group": {"_id": "$league"}},
        ]):
            v = r.get("_id")
            if v:
                out.add(str(v))
        return sorted(out)
    except Exception:
        return []


async def refresh_soccer_injuries_event_aware(
    db, *, hours_ahead: int = 24, dry_run: bool = False,
) -> dict:
    """Event-aware injury refresh.  Called by the operator / scheduler,
    NOT continuously.  Returns a summary counter dict.
    """
    leagues = await _leagues_with_upcoming_picks(
        db, hours_ahead=hours_ahead)
    counts = {"leagues_scanned": len(leagues),
              "teams_refreshed": 0,
              "injuries_persisted": 0,
              "provider_calls":   0,
              "cache_hits":       0,
              "errors":           0}
    if not leagues:
        return counts

    # Map user-facing league labels → ESPN slugs used by the injury API.
    from services.espn_live_soccer_rosters import LEAGUE_SLUGS
    label_to_slug = {label.lower(): slug
                     for slug, label in LEAGUE_SLUGS.items()}

    slugs: list[str] = []
    for lg in leagues:
        for lbl_prefix in (lg, lg.split(" · ")[0]):
            slug = label_to_slug.get(lbl_prefix.lower())
            if slug and slug not in slugs:
                slugs.append(slug)

    if dry_run or not slugs:
        counts["planned_slugs"] = slugs
        return counts

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0),
                                  headers={"User-Agent": "PerkLocks/1.0"}) as cx:
        for slug in slugs:
            # For each slug, get the team list once (cached in
            # espn_team_meta) and refresh injuries per team where the
            # persisted record is stale.
            try:
                year = now.year
                r = await cx.get(_ESPN_TEAMS_URL.format(slug=slug, year=year))
                counts["provider_calls"] += 1
                if r.status_code != 200:
                    counts["errors"] += 1
                    continue
                items = ((r.json() or {}).get("items") or [])
                team_refs = [it.get("$ref") for it in items if it.get("$ref")]
            except Exception:
                counts["errors"] += 1
                continue

            for ref in team_refs:
                # Resolve team id from ref URL.
                try:
                    team_id = ref.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
                except Exception:
                    continue
                # Skip if we already have a fresh injury doc.
                try:
                    doc = await db.soccer_injuries.find_one(
                        {"league_slug": slug, "team_id": team_id})
                except Exception:
                    doc = None
                stale = True
                if doc:
                    try:
                        ts = doc.get("updated_at")
                        dt = datetime.fromisoformat(
                            str(ts).replace("Z", "+00:00"))
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        age = (now - dt).total_seconds() / 60.0
                        stale = age > _INJURY_TTL_MATCHDAY_MIN
                    except Exception:
                        stale = True
                if not stale:
                    counts["cache_hits"] += 1
                    continue
                # Fetch injuries for this team.
                try:
                    url = _ESPN_LEAGUE_INJURY_URL.format(
                        slug=slug, team_id=team_id)
                    rr = await cx.get(url)
                    counts["provider_calls"] += 1
                    if rr.status_code != 200:
                        counts["errors"] += 1
                        continue
                    payload = rr.json() or {}
                except Exception:
                    counts["errors"] += 1
                    continue

                injuries: list[dict] = []
                for it in payload.get("items", []) or []:
                    ath = ((it.get("athlete") or {}).get("displayName")
                           or (it.get("athlete") or {}).get("fullName"))
                    if not ath:
                        continue
                    injuries.append({
                        "athlete":     ath,
                        "status":      it.get("status")
                                          or it.get("type"),
                        "description": (it.get("shortComment")
                                        or it.get("longComment")),
                        "date":        it.get("date"),
                    })
                # Persist even when the injury list is empty — that
                # itself is meaningful evidence (no injuries reported).
                try:
                    await db.soccer_injuries.update_one(
                        {"league_slug": slug, "team_id": team_id},
                        {"$set": {
                            "league_slug":  slug,
                            "team_id":      team_id,
                            "injuries":     injuries,
                            "source":       "espn_core",
                            "updated_at":   now_iso,
                        }},
                        upsert=True,
                    )
                    counts["teams_refreshed"] += 1
                    counts["injuries_persisted"] += len(injuries)
                except Exception:
                    counts["errors"] += 1

    return counts


__all__ = [
    "get_or_refresh_lineup",
    "refresh_soccer_injuries_event_aware",
]
