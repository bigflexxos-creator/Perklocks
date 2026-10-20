"""Run full NBA pipeline via generate_all_picks + sim + lock A-S report."""
import asyncio, os, sys, json, datetime, collections
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))
os.environ['LS_INIT_LITE'] = '1'

async def main():
    from sports_engine import generate_all_picks
    from brain.sim_runner import apply_simulations
    date_str = datetime.datetime.utcnow().strftime('%Y-%m-%d')
    picks = await generate_all_picks(date_str, sport_filter='NBA')
    print('=== STAGE 1: generate_all_picks returned', len(picks), 'raw candidates')

    # Apply simulations
    counts = apply_simulations(picks)
    print('=== STAGE 2: apply_simulations counts:', counts)

    # Keep only NBA (sport_filter should guarantee this)
    nba_picks = [p for p in picks if (p.get('sport') or '') == 'NBA']
    print('=== STAGE 3: NBA picks after sim:', len(nba_picks))

    def bucket(p):
        m = p.get('market') or ''
        if 'Moneyline' in m: return 'ML'
        if 'Spread' in m: return 'SPREAD'
        if 'Total ' in m or 'Total:' in m: return 'TOTAL'
        if 'Rebounds' in m and 'Assists' in m and 'Points' in m: return 'PRA'
        if 'Rebounds' in m and 'Points' not in m: return 'REB'
        if 'Assists' in m and 'Points' not in m: return 'AST'
        if 'Threes' in m or '3-Pointer' in m or 'Three-Pointer' in m or 'Three Pointer' in m: return 'THREES'
        if 'Points' in m: return 'PTS'
        return 'OTHER'

    by_market = collections.Counter()
    by_market_85 = collections.Counter()
    locks = []
    for p in nba_picks:
        b = bucket(p)
        by_market[b] += 1
        ls = float(p.get('lock_score') or 0)
        locks.append(ls)
        if ls >= 85:
            by_market_85[b] += 1

    print('\nA. events processed: see below (unique events in picks)')
    events = set(p.get('event') for p in nba_picks)
    print('   unique events:', len(events))
    for ev in sorted(events)[:20]:
        print('    ', ev)

    print('\nE. model attempts by market (= candidate count by bucket):')
    for k,v in by_market.most_common():
        print(f'   {k:10s} {v:>5d}')

    print('\nG. >=85 by market:')
    for k,v in by_market_85.most_common():
        print(f'   {k:10s} {v:>5d}')
    print('   TOTAL >= 85:', sum(by_market_85.values()))

    bins = {'<60':0,'60-69':0,'70-79':0,'80-84':0,'85-89':0,'90-94':0,'95+':0}
    for l in locks:
        if l<60: bins['<60']+=1
        elif l<70: bins['60-69']+=1
        elif l<80: bins['70-79']+=1
        elif l<85: bins['80-84']+=1
        elif l<90: bins['85-89']+=1
        elif l<95: bins['90-94']+=1
        else: bins['95+']+=1
    print('\nJ. Lock Score distribution:')
    for k,v in bins.items():
        print(f'   {k:6s} {v:>5d}')
    print(f'   min={min(locks or [0]):.1f}  avg={(sum(locks)/max(1,len(locks))):.1f}  max={max(locks or [0]):.1f}')

    print('\nI. TOP 10 by Lock Score:')
    for p in sorted(nba_picks, key=lambda x: -(x.get('lock_score') or 0))[:10]:
        print('  lock=%-5s wp=%-6s sim_wp=%-6s runs=%-6s mkt=%s prov=%s' % (
            p.get('lock_score'), p.get('win_probability'),
            p.get('sim_win_probability'), p.get('sim_runs'),
            (p.get('market') or '')[:45],
            p.get('probability_provenance') or p.get('simulator_provenance') or '',
        ))

    # Best per market
    print('\nK-P. Best Pick per market:')
    for cat in ('ML','SPREAD','TOTAL','PTS','REB','AST','PRA','THREES'):
        best = None
        for p in nba_picks:
            if bucket(p) != cat: continue
            if not best or (p.get('lock_score') or 0) > (best.get('lock_score') or 0):
                best = p
        if best:
            print(f'   {cat:7s}  lock={best.get("lock_score")} wp={best.get("win_probability")} mkt={best.get("market")}')
        else:
            print(f'   {cat:7s}  (no candidates)')

    # Count D: standard vs alt
    std = sum(1 for p in nba_picks if not p.get('is_alt'))
    alt = sum(1 for p in nba_picks if p.get('is_alt'))
    print(f'\nC/D. Standard lines: {std}  Alt lines: {alt}')

asyncio.run(main())
