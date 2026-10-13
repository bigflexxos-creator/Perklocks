"""HI Freshness Report — runs ``verify_hi_freshness`` against the real
Preview MongoDB and prints the canonical freshness block per sport.

Usage:
    cd /app/backend && python -m tests.run_hi_freshness_report

Output: one JSON block per sport containing exactly the user-required
fields:

    provider_latest_final
    consumer_latest_final  (alias of consumer_store_latest)
    lag_seconds
    status
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402

from services.hi_freshness_verifier import verify_hi_freshness_all  # noqa: E402


SPORTS = ["nfl", "mlb", "nba", "nhl", "soccer", "tennis", "cfb"]


async def main() -> int:
    mongo_url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
    db_name = os.environ.get("DB_NAME", "lockscore_db")
    client = AsyncIOMotorClient(mongo_url)
    db = client[db_name]
    try:
        payload = await verify_hi_freshness_all(db, sports=SPORTS)
    finally:
        client.close()

    print("=" * 72)
    print("HI FRESHNESS REPORT — Preview MongoDB")
    print(f"MONGO_URL={mongo_url}  DB_NAME={db_name}")
    print("=" * 72)
    header = f"{'sport':>6}  {'status':>18}  {'lag_seconds':>12}  {'provider_latest_final':>28}  {'consumer_latest_final':>28}"
    print(header)
    print("-" * len(header))
    for sport in SPORTS:
        row = payload.get(sport, {})
        print(
            f"{sport:>6}  "
            f"{str(row.get('status') or '-'):>18}  "
            f"{str(row.get('lag_seconds') if row.get('lag_seconds') is not None else '-'):>12}  "
            f"{str(row.get('provider_latest_final') or '-'):>28}  "
            f"{str(row.get('consumer_store_latest') or '-'):>28}"
        )
    print()
    print("Full per-sport payload (JSON) ↓")
    print(json.dumps(payload, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
