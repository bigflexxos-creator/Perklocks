"""NFL canonical-opponent enrichment (Session F1, 2026-06-XX).

Deterministic authoritative join via nflfastR game_id convention:
    player_game_actuals.event_id = "{season}_{week:02d}_{away}_{home}"
    Example: "2019_01_IND_LAC"  →  season=2019, week=1, away=IND, home=LAC

For each row:
    canonical_team_id     = player.team.upper()  (already 3-letter abbrev)
    canonical_opponent_id = derived from event_id parse (the OTHER team)
    canonical_event_id    = event_id (already the authoritative NFL game id)
    home_away             = "away" if team == parts[2] else "home"

Conflict guard: if player.opponent (already stored) disagrees with the
parse-derived opponent, we DO NOT overwrite — recorded as
identity_conflict.

Idempotent: rows already stamped are skipped.
Bounded: caller controls `batch_size` and `limit`.
NEVER writes actual stat values.
"""
from __future__ import annotations
from typing import Any
from datetime import datetime, timezone


def _parse_nfl_event(eid: str) -> tuple[str, str] | None:
    """Return (away_abbrev, home_abbrev) or None if bad format."""
    if not eid:
        return None
    parts = eid.split("_")
    if len(parts) != 4:
        return None
    _, _, away, home = parts
    if not away or not home or len(away) < 2 or len(home) < 2:
        return None
    return away.upper(), home.upper()


async def enrich_nfl_opponent_batch(db, *, batch_size: int = 1000,
                                    limit: int = 0,
                                    dry_run: bool = False) -> dict:
    """Enrich NFL player_game_actuals with canonical opponent identity
    derived deterministically from event_id parse.

    Returns detailed counters.
    """
    stats = {"scanned": 0, "would_update": 0, "updated": 0,
             "already_stamped": 0, "unresolved": 0,
             "identity_conflict": 0, "batches": 0, "bad_format": 0}

    query = {"sport": "nfl",
             "$or": [
                 {"canonical_opponent_id": {"$exists": False}},
                 {"canonical_opponent_id": None},
             ]}
    cursor = db.player_game_actuals.find(query, {
        "_id": 1, "event_id": 1, "team": 1, "opponent": 1,
        "canonical_team_id": 1, "canonical_opponent_id": 1,
        "canonical_event_id": 1, "home_away": 1,
    })
    if limit:
        cursor = cursor.limit(limit)

    batch_ops = []
    async for p in cursor:
        stats["scanned"] += 1
        eid = str(p.get("event_id") or "")
        pteam = (p.get("team") or "").strip().upper()
        popp  = (p.get("opponent") or "").strip().upper()
        if not eid or not pteam:
            stats["unresolved"] += 1
            continue
        parsed = _parse_nfl_event(eid)
        if not parsed:
            stats["bad_format"] += 1
            continue
        away, home = parsed
        if pteam == away:
            new_op, new_ha = home, "away"
        elif pteam == home:
            new_op, new_ha = away, "home"
        else:
            # player.team not in event_id → cannot resolve authoritatively
            stats["unresolved"] += 1
            continue
        # Consistency check with stored opponent (if any)
        if popp and popp != new_op:
            stats["identity_conflict"] += 1
            continue
        # Conflict guard on existing canonical fields
        ex_ctid = p.get("canonical_team_id")
        ex_cop  = p.get("canonical_opponent_id")
        if ex_ctid and ex_ctid != pteam:
            stats["identity_conflict"] += 1
            continue
        if ex_cop and ex_cop != new_op:
            stats["identity_conflict"] += 1
            continue

        set_doc: dict[str, Any] = {}
        if not ex_ctid: set_doc["canonical_team_id"] = pteam
        if not ex_cop:  set_doc["canonical_opponent_id"] = new_op
        if not p.get("canonical_event_id"): set_doc["canonical_event_id"] = eid
        if not p.get("home_away"): set_doc["home_away"] = new_ha
        if not set_doc:
            stats["already_stamped"] += 1
            continue
        set_doc["opponent_enriched_at"] = datetime.now(timezone.utc) \
            .isoformat().replace("+00:00", "Z")
        set_doc["opponent_enrichment_source"] = "nfl_event_id_parse_v1"
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


__all__ = ["enrich_nfl_opponent_batch"]
