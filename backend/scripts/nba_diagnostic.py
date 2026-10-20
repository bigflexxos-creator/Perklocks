"""NBA Pipeline Diagnostic — Read-only funnel audit.

Prints real counts at every stage:
  Odds API events → candidates → precompute → model attempts →
  Lock Score distribution → published picks.

Does NOT mutate anything.
"""
from __future__ import annotations
import asyncio
import os
import sys
from datetime import datetime, timezone, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DIAG_RUN", "1")

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

import importlib  # noqa: E402

async def main():
    # 1. Snapshot current DB state for NBA
    from motor.motor_asyncio import AsyncIOMotorClient
    mongo_url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
    db_name = os.environ.get("DB_NAME", "lockscore_db")
    canonical_db_name = os.environ.get("CANONICAL_DB_NAME", "lockscore_canonical")
    cli = AsyncIOMotorClient(mongo_url)
    db = cli[db_name]
    cdb = cli[canonical_db_name]

    print("=" * 80)
    print("NBA PIPELINE DIAGNOSTIC — read-only snapshot")
    print("=" * 80)

    today_str = date.today().isoformat()
    print(f"today={today_str}")

    # Current pick counts in both DBs
    for name, dbi in [("lockscore_db", db), ("lockscore_canonical", cdb)]:
        for coll in ("picks", "canonical_picks"):
            try:
                total = await dbi[coll].count_documents({})
                nba   = await dbi[coll].count_documents({"sport": "NBA"})
                print(f"  {name}.{coll}: total={total}  NBA={nba}")
            except Exception as e:
                print(f"  {name}.{coll}: err {e}")

    # Gamelogs
    try:
        gl_total = await db["player_game_logs"].count_documents({"sport": "nba"})
        gl_recent = await db["player_game_logs"].count_documents({
            "sport": "nba",
            "game_date": {"$gte": "2026-01-01"},
        })
        print(f"  player_game_logs sport=nba: total={gl_total}  since_2026={gl_recent}")
    except Exception as e:
        print(f"  player_game_logs: err {e}")

    # 2. Odds API — fetch raw NBA events (standard + alt props)
    from services import odds_api_gateway  # type: ignore
    from sports_engine import PLAYER_PROP_MARKETS, SPORT_KEYS  # type: ignore

    odds_key = SPORT_KEYS["NBA"][0]
    print(f"\nOdds API key for NBA: {odds_key}")

    # Standard bulk odds (h2h, spreads, totals) via raw Odds API
    import httpx
    events = []
    api_key = os.environ.get("THE_ODDS_API_KEY") or ""
    url = f"https://api.the-odds-api.com/v4/sports/{odds_key}/odds"
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(url, params={
                "apiKey": api_key,
                "regions": "us",
                "markets": "h2h,spreads,totals",
                "oddsFormat": "american",
            })
            if r.status_code == 200:
                events = r.json() or []
            else:
                print(f"  Odds API HTTP {r.status_code}: {r.text[:200]}")
        print(f"  Odds API bulk events (h2h/spreads/totals): {len(events)}")
        if events:
            e0 = events[0]
            print(f"    sample event: {e0.get('home_team')} vs {e0.get('away_team')}  commence={e0.get('commence_time')}")
            print(f"    bookmakers: {len(e0.get('bookmakers') or [])}")
    except Exception as e:
        print(f"  Odds API bulk err: {e!r}")

    # Count prop markets
    prop_markets = PLAYER_PROP_MARKETS.get("NBA", [])
    std_markets = [m for m in prop_markets if "alternate" not in m]
    alt_markets = [m for m in prop_markets if "alternate" in m]
    print(f"  configured standard prop markets: {len(std_markets)} -> {std_markets[:6]}...")
    print(f"  configured alt prop markets:      {len(alt_markets)} -> {alt_markets[:6]}...")

    # 3. Run the actual NBA pipeline — fetch_nba_picks
    print("\n" + "=" * 80)
    print("Running fetch_nba_picks ...")
    print("=" * 80)
    try:
        import sports_engine
        importlib.reload(sports_engine)  # fresh counters
    except Exception:
        import sports_engine

    try:
        picks = await sports_engine.fetch_nba_picks(today_str)
        print(f"  fetch_nba_picks returned: {len(picks)} picks")
        if picks:
            # bucket by market, lock_score
            from collections import Counter
            by_mk = Counter(p.get("market", "?")[:40] for p in picks)
            print(f"  by market (top 10):")
            for mk, n in by_mk.most_common(10):
                print(f"    {n:4d}  {mk}")
            lock_scores = [float(p.get("lock_score") or 0) for p in picks]
            ge85 = sum(1 for s in lock_scores if s >= 85)
            ge80 = sum(1 for s in lock_scores if s >= 80)
            print(f"  Lock Score ≥85: {ge85}")
            print(f"  Lock Score ≥80: {ge80}")
            if lock_scores:
                print(f"  Lock Score max={max(lock_scores):.1f} mean={sum(lock_scores)/len(lock_scores):.1f} min={min(lock_scores):.1f}")

            for i, p in enumerate(picks[:5]):
                print(f"  Pick[{i}]: sport={p.get('sport')} market={p.get('market','')[:70]} "
                      f"player={p.get('player_name') or p.get('player')} "
                      f"team={p.get('player_current_team')} "
                      f"ls={p.get('lock_score')} wp={p.get('win_probability')}")
        else:
            print("  (0 picks returned)")
    except Exception as e:
        import traceback
        print(f"  fetch_nba_picks ERR: {e!r}")
        traceback.print_exc()

    # 4. Check funnel telemetry drop reasons
    print("\n" + "=" * 80)
    print("Funnel telemetry — NBA drop reasons (last 500)")
    print("=" * 80)
    try:
        from collections import Counter
        reasons = Counter()
        async for doc in db["funnel_telemetry"].find({"sport": "NBA"}).sort("_id", -1).limit(500):
            reasons[doc.get("reason") or doc.get("stage") or "?"] += 1
        for r, n in reasons.most_common(20):
            print(f"  {n:5d}  {r}")
        if not reasons:
            print("  (no funnel_telemetry entries for NBA)")
    except Exception as e:
        print(f"  funnel_telemetry err: {e!r}")

    print("\n" + "=" * 80)
    print("DONE")
    print("=" * 80)

if __name__ == "__main__":
    asyncio.run(main())
