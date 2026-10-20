"""P0-2 NBA Activation diagnostic — run fetch_nba_picks directly and report A-S distribution."""
import asyncio, os, sys, json, datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))
os.environ['LS_INIT_LITE'] = '1'

from sports_engine import fetch_nba_picks


def bucket(p):
    m = p.get('market') or ''
    if 'Moneyline' in m: return 'ML'
    if 'Spread' in m or 'Puck Line' in m: return 'SPREAD'
    if 'Total' in m and 'Points' not in m.replace('Total Points',''): pass
    if m.startswith('Total ') or m.startswith('Total:'): return 'TOTAL'
    if 'Total Points' in m: return 'TOTAL'
    if 'Rebounds' in m: return 'REB'
    if 'Assists' in m and 'Points' not in m: return 'AST'
    if 'PRA' in m or 'Points + Rebounds + Assists' in m: return 'PRA'
    if 'Threes' in m or '3-Pointer' in m or 'Three-Pointer' in m: return 'THREES'
    if 'Points' in m: return 'PTS'
    return 'OTHER'


async def main():
    date_str = datetime.datetime.utcnow().strftime('%Y-%m-%d')
    picks = await fetch_nba_picks(date_str)
    print('TOTAL NBA picks returned:', len(picks))
    by_market = {}
    by_market_lock85 = {}
    locks = []
    for p in picks:
        b = bucket(p)
        by_market[b] = by_market.get(b, 0) + 1
        ls = float(p.get('lock_score') or 0)
        locks.append(ls)
        if ls >= 85:
            by_market_lock85[b] = by_market_lock85.get(b, 0) + 1
    print('BY MARKET:', json.dumps(by_market, indent=2))
    print('BY MARKET with Lock>=85:', json.dumps(by_market_lock85, indent=2))
    print('LOCK distribution:')
    print('  min/avg/max:', min(locks or [0]), round(sum(locks)/max(1,len(locks)),2), max(locks or [0]))
    bins = {'<60':0,'60-69':0,'70-79':0,'80-84':0,'85-89':0,'90-94':0,'95+':0}
    for l in locks:
        if l<60: bins['<60']+=1
        elif l<70: bins['60-69']+=1
        elif l<80: bins['70-79']+=1
        elif l<85: bins['80-84']+=1
        elif l<90: bins['85-89']+=1
        elif l<95: bins['90-94']+=1
        else: bins['95+']+=1
    print('  bins:', bins)
    print('\nTOP 10 by lock_score:')
    for p in sorted(picks, key=lambda x: -(x.get('lock_score') or 0))[:10]:
        print('  lock=%-5s wp=%-5s mkt=%-32s player=%-28s book=%s prov=%s sim_wp=%s runs=%s' % (
            p.get('lock_score'), p.get('win_probability'),
            (p.get('market') or '')[:32], (p.get('player') or '')[:28],
            p.get('selected_bookmaker') or p.get('book'),
            p.get('probability_provenance') or p.get('probability_source') or '',
            p.get('sim_win_probability'), p.get('sim_runs'),
        ))

    # Show one of each bucket
    print('\nONE SAMPLE PER MARKET BUCKET:')
    seen = set()
    for p in picks:
        b = bucket(p)
        if b in seen:
            continue
        seen.add(b)
        print(' ', b, '|', p.get('market'), '|', 'player=', p.get('player'), '|', 'wp=', p.get('win_probability'), '| lock=', p.get('lock_score'), '| sim_wp=', p.get('sim_win_probability'), '| provenance=', p.get('probability_provenance'), '| source=', p.get('probability_source'))

    # Deep look at one game-market pick + one player-prop
    print('\n=== FULL SAMPLE — Moneyline ===')
    for p in picks:
        if bucket(p) == 'ML':
            print(json.dumps({k:v for k,v in p.items() if not isinstance(v,(dict,list)) or k in ('factors','sim_alt_lines')}, indent=2, default=str)[:4000])
            break
    print('\n=== FULL SAMPLE — PLAYER PROP (first one) ===')
    for p in picks:
        if bucket(p) in ('PTS','REB','AST','PRA','THREES'):
            print(json.dumps({k:v for k,v in p.items() if not isinstance(v,(dict,list)) or k in ('factors','sim_alt_lines')}, indent=2, default=str)[:4000])
            break
    else:
        print('  NO PLAYER PROPS RETURNED')


asyncio.run(main())
