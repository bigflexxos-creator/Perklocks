"""Deeper NBA pipeline trace — follow the actual precompute + emission flow."""
from __future__ import annotations
import asyncio
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

import httpx
import logging
logging.basicConfig(level=logging.WARNING)


async def main():
    api_key = os.environ.get("THE_ODDS_API_KEY") or ""

    # 1. Fetch events, then fetch per-event player prop markets
    print("STEP 1 — fetching NBA events + per-event prop markets")
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(
            f"https://api.the-odds-api.com/v4/sports/basketball_nba/events",
            params={"apiKey": api_key},
        )
        events = r.json() if r.status_code == 200 else []
    print(f"  /events: {len(events)}")

    if not events:
        return
    sample_eid = events[0]["id"]
    print(f"  sample event id: {sample_eid}  {events[0].get('home_team')} vs {events[0].get('away_team')}")

    # Fetch standard player props for this event
    prop_markets = "player_points,player_rebounds,player_assists,player_points_rebounds_assists,player_threes"
    alt_markets  = "player_points_alternate,player_rebounds_alternate,player_assists_alternate,player_points_rebounds_assists_alternate,player_threes_alternate"
    async with httpx.AsyncClient(timeout=20) as c:
        for label, mkts in [("STANDARD", prop_markets), ("ALT", alt_markets)]:
            r = await c.get(
                f"https://api.the-odds-api.com/v4/sports/basketball_nba/events/{sample_eid}/odds",
                params={"apiKey": api_key, "regions": "us", "markets": mkts, "oddsFormat": "american"},
            )
            print(f"\n  Event-props [{label}] status={r.status_code}")
            if r.status_code != 200:
                print(f"    body: {r.text[:300]}")
                continue
            d = r.json()
            bms = d.get("bookmakers") or []
            print(f"    bookmakers: {len(bms)}")
            if bms:
                # count unique markets + unique players
                all_mkts = set()
                all_players = set()
                sample_outcomes = []
                for b in bms[:3]:
                    for m in b.get("markets") or []:
                        all_mkts.add(m.get("key"))
                        for o in (m.get("outcomes") or [])[:2]:
                            all_players.add(o.get("description"))
                            if len(sample_outcomes) < 4:
                                sample_outcomes.append({
                                    "book": b.get("key"),
                                    "mk": m.get("key"),
                                    "player": o.get("description"),
                                    "line": o.get("point"),
                                    "side": o.get("name"),
                                    "price": o.get("price"),
                                })
                print(f"    unique markets: {sorted(all_mkts)}")
                print(f"    unique players (first 10): {list(all_players)[:10]}")
                print(f"    sample outcomes:")
                for s in sample_outcomes:
                    print(f"      {s}")

    # 2. Check what the sport pipeline sees — invoke candidate extractor directly
    print("\nSTEP 2 — Candidate extractor + precompute test")
    from sports_engine import _extract_nba_prop_candidates  # type: ignore
    # Need to pass an event payload — construct one from the raw calls
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(
            f"https://api.the-odds-api.com/v4/sports/basketball_nba/events/{sample_eid}/odds",
            params={"apiKey": api_key, "regions": "us",
                    "markets": prop_markets + "," + alt_markets,
                    "oddsFormat": "american"},
        )
        payload = r.json() if r.status_code == 200 else {}
    print(f"  payload bookmakers: {len(payload.get('bookmakers') or [])}")

    try:
        players, markets, lines = _extract_nba_prop_candidates(payload)
        print(f"  extracted: {len(players)} players, {len(markets)} markets")
        print(f"    players (first 10): {list(players)[:10]}")
        print(f"    markets: {markets}")
        print(f"    lines sample:")
        for k, v in list(lines.items())[:5]:
            print(f"      {k} -> {v}")
    except Exception as e:
        import traceback
        print(f"  extractor err: {e!r}")
        traceback.print_exc()
        return

    # 3. Run precompute_nba_prop_factors — see if gamelogs produce factors
    print("\nSTEP 3 — precompute_nba_prop_factors (feature engine)")
    from motor.motor_asyncio import AsyncIOMotorClient
    mongo_url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
    db_name = os.environ.get("DB_NAME", "lockscore_db")
    db = AsyncIOMotorClient(mongo_url)[db_name]

    from services.nba_feature_engine import precompute_nba_prop_factors  # type: ignore
    try:
        # Correct signature: (db, *, players, market_keys, lines_by_player_market)
        pre_wrap = await precompute_nba_prop_factors(
            db,
            players=list(players)[:5],
            market_keys=list(markets)[:3],
            lines_by_player_market=lines,
        )
        pre = pre_wrap.get("nba_precomputed", {})
        print(f"  precompute keys: {len(pre)}")
        if pre:
            for pl, mk_map in list(pre.items())[:3]:
                print(f"  {pl}:")
                for mk, data in mk_map.items():
                    f = data.get("factors") or {}
                    srcs = data.get("sources") or []
                    print(f"    {mk}: factors={len(f)}  sources={srcs}")
                    for fk, fv in list(f.items())[:5]:
                        print(f"       - {fk}={fv}")
        else:
            print("  (no precompute result — this is the blocker)")
    except Exception as e:
        import traceback
        print(f"  precompute err: {e!r}")
        traceback.print_exc()

    # 4. Check which NBA players have ANY recent gamelogs
    print("\nSTEP 4 — gamelog coverage for extracted players")
    sample_player = list(players)[0] if players else None
    if sample_player:
        from services.nba_feature_engine import _player_recent_logs  # type: ignore
        for pl in list(players)[:5]:
            try:
                rows = await _player_recent_logs(db, pl, limit=10)
                if rows:
                    r0 = rows[0]
                    print(f"  {pl}: {len(rows)} rows  last_game={r0.get('date') or r0.get('game_date')}  "
                          f"min={r0.get('minutes')!r} pts={r0.get('points')!r}")
                else:
                    print(f"  {pl}: 0 rows")
            except Exception as e:
                print(f"  {pl}: err {e}")

    # 5. Direct raw gamelog counts
    print("\nSTEP 5 — raw player_game_logs breakdown")
    for pl in list(players)[:3]:
        n_total = await db.player_game_logs.count_documents({
            "sport": "nba",
            "$or": [
                {"player_name": {"$regex": f"^{pl}$", "$options": "i"}},
                {"player_id": pl.lower().replace(" ", "-")},
            ],
        })
        print(f"  {pl}: total gamelogs = {n_total}")

    recent_any = await db.player_game_logs.find_one({"sport": "nba"}, sort=[("game_date", -1)])
    print(f"  Most recent NBA gamelog across all players: {recent_any.get('player_name') if recent_any else None} on {recent_any.get('game_date') if recent_any else None}")


if __name__ == "__main__":
    asyncio.run(main())
