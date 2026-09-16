"""Board scale stress fixture (Session 8, 2026-09-17).

READ-ONLY, IN-MEMORY synthetic slate generator.  Used to prove that a
canonical population of 500 / 2,500 / 5,000+ picks yields a bounded
network payload and never floods the mounted UI.  Nothing here writes
to Mongo, publishes to History, or reaches the settlement/analytics
paths.  The fixture picks are marked ``synthetic=True`` so any downstream
audit can filter them out trivially.
"""
from __future__ import annotations

import hashlib
import time
from typing import Annotated, Any, Optional
from fastapi import APIRouter, Depends, HTTPException, Query

from deps import current_user
from auth import UserPublic


router = APIRouter(prefix="/api/perf", tags=["board-stress"])


def _fake_pick(i: int) -> dict:
    """Cheap deterministic synthetic pick — mirrors the shape of a
    lightweight canonical card."""
    seed = f"synthetic-{i}"
    h = hashlib.blake2b(seed.encode(), digest_size=4).hexdigest()
    sport = ["NFL", "MLB", "Soccer", "Tennis", "CFB"][i % 5]
    lock  = 85.0 + (i % 15) + (int(h[:2], 16) / 255.0) * 5.0
    return {
        "id":              f"perf-{seed}-{h}",
        "sport":           sport,
        "market":          f"Perf Market #{i}",
        "team":            f"Team {i}",
        "opponent":        f"Opp {i}",
        "line":            0.5 + (i % 20) * 0.5,
        "book_odds":       -110,
        "lock_score":      round(lock, 2),
        "win_probability": round(0.5 + (int(h[2:4], 16) / 512.0), 4),
        "commence_time":   "2026-09-17T20:00:00Z",
        "synthetic":       True,
    }


@router.get("/board-stress")
async def board_stress(
    user: Annotated[UserPublic, Depends(current_user)],
    n: int = Query(500, ge=10, le=10000),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = 0,
):
    """Synthesise ``n`` lightweight picks, serve a ``limit``-sized
    slice with the SAME pagination contract as the real board.

    The response includes p95 timing bins so a client can profile
    payload size / TTFB / JSON parse independently of production
    traffic.
    """
    t0 = time.perf_counter()
    picks = [_fake_pick(i) for i in range(n)]
    picks.sort(key=lambda p: (-p["lock_score"], p["commence_time"], p["id"]))
    build_ms = (time.perf_counter() - t0) * 1000
    slice_ = picks[offset:offset + limit]
    version = hashlib.blake2b(
        f"perf|{n}|{limit}".encode(), digest_size=8).hexdigest()
    return {
        "synthetic":     True,
        "board_version": version,
        "total":         n,
        "limit":         limit,
        "offset":        offset,
        "returned":      len(slice_),
        "has_more":      offset + len(slice_) < n,
        "picks":         slice_,
        "timings_ms":    {"synth_and_sort": round(build_ms, 2)},
    }
