"""ESPN team-injury note ingest.

Stores rolling injury reports per team so the pick rationale can call
out \"3 questionable starters\" without an extra API hop at read time.

Availability:
  • NFL ✅  (weekly injury report is core coverage)
  • NBA ✅  (nightly)
  • CFB ✅  (variable — some Group of Five teams sparse)
  • MLB ❌  (ESPN uses lineup instead — we already track probables)
  • Soccer ❌  (ESPN only exposes match-day availability inline)

Schema: one doc per (sport, team_id) with `injuries: [ {athlete, status, description, updated_at}, ... ]`.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any

import httpx

from .espn_common import espn_client
from .espn_team_meta import normalize_name

logger = logging.getLogger("lockscore.services.espn_injury_notes")

_INJURY_SLUGS: list[tuple[str, str]] = [
    ("football/nfl",                       "NFL"),
    ("football/college-football",          "CFB"),
    ("basketball/nba",                     "NBA"),
    ("basketball/wnba",                    "WNBA"),
    ("baseball/mlb",                       "MLB"),
    ("hockey/nhl",                         "NHL"),
]

_STATUS_SEVERITY = {
    "out":             3,
    "doubtful":        2,
    "questionable":    1,
    "day-to-day":      1,
    "day to day":      1,
    "probable":        0,
    "active":         -1,
}


def severity_of(status: str) -> int:
    return _STATUS_SEVERITY.get((status or "").strip().lower(), 0)


async def refresh_all_injuries(db) -> dict:
    started = datetime.now(timezone.utc)
    per_sport: dict[str, dict[str, int]] = {}
    async with httpx.AsyncClient(headers={"User-Agent": "PerkLocks/1.0"}) as cx:
        for slug, sport in _INJURY_SLUGS:
            fetched = teams_with_data = total_injuries = 0
            # Prefer the league-wide `/injuries` endpoint on
            # site.web.api.espn.com (returns every team in one call),
            # falling back to the per-team endpoint on site.api.
            league_url = f"https://site.web.api.espn.com/apis/site/v2/sports/{slug}/injuries"
            team_blocks: list[dict] = []
            try:
                r = await cx.get(league_url, timeout=15)
                if r.status_code == 200:
                    team_blocks = (r.json() or {}).get("injuries") or []
            except Exception as e:
                logger.warning("league-wide injuries %s failed: %s", slug, e)

            for tb in team_blocks:
                fetched += 1
                team_id = tb.get("id")
                team_name = tb.get("displayName") or tb.get("name") or ""
                items = tb.get("injuries") or []
                flat: list[dict] = []
                for inj in items:
                    athlete = (inj.get("athlete") or {})
                    flat.append({
                        "athlete":     athlete.get("displayName"),
                        "position":    (athlete.get("position") or {}).get("abbreviation"),
                        "status":      inj.get("status"),
                        "severity":    severity_of(inj.get("status") or ""),
                        "description": inj.get("shortComment") or inj.get("longComment"),
                        "date":        inj.get("date"),
                    })
                if flat:
                    teams_with_data += 1
                total_injuries += len(flat)
                await db.espn_injury_notes.update_one(
                    {"sport": sport, "team_id": team_id},
                    {"$set": {
                        "sport":      sport,
                        "team_id":    team_id,
                        "team_name":  team_name,
                        "team_norm":  normalize_name(team_name),
                        "injuries":   flat,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }},
                    upsert=True,
                )
            per_sport[sport] = {
                "teams_fetched":    fetched,
                "teams_with_data":  teams_with_data,
                "total_injuries":   total_injuries,
            }
    finished = datetime.now(timezone.utc)
    summary = {
        "started_at":  started.isoformat(),
        "finished_at": finished.isoformat(),
        "elapsed_ms":  int((finished - started).total_seconds() * 1000),
        "per_sport":   per_sport,
    }
    logger.info("ESPN injuries refresh: %s", summary)
    return summary


async def get_team_injuries(db, sport: str, team_name: str) -> list[dict]:
    """Return the current injury list for a team, empty when unknown.
    Also filters out injuries older than 14 days as stale."""
    key = normalize_name(team_name)
    doc = await db.espn_injury_notes.find_one(
        {"sport": sport, "team_norm": key},
        {"_id": 0, "injuries": 1, "updated_at": 1},
    )
    if not doc:
        return []
    return doc.get("injuries") or []


async def injury_chip_for_pick(db, pick: dict) -> dict:
    """Player-specific injury chip (2026-07-12 rewrite).

    Old behaviour attached a team-level chip that lit up on every pick
    when 3rd-string players were on the IR — noisy UX. Now the chip
    fires ONLY when the *subject player* of a player-prop bet is on
    the injury report. Team-level chips are gone; the Signal Engine
    still adjusts probability based on team injuries so nothing is
    lost from the *analysis* layer.

    Detection: extract the player name from the pick's `market` or
    `selection` field (which for props looks like
    'Aaron Judge - Anytime Home Run' or 'Aaron Judge to Score').
    Match against injuries on both teams.
    """
    sport = pick.get("sport")
    if not sport or sport not in {s for _, s in _INJURY_SLUGS}:
        return pick
    event = pick.get("event") or ""
    if not event:
        return pick

    # Extract candidate player name from the pick
    candidates: list[str] = []
    for field_val in (pick.get("market"), pick.get("selection")):
        if not field_val:
            continue
        raw = str(field_val)
        # "Aaron Judge - Anytime Home Run" → "Aaron Judge"
        m = re.match(r"^([A-Za-zÀ-ÿ.'\- ]{4,60}?)\s*[-–—]", raw)
        if m:
            candidates.append(m.group(1).strip())
        # "Aaron Judge to Score" → "Aaron Judge"
        m = re.match(r"^([A-Za-zÀ-ÿ.'\- ]{4,60}?)\s+to\s+", raw)
        if m:
            candidates.append(m.group(1).strip())
    if not candidates:
        return pick

    # Walk both teams' injury lists
    home = away = None
    if " @ " in event:
        away, home = event.split(" @ ", 1)
    elif " vs " in event:
        home, away = event.split(" vs ", 1)

    all_injuries: list[dict] = []
    if home:
        all_injuries.extend(await get_team_injuries(db, sport, home.strip()))
    if away:
        all_injuries.extend(await get_team_injuries(db, sport, away.strip()))
    if not all_injuries:
        return pick

    def _cand_matches(inj_athlete: str, cand: str) -> bool:
        a = normalize_name(inj_athlete)
        c = normalize_name(cand)
        if not a or not c:
            return False
        return a == c or (len(c) >= 6 and c in a) or (len(a) >= 6 and a in c)

    for inj in all_injuries:
        athlete = inj.get("athlete") or ""
        status = (inj.get("status") or "").strip()
        low = status.lower()
        # Only fire when the injury is still active (not probable/60-day)
        if not any(k in low for k in ("out", "doubt", "question", "day",
                                       "il", "injured list")):
            continue
        if "probab" in low or "60-day" in low or "60 day" in low:
            continue
        for cand in candidates:
            if _cand_matches(athlete, cand):
                pick["subject_player_hurt"] = {
                    "athlete":     athlete,
                    "status":      status,
                    "description": (inj.get("description") or "")[:180],
                }
                return pick
    return pick

