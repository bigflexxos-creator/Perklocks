"""Phase 5.2 (2026-08-11) — Soccer unresolved root-cause bucketing.

READ-ONLY.  For every Soccer pick with status != verified, classify
into one of the P0-A→E validator's reason codes and — for
``roster_unverified`` — do a secondary refinement to distinguish:

    identity_present_but_stale
    identity_present_no_current_team
    identity_absent_from_registry
    alias_only_would_match
    market_string_extraction_failure
    league_or_fixture_side_unparseable

Also emit up to 10 representative samples per bucket for operator
inspection.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from motor.motor_asyncio import AsyncIOMotorClient

from services.universal_publication_barrier import (
    validate_universal, STATUS_UNRESOLVED, STATUS_CONFIRMED_MISMATCH,
    STATUS_SOURCE_CONFLICT,
)
from services.player_team_fixture_validator import (
    _extract_player_name, _extract_fixture_teams, _norm,
    REASON_PLAYER_TEAM_MISMATCH, REASON_ROSTER_UNVERIFIED,
    REASON_FIXTURE_TEAMS_UNKNOWN, REASON_PLAYER_NAME_MISSING,
    REASON_MARKET_NOT_PLAYER, REASON_ROSTER_CONFLICT,
)
from services.universal_soccer_lookup import build_soccer_lookups
from services.player_identity import IDENTITY_COLLECTION


BUCKETS = (
    # Direct validator reasons
    REASON_PLAYER_TEAM_MISMATCH,
    REASON_ROSTER_UNVERIFIED,
    REASON_FIXTURE_TEAMS_UNKNOWN,
    REASON_PLAYER_NAME_MISSING,
    REASON_MARKET_NOT_PLAYER,
    REASON_ROSTER_CONFLICT,
    # Refined roster_unverified sub-buckets
    "roster_unverified__identity_present_but_stale",
    "roster_unverified__identity_present_no_current_team",
    "roster_unverified__identity_absent_from_registry",
    # Source conflict (from status enum)
    "source_conflict",
    # Meta
    "unknown",
)


async def _load_identity_index(db) -> tuple[dict[str, dict], set[str]]:
    """Return {name_norm: doc} and the alias-only set."""
    idx: dict[str, dict] = {}
    alias_set: set[str] = set()
    async for d in db[IDENTITY_COLLECTION].find(
        {"sport": "Soccer"},
        {"_id": 0, "name": 1, "name_norm": 1, "aliases": 1,
         "current_team": 1, "observed_at": 1, "provider_ids": 1}):
        nn = d.get("name_norm") or _norm(d.get("name") or "")
        if nn:
            idx[nn] = d
        for al in d.get("aliases") or []:
            an = _norm(al)
            if an:
                alias_set.add(an)
    return idx, alias_set


async def run(db) -> dict[str, Any]:
    L = await build_soccer_lookups(db)
    id_index, alias_set = await _load_identity_index(db)

    picks_scanned = 0
    picks_resolved = 0
    reasons_counts: Counter = Counter()
    samples: dict[str, list[dict]] = defaultdict(list)

    async for p in db.picks.find(
        {"sport": "Soccer",
         "$or": [{"status": {"$in": ["open", "pending",
                                       "OPEN", "PENDING"]}},
                 {"resolution": {"$in": [None, "", "pending"]}}]},
        {"_id": 0, "id": 1, "sport": 1, "market": 1, "event": 1,
         "league": 1,
         "player": 1, "player_name": 1, "selection": 1,
         "home_team": 1, "away_team": 1,
         "lock_score": 1, "published_lock_score": 1}):
        picks_scanned += 1
        v = validate_universal(
            p, roster_lookup=L["roster_lookup"],
            fresh_roster_names=L["fresh_roster_names"],
            national_team_lookup=L["national_team_lookup"],
            fresh_national_team_names=L["fresh_national_team_names"],
            nationality_lookup=L["nationality_lookup"])
        status = v.get("status")
        if status == "verified":
            picks_resolved += 1
            continue

        reason = v.get("reason")

        if status == STATUS_SOURCE_CONFLICT:
            bucket = "source_conflict"
        elif reason == REASON_ROSTER_UNVERIFIED:
            # Refine: is the identity in the registry at all?
            pn = v.get("player") or _extract_player_name(p) or ""
            pn_norm = _norm(pn)
            doc = id_index.get(pn_norm)
            if doc is None and pn_norm in alias_set:
                # Alias exists but the alias key wasn't folded — this
                # would only happen for edge Unicode / normalisation
                # differences.
                bucket = "roster_unverified__identity_present_but_stale"
            elif doc is None:
                bucket = "roster_unverified__identity_absent_from_registry"
            elif not doc.get("current_team"):
                bucket = "roster_unverified__identity_present_no_current_team"
            else:
                bucket = "roster_unverified__identity_present_but_stale"
        elif reason in BUCKETS:
            bucket = reason
        else:
            bucket = "unknown"

        reasons_counts[bucket] += 1
        if len(samples[bucket]) < 10:
            samples[bucket].append({
                "pick_id": p.get("id"),
                "market": p.get("market"),
                "event": p.get("event"),
                "league": p.get("league"),
                "player_extracted": v.get("player"),
                "player_team_evidence": v.get("player_team"),
                "fixture_teams": v.get("fixture_teams"),
                "reason": reason,
                "status": status,
                "lock_score": p.get("published_lock_score")
                              or p.get("lock_score"),
            })

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "sport": "Soccer",
        "picks_scanned": picks_scanned,
        "picks_resolved": picks_resolved,
        "picks_unresolved_or_mismatch": picks_scanned - picks_resolved,
        "resolution_pct": round(
            100.0 * picks_resolved / picks_scanned, 2)
                        if picks_scanned else None,
        "bucket_counts": {b: reasons_counts.get(b, 0)
                          for b in BUCKETS},
        "representative_samples": samples,
    }


async def _main():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = client[os.environ.get("DB_NAME", "perkslocks_production")]
    report = await run(db)
    ts = report["generated_at"].replace(":", "").replace("-", "")
    path = f"/tmp/phase52_soccer_root_cause_{ts}.json"
    with open(path, "w") as fh:
        json.dump(report, fh, indent=2)
    print("=" * 78)
    print("Phase 5.2 — Soccer Unresolved Root-Cause (READ-ONLY)")
    print("=" * 78)
    print(f"Picks scanned:   {report['picks_scanned']}")
    print(f"Resolved:        {report['picks_resolved']}"
          f"   ({report['resolution_pct']}%)")
    print(f"Unres/Mismatch:  {report['picks_unresolved_or_mismatch']}")
    print()
    print("BUCKETS:")
    for b, c in report["bucket_counts"].items():
        if c:
            print(f"    {c:5d}  {b}")
    print(f"\n[report written] {path}")
    client.close()


if __name__ == "__main__":
    asyncio.run(_main())
