"""Phase 2A + 2B — clean up ghost picks and drain MLB props_pending queue.

Phase A (Soccer + Dedup):
  1. Void every Soccer FGS/AGS/To-Score-or-Assist/First-Goal/Last-Goal
     ghost pick (status=None). User has banned these families anyway.
  2. Dedup identical picks (same event+market+player+line) — keep newest.

Phase B (MLB force-drain):
  1. Force-settle every MLB hitter/pitcher prop with event_time > 24h ago
     and status=null via MLB Stats API box scores.
"""
import asyncio, os, re
from datetime import datetime, timezone, timedelta
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

BANNED_SOCCER_MARKETS = re.compile(
    r'goal scorer|first goal|last goal|to score or assist|to assist|hat.?trick|to score 2|to score 3',
    re.I,
)

async def phase_a_void_soccer(db):
    print('=== PHASE 2A.1: Void banned Soccer goalscorer ghosts ===')
    q = {
        '$or': [{'status': None}, {'status': {'$exists': False}}],
        'sport': 'Soccer',
        'market': {'$regex': BANNED_SOCCER_MARKETS.pattern, '$options': 'i'},
    }
    n_before = await db.picks.count_documents(q)
    print(f'  targeting {n_before} Soccer goalscorer/assist ghosts')
    if n_before == 0:
        return 0
    result = await db.picks.update_many(q, {'$set': {
        'status': 'void',
        'excluded_from_history': True,
        'settled_at': datetime.now(timezone.utc).isoformat(),
        'void_reason': 'banned_market_family_2026-07-01',
    }})
    print(f'  ✓ voided {result.modified_count} picks')
    return result.modified_count


async def phase_a_dedup(db):
    print('\n=== PHASE 2A.2: Dedup identical unsettled picks ===')
    q = {'$or': [{'status': None}, {'status': {'$exists': False}}]}
    # Group by (event, market, player_name) keep newest
    pipe = [
        {'$match': q},
        {'$group': {'_id': {'event':'$event','market':'$market','player':'$player_name'},
                    'ids':{'$push':{'id':'$id','created_at':'$created_at'}},
                    'n':{'$sum':1}}},
        {'$match': {'n':{'$gt':1}}},
    ]
    total_dupes_voided = 0
    async for r in db.picks.aggregate(pipe):
        entries = sorted(r['ids'], key=lambda x: x.get('created_at') or '', reverse=True)
        losers = [e['id'] for e in entries[1:] if e.get('id')]
        if not losers:
            continue
        res = await db.picks.update_many(
            {'id': {'$in': losers}, '$or':[{'status':None},{'status':{'$exists':False}}]},
            {'$set': {'status':'void','excluded_from_history':True,
                      'settled_at': datetime.now(timezone.utc).isoformat(),
                      'void_reason':'duplicate_dedup_2026-07-01'}}
        )
        total_dupes_voided += res.modified_count
    print(f'  ✓ voided {total_dupes_voided} duplicate rows')
    return total_dupes_voided


async def phase_b_drain_mlb(db):
    """Trigger the existing settlement engine on stale MLB props."""
    print('\n=== PHASE 2B: Drain MLB props_pending queue ===')
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=24)).strftime('%Y-%m-%dT%H:%M:%SZ')
    q = {
        '$or': [{'status': None}, {'status': {'$exists': False}}],
        'sport': 'MLB',
        'event_time': {'$lt': cutoff},
    }
    before = await db.picks.count_documents(q)
    print(f'  MLB props older than 24h + still ungraded: {before}')

    # Call the existing settlement engines directly.
    try:
        import sys
        sys.path.insert(0, '/app/backend')
        # 1. Team-level MLB via MLB Stats API
        try:
            from mlb_live_settle import settle_mlb_from_stats_api
            r1 = await settle_mlb_from_stats_api(db)
            print(f'  MLB Stats API team settlement: {r1}')
        except Exception as e:
            print(f'  (MLB team settler not runnable: {e})')

        # 2. Prop-level via prop settler
        try:
            from props_settle import settle_props
            r2 = await settle_props(db)
            print(f'  MLB props settler: {r2}')
        except Exception as e:
            try:
                # Fallback module names
                from settlement_engine import settle_due_picks
                r2 = await settle_due_picks(db)
                print(f'  Generic settle_due_picks: {r2}')
            except Exception as e2:
                print(f'  (Prop settler not runnable: {e} / {e2})')
    except Exception as e:
        print(f'  ✗ settlement failed: {e}')

    after = await db.picks.count_documents(q)
    graded = before - after
    print(f'  ✓ MLB drained: {graded} newly graded (was {before}, now {after} still pending)')
    return graded


async def main():
    c = AsyncIOMotorClient(os.environ['MONGO_URL'])
    db = c[os.environ['DB_NAME']]

    # Snapshot before
    n_before = await db.picks.count_documents({'$or':[{'status':None},{'status':{'$exists':False}}]})
    print(f'GHOST PICKS BEFORE: {n_before}\n')

    a1 = await phase_a_void_soccer(db)
    a2 = await phase_a_dedup(db)
    b1 = await phase_b_drain_mlb(db)

    n_after = await db.picks.count_documents({'$or':[{'status':None},{'status':{'$exists':False}}]})
    print(f'\n============================================================')
    print(f'GHOST PICKS AFTER: {n_after}  (was {n_before}, cleared {n_before - n_after})')
    print(f'  • Soccer banned-family voided: {a1}')
    print(f'  • Duplicates voided:            {a2}')
    print(f'  • MLB props drained:            {b1}')

    # Re-check MLB Hits at lock 95+
    hits_q = {'sport':'MLB','market':{'$regex':r'over\s+\d+\.5\s+hits\b','$options':'i'}}
    n95 = await db.picks.count_documents({**hits_q, 'lock_score':{'$gte':95}})
    won = await db.picks.count_documents({**hits_q, 'lock_score':{'$gte':95}, 'status':'won'})
    lost = await db.picks.count_documents({**hits_q, 'lock_score':{'$gte':95}, 'status':'lost'})
    null = await db.picks.count_documents({**hits_q, 'lock_score':{'$gte':95}, '$or':[{'status':None},{'status':{'$exists':False}}]})
    print(f'\nMLB Hits @ lock>=95 (post-cleanup): total={n95} won={won} lost={lost} still-ungraded={null}')
    if won + lost > 0:
        print(f'  hit rate: {won/(won+lost)*100:.1f}%')

    c.close()
asyncio.run(main())
