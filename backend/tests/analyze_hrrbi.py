import asyncio, os, re
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

def norm_wp(v):
    try: f = float(v or 0)
    except: return 0
    return f/100 if f > 1 else f

async def main():
    c = AsyncIOMotorClient(os.environ['MONGO_URL'])
    db = c[os.environ['DB_NAME']]
    base = {
        'status': {'$in': ['won','lost']},
        'excluded_from_history': {'$ne': True},
        'sport': 'MLB',
        'market': {'$regex': r'hits.+runs.+rbi', '$options':'i'},
    }
    total = await db.picks.count_documents(base)
    won   = await db.picks.count_documents({**base, 'status':'won'})
    print(f'=== H+R+RBI universe: n={total}  wr={won/total*100:.1f}%\n')

    # Fetch all rows for finer analysis
    rows = []
    async for p in db.picks.find(base, {'_id':0,'lock_score':1,'win_probability':1,'edge_percent':1,'book_odds':1,'is_alt':1,'market':1,'status':1,'event':1}):
        rows.append(p)

    def bucket_stats(name, buckets, key_fn):
        print(f'\nBY {name}:')
        by = {}
        for p in rows:
            k = key_fn(p)
            if k is None: continue
            by.setdefault(k, []).append(p['status'])
        for label in buckets:
            arr = by.get(label, [])
            if not arr: continue
            n = len(arr); w = sum(1 for s in arr if s=='won')
            tag = '✅' if w/n >= 0.55 else ('⚠️ ' if w/n < 0.40 else '  ')
            print(f'  {tag} {label:<20} n={n:>3}  wr={w/n*100:>5.1f}%  ({w}/{n})')

    # By lock band
    def lock_bucket(p):
        l = p.get('lock_score') or 0
        for lo,hi,lab in [(99,101,'99'),(95,99,'95-98'),(89,95,'89-94'),(85,89,'85-88'),(80,85,'80-84'),(70,80,'70-79'),(0,70,'<70')]:
            if lo <= l < hi: return lab
    bucket_stats('LOCK_SCORE',
        ['99','95-98','89-94','85-88','80-84','70-79','<70'], lock_bucket)

    # By WP band
    def wp_bucket(p):
        w = norm_wp(p.get('win_probability'))
        for lo,hi,lab in [(0.85,1.01,'wp≥85%'),(0.80,0.85,'80-85%'),(0.75,0.80,'75-80%'),(0.70,0.75,'70-75%'),(0.65,0.70,'65-70%'),(0.60,0.65,'60-65%'),(0.50,0.60,'50-60%'),(0.0,0.50,'<50%')]:
            if lo <= w < hi: return lab
    bucket_stats('WIN_PROBABILITY',
        ['wp≥85%','80-85%','75-80%','70-75%','65-70%','60-65%','50-60%','<50%'], wp_bucket)

    # By edge
    def edge_bucket(p):
        e = float(p.get('edge_percent') or 0)
        for lo,hi,lab in [(0,3,'0-3%'),(3,5,'3-5%'),(5,8,'5-8%'),(8,12,'8-12%'),(12,20,'12-20%'),(20,999,'20%+')]:
            if lo <= e < hi: return lab
    bucket_stats('EDGE', ['0-3%','3-5%','5-8%','8-12%','12-20%','20%+'], edge_bucket)

    # By odds
    def odds_bucket(p):
        o = float(p.get('book_odds') or 0)
        if o == 0: return None
        for lo,hi,lab in [(-9999,-300,'<-300'),(-300,-200,'-300 to -200'),(-200,-140,'-200 to -140'),(-140,-110,'-140 to -110'),(-110,100,'-110 to +100'),(100,200,'+100 to +200'),(200,9999,'+200 or more')]:
            if lo <= o < hi: return lab
    bucket_stats('ODDS', ['<-300','-300 to -200','-200 to -140','-140 to -110','-110 to +100','+100 to +200','+200 or more'], odds_bucket)

    # By alt vs main
    bucket_stats('ALT vs MAIN', ['main','alt'], lambda p: 'alt' if p.get('is_alt') else 'main')

    # By line threshold (the "Over 0.5" / "Over 1.5" number in the market)
    def line_bucket(p):
        m = re.search(r'over\s+(\d+\.?\d*)', (p.get('market') or ''), re.I)
        if not m: return None
        try: v = float(m.group(1))
        except: return None
        return f"Over {v:g}"
    bucket_stats('LINE', ['Over 0.5','Over 1.5','Over 2.5','Over 3.5'], line_bucket)

    # Combined winning shape
    print('\n=== BEST H+R+RBI SHAPE ===')
    best = [p for p in rows
            if (p.get('lock_score') or 0) >= 95
            and norm_wp(p.get('win_probability')) >= 0.75
            and 0 <= float(p.get('edge_percent') or 0) <= 12
            and float(p.get('book_odds') or -9999) >= -300]
    if best:
        w = sum(1 for p in best if p['status'] == 'won')
        print(f'  lock>=95 + wp>=75% + edge 0-12% + odds>=-300: n={len(best)} wr={w/len(best)*100:.1f}%')
    else:
        print('  (no picks match strict shape — sample too small)')

    # Show all winning picks so user can see patterns
    print('\n=== ALL WINNERS (H+R+RBI) ===')
    for p in [x for x in rows if x['status']=='won']:
        print(f"  lock={p.get('lock_score')} wp={norm_wp(p.get('win_probability'))*100:.0f}% edge={p.get('edge_percent')} odds={p.get('book_odds')} alt={bool(p.get('is_alt'))} · {p.get('market','')[:70]}")

    c.close()
asyncio.run(main())
