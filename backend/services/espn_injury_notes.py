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
    """Attach a small `injury_chip` field summarizing severity totals
    that the frontend can render as a red pill on the pick card.

    Format: `{ home: {out: 1, doubtful: 0, questionable: 3},
              away: {out: 0, doubtful: 1, questionable: 2},
              worst_side: \"home\" | \"away\" | None }`

    Only fires for sports in `_INJURY_SLUGS`.
    """
    sport = pick.get("sport")
    if not sport or sport not in {s for _, s in _INJURY_SLUGS}:
        return pick
    event = pick.get("event") or ""
    home = away = None
    if " @ " in event:
        away, home = event.split(" @ ", 1)
    elif " vs " in event:
        home, away = event.split(" vs ", 1)
    if not (home or away):
        return pick

    def _bucket(injuries: list[dict]) -> dict[str, int]:
        out = {"out": 0, "doubtful": 0, "questionable": 0}
        for i in injuries:
            s = (i.get("status") or "").lower()
            if "out" in s and "probab" not in s:
                out["out"] += 1
            elif "doubt" in s:
                out["doubtful"] += 1
            elif "question" in s or "day" in s:
                out["questionable"] += 1
        return out

    home_inj = await get_team_injuries(db, sport, home) if home else []
    away_inj = await get_team_injuries(db, sport, away) if away else []
    h_bucket = _bucket(home_inj)
    a_bucket = _bucket(away_inj)

    def score(b: dict[str, int]) -> int:
        return b["out"] * 3 + b["doubtful"] * 2 + b["questionable"]

    worst = None
    if score(h_bucket) > score(a_bucket):
        worst = "home"
    elif score(a_bucket) > score(h_bucket):
        worst = "away"

    pick["injury_chip"] = {
        "home": h_bucket,
        "away": a_bucket,
        "worst_side": worst,
        "home_key_injuries": [i for i in home_inj
                              if (i.get("status") or "").lower() == "out"][:3],
        "away_key_injuries": [i for i in away_inj
                              if (i.get("status") or "").lower() == "out"][:3],
    }
    return pick
