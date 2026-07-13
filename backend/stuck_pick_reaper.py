"""Stuck-Pick Reaper — permanent guardrail against picks getting stuck.

User mandate (2026-07-13):
  "history not grading and grading picks that's not on board is it a
  permanent fix that's give history a smooth flow and stop picks from
  being stuck"

Design:
  • Every 30 min, scan `picks` for documents whose event_time is in the
    PAST BY 48h+ but still have status ∈ {None, "pending"}.
  • These picks have missed every settler cycle. To keep the History
    tab clean and honest we tag them:
        status:          "void"
        settled_at:      now-ish
        void_reason:     "auto_void_stuck_pick_reaper"
        settle_source:   "stuck_pick_reaper"
    Voided picks are hidden from History by the `/history` endpoint's
    existing `status: {$nin: ["void"]}` clause.

  • We keep `learning_excluded: true` too so the learning engine
    doesn't record these as user-facing wins/losses (they didn't
    grade — we don't know the outcome).

  • Cheap: single indexed query + bulk update. Idempotent — running it
    twice is safe; the query filter excludes already-settled picks.

  • Metrics logged every run so we can spot regressions (e.g. if the
    Nordic name-matcher breaks again and 100 picks pile up in a day).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("lockscore.stuck_pick_reaper")

# Void picks whose event_time is >= 72h in the past. Chosen so:
#   • Normal settlement cycles have plenty of time to converge,
#   • FotMob primary + ESPN fallback grading has time to complete,
#   • Overnight backlog after a service outage doesn't get nuked
#     prematurely (we typically catch back up within a few hours),
#   • 3-day window still keeps the History tab clean.
_STUCK_HOURS = 72

_REAP_INTERVAL_SECS = 30 * 60      # 30 min cadence


async def reap_stuck_picks(db, *, hours: int = _STUCK_HOURS) -> dict:
    """One-shot reap. Returns a summary dict. Safe to call directly
    (bootstrap / admin CLI) or from a background loop."""
    now = datetime.now(timezone.utc)
    cutoff_iso = (now - timedelta(hours=hours)).isoformat()

    q = {
        # Missing OR still-pending status. Handles both:
        #  • picks created without a status field (legacy hot-scorer bug),
        #  • picks that were tagged "pending" but never settled.
        "$or": [
            {"status": {"$exists": False}},
            {"status": None},
            {"status": "pending"},
        ],
        # event_time is stored as ISO string with a mix of `Z` and
        # `+00:00` suffixes across sources. Lexicographic comparison
        # works for both because they share the same date/time prefix.
        "event_time": {"$lt": cutoff_iso},
    }
    # Sample a handful for the diagnostic log so we can see WHAT is
    # getting reaped without dumping the full working set.
    sample = await db.picks.find(
        q,
        {"id": 1, "sport": 1, "league": 1, "market": 1, "source": 1,
         "event_time": 1, "status": 1},
    ).limit(5).to_list(length=5)

    now_iso = now.isoformat()
    res = await db.picks.update_many(
        q,
        {"$set": {
            "status":            "void",
            "settled_at":        now_iso,
            "void_reason":       "auto_void_stuck_pick_reaper",
            "settle_source":     "stuck_pick_reaper",
            "learning_excluded": True,
        }},
    )

    summary = {
        "reaped":       res.modified_count,
        "cutoff_hours": hours,
        "cutoff_iso":   cutoff_iso,
        "sample":       [
            f"{s.get('sport')}/{s.get('league')}/{s.get('market','?')[:40]} "
            f"(src={s.get('source')}, evt={s.get('event_time','?')[:19]})"
            for s in sample
        ],
    }
    if res.modified_count:
        logger.info("Stuck-pick reaper voided %d picks (>%dh past event_time). Sample: %s",
                    res.modified_count, hours, summary["sample"])
    return summary


async def stuck_pick_reaper_loop(db) -> None:
    """Long-running 30-min loop. Attach via _deferred_task in server.py."""
    # Small startup delay so the settlement engine's first tick fires
    # BEFORE the reaper — otherwise picks that would have settled might
    # get prematurely voided during a slow bootstrap.
    await asyncio.sleep(5 * 60)
    while True:
        try:
            await reap_stuck_picks(db)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("stuck_pick_reaper loop error: %s", e)
        await asyncio.sleep(_REAP_INTERVAL_SECS)
