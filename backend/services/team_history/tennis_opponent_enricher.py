"""Tennis canonical-opponent enrichment (Session F2, 2026-06-XX).

Deterministic authoritative join via shared match/event ID:
    player_game_actuals.event_id  is  the authoritative match ID.
    Every match yields exactly 2 rows — one per participant.

For each event_id with exactly 2 rows [A, B]:
    A.canonical_opponent_id = B.canonical_player_id
    B.canonical_opponent_id = A.canonical_player_id
    A.canonical_event_id = event_id
    B.canonical_event_id = event_id

We DO NOT populate canonical_team_id and DO NOT write team_game_actuals
for Tennis — opponent is the other player, not a team.

Idempotent: rows already stamped are skipped.
Bounded: caller controls `batch_size` and `limit` (limit caps EVENTS
processed, not rows).
Conflict-safe: if the OTHER row's canonical_player_id disagrees with
an existing canonical_opponent_id, we DO NOT overwrite — recorded as
identity_conflict.
Orphan-safe: events with != 2 rows are recorded as unresolved.

NEVER writes actual stat values.
"""
from __future__ import annotations
from collections import defaultdict
from typing import Any
from datetime import datetime, timezone


async def enrich_tennis_opponent_batch(db, *, batch_size: int = 1000,
                                       limit: int = 0,
                                       dry_run: bool = False) -> dict:
    """Enrich Tennis player_game_actuals with canonical opponent
    (= other participant's canonical_player_id) by grouping rows on
    event_id.

    Returns detailed counters.
    """
    stats = {"scanned_rows": 0, "events_scanned": 0, "would_update": 0,
             "updated": 0, "already_stamped": 0, "unresolved": 0,
             "identity_conflict": 0, "batches": 0, "orphan_events": 0}

    # Load minimal fields for ALL tennis rows that need enrichment.
    # (Tennis dataset is ~86k rows — comfortable to page through.)
    query = {"sport": "tennis",
             "$or": [
                 {"canonical_opponent_id": {"$exists": False}},
                 {"canonical_opponent_id": None},
             ]}
    grouped: dict[str, list] = defaultdict(list)
    async for r in db.player_game_actuals.find(query, {
            "_id": 1, "event_id": 1, "canonical_player_id": 1,
            "player_id": 1, "canonical_opponent_id": 1,
            "canonical_event_id": 1,
    }):
        stats["scanned_rows"] += 1
        eid = r.get("event_id")
        if eid:
            grouped[eid].append(r)

    batch_ops = []
    events = list(grouped.items())
    if limit:
        events = events[:limit]
    stats["events_scanned"] = len(events)

    for eid, rows in events:
        if len(rows) != 2:
            stats["orphan_events"] += 1
            stats["unresolved"] += len(rows)
            continue
        a, b = rows
        a_pid = str(a.get("canonical_player_id") or a.get("player_id") or "")
        b_pid = str(b.get("canonical_player_id") or b.get("player_id") or "")
        if not a_pid or not b_pid or a_pid == b_pid:
            stats["unresolved"] += 2
            continue

        for row, my_pid, opp_pid in ((a, a_pid, b_pid), (b, b_pid, a_pid)):
            ex_cop = row.get("canonical_opponent_id")
            if ex_cop and str(ex_cop) != opp_pid:
                stats["identity_conflict"] += 1
                continue
            set_doc: dict[str, Any] = {}
            if not ex_cop:
                set_doc["canonical_opponent_id"] = opp_pid
            if not row.get("canonical_event_id"):
                set_doc["canonical_event_id"] = eid
            if not set_doc:
                stats["already_stamped"] += 1
                continue
            set_doc["opponent_enriched_at"] = datetime.now(timezone.utc) \
                .isoformat().replace("+00:00", "Z")
            set_doc["opponent_enrichment_source"] = \
                "tennis_event_id_group_v1"
            stats["would_update"] += 1
            if not dry_run:
                from pymongo import UpdateOne
                batch_ops.append(UpdateOne({"_id": row["_id"]},
                                            {"$set": set_doc}))
                if len(batch_ops) >= batch_size:
                    r_ = await db.player_game_actuals.bulk_write(batch_ops,
                                                                  ordered=False)
                    stats["updated"] += r_.modified_count
                    stats["batches"] += 1
                    batch_ops = []
    if batch_ops:
        r_ = await db.player_game_actuals.bulk_write(batch_ops,
                                                     ordered=False)
        stats["updated"] += r_.modified_count
        stats["batches"] += 1
    return stats


__all__ = ["enrich_tennis_opponent_batch"]
