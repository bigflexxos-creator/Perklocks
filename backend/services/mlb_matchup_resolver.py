"""MLB matchup resolver — bridges Pick data → mlb_hitter_intel inputs.

The picks pipeline only has `market="Xavier Edwards Over 0.5 Hits"` and
`event="Arizona Diamondbacks @ Miami Marlins"` — no batter_id or pitcher_id.
This module resolves both via the free MLB Stats API schedule endpoint
(probable pitchers + venue + rosters).

Cached per (date, home, away) for 6 h so a 30-game slate makes ≤ 30
schedule fetches per refresh cycle.
"""
from __future__ import annotations
import logging
import re
import time
import unicodedata
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger("lockscore.mlb_matchup_resolver")
MLB_BASE = "https://statsapi.mlb.com/api/v1"


def _norm(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s.lower()).strip()


async def resolve_matchup(
    db,
    batter_name: str,
    event: str,
    event_time_iso: str,
) -> Optional[dict]:
    """Returns dict {batter_id, pitcher_id, ballpark, is_home, batter_team,
    pitcher_team, batting_order} or None.

    Uses MLB Stats API:
      * /schedule?date= → list of games with home/away IDs + venue + probable pitchers
      * /teams/{id}/roster?rosterType=active → batter ID lookup by name
    """
    if not batter_name or not event or "@" not in event:
        return None
    try:
        date_str = event_time_iso[:10]   # YYYY-MM-DD
    except Exception:
        return None
    away_name, home_name = [x.strip() for x in event.split("@", 1)]
    cache_key = f"mlb_matchup:{date_str}:{_norm(home_name)}:{_norm(away_name)}:{_norm(batter_name)}"
    try:
        cached = await db.mlb_matchup_resolver_cache.find_one({"_id": cache_key})
        if cached and (time.time() - (cached.get("ts") or 0)) < 6 * 3600:
            return cached.get("data")
    except Exception:
        pass

    # 2026-07-02 bug fix (user report: "vs pitcher not showing on Hits
    # cards"): MLB Stats API groups games by *local* game date, but our
    # pick's `event_time_iso` is in UTC. For games starting 7-11 PM ET
    # (e.g. late Twins @ Astros), the UTC ISO string bumps into the
    # NEXT calendar day (e.g. `2026-07-02T00:11:00Z` for a game that
    # MLB Stats indexes under 2026-07-01). Search a small window
    # around the target date so we find the game regardless of DST /
    # timezone drift.
    tried_dates = []
    try:
        base = datetime.fromisoformat(date_str)
        from datetime import timedelta
        window = [
            (base - timedelta(days=1)).date().isoformat(),
            date_str,
            (base + timedelta(days=1)).date().isoformat(),
        ]
    except Exception:
        window = [date_str]

    async with httpx.AsyncClient(timeout=12.0) as client:
        games: list[dict] = []
        for ds in window:
            tried_dates.append(ds)
            try:
                r = await client.get(f"{MLB_BASE}/schedule", params={
                    "sportId": 1, "date": ds,
                    "hydrate": "probablePitcher,venue,team",
                })
                r.raise_for_status()
                for d in r.json().get("dates", []):
                    games.extend(d.get("games", []))
            except Exception as e:
                logger.debug(f"schedule fetch fail {ds}: {e}")
                continue
        if not games:
            return None

        # Match game by team names (tolerant, DIRECTION-AGNOSTIC).
        # 2026-07-02 bug fix: Odds API and MLB Stats API sometimes
        # disagree on home/away ordering (e.g. Odds says "Milwaukee @
        # Cincinnati" but MLB Stats says home=Milwaukee). We now match
        # each side against BOTH candidates and detect the swap.
        target_home, target_away = _norm(home_name), _norm(away_name)
        game = None
        _swapped = False
        for g in games:
            h = _norm((g.get("teams") or {}).get("home", {}).get("team", {}).get("name", ""))
            a = _norm((g.get("teams") or {}).get("away", {}).get("team", {}).get("name", ""))
            straight = ((target_home in h or h in target_home)
                        and (target_away in a or a in target_away))
            reversed_ = ((target_home in a or a in target_home)
                         and (target_away in h or h in target_away))
            if straight:
                game = g; break
            if reversed_:
                game = g
                _swapped = True
                logger.info(
                    "mlb_matchup_resolver: home/away swap detected for '%s @ %s' — MLB API says home=%s, away=%s",
                    away_name, home_name, h, a,
                )
                break
        if not game:
            return None
        home_team = game.get("teams", {}).get("home", {})
        away_team = game.get("teams", {}).get("away", {})
        venue = (game.get("venue") or {}).get("name") or ""

        # Find which side has the batter via active rosters
        async def _roster(team_id):
            try:
                rr = await client.get(f"{MLB_BASE}/teams/{team_id}/roster",
                                       params={"rosterType": "active"})
                rr.raise_for_status()
                return rr.json().get("roster") or []
            except Exception:
                return []

        b_norm = _norm(batter_name)
        b_last = b_norm.split()[-1] if b_norm else ""
        home_team_id = (home_team.get("team") or {}).get("id")
        away_team_id = (away_team.get("team") or {}).get("id")
        found = None
        for side, tid, oppside in (("home", home_team_id, "away"), ("away", away_team_id, "home")):
            for p in await _roster(tid):
                full = _norm(p.get("person", {}).get("fullName") or "")
                if full == b_norm or (b_last and full.endswith(" " + b_last)):
                    found = (side, p.get("person", {}).get("id"))
                    break
            if found:
                break
        if not found:
            return None
        is_home_batter = found[0] == "home"
        batter_id = found[1]
        # Opposing starter
        opp_side = "away" if is_home_batter else "home"
        opp_block = game.get("teams", {}).get(opp_side, {})
        pp = opp_block.get("probablePitcher") or {}
        pitcher_id = pp.get("id")
        if not pitcher_id:
            return None

        result = {
            "batter_id": batter_id,
            "pitcher_id": pitcher_id,
            "ballpark": venue,
            "is_home": is_home_batter,
            "batter_team": (home_team if is_home_batter else away_team).get("team", {}).get("name"),
            "pitcher_team": opp_block.get("team", {}).get("name"),
            "pitcher_name": pp.get("fullName") or "",
            "batting_order": None,   # MLB doesn't publish until lineup card released
        }
        try:
            await db.mlb_matchup_resolver_cache.update_one(
                {"_id": cache_key},
                {"$set": {"ts": time.time(), "data": result}}, upsert=True,
            )
        except Exception:
            pass
        return result
