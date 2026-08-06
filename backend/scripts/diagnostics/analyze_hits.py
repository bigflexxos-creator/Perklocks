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
    # MLB Hits — hitter, "Over X.5 Hits" pattern, exclude H+R+RBI and Total Bases
    base = {
        'status': {'$in': ['won','lost']},
        'excluded_from_history': {'$ne': True},
        'sport': 'MLB',
        'market': {'$regex': r'over\s+\d+\.5\s+hits\b', '$options':'i'},
    }
    total = await db.picks.count_documents(base)
    won   = await db.picks.count_documents({**base, 'status':'won'})
    print(f'=== MLB Hits universe: n={total}  wr={won/total*100:.1f}%\n')

    rows = []
    async for p in db.picks.find(base, {'_id':0,'lock_score':1,'win_probability':1,'edge_percent':1,'book_odds':1,'is_alt':1,'market':1,'status':1}):
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
            wr = w/n*100
            tag = '🏆' if wr >= 70 else ('✅' if wr >= 60 else ('  ' if wr >= 50 else '⚠️ '))
            print(f'  {tag} {label:<18} n={n:>4}  wr={wr:>5.1f}%  ({w}/{n})')

    # Lock band
    def lock_bucket(p):
        l = p.get('lock_score') or 0
        for lo,hi,lab in [(99,101,'99'),(95,99,'95-98'),(89,95,'89-94'),(85,89,'85-88'),(80,85,'80-84'),(70,80,'70-79'),(60,70,'60-69'),(0,60,'<60')]:
            if lo <= l < hi: return lab
    bucket_stats('LOCK_SCORE',
        ['99','95-98','89-94','85-88','80-84','70-79','60-69','<60'], lock_bucket)

    # WP band
    def wp_bucket(p):
        w = norm_wp(p.get('win_probability'))
        for lo,hi,lab in [(0.85,1.01,'wp>=85%'),(0.80,0.85,'80-85%'),(0.75,0.80,'75-80%'),(0.70,0.75,'70-75%'),(0.65,0.70,'65-70%'),(0.60,0.65,'60-65%'),(0.50,0.60,'50-60%'),(0.0,0.50,'<50%')]:
            if lo <= w < hi: return lab
    bucket_stats('WIN_PROBABILITY',
        ['wp>=85%','80-85%','75-80%','70-75%','65-70%','60-65%','50-60%','<50%'], wp_bucket)

    # Edge band
    def edge_bucket(p):
        e = float(p.get('edge_percent') or 0)
        for lo,hi,lab in [(-999,0,'NEGATIVE'),(0,3,'0-3%'),(3,5,'3-5%'),(5,8,'5-8%'),(8,12,'8-12%'),(12,20,'12-20%'),(20,999,'20%+')]:
            if lo <= e < hi: return lab
    bucket_stats('EDGE', ['NEGATIVE','0-3%','3-5%','5-8%','8-12%','12-20%','20%+'], edge_bucket)

    # Odds band
    def odds_bucket(p):
        o = float(p.get('book_odds') or 0)
        if o == 0: return None
        for lo,hi,lab in [(-9999,-400,'<-400'),(-400,-300,'-400 to -300'),(-300,-200,'-300 to -200'),(-200,-140,'-200 to -140'),(-140,-110,'-140 to -110'),(-110,100,'-110 to +100'),(100,200,'+100 to +200'),(200,9999,'+200+')]:
            if lo <= o < hi: return lab
    bucket_stats('ODDS', ['<-400','-400 to -300','-300 to -200','-200 to -140','-140 to -110','-110 to +100','+100 to +200','+200+'], odds_bucket)

    # Alt vs main
    bucket_stats('ALT vs MAIN', ['main','alt'], lambda p: 'alt' if p.get('is_alt') else 'main')

    # Line
    def line_bucket(p):
        m = re.search(r'over\s+(\d+\.?\d*)\s+hits', (p.get('market') or ''), re.I)
        if not m: return None
        try: return f'Over {float(m.group(1)):g} Hits'
        except: return None
    bucket_stats('LINE', ['Over 0.5 Hits','Over 1.5 Hits','Over 2.5 Hits'], line_bucket)

    # Best combined shape
    print('\n=== HITTER PROP: BEST SHAPES ===')
    shapes = [
        ('lock>=95 + edge>0 + odds>=-300',
         lambda p: (p.get('lock_score') or 0) >= 95 and float(p.get('edge_percent') or 0) > 0 and float(p.get('book_odds') or -9999) >= -300),
        ('lock 89-94 + wp>=80% + edge 0-8%',
         lambda p: 89 <= (p.get('lock_score') or 0) < 95 and norm_wp(p.get('win_probability')) >= 0.80 and 0 <= float(p.get('edge_percent') or 0) < 8),
        ('Over 0.5 Hits + alt + odds -300 to -200',
         lambda p: p.get('is_alt') and 'over 0.5' in (p.get('market') or '').lower() and -300 <= float(p.get('book_odds') or 0) < -200),
        ('Over 0.5 Hits + alt (all)',
         lambda p: p.get('is_alt') and 'over 0.5' in (p.get('market') or '').lower()),
        ('Over 1.5 Hits (all)',
         lambda p: 'over 1.5' in (p.get('market') or '').lower()),
        ('edge<0 (negative — false signal)',
         lambda p: float(p.get('edge_percent') or 0) < 0),
        ('wp>=80% (any edge, any lock)',
         lambda p: norm_wp(p.get('win_probability')) >= 0.80),
    ]
    for label, fn in shapes:
        matches = [p for p in rows if fn(p)]
        if not matches: continue
        w = sum(1 for p in matches if p['status']=='won')
        wr = w/len(matches)*100
        tag = '🏆' if wr >= 70 else ('✅' if wr >= 60 else ('  ' if wr >= 50 else '⚠️ '))
        print(f'  {tag} {label:<50} n={len(matches):>4}  wr={wr:>5.1f}%')

    c.close()
asyncio.run(main())
