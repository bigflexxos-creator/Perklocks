"""P0 Server-Stability — Live memory diagnostics.

Read-only introspection endpoints for identifying memory retainers
inside the running Uvicorn worker.  Zero mutation of application
state.  Every endpoint is gated behind a simple query-token check
so it isn't callable from the public internet even though it is
mounted under ``/api``.

Endpoints
---------

    GET  /api/_diag/memory                 — RSS + malloc arena stats
    GET  /api/_diag/memory/types           — gc.get_objects() aggregated by type
    GET  /api/_diag/memory/globals         — largest module-level dict/list/set
    GET  /api/_diag/memory/asyncio         — task + thread counts
    POST /api/_diag/memory/trim            — force malloc_trim(0)

The token is read from env ``PL_DIAG_TOKEN`` (default ``lockmem2026``);
set it via .env or leave as-is for local diagnostics.
"""
from __future__ import annotations

import gc
import os
import sys
import threading
import time
import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Query

try:
    from services.memory_hygiene import stats as _hygiene_stats, malloc_trim as _trim
except Exception:                                       # pragma: no cover
    _hygiene_stats = lambda: {}                          # type: ignore
    _trim = lambda: False                                # type: ignore


router = APIRouter(prefix="/api/_diag/memory", tags=["diag"])

_DEFAULT_TOKEN = "lockmem2026"


def _auth(token: str) -> None:
    expected = os.environ.get("PL_DIAG_TOKEN", _DEFAULT_TOKEN)
    if token != expected:
        raise HTTPException(status_code=403, detail="invalid token")


# --- helpers ---------------------------------------------------------

def _rss_snapshot() -> dict:
    """Read /proc/self/status for RSS + thread count."""
    out: dict[str, Any] = {}
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith((
                    "VmRSS:", "VmSize:", "VmData:", "VmPeak:",
                    "RssAnon:", "RssFile:", "RssShmem:", "Threads:",
                )):
                    key, val = line.split(":", 1)
                    out[key] = val.strip()
    except Exception as exc:
        out["_error"] = str(exc)
    return out


def _rollup_snapshot() -> dict:
    """/proc/self/smaps_rollup for authoritative Pss/Anon totals."""
    out: dict[str, Any] = {}
    try:
        with open("/proc/self/smaps_rollup") as f:
            for line in f:
                if ":" in line:
                    k, v = line.split(":", 1)
                    if k in {"Rss", "Pss", "Pss_Anon", "Pss_File",
                             "Anonymous", "Private_Dirty",
                             "Private_Clean", "Shared_Clean",
                             "AnonHugePages", "Swap"}:
                        out[k] = v.strip()
    except Exception as exc:
        out["_error"] = str(exc)
    return out


def _map_buckets() -> dict:
    """Bucket every rw-p mapping in /proc/self/smaps by size class."""
    buckets = {
        "stack_8mb":  {"count": 0, "rss_kb": 0},
        "small_<8mb": {"count": 0, "rss_kb": 0},
        "med_8_64mb": {"count": 0, "rss_kb": 0},
        "huge_>=64mb": {"count": 0, "rss_kb": 0},
        "ro":         {"count": 0, "rss_kb": 0},
    }
    try:
        sz = 0
        perm = ""
        with open("/proc/self/smaps") as f:
            for line in f:
                if line and line[0].isalnum() and line[0] != "V":
                    # header line — parse permission
                    parts = line.split()
                    if len(parts) >= 2:
                        perm = parts[1]
                    sz = 0
                elif line.startswith("Size:"):
                    sz = int(line.split()[1])
                elif line.startswith("Rss:"):
                    rss = int(line.split()[1])
                    if perm == "rw-p":
                        if sz == 8192:
                            b = "stack_8mb"
                        elif sz < 8192:
                            b = "small_<8mb"
                        elif sz <= 65536:
                            b = "med_8_64mb"
                        else:
                            b = "huge_>=64mb"
                    else:
                        b = "ro"
                    buckets[b]["count"] += 1
                    buckets[b]["rss_kb"] += rss
    except Exception as exc:
        buckets["_error"] = str(exc)                     # type: ignore
    return buckets


def _top_types(limit: int = 30) -> list[dict]:
    """Aggregate all live objects by type — count + total size."""
    from collections import defaultdict
    counts = defaultdict(int)
    sizes = defaultdict(int)
    for obj in gc.get_objects():
        try:
            t = type(obj).__name__
            counts[t] += 1
            try:
                sizes[t] += sys.getsizeof(obj)
            except Exception:
                pass
        except Exception:
            continue
    ranked = sorted(sizes.items(), key=lambda kv: -kv[1])[: limit]
    return [
        {"type": t, "count": counts[t], "total_bytes": sz}
        for t, sz in ranked
    ]


