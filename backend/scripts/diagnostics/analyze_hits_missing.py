import asyncio, os
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

async def main():
    c = AsyncIOMotorClient(os.environ['MONGO_URL'])
    db = c[os.environ['DB_NAME']]

    # ALL Hits picks in DB (not just settled) — see what's going on
    base = {'sport': 'MLB', 'market': {'$regex': r'over\s+\d+\.5\s+hits\b', '$options':'i'}}
    n_total = await db.picks.count_documents(base)
    print(f'=== ALL MLB Hits picks in DB: {n_total} ===\n')

    # By status
    for status in ['won','lost','push','void','pending', None]:
        q = {**base, 'status': status} if status else {**base, 'status': {'$exists': False}}
        n = await db.picks.count_documents(q)
        print(f'  status={str(status):<10} n={n}')

    # By excluded_from_history flag
    n_excl = await db.picks.count_documents({**base, 'excluded_from_history': True})
    print(f'\n  excluded_from_history=True: {n_excl}')

    # Lock 95+ breakdown
    print('\n=== Hits picks at lock >= 95 (ALL statuses) ===')
    lock95 = await db.picks.count_documents({**base, 'lock_score': {'$gte': 95}})
    print(f'Total 95+: {lock95}')
    for status in ['won','lost','push','void','pending', None]:
        q = {**base, 'lock_score': {'$gte': 95}, 'status': status} if status else {**base, 'lock_score': {'$gte': 95}, 'status': {'$exists': False}}
        n = await db.picks.count_documents(q)
        if n: print(f'  status={str(status):<10} n={n}')

    # Pending picks that SHOULD be settled by now (event_time older than 6h ago)
    from datetime import datetime, timezone, timedelta
    six_h_ago = (datetime.now(timezone.utc) - timedelta(hours=6)).strftime('%Y-%m-%dT%H:%M:%SZ')
    q_stale = {**base, 'lock_score': {'$gte': 95}, 'status': {'$ne': 'won', '$ne':'lost'}, 'event_time': {'$lt': six_h_ago}}
    # rewrite properly
    q_stale = {**base, 'lock_score': {'$gte': 95}, 'status': {'$nin': ['won','lost']}, 'event_time': {'$lt': six_h_ago}}
    n_stale = await db.picks.count_documents(q_stale)
    print(f'\n  95+ picks with event >6h ago but NOT settled won/lost: {n_stale}')
    print('  Sample of stale unsettled 95+ Hits picks:')
    async for p in db.picks.find(q_stale).sort('event_time', -1).limit(10):
        print(f"    lock={p.get('lock_score')} status={p.get('status')} settled_at={p.get('settled_at')} event_time={p.get('event_time')} · {p.get('market','')[:60]}")

    # By pick_date (recency)
    print('\n=== Hits picks (lock>=95) by pick_date, last 20 days ===')
    pipe = [
        {'$match': {**base, 'lock_score': {'$gte': 95}}},
        {'$group': {'_id': '$pick_date', 'n': {'$sum': 1},
                    'won': {'$sum': {'$cond':[{'$eq':['$status','won']},1,0]}},
                    'lost': {'$sum': {'$cond':[{'$eq':['$status','lost']},1,0]}},
                    'other': {'$sum': {'$cond':[{'$in':['$status',['won','lost']]},0,1]}}}},
        {'$sort': {'_id': -1}}, {'$limit': 20}
    ]
    async for r in db.picks.aggregate(pipe):
        print(f"  {r['_id']}  n={r['n']:>3} won={r['won']:>2} lost={r['lost']:>2} unsettled={r['other']:>3}")

    # Settlement stats overall
    print('\n=== SETTLEMENT HEALTH ===')
    n_all = await db.picks.count_documents({})
    n_settled = await db.picks.count_documents({'status':{'$in':['won','lost']}})
    n_void = await db.picks.count_documents({'status':'void'})
    n_pending = await db.picks.count_documents({'status':'pending'})
    n_null = await db.picks.count_documents({'status':None})
    n_none = await db.picks.count_documents({'status':{'$exists':False}})
    print(f'  Total: {n_all}')
    print(f'  won/lost: {n_settled} ({n_settled/n_all*100:.1f}%)')
    print(f'  void: {n_void}')
    print(f'  pending: {n_pending}')
    print(f'  status=null: {n_null}  status missing: {n_none}')

    c.close()
asyncio.run(main())
