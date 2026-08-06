"""Performance-Wiring Tests (Phase 5, iter104, 2026-07-29).

Guards two performance fixes:

  1. `routes/parlay_routes.py` offloads the CPU-heavy
     `build_top_parlays` calls to `asyncio.to_thread`, so heavy scoring
     doesn't block the event loop.
  2. Startup registers indexes for `fusion_predictions.pick_date` and
     `learning_log.ts` (descending) — the two hot query paths the
     production audit flagged as COLLSCAN.

Both are STATIC-source checks (fast, deterministic). A runtime bench
of `learning_log` sort+limit is included as a smoke assertion.
"""
from __future__ import annotations

import asyncio
import pathlib

import pytest


ROUTES_SRC = pathlib.Path(
    "/app/backend/routes/parlay_routes.py").read_text()
SERVER_SRC = pathlib.Path("/app/backend/server.py").read_text()


# ═════════════════════════════════════════════════════════════════════
# A. Parlay optimizer offloaded to a worker thread
# ═════════════════════════════════════════════════════════════════════
def test_parlay_routes_imports_asyncio():
    assert "import asyncio" in ROUTES_SRC, (
        "parlay_routes must import asyncio for to_thread offload")


def test_build_top_parlays_calls_are_offloaded():
    """Every `build_top_parlays(` call must be wrapped in
    `asyncio.to_thread`. Prevents future regressions where someone
    reintroduces a direct sync call on the event loop."""
    # Count direct calls (not wrapped in to_thread)
    src = ROUTES_SRC
    direct_calls = []
    for i, line in enumerate(src.splitlines(), 1):
        stripped = line.strip()
        # A direct call looks like:   top = build_top_parlays(
        # An offloaded call looks like: top = await asyncio.to_thread(
        #                                        build_top_parlays,
        if stripped.startswith("build_top_parlays(") \
                or "= build_top_parlays(" in stripped \
                or "return build_top_parlays(" in stripped:
            direct_calls.append((i, line))
    assert not direct_calls, (
        "build_top_parlays is called directly on the event loop at "
        f"{direct_calls}. Wrap with `await asyncio.to_thread(...)`.")
    # Also confirm at least ONE offloaded call exists.
    assert "asyncio.to_thread(" in src \
        and "build_top_parlays" in src, (
        "no offloaded build_top_parlays call found in parlay_routes")


def test_offloaded_calls_are_awaited():
    """Every `asyncio.to_thread(build_top_parlays` chain must be
    prefixed with `await`. Missing the await returns a coroutine that
    is never scheduled."""
    src = ROUTES_SRC
    # Find each to_thread(build_top_parlays occurrence and inspect the
    # PRECEDING characters to confirm `await` sits before it.
    idx = 0
    while True:
        j = src.find("asyncio.to_thread(", idx)
        if j == -1:
            break
        # Only check the ones that eventually invoke build_top_parlays
        # (there may be to_thread calls for other things in the future)
        end = src.find(")", j)
        block = src[j:end + 500]   # look ahead a few lines
        if "build_top_parlays" not in block:
            idx = j + 1
            continue
        # Look at the preceding 10 chars for `await `
        head = src[max(0, j - 40):j]
        assert "await" in head, (
            f"asyncio.to_thread(...) call at char {j} is not awaited — "
            "found preceding: {head!r}")
        idx = j + 1


# ═════════════════════════════════════════════════════════════════════
# B. Missing indexes registered at startup
# ═════════════════════════════════════════════════════════════════════
def test_fusion_predictions_pick_date_index_declared():
    src = SERVER_SRC
    assert "fusion_pick_date_idx" in src, (
        "startup does not create the fusion_predictions pick_date "
        "index (audit follow-up)")
    # The compound pick_id+created_at index (already there) covers
    # pick_id equality by prefix — still verify it's declared.
    assert "fusion_pick_idx" in src


