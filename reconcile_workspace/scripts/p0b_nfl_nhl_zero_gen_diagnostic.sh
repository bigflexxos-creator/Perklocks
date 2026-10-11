#!/usr/bin/env bash
# ======================================================================
# P0-B NFL/NHL zero-generation — Production READ-ONLY diagnostic bundle
# ======================================================================
#
# Run on Production (or wherever the live MongoDB and Odds-API worker
# process live) and paste the full output back.  Every command is
# strictly READ-ONLY — no writes, no restarts, no flag changes.
#
# Requires:
#   - MONGO_URL env (same one the FastAPI uses)
#   - ODDS_API_KEY env (same one the pipeline uses)
#   - Python 3.11 with pymongo (or motor) installed in the venv
#   - jq + curl
#
# Expected runtime: < 60 seconds total.
# ======================================================================

set -u

echo "=== 0.  Environment sanity ==="
python3 - <<'PY'
import os, sys
print("MONGO_URL set:", bool(os.getenv("MONGO_URL")))
print("ODDS_API_KEY set:", bool(os.getenv("ODDS_API_KEY")))
print("Python:", sys.version.split()[0])
try:
    import pymongo; print("pymongo:", pymongo.__version__)
except Exception as e: print("pymongo import failed:", e)
PY

echo
echo "=== 1.  Mongo collection coverage — db.games (NFL + NHL) ==="
python3 - <<'PY'
import os, pymongo
db = pymongo.MongoClient(os.environ["MONGO_URL"]).get_default_database()
for sport in ("nfl", "nhl"):
    total = db.games.count_documents({"sport": sport})
    final = db.games.count_documents({"sport": sport, "status": "Final"})
    with_result = db.games.count_documents({
        "sport": sport, "status": "Final",
        "result.home": {"$ne": None}, "result.away": {"$ne": None},
    })
    print(f"  {sport.upper():3s}  total={total:>5d}  Final={final:>5d}  "
          f"Final+scored={with_result:>5d}")
    print(f"       distinct home teams (Final):",
          len(db.games.distinct("home", {"sport": sport, "status": "Final"})))
PY

echo
echo "=== 2.  Mongo collection coverage — db.player_game_logs (NFL + NHL) ==="
python3 - <<'PY'
import os, pymongo
db = pymongo.MongoClient(os.environ["MONGO_URL"]).get_default_database()
for sport in ("nfl", "nhl"):
    total = db.player_game_logs.count_documents({"sport": sport})
    recent = db.player_game_logs.count_documents({
        "sport": sport, "game_date": {"$gte": "2025-01-01"},
    })
    print(f"  {sport.upper():3s}  total={total:>7d}  since_2025_01={recent:>7d}")
PY

echo
echo "=== 3.  NFL _team_ratings output (same call build_nfl_game_model_context makes) ==="
python3 - <<'PY'
import os, asyncio, sys
sys.path.insert(0, "/app/backend")
async def _run():
    from services.database import get_database
    from nfl_game_engine import _team_ratings
    db = get_database()
    ctx = await _team_ratings(db)
    ratings = ctx.get("ratings") or {}
    print(f"  teams_indexed: {len(ratings)}")
    sample = list(ratings.items())[:3]
    for t, r in sample:
        print(f"    - {t}: rating={r.get('rating'):.2f} ppg={r.get('ppg'):.1f} "
              f"opp_ppg={r.get('opp_ppg'):.1f}")
    if not ratings:
        print("  ⚠ EMPTY — _team_ratings returned no rows. "
              "This is NFL root cause.")
asyncio.run(_run())
PY

echo
echo "=== 4.  NHL _team_recent_stats smoke-test on a current team ==="
python3 - <<'PY'
import os, asyncio, sys
sys.path.insert(0, "/app/backend")
async def _run():
    from services.database import get_database
    from services.nhl_feature_engine import _team_recent_stats
    db = get_database()
    # Try a handful of likely-current NHL team names
    for name in ("Edmonton Oilers", "Florida Panthers", "Toronto Maple Leafs",
                 "Detroit", "TOR"):
        s = await _team_recent_stats(db, name)
        print(f"  {name!r:30s}  games={s.get('games'):>3d}  "
              f"gf_mean={s.get('gf_mean', 0):.2f}  "
              f"ga_mean={s.get('ga_mean', 0):.2f}")
asyncio.run(_run())
PY

