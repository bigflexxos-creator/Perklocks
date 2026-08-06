"""Phase 2B (finalize) — auto-void MLB picks where the game is Final but
the player wasn't in the box score. This is the "Sean Sullivan / DNP"
case: pick was generated for a player who never actually played (recalled
to AAA, wrong-Sullivan bug, prop-line API stale roster, etc.).

Sportsbook convention: no action → PUSH (void, refund). We flag them as
`status="void"` with `void_reason="player_not_in_box"` and set
`excluded_from_history=True` so analytics ignore them.

Also grades any picks where the player DID play — accent-normalized matching
already lives in `_names_match`, so this just runs the settler with a wider
window to sweep up anything the background loop missed.
"""
import asyncio, os, sys
from datetime import datetime, timezone, timedelta
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")
sys.path.insert(0, "/app/backend")

import httpx
from prop_settlement import (
    _stat_key_for_market, _extract_line, _parse_event_teams,
    _mlb_games_on, _mlb_find_game, _mlb_stat_for_player,
    _mlb_boxscore, _player_from_market, _date_str_for_pick, _grade,
)

async def main():
    c = AsyncIOMotorClient(os.environ['MONGO_URL'])
    db = c[os.environ['DB_NAME']]
    now = datetime.now(timezone.utc)
    # No age cutoff — grade any unsettled MLB pick whose game is Final.
    # Only skip picks whose games haven't finalized yet (checked below).

    q = {
        '$or':[{'status':None},{'status':{'$exists':False}}],
        'sport':'MLB',
    }
    picks = await db.picks.find(q).to_list(length=2000)
    print(f'MLB stale picks to process: {len(picks)}')

    graded_won = graded_lost = voided_no_player = voided_no_game = voided_no_market = 0
    skipped_not_final = skipped_no_line = skipped_no_teams = 0

    # Cache games/boxes per date
    games_cache = {}
    box_cache = {}

    async with httpx.AsyncClient(timeout=15, headers={'User-Agent':'PerksLocks/1.0'}) as cx:
        for p in picks:
            market = p.get('market') or ''
            stat_key = _stat_key_for_market(market)
            line = _extract_line(market)
            # Team-level markets (Spread/Moneyline/RunLine/NRFI) — skip, let team settler handle
            if not stat_key or not line:
                voided_no_market += 1
                continue
            away, home = _parse_event_teams(p.get('event') or '')
            if not away or not home:
                skipped_no_teams += 1
                continue
            date_str = _date_str_for_pick(p)
            if not date_str:
                skipped_no_teams += 1
                continue
            # Fetch games for date_str and date-1 (cache)
            if date_str not in games_cache:
                games = await _mlb_games_on(cx, date_str)
                try:
                    prev = (datetime.fromisoformat(date_str) - timedelta(days=1)).strftime('%Y-%m-%d')
                    prev_games = await _mlb_games_on(cx, prev)
                    seen = {g.get('gamePk') for g in games}
                    for g in prev_games:
                        if g.get('gamePk') not in seen:
                            games.append(g)
                except Exception:
                    pass
                games_cache[date_str] = games
                await asyncio.sleep(0.2)
            games = games_cache[date_str]
            game = _mlb_find_game(games, away, home)
            if not game:
                voided_no_game += 1
                continue
            status = ((game.get('status') or {}).get('abstractGameState') or '').lower()
            if status != 'final':
                skipped_not_final += 1
                continue
            pk = game.get('gamePk')
            if pk not in box_cache:
                box_cache[pk] = await _mlb_boxscore(cx, pk) or {}
                await asyncio.sleep(0.2)
            box = box_cache[pk]
            player = (p.get('selection') or '').strip() or _player_from_market(market)
            val = _mlb_stat_for_player(box, player, stat_key)
            if val is None:
                # Player wasn't on the roster / didn't play — auto-void
                await db.picks.update_one({'id': p['id']}, {'$set': {
                    'status': 'void',
                    'excluded_from_history': True,
                    'settled_at': datetime.now(timezone.utc).isoformat(),
                    'void_reason': 'player_not_in_boxscore',
                }})
                voided_no_player += 1
                continue
            outcome = _grade(val, line[0], line[1])
            update = {
                'status': outcome,
                'settled_at': datetime.now(timezone.utc).isoformat(),
                'settled_via': 'prop_backfill_2026-07-01',
                'result_value': val,
                'result_line': line[0],
            }
            await db.picks.update_one({'id': p['id']}, {'$set': update})
            if outcome == 'won':
                graded_won += 1
            elif outcome == 'lost':
                graded_lost += 1

    print(f'\n=== BACKFILL RESULTS ===')
    print(f'  Newly graded WON:            {graded_won}')
    print(f'  Newly graded LOST:           {graded_lost}')
    print(f'  Voided (player not in box):  {voided_no_player}')
    print(f'  Voided (game not found):     {voided_no_game}')
    print(f'  Skipped (team-level markets, handled by team settler): {voided_no_market}')
    print(f'  Skipped (game not final):    {skipped_not_final}')
    print(f'  Skipped (no team/date):      {skipped_no_teams}')

    # Fresh Hits @ 95+ numbers
    hits_q = {'sport':'MLB','market':{'$regex':r'over\s+\d+\.5\s+hits\b','$options':'i'}}
    tot = await db.picks.count_documents({**hits_q, 'lock_score':{'$gte':95}})
    won = await db.picks.count_documents({**hits_q, 'lock_score':{'$gte':95}, 'status':'won'})
    lost = await db.picks.count_documents({**hits_q, 'lock_score':{'$gte':95}, 'status':'lost'})
    print(f'\nMLB Hits @ lock>=95 (post-backfill): total={tot} won={won} lost={lost} wr={won/(won+lost)*100 if won+lost else 0:.1f}%')

    tot2 = await db.picks.count_documents({**hits_q})
    won2 = await db.picks.count_documents({**hits_q, 'status':'won'})
    lost2 = await db.picks.count_documents({**hits_q, 'status':'lost'})
    print(f'MLB Hits (all locks) post-backfill: total={tot2} won={won2} lost={lost2} wr={won2/(won2+lost2)*100 if won2+lost2 else 0:.1f}%')

    c.close()
asyncio.run(main())
