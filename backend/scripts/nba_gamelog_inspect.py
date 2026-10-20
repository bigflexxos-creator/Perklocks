"""Inspect NBA gamelog records — what fields do they have?"""
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

    print("=== Field analysis of player_game_logs sport=nba ===")
    pipeline = [
        {"$match": {"sport": "nba"}},
        {"$limit": 500},
    ]
    sample_docs = [doc async for doc in db.player_game_logs.aggregate(pipeline)]
    print(f"Sampled {len(sample_docs)}")

    if sample_docs:
        from collections import Counter
        all_keys = Counter()
        for d in sample_docs:
            for k in d.keys():
                all_keys[k] += 1
        print("Keys present in sample (freq):")
        for k, n in all_keys.most_common(40):
            print(f"  {n:4d}  {k}")

        print("\nFirst 2 sample docs (truncated):")
        for d in sample_docs[:2]:
            short = {k: str(v)[:60] for k, v in d.items() if k != "_id"}
            print(f"  {short}")

        # date distribution — look for any kind of date field
        date_fields = ["game_date", "date", "gameDate", "commence_time", "created_at", "fetched_at"]
        print("\nDate field distribution:")
        for df in date_fields:
            docs_with = sum(1 for d in sample_docs if d.get(df))
            if docs_with:
                vals = sorted({str(d.get(df))[:10] for d in sample_docs if d.get(df)})
                print(f"  {df}: {docs_with}/{len(sample_docs)} docs; range={vals[0]} .. {vals[-1]}")

    # Try a broader query — any gamelog with 2026 in a date-like field
    print("\n=== Searching for 2026 NBA gamelogs via multiple fields ===")
    for df in ["game_date", "date", "gameDate"]:
        try:
            cnt = await db.player_game_logs.count_documents({
                "sport": "nba",
                df: {"$regex": "^2026"},
            })
            if cnt:
                print(f"  sport=nba {df}~2026-*: {cnt}")
        except Exception:
            pass
        try:
            cnt = await db.player_game_logs.count_documents({
                "sport": "nba",
                df: {"$gte": "2026-01-01"},
            })
            if cnt:
                print(f"  sport=nba {df}>=2026-01-01: {cnt}")
        except Exception:
            pass

    # Also check canonical gamelog collection names
    print("\n=== Other possible NBA log collections ===")
    for name in await db.list_collection_names():
        if any(t in name.lower() for t in ("nba", "basketball", "gamelog", "player_game")):
            try:
                cnt = await db[name].count_documents({})
                if cnt:
                    print(f"  {name}: {cnt}")
            except Exception as e:
                print(f"  {name}: err {e}")

    # Check canonical_history_resolver route — this is used by feature engine
    print("\n=== Trying canonical_history_resolver ===")
    try:
        from services.canonical_history_resolver import read_player_recent  # type: ignore
        for p in ["Cade Cunningham", "Jayson Tatum", "LeBron James"]:
            rows = await read_player_recent(db, sport="nba", player_name=p, limit=10)
            print(f"  {p}: {len(rows) if rows else 0} rows via canonical resolver")
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"  canonical_history_resolver err: {e!r}")


asyncio.run(main())
