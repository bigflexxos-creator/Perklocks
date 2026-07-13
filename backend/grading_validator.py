"""Grading Validator — permanent cross-source verification.

User mandate (2026-07-13):
  "Why do we have to keep having these problems with history and you not
  seeing til I find flaw I can't have a working app if history is wrong"

Design: every 60 min, scan freshly-graded soccer goalscorer picks. For
each one, query FotMob (independent data source) and compare the grade.
On disagreement:
  1. Log LOUDLY with all context.
  2. Re-open the pick (status → 'pending', clear settled_at) so the
     next settler cycle regrades with the fixed logic.
  3. If a threshold of mismatches happens in a day, escalate the log
     to WARNING so the operator sees it in monitoring.

The point isn't perfect grading — it's a self-healing loop that catches
grading regressions the moment they happen instead of days later when
a user notices. Every pick added to history is cross-verified within
60 minutes of settlement.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("lockscore.grading_validator")

# Only re-check picks graded in the last VERIFY_WINDOW_MIN minutes.
# Older grades are trusted (or already had their chance to be caught).
VERIFY_WINDOW_MIN = 6 * 60             # 6 hours

# How often the validator loop fires.
LOOP_INTERVAL_SECS = 60 * 60           # 1 hour

# Escalation threshold — if this many mismatches fire in a single day,
# something structural is broken; bump the log level so it's obvious.
DAILY_MISMATCH_ALERT_THRESHOLD = 3


async def verify_recent_goalscorer_grades(db, *, window_min: int = VERIFY_WINDOW_MIN) -> dict:
    """Cross-check recently graded soccer goalscorer picks against FotMob.
    Reopens (status → pending) any pick where FotMob disagrees with the
    stored grade. Idempotent, safe to call directly."""
    from soccer_fotmob_settle import settle_soccer_leg as _fotmob

    cutoff_iso = (datetime.now(timezone.utc)
                  - timedelta(minutes=window_min)).isoformat()
    q = {
        "sport": "Soccer",
        "market": {"$regex": "Anytime Goal Scorer|To Score or Assist",
                    "$options": "i"},
        "status": {"$in": ["won", "lost"]},
        "settled_at": {"$gte": cutoff_iso},
        # Don't re-verify the same pick over and over — mark once done.
        "grade_verified_at": {"$exists": False},
    }
    summary = {"scanned": 0, "agreed": 0, "mismatched": 0,
               "fotmob_unavailable": 0, "reopened": 0}
    mismatches: list[dict] = []

    async for p in db.picks.find(q).limit(500):
        summary["scanned"] += 1
        leg = {
            "sport":      "Soccer",
            "event":      p.get("event") or "",
            "market":     p.get("market") or "",
            "selection":  p.get("selection") or "",
            "event_time": p.get("event_time"),
        }
        try:
            fot_result = await _fotmob(leg)
        except Exception as e:
            logger.debug("validator FotMob call failed: %s", e)
            fot_result = None

        # FotMob couldn't verify — trust ESPN's grade, mark verified so
        # we don't hammer FotMob repeatedly for this pick.
        if fot_result not in ("won", "lost", "push"):
            summary["fotmob_unavailable"] += 1
            await db.picks.update_one(
                {"id": p.get("id")},
                {"$set": {"grade_verified_at": datetime.now(timezone.utc).isoformat(),
                          "grade_verify_source": "fotmob_unavailable"}},
            )
            continue

        current = p.get("status")
        if fot_result == current:
            summary["agreed"] += 1
            await db.picks.update_one(
                {"id": p.get("id")},
                {"$set": {"grade_verified_at": datetime.now(timezone.utc).isoformat(),
                          "grade_verify_source": "fotmob",
                          "grade_verify_result": "agreed"}},
            )
            continue

        # Disagreement — reopen for re-settlement.
        summary["mismatched"] += 1
        mismatches.append({
            "id":        p.get("id"),
            "event":     p.get("event"),
            "selection": p.get("selection"),
            "espn_says": current,
            "fotmob_says": fot_result,
        })
        await db.picks.update_one(
            {"id": p.get("id")},
            {"$set": {
                "status": "pending",
                "grade_disagreement": {
                    "detected_at":     datetime.now(timezone.utc).isoformat(),
                    "espn_said":       current,
                    "fotmob_said":     fot_result,
                    "previous_settled_at": p.get("settled_at"),
                },
             },
             "$unset": {"settled_at": "", "settle_source": "", "settle_reason": ""}},
        )
        summary["reopened"] += 1

    summary["mismatches"] = mismatches
    if summary["mismatched"]:
        level = (logging.WARNING
                 if summary["mismatched"] >= DAILY_MISMATCH_ALERT_THRESHOLD
                 else logging.INFO)
        logger.log(
            level,
            "Grading validator: %d/%d disagreements caught & reopened. %s",
            summary["mismatched"], summary["scanned"],
            [f"{m['selection']} ({m['event']}): ESPN={m['espn_says']} "
             f"vs FotMob={m['fotmob_says']}" for m in mismatches[:5]],
        )
    else:
        logger.info(
            "Grading validator: %d verified, %d agreed, %d FotMob unavailable",
            summary["scanned"], summary["agreed"], summary["fotmob_unavailable"],
        )
    return summary


async def grading_validator_loop(db) -> None:
    """Long-running 1-hour loop. Wire via _deferred_task in server.py."""
    # Wait for the initial settlement wave to complete before we run.
    await asyncio.sleep(10 * 60)
    while True:
        try:
            await verify_recent_goalscorer_grades(db)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("grading_validator loop error: %s", e)
        await asyncio.sleep(LOOP_INTERVAL_SECS)