def _largest_globals(limit: int = 40, min_kb: int = 128) -> list[dict]:
    """Enumerate module-level containers inside /app/backend and rank
    by shallow ``sys.getsizeof`` * len."""
    out: list[dict] = []
    for mod_name, mod in list(sys.modules.items()):
        if mod is None:
            continue
        mod_file = getattr(mod, "__file__", "") or ""
        if "/app/backend" not in mod_file:
            continue
        try:
            gv = getattr(mod, "__dict__", {})
        except Exception:
            continue
        for k, v in list(gv.items()):
            if k.startswith("__") and k.endswith("__"):
                continue
            try:
                if isinstance(v, (dict, list, set, tuple, bytes, bytearray)):
                    if isinstance(v, dict):
                        try:
                            approx = sys.getsizeof(v) + sum(
                                sys.getsizeof(x) for x in list(v.values())[:200]
                            )
                        except Exception:
                            approx = sys.getsizeof(v)
                        length = len(v)
                    elif isinstance(v, (list, set, tuple)):
                        try:
                            approx = sys.getsizeof(v) + sum(
                                sys.getsizeof(x) for x in list(v)[:200]
                            )
                        except Exception:
                            approx = sys.getsizeof(v)
                        length = len(v)
                    else:
                        approx = sys.getsizeof(v)
                        length = len(v)
                    if approx >= min_kb * 1024:
                        out.append({
                            "module": mod_name,
                            "name": k,
                            "type": type(v).__name__,
                            "len": length,
                            "approx_bytes": approx,
                        })
                # Also flag pandas / pyarrow if they exist
                type_name = type(v).__module__ + "." + type(v).__name__
                if any(t in type_name for t in (
                    "pandas.", "pyarrow.", "numpy.ndarray",
                )):
                    try:
                        # Best-effort size
                        if hasattr(v, "memory_usage"):
                            approx = int(v.memory_usage(deep=True).sum())
                        elif hasattr(v, "nbytes"):
                            approx = int(v.nbytes)
                        else:
                            approx = sys.getsizeof(v)
                        out.append({
                            "module": mod_name,
                            "name": k,
                            "type": type_name,
                            "len": None,
                            "approx_bytes": approx,
                        })
                    except Exception:
                        pass
            except Exception:
                continue
    out.sort(key=lambda r: -r["approx_bytes"])
    return out[:limit]


def _asyncio_snapshot() -> dict:
    tasks: list[str] = []
    try:
        loop = asyncio.get_running_loop()
        for t in asyncio.all_tasks(loop):
            name = t.get_name()
            coro = t.get_coro()
            qn = getattr(coro, "__qualname__", str(coro))
            tasks.append(f"{name}: {qn}")
    except Exception as exc:
        tasks.append(f"error: {exc}")
    threads = [t.name for t in threading.enumerate()]
    return {
        "asyncio_task_count": len(tasks),
        "asyncio_tasks":      tasks[:80],
        "thread_count":       len(threads),
        "thread_names":       threads[:60],
    }


# --- Endpoints -------------------------------------------------------

@router.get("")
async def memory_overview(token: str = Query(...)):
    _auth(token)
    return {
        "ts": time.time(),
        "pid": os.getpid(),
        "status": _rss_snapshot(),
        "rollup": _rollup_snapshot(),
        "map_buckets_kb": _map_buckets(),
        "hygiene": _hygiene_stats(),
        "gc_counts": gc.get_count(),
        "gc_threshold": gc.get_threshold(),
    }


@router.get("/types")
async def top_types(token: str = Query(...), limit: int = Query(30, ge=1, le=200)):
    _auth(token)
    return {"top_types": _top_types(limit=limit)}


@router.get("/globals")
async def largest_globals(
    token: str = Query(...),
    limit: int = Query(40, ge=1, le=200),
    min_kb: int = Query(128, ge=1, le=1_000_000),
):
    _auth(token)
    return {"largest_globals": _largest_globals(limit=limit, min_kb=min_kb)}


@router.get("/asyncio")
async def asyncio_snapshot(token: str = Query(...)):
    _auth(token)
    return _asyncio_snapshot()


@router.post("/trim")
async def force_trim(token: str = Query(...)):
    _auth(token)
    released = _trim()
    return {"released": released, "hygiene_stats": _hygiene_stats()}


__all__ = ["router"]
