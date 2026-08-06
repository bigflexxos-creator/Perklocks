import asyncio, os
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

async def main():
    c = AsyncIOMotorClient(os.environ['MONGO_URL'])
    db = c[os.environ['DB_NAME']]
    q = {'status': {'$in': ['won','lost']}, 'excluded_from_history': {'$ne': True}}
    n = await db.picks.count_documents(q)
    print(f'=== Total settled picks (excl. void/push): {n} ===\n')

    # 1. By sport
    pipe = [{'$match': q}, {'$group':{'_id':'$sport','n':{'$sum':1},'won':{'$sum':{'$cond':[{'$eq':['$status','won']},1,0]}}}}]
    print('BY SPORT (n>=15):')
    rows = []
    async for r in db.picks.aggregate(pipe):
        if r['n'] < 15: continue
        rows.append((r['_id'] or '?', r['n'], r['won']/r['n']*100))
    for s,n_,wr in sorted(rows, key=lambda x: -x[2]):
        print(f'  {s:<10} n={n_:>4}  wr={wr:>5.1f}%')

    # 2. Lock band
    print('\nBY LOCK_SCORE BAND:')
    for lo,hi in [(99,100),(95,99),(89,95),(85,89),(80,85),(70,80),(60,70)]:
        qq = {**q, 'lock_score': {'$gte': lo, '$lt': hi}}
        nn = await db.picks.count_documents(qq)
        if nn < 10: continue
        won = await db.picks.count_documents({**qq, 'status':'won'})
        print(f'  {lo}-{hi-1:>2}: n={nn:>4}  wr={won/nn*100:>5.1f}%')

    # 3. Market families
    print('\nBY MARKET FAMILY:')
    families = {
        'anytime_gs': r'anytime goal scorer',
        'first_gs': r'first goal scorer|last goal scorer',
        'total_goals': r'total goals',
        'moneyline': r'moneyline|to win outright',
        'win_or_draw': r'win or draw|double chance',
        'run_line/spread': r'run line|spread|handicap',
        'mlb_strikeouts': r'strikeouts',
        'mlb_outs': r'outs recorded',
        'mlb_hits': r'\bover.*\d+\.5 hits\b',
        'mlb_h+r+rbi': r'hits.+runs.+rbi',
        'mlb_total_bases': r'total bases',
        'tennis_games': r'games \+|games -|game spread|games alt',
        'nrfi_yrfi': r'nrfi|yrfi',
    }
    rows = []
    for name, pat in families.items():
        qq = {**q, 'market': {'$regex': pat, '$options':'i'}}
        nn = await db.picks.count_documents(qq)
        if nn < 15: continue
        won = await db.picks.count_documents({**qq, 'status':'won'})
        rows.append((name, nn, won/nn*100))
    for name,n_,wr in sorted(rows, key=lambda x: -x[2]):
        print(f'  {name:<18} n={n_:>4}  wr={wr:>5.1f}%')

    # 4. Edge bands
    print('\nBY EDGE BAND:')
    for lo,hi in [(0,3),(3,5),(5,8),(8,12),(12,20),(20,999)]:
        qq = {**q, 'edge_percent': {'$gte': lo, '$lt': hi}}
        nn = await db.picks.count_documents(qq)
        if nn < 20: continue
        won = await db.picks.count_documents({**qq, 'status':'won'})
        print(f'  {lo}-{hi}%:  n={nn:>4}  wr={won/nn*100:>5.1f}%')

    # 5. Odds bands
    print('\nBY ODDS BAND:')
    for lo,hi in [(-9999,-300),(-300,-200),(-200,-140),(-140,-110),(-110,100),(100,150),(150,300),(300,9999)]:
        qq = {**q, 'book_odds': {'$gte': lo, '$lt': hi}}
        nn = await db.picks.count_documents(qq)
        if nn < 20: continue
        won = await db.picks.count_documents({**qq, 'status':'won'})
        print(f'  {lo:>6} to {hi:>5}:  n={nn:>4}  wr={won/nn*100:>5.1f}%')

    # 6. Main vs Alt
    print('\nMAIN vs ALT:')
    for tag, cond in [('main', {'is_alt':{'$ne':True}}), ('alt', {'is_alt':True})]:
        qq = {**q, **cond}
        nn = await db.picks.count_documents(qq)
        won = await db.picks.count_documents({**qq, 'status':'won'})
        print(f'  {tag:<5} n={nn:>4}  wr={won/nn*100:>5.1f}%')

    # 7. Sport x Market top winners
    print('\nTOP (sport, market_family) combos where n>=20 and wr>=65:')
    top_combos = []
    for sport in ['MLB','Soccer','Tennis','WNBA','MLS','NBA','NFL','KBO']:
        for fname, pat in families.items():
            qq = {**q, 'sport': sport, 'market': {'$regex': pat, '$options':'i'}}
            nn = await db.picks.count_documents(qq)
            if nn < 20: continue
            won = await db.picks.count_documents({**qq, 'status':'won'})
            wr = won/nn*100
            top_combos.append((sport, fname, nn, wr))
    for s, f, n_, wr in sorted(top_combos, key=lambda x: -x[3]):
        tag = '✅' if wr >= 65 else ('⚠️ ' if wr < 50 else '  ')
        print(f'  {tag} {s:<8} {f:<20} n={n_:>4}  wr={wr:>5.1f}%')

    c.close()
asyncio.run(main())
