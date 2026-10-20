"""Trace actual NBA factor engine output."""
import asyncio, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))
os.environ['LS_INIT_LITE'] = '1'

async def main():
    from services.database import get_database
    from services.nba_feature_engine import _player_recent_logs, build_nba_prop_factors
    db = get_database()
    for name in ('LeBron James', 'Jayson Tatum', 'Victor Wembanyama', 'Cade Cunningham'):
        rows = await _player_recent_logs(db, name, limit=10)
        print(f'{name}: {len(rows)} rows')
        if rows:
            print(f'  keys: {sorted(rows[0].keys())[:12]}')
            print(f'  first: {dict((k,v) for k,v in list(rows[0].items())[:8])}')
            # Try factor build for Points over 20.5
            factors, sources = await build_nba_prop_factors(db, player=name, market_key='player_points', side='Over', line=20.5)
            print(f'  factors ({len(factors)}): {dict((k, round(v,2) if v is not None else None) for k,v in factors.items())}')
            print(f'  sources: {sources}')

asyncio.run(main())
