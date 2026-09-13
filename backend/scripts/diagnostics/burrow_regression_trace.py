"""P0-A + P0-B: Locate historical Burrow LS95 pick(s) and replay
identical inputs through the CURRENT scorer to isolate any
authority regression."""
from __future__ import annotations
import asyncio, os, sys, pathlib, json
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
load_dotenv()


async def main():
    c = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = c[os.environ.get("DB_NAME", "lockscore_db")]

    # ── P0-A: highest historical Burrow rows across ALL time ─────
    q = {"sport": "NFL", "$or": [
        {"selection": {"$regex": "Burrow", "$options": "i"}},
        {"player":    {"$regex": "Burrow", "$options": "i"}},
        {"player_name": {"$regex": "Burrow", "$options": "i"}},
        {"market":    {"$regex": "Burrow", "$options": "i"}},
    ]}
    rows = await db.picks.find(q).sort("lock_score", -1).limit(15).to_list(None)
    print(f"=== TOP 15 Burrow ROWS ALL-TIME (n_query_matches={len(rows)}) ===")
    for i, r in enumerate(rows):
        print(f"{i+1}. LS={r.get('lock_score')} WP={r.get('win_probability')} "
              f"pick_date={r.get('pick_date')} v={r.get('lock_score_version')}")
        print(f"    market={r.get('market')} book_odds={r.get('book_odds')} line={r.get('line')}")
        print(f"    nfl_prop_authority_applied={r.get('nfl_prop_authority_applied')} "
              f"authority_ceiling={r.get('nfl_prop_authority_ceiling')} "
              f"mult={r.get('nfl_prop_authority_evidence_mult')}")
        print(f"    bq_ceiling={(r.get('bet_quality_authority') or {}).get('ceiling')}")
        print(f"    factors_keys={list((r.get('factors') or {}).keys())[:10]}")

    # ── P1 raw provider for Burrow AND Dak: enumerate every observed
    #     passing-yard sample from provider caches
    print("\n=== RAW PROVIDER SAMPLES — pass yards for Burrow / Dak ===")
    # 1. odds_api_cache
    if "odds_api_cache" in await db.list_collection_names():
        for qb in ("Burrow", "Prescott"):
            n = 0
            async for entry in db.odds_api_cache.find({}).limit(500):
                raw = entry.get("data") or entry.get("payload") or entry.get("raw")
                if isinstance(raw, dict):
                    raw = json.dumps(raw)
                if isinstance(raw, str) and qb in raw and "pass" in raw.lower():
                    n += 1
                    if n <= 2:
                        # try to extract lines
                        import re
                        lines = re.findall(
                            rf'"description":\s*"[^"]*{qb}[^"]*".*?"market_key":\s*"(player_pass_yds[a-z_]*)".*?"point":\s*([0-9.]+).*?"price":\s*(-?\d+)',
                            raw[:60000], flags=re.S)
                        print(f'  cache hits for {qb}: sample market/thresholds: {lines[:20]}')
            print(f'  {qb}: total odds_api_cache entries mentioning: {n}')

    # ── P1-B: current passing-yard alts for Burrow / Dak ─────────
    print("\n=== CURRENT DB Burrow / Dak PASSING-YARD ROWS ===")
    for qb in ("Burrow", "Prescott"):
        rows = await db.picks.find({
            "sport": "NFL",
            "market": {"$regex": rf"{qb}.*(?:pass yds|passing yards|Player Pass)", "$options": "i"},
        }).to_list(None)
        thresholds = sorted({r.get("line") for r in rows if r.get("line") is not None})
        pub = sum(1 for r in rows if r.get("published_at") or r.get("published_lock_score"))
        print(f'  {qb}: rows={len(rows)} published={pub} thresholds={thresholds}')
        for r in sorted(rows, key=lambda x: x.get("line") or 0):
            print(f'    line={r.get("line")!s:>7} LS={r.get("lock_score")} WP={r.get("win_probability")} '
                  f'book_odds={r.get("book_odds")} bq_applied={r.get("nfl_prop_authority_applied")}')

    # ── P2: MLB 90+ visibility trace ──────────────────────────
    print("\n=== P2 — MLB 90+ TODAY VISIBILITY TRACE ===")
    from services.perklocks_day import current_slate_day
    today = current_slate_day()
    mlb90 = await db.picks.find({
        "sport": "MLB", "pick_date": today,
        "lock_score": {"$gte": 90.0}
    }).to_list(None)
    print(f'  MLB 90+ DB rows: {len(mlb90)}')
    for r in mlb90[:10]:
        print(f'    LS={r.get("lock_score")} mkt={r.get("market")} '
              f'pub_state={r.get("publication_state")} pub_at={r.get("published_at")} '
              f'off_board={r.get("off_board")} evt={r.get("event_time")}')


asyncio.run(main())
