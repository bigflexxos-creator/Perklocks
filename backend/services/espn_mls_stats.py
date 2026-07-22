"""ESPN MLS stat leaders ingest.

Uses ESPN's public `sports.core.api.espn.com/v2` JSON endpoint for MLS
season leaders (goals + assists). No HTML scraping, no Cloudflare
challenges. Stores results in `espn_mls_stats` MongoDB collection.

Consumed by `services/mls_scorer_gate.is_mls_scorer_pick_ok()` to
hard-gate MLS Anytime Goal Scorer / SoA / First Goal Scorer picks
so book-priced reserves can't surface as Elite Locks over real starters.

Refresh cadence: every 12h (loop armed in server.py startup).

Data shape stored:
  { "_id": "<espn_player_id>",
    "name": "Lionel Messi", "name_norm": "lionel messi",
    "team_espn_id": "20232", "team": "Inter Miami CF",
    "games": 28, "goals": 29, "assists": 20,
    "season": 2025, "refreshed_at": ISO }
"""
from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger("lockscore.espn_mls_stats")

_LEADERS_URL = (
    "https://sports.core.api.espn.com/v2/sports/soccer/leagues/usa.1/"
    "seasons/{year}/types/1/leaders?limit=50"
)
# Team endpoint used to resolve team_id -> team_name.
_TEAM_URL = (
    "https://sports.core.api.espn.com/v2/sports/soccer/leagues/usa.1/"
    "seasons/{year}/teams/{team_id}?lang=en&region=us"
)
_ATHLETE_URL_TMPL = (
    "https://sports.core.api.espn.com/v2/sports/soccer/leagues/usa.1/"
    "seasons/{year}/athletes/{aid}?lang=en&region=us"
)

# Parse "M: 34, G: 24: A: 5" style stat blobs.
_STAT_RE = re.compile(r"M:\s*(\d+),\s*G:\s*(\d+)(?::\s*A:\s*(\d+))?")


def _norm(name: str) -> str:
    if not name:
        return ""
    nk = unicodedata.normalize("NFKD", name)
    return "".join(c for c in nk if not unicodedata.combining(c)).lower().strip()


def _extract_id(url: str) -> Optional[str]:
    m = re.search(r"/(?:athletes|teams)/(\d+)", url or "")
    return m.group(1) if m else None


async def _get_json(cx: httpx.AsyncClient, url: str) -> Optional[dict]:
    try:
        r = await cx.get(url)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        logger.debug("ESPN GET %s failed: %s", url, e)
    return None


async def refresh_mls_leaders(season: int = 2025) -> dict:
    """Fetch ESPN's MLS season leaders + resolve player/team names.

    Returns a diagnostic dict. Upserts every leader-appearing player
    (goals or assists categories) into `espn_mls_stats` collection.
    """
    from deps import db

    url = _LEADERS_URL.format(year=season)
    async with httpx.AsyncClient(timeout=15) as cx:
        blob = await _get_json(cx, url)
        if not blob:
            return {"ok": False, "reason": "leaders_fetch_failed"}

        cats = blob.get("categories", [])
        # Merge goalsLeaders + assistsLeaders into one dict keyed by aid.
        merged: dict[str, dict] = {}
        for cat in cats:
            if cat.get("name") not in ("goalsLeaders", "assistsLeaders"):
                continue
            for entry in cat.get("leaders", []):
                aid = _extract_id((entry.get("athlete") or {}).get("$ref") or "")
                tid = _extract_id((entry.get("team") or {}).get("$ref") or "")
                if not aid:
                    continue
                sdv = entry.get("shortDisplayValue") or ""
                m = _STAT_RE.search(sdv)
                games = int(m.group(1)) if m else 0
                goals = int(m.group(2)) if m and m.group(2) else 0
                assists = int(m.group(3)) if m and m.group(3) else 0
                rec = merged.setdefault(aid, {
                    "aid": aid, "tid": tid,
                    "games": 0, "goals": 0, "assists": 0,
                })
                rec["games"] = max(rec["games"], games)
                rec["goals"] = max(rec["goals"], goals)
                rec["assists"] = max(rec["assists"], assists)
                if tid:
                    rec["tid"] = tid

        if not merged:
            return {"ok": False, "reason": "no_leaders_parsed"}

        # Resolve names in parallel (bounded concurrency).
        sem = asyncio.Semaphore(8)

        async def _resolve_ath(aid: str) -> tuple[str, Optional[str]]:
            async with sem:
                data = await _get_json(cx, _ATHLETE_URL_TMPL.format(year=season, aid=aid))
                if not data:
                    return aid, None
                return aid, (data.get("fullName")
                             or data.get("displayName")
                             or data.get("name"))

        async def _resolve_team(tid: str) -> tuple[str, Optional[str]]:
            async with sem:
                data = await _get_json(cx, _TEAM_URL.format(year=season, team_id=tid))
                if not data:
                    return tid, None
                return tid, (data.get("displayName")
                             or data.get("name")
                             or data.get("shortDisplayName"))

        names_task = asyncio.gather(*[_resolve_ath(aid) for aid in merged.keys()])
        team_ids = {r["tid"] for r in merged.values() if r.get("tid")}
        teams_task = asyncio.gather(*[_resolve_team(tid) for tid in team_ids])
        name_pairs, team_pairs = await asyncio.gather(names_task, teams_task)

        names_by_id = {aid: nm for aid, nm in name_pairs if nm}
        teams_by_id = {tid: tm for tid, tm in team_pairs if tm}

    # Prepare upserts.
    now = datetime.now(timezone.utc).isoformat()
    docs = []
    for aid, rec in merged.items():
        nm = names_by_id.get(aid)
        if not nm:
            continue
        docs.append({
            "_id": aid,
            "player_id": aid,
            "name": nm,
            "name_norm": _norm(nm),
            "team": teams_by_id.get(rec.get("tid"), ""),
            "team_espn_id": rec.get("tid") or "",
            "games": rec["games"],
            "goals": rec["goals"],
            "assists": rec["assists"],
            "season": season,
            "refreshed_at": now,
        })
    if not docs:
        return {"ok": False, "reason": "no_named_players"}

    from pymongo import ReplaceOne
    await db.espn_mls_stats.bulk_write(
        [ReplaceOne({"_id": d["_id"]}, d, upsert=True) for d in docs],
        ordered=False,
    )
    logger.info(
        "ESPN MLS stats refresh: %d players (season=%d)", len(docs), season,
    )
    return {"ok": True, "players": len(docs), "season": season}


async def load_gate_snapshot() -> tuple[dict[str, dict], set[str]]:
    """Return (by_name_norm, name_set) for the scorer gate."""
    from deps import db
    docs = await db.espn_mls_stats.find({}).to_list(length=500)
    by = {d["name_norm"]: d for d in docs if d.get("name_norm")}
    names = set(by.keys())
    return by, names
