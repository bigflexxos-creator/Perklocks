"""Phase 5.1 (2026-08-11) — Full-roster ingest runner.

Runs :func:`services.universal_identity_ingest.ingest_all` and prints
a compact summary.  Idempotent — safe to re-run.

Usage:
    cd /app/backend && python -m scripts.phase51_run_full_ingest
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from motor.motor_asyncio import AsyncIOMotorClient

from services.universal_identity_ingest import ingest_all
from services import player_identity


async def _main():
    db = AsyncIOMotorClient(os.environ["MONGO_URL"])[
        os.environ.get("DB_NAME", "perkslocks_production")]
    # Idempotent replay — hydrate from Mongo first so upserts land as
    # advanced/merged where appropriate.
    player_identity.reset_registry_for_tests()
    docs = [d async for d in db.player_identities.find(
        {"sport": {"$ne": "Soccer"}}, {"_id": 0})]
    if docs:
        player_identity.hydrate_registry(docs)
    result = await ingest_all(db)
    print(json.dumps(result, indent=2))
    print()
    print("─── DB counts by sport ───")
    for sp in ("NFL", "NBA", "MLB", "NHL", "CFB", "UFC", "Tennis", "Soccer"):
        n = await db.player_identities.count_documents({"sport": sp})
        print(f"  {sp:8s}  {n}")


if __name__ == "__main__":
    asyncio.run(_main())
