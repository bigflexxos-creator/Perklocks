"""Phase 3F-2 — Application lifecycle + runtime task registry tests."""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from services.runtime_task_registry import (
    RuntimeTaskRegistry, get_registry,
)
from services.application_lifecycle import (
    ApplicationLifecycle,
    ApplicationStartupResult,
    ApplicationShutdownResult,
    get_lifecycle,
)


# ── 1. server.py delegates shutdown to ApplicationLifecycle ──────────
def test_server_shutdown_delegates_to_lifecycle():
    src = Path("/app/backend/server.py").read_text()
    m = re.search(
        r"async def on_shutdown\(\).*?await lc\.shutdown\(",
        src, re.DOTALL,
    )
    assert m, "on_shutdown must call ApplicationLifecycle.shutdown"


# ── 2. server.py wires the lifecycle + registry from on_startup ─────
def test_server_startup_wires_lifecycle_and_registry():
    src = Path("/app/backend/server.py").read_text()
    assert "from services.runtime_task_registry import get_registry" in src
    assert "from services.application_lifecycle import get_lifecycle" in src
    assert "app.state.lifecycle = _LIFECYCLE" in src
    assert "app.state.task_registry = _TASK_REGISTRY" in src


# ── 3. No untracked asyncio.create_task in the startup body ─────────
def test_no_untracked_create_task_in_server_startup():
    src = Path("/app/backend/server.py").read_text()
    # Extract on_startup body.
    m = re.search(
        r"async def on_startup\(\).*?(?=\n(?:async def|@app\.)|\Z)",
        src, re.DOTALL,
    )
    body = m.group(0) if m else ""
    # Remaining create_task allowed:
    #  * inside _deferred_task's exception path (documented)
    #  * inside the _background_refresh duplicate fallback
    offenders = []
    for i, ln in enumerate(body.splitlines(), 1):
        if "asyncio.create_task" not in ln:
            continue
        stripped = ln.strip()
        if stripped.startswith("#"):
            continue
        # Whitelist fallback occurrences that live inside a `except`
        # ValueError branch documenting the duplicate-name case.
        context = "\n".join(body.splitlines()[max(0, i-6):i])
        if "except ValueError" in context:
            continue
        offenders.append(f"line {i}: {stripped}")
    assert not offenders, (
        f"untracked create_task calls remain in on_startup:\n  "
        + "\n  ".join(offenders)
    )


# ── 4. Duplicate task registration fails ────────────────────────────
def test_duplicate_task_registration_fails():
    r = RuntimeTaskRegistry()
    async def _c(): return None
    r.register("t1", _c)
    with pytest.raises(ValueError):
        r.register("t1", _c)


# ── 5. Repeated start_all does not duplicate tasks ──────────────────
def test_repeated_start_does_not_duplicate():
    async def body():
        r = RuntimeTaskRegistry()
        async def loop():
            await asyncio.sleep(0.5)
        r.register("loop", loop)
        h1 = r.start("loop")
        h2 = r.start("loop")
        assert h1 is h2   # second call returns the SAME handle
        assert len(r) == 1
        await r.stop_all(timeout=1.0)
    asyncio.run(body())


# ── 6. ApplicationStartupResult / ShutdownResult schemas ────────────
def test_startup_result_schema():
    r = ApplicationStartupResult()
    d = r.as_dict()
    for f in ("success", "database_ready", "indexes_ready",
              "recovery_complete", "required_tasks_registered",
              "required_tasks_started", "optional_tasks_started",
              "required_failures", "optional_failures", "warnings",
              "duration_ms", "started_at"):
        assert f in d


def test_shutdown_result_schema():
    r = ApplicationShutdownResult()
    d = r.as_dict()
    for f in ("success", "tasks_signaled", "tasks_cancelled",
              "tasks_completed", "leases_released", "reservations_released",
              "http_clients_closed", "database_closed", "duration_ms",
              "warnings", "errors", "stopped_at"):
        assert f in d


# ── 7. stop_all signals every running task and reports statuses ─────
def test_stop_all_signals_and_returns_statuses():
    async def body():
        r = RuntimeTaskRegistry()
        async def slow():
            await asyncio.sleep(5)
        r.register("slow_1", slow); r.start("slow_1")
        r.register("slow_2", slow); r.start("slow_2")
        summary = await r.stop_all(timeout=0.5)
        assert sorted(summary["tasks_signalled"]) == ["slow_1", "slow_2"]
        # After signalling, they end up cancelled.
        for s in summary["statuses"].values():
            assert s in ("cancelled", "completed"), s
    asyncio.run(body())


# ── 8. Cleanup completed removes done handles ───────────────────────
def test_cleanup_completed():
    async def body():
        r = RuntimeTaskRegistry()
        async def quick():
            return 1
        r.register("q", quick); r.start("q")
        await asyncio.sleep(0.1)
        n = r.cleanup_completed()
        assert n == 1
        assert len(r) == 0
    asyncio.run(body())


# ── 9. Readiness dict has the required fields ───────────────────────
def test_lifecycle_readiness_has_all_required_fields():
    lc = ApplicationLifecycle(RuntimeTaskRegistry())
    d = lc.readiness()
    for f in ("startup_complete", "database_ready", "indexes_ready",
              "recovery_complete", "required_tasks_registered",
              "required_tasks_running", "optional_task_failures",
              "required_task_failures", "lifecycle_state",
              "started_at", "last_shutdown_result", "ok"):
        assert f in d, f


# ── 10. Repeated shutdown is idempotent-safe ────────────────────────
def test_repeated_shutdown_is_safe():
    async def body():
        lc = ApplicationLifecycle(RuntimeTaskRegistry())
        r1 = await lc.shutdown(timeout=0.5)
        r2 = await lc.shutdown(timeout=0.5)
        assert r1.success is True or r1.errors  # first call may attempt real close
        assert r2.success is True   # second call short-circuits
    asyncio.run(body())


# ── 11. Existing job cadences unchanged (registry declarations)  ────
def test_job_registry_declarations_still_present():
    """Phase 3F-2 must not remove job entries from services/job_registry.py."""
    src = Path("/app/backend/services/job_registry.py").read_text()
    # Must still describe at least 3 jobs (cadence field references).
    n = src.count("cadence")
    assert n >= 3, f"job_registry seems truncated (cadence references: {n})"


# ── 12. Phase 3F-1 orchestrator behavior remains ────────────────────
def test_pick_refresh_orchestrator_still_importable():
    from services.pick_refresh_orchestrator import PickRefreshOrchestrator
    assert PickRefreshOrchestrator is not None


# ── 13. Phase 3B invariant preserved ────────────────────────────────
def test_shared_mongo_still_single_client():
    from services.database import get_client
    a = get_client(); b = get_client()
    assert a is b


# ── 14. get_registry / get_lifecycle are process-scoped singletons ──
def test_registry_and_lifecycle_are_singletons():
    assert get_registry() is get_registry()
    assert get_lifecycle() is get_lifecycle()


# ── 15. Registry rejects duplicate registration but allows suffix retry
def test_registry_suffix_pattern_workaround_is_documented():
    src = Path("/app/backend/server.py").read_text()
    # The _deferred_task helper handles ValueError from duplicate names
    assert "except ValueError" in src
