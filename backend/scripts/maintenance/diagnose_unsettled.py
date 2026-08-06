import asyncio, os
from datetime import datetime, timezone, timedelta
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

async def main():
    c = AsyncIOMotorClient(os.environ['MONGO_URL'])
    db = c[os.environ['DB_NAME']]
    now = datetime.now(timezone.utc)
    day_ago = (now - timedelta(hours=24)).strftime('%Y-%m-%dT%H:%M:%SZ')
    week_ago = (now - timedelta(days=7)).strftime('%Y-%m-%dT%H:%M:%SZ')
    month_ago = (now - timedelta(days=30)).strftime('%Y-%m-%dT%H:%M:%SZ')

    # All unsettled picks (status None or missing)
    q = {'$or': [{'status': None}, {'status': {'$exists': False}}]}
    n = await db.picks.count_documents(q)
    print(f'=== UNSETTLED (status=null or missing): {n} ===\n')

    # By sport
    print('BY SPORT:')
    pipe = [{'$match': q}, {'$group':{'_id':'$sport','n':{'$sum':1}}}, {'$sort':{'n':-1}}]
    async for r in db.picks.aggregate(pipe):
        print(f"  {(r['_id'] or '?'):<12} {r['n']:>5}")

    # Age: how old are these unsettled picks?
    print('\nBY AGE (event_time relative to now):')
    async def cnt(cond, label):
        n = await db.picks.count_documents({**q, **cond})
        print(f'  {label:<40} {n:>5}')
    await cnt({'event_time': {'$lt': month_ago}}, 'event >30 days old (definitely stale)')
    await cnt({'event_time': {'$gte': month_ago, '$lt': week_ago}}, 'event 7-30 days old (stale)')
    await cnt({'event_time': {'$gte': week_ago, '$lt': day_ago}}, 'event 24h-7d old (should have settled)')
    await cnt({'event_time': {'$gte': day_ago}}, 'event <24h old (may still be legit pending)')
    await cnt({'event_time': {'$exists': False}}, 'NO event_time (metadata missing)')
    await cnt({'event_time': ''}, 'event_time is empty string')

    # By market family — see which pipelines fail most
    print('\nBY MARKET FAMILY (top 15):')
    pipe = [{'$match': q},
            {'$addFields': {'mkt_lower': {'$toLower': '$market'}}},
            {'$group': {'_id': None, 'total':{'$sum':1}}}]
    families = {
        'anytime_gs':      r'anytime goal scorer',
        'first_last_gs':   r'first goal scorer|last goal scorer',
        'total_goals':     r'total goals',
        'moneyline':       r'moneyline',
        'win_or_draw':     r'win or draw|double chance',
        'run_line/spread': r'run line|spread|handicap|\+\d|\-\d',
        'mlb_strikeouts':  r'strikeouts',
        'mlb_outs':        r'outs recorded',
        'mlb_hits':        r'over\s+\d+\.5\s+hits\b',
        'mlb_h+r+rbi':     r'hits.+runs.+rbi',
        'mlb_total_bases': r'total bases',
        'tennis_games':    r'games (\+|\-)',
        'nrfi_yrfi':       r'nrfi|yrfi',
        'to_score_assist': r'to score or assist|to assist',
    }
    for fname, pat in families.items():
        n = await db.picks.count_documents({**q, 'market': {'$regex': pat, '$options':'i'}})
        if n >= 5:
            print(f'  {fname:<20} {n:>5}')

    # By lock band — do we have unsettled high-conviction picks?
    print('\nBY LOCK_SCORE band:')
    for lo,hi,lab in [(95,101,'95+ (rollover-tier!)'), (89,95,'89-94'), (85,89,'85-88'),
                     (80,85,'80-84'), (70,80,'70-79'), (0,70,'<70')]:
        n = await db.picks.count_documents({**q, 'lock_score': {'$gte': lo, '$lt': hi}})
        print(f'  {lab:<22} {n:>5}')

    # Recent unsettled — pick_date last 14 days
    print('\nUNSETTLED BY pick_date (last 14 days):')
    pipe = [{'$match': q},
            {'$group': {'_id':'$pick_date','n':{'$sum':1},
                        'mlb':{'$sum':{'$cond':[{'$eq':['$sport','MLB']},1,0]}},
                        'tennis':{'$sum':{'$cond':[{'$eq':['$sport','Tennis']},1,0]}},
                        'soccer':{'$sum':{'$cond':[{'$eq':['$sport','Soccer']},1,0]}},
                       }},
            {'$sort':{'_id':-1}}, {'$limit':14}]
    async for r in db.picks.aggregate(pipe):
        print(f"  {r['_id']:<12} n={r['n']:>4}  MLB={r['mlb']:>3}  Tennis={r['tennis']:>3}  Soccer={r['soccer']:>3}")

    # Sample 10 stale unsettled MLB picks to see WHY
    print('\n=== SAMPLE STALE UNSETTLED MLB (event >24h ago, status null) ===')
    async for p in db.picks.find({**q, 'sport':'MLB', 'event_time':{'$lt': day_ago}}).sort('event_time',-1).limit(10):
        print(f"  {p.get('event_time','?')[:19]}  {(p.get('event','') or '')[:40]}  · {(p.get('market','') or '')[:60]}")

    # Sample 10 stale unsettled Tennis picks
    print('\n=== SAMPLE STALE UNSETTLED TENNIS ===')
    async for p in db.picks.find({**q, 'sport':'Tennis', 'event_time':{'$lt': day_ago}}).sort('event_time',-1).limit(10):
        print(f"  {p.get('event_time','?')[:19]}  {(p.get('event','') or '')[:40]}  · {(p.get('market','') or '')[:60]}")

    # Sample 10 stale unsettled Soccer
    print('\n=== SAMPLE STALE UNSETTLED SOCCER ===')
    async for p in db.picks.find({**q, 'sport':'Soccer', 'event_time':{'$lt': day_ago}}).sort('event_time',-1).limit(10):
        print(f"  {p.get('event_time','?')[:19]}  {(p.get('event','') or '')[:40]}  · {(p.get('market','') or '')[:60]}")

    c.close()
asyncio.run(main())
