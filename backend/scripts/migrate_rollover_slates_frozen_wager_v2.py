"""Migration — Rollover Slates Frozen Wager v2 (2026-06-21)
============================================================

Root Closure guardrail per user directive:

  For existing rollover_slates, only backfill/migrate historical frozen
  wager fields when the ORIGINAL value is provably recoverable from
  existing immutable/published snapshot data.

  DO NOT populate historical line/odds/WP/Lock/book from today's mutable
  db.picks and represent those values as the original frozen wager.

  If an original historical value cannot be proven:
    - preserve the existing known fields
    - mark the unavailable field as legacy/unavailable
    - preserve provenance
    - do not manufacture historical truth

This script iterates every ``rollover_slates`` document, looks for a
matching ``rollover_slate_events`` FROZEN event (which embedded the
original leg snapshot at freeze time), and — ONLY for fields present
in that immutable event — backfills the v1 leg with the v2 field
names.  Everything else stays as-is and gets ``frozen_wager_version=1``
with ``frozen_wager_provenance="legacy_v1_partial"``.

Run once from the backend container:
    python -m scripts.migrate_rollover_slates_frozen_wager_v2

Idempotent — a leg already marked ``frozen_wager_version >= 2`` is skipped.
"""
from __future__ import annotations

import asyncio
import os
import sys
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from motor.motor_asyncio import AsyncIOMotorClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("migrate_rollover_slates_v2")


# Fields we care about in v2 legs.  Backfill each ONLY when the FROZEN
# event's original leg dict carried it (proof of provenance).
_V2_KEYS_FROM_LEG = (
    "market", "selection", "line",
    "sportsbook", "odds",
    "win_probability", "lock_score", "grade",
    "publication_version", "board_version",
    "canonical_event_id", "event_time",
    "sport", "league", "event",
)


def _reconstruct_leg_v2(original_leg: dict, existing_leg: dict) -> dict:
    """Return a new leg dict with v2 fields provably recovered from the
    original FROZEN event.  Any field not present in the original event
    is left absent (never fabricated from today's mutable data).
    """
    out = dict(existing_leg)
    provenance = {}
    for k in _V2_KEYS_FROM_LEG:
        # Only backfill if we have an ORIGINAL value AND the current leg
        # is missing it.  Never overwrite an existing leg field with the
        # original event value (they should already agree — this is a
        # safe read-only recovery).
        orig_val = original_leg.get(k)
        if orig_val is None:
            provenance[k] = "unavailable"
            continue
        if out.get(k) is None:
            out[k] = orig_val
            provenance[k] = "recovered_from_frozen_event"
        else:
            provenance[k] = "already_present"
    # Version + provenance tags.
    out["frozen_wager_version"] = 2
    out["frozen_wager_provenance"] = "migrated_from_frozen_event"
    out["frozen_wager_field_provenance"] = provenance
    return out


async def migrate_slate(db, slate: dict) -> dict:
    """Migrate one slate.  Returns a summary dict."""
    slate_id = slate.get("slate_id")
    slate_date = slate.get("slate_date")
    scope = slate.get("scope")
    legs = list(slate.get("legs") or [])
    if not legs:
        return {"slate_id": slate_id, "skipped": "no_legs"}

    # Already at v2? — nothing to do.
    if all(int((L or {}).get("frozen_wager_version") or 1) >= 2 for L in legs):
        return {"slate_id": slate_id, "skipped": "already_v2"}

    # Pull the FROZEN event for this slate — it embeds the original legs.
    frozen_event = await db.rollover_slate_events.find_one(
        {"slate_id": slate_id, "event": "FROZEN"},
        {"_id": 0, "legs": 1, "at": 1},
    )
    original_legs = (frozen_event or {}).get("legs") or []
    original_by_rank = {int(L.get("rank") or 0): L for L in original_legs if isinstance(L, dict)}

    changed = 0
    unavailable_only = 0
    new_legs = []
    for leg in legs:
        fv = int(leg.get("frozen_wager_version") or 1)
        if fv >= 2:
            new_legs.append(leg)
            continue
        rank = int(leg.get("rank") or 0)
        original = original_by_rank.get(rank)
        if not original:
            # No provenance — preserve as-is, mark as legacy unavailable.
            marked = {
                **leg,
                "frozen_wager_version": 1,
                "frozen_wager_provenance": "legacy_v1_unrecoverable",
            }
            new_legs.append(marked)
            unavailable_only += 1
            continue
        new_legs.append(_reconstruct_leg_v2(original, leg))
        changed += 1

    if changed == 0 and unavailable_only == 0:
        return {"slate_id": slate_id, "skipped": "no_changes"}

    await db.rollover_slates.update_one(
        {"slate_id": slate_id},
        {"$set": {
            "legs": new_legs,
            # Slate-level version stays what it was — this is a schema
            # migration, not a wager change.  We only bump the frozen
            # wager version marker on the slate for reader-side dispatch.
            "frozen_wager_version": 2 if changed > 0 else 1,
            "migrated_to_frozen_wager_v2_at": _now_iso(),
        }},
    )
    return {"slate_id": slate_id, "slate_date": slate_date, "scope": scope,
            "recovered": changed, "unrecoverable": unavailable_only}


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


async def main() -> None:
    mongo_url = os.environ.get("MONGO_URL")
    if not mongo_url:
        log.error("MONGO_URL not set — cannot migrate.")
        sys.exit(1)
    # Use the same shared client init as the app so we get the right db.
    from services.database import initialize_database, get_database
    initialize_database()
    db = get_database()

    total = 0
    recovered = 0
    unavailable = 0
    skipped = 0
    async for slate in db.rollover_slates.find({}, {"_id": 0}):
        total += 1
        try:
            result = await migrate_slate(db, slate)
        except Exception as e:
            log.exception("Slate migration failed: %s", e)
            continue
        if "recovered" in result:
            recovered += int(result.get("recovered") or 0)
            unavailable += int(result.get("unrecoverable") or 0)
            log.info("Migrated %s: recovered=%d unrecoverable=%d",
                     result.get("slate_id"), result.get("recovered"),
                     result.get("unrecoverable"))
        else:
            skipped += 1

    log.info("DONE — total_slates=%d recovered_legs=%d unrecoverable_legs=%d skipped_slates=%d",
             total, recovered, unavailable, skipped)


if __name__ == "__main__":
    asyncio.run(main())
