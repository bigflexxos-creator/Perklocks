"""Phase 2B — force-drain MLB props_pending queue.

Uses `prop_settlement.settle_player_props` which is the same module that
runs in the background loop — but with a higher max_picks cap so it
gets through the entire backlog in one shot.
"""
import asyncio, os, sys
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")
sys.path.insert(0, "/app/backend")

async def main():
    c = AsyncIOMotorClient(os.environ['MONGO_URL'])
    db = c[os.environ['DB_NAME']]

    # Snapshot before
    before_all = await db.picks.count_documents({'$or':[{'status':None},{'status':{'$exists':False}}]})
    before_mlb = await db.picks.count_documents({'$or':[{'status':None},{'status':{'$exists':False}}], 'sport':'MLB'})
    print(f'BEFORE  total-ghosts={before_all}  MLB-ghosts={before_mlb}')

    # Iteratively drain until the counter stops falling. Each call caps at
    # max_picks=800, so a few passes may be needed.
    from prop_settlement import settle_player_props
    round_num = 0
    while True:
        round_num += 1
        counts = await settle_player_props(db, max_picks=2000)
        print(f'  round {round_num}: {counts}')
        if counts.get('settled', 0) == 0 or round_num >= 4:
            break

    # Also drain Soccer W/D and Total Goals if any left
    from settlement_engine import settle_due_picks
    counts_te = await settle_due_picks(db)
    print(f'  team-level settle_due_picks: {counts_te}')

    # Tennis (extend regex support upstream is TODO)
    try:
        from espn_settlement import settle_tennis_via_espn
        r_tennis = await settle_tennis_via_espn(db)
        print(f'  tennis ESPN: {r_tennis}')
    except Exception as e:
        print(f'  tennis skipped: {e}')

    after_all = await db.picks.count_documents({'$or':[{'status':None},{'status':{'$exists':False}}]})
    after_mlb = await db.picks.count_documents({'$or':[{'status':None},{'status':{'$exists':False}}], 'sport':'MLB'})
    print(f'\nAFTER   total-ghosts={after_all}  (cleared {before_all - after_all})')
    print(f'        MLB-ghosts={after_mlb}     (cleared {before_mlb - after_mlb})')

    # Post-cleanup MLB Hits @ 95+ real numbers
    hits_q = {'sport':'MLB','market':{'$regex':r'over\s+\d+\.5\s+hits\b','$options':'i'}}
    tot = await db.picks.count_documents({**hits_q, 'lock_score':{'$gte':95}})
    won = await db.picks.count_documents({**hits_q, 'lock_score':{'$gte':95}, 'status':'won'})
    lost = await db.picks.count_documents({**hits_q, 'lock_score':{'$gte':95}, 'status':'lost'})
    null = await db.picks.count_documents({**hits_q, 'lock_score':{'$gte':95}, '$or':[{'status':None},{'status':{'$exists':False}}]})
    print(f'\nMLB Hits @ lock>=95 post-drain: total={tot} won={won} lost={lost} still-ungraded={null}')
    if won + lost > 0:
        print(f'  hit rate: {won/(won+lost)*100:.1f}%')

    c.close()
asyncio.run(main())
