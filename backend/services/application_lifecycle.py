"""application_lifecycle — Phase 3F-2 startup + shutdown owner.

Owns the SEQUENCING of application boot and teardown.  Delegates
task registration to ``services/runtime_task_registry``.  Delegates
declarative job metadata to ``services/job_registry``.

Contract
────────
* Startup order (invariant, verified by tests):
    1. Load typed settings (Phase 3A)
    2. Initialize shared Mongo lifecycle (Phase 3B)
    3. Ping Mongo (Phase 3B)
    4. Ensure & verify critical indexes (Phase 3C)
    5. Recover expired leases/reservations (Phase 2β)
    6. Register runtime background tasks
    7. Start required tasks first, then optional
    8. Mark readiness
* Shutdown order:
    1. Stop accepting new background work
    2. Signal registered tasks
    3. Await completion within timeout
    4. Force-cancel stragglers
    5. Release owned leases/reservations
    6. Close shared HTTP clients
    7. Close MongoDB once
    8. Mark lifecycle stopped
* Repeated shutdown is safe (idempotent close).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from services.runtime_task_registry import (
    RuntimeTaskRegistry, get_registry,
)

logger = logging.getLogger("lockscore.application_lifecycle")


@dataclass
class ApplicationStartupResult:
    success:                    bool           = False
    database_ready:             bool           = False
    indexes_ready:              bool           = False
    recovery_complete:          bool           = False
    required_tasks_registered:  int            = 0
    required_tasks_started:     int            = 0
    optional_tasks_started:     int            = 0
    required_failures:          list[str]      = field(default_factory=list)
    optional_failures:          list[str]      = field(default_factory=list)
    warnings:                   list[str]      = field(default_factory=list)
    duration_ms:                int            = 0
    started_at:                 Optional[str]  = None

    def as_dict(self) -> dict[str, Any]:
        d = {
            "success":                    self.success,
            "database_ready":             self.database_ready,
            "indexes_ready":              self.indexes_ready,
            "recovery_complete":          self.recovery_complete,
            "required_tasks_registered":  self.required_tasks_registered,
            "required_tasks_started":     self.required_tasks_started,
            "optional_tasks_started":     self.optional_tasks_started,
            "required_failures":          list(self.required_failures),
            "optional_failures":          list(self.optional_failures),
            "warnings":                   list(self.warnings),
            "duration_ms":                self.duration_ms,
            "started_at":                 self.started_at,
        }
        return d


@dataclass
class ApplicationShutdownResult:
    success:                bool           = False
    tasks_signaled:         int            = 0
    tasks_cancelled:        int            = 0
    tasks_completed:        int            = 0
    leases_released:        int            = 0
    reservations_released:  int            = 0
    http_clients_closed:    int            = 0
    database_closed:        bool           = False
    duration_ms:            int            = 0
    warnings:               list[str]      = field(default_factory=list)
    errors:                 list[str]      = field(default_factory=list)
    stopped_at:             Optional[str]  = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "success":               self.success,
            "tasks_signaled":        self.tasks_signaled,
            "tasks_cancelled":       self.tasks_cancelled,
            "tasks_completed":       self.tasks_completed,
            "leases_released":       self.leases_released,
            "reservations_released": self.reservations_released,
            "http_clients_closed":   self.http_clients_closed,
            "database_closed":       self.database_closed,
            "duration_ms":           self.duration_ms,
            "warnings":              list(self.warnings),
            "errors":                list(self.errors),
            "stopped_at":            self.stopped_at,
        }


class ApplicationLifecycle:
    """Sole owner of application startup / shutdown sequencing."""

    def __init__(self, registry: Optional[RuntimeTaskRegistry] = None):
        self.registry = registry or get_registry()
        self._state:  str = "uninitialised"   # → "starting" → "ready" → "stopping" → "stopped"
        self._last_startup: Optional[ApplicationStartupResult]  = None
        self._last_shutdown: Optional[ApplicationShutdownResult] = None
        self._started_at:  Optional[float]  = None

    # ── Introspection ───────────────────────────────────────────────
    @property
    def state(self) -> str:
        return self._state

    def readiness(self) -> dict[str, Any]:
        last = self._last_startup
        crit_ok = (last is not None and not last.required_failures)
        registered = len(self.registry)
        running    = self.registry.running_count()
        return {
            "startup_complete":         self._state in ("ready", "stopping", "stopped"),
            "database_ready":           bool(last and last.database_ready),
            "indexes_ready":            bool(last and last.indexes_ready),
            "recovery_complete":        bool(last and last.recovery_complete),
            "required_tasks_registered": last.required_tasks_registered if last else 0,
            "required_tasks_running":   self.registry.critical_all_running(),
            "registered_task_count":    registered,
            "running_task_count":       running,
            "optional_task_failures":   list(last.optional_failures) if last else [],
            "required_task_failures":   list(last.required_failures) if last else [],
            "lifecycle_state":          self._state,
            "started_at":               (last.started_at if last else None),
            "last_shutdown_result":     (self._last_shutdown.as_dict() if self._last_shutdown else None),
            "ok":                       crit_ok and self._state == "ready",
        }

    # ── Startup phases ──────────────────────────────────────────────
    async def preflight(self) -> ApplicationStartupResult:
        """Run the settings/DB/indexes/recovery preflight phases only.
        The task-registration phase is left to the caller (server.py's
        on_startup) so behaviour is preserved verbatim."""
        started = time.perf_counter()
        result = ApplicationStartupResult(
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        self._state = "starting"

        # Phase 3A — typed settings
        try:
            from services.settings import AppSettings
            AppSettings.load()
        except Exception as e:
            result.warnings.append(f"settings.load: {type(e).__name__}: {e}")

        # Phase 3B — shared Mongo lifecycle
        try:
            from services.database import (
                initialize_database, ping_database,
            )
            initialize_database()
            ok = await ping_database(timeout_ms=5000)
            result.database_ready = bool(ok)
            if not ok:
                result.required_failures.append("mongo_ping_failed")
        except Exception as e:
            result.required_failures.append(f"database_init: {type(e).__name__}: {e}")

        # Phase 3C — index registry
        try:
            from services.database import get_database
            from services.index_registry import ensure_all_indexes
            db = get_database()
            summary = await ensure_all_indexes(db)
            result.indexes_ready = bool(summary.get("critical_ok"))
            if not result.indexes_ready:
                result.required_failures.append("index_registry_critical_not_ok")
        except Exception as e:
            result.required_failures.append(f"index_registry: {type(e).__name__}: {e}")

        # Phase 2β — lease / reservation recovery (delegated to job coordinator)
        try:
            from services.database import get_database
            from services.job_coordinator import JobCoordinator
            db = get_database()
            jc = JobCoordinator(db)
            recovered = await jc.recover_expired_leases()
            result.recovery_complete = True
            if recovered:
                result.warnings.append(
                    f"lease_recovery: {recovered} expired lease(s) reclaimed"
                )
        except Exception as e:
            result.warnings.append(f"lease_recovery: {type(e).__name__}: {e}")
            # Recovery failure is a WARNING, not a required failure — the
            # scheduler will retry on the next tick.
            result.recovery_complete = False

        result.duration_ms = int((time.perf_counter() - started) * 1000)
        result.success = not result.required_failures
        self._last_startup = result
        self._started_at   = time.time()
        if result.success:
            self._state = "ready"
        else:
            self._state = "starting"  # remain not-ready
        return result

    def record_task_registration(
        self,
        *, required_registered: int, required_started: int,
        optional_started: int, required_failures: list[str] = None,
        optional_failures: list[str] = None,
    ) -> None:
        """Server.py calls this after registering + starting its tasks."""
        if self._last_startup is None:
            self._last_startup = ApplicationStartupResult(
                started_at=datetime.now(timezone.utc).isoformat(),
            )
        r = self._last_startup
        r.required_tasks_registered = required_registered
        r.required_tasks_started    = required_started
        r.optional_tasks_started    = optional_started
        r.required_failures        += list(required_failures or ())
        r.optional_failures        += list(optional_failures or ())
        if not r.required_failures and self._state != "ready":
            self._state = "ready"

    # ── Shutdown ────────────────────────────────────────────────────
    async def shutdown(self, timeout: float = 10.0) -> ApplicationShutdownResult:
        started = time.perf_counter()
        result = ApplicationShutdownResult(
            stopped_at=datetime.now(timezone.utc).isoformat(),
        )
        if self._state == "stopped":
            # Repeat-shutdown safety.
            result.success = True
            return result
        self._state = "stopping"

        # 1-4. Signal & await tasks; force-cancel via registry.stop_all
        try:
            summary = await self.registry.stop_all(timeout=timeout)
            result.tasks_signaled  = len(summary.get("tasks_signalled", []))
            statuses = summary.get("statuses", {})
            result.tasks_cancelled = sum(1 for s in statuses.values() if s == "cancelled")
            result.tasks_completed = sum(1 for s in statuses.values() if s == "completed")
            if summary.get("still_running"):
                result.warnings.append(
                    f"still_running_after_timeout: {summary['still_running']}"
                )
        except Exception as e:
            result.errors.append(f"tasks_stop_all: {type(e).__name__}: {e}")

        # 5. Release leases/reservations owned by this process
        try:
            from services.database import get_database
            from services.job_coordinator import JobCoordinator
            db = get_database()
            jc = JobCoordinator(db)
            released = await jc.release_all_local_leases()
            result.leases_released = int(released or 0)
        except Exception as e:
            result.warnings.append(f"lease_release: {type(e).__name__}: {e}")
        try:
            from services.provider_budget import ProviderBudget
            db = get_database()
            pb = ProviderBudget(db)
            n = await pb.release_all_intents_owned_by(source="local_shutdown")
            result.reservations_released = int(n or 0)
        except Exception as e:
            result.warnings.append(f"budget_release: {type(e).__name__}: {e}")

        # 6. Close shared HTTP clients (nothing owns a shared client yet)
        result.http_clients_closed = 0

        # 7. Close MongoDB once
        try:
            from services.database import close_database
            await close_database()
            result.database_closed = True
        except Exception as e:
            result.errors.append(f"database_close: {type(e).__name__}: {e}")

        # 8. Mark stopped
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        result.success = not result.errors
        self._state = "stopped"
        self._last_shutdown = result
        return result


# ── Process-scoped singleton ────────────────────────────────────────
_lifecycle: Optional[ApplicationLifecycle] = None


def get_lifecycle() -> ApplicationLifecycle:
    global _lifecycle
    if _lifecycle is None:
        _lifecycle = ApplicationLifecycle()
    return _lifecycle


def _reset_lifecycle_for_testing() -> None:  # pragma: no cover
    global _lifecycle
    _lifecycle = None


__all__ = [
    "ApplicationLifecycle",
    "ApplicationStartupResult",
    "ApplicationShutdownResult",
    "get_lifecycle",
]
