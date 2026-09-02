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
        # DO NOT reap picks that the grading validator recently reopened
        # for cross-source re-settlement. They're pending on purpose and
        # will be regraded by the next settler cycle — voiding them here
        # loses the correct grade and silently drops user history.
        "grade_disagreement": {"$exists": False},
    }
    # Sample a handful for the diagnostic log so we can see WHAT is
    # getting reaped without dumping the full working set.
    sample = await db.picks.find(
        q,
        {"id": 1, "sport": 1, "league": 1, "market": 1, "source": 1,
         "event_time": 1, "status": 1},
    ).limit(5).to_list(length=5)

    # P0.2b — canonical routing via SettlementService.  The reaper
    # does NOT invent WON/LOST — it only VOIDS picks that have exceeded
    # the settlement window with no authoritative outcome available.
    # VOID skips the FINAL barrier inside SettlementService, but still
    # gets a settlement_events row + compat-mirror write.  Iterating
    # per-pick (instead of a bulk update_many) keeps the immutable
    # ledger honest — one row per void event.
    from services.settlement_service import SettlementService
    _svc = SettlementService(db)
    await _svc.ensure_indices()

    # ── Block 4E μ-closure — BOUNDED REAPER ──────────────────────
    # PRIOR DEFECT: ``to_list(length=None)`` loaded ALL matching
    # stuck picks into memory in one shot — a large backlog could
    # OOM or block for tens of seconds.  New: bounded per-run batch;
    # subsequent runs advance naturally because reaped rows exit
    # the ``status:"pending"`` filter above.
    _STUCK_REAPER_BATCH = 500
    to_reap = await db.picks.find(
        q,
        {"id": 1, "event": 1, "market": 1, "side": 1, "line": 1,
         "fanduel_event_id": 1, "event_id": 1, "sport": 1, "league": 1,
         "source": 1, "event_time": 1, "status": 1},
    ).limit(_STUCK_REAPER_BATCH).to_list(length=_STUCK_REAPER_BATCH)

    # ── Root Closure 2026-06 — NO FABRICATED VOIDS ─────────────
    # PRIOR DEFECT: The reaper routed stuck picks through
    # SettlementService with `result='void'` + `authoritative_event_final=
    # False`.  That inserted a canonical settlement_events row claiming
    # the wager was VOID — but VOID is a real book status (game
    # cancelled, market pulled, etc.).  For a pick whose event actually
    # happened but whose grading feed simply never caught up, VOID is a
    # fabricated outcome that contaminates hit-rate, ROI, and public
    # History with false negatives.
    #
    # CORRECT CONTRACT (Root Closure Q28 + Q-Reaper):
    #   * `status='unresolved'`
    #   * `settlement_status='UNRESOLVED'`
    #   * `unresolved_reason='stuck_past_settlement_window'`
    #   * NO settlement_events row inserted (the append-only ledger
    #     stays honest — it only records real settlements).
    # Downstream History surfaces render UNRESOLVED distinctly from VOID.
    unresolved_n = 0
    for _r in to_reap:
        _pid = _r.get("id")
        if not _pid:
            continue
        try:
            _upd = await db.picks.update_one(
                {"id": _pid, "status": {"$in": [None, "pending"]}},
                {"$set": {
                    "status":                 "unresolved",
                    "settlement_status":      "UNRESOLVED",
                    "unresolved_reason":      "stuck_past_settlement_window",
                    "unresolved_by":          "stuck_pick_reaper",
                    "unresolved_at":          now.isoformat(),
                    "learning_excluded":      True,
                }},
            )
            if _upd.modified_count:
                unresolved_n += 1
        except Exception as _e:
            logger.debug("reaper unresolved err for %s: %s", _pid, _e)
    voided_n = unresolved_n   # keep response shape stable for callers

    summary = {
        "reaped":       voided_n,
        "unresolved":   voided_n,
        "cutoff_hours": hours,
        "cutoff_iso":   cutoff_iso,
        "sample":       [
            f"{s.get('sport')}/{s.get('league')}/{(s.get('market') or '?')[:40]} "
            f"(src={s.get('source')}, evt={(s.get('event_time') or '?')[:19]})"
            for s in sample
        ],
    }
    if voided_n:
        logger.info("Stuck-pick reaper marked %d picks UNRESOLVED (>%dh past event_time, no authoritative actual). Sample: %s",
                    voided_n, hours, summary["sample"])
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
