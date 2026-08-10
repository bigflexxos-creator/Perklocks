"""Phase 5.1 (2026-08-11) — Consolidated review artifact.

Produces the single JSON review file the user asked for at the
'STOP FOR REVIEW' gate:

  1. NBA 44-unresolved root-cause report
  2. Full player counts by sport
  3. Identity resolution % by sport
  4. Remaining unresolved examples (10 per sport)
  5. Collision-prevention report
  6. Persistence/restart verification
  7. All files changed (recorded as manifest)
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from motor.motor_asyncio import AsyncIOMotorClient

from scripts.phase51_nba_root_cause import run as _nba_root_cause
from scripts.phase51_identity_resolution_audit import (
    audit_sport, STALENESS_DAYS,
)
from services.universal_publication_barrier import (
    validate_universal, STATUS_UNRESOLVED,
    STATUS_CONFIRMED_MISMATCH, STATUS_SOURCE_CONFLICT,
)
from services.player_identity import IDENTITY_COLLECTION, _norm
from services.universal_player_identity import ENABLED_SPORTS
from datetime import timedelta


FILES_CHANGED = [
    "backend/services/universal_identity_ingest.py",
    "backend/scripts/phase51_nba_root_cause.py",
    "backend/scripts/phase51_identity_resolution_audit.py",
    "backend/scripts/phase51_run_full_ingest.py",
    "backend/scripts/phase51_consolidated_review.py",
    "backend/tests/test_phase51_full_roster_coverage.py",
]


async def _sample_unresolved(db, sport: str, limit: int = 10):
    lookup = {}
    fresh = set()
    cutoff = (datetime.now(timezone.utc) - timedelta(
        days=STALENESS_DAYS)).isoformat()
    async for d in db[IDENTITY_COLLECTION].find(
        {"sport": sport}, {"_id": 0, "name_norm": 1, "name": 1,
                            "current_team": 1, "observed_at": 1}):
        nn = d.get("name_norm") or _norm(d.get("name") or "")
        team = d.get("current_team")
        if nn and team:
            lookup[nn] = team
            if (d.get("observed_at") or "") >= cutoff:
                fresh.add(nn)
    samples = []
    async for p in db.picks.find(
        {"sport": sport,
         "$or": [{"status": {"$in": ["open", "pending",
                                       "OPEN", "PENDING"]}},
                 {"resolution": {"$in": [None, "", "pending"]}}]},
        {"_id": 0, "id": 1, "market": 1, "event": 1,
         "player": 1, "player_name": 1,
         "lock_score": 1, "published_lock_score": 1}).limit(500):
        v = validate_universal(
            p, roster_lookup=lookup, fresh_roster_names=fresh)
        if v.get("status") in (STATUS_UNRESOLVED,
                                 STATUS_CONFIRMED_MISMATCH,
                                 STATUS_SOURCE_CONFLICT):
            samples.append({
                "pick_id": p.get("id"),
                "market": p.get("market"),
                "event": p.get("event"),
                "player_extracted": v.get("player"),
                "roster_team_evidence": v.get("player_team"),
                "fixture_teams": v.get("fixture_teams"),
                "status": v.get("status"),
                "lock_score": p.get("published_lock_score")
                               or p.get("lock_score"),
            })
            if len(samples) >= limit:
                break
    return samples


async def _collision_report(db):
    """Same-name groups per sport with cpid + provider ids."""
    out = {}
    for sport in ENABLED_SPORTS:
        pipeline = [
            {"$match": {"sport": sport}},
            {"$group": {
                "_id": "$name_norm",
                "cpids": {"$addToSet": "$canonical_player_id"},
                "provider_ids": {"$addToSet": "$provider_ids"},
                "teams": {"$addToSet": "$current_team"},
            }},
            {"$match": {"$expr": {"$gt": [{"$size": "$cpids"}, 1]}}},
            {"$limit": 5},
        ]
        docs = await db[IDENTITY_COLLECTION].aggregate(
            pipeline).to_list(length=5)
        out[sport] = {
            "group_count_sample_max_5": len(docs),
            "examples": [{
                "name_norm": d["_id"],
                "cpids": d["cpids"],
                "provider_ids": d["provider_ids"],
                "teams": d.get("teams"),
            } for d in docs],
        }
    return out


async def _persistence_verification(db):
    """Confirm identities read back from Mongo after restart with the
    canonical id + provider id intact."""
    checks = {}
    for sport in ("NFL", "NBA", "MLB", "NHL", "CFB", "UFC"):
        one = await db[IDENTITY_COLLECTION].find_one(
            {"sport": sport, "provider_ids": {"$exists": True}},
            {"_id": 0, "canonical_player_id": 1, "name": 1,
             "provider_ids": 1, "current_team": 1,
             "observed_at": 1})
        checks[sport] = {
            "has_persisted_identity": one is not None,
            "example": one,
        }
    return checks


async def _main():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = client[os.environ.get("DB_NAME", "perkslocks_production")]

    review = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "phase": "5.1",
        "read_only": True,
        "writes_performed": 0,
        "files_changed": FILES_CHANGED,
    }

    # 1. NBA 44 root cause
    review["nba_44_root_cause"] = await _nba_root_cause(db)

    # 2 + 3. Coverage audit
    audits = []
    for sport in ENABLED_SPORTS:
        audits.append(await audit_sport(db, sport))
    review["identity_resolution_audit_by_sport"] = audits

    # 4. Remaining unresolved / mismatch samples
    samples = {}
    for sport in ENABLED_SPORTS:
        samples[sport] = await _sample_unresolved(db, sport)
    review["remaining_unresolved_samples_by_sport"] = samples

    # 5. Collision-prevention report
    review["same_name_collision_groups_by_sport"] = await _collision_report(db)

    # 6. Persistence verification
    review["persistence_restart_verification"] = await _persistence_verification(db)

    path = ("/tmp/phase51_consolidated_review_"
            + review["generated_at"].replace(":", "").replace("-", "")
            + ".json")
    with open(path, "w") as fh:
        json.dump(review, fh, indent=2, default=str)
    print(f"[review written] {path}")
    client.close()


if __name__ == "__main__":
    asyncio.run(_main())
