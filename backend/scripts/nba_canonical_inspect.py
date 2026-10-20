"""Inspect NBA canonical player_game_actuals schema"""
import asyncio
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

async def main():
    from motor.motor_asyncio import AsyncIOMotorClient
    db = AsyncIOMotorClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]

    print("=== player_game_actuals sport=nba sample ===")
    docs = [d async for d in db.player_game_actuals.find({"sport": "nba"}).limit(5)]
    print(f"Count={len(docs)}")
    for d in docs:
        print(f"  Doc: {dict((k, str(v)[:60]) for k, v in d.items() if k != '_id')}")

    # Specifically find Cade Cunningham
    print("\n=== player_game_actuals Cade Cunningham ===")
    docs2 = [d async for d in db.player_game_actuals.find({
        "sport": "nba",
        "$or": [
            {"player_name": {"$regex": "cade cunningham", "$options": "i"}},
            {"name": {"$regex": "cade cunningham", "$options": "i"}},
        ],
    }).sort([("game_date", -1), ("date", -1)]).limit(3)]
    print(f"Count={len(docs2)}")
    for d in docs2:
        print(f"  Doc keys: {sorted(d.keys())}")
        print(f"  Doc: {dict((k, str(v)[:60]) for k, v in d.items() if k != '_id')}")

    # Try canonical_player_game_actuals
    print("\n=== canonical_player_game_actuals sport=nba sample ===")
    docs3 = [d async for d in db.canonical_player_game_actuals.find({"sport": "nba"}).limit(3)]
    print(f"Count={len(docs3)}")
    for d in docs3:
        print(f"  Doc keys: {sorted(d.keys())}")
        print(f"  Doc sample: {dict((k, str(v)[:60]) for k, v in d.items() if k != '_id')}")

    # Also canonical_player_game_logs
    print("\n=== canonical_player_game_logs sport=nba sample ===")
    docs4 = [d async for d in db.canonical_player_game_logs.find({"sport": "nba"}).limit(3)]
    print(f"Count={len(docs4)}")
    for d in docs4:
        print(f"  Doc keys: {sorted(d.keys())}")

    # Finally, look at legacy player_game_logs by player name
    print("\n=== player_game_logs by `player` field for Cade Cunningham ===")
    docs5 = [d async for d in db.player_game_logs.find({
        "sport": "nba",
        "player": {"$regex": "cade cunningham", "$options": "i"},
    }).sort([("date", -1)]).limit(3)]
    print(f"Count={len(docs5)}")
    for d in docs5:
        print(f"  Doc: {dict((k, str(v)[:60]) for k, v in d.items() if k != '_id')}")

asyncio.run(main())
