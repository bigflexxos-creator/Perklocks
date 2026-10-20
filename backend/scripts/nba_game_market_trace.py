"""Trace NBA game market generation: did games get fetched? Did ratings load? Did estimate_nba_game run?"""
from __future__ import annotations
import asyncio, os, sys, logging

sys.path.insert(0, "/app/backend")
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")
os.environ.setdefault("THEODDS_FILTER_SPORT", "NBA")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

import sports_engine as se


async def main():
    date_str = "2026-10-20"
    print("=" * 70)
    print("[1] Fetching basketball_nba odds via _fetch_odds_for")
    print("=" * 70)
    games = await se._fetch_odds_for("basketball_nba", regions="us", sport="NBA")
    print(f"\n  games returned: {len(games)}")
    for i, g in enumerate(games[:3]):
        print(f"\n  [{i}] {g.get('away_team')} @ {g.get('home_team')}  commence={g.get('commence_time')}")
        for bk in (g.get("bookmakers") or [])[:1]:
            for m in bk.get("markets", [])[:5]:
                print(f"     mkt: {m.get('key')}  outcomes={len(m.get('outcomes') or [])}")

    if not games:
        print("\n[!] NO basketball_nba games returned — upstream fetch issue")
        return

    print("\n" + "=" * 70)
    print("[2] Loading NBA team ratings")
    print("=" * 70)
    from services.nba_game_model import load_nba_team_ratings, estimate_nba_game
    from services.database import get_database
    db = get_database()
    ratings = await load_nba_team_ratings(db, window=20)
    print(f"\n  teams in ratings dict: {len(ratings)}")
    for tk in list(ratings.keys())[:5]:
        print(f"     {tk!r}: {ratings[tk]}")

    print("\n" + "=" * 70)
    print("[3] Estimating NBA game markets for each slate game")
    print("=" * 70)
    ctx = {"nba_team_ratings_by_team": ratings}
    for g in games[:5]:
        h, a = g.get("home_team"), g.get("away_team")
        est = estimate_nba_game(ctx, h, a)
        print(f"\n  {a} @ {h}")
        print(f"     available={est.available}  reason={est.reason}  tier={est.tier}")
        if est.available:
            print(f"     p_home_ml={est.p_home_ml:.3f}  "
                  f"expected_margin={est.expected_margin:.2f}  "
                  f"expected_total={est.expected_total:.2f}")

    print("\n" + "=" * 70)
    print("[4] Running _picks_from_game sync branch for first game")
    print("=" * 70)
    first = dict(games[0])
    first["_ctx"] = {"nba_team_ratings_by_team": ratings}
    picks = se._picks_from_game("NBA", "NBA", first, date_str)
    print(f"\n  picks from first game: {len(picks)}")
    for p in picks[:8]:
        wp = p.get("model_win_prob") or p.get("win_probability")
        print(f"     market={p.get('market')}  wp={wp}  "
              f"lock={p.get('lock_score')}  edge={p.get('edge_percent')}  "
              f"pick={p.get('pick')}")


if __name__ == "__main__":
    asyncio.run(main())
