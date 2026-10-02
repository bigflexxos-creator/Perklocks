"""READ-ONLY cross-environment DB inventory  ·  2026-10-02
==========================================================

Run from a Production shell (or any pod with the same Mongo client
drivers) to emit a byte-for-byte comparable snapshot of the Preview
inventory I just produced.  NO mutations, NO network writes, NO
schema changes — only ``find`` / ``count_documents`` /
``estimated_document_count``.

Usage
-----
From inside the Production backend container::

    python3 /app/backend/scripts/db_inventory_readonly.py

Or ad-hoc, from any shell that can see Production's Mongo::

    MONGO_URL='<prod-uri>' DB_NAME='lockscore_db' \\
        python3 /app/backend/scripts/db_inventory_readonly.py

The script will exit non-zero only if it cannot connect — never
because of missing data.  Compare the resulting block to the Preview
inventory to decide which DB is authoritative.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

try:
    from pymongo import MongoClient
except ImportError:                                    # pragma: no cover
    sys.stderr.write("pymongo not installed — pip install pymongo\n")
    raise SystemExit(2)


def row(label: str, value) -> None:
    print(f"  {label:<45}  {value}")


def main() -> None:
    uri = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
    dbn = os.environ.get("DB_NAME",   "lockscore_db")
    c = MongoClient(uri, serverSelectionTimeoutMS=5000)
    db = c[dbn]

    print("=" * 90)
    print(f"DB INVENTORY  ·  {datetime.now(timezone.utc).isoformat()}  ·  READ-ONLY")
    print(f"db_name = {dbn}   host_class = "
          f"{'atlas_srv' if uri.startswith('mongodb+srv://') else ('local' if 'localhost' in uri else 'remote')}")
    print("=" * 90)

    row("pick_count (total picks)",
        db.picks.estimated_document_count())
    latest = list(db.picks.find({}).sort("event_time", -1).limit(1))
    row("latest pick event_time",
        latest[0].get("event_time") if latest else None)
    row("latest pick id",
        latest[0].get("id") if latest else None)
    pub = db.picks.find({"publication_state": {"$exists": True}}
                        ).sort("event_time", -1).limit(1)
    for p in pub:
        row("latest publication_state", p.get("publication_state"))
        row("latest publication revision",
            p.get("board_revision_id") or p.get("revision"))
    try:
        rev_count = db.board_revisions.estimated_document_count()
        row("board_revisions count", rev_count)
        latest_rev = list(db.board_revisions.find({}).sort("created_at", -1).limit(1))
        if latest_rev:
            row("latest board_revisions.created_at", latest_rev[0].get("created_at"))
    except Exception as e:
        row("board_revisions", f"ERR {e}")

    # NFL
    print()
    row("NFL player_game_actuals (total)",
        db.player_game_actuals.count_documents({"sport": "nfl"}))
    row("NFL player_game_actuals 2026-09+",
        db.player_game_actuals.count_documents(
            {"sport": "nfl", "event_time": {"$gte": "2026-09-01"}}))
    row("NFL player_game_actuals 2025 season",
        db.player_game_actuals.count_documents(
            {"sport": "nfl", "event_time":
                {"$gte": "2025-09-01", "$lt": "2026-07-01"}}))
    row("NFL nfl_player_weekly rows",
        db.nfl_player_weekly.estimated_document_count())
    row("NFL games 2026",
        db.games.count_documents(
            {"sport": "nfl", "date": {"$gte": "2026-08-01"}}))
    lp = list(db.player_game_actuals.find(
        {"sport": "nfl", "event_time": {"$ne": None}}).sort("event_time", -1).limit(1))
    row("NFL latest actual event_time",
        lp[0].get("event_time") if lp else None)

    # CFB
    print()
    row("CFB games 2026",
        db.games.count_documents(
            {"sport": "cfb", "date": {"$gte": "2026-08-01"}}))
    row("CFB games 2025",
        db.games.count_documents(
            {"sport": "cfb",
             "date": {"$gte": "2025-08-01", "$lt": "2026-08-01"}}))
    row("CFB player_game_logs 2026",
        db.player_game_logs.count_documents(
            {"sport": "cfb", "season": 2026}))
    row("CFB player_game_logs (total)",
        db.player_game_logs.count_documents({"sport": "cfb"}))
    lg = list(db.games.find({"sport": "cfb"}).sort("date", -1).limit(1))
    row("CFB latest game date",
        lg[0].get("date") if lg else None)

    # MLB
    print()
    row("MLB games postseason (date>=2026-09-28)",
        db.games.count_documents(
            {"sport": "mlb", "date": {"$gte": "2026-09-28"}}))
    row("MLB games regular 2026",
        db.games.count_documents(
            {"sport": "mlb",
             "date": {"$gte": "2026-03-01", "$lt": "2026-09-28"}}))
    row("MLB player_game_logs (total)",
        db.player_game_logs.count_documents({"sport": "mlb"}))
    row("MLB player_game_logs 2026-09+",
        db.player_game_logs.count_documents(
            {"sport": "mlb", "event_time": {"$gte": "2026-09-01"}}))
    lm = list(db.games.find({"sport": "mlb"}).sort("date", -1).limit(1))
    row("MLB latest game date",
        lm[0].get("date") if lm else None)

    # NHL
    print()
    row("NHL games (total)",
        db.games.count_documents({"sport": "nhl"}))
    row("NHL player_game_logs (total)",
        db.player_game_logs.count_documents({"sport": "nhl"}))

    # Soccer
    print()
    row("Soccer games (total)",
        db.games.count_documents({"sport": "soccer"}))
    row("Soccer soccer_matches",
        db.soccer_matches.estimated_document_count())
    row("Soccer soccer_player_game_logs",
        db.soccer_player_game_logs.estimated_document_count())
    row("Soccer player_game_actuals",
        db.player_game_actuals.count_documents({"sport": "soccer"}))
    ls = list(db.player_game_actuals.find({"sport": "soccer"}
                                          ).sort("event_time", -1).limit(1))
    row("Soccer latest actual event_time",
        ls[0].get("event_time") if ls else None)

    # Tennis
    print()
    row("Tennis tennis_matches_history",
        db.tennis_matches_history.estimated_document_count())
    row("Tennis player_game_actuals",
        db.player_game_actuals.count_documents({"sport": "tennis"}))
    lt = list(db.player_game_actuals.find({"sport": "tennis"}
                                          ).sort("event_time", -1).limit(1))
    row("Tennis latest actual event_time",
        lt[0].get("event_time") if lt else None)

    # Historical ingestion state
    print()
    row("historical_ingestion_state docs",
        db.historical_ingestion_state.estimated_document_count())
    for d in (db.historical_ingestion_state.find({})
              .sort("last_run_at", -1).limit(10)):
        row(f"  state.{d.get('sport'):8s} s={d.get('season')}",
            f"last_run={d.get('last_run_at') or d.get('last_successful_ingestion')}")

    row("historical_freshness docs",
        db.historical_freshness.estimated_document_count())
    for d in db.historical_freshness.find({}):
        row(f"  freshness.{d.get('sport'):8s}",
            f"status={d.get('status')} ev={d.get('events_ingested')} "
            f"logs={d.get('player_logs_inserted')}")

    # Known canaries
    print()
    row("Brock Purdy actuals (00-0037834)",
        db.player_game_actuals.count_documents(
            {"sport": "nfl", "canonical_player_id": "00-0037834"}))
    row("Sam LaPorta actuals (name match)",
        db.player_game_actuals.count_documents(
            {"sport": "nfl",
             "player_name": {"$regex": r"^Sam\s+LaPorta", "$options": "i"}}))
    row("Delaware 2026 games",
        db.games.count_documents(
            {"sport": "cfb", "date": {"$gte": "2026-08-01"},
             "$or": [
                 {"home": {"$regex": "Delaware Blue", "$options": "i"}},
                 {"away": {"$regex": "Delaware Blue", "$options": "i"}},
             ]}))

    print()
    print("=" * 90)
    print("END OF INVENTORY — no mutations, no network writes")
    print("=" * 90)


if __name__ == "__main__":
    main()
