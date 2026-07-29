"""Background ingestion runner: Big 5 leagues × 3 seasons (2023-25).
Auto-triggers Part 4b retraining on the FULL dataset when complete.
"""
import asyncio
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv()

from motor.motor_asyncio import AsyncIOMotorClient
from ml.ingestors.soccer_understat import ingest_big5_seasons

CHECKPOINT_JSON = "/tmp/soccer_ingest_big5.json"
LOG_FILE = "/tmp/soccer_ingest_big5.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("big5_ingest")


async def main() -> None:
    log.info("=== Big 5 × 3-seasons ingestion START ===")
    c = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = c["lockscore_db"]

    # Seasons: 2023-24, 2024-25, 2025-26.
    # Understat season is the START year (2023 = 2023-24).
    results = await ingest_big5_seasons(
        db,
        seasons=(2023, 2024, 2025),
        skip_existing=True,   # resume-safe
    )

    with open(CHECKPOINT_JSON, "w") as f:
        json.dump({
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "results":     results,
        }, f, indent=2, default=str)
    log.info("=== Big 5 × 3-seasons ingestion DONE ===")
    log.info("results: %s", json.dumps(results, default=str))

    # Aggregate row count for post-run confidence.
    total = await db.soccer_player_game_logs.count_documents({})
    log.info("Total soccer_player_game_logs rows: %s", total)

    # ─── AUTO-TRIGGER RETRAIN on the FULL dataset ────────────────
    log.info("Auto-triggering retrain on the full dataset…")
    try:
        cmd = [
            sys.executable,
            "/app/backend/scripts/train_soccer_full.py",
        ]
        p = subprocess.Popen(
            cmd,
            stdout=open("/tmp/soccer_retrain_full.log", "w"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        log.info("Retrain launched: PID=%s. Log: /tmp/soccer_retrain_full.log", p.pid)
    except Exception as e:
        log.error("Retrain launch failed: %s", e)


if __name__ == "__main__":
    asyncio.run(main())
