"""P0-A/B/M — Raw-provider alt-ladder truth diagnostic for TB @ CIN.

Reads only:
  * db.odds_api_cache        — raw provider payload / timestamps
  * db.picks                  — canonical published rows
  * db.live_alt_lines (opt.)  — current alt-line snapshot store

Never reads broad backend logs.
"""
import asyncio
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta

sys.path.insert(0, "/app/backend")
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")
from motor.motor_asyncio import AsyncIOMotorClient  # noqa

TARGET_PLAYERS = {
    "Joe Burrow":       "passing_yards",
    "Baker Mayfield":   ("passing_yards", "rushing_yards"),
    "Ja'Marr Chase":    "receiving_yards",
    "Tee Higgins":      "receiving_yards",
    "Cade Otton":       "receiving_yards",
}


async def main():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = client[os.environ.get("DB_NAME", "test_database")]

    # 1) Locate the TB @ CIN raw provider payload(s) from odds_api_cache.
    print("=" * 78)
    print("STEP 1 — Locate TB @ CIN raw provider payloads")
    print("=" * 78)
    tb_cin_rows = []
    cache_cols = [c for c in await db.list_collection_names()
                   if "odds_api_cache" in c.lower()]
    print("cache collections:", cache_cols)
    for coll in cache_cols:
        async for r in db[coll].find({}, {
            "_id": 0, "event_id": 1, "home_team": 1, "away_team": 1,
            "cache_key": 1, "cached_at": 1, "sport_key": 1,
            "commence_time": 1, "market_key": 1, "payload": 1,
        }).limit(2000):
            h = str(r.get("home_team") or "")
            a = str(r.get("away_team") or "")
            ck = str(r.get("cache_key") or "")
            payload_head = str(r.get("payload") or "")[:200]
            hits = any(
                s in (h + a + ck + payload_head)
                for s in ("Bengals", "Buccaneers", "cincinnati", "tampa")
            )
            if hits:
                tb_cin_rows.append((coll, r))
    print(f"  found {len(tb_cin_rows)} tb/cin-related cache rows")
    for coll, r in tb_cin_rows[:8]:
        print(f"    coll={coll} event={r.get('event_id')} mkt={r.get('market_key')} "
              f"home={r.get('home_team')} away={r.get('away_team')} "
              f"cached={r.get('cached_at')} key={(r.get('cache_key') or '')[:60]}")

    # 2) Extract raw rungs per target player, per book.
    print()
    print("=" * 78)
    print("STEP 2 — Raw provider rungs per player/market/book")
    print("=" * 78)
    # ladder[(player, market_group, book)] = [(point, price, cache_ts, cache_key)]
    ladder = defaultdict(list)

    for coll, r in tb_cin_rows:
        # Fetch full payload
        full = await db[coll].find_one({"cache_key": r.get("cache_key")},
                                         {"_id": 0, "payload": 1,
                                          "cache_key": 1, "cached_at": 1,
                                          "market_key": 1, "event_id": 1,
                                          "home_team": 1, "away_team": 1})
        if not full:
            continue
        payload = full.get("payload")
        # payload can be JSON string or already-decoded list/dict
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                continue
        if not payload:
            continue
        cached_at = full.get("cached_at")
        cache_key = full.get("cache_key")

        # OddsAPI event schema: {bookmakers: [{key, markets: [{key, outcomes:[{name,price,point,description}]}]}]}
        def _iter_offers(node):
            if isinstance(node, dict):
                books = node.get("bookmakers") or []
                for b in books:
                    for m in (b.get("markets") or []):
                        for o in (m.get("outcomes") or []):
                            yield b.get("key"), m.get("key"), o
            elif isinstance(node, list):
                for item in node:
                    for x in _iter_offers(item):
                        yield x

        for book, mkey, out in _iter_offers(payload):
            desc = out.get("description") or out.get("participant") or out.get("name") or ""
            point = out.get("point")
            price = out.get("price")
            name = out.get("name") or ""
            # Consider only OVER outcomes on player-yards markets.
            if not name or str(name).lower() not in ("over", "under"):
                continue
            for pl_name, mkt in TARGET_PLAYERS.items():
                if pl_name.lower() not in desc.lower():
                    continue
                mkts = mkt if isinstance(mkt, tuple) else (mkt,)
                # Match by market_key suffix (pass_yds / rush_yds / reception_yds)
                mkey_l = (mkey or "").lower()
                fam = None
                if "pass" in mkey_l:      fam = "passing_yards"
                elif "rush" in mkey_l:    fam = "rushing_yards"
                elif "reception_yds" in mkey_l or "receiving_yds" in mkey_l:
                    fam = "receiving_yards"
                if fam not in mkts:
                    continue
                ladder[(pl_name, fam, book, str(name).lower())].append({
                    "point": point, "price": price,
                    "cached_at": str(cached_at), "cache_key": cache_key,
                    "market_key": mkey,
                })

    # Print aggregate summary
    for key, rungs in sorted(ladder.items()):
        pl, fam, book, side = key
        pts = sorted({r["point"] for r in rungs if r["point"] is not None})
        latest_by_pt = {}
        for r in rungs:
            pt = r["point"]
            if pt is None: continue
            if pt not in latest_by_pt or r["cached_at"] > latest_by_pt[pt]["cached_at"]:
                latest_by_pt[pt] = r
        print(f"\n  {pl} · {fam} · {book} · {side}")
        for pt in sorted(latest_by_pt):
            r = latest_by_pt[pt]
            print(f"      point={pt:<7} price={r['price']:<6} cached={r['cached_at']} mkey={r['market_key']}")

    # 3) Compare against currently-PUBLISHED picks for those players.
    print()
    print("=" * 78)
    print("STEP 3 — Currently-PUBLISHED canonical picks for the same players")
    print("=" * 78)
    since = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
    for pl_name in TARGET_PLAYERS:
        n = await db.picks.count_documents({
            "sport": "NFL", "created_at": {"$gte": since},
            "publication_state": "PUBLISHED",
            "selection": {"$regex": pl_name.replace("'", "."), "$options": "i"},
        })
        print(f"\n  {pl_name}: {n} published picks")
        async for p in db.picks.find({
            "sport": "NFL", "created_at": {"$gte": since},
            "publication_state": "PUBLISHED",
            "selection": {"$regex": pl_name.replace("'", "."), "$options": "i"},
        }, {"_id": 0, "market": 1, "selection": 1, "published_line": 1,
             "line": 1, "book_odds": 1, "sportsbook": 1, "is_alt_line": 1,
             "created_at": 1}).sort("published_line", 1).limit(30):
            print(f"    line={p.get('published_line') or p.get('line'):<7} "
                  f"odds={p.get('book_odds')!r:<6} "
                  f"book={p.get('sportsbook','?'):<10} "
                  f"alt={bool(p.get('is_alt_line'))}  "
                  f"{(p.get('market') or '?')[:60]}")


if __name__ == "__main__":
    asyncio.run(main())
