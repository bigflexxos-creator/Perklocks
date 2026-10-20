"""Check NBA player prop acquisition directly."""
import asyncio, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))
os.environ['LS_INIT_LITE'] = '1'

from sports_engine import _fetch_player_props_for_sport, _ACTIVE_KEYS, SPORT_KEYS, _load_active_sports


async def main():
    await _load_active_sports()
    print('NBA SPORT_KEYS:', SPORT_KEYS.get('NBA'))
    print('ACTIVE_KEYS has basketball_nba?:', 'basketball_nba' in _ACTIVE_KEYS)
    print('ACTIVE_KEYS size:', len(_ACTIVE_KEYS))
    print('All active basketball_* keys:', [k for k in _ACTIVE_KEYS if 'basket' in k.lower()])
    props = await _fetch_player_props_for_sport('NBA')
    print('NBA player prop candidates:', len(props))
    if props:
        for p in props[:5]:
            print('  mkt=%s  player=%s  wp=%s  lock=%s  provenance=%s source=%s is_alt=%s' % (
                p.get('market'), p.get('player'),
                p.get('win_probability'), p.get('lock_score'),
                p.get('probability_provenance'), p.get('probability_source'),
                p.get('is_alt'),
            ))
    import json
    if props:
        print('\nFIRST PROP SAMPLE (keys only):')
        print(sorted(props[0].keys()))

asyncio.run(main())
