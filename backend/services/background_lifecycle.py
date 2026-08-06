"""background_lifecycle — Phase 2δ centralized background-task lifecycle.

Owns the registry of long-running background tasks so:
  • startup can register them in one place,
  • shutdown can cancel them gracefully within a deadline,
  • the operational dashboard can observe their state, and
  • expired JobCoordinator leases are recovered on boot before
    any snapshot loop is armed.

Usage
─────
    lifecycle = BackgroundLifecycle(db)
    await lifecycle.on_startup()      # recovers stale leases
    lifecycle.register(name, task)    # any asyncio.Task
    ...
    await lifecycle.on_shutdown()     # graceful cancel with timeout
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("lockscore.background_lifecycle")

DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 15.0


class BackgroundLifecycle:
    def __init__(self, db) -> None:
        self.db = db
        self._tasks: dict[str, asyncio.Task] = {}
        self._registered_at: dict[str, datetime] = {}
        self._shutdown_started = False

    # ─────────────────────────────────────────────────────────
    # Startup
    # ─────────────────────────────────────────────────────────
    async def on_startup(self) -> dict:
        """Called after the FastAPI app boots and BEFORE background
        tasks are armed.  Recovers stale JobCoordinator leases and
        expired ProviderBudget reservations left over from an unclean
        shutdown of the prior instance."""
        result = {
            "started_at": datetime.now(timezone.utc).isoformat(),
            "recovered_leases": 0,
            "recovered_reservations": 0,
        }
        try:
            from services.job_coordinator import JobCoordinator
            coord = JobCoordinator(self.db)
            await coord.ensure_indices()
            n = await coord.recover_expired_leases()
            result["recovered_leases"] = int(n)
            if n:
                logger.info(
                    "BackgroundLifecycle: recovered %d expired leases on startup",
                    n,
                )
        except Exception as e:  # pragma: no cover
            logger.warning("startup lease recovery err: %s", e)
        try:
            from services.provider_budget import ProviderBudget
            budget = ProviderBudget(self.db)
            await budget.ensure_indices()
            n = await budget.sweep_expired_reservations()
            result["recovered_reservations"] = int(n)
            if n:
                logger.info(
                    "BackgroundLifecycle: swept %d expired reservations on startup",
                    n,
                )
        except Exception as e:  # pragma: no cover
            logger.warning("startup reservation sweep err: %s", e)
        return result

    # ─────────────────────────────────────────────────────────
    # Task registry
    # ─────────────────────────────────────────────────────────
    def register(self, name: str, task: asyncio.Task) -> None:
        """Track a long-running background task by name."""
        if name in self._tasks:
            existing = self._tasks[name]
            if not existing.done():
                logger.warning(
                    "BackgroundLifecycle: '%s' already registered and "
                    "not done; overwriting", name,
                )
        self._tasks[name] = task
        self._registered_at[name] = datetime.now(timezone.utc)

    def status(self) -> dict:
        return {
            "shutdown_started": self._shutdown_started,
            "tasks": [
                {
                    "name":         name,
                    "done":         t.done(),
                    "cancelled":    t.cancelled(),
                    "registered_at": self._registered_at.get(name,
                                                              datetime.now(timezone.utc)
                                                              ).isoformat(),
                    "exception":    (
                        str(t.exception())[:200]
                        if t.done() and not t.cancelled()
                        and t.exception() is not None else None
                    ),
                }
                for name, t in self._tasks.items()
            ],
        }

    # ─────────────────────────────────────────────────────────
    # Shutdown
    # ─────────────────────────────────────────────────────────
    async def on_shutdown(self, *,
                          timeout: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
                          ) -> dict:
        """Gracefully cancel every registered task, waiting up to
        ``timeout`` seconds for them to finish.  Any task still
        running after the deadline is force-cancelled.

        Also proactively releases any leases still owned by this
        process so the next-boot recovery has less to clean up.
        """
        self._shutdown_started = True
        started = datetime.now(timezone.utc)
        cancelled = 0
        timed_out = 0
        errored = 0
        for name, t in list(self._tasks.items()):
            if t.done():
                continue
            t.cancel()
            cancelled += 1
        # Give tasks a chance to unwind.
        if self._tasks:
            done, pending = await asyncio.wait(
                list(self._tasks.values()), timeout=timeout,
                return_when=asyncio.ALL_COMPLETED,
            )
            timed_out = len(pending)
            for t in done:
                if t.cancelled():
                    continue
                if t.exception() is not None:
                    errored += 1
        # Best-effort: release any lease this process still owns.
        try:
            from services.job_coordinator import (
                JobCoordinator, COLLECTION, _instance_id, STATUS_RUNNING,
            )
            now = datetime.now(timezone.utc)
            our_id = _instance_id()
            res = await self.db[COLLECTION].update_many(
                {"status": STATUS_RUNNING, "owner_instance": our_id},
                {"$set": {"status": "expired", "lease_until": now,
                           "updated_at": now}},
            )
            released_leases = int(res.modified_count)
        except Exception:  # pragma: no cover
            released_leases = 0
        summary = {
            "started_at":       started.isoformat(),
            "finished_at":      datetime.now(timezone.utc).isoformat(),
            "cancelled":        cancelled,
            "timed_out":        timed_out,
            "errored":          errored,
            "released_leases":  released_leases,
        }
        logger.info("BackgroundLifecycle.on_shutdown: %s", summary)
        return summary


__all__ = [
    "BackgroundLifecycle", "DEFAULT_SHUTDOWN_TIMEOUT_SECONDS",
]
