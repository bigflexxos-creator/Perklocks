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
    """Attach a small `injury_chip` field ONLY when there are active
    injuries relevant to today's game. Long-term IL entries and status
    entries older than 30 days are filtered out so the frontend chip
    doesn't flash on every card in the slate.
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

    def _is_recent(iso_date: str, max_age_days: int = 30) -> bool:
        if not iso_date:
            return True
        try:
            s = iso_date.replace("Z", "+00:00")
            d = datetime.fromisoformat(s)
            return (datetime.now(timezone.utc) - d).days <= max_age_days
        except Exception:
            return True

    def _active(injuries: list[dict]) -> list[dict]:
        out: list[dict] = []
        for inj in injuries or []:
            status = (inj.get("status") or "").strip().lower()
            # Long-term IL is not moving today's line.
            if "60-day" in status or "60 day" in status:
                continue
            if not any(k in status for k in
                       ("out", "doubt", "question", "day-to-day",
                        "day to day", "-day-il", " day il", "10-day",
                        "15-day", "7-day", "il")):
                continue
            desc = (inj.get("description") or "").lower()
            if any(bad in desc for bad in
                   ("season-ending", "season ending",
                    "suspended", "suspension", "retirement", "retired")):
                continue
            if not _is_recent(inj.get("date") or "", 30):
                continue
            out.append(inj)
        return out

    def _bucket(injuries: list[dict]) -> dict[str, int]:
        """Same tier map as espn_signal_engine._injury_tier so the chip
        counts and the analysis engine always agree."""
        out = {"out": 0, "doubtful": 0, "questionable": 0}
        for i in injuries:
            s = (i.get("status") or "").lower()
            if not s or "probab" in s:
                continue
            if "il" in s or "injured list" in s or "out" in s:
                out["out"] += 1
            elif "doubt" in s:
                out["doubtful"] += 1
            elif "question" in s or "day" in s:
                out["questionable"] += 1
        return out

    home_all = await get_team_injuries(db, sport, home) if home else []
    away_all = await get_team_injuries(db, sport, away) if away else []
    home_inj = _active(home_all)
    away_inj = _active(away_all)

    h_bucket = _bucket(home_inj)
    a_bucket = _bucket(away_inj)
    home_total = h_bucket["out"] + h_bucket["doubtful"] + h_bucket["questionable"]
    away_total = a_bucket["out"] + a_bucket["doubtful"] + a_bucket["questionable"]

    # If both sides have zero active injuries, don't attach a chip.
    # Keeps the card clean and prevents the "chip on every game" UX.
    if home_total == 0 and away_total == 0:
        return pick

    def score(b: dict[str, int]) -> int:
        return b["out"] * 3 + b["doubtful"] * 2 + b["questionable"]

    worst = None
    if score(h_bucket) > score(a_bucket):
        worst = "home"
    elif score(a_bucket) > score(h_bucket):
        worst = "away"

    def _is_out_tier(inj: dict) -> bool:
        s = (inj.get("status") or "").lower()
        if not s or "probab" in s:
            return False
        return "il" in s or "injured list" in s or "out" in s

    pick["injury_chip"] = {
        "home": h_bucket,
        "away": a_bucket,
        "worst_side": worst,
        "home_key_injuries": [i for i in home_inj if _is_out_tier(i)][:3],
        "away_key_injuries": [i for i in away_inj if _is_out_tier(i)][:3],
    }
    return pick