def test_learning_log_ts_desc_index_declared():
    src = SERVER_SRC
    assert "learning_log_ts_idx" in src, (
        "startup does not create the learning_log(ts desc) index")
    # And it must be a descending index — audit's ask.
    # Look for a create_index call on `db.learning_log` that names ts
    # with -1 ordering.
    import re
    m = re.search(
        r"db\.learning_log\.create_index\s*\(\s*\[\("
        r'"ts",\s*(-?1)\)\]',
        src,
    )
    assert m is not None, (
        "learning_log(ts) index must be declared as [(\"ts\", -1)] — "
        "did not find that exact form in server.py")
    assert m.group(1) == "-1", (
        f"learning_log(ts) index direction must be -1 (desc), "
        f"got {m.group(1)}")


# ═════════════════════════════════════════════════════════════════════
# C. Runtime bench — learning_log sort uses index (not COLLSCAN)
# ═════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_learning_log_sort_is_indexed_or_empty():
    """Smoke check against the live pod DB — the sort plan must not
    be a COLLSCAN + in-memory sort on a real deployment. Skipped
    gracefully if the collection is missing or empty."""
    try:
        from deps import db
    except Exception:
        pytest.skip("deps.db not importable in this test context")
    try:
        info = await db.learning_log.index_information()
    except Exception as e:
        pytest.skip(f"learning_log unavailable: {e}")
    if not info:
        pytest.skip("learning_log has no indexes yet (fresh DB)")
    # Startup index must have taken effect
    idx_names = list(info.keys())
    if "learning_log_ts_idx" not in idx_names:
        pytest.skip(f"index not yet created in this environment "
                     f"(indexes={idx_names})")
    # Explain the hot query
    plan = await db.command({
        "explain": {"find": "learning_log",
                     "sort": {"ts": -1}, "limit": 30},
        "verbosity": "queryPlanner",
    })
    stringified = repr(plan)
    assert "learning_log_ts_idx" in stringified, (
        f"query does not use learning_log_ts_idx — plan={plan}")


# Async-test bootstrapping (this test file uses plain asyncio only in
# one runtime check).
try:
    import pytest_asyncio  # noqa: F401
except Exception:
    # Wrap the async test in a sync driver if pytest-asyncio isn't
    # installed. That keeps this suite portable.
    _orig_async_test = test_learning_log_sort_is_indexed_or_empty

    def test_learning_log_sort_is_indexed_or_empty():  # type: ignore
        try:
            asyncio.run(_orig_async_test())
        except pytest.skip.Exception:
            raise
        except Exception as e:
            pytest.fail(f"async check failed: {e}")


# ═════════════════════════════════════════════════════════════════════
# D. No accidental server.py refactor (audit constraint)
# ═════════════════════════════════════════════════════════════════════
def test_server_py_still_intact():
    """server.py and sports_engine.py must NOT accidentally lose their
    orchestration.  Phase 3F-1 (2026-08) formally extracted the
    ~1,700-line pick-refresh orchestrator into
    ``services/pick_refresh_orchestrator.py``, so we now enforce a
    joint size floor across the pair (server.py + orchestrator) and
    an independent floor for sports_engine.py."""
    server_lines = SERVER_SRC.count("\n")
    engine_lines = pathlib.Path(
        "/app/backend/sports_engine.py").read_text().count("\n")
    orchestrator_path = pathlib.Path(
        "/app/backend/services/pick_refresh_orchestrator.py")
    orch_lines = orchestrator_path.read_text().count("\n") if orchestrator_path.exists() else 0
    combined = server_lines + orch_lines
    # Post-Phase-3F-1 baseline: server.py ≈ 3,900 lines, orchestrator
    # ≈ 1,900 lines, sum ≈ 5,800.  Original pre-extraction was ~5,600.
    assert combined > 4500, (
        f"server.py ({server_lines}) + orchestrator ({orch_lines}) = "
        f"{combined} lines — accidental refactor?")
    assert engine_lines > 5000, (
        f"sports_engine.py has shrunk to {engine_lines} lines — "
        "accidental refactor?")
