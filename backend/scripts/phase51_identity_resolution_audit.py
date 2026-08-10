"""Phase 5.1 (2026-08-11) — READ-ONLY identity resolution audit.

Reports per-sport:

    total_active_identities        # in db.player_identities
    fresh_identities               # observed_at within staleness window
    stale_identities
    with_provider_id
    same_name_groups               # different cpid, same name_norm
    duplicate_provider_id_conflicts
    picks_scanned
    picks_resolved
    picks_unresolved
    source_conflicts
    confirmed_mismatches
    high_conf_gt_85_unresolved
    resolution_pct

This script writes ONLY to /tmp — never to Mongo.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from motor.motor_asyncio import AsyncIOMotorClient

from services.universal_publication_barrier import (
    validate_universal, STATUS_UNRESOLVED, STATUS_SOURCE_CONFLICT,
    STATUS_CONFIRMED_MISMATCH,
)
from services.player_identity import IDENTITY_COLLECTION, _norm
from services import sport_adapters
from services.universal_player_identity import ENABLED_SPORTS


STALENESS_DAYS = 30
LOCKS_THRESHOLD = 85.0


async def _load_lookup(db, sport: str) -> tuple[dict, set, int, int, int, int]:
    lookup: dict[str, str] = {}
    fresh: set[str] = set()
    total = 0
    fresh_ct = 0
    with_pid = 0
    same_name_groups = 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=STALENESS_DAYS)).isoformat()
    seen_norms: dict[str, set[str]] = {}
    async for d in db[IDENTITY_COLLECTION].find(
        {"sport": sport}, {"_id": 0, "name": 1, "name_norm": 1,
                            "current_team": 1, "observed_at": 1,
                            "canonical_player_id": 1,
                            "provider_ids": 1}):
        total += 1
        nn = d.get("name_norm") or _norm(d.get("name") or "")
        if nn:
            seen_norms.setdefault(nn, set()).add(
                d.get("canonical_player_id") or "")
        team = d.get("current_team")
        if nn and team:
            lookup[nn] = team
            if (d.get("observed_at") or "") >= cutoff:
                fresh.add(nn)
                fresh_ct += 1
        if d.get("provider_ids"):
            with_pid += 1
    for nn, cids in seen_norms.items():
        if len(cids) > 1:
            same_name_groups += 1
    stale = total - fresh_ct
    return lookup, fresh, total, fresh_ct, stale, with_pid, same_name_groups


async def audit_sport(db, sport: str) -> dict[str, Any]:
    adapter = sport_adapters.get_adapter(sport)
    roster_source = adapter.ROSTER_SOURCE if adapter else "unknown"
    (lookup, fresh, total, fresh_ct, stale, with_pid,
     same_name_groups) = await _load_lookup(db, sport)

    # ── Phase 5.2 (2026-08-11) — Soccer needs 3 additional lookups
    #    (national team, nationality + freshness).  Non-Soccer sports
    #    stay on the club-only path.
    soccer_extras: dict[str, Any] = {}
    if sport == "Soccer":
        from services.universal_soccer_lookup import build_soccer_lookups
        L = await build_soccer_lookups(db, staleness_days=STALENESS_DAYS)
        lookup = L["roster_lookup"]
        fresh = L["fresh_roster_names"]
        soccer_extras = {
            "national_team_lookup": L["national_team_lookup"],
            "fresh_national_team_names": L["fresh_national_team_names"],
            "nationality_lookup": L["nationality_lookup"],
        }

    picks_scanned = 0
    picks_resolved = 0
    picks_unresolved = 0
    source_conflicts = 0
    confirmed_mismatches = 0
    high_conf_unresolved = 0

    async for p in db.picks.find(
        {"sport": sport,
         "$or": [{"status": {"$in": ["open", "pending",
                                       "OPEN", "PENDING"]}},
                 {"resolution": {"$in": [None, "", "pending"]}}]},
        {"_id": 0, "id": 1, "sport": 1, "market": 1, "event": 1,
         "player": 1, "player_name": 1, "selection": 1,
         "home_team": 1, "away_team": 1, "league": 1,
         "lock_score": 1, "published_lock_score": 1}):
        picks_scanned += 1
        v = validate_universal(
            p, roster_lookup=lookup, fresh_roster_names=fresh,
            **soccer_extras)
        s = v.get("status")
        if s == "verified":
            picks_resolved += 1
        elif s == STATUS_UNRESOLVED:
            picks_unresolved += 1
            ls = p.get("published_lock_score") or p.get("lock_score") or 0
            try:
                if float(ls) > LOCKS_THRESHOLD:
                    high_conf_unresolved += 1
            except (TypeError, ValueError):
                pass
        elif s == STATUS_SOURCE_CONFLICT:
            source_conflicts += 1
        elif s == STATUS_CONFIRMED_MISMATCH:
            confirmed_mismatches += 1
    resolution_pct = round(
        100.0 * picks_resolved / picks_scanned, 2) if picks_scanned else None
    return {
        "sport": sport,
        "roster_source": roster_source,
        "total_active_identities": total,
        "fresh_identities": fresh_ct,
        "stale_identities": stale,
        "with_provider_id": with_pid,
        "same_name_collision_groups": same_name_groups,
        "picks_scanned": picks_scanned,
        "picks_resolved": picks_resolved,
        "picks_unresolved": picks_unresolved,
        "source_conflicts": source_conflicts,
        "confirmed_mismatches": confirmed_mismatches,
        "high_conf_gt_85_unresolved": high_conf_unresolved,
        "resolution_pct": resolution_pct,
    }


async def run_audit(mongo_url: str, db_name: str) -> dict[str, Any]:
    client = AsyncIOMotorClient(mongo_url)
    db = client[db_name]
    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "writes_performed": 0,
        "locks_threshold": LOCKS_THRESHOLD,
        "staleness_days": STALENESS_DAYS,
        "sports": [],
    }
    for sport in ENABLED_SPORTS:
        report["sports"].append(await audit_sport(db, sport))
    client.close()
    return report


def _print_report(r):
    print("=" * 78)
    print("Phase 5.1 — READ-ONLY Identity Resolution Audit")
    print("=" * 78)
    print(f"Generated at: {r['generated_at']}")
    print(f"Read-only: {r['read_only']}   Writes: {r['writes_performed']}")
    print(f"Locks strict > {r['locks_threshold']}, staleness = {r['staleness_days']}d")
    print()
    for s in r["sports"]:
        print(f"── {s['sport']} ({s['roster_source']}) ──")
        print(f"    identity universe:        {s['total_active_identities']:>7d}"
              f"   fresh: {s['fresh_identities']:>7d}   stale: {s['stale_identities']:>7d}")
        print(f"    with provider_id:         {s['with_provider_id']:>7d}"
              f"   same-name collision groups: {s['same_name_collision_groups']}")
        print(f"    picks scanned:            {s['picks_scanned']:>7d}"
              f"   resolved: {s['picks_resolved']:>7d}   unresolved: {s['picks_unresolved']:>7d}")
        print(f"    source conflicts:         {s['source_conflicts']:>7d}"
              f"   confirmed mismatches: {s['confirmed_mismatches']}")
        print(f"    high-conf (>85) unresolved: {s['high_conf_gt_85_unresolved']:>5d}"
              f"   RESOLUTION: {s['resolution_pct']}%")
        print()


async def _main():
    r = await run_audit(
        os.environ["MONGO_URL"],
        os.environ.get("DB_NAME", "perkslocks_production"))
    path = f"/tmp/phase51_identity_resolution_audit_{r['generated_at'].replace(':','').replace('-','')}.json"
    with open(path, "w") as fh:
        json.dump(r, fh, indent=2)
    _print_report(r)
    print(f"\n[report written] {path}")


if __name__ == "__main__":
    asyncio.run(_main())


__all__ = ["audit_sport", "run_audit"]