echo
echo "=== 5.  Odds API active-sports catalogue (what Perklocks sees now) ==="
curl -s "https://api.the-odds-api.com/v4/sports/?apiKey=${ODDS_API_KEY}" \
  | jq -r '.[] | select(.key|test("nfl|nhl|preseason")) |
           [.key, .active, .has_outrights, .title] | @tsv' \
  | column -t -s$'\t'

echo
echo "=== 6.  Odds API event counts for today — NFL + NHL ==="
TODAY=$(date -u +"%Y-%m-%dT00:00:00Z")
TOMORROW=$(date -u -d "+2 days" +"%Y-%m-%dT00:00:00Z" 2>/dev/null || \
           date -u -v+2d +"%Y-%m-%dT00:00:00Z")
for KEY in americanfootball_nfl americanfootball_nfl_preseason icehockey_nhl; do
  echo "  Fetching events for $KEY ..."
  curl -s "https://api.the-odds-api.com/v4/sports/${KEY}/events/?apiKey=${ODDS_API_KEY}&commenceTimeFrom=${TODAY}&commenceTimeTo=${TOMORROW}" \
    | jq -r 'if type=="array" then
               "    events=\(length)" + if length>0 then
                 ("\n    sample: " + (.[0] | "\(.id) \(.away_team) @ \(.home_team) \(.commence_time)"))
               else "" end
             else "    ERR: \(.)" end'
done

echo
echo "=== 7.  Odds API market coverage for ONE representative NFL event ==="
NFL_EID=$(curl -s "https://api.the-odds-api.com/v4/sports/americanfootball_nfl/events/?apiKey=${ODDS_API_KEY}&commenceTimeFrom=${TODAY}&commenceTimeTo=${TOMORROW}" | jq -r '.[0].id // empty')
if [ -n "${NFL_EID:-}" ]; then
  echo "  event_id=${NFL_EID}"
  curl -s "https://api.the-odds-api.com/v4/sports/americanfootball_nfl/events/${NFL_EID}/odds/?apiKey=${ODDS_API_KEY}&regions=us&markets=h2h,spreads,totals" \
    | jq -r '.bookmakers | length as $n |
             "    bookmakers=\($n)",
             (.[0:3][] | "      \(.key)  markets=" + ([.markets[].key] | tostring))'
else
  echo "  NO NFL event available in next 2 days — expected if slate is empty."
fi

echo
echo "=== 8.  Mongo picks today — zero-generation canary ==="
python3 - <<'PY'
import os, pymongo, datetime
db = pymongo.MongoClient(os.environ["MONGO_URL"]).get_default_database()
today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
for sport in ("NFL", "NHL"):
    total = db.canonical_picks.count_documents({"sport": sport, "date": today})
    published = db.canonical_picks.count_documents({
        "sport": sport, "date": today, "published_state": "PUBLISHED",
    })
    print(f"  {sport}  date={today}  total_rows={total}  published={published}")
PY

echo
echo "=== 9.  funnel_telemetry — latest NFL + NHL rejection reasons ==="
python3 - <<'PY'
import os, pymongo, datetime
db = pymongo.MongoClient(os.environ["MONGO_URL"]).get_default_database()
if "funnel_telemetry" not in db.list_collection_names():
    print("  funnel_telemetry collection not present")
else:
    cutoff = datetime.datetime.now(datetime.timezone.utc) \
             - datetime.timedelta(hours=24)
    pipeline = [
        {"$match": {"sport": {"$in": ["NFL", "NHL"]},
                    "ts":   {"$gte": cutoff}}},
        {"$group": {"_id": {"sport": "$sport", "stage": "$stage",
                             "reason": "$reason"},
                     "n": {"$sum": 1}}},
        {"$sort": {"_id.sport": 1, "n": -1}},
    ]
    for row in db.funnel_telemetry.aggregate(pipeline):
        print(f"  {row['_id']['sport']:4s} {row['_id']['stage']:15s} "
              f"{row['_id']['reason']:40s} count={row['n']}")
PY

echo
echo "=== 10.  Live /api/picks/today — NFL + NHL filter ==="
curl -s "http://127.0.0.1:8001/api/picks/today" \
  | jq '[.picks[]? | select(.sport=="NFL" or .sport=="NHL") |
         {sport, market, selection, book_odds, bookmaker, odds_source,
          no_real_book_line}] |
        {total: length,
         sample: (.[0:5])}'

echo
echo "=== END — paste full output to the agent ==="
