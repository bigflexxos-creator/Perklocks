"""Dry-run scanner for OPEN/PENDING Soccer player props.

Runs the Layer-B integrity gate against every currently-open Soccer
player-based pick and reports counts by verdict category — WITHOUT
mutating the DB.  Settled history is untouched.

Usage:
    python -m scripts.dryrun_player_team_integrity

Also exposes an admin endpoint helper: `dryrun_scan(db) -> dict` for
programmatic use inside tests / admin routes.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any


async def dryrun_scan(db) -> dict[str, Any]:
    from services.player_team_fixture_validator import (
        validate_player_fixture_pick, _norm,
    )

    # Build the freshest roster snapshot.
    roster_lookup: dict[str, str] = {}
    fresh_names: set[str] = set()
    try:
        from services import mls_scorer_gate as _mls
        snap = getattr(_mls, "_espn_by_name", None) or {}
        for name, entry in snap.items():
            t = entry.get("team") if isinstance(entry, dict) else None
            if t:
                k = _norm(name)
                roster_lookup[k] = t
                fresh_names.add(k)
    except Exception:
        pass

    stats = {
        "scanned": 0,
        "valid": 0,
        "team_mismatch": 0,
        "roster_unverified": 0,
        "fixture_teams_unknown": 0,
        "player_name_missing": 0,
        "non_player_market": 0,
        "already_off_board": 0,
        "would_be_ineligible": 0,
        "over_85_ineligible": 0,
        "roster_lookup_size": len(roster_lookup),
    }
    query = {
        "sport": "Soccer",
        "settled": {"$ne": True},
        "no_bet": {"$ne": True},
    }
    async for p in db.picks.find(query,
                                  {"_id": 0, "id": 1, "market": 1,
                                   "event": 1, "player_name": 1,
                                   "player": 1, "player_current_team": 1,
                                   "off_board": 1, "lock_score": 1,
                                   "published_lock_score": 1,
                                   "publication_source": 1}):
        # Per-pick roster overlay (writer-supplied fresh team).
        local_roster = dict(roster_lookup)
        local_fresh = set(fresh_names)
        pn = p.get("player_name") or p.get("player")
        pct = p.get("player_current_team")
        if isinstance(pn, str) and isinstance(pct, str):
            k = _norm(pn)
            local_roster[k] = pct
            local_fresh.add(k)

        stats["scanned"] += 1
        v = validate_player_fixture_pick(
            p, local_roster,
            fresh_roster_names=(local_fresh or None),
        )
        reason = v.get("reason") or ""
        if v.get("verified"):
            if reason == "market_not_player_based":
                stats["non_player_market"] += 1
            else:
                stats["valid"] += 1
            continue
        stats["would_be_ineligible"] += 1
        if p.get("off_board") is True:
            stats["already_off_board"] += 1
        # Lock over 85 → this pick is currently on the board despite
        # being integrity-invalid.  That's what we must hide.
        lock = p.get("published_lock_score") or p.get("lock_score") or 0
        try:
            if float(lock) > 85.0:
                stats["over_85_ineligible"] += 1
        except (TypeError, ValueError):
            pass
        key = reason if reason in {
            "team_mismatch", "player_team_mismatch",
            "roster_unverified", "fixture_teams_unknown",
            "player_name_missing",
        } else "roster_unverified"
        # Normalize the enum name.
        if key == "player_team_mismatch":
            key = "team_mismatch"
        stats[key] = stats.get(key, 0) + 1
    return stats


async def _cli() -> None:
    from dotenv import load_dotenv
    load_dotenv("/app/backend/.env")
    from motor.motor_asyncio import AsyncIOMotorClient
    db = AsyncIOMotorClient(os.environ["MONGO_URL"])[
        os.environ.get("DB_NAME", "lockscore_db")]
    stats = await dryrun_scan(db)
    print("── Soccer player-prop integrity dry-run ──────────────────")
    for k, v in stats.items():
        print(f"  {k:<28} {v}")


if __name__ == "__main__":
    asyncio.run(_cli())
