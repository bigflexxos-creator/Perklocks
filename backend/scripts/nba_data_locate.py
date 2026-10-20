"""Find NBA historical/gamelog data in Mongo."""
import asyncio, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))
os.environ['LS_INIT_LITE'] = '1'

async def main():
    from services.database import get_database
    db = get_database()
    all_cols = await db.list_collection_names()
    print('All collections ({}):'.format(len(all_cols)))
    for c in sorted(all_cols):
        if any(k in c.lower() for k in ('nba', 'player', 'game', 'log', 'canonical', 'actual')):
            try:
                cnt = await db[c].count_documents({})
                print(f'  {c:60s} {cnt:>8}')
            except Exception:
                pass

    # Look specifically at collections mentioning NBA
    print('\n--- NBA sample from canonical ingested collections ---')
    for c in sorted(all_cols):
        if 'nba' in c.lower() or 'player_game' in c.lower() or 'player_registry' in c.lower():
            try:
                sample = await db[c].find({}).limit(1).to_list(1)
                if sample:
                    print(f'\n{c}: sample keys = {sorted(sample[0].keys())}')
                    print(f'  sample = {dict((k,v) for k,v in list(sample[0].items())[:8])}'[:500])
            except Exception as e:
                print(f'  {c}: err {e}')

asyncio.run(main())
