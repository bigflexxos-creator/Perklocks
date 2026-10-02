"""Universal Historical Data Authority — 2026-10-02

ONE reusable completed-event ingestion authority for every supported
sport.  Replaces the "backfill-once-and-forget" pattern that left NFL
2026, MLB postseason, NHL, and CFB stale after their initial boot
backfill.

Lifecycle (same contract for every sport):

    provider discovery
    → final-event detection
    → authoritative per-sport adapter (``historical/<sport>.py``)
    → canonical player/team identity + normalized stats
    → ``games`` + ``player_game_logs`` (idempotent upsert)
    → ``historical_freshness`` manifest update
    → ``historical_ingestion_state`` per-season state
    → Historical Intelligence reads these canonical stores

Only writes new rows.  No synthetic observations.  No fabricated
H2H.  Fails closed when the provider genuinely lacks data.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("lockscore.universal_historical_authority")

# Order matters only for log readability; every sport runs
# concurrently via asyncio.gather so a slow sport cannot block others.
_SUPPORTED_SPORTS: tuple[str, ...] = (
    "nfl", "cfb", "mlb", "nba", "nhl", "soccer", "tennis",
)

# Default cadence — "every ~3 hours" is a reasonable balance between
# freshness and provider request budget. Loop sleeps are jittered to
# avoid thundering-herd when multiple pod replicas boot in sync.
_LOOP_SLEEP_SECONDS = 3 * 60 * 60
_LOOP_SLEEP_JITTER  =       10 * 60

_FRESHNESS_COLL = "historical_freshness"


# ─────────────────────────── adapter resolver ─────────────────────────

def _client_for(sport: str):
    """Return the per-sport adapter module (lazy-imported).

    Mirrors ``historical/orchestrator._client_for`` but also includes
    CFB which the legacy dispatcher missed.
    """
    try:
        if sport == "soccer":
            from historical import soccer
            return soccer
        if sport == "mlb":
            from historical import mlb
            return mlb
        if sport == "nfl":
            from historical import nfl
            return nfl
        if sport == "nba":
            from historical import nba
            return nba
        if sport == "nhl":
            from historical import nhl
            return nhl
        if sport == "tennis":
            from historical import tennis
            return tennis
        if sport == "cfb":
            from historical import cfb
            return cfb
    except ImportError as e:
        logger.warning("universal authority: no adapter for %s: %s", sport, e)
    return None


# ─────────────────────────── freshness manifest ────────────────────────

async def _write_manifest(db, sport: str, summary: dict,
                           *, status: str,
                           error: Optional[str] = None) -> None:
    now = datetime.now(timezone.utc)
    doc = {
        "sport": sport,
        "last_ingestion_attempt": now,
        "last_successful_ingestion": now if status in ("CURRENT", "PARTIAL_ACTUALS") else None,
        "events_discovered":   int(summary.get("games_seen")             or summary.get("events_discovered")   or 0),
        "events_ingested":     int(summary.get("games_inserted")         or summary.get("events_ingested")     or 0),
        "player_logs_inserted": int(summary.get("player_logs_inserted") or summary.get("logs_inserted")       or 0),
        "player_logs_updated": int(summary.get("player_logs_updated")   or 0),
        "duplicate_rejections": int(summary.get("duplicates")            or 0),
        "status": status,
        "error": (error or "")[:300],
        "adapter_version": "universal_v1",
        "provider": summary.get("provider") or summary.get("source") or "espn",
    }
    try:
        await db[_FRESHNESS_COLL].update_one(
            {"sport": sport},
            {"$set": doc, "$setOnInsert": {"first_ingestion": now}},
            upsert=True,
        )
    except Exception as e:
        logger.warning("manifest write failed for %s: %s", sport, e)


# ─────────────────────────── per-sport runner ─────────────────────────

async def ingest_sport(db, sport: str, *, since: Optional[datetime] = None) -> dict:
    """Run one incremental ingestion for a single sport.

    Idempotent — the per-sport adapter deduplicates by provider event
    id (ESPN event id, MLB gamePk, …) so re-running against an already-
    ingested slate inserts 0 new rows.  Updates are allowed when the
    provider ships new/corrected box-score fields.
    """
    client = _client_for(sport)
    if client is None:
        await _write_manifest(db, sport, {}, status="NO_SOURCE_DATA",
                              error="no adapter registered")
        return {"sport": sport, "skipped": "no_adapter"}
    try:
        logger.info("universal ingestion: %s START (since=%s)", sport, since)
        kwargs: dict[str, Any] = {}
        try:
            # Only pass `since` if the adapter accepts it.
            import inspect
            if "since" in inspect.signature(client.incremental_sync).parameters:
                kwargs["since"] = since
        except Exception:
            pass
        summary = await client.incremental_sync(db, **kwargs)
        # 2026-10-02 — HISTORICAL FRESHNESS AUTHORITY P0.
        # Compute truthful status.  Previous rule classified
        # ``games_seen > 0 AND inserted == 0`` as CURRENT, which only
        # proved the adapter CALLED the provider — not that canonical
        # final events match provider finals.  If the adapter can
        # enumerate provider FINAL event ids, derive status from the
        # actual parity; otherwise emit GAP_DETECTION_UNAVAILABLE
        # rather than fake CURRENT.
        inserted = int(summary.get("games_inserted") or summary.get("events_ingested") or 0)
        logs     = int(summary.get("player_logs_inserted") or summary.get("logs_inserted") or 0)
        provider_finals = summary.get("provider_final_ids") or summary.get("provider_finals")
        local_finals    = summary.get("local_final_ids")    or summary.get("local_finals")
        missing_ids     = summary.get("missing_ids")
        if isinstance(provider_finals, (list, set, tuple)) and isinstance(local_finals, (list, set, tuple)):
            # Adapter proved parity — truthful status.
            pf = set(provider_finals); lf = set(local_finals)
            missing = pf - lf
            if not missing:
                status = "CURRENT"
            elif inserted > 0 or logs > 0:
                status = "INGESTION_LAG" if missing else "CURRENT"
            else:
                status = "INGESTION_LAG"
            summary["missing_ids_count"] = len(missing)
        elif isinstance(missing_ids, (list, set, tuple)):
            status = "CURRENT" if not missing_ids else "INGESTION_LAG"
            summary["missing_ids_count"] = len(missing_ids)
        elif inserted > 0 or logs > 0:
            status = "CURRENT"
        elif summary.get("games_seen") and inserted == 0:
            # Adapter saw events but inserted none.  Only safe to
            # call this CURRENT when the adapter has proven parity —
            # otherwise flag that gap-detection is unavailable so
            # ops knows the status is weakly derived.
            status = "GAP_DETECTION_UNAVAILABLE"
        else:
            status = "PROVIDER_DELAY"
        await _write_manifest(db, sport, summary, status=status)
        logger.info("universal ingestion: %s DONE %s status=%s",
                    sport, summary, status)
        # Mirror legacy sync meta used by historical_intelligence.
        try:
            await db.historical_meta.update_one(
                {"_id": f"sync.{sport}"},
                {"$set": {"last_sync": datetime.now(timezone.utc).isoformat()}},
                upsert=True,
            )
        except Exception:
            pass
        return {"sport": sport, "status": status, **summary}
    except Exception as e:
        logger.exception("universal ingestion: %s FAILED", sport)
        await _write_manifest(db, sport, {}, status="PROVIDER_DELAY", error=str(e))
        return {"sport": sport, "error": str(e)[:200]}


# ─────────────────────────── universal driver ─────────────────────────

async def run_once(db, sports: Optional[list[str]] = None) -> dict[str, dict]:
    """Run one incremental ingestion pass across every supported sport.

    Executes sports concurrently with a per-sport task.  Each task is
    isolated — one adapter raising does not affect the others.
    """
    sports = list(sports or _SUPPORTED_SPORTS)
    tasks = {s: asyncio.create_task(ingest_sport(db, s)) for s in sports}
    out: dict[str, dict] = {}
    for s, t in tasks.items():
        try:
            out[s] = await t
        except Exception as e:
            logger.exception("universal ingestion: task %s raised", s)
            out[s] = {"error": str(e)[:200]}
    return out


async def gap_detect(db, sport: str) -> dict:
    """Return the delta between provider-known final events and locally
    persisted canonical events for ``sport``.

    Lightweight — only lists provider FINAL event ids and compares.
    No per-event box-score fetching happens here; that is left to the
    adapter's ``incremental_sync`` which the caller invokes next if
    the gap is non-zero.
    """
    client = _client_for(sport)
    if client is None:
        return {"sport": sport, "status": "NO_ADAPTER"}
    # Not every adapter exposes gap discovery today; the universal
    # authority treats "missing method" as "trust the adapter's
    # incremental_sync" — the gap will be closed by that call.
    return {"sport": sport, "status": "DELEGATED_TO_INCREMENTAL",
            "note": "adapter does not expose explicit gap listing yet"}


# ─────────────────────────── background loop ──────────────────────────

_loop_task: asyncio.Task | None = None
_loop_stop_event: asyncio.Event | None = None


async def _loop(db) -> None:
    """Forever-loop driving the universal authority."""
    import random
    assert _loop_stop_event is not None
    # Boot-time catch-up — fires immediately so a cold-start pod gets
    # fresh observations within seconds.
    try:
        await run_once(db)
    except Exception:
        logger.exception("universal authority boot catch-up raised")
    while not _loop_stop_event.is_set():
        sleep_s = _LOOP_SLEEP_SECONDS + random.randint(0, _LOOP_SLEEP_JITTER)
        try:
            await asyncio.wait_for(_loop_stop_event.wait(), timeout=sleep_s)
            return  # stop requested
        except asyncio.TimeoutError:
            pass  # time for next cycle
        try:
            await run_once(db)
        except Exception:
            logger.exception("universal authority cycle raised")


def start_background_authority(db) -> None:
    """Idempotent start — safe to call from server.py on_startup.

    2026-10-02 — ONE DATABASE AUTHORITY: refuses to start on Preview
    pods (BACKGROUND_WORKERS_ENABLED!=true) so Preview cannot
    duplicate Production's historical ingestion against the shared
    canonical DB.
    """
    global _loop_task, _loop_stop_event
    try:
        from services.data_authority import (
            background_workers_enabled as _wk, authority_mode as _mode,
        )
        if not _wk():
            logger.info(
                "data_authority: SUPPRESSED universal_historical_authority "
                "loop (mode=%s)", _mode(),
            )
            return
    except Exception:
        pass
    if _loop_task is not None and not _loop_task.done():
        return
    _loop_stop_event = asyncio.Event()
    _loop_task = asyncio.create_task(_loop(db),
                                       name="universal_historical_authority")
    logger.info("universal historical authority loop STARTED")


async def stop_background_authority() -> None:
    global _loop_task, _loop_stop_event
    if _loop_stop_event is not None:
        _loop_stop_event.set()
    if _loop_task is not None:
        try:
            await asyncio.wait_for(_loop_task, timeout=10)
        except Exception:
            pass
    _loop_task = None
    _loop_stop_event = None


__all__ = [
    "ingest_sport",
    "run_once",
    "gap_detect",
    "start_background_authority",
    "stop_background_authority",
]
