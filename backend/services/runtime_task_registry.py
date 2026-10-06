"""runtime_task_registry — Phase 3F-2 registered-task lifecycle.

Owns every asyncio task created during application startup.  Retains
task handles so shutdown can signal them, await them, and force-
cancel stragglers.

This is a RUNTIME registry — the declarative operational job catalog
in services/job_registry.py describes cadence/costs/leases, while
THIS module owns the actual live task handles per process.

Design goals
────────────
* Duplicate task names fail registration (invariant).
* Repeated startup does not duplicate tasks.
* Registered tasks are visible to shutdown logic.
* No automatic restart unless the task's coroutine already implements
  its own reconnect/backoff loop.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("lockscore.runtime_task_registry")


TaskType   = str    # "startup_recovery" | "recurring_loop" | "one_shot" | "coordinator_job"
Cadence    = str    # human-readable e.g. "every 5 min", "hourly", "on startup only"
Restart    = str    # "none" | "on_failure" | "self_managed"


@dataclass
class _RegisteredTask:
    name:              str
    owner_service:     str
    task_type:         TaskType
    coroutine_factory: Callable[[], Awaitable[Any]]
    critical:          bool           = False
    paid_provider:     bool           = False
    coordinator_job:   Optional[str]  = None
    cadence:           Cadence        = ""
    startup_behavior:  str            = "eager"
    restart_policy:    Restart        = "self_managed"
    shutdown_timeout:  float          = 10.0
    handle:            Optional[asyncio.Task] = None
    started_at:        Optional[float] = None
    completed_at:      Optional[float] = None
    last_error:        Optional[str]   = None

    @property
    def status(self) -> str:
        if self.handle is None:
            return "registered"
        if self.handle.done():
            if self.handle.cancelled():
                return "cancelled"
            if self.handle.exception():
                return "failed"
            return "completed"
        return "running"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name":            self.name,
            "owner_service":   self.owner_service,
            "task_type":       self.task_type,
            "critical":        self.critical,
            "paid_provider":   self.paid_provider,
            "coordinator_job": self.coordinator_job,
            "cadence":         self.cadence,
            "restart_policy":  self.restart_policy,
            "shutdown_timeout": self.shutdown_timeout,
            "status":          self.status,
            "started_at":      self.started_at,
            "completed_at":    self.completed_at,
            "last_error":      self.last_error,
        }


class RuntimeTaskRegistry:
    """Process-scoped registry of active background tasks."""

    def __init__(self):
        self._tasks: dict[str, _RegisteredTask] = {}

    # ── Registration ────────────────────────────────────────────────
    def register(
        self,
        name:              str,
        coroutine_factory: Callable[[], Awaitable[Any]],
        *,
        owner_service:     str = "server_startup",
        task_type:         TaskType = "recurring_loop",
        critical:          bool = False,
        paid_provider:     bool = False,
        coordinator_job:   Optional[str] = None,
        cadence:           Cadence = "",
        startup_behavior:  str = "eager",
        restart_policy:    Restart = "self_managed",
        shutdown_timeout:  float = 10.0,
    ) -> _RegisteredTask:
        if name in self._tasks:
            raise ValueError(f"duplicate runtime task name: {name!r}")
        rt = _RegisteredTask(
            name=name, owner_service=owner_service, task_type=task_type,
            coroutine_factory=coroutine_factory, critical=critical,
            paid_provider=paid_provider, coordinator_job=coordinator_job,
            cadence=cadence, startup_behavior=startup_behavior,
            restart_policy=restart_policy, shutdown_timeout=shutdown_timeout,
        )
        self._tasks[name] = rt
        return rt

    def register_and_start(
        self, name: str, coroutine_factory: Callable[[], Awaitable[Any]],
        *, force: bool = False,
        **kw,
    ) -> Optional[asyncio.Task]:
        """Convenience — register then start the task in one call.

        ONE DATABASE AUTHORITY — background-worker gating (2026-10-05)
        ──────────────────────────────────────────────────────────────
        Emergent Support root-caused Phase-5 R3 pod recycling to
        event-loop starvation caused by optional background workers
        bypassing ``BACKGROUND_WORKERS_ENABLED=false``.  This gate
        makes the environment flag AUTHORITATIVE for every caller:

        * When ``BACKGROUND_WORKERS_ENABLED`` is false AND ``force``
          is false → the task is NOT created.  We skip registration
          entirely (no handle, no coroutine, no CPU) and log the
          suppression exactly once per task name.
        * When ``force=True`` is explicitly passed → the task bypasses
          the gate.  Reserve this for genuinely required tasks (e.g.
          deferred_task runners already pre-gated by the caller,
          lease heartbeats, request-serving infrastructure).
        * When ``BACKGROUND_WORKERS_ENABLED`` is true → normal
          behavior (register + start).

        Returns the asyncio.Task on success, or ``None`` when the
        task was suppressed by the authority gate.  Callers that
        chain off the return value must treat ``None`` as
        "skipped".
        """
        if not force:
            try:
                from services.data_authority import (
                    background_workers_enabled as _wk_enabled,
                    authority_mode as _mode,
                )
                if not _wk_enabled():
                    logger.info(
                        "runtime_task_registry: SUPPRESSED optional "
                        "background task '%s' (mode=%s, "
                        "BACKGROUND_WORKERS_ENABLED=false)",
                        name, _mode(),
                    )
                    return None
            except Exception:
                # Authority module missing — fall through to legacy
                # behavior so the pod can still boot.  This mirrors
                # the pre-existing fallback in server._deferred_task.
                pass
        self.register(name, coroutine_factory, **kw)
        return self.start(name)

    # ── Start / stop ────────────────────────────────────────────────
    def start(self, name: str) -> asyncio.Task:
        rt = self._tasks[name]
        if rt.handle is not None and not rt.handle.done():
            return rt.handle
        try:
            coro = rt.coroutine_factory()
        except Exception as e:
            rt.last_error = f"factory: {type(e).__name__}: {e}"
            raise
        task = asyncio.create_task(coro, name=name)
        rt.handle = task
        rt.started_at = time.time()
        return task

    def start_all(self, *, critical_only: bool = False) -> list[str]:
        started: list[str] = []
        for name, rt in list(self._tasks.items()):
            if critical_only and not rt.critical:
                continue
            if rt.startup_behavior == "manual":
                continue
            try:
                self.start(name)
                started.append(name)
            except Exception as e:
                rt.last_error = f"start: {type(e).__name__}: {e}"
                logger.warning("registry: failed to start %s: %s", name, e)
        return started

    async def stop(self, name: str, timeout: Optional[float] = None) -> str:
        rt = self._tasks.get(name)
        if rt is None or rt.handle is None:
            return "unknown"
        h = rt.handle
        if h.done():
            rt.completed_at = time.time()
            return rt.status
        h.cancel()
        try:
            await asyncio.wait_for(h, timeout=timeout or rt.shutdown_timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        except Exception as e:                                # pragma: no cover
            rt.last_error = f"stop: {type(e).__name__}: {e}"
        rt.completed_at = time.time()
        return rt.status

    async def stop_all(self, timeout: float = 10.0) -> dict[str, Any]:
        """Signal every registered task, then await them within
        ``timeout``.  Returns per-task statuses + counts."""
        started_ts = time.time()
        signalled: list[str] = []
        for name, rt in self._tasks.items():
            if rt.handle is not None and not rt.handle.done():
                rt.handle.cancel()
                signalled.append(name)
        # Wait for them to actually finish, bounded by `timeout`.
        pending = [rt.handle for rt in self._tasks.values()
                    if rt.handle is not None and not rt.handle.done()]
        if pending:
            try:
                await asyncio.wait(
                    pending, timeout=timeout,
                    return_when=asyncio.ALL_COMPLETED,
                )
            except Exception:                                 # pragma: no cover
                pass
        statuses = {name: rt.status for name, rt in self._tasks.items()}
        return {
            "tasks_signalled":   signalled,
            "statuses":          statuses,
            "elapsed_ms":        int((time.time() - started_ts) * 1000),
            "still_running":     [n for n, s in statuses.items() if s == "running"],
        }

    # ── Diagnostics ─────────────────────────────────────────────────
    def get_status(self, name: str) -> dict[str, Any]:
        rt = self._tasks.get(name)
        if rt is None:
            return {"name": name, "status": "unknown"}
        return rt.to_dict()

    def list_statuses(self) -> list[dict[str, Any]]:
        return [rt.to_dict() for rt in self._tasks.values()]

    def cleanup_completed(self) -> int:
        drop = [n for n, rt in self._tasks.items()
                 if rt.handle is not None and rt.handle.done()]
        for n in drop:
            self._tasks.pop(n, None)
        return len(drop)

    def mark_failure(self, name: str, error: str) -> None:
        rt = self._tasks.get(name)
        if rt is not None:
            rt.last_error = error

    def running_count(self) -> int:
        return sum(1 for rt in self._tasks.values() if rt.status == "running")

    def critical_all_running(self) -> bool:
        for rt in self._tasks.values():
            if rt.critical and rt.status != "running":
                return False
        return True

    def __len__(self) -> int:
        return len(self._tasks)


# ── Process-scoped singleton ────────────────────────────────────────
_registry: Optional[RuntimeTaskRegistry] = None


def get_registry() -> RuntimeTaskRegistry:
    global _registry
    if _registry is None:
        _registry = RuntimeTaskRegistry()
    return _registry


def _reset_registry_for_testing() -> None:  # pragma: no cover
    global _registry
    _registry = None


__all__ = [
    "RuntimeTaskRegistry",
    "get_registry",
]
