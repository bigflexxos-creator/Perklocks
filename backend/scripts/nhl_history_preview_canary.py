"""Preview read-only canary — prove the newly wired NHL Historical
Intelligence adapters surface real existing Preview NHL rows.

Strict read-only.  Never writes.  Honours Preview authority flags.
Prints a short JSON summary so an operator can paste the output
straight into the final acceptance report.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))

from motor.motor_asyncio import AsyncIOMotorClient
from services.historical_intelligence import (
    HistoricalQuery,
    NHLPlayerHistoricalAdapter,
    NHLTeamHistoricalAdapter,
)


MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME   = os.environ.get("DB_NAME", "lockscore_db")


async def _pick_player(db) -> dict | None:
    # Prefer canonical actuals; fall back to legacy logs.
    doc = await db.player_game_actuals.find_one({"sport": "nhl"})
    if doc:
        return {
            "source": "player_game_actuals",
            "canonical_player_id": doc.get("canonical_player_id"),
            "name":  doc.get("player_name") or doc.get("name"),
        }
    doc = await db.player_game_logs.find_one({"sport": "nhl"})
    if doc:
        return {
            "source": "player_game_logs",
            "canonical_player_id": doc.get("player_id"),
            "name":  doc.get("name"),
        }
    return None


async def _pick_team(db) -> dict | None:
    doc = await db.team_game_actuals.find_one({"sport": "nhl"})
    if doc:
        return {"source": "team_game_actuals",
                "team": doc.get("canonical_team_id")}
    doc = await db.games.find_one(
        {"sport": "nhl", "status": "Final",
         "home": {"$nin": [None, ""]}},
    )
    if doc:
        return {"source": "games", "team": doc.get("home")}
    return None


async def main():
    client = AsyncIOMotorClient(MONGO_URL)
    db = client[DB_NAME]
    out: dict = {
        "environment":                os.environ.get("DATA_AUTHORITY", "preview"),
        "canonical_write_enabled":    os.environ.get("CANONICAL_WRITE_ENABLED", "false"),
        "background_workers_enabled": os.environ.get("BACKGROUND_WORKERS_ENABLED", "false"),
        "mongo_fingerprint_db":       DB_NAME,
        "preview_counts": {
            "player_game_actuals_nhl": await db.player_game_actuals.count_documents({"sport": "nhl"}),
            "player_game_logs_nhl":    await db.player_game_logs.count_documents({"sport": "nhl"}),
            "team_game_actuals_nhl":   await db.team_game_actuals.count_documents({"sport": "nhl"}),
            "games_nhl":               await db.games.count_documents({"sport": "nhl"}),
        },
    }

    p_adapter = NHLPlayerHistoricalAdapter()
    t_adapter = NHLTeamHistoricalAdapter()

    out["player_canary"] = {}
    pseed = await _pick_player(db)
    if pseed is None:
        out["player_canary"] = {"status": "NO_NHL_PLAYER_SOURCE_ROW_FOUND"}
    else:
        for family in ("goals", "assists", "points", "shots_on_goal"):
            q = HistoricalQuery(
                sport="NHL", entity_type="player",
                entity_id=pseed["canonical_player_id"],
                entity_name=pseed.get("name"),
                market_family=family,
                sample_scope="L10",
            )
            try:
                obs = await p_adapter.fetch_observations(db, q)
            except Exception as e:
                out["player_canary"][family] = {"status": "ADAPTER_ERROR", "err": str(e)[:200]}
                continue
            out["player_canary"][family] = {
                "status":           "AVAILABLE" if obs else "AVAILABLE_EMPTY",
                "observations":     len(obs),
                "l5":               [o.actual for o in obs[:5]],
                "l10":              [o.actual for o in obs[:10]],
                "l20":              [o.actual for o in obs[:20]],
                "provenance_set":   sorted({o.provenance for o in obs if o.provenance}),
                "sample_source":    pseed["source"],
                "player_id":        pseed["canonical_player_id"],
                "player_name":      pseed.get("name"),
            }

    out["team_canary"] = {}
    tseed = await _pick_team(db)
    if tseed is None:
        out["team_canary"] = {"status": "NO_NHL_TEAM_SOURCE_ROW_FOUND"}
    else:
        for family in ("moneyline", "puck_line", "total"):
            q = HistoricalQuery(
                sport="NHL", entity_type="team",
                entity_id=tseed["team"], market_family=family,
            )
            try:
                obs = await t_adapter.fetch_observations(db, q)
            except Exception as e:
                out["team_canary"][family] = {"status": "ADAPTER_ERROR", "err": str(e)[:200]}
                continue
            home_obs = [o for o in obs if o.home_away == "home"]
            away_obs = [o for o in obs if o.home_away == "away"]
            out["team_canary"][family] = {
                "status":        "AVAILABLE" if obs else "AVAILABLE_EMPTY",
                "observations":  len(obs),
                "home_count":    len(home_obs),
                "away_count":    len(away_obs),
                "l5":            [o.actual for o in obs[:5]],
                "l10":           [o.actual for o in obs[:10]],
                "sample_source": tseed["source"],
                "team":          tseed["team"],
            }

    out["generated_at"] = datetime.now(timezone.utc).isoformat()
    print(json.dumps(out, indent=2, default=str))
    client.close()


if __name__ == "__main__":
    asyncio.run(main())
