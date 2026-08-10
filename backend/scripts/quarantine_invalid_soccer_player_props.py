"""Non-destructive quarantine of already-published invalid/unverified
OPEN/PENDING Soccer player-based picks.

Marks the offending picks `off_board=True` + `player_team_invalid=True`
so they cannot appear on `/picks/today`, WITHOUT deleting the picks
themselves or their `prediction_snapshots` audit records.  Settled
history is never touched.

Usage:
    # Dry run first — prints counts, writes nothing.
    python -m scripts.quarantine_invalid_soccer_player_props --dry-run

    # Apply — writes off_board flags.
    python -m scripts.quarantine_invalid_soccer_player_props --apply

Callable from tests via `run(db, apply=False)`.
"""
from __future__ import annotations

import asyncio
import argparse
import os
from typing import Any


async def run(db, *, apply: bool = False) -> dict[str, Any]:
    from services.player_team_fixture_validator import (
        validate_player_fixture_pick, _norm,
    )
    # Freshest roster snapshot from `player_identities` collection
    # (populated by mls_scorer_gate.apply_espn_snapshot).
    roster_lookup: dict[str, str] = {}
    fresh_names: set[str] = set()
    # P0-C — separate national-team lookup for international fixtures.
    national_team_lookup: dict[str, str] = {}
    fresh_nt_names: set[str] = set()
    # P0-E — weak citizenship signal for source-conflict detection.
    nationality_lookup: dict[str, str] = {}
    async for doc in db["player_identities"].find(
        {"sport": "Soccer"},
        {"_id": 0, "name_norm": 1, "current_team": 1, "observed_at": 1,
         "current_national_team": 1, "national_team_observed_at": 1,
         "nationality": 1},
    ):
        n = doc.get("name_norm")
        t = doc.get("current_team")
        if n and t:
            roster_lookup[n] = t
            fresh_names.add(n)
        nt = doc.get("current_national_team")
        if n and nt:
            national_team_lookup[n] = nt
            fresh_nt_names.add(n)
        nat = doc.get("nationality")
        if n and nat and n not in nationality_lookup:
            nationality_lookup[n] = nat

    # Also merge legacy in-memory ESPN snapshot if present.
    try:
        from services import mls_scorer_gate as _mls
        snap = getattr(_mls, "_espn_by_name", None) or {}
        for name, entry in snap.items():
            if not isinstance(entry, dict):
                continue
            t = entry.get("team")
            if t:
                k = _norm(name)
                roster_lookup[k] = t
                fresh_names.add(k)
    except Exception:
        pass

    stats = {
        "scanned": 0, "valid": 0,
        "team_mismatch": 0, "roster_unverified": 0,
        "roster_conflict": 0,
        "fixture_teams_unknown": 0, "player_name_missing": 0,
        "non_player_market": 0, "quarantined_writes": 0,
        "over_85_ineligible": 0,
        "roster_lookup_size": len(roster_lookup),
        "national_team_lookup_size": len(national_team_lookup),
        "nationality_lookup_size": len(nationality_lookup),
    }
    query = {
        "sport": "Soccer",
        "settled": {"$ne": True},          # NEVER touch settled history
        "no_bet": {"$ne": True},
    }
    async for p in db.picks.find(query,
                                  {"_id": 0, "id": 1, "market": 1,
                                   "event": 1, "player_name": 1,
                                   "player": 1, "player_current_team": 1,
                                   "off_board": 1, "lock_score": 1,
                                   "published_lock_score": 1,
                                   "publication_source": 1,
                                   "settled": 1, "league": 1,
                                   "competition": 1}):
        stats["scanned"] += 1
        local_roster = dict(roster_lookup)
        local_fresh = set(fresh_names)
        local_nt = dict(national_team_lookup)
        local_nt_fresh = set(fresh_nt_names)
        pn = p.get("player_name") or p.get("player")
        pct = p.get("player_current_team")
        if isinstance(pn, str) and isinstance(pct, str):
            k = _norm(pn)
            local_roster[k] = pct
            local_fresh.add(k)
        v = validate_player_fixture_pick(
            p, local_roster,
            fresh_roster_names=(local_fresh or None),
            national_team_lookup=local_nt,
            fresh_national_team_names=(local_nt_fresh or None),
            nationality_lookup=nationality_lookup,
        )
        reason = v.get("reason") or ""
        if v.get("verified"):
            if reason == "market_not_player_based":
                stats["non_player_market"] += 1
            else:
                stats["valid"] += 1
            continue
        key = reason if reason in {"roster_unverified",
                                    "roster_conflict",
                                    "fixture_teams_unknown",
                                    "player_name_missing"} \
              else "team_mismatch"
        stats[key] = stats.get(key, 0) + 1
        try:
            lock = float(p.get("published_lock_score")
                         or p.get("lock_score") or 0)
        except (TypeError, ValueError):
            lock = 0.0
        if lock > 85.0:
            stats["over_85_ineligible"] += 1
        if apply:
            # Non-destructive: set off_board + tag reason.  Snapshots
            # in `prediction_snapshots` are untouched for audit.
            await db.picks.update_one(
                {"id": p["id"]},
                {"$set": {
                    "off_board": True,
                    "player_team_invalid": True,
                    "player_team_invalid_reason": reason,
                    "quarantine_applied_at":
                        _now_iso(),
                    "off_board_reasons": ["player_team_invalid", reason],
                }}
            )
            stats["quarantined_writes"] += 1
    return stats


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


async def _cli() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true")
    p.add_argument("--dry-run", action="store_true", default=True)
    args = p.parse_args()
    apply = bool(args.apply)
    from dotenv import load_dotenv
    load_dotenv("/app/backend/.env")
    from motor.motor_asyncio import AsyncIOMotorClient
    db = AsyncIOMotorClient(os.environ["MONGO_URL"])[
        os.environ.get("DB_NAME", "lockscore_db")]
    stats = await run(db, apply=apply)
    print("── Quarantine invalid Soccer player-props ──")
    print(f"  apply mode: {apply}")
    for k, v in stats.items():
        print(f"    {k:<28} {v}")


if __name__ == "__main__":
    asyncio.run(_cli())
