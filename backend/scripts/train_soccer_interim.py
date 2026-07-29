"""Interim soccer model training — Phase 7 Part 4b.

Trains dual (LightGBM + XGBoost) models for 5 supported soccer prop
stats using ONLY the currently-ingested data (EPL 2024-25).

Each generated .pkl / .meta.json file is stamped with:
    interim: true
    interim_reason: "Trained on partial dataset (EPL 2024-25 only)"
    interim_dataset: {league, season, rows, players}

Auto-retrain via `train_soccer_full.py` will REPLACE these files and
clear the interim flags.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import sys
import time

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, "/app/backend")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("train_soccer_interim")

SOCCER_STATS = ("goals", "assists", "shots", "shots_on_target", "xg",
                 "goal_contributions")
MODEL_DIR = pathlib.Path("/app/backend/models")


async def main() -> None:
    from motor.motor_asyncio import AsyncIOMotorClient
    from ml.train_prop_model import train_soccer

    c = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = c["lockscore_db"]

    # Coverage snapshot BEFORE training.
    total_rows = await db.soccer_player_game_logs.count_documents({})
    leagues_seasons = await db.soccer_player_game_logs.aggregate([
        {"$group": {"_id": {"l": "$league", "s": "$season"},
                     "n": {"$sum": 1}}},
    ]).to_list(20)
    log.info("Coverage snapshot: %s rows across %s league-seasons",
              total_rows, len(leagues_seasons))
    for ls in leagues_seasons:
        log.info("  %s %s: %s rows", ls["_id"]["l"], ls["_id"]["s"], ls["n"])

    results: dict[str, dict] = {}
    for stat in SOCCER_STATS:
        try:
            log.info("=== Training INTERIM soccer/%s ===", stat)
            t0 = time.time()
            meta = await train_soccer(
                stat=stat,
                split_date="2025-01-01",  # EPL 2024-25 split
                min_prior_matches=5,
            )
            elapsed = round(time.time() - t0, 1)
            log.info("  soccer/%s trained in %.1fs · winner=%s · "
                      "LGB MAE=%.3f XGB MAE=%.3f",
                      stat, elapsed, meta["winner"],
                      meta["lgbm"]["mae"], meta["xgb"]["mae"])

            # Stamp INTERIM flag on the meta json in place.
            meta_path = MODEL_DIR / f"soccer_{stat}.meta.json"
            with open(meta_path) as f:
                m = json.load(f)
            m["interim"] = True
            m["interim_reason"] = "Trained on partial dataset (EPL 2024-25 only)"
            m["interim_dataset"] = {
                "leagues":       ["EPL"],
                "seasons":       [2024],
                "total_rows":    total_rows,
                "trained_at":    m.get("trained_at"),
            }
            with open(meta_path, "w") as f:
                json.dump(m, f, indent=2, default=str)
            results[stat] = {
                "winner":  meta["winner"],
                "lgb_mae": meta["lgbm"]["mae"],
                "xgb_mae": meta["xgb"]["mae"],
                "elapsed": elapsed,
                "interim": True,
            }
        except SystemExit as e:
            log.warning("soccer/%s SKIPPED: %s", stat, e)
            results[stat] = {"error": str(e)}
        except Exception as e:
            log.exception("soccer/%s FAILED: %s", stat, e)
            results[stat] = {"error": str(e)}

    with open("/tmp/soccer_train_interim.json", "w") as f:
        json.dump(results, f, indent=2, default=str)
    log.info("=== INTERIM training DONE ===")
    log.info("results: %s", json.dumps(results, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
