"""Trace NBA precompute status."""
import asyncio, os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))
os.environ['LS_INIT_LITE'] = '1'

async def main():
    from services.database import get_database
    db = get_database()
    # Check what's in player_game_logs for NBA
    cnt = await db.player_game_logs.count_documents({"sport": "NBA"})
    print(f'player_game_logs NBA count: {cnt}')
    # Latest sample
    sample = await db.player_game_logs.find({"sport": "NBA"}).sort([("game_date", -1)]).limit(3).to_list(3)
    for s in sample:
        print(' ', {k:v for k,v in s.items() if k not in ('_id',)})

    # also try nba_feature_engine directly
    from services.nba_feature_engine import precompute_nba_prop_factors
    players = ['LeBron James', 'Victor Wembanyama', 'Jayson Tatum', 'Cade Cunningham']
    markets = ['player_points', 'player_rebounds', 'player_assists']
    lines_bp = {p: {m: [(10.5, 'over')] for m in markets} for p in players}
    try:
        res = await precompute_nba_prop_factors(db, players=players, market_keys=markets, lines_by_player_market=lines_bp)
        print('\nprecompute result keys:', list(res.keys()))
        pre = res.get('nba_precomputed') or {}
        print('nba_precomputed players:', list(pre.keys())[:10])
        for p, mp in list(pre.items())[:3]:
            print(f'  player: {p}')
            for mk, entry in list(mp.items())[:2]:
                print(f'    mk={mk} factors={list((entry.get("factors") or {}).keys())[:4]} sources={entry.get("sources")}')
    except Exception as e:
        import traceback
        traceback.print_exc()
        print('Error:', e)

asyncio.run(main())
