"""Regression tests for Emergent-Support's 2026-10-05 event-loop-starvation fix.

Covers:
 1. BACKGROUND_WORKERS_ENABLED=false → optional background tasks are NOT
    created (register_and_start returns None, no coroutine runs).
 2. BACKGROUND_WORKERS_ENABLED=true → normal behavior (task created,
    coroutine runs to completion).
 3. force=True → task is created regardless of the authority gate
    (reserved for required infrastructure like deferred_task runners).
 4. The main asyncio event loop stays responsive while a CPU-bound
    simulate_board-style function runs via asyncio.to_thread.

Run via:
    cd /app/backend && pytest tests/test_runtime_task_registry_gating.py -v
"""
from __future__ import annotations

import asyncio
import time

import pytest

from services import runtime_task_registry as rtr


@pytest.fixture(autouse=True)
def _reset_registry():
    """Each test gets a fresh registry."""
    rtr._reset_registry_for_testing()
    yield
    rtr._reset_registry_for_testing()


# ─── 1. workers=false → optional tasks SUPPRESSED ──────────────────────
@pytest.mark.asyncio
async def test_optional_task_suppressed_when_workers_disabled(monkeypatch):
    monkeypatch.setenv("BACKGROUND_WORKERS_ENABLED", "false")
    reg = rtr.get_registry()

    ran = {"n": 0}

    async def _worker():
        ran["n"] += 1

    handle = reg.register_and_start(
        "optional_test_worker", _worker,
        task_type="recurring_loop", critical=False,
    )

    assert handle is None, "register_and_start should return None when gate suppresses"
    # Nothing was registered either
    assert "optional_test_worker" not in getattr(reg, "_tasks", {})
    # Give the loop a tick to prove no task was scheduled
    await asyncio.sleep(0.05)
    assert ran["n"] == 0, "coroutine must NOT execute when workers disabled"


# ─── 2. workers=true → normal behavior preserved ───────────────────────
@pytest.mark.asyncio
async def test_optional_task_runs_when_workers_enabled(monkeypatch):
    monkeypatch.setenv("BACKGROUND_WORKERS_ENABLED", "true")
    reg = rtr.get_registry()

    ran = {"n": 0}

    async def _worker():
        ran["n"] += 1

    handle = reg.register_and_start(
        "optional_enabled_worker", _worker,
        task_type="one_shot", critical=False,
    )
    assert handle is not None
    await asyncio.wait_for(handle, timeout=2.0)
    assert ran["n"] == 1


# ─── 3. force=True bypasses the gate even when workers disabled ────────
@pytest.mark.asyncio
async def test_force_true_bypasses_gate(monkeypatch):
    monkeypatch.setenv("BACKGROUND_WORKERS_ENABLED", "false")
    reg = rtr.get_registry()

    ran = {"n": 0}

    async def _worker():
        ran["n"] += 1

    handle = reg.register_and_start(
        "forced_task", _worker,
        force=True,
        task_type="deferred_startup", critical=False,
    )
    assert handle is not None, "force=True must create the task even when workers disabled"
    await asyncio.wait_for(handle, timeout=2.0)
    assert ran["n"] == 1


# ─── 4. default force value is False (positional safety) ───────────────
@pytest.mark.asyncio
async def test_force_defaults_false(monkeypatch):
    """Regression guard: no accidental force=True default."""
    monkeypatch.setenv("BACKGROUND_WORKERS_ENABLED", "false")
    reg = rtr.get_registry()

    ran = {"n": 0}

    async def _worker():
        ran["n"] += 1

    # Explicitly do not pass force → defaults to False → suppressed.
    handle = reg.register_and_start("default_force", _worker)
    assert handle is None
    await asyncio.sleep(0.05)
    assert ran["n"] == 0


# ─── 5. Event loop stays responsive while CPU-bound sim runs via to_thread
@pytest.mark.asyncio
async def test_event_loop_responsive_during_simulation_via_to_thread():
    """Prove asyncio.to_thread keeps the main event loop responsive
    while CPU-bound sync work (simulate_board-style) is executing.

    Simulates the production scenario where a request handler needs
    to respond quickly (e.g. /api/health) while a Monte Carlo sweep
    runs for the picks pipeline.
    """

    def _cpu_bound_simulation(duration_s: float = 0.6) -> int:
        # Busy loop to pin a CPU thread — mirror simulate_board shape.
        t_end = time.time() + duration_s
        n = 0
        while time.time() < t_end:
            # Pure Python work; GIL releases on I/O boundary at the end
            # of each loop iteration naturally via time.time().
            n += 1
        return n

    tick_latencies: list[float] = []

    async def _health_pulser():
        """Pretend to be the health endpoint: tick every 50ms and
        record the actual wall-clock gap between ticks."""
        prev = time.perf_counter()
        for _ in range(20):   # ~1 second total of ticks
            await asyncio.sleep(0.05)
            now = time.perf_counter()
            tick_latencies.append((now - prev) * 1000.0)   # ms
            prev = now

    # Fire the sim via to_thread + run the health pulser concurrently.
    sim_task = asyncio.create_task(
        asyncio.to_thread(_cpu_bound_simulation, 0.6),
    )
    pulser_task = asyncio.create_task(_health_pulser())
    await asyncio.gather(sim_task, pulser_task)

    # The event loop must have serviced the pulser on time: no single
    # 50-ms tick should have been delayed by more than ~5× its target
    # (i.e. <250 ms), which would indicate loop starvation.
    worst = max(tick_latencies)
    assert worst < 250.0, (
        f"Event loop was starved while to_thread ran a CPU-bound sim: "
        f"worst inter-tick latency {worst:.1f}ms (target <250ms)"
    )
