"""Trace exactly why one specific MLB pick isn't settling."""
import asyncio, os, sys
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")
sys.path.insert(0, "/app/backend")

import httpx
from prop_settlement import (
    _stat_key_for_market, _extract_line, _parse_event_teams,
    _mlb_games_on, _mlb_find_game, _mlb_stat_for_player,
    _player_from_market, _date_str_for_pick,
)
from datetime import datetime, timezone, timedelta

async def main():
    c = AsyncIOMotorClient(os.environ['MONGO_URL'])
    db = c[os.environ['DB_NAME']]

    # Grab one stale MLB Hits pick
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=24)).strftime('%Y-%m-%dT%H:%M:%SZ')
    # Force a player prop, not a spread
    p = await db.picks.find_one({
        '$or':[{'status':None},{'status':{'$exists':False}}],
        'sport':'MLB',
        'event_time':{'$lt': cutoff},
        'market':{'$regex':r'(strikeouts|hits|outs recorded|rbi|runs|home runs)','$options':'i'},
    })
    if not p:
        print('no stale MLB ghost — quitting')
        return
    print(f'PICK: {p.get("market")} | event={p.get("event")} | et={p.get("event_time")}')
    print(f'  selection={p.get("selection")}  status={p.get("status")}')

    stat_key = _stat_key_for_market(p.get('market') or '')
    line = _extract_line(p.get('market') or '')
    print(f'  stat_key={stat_key}  line={line}')

    away, home = _parse_event_teams(p.get('event') or '')
    print(f'  teams: away={away!r}  home={home!r}')

    date_str = _date_str_for_pick(p)
    print(f'  date_str={date_str}')

    async with httpx.AsyncClient(timeout=10, headers={'User-Agent':'PerksLocks/1.0'}) as cx:
        games = await _mlb_games_on(cx, date_str)
        try:
            prev = (datetime.fromisoformat(date_str) - timedelta(days=1)).strftime('%Y-%m-%d')
            prev_games = await _mlb_games_on(cx, prev)
            seen = {g.get('gamePk') for g in games}
            for g in prev_games:
                if g.get('gamePk') not in seen:
                    games.append(g)
        except Exception as e:
            print(f'  prev-day fetch failed: {e}')
        print(f'  MLB API returned {len(games)} games for {date_str} + {prev}')

        game = _mlb_find_game(games, away, home)
        print(f'  matched game? {"YES" if game else "NO — TEAM NAME MISMATCH"}')
        if not game:
            # Show a few available teams so we can eyeball
            print('  available teams:')
            for g in games[:8]:
                t = g.get('teams') or {}
                print(f"    {(t.get('away') or {}).get('team',{}).get('name')} @ {(t.get('home') or {}).get('team',{}).get('name')}")
            return

        status = ((game.get('status') or {}).get('abstractGameState') or '').lower()
        print(f'  game status: {status}')
        if status != 'final':
            return

        game_pk = game.get('gamePk')
        from prop_settlement import _mlb_boxscore
        box = await _mlb_boxscore(cx, game_pk)
        player = (p.get('selection') or '').strip() or _player_from_market(p.get('market'))
        print(f'  player lookup: {player!r}')
        val = _mlb_stat_for_player(box, player, stat_key)
        print(f'  stat value: {val}  (line was {line})')

    c.close()
asyncio.run(main())
