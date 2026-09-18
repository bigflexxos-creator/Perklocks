"""P1.3 — normalize soccer match-level player actuals (Understat ingest) into
the history authority shape.  NO synthetic games: only stamps identity /
provenance fields onto rows that came from real match payloads.

  canonical_player_id  ← soccer_player_form.understat_id join (verified provider id)
  canonical_event_id   ← "understat:{match_id}"
  competition          ← league
  opponent_name        ← opponent_team_name (alias for the universal contract)
  provenance / data_as_of

Usage: python scripts/normalize_soccer_game_logs.py
"""
import asyncio, os, sys
sys.path.insert(0, "/app/backend")
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")
from motor.motor_asyncio import AsyncIOMotorClient
from datetime import datetime, timezone


async def main():
    db = AsyncIOMotorClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]
    logs = db.soccer_player_game_logs
    now = datetime.now(timezone.utc).isoformat()
    # understat_id → canonical_player_id map (verified provider ids only)
    id_map: dict[str, str] = {}
    async for ident in db.player_identities.find({"sport": "Soccer", "provider_ids.understat": {"$exists": True},
                                                  "canonical_player_id": {"$ne": None}},
                                                 {"_id": 0, "provider_ids.understat": 1, "canonical_player_id": 1}):
        uid = (ident.get("provider_ids") or {}).get("understat")
        if uid and ident.get("canonical_player_id"):
            id_map[str(uid)] = ident["canonical_player_id"]
    stamped_identity = stamped_meta = 0
    for uid, cid in id_map.items():
        r = await logs.update_many({"player_id": uid, "canonical_player_id": {"$exists": False}},
                                   {"$set": {"canonical_player_id": cid, "identity_source": "understat_id_verified"}})
        stamped_identity += r.modified_count
    # provenance / competition / canonical_event_id via pipeline update
    r = await logs.update_many(
        {"provenance": {"$exists": False}},
        [{"$set": {
            "competition": {"$ifNull": ["$competition", "$league"]},
            "opponent_name": {"$ifNull": ["$opponent_name", "$opponent_team_name"]},
            "canonical_event_id": {"$ifNull": ["$canonical_event_id", {"$concat": ["understat:", {"$toString": "$match_id"}]}]},
            "provenance": "understat_match_payload",
            "data_as_of": now,
        }}],
    )
    stamped_meta = r.modified_count
    for keys in ([("canonical_player_id", 1), ("match_date", -1)], [("name_canonical", 1), ("match_date", -1)]):
        try:
            await logs.create_index(keys, background=True)
        except Exception:
            pass  # index already present under another name
    total = await logs.count_documents({})
    with_cpid = await logs.count_documents({"canonical_player_id": {"$exists": True}})
    print(f"rows={total} identity_stamped_now={stamped_identity} with_canonical_id={with_cpid} meta_stamped_now={stamped_meta} id_map={len(id_map)}")

asyncio.run(main())
