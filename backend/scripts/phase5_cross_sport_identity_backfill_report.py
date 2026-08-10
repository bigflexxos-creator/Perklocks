"""Phase 5 (2026-08-11) — READ-ONLY cross-sport identity backfill report.

This script is EXPLICITLY read-only: it MUST NOT write to Mongo, MUST
NOT quarantine picks, MUST NOT deploy anything.  It is the safety
gate before any subsequent backfill / write pass is authorised.

It walks every OPEN player-based pick per sport and reports:

    identities_resolved              — resolved via provider id or exact name
    unresolved                        — no fresh identity evidence
    collisions_prevented              — same-name / different-provider ids
                                        avoided a bad merge
    current_team_mismatches           — confirmed_mismatch verdict from the
                                        universal barrier
    history_rows_linked               — how many rows attach to a cpid
    high_confidence_picks_affected    — count of picks with lock_score > 85
                                        that would be blocked / degraded
    roster_source                     — the ROSTER_SOURCE the adapter
                                        actually used for that sport
    threshold_history_ready           — # of players with ≥ 3 history rows

Output: JSON file at /tmp/phase5_backfill_report_<ts>.json plus
formatted stdout summary.

Usage:
    cd /app/backend && python -m scripts.phase5_cross_sport_identity_backfill_report
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

# Ensure backend is importable when invoked from /app/backend.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from motor.motor_asyncio import AsyncIOMotorClient

from services import sport_adapters
from services.universal_player_identity import (
    ENABLED_SPORTS, PLAYER_HISTORY_COLLECTION,
)
from services.universal_publication_barrier import (
    validate_universal, STATUS_VERIFIED, STATUS_UNRESOLVED,
    STATUS_SOURCE_CONFLICT, STATUS_CONFIRMED_MISMATCH,
)
from services.player_identity import IDENTITY_COLLECTION


LOCKS_THRESHOLD = 85.0   # strict >85 — DO NOT change


async def _load_roster_lookup_for_sport(
    db, sport: str,
) -> tuple[dict[str, str], set[str], str]:
    """Build ``(name_norm → current_team, fresh_names, source_used)``
    from ``db.player_identities`` for a given sport.  Only fresh
    (within staleness window) observations are considered fresh.
    """
    from services.player_identity import _norm as _n
    lookup: dict[str, str] = {}
    fresh: set[str] = set()
    src = "player_identities"
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    async for d in db[IDENTITY_COLLECTION].find(
        {"sport": sport}, {"_id": 0, "name_norm": 1, "name": 1,
                             "current_team": 1, "observed_at": 1,
                             "source": 1}):
        nn = d.get("name_norm") or _n(d.get("name") or "")
        team = d.get("current_team")
        if not nn or not team:
            continue
        lookup[nn] = team
        if (d.get("observed_at") or "") >= cutoff:
            fresh.add(nn)
    return lookup, fresh, src


async def _pick_snapshot_for_sport(db, sport: str) -> list[dict]:
    """Snapshot the current OPEN/PENDING pick population per sport."""
    q = {"sport": sport,
         "$or": [{"status": {"$in": ["open", "pending", "OPEN", "PENDING"]}},
                  {"resolution": {"$in": [None, "", "pending"]}}]}
    cursor = db.picks.find(
        q, {"_id": 0, "id": 1, "sport": 1, "market": 1, "event": 1,
              "player": 1, "player_name": 1, "selection": 1,
              "home_team": 1, "away_team": 1, "league": 1,
              "lock_score": 1, "published_lock_score": 1,
              "publication_source": 1, "book_odds": 1,
              "no_real_book_line": 1}).limit(5000)
    return [d async for d in cursor]


async def _history_stats(db, sport: str) -> tuple[int, int]:
    """(rows_linked_for_sport, players_with_at_least_3_rows)."""
    rows = await db[PLAYER_HISTORY_COLLECTION].count_documents(
        {"sport": sport})
    pipeline = [
        {"$match": {"sport": sport}},
        {"$group": {"_id": "$canonical_player_id", "n": {"$sum": 1}}},
        {"$match": {"n": {"$gte": 3}}},
        {"$count": "players"},
    ]
    doc = await db[PLAYER_HISTORY_COLLECTION].aggregate(
        pipeline).to_list(length=1)
    threshold_ready = doc[0]["players"] if doc else 0
    return rows, threshold_ready


async def _collision_scan(db, sport: str) -> int:
    """Count same-name / different-provider-id groupings — these are
    where a name-only merge would have been wrong.  This is a
    'prevented collisions' proxy — the current registry has already
    minted DIFFERENT canonical_player_ids for these groups."""
    pipeline = [
        {"$match": {"sport": sport}},
        {"$group": {"_id": "$name_norm",
                    "ids": {"$addToSet": "$canonical_player_id"}}},
        {"$match": {"$expr": {"$gt": [{"$size": "$ids"}, 1]}}},
        {"$count": "collisions"},
    ]
    doc = await db[IDENTITY_COLLECTION].aggregate(
        pipeline).to_list(length=1)
    return doc[0]["collisions"] if doc else 0


async def scan_sport(db, sport: str) -> dict[str, Any]:
    adapter = sport_adapters.get_adapter(sport)
    roster_source = adapter.ROSTER_SOURCE if adapter else "unknown"
    roster_lookup, fresh, _ = await _load_roster_lookup_for_sport(db, sport)
    picks = await _pick_snapshot_for_sport(db, sport)
    identities_resolved = 0
    unresolved = 0
    current_team_mismatches = 0
    source_conflicts = 0
    high_conf_affected = 0
    # Small operator-review samples (bounded — this is a summary
    # report, not a full dump).
    mismatch_samples: list[dict[str, Any]] = []
    conflict_samples: list[dict[str, Any]] = []
    for p in picks:
        v = validate_universal(
            p, roster_lookup=roster_lookup, fresh_roster_names=fresh)
        status = v.get("status")
        if status == STATUS_VERIFIED:
            identities_resolved += 1
        elif status == STATUS_UNRESOLVED:
            unresolved += 1
        elif status == STATUS_CONFIRMED_MISMATCH:
            current_team_mismatches += 1
            if len(mismatch_samples) < 10:
                mismatch_samples.append({
                    "pick_id": p.get("id"),
                    "market": p.get("market"),
                    "event": p.get("event"),
                    "player_extracted": v.get("player"),
                    "player_team_evidence": v.get("player_team"),
                    "fixture_teams": v.get("fixture_teams"),
                    "lock_score": p.get("published_lock_score")
                                  or p.get("lock_score"),
                })
        elif status == STATUS_SOURCE_CONFLICT:
            source_conflicts += 1
            if len(conflict_samples) < 10:
                conflict_samples.append({
                    "pick_id": p.get("id"),
                    "market": p.get("market"),
                    "event": p.get("event"),
                    "player_extracted": v.get("player"),
                    "player_team_evidence": v.get("player_team"),
                    "fixture_teams": v.get("fixture_teams"),
                    "lock_score": p.get("published_lock_score")
                                  or p.get("lock_score"),
                })
        # High-confidence Locks affected only when status != VERIFIED
        # (i.e. would be blocked/quarantined by a future write-pass).
        if status != STATUS_VERIFIED:
            ls = p.get("published_lock_score") or p.get("lock_score") or 0
            try:
                if float(ls) > LOCKS_THRESHOLD:
                    high_conf_affected += 1
            except (TypeError, ValueError):
                pass
    collisions_prevented = await _collision_scan(db, sport)
    rows_linked, threshold_ready = await _history_stats(db, sport)
    return {
        "sport": sport,
        "roster_source": roster_source,
        "picks_scanned": len(picks),
        "identities_resolved": identities_resolved,
        "unresolved": unresolved,
        "source_conflicts": source_conflicts,
        "current_team_mismatches": current_team_mismatches,
        "collisions_prevented": collisions_prevented,
        "history_rows_linked": rows_linked,
        "high_confidence_picks_affected_gt_85": high_conf_affected,
        "threshold_history_ready_players": threshold_ready,
        "mismatch_samples": mismatch_samples,
        "source_conflict_samples": conflict_samples,
    }


async def run_report(mongo_url: str, db_name: str) -> dict[str, Any]:
    client = AsyncIOMotorClient(mongo_url)
    db = client[db_name]
    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "writes_performed": 0,
        "locks_threshold": LOCKS_THRESHOLD,
        "notes": [
            "READ-ONLY dry run — no Mongo writes, no quarantine, no deploy.",
            "This is Phase 5 gate #1: identity + roster coverage sanity.",
            "Only status='confirmed_mismatch' would be a hard-reject "
            "if a subsequent write pass were authorised.  'unresolved' "
            "and 'source_conflict' remain quarantine-eligible.",
        ],
        "sports": [],
        "totals": {},
    }
    totals = {"picks_scanned": 0, "identities_resolved": 0,
               "unresolved": 0, "source_conflicts": 0,
               "current_team_mismatches": 0,
               "collisions_prevented": 0, "history_rows_linked": 0,
               "high_confidence_picks_affected_gt_85": 0,
               "threshold_history_ready_players": 0}
    for sport in ENABLED_SPORTS:
        r = await scan_sport(db, sport)
        report["sports"].append(r)
        for k in totals:
            totals[k] += r.get(k, 0)
    report["totals"] = totals
    client.close()
    return report


def _write_report(report: dict[str, Any]) -> str:
    ts = report["generated_at"].replace(":", "").replace("-", "")
    path = f"/tmp/phase5_backfill_report_{ts}.json"
    with open(path, "w") as fh:
        json.dump(report, fh, indent=2)
    return path


def _pretty_print(report: dict[str, Any]) -> None:
    print("=" * 72)
    print("Phase 5 — READ-ONLY Cross-Sport Identity Backfill Report")
    print("=" * 72)
    print(f"Generated at:   {report['generated_at']}")
    print(f"Read-only:      {report['read_only']}")
    print(f"Writes:         {report['writes_performed']}")
    print(f"Locks threshold: strict > {report['locks_threshold']}")
    print()
    for r in report["sports"]:
        print(f"── {r['sport']} ({r['roster_source']}) ──")
        print(f"    picks scanned:                      {r['picks_scanned']}")
        print(f"    identities resolved:                {r['identities_resolved']}")
        print(f"    unresolved:                         {r['unresolved']}")
        print(f"    source conflicts:                   {r['source_conflicts']}")
        print(f"    current-team mismatches (confirmed): {r['current_team_mismatches']}")
        print(f"    same-name collisions prevented:      {r['collisions_prevented']}")
        print(f"    history rows linked:                 {r['history_rows_linked']}")
        print(f"    threshold-history-ready players:     {r['threshold_history_ready_players']}")
        print(f"    high-conf (>85) picks affected:      {r['high_confidence_picks_affected_gt_85']}")
        print()
    t = report["totals"]
    print("── TOTAL ──")
    for k, v in t.items():
        print(f"    {k}: {v}")


async def _main():
    mongo_url = os.environ["MONGO_URL"]
    db_name = os.environ.get("DB_NAME", "perkslocks_production")
    report = await run_report(mongo_url, db_name)
    path = _write_report(report)
    _pretty_print(report)
    print(f"\n[report written] {path}")


if __name__ == "__main__":
    asyncio.run(_main())


__all__ = ["run_report", "scan_sport"]
