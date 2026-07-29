"""Full soccer retrain — triggered automatically by the Big 5 ingestion
script when it completes.

Trains the same 5 stats as the interim run (`train_soccer_interim.py`)
but on the FULL Big 5 × 3-seasons dataset. Preserves the before/after
metrics in `/tmp/soccer_train_full.json` and REPLACES the interim
models in place (clearing the `interim: true` flags in `meta.json`).
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
log = logging.getLogger("train_soccer_full")

SOCCER_STATS = ("goals", "assists", "shots", "shots_on_target", "xg",
                 "goal_contributions")
MODEL_DIR = pathlib.Path("/app/backend/models")


async def main() -> None:
    from motor.motor_asyncio import AsyncIOMotorClient
    from ml.train_prop_model import train_soccer

    c = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = c["lockscore_db"]

    total_rows = await db.soccer_player_game_logs.count_documents({})
    leagues = await db.soccer_player_game_logs.distinct("league")
    seasons = sorted(await db.soccer_player_game_logs.distinct("season"))
    log.info("Full-retrain dataset: %s rows · leagues=%s · seasons=%s",
              total_rows, leagues, seasons)

    # Load interim metrics for before/after comparison.
    interim_metrics: dict[str, dict] = {}
    interim_summary = pathlib.Path("/tmp/soccer_train_interim.json")
    if interim_summary.exists():
        try:
            interim_metrics = json.loads(interim_summary.read_text())
        except Exception:
            interim_metrics = {}

    results: dict[str, dict] = {}
    for stat in SOCCER_STATS:
        try:
            log.info("=== Training FULL soccer/%s ===", stat)
            t0 = time.time()
            meta = await train_soccer(
                stat=stat,
                split_date="2025-06-01",   # 2025-26 season = validation
                min_prior_matches=5,
            )
            elapsed = round(time.time() - t0, 1)

            # Clear the interim flag on the fresh meta json.
            meta_path = MODEL_DIR / f"soccer_{stat}.meta.json"
            with open(meta_path) as f:
                m = json.load(f)
            m["interim"] = False
            m.pop("interim_reason", None)
            m.pop("interim_dataset", None)
            m["full_dataset"] = {
                "leagues":  leagues,
                "seasons":  seasons,
                "n_rows":   total_rows,
            }
            with open(meta_path, "w") as f:
                json.dump(m, f, indent=2, default=str)

            before = interim_metrics.get(stat, {})
            results[stat] = {
                "winner":     meta["winner"],
                "lgb_mae":    meta["lgbm"]["mae"],
                "xgb_mae":    meta["xgb"]["mae"],
                "elapsed":    elapsed,
                "before_lgb": before.get("lgb_mae"),
                "before_xgb": before.get("xgb_mae"),
                "delta_lgb":  (round(before.get("lgb_mae", 0) -
                                      meta["lgbm"]["mae"], 4)
                               if before else None),
                "delta_xgb":  (round(before.get("xgb_mae", 0) -
                                      meta["xgb"]["mae"], 4)
                               if before else None),
                "interim":    False,
            }
            log.info(
                "  soccer/%s trained in %.1fs · winner=%s · LGB MAE=%.3f "
                "(Δ=%s) · XGB MAE=%.3f (Δ=%s)",
                stat, elapsed, meta["winner"],
                meta["lgbm"]["mae"], results[stat]["delta_lgb"],
                meta["xgb"]["mae"], results[stat]["delta_xgb"],
            )
        except SystemExit as e:
            log.warning("soccer/%s SKIPPED: %s", stat, e)
            results[stat] = {"error": str(e)}
        except Exception as e:
            log.exception("soccer/%s FAILED: %s", stat, e)
            results[stat] = {"error": str(e)}

    with open("/tmp/soccer_train_full.json", "w") as f:
        json.dump({
            "dataset_rows":   total_rows,
            "leagues":        leagues,
            "seasons":        seasons,
            "results":        results,
        }, f, indent=2, default=str)
    log.info("=== FULL retrain DONE ===")
    log.info("results: %s", json.dumps(results, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
