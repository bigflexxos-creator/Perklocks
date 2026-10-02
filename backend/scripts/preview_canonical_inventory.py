"""Preview-side canonical inventory — PHASE 1 (Preview half only).

Runs against the CURRENT Preview MongoDB (localhost pod-local).  Does
NOT touch Production.  Produces counts + canaries + a safe database
fingerprint so Phase 2 (Preview vs Production comparison) can be
performed OFFLINE by the operator against the Production inventory
they collect through Manage Publishes → Database.

This script is READ-ONLY.  It never writes to any collection.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

# Ensure backend/ is importable regardless of cwd.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(_HERE), ".env"))

from motor.motor_asyncio import AsyncIOMotorClient


MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME   = os.environ.get("DB_NAME", "lockscore_db")


def _fingerprint(mongo_url: str, db_name: str) -> str:
    return hashlib.sha256(f"{mongo_url}|{db_name}".encode()).hexdigest()[:12]


async def _count(coll, filt=None):
    try:
        if filt is None:
            return await coll.estimated_document_count()
        return await coll.count_documents(filt or {})
    except Exception as e:
        return f"ERROR:{type(e).__name__}:{str(e)[:60]}"


async def _latest(coll, filt, sort_field):
    try:
        docs = await coll.find(filt).sort(sort_field, -1).limit(1).to_list(1)
        if not docs:
            return None
        d = docs[0]
        v = d.get(sort_field)
        if isinstance(v, datetime):
            return v.replace(tzinfo=timezone.utc).isoformat() if v.tzinfo is None else v.isoformat()
        return str(v)
    except Exception as e:
        return f"ERROR:{type(e).__name__}:{str(e)[:60]}"


async def _distinct(coll, field, filt=None):
    try:
        vals = await coll.distinct(field, filt or {})
        return vals
    except Exception as e:
        return [f"ERROR:{type(e).__name__}"]


async def inventory() -> dict:
    client = AsyncIOMotorClient(MONGO_URL)
    db = client[DB_NAME]

    out: dict = {
        "generated_at":        datetime.now(timezone.utc).isoformat(),
        "environment":         "preview",
        "mongo_fingerprint":   _fingerprint(MONGO_URL, DB_NAME),
        "db_name":             DB_NAME,
        "mongo_host_class":    "local" if "localhost" in MONGO_URL or "127.0.0.1" in MONGO_URL else "remote",
    }

    # ── 1. picks ─────────────────────────────────────────────────────
    out["picks"] = {
        "total":                     await _count(db.picks),
        "current_published":         await _count(db.picks, {"publication_state": "PUBLISHED"}),
        "publication_state_dist":    {},
        "latest_event_time":         await _latest(db.picks, {}, "event_time"),
        "latest_published_at":       await _latest(db.picks, {}, "published_at"),
    }
    try:
        pipe = [{"$group": {"_id": "$publication_state", "n": {"$sum": 1}}}]
        async for d in db.picks.aggregate(pipe):
            out["picks"]["publication_state_dist"][str(d["_id"])] = d["n"]
    except Exception as e:
        out["picks"]["publication_state_dist"] = f"ERROR:{e}"

    # ── 2. board_revisions ───────────────────────────────────────────
    out["board_revisions"] = {
        "total":          await _count(db.board_revisions),
        "latest_revision": await _latest(db.board_revisions, {}, "revision"),
        "latest_ts":       await _latest(db.board_revisions, {}, "generated_at"),
    }

    # ── 3. games (by sport, current season) ─────────────────────────
    out["games"] = {}
    for sport in ("nfl", "cfb", "mlb", "nba", "nhl", "soccer", "tennis"):
        try:
            total = await _count(db.games, {"sport": sport})
            season_filter = {"sport": sport, "date": {"$gte": "2026-08-01"}}
            if sport == "nhl":
                season_filter = {"sport": sport, "date": {"$gte": "2026-10-01"}}
            if sport == "nba":
                season_filter = {"sport": sport, "date": {"$gte": "2026-10-01"}}
            if sport == "mlb":
                season_filter = {"sport": sport, "date": {"$gte": "2026-03-01"}}
            curr = await _count(db.games, season_filter)
            latest = await _latest(db.games, {"sport": sport}, "date")
            out["games"][sport] = {"total": total, "current_season": curr, "latest_event": latest}
        except Exception as e:
            out["games"][sport] = {"error": str(e)[:120]}

    # ── 4. player_game_logs ─────────────────────────────────────────
    out["player_game_logs"] = {}
    for sport in ("nfl", "cfb", "mlb", "nba", "nhl", "soccer"):
        try:
            total = await _count(db.player_game_logs, {"sport": sport})
            curr = await _count(db.player_game_logs,
                                 {"sport": sport, "date": {"$gte": "2026-01-01"}})
            latest = await _latest(db.player_game_logs, {"sport": sport}, "date")
            out["player_game_logs"][sport] = {
                "total": total, "current_season": curr, "latest": latest,
            }
        except Exception as e:
            out["player_game_logs"][sport] = {"error": str(e)[:120]}

    # ── 5. player_game_actuals ──────────────────────────────────────
    out["player_game_actuals"] = {}
    for sport in ("nfl", "cfb", "mlb", "nba", "nhl", "soccer", "tennis"):
        try:
            total = await _count(db.player_game_actuals, {"sport": sport})
            curr = await _count(db.player_game_actuals,
                                 {"sport": sport, "date": {"$gte": "2026-09-01"}})
            latest = await _latest(db.player_game_actuals, {"sport": sport}, "date")
            out["player_game_actuals"][sport] = {
                "total": total, "current_season": curr, "latest": latest,
            }
        except Exception as e:
            out["player_game_actuals"][sport] = {"error": str(e)[:120]}

    # ── 6. NFL-specific ─────────────────────────────────────────────
    out["nfl_specific"] = {
        "nfl_player_weekly": await _count(db.nfl_player_weekly),
    }

    # ── 7. Soccer specific ──────────────────────────────────────────
    out["soccer_specific"] = {
        "soccer_matches":            await _count(db.soccer_matches),
        "soccer_player_game_logs":   await _count(db.soccer_player_game_logs),
    }

    # ── 8. Tennis specific ──────────────────────────────────────────
    out["tennis_specific"] = {
        "tennis_matches_history":    await _count(db.tennis_matches_history),
    }

    # ── 9. canonical identities ─────────────────────────────────────
    try:
        out["identities"] = {
            "team_identity":   await _count(db.team_identities),
            "player_identity": await _count(db.player_identities),
        }
    except Exception as e:
        out["identities"] = {"error": str(e)[:120]}

    # ── 10. settlement ───────────────────────────────────────────────
    out["settlement"] = {}
    for status in ("HIT", "MISS", "PUSH", "VOID", "UNRESOLVED"):
        out["settlement"][status] = await _count(db.picks, {"settlement_status": status})

    # ── 11. canaries ────────────────────────────────────────────────
    out["canaries"] = {}
    try:
        laporta = await _count(db.player_game_actuals,
                                {"sport": "nfl", "player_name": {"$regex": "LaPorta", "$options": "i"}})
    except Exception:
        laporta = -1
    try:
        purdy = await _count(db.player_game_actuals,
                              {"sport": "nfl", "player_name": {"$regex": "Purdy", "$options": "i"}})
    except Exception:
        purdy = -1
    try:
        delaware = await _count(db.games, {
            "sport": "cfb", "date": {"$gte": "2026-08-01"},
            "$or": [{"home": {"$regex": "Delaware", "$options": "i"}},
                    {"away": {"$regex": "Delaware", "$options": "i"}}],
        })
    except Exception:
        delaware = -1
    try:
        mlb_post = await _count(db.games, {"sport": "mlb", "date": {"$gte": "2026-09-28"}})
    except Exception:
        mlb_post = -1
    try:
        nhl_hist = await _count(db.games, {"sport": "nhl"})
    except Exception:
        nhl_hist = -1
    out["canaries"] = {
        "sam_laporta_actuals":  laporta,
        "brock_purdy_actuals":  purdy,
        "delaware_2026_games":  delaware,
        "mlb_postseason_games": mlb_post,
        "nhl_history_games":    nhl_hist,
    }

    client.close()
    return out


async def main():
    inv = await inventory()
    print(json.dumps(inv, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
