"""Soccer historical client — football-data.org v4 (free tier).

Free-tier limits:
  • 10 requests per minute
  • Current season + 1 prior season for top competitions
  • No detailed player game logs — only competition-level top scorers

We lean on the existing `soccer.client.SoccerAPI` wrapper which already
handles 429 retries, quota headers, and X-Auth-Token auth. This module
only needs to map football-data shapes into our historical schema.

What we ingest:
  • Competitions: top scorers (goals, assists, penalties, matches) →
    `season_totals` collection (sport='soccer').
  • Standings: team rolling form (W/D/L last 5) → `team_form`.
  • Fixtures (FINISHED): per-team last-5 goals scored / conceded → `team_form`.

What we DON'T fetch (intentionally — too expensive):
  • Per-player per-match logs — not on free tier.
  • Lineups / injuries — not on free tier.

Competitions covered (CURRENT_SEASON_COMPETITIONS): top European leagues
+ Champions League. Easy to add via env override `HIST_SOCCER_COMPS`.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger("lockscore.historical.soccer")

# Free tier covers these competitions. Codes are football-data.org codes.
_DEFAULT_COMPS = ["PL", "PD", "BL1", "SA", "FL1", "CL", "EC", "WC"]
# PL=Premier League, PD=La Liga, BL1=Bundesliga, SA=Serie A, FL1=Ligue 1,
# CL=Champions League, EC=Euro, WC=World Cup.

_PACE = 6.5  # seconds between requests = ~9 req/min, well under 10/min ceiling.


def _competitions() -> list[str]:
    env = os.environ.get("HIST_SOCCER_COMPS", "").strip()
    if env:
        return [c.strip().upper() for c in env.split(",") if c.strip()]
    return _DEFAULT_COMPS


async def _api():
    """Lazy-import the existing FootballDataClient wrapper."""
    from soccer.client import FootballDataClient
    return FootballDataClient()


async def backfill_current_season(db) -> dict:
    """Pull current-season scorers + standings for each tracked competition.

    Designed to be re-runnable. Each request is paced at ~6.5s to stay under
    the 10/min ceiling even on a noisy network.
    """
    api = await _api()
    comps = _competitions()
    scorers_inserted = teams_seen = 0
    errors: list[str] = []

    try:
        for comp in comps:
            # ── 1) Top scorers (season totals) ─────────────────────
            try:
                data = await api._request(f"/competitions/{comp}/scorers", {"limit": 100})
                season = ((data.get("season") or {}).get("startDate") or "")[:4] or str(datetime.utcnow().year)
                for s in data.get("scorers", []):
                    player = s.get("player") or {}
                    team = s.get("team") or {}
                    pid = player.get("id")
                    if not pid:
                        continue
                    await db.players.update_one(
                        {"player_id": f"fd_{pid}", "sport": "soccer"},
                        {"$set": {
                            "player_id": f"fd_{pid}",
                            "sport": "soccer",
                            "name": player.get("name"),
                            "team": team.get("name"),
                            "position": player.get("position"),
                            "nationality": player.get("nationality"),
                        }},
                        upsert=True,
                    )
                    await db.season_totals.update_one(
                        {"player_id": f"fd_{pid}", "sport": "soccer", "season": season, "competition": comp},
                        {"$set": {
                            "player_id": f"fd_{pid}",
                            "sport": "soccer",
                            "season": season,
                            "competition": comp,
                            "team": team.get("name"),
                            "name": player.get("name"),
                            "games": s.get("playedMatches") or s.get("matches"),
                            "goals": s.get("goals") or 0,
                            "assists": s.get("assists") or 0,
                            "penalties": s.get("penalties") or 0,
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        }},
                        upsert=True,
                    )
                    scorers_inserted += 1
            except Exception as e:
                errors.append(f"{comp} scorers: {str(e)[:100]}")
            await asyncio.sleep(_PACE)

            # ── 2) Standings (team form proxy) ─────────────────────
            try:
                std = await api._request(f"/competitions/{comp}/standings")
                for table_block in std.get("standings", []):
                    if table_block.get("type") != "TOTAL":
                        continue
                    for row in table_block.get("table", []):
                        team = row.get("team") or {}
                        tid = team.get("id")
                        if not tid:
                            continue
                        await db.team_form.update_one(
                            {"team_id": f"fd_{tid}", "sport": "soccer", "competition": comp},
                            {"$set": {
                                "team_id": f"fd_{tid}",
                                "sport": "soccer",
                                "competition": comp,
                                "name": team.get("name"),
                                "played": row.get("playedGames"),
                                "won": row.get("won"),
                                "drawn": row.get("draw"),
                                "lost": row.get("lost"),
                                "goals_for": row.get("goalsFor"),
                                "goals_against": row.get("goalsAgainst"),
                                "points": row.get("points"),
                                "form": row.get("form"),  # e.g. 'WWDLW'
                                "ppm": ((row.get("points") or 0) / max(1, row.get("playedGames") or 1)),
                                "updated_at": datetime.now(timezone.utc).isoformat(),
                            }},
                            upsert=True,
                        )
                        teams_seen += 1
            except Exception as e:
                errors.append(f"{comp} standings: {str(e)[:100]}")
            await asyncio.sleep(_PACE)
    finally:
        try:
            await api.aclose()
        except Exception:
            pass

    return {
        "competitions": comps,
        "scorer_rows_upserted": scorers_inserted,
        "team_rows_upserted": teams_seen,
        "errors": errors[:10],
    }


async def incremental_sync(db, since: Optional[datetime] = None) -> dict:
    """Soccer doesn't expose per-player per-match logs cheaply on free tier.

    For incremental updates we just re-pull the top-scorer leaderboards once
    every 24h (this is what `since` gates against in the orchestrator). The
    leaderboards already contain cumulative season totals, so this is enough
    to keep `season_totals` fresh.
    """
    if since is not None and (datetime.now(timezone.utc) - since) < timedelta(hours=20):
        return {"skipped": "recent sync", "since": since.isoformat()}
    return await backfill_current_season(db)
