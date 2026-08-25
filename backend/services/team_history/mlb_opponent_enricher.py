"""MLB canonical-opponent enrichment (Session E, 2026-08-25).

Deterministic authoritative join:
    player_game_actuals.event_id → team_game_actuals.event_id
    → find both team rows for the event (home + away)
    → identify player's team via team_game_actuals.canonical_team_id ==
      player_game_actuals.team (case-insensitive)
    → the OTHER team row supplies canonical_opponent_id + home_away

NEVER writes actual stat values.  Only fills the missing identity
fields:
    canonical_team_id, canonical_opponent_id, canonical_event_id,
    home_away

Idempotent: safe to re-run.  Rows already stamped are skipped.
Bounded: caller controls `batch_size` and `limit`.
Conflict-safe: if an existing canonical field disagrees with the
newly resolved value, records an IDENTITY_CONFLICT and does not
overwrite.

Reusable as both:
  • one-off backfill (via ops route)
  • permanent completion hook (called by the future MLB ingest
    service — currently no active writer exists to hook, so the
    backfill runs on-demand until an active writer is built)
"""
from __future__ import annotations
from typing import Any
from datetime import datetime, timezone


async def enrich_mlb_opponent_batch(db, *, batch_size: int = 500,
                                     limit: int = 0,
                                     dry_run: bool = False) -> dict:
    """Enrich MLB player_game_actuals rows with canonical opponent
    identity by joining against team_game_actuals.

    Returns detailed counters.
    """
    stats = {"scanned": 0, "would_update": 0, "updated": 0,
             "already_stamped": 0, "unresolved": 0,
             "identity_conflict": 0, "batches": 0}
    query = {"sport": "mlb",
             "$or": [
                 {"canonical_opponent_id": {"$exists": False}},
                 {"canonical_opponent_id": None},
             ]}
    cursor = db.player_game_actuals.find(query, {
        "_id": 1, "event_id": 1, "team": 1,
        "canonical_team_id": 1, "canonical_opponent_id": 1,
        "canonical_event_id": 1, "home_away": 1,
    })
    if limit:
        cursor = cursor.limit(limit)

    # Team-row cache — per-event fetch once
    event_cache: dict[str, list] = {}

    batch_ops = []
    async for p in cursor:
        stats["scanned"] += 1
        eid = str(p.get("event_id") or "")
        pteam = (p.get("team") or "").strip()
        if not eid or not pteam:
            stats["unresolved"] += 1
            continue
        if eid not in event_cache:
            # BOTH stores use STRING event_id ("661117"). Never
            # convert to int — that breaks the match on shared events.
            rows = await db.team_game_actuals.find(
                {"sport": "mlb", "event_id": eid},
                {"canonical_team_id": 1, "home_away": 1,
                 "_id": 0}
            ).to_list(length=4)
            event_cache[eid] = rows
        rows = event_cache[eid]
        if len(rows) != 2:
            stats["unresolved"] += 1
            continue
        # Match player_team to a team row (case-insensitive)
        player_row = next((r for r in rows
                            if (r.get("canonical_team_id") or "").lower()
                               == pteam.lower()), None)
        opp_row = next((r for r in rows
                         if (r.get("canonical_team_id") or "").lower()
                            != pteam.lower()), None)
        if not player_row or not opp_row:
            stats["unresolved"] += 1
            continue
        new_ctid = player_row.get("canonical_team_id")
        new_cop  = opp_row.get("canonical_team_id")
        new_ha   = player_row.get("home_away")
        # Idempotency + conflict guard
        ex_ctid = p.get("canonical_team_id")
        ex_cop  = p.get("canonical_opponent_id")
        if ex_ctid and ex_ctid != new_ctid:
            stats["identity_conflict"] += 1
            continue
        if ex_cop and ex_cop != new_cop:
            stats["identity_conflict"] += 1
            continue
        # Build $set — only missing fields
        set_doc: dict[str, Any] = {}
        if not ex_ctid: set_doc["canonical_team_id"] = new_ctid
        if not ex_cop:  set_doc["canonical_opponent_id"] = new_cop
        if not p.get("canonical_event_id"): set_doc["canonical_event_id"] = eid
        if not p.get("home_away") and new_ha: set_doc["home_away"] = new_ha
        if not set_doc:
            stats["already_stamped"] += 1
            continue
        set_doc["opponent_enriched_at"] = datetime.now(timezone.utc) \
            .isoformat().replace("+00:00", "Z")
        set_doc["opponent_enrichment_source"] = "team_game_actuals_join_v1"
        stats["would_update"] += 1
        if not dry_run:
            from pymongo import UpdateOne
            batch_ops.append(UpdateOne({"_id": p["_id"]}, {"$set": set_doc}))
            if len(batch_ops) >= batch_size:
                r = await db.player_game_actuals.bulk_write(batch_ops,
                                                             ordered=False)
                stats["updated"] += r.modified_count
                stats["batches"] += 1
                batch_ops = []
    if batch_ops:
        r = await db.player_game_actuals.bulk_write(batch_ops,
                                                     ordered=False)
        stats["updated"] += r.modified_count
        stats["batches"] += 1
    return stats


__all__ = ["enrich_mlb_opponent_batch"]
