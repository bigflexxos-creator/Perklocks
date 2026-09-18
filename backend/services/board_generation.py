"""BOARD GENERATION AUTHORITY (iteration 144) — committed-generation boundary.

Readers of the Locks board (GET /api/picks/today) must never observe a
partially rebuilt population.  Every membership-changing build (scheduled
refresh, manual UPDATE, regeneration, healing) runs inside a generation:

    BUILDING → VALIDATING → COMMITTED | FAILED

While ANY generation is BUILDING the read path is PINNED to the last
COMMITTED snapshots (board_snapshot_cache ignores its TTL and refuses to
store new snapshots), so a request can only see generation A until B
commits — one visible transition.  The active pointer is a single tiny
document updated with ONE conditional (compare-and-set) operation.

Nothing here copies unrelated collections; generation truth is for
published board membership only.
"""
from __future__ import annotations

import logging
import os
import socket
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("lockscore.board_generation")

COLL = "board_generations"
ACTIVE_ID = "active"
SCHEMA_VERSION = "board.v1"
BACKEND_REVISION = os.environ.get("BACKEND_REVISION") or os.environ.get("GIT_SHA") or "dev"

_building: dict[str, dict] = {}          # generation_id -> {scope, started}
_active: dict[str, Any] = {"generation_id": None, "board_version": None,
                           "committed_at": None, "revision": 0, "state": "COMMITTED"}


def _db():
    from server import db as _d
    return _d


def is_building() -> bool:
    return bool(_building)


def building_ids() -> list[str]:
    return list(_building)


def active() -> dict[str, Any]:
    return dict(_active)


async def load_active() -> dict[str, Any]:
    doc = await _db()[COLL].find_one({"_id": ACTIVE_ID}, {"_id": 0})
    if doc:
        _active.update({k: doc.get(k) for k in ("generation_id", "board_version", "committed_at", "revision")})
        _active["state"] = "COMMITTED"
    return active()


async def begin(scope: str = "ALL") -> str:
    """Open candidate generation B.  Readers keep serving A (pinned)."""
    gen_id = f"gen_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}"
    _building[gen_id] = {"scope": scope, "started": time.time()}
    try:
        await _db()[COLL].insert_one({
            "_id": gen_id, "generation_id": gen_id, "scope": scope, "state": "BUILDING",
            "expected_active": _active.get("generation_id"),
            "started_at": datetime.now(timezone.utc).isoformat(), "backend_revision": BACKEND_REVISION,
        })
    except Exception as exc:  # DB hiccup must not block the build itself
        logger.warning("generation begin persist failed: %s", exc)
    return gen_id


async def _set_state(gen_id: str, state: str, **extra) -> None:
    try:
        await _db()[COLL].update_one({"_id": gen_id}, {"$set": {"state": state, **extra}})
    except Exception as exc:
        logger.warning("generation state persist failed (%s→%s): %s", gen_id, state, exc)


async def validate(gen_id: str, pick_count: int) -> bool:
    """Publication/completeness validation gate.  A generation that
    produced NOTHING for a full-board scope is not a committable board
    (A stays active — 'never delete A then slowly construct B')."""
    await _set_state(gen_id, "VALIDATING", pick_count=pick_count)
    scope = (_building.get(gen_id) or {}).get("scope") or "ALL"
    if scope == "ALL" and pick_count <= 0:
        return False
    return True


async def board_state() -> tuple[Optional[str], int, int]:
    """(board_version, pick_count, event_count) of the current published
    main-board population (upcoming events, Locks contract)."""
    try:
        from services.main_board_eligibility import main_board_lock_score_query
        from services.board_snapshot_cache import compute_board_version
        q = dict(main_board_lock_score_query())
        q["event_time"] = {"$gte": datetime.now(timezone.utc).isoformat()[:19]}
        rows = await _db().picks.find(q, {"_id": 0, "id": 1, "canonical_event_id": 1, "event_id": 1,
                                          "event": 1, "updated_at": 1}).to_list(20000)
        events = {str(r.get("canonical_event_id") or r.get("event_id") or r.get("event")) for r in rows}
        return compute_board_version(rows), len(rows), len(events)
    except Exception as exc:
        logger.warning("board_state failed: %s", exc)
        return None, 0, 0


async def commit(gen_id: str, board_version: Optional[str], pick_count: int, event_count: int) -> bool:
    """Atomic active-pointer promotion A → B (compare-and-set on the
    expected active generation).  Exactly ONE snapshot invalidation, and
    ONLY when board membership actually changed — a build that reproduces
    the identical population is a no-op commit (A stays, no churn)."""
    if board_version is None:
        board_version, pick_count, event_count = await board_state()
    if board_version is not None and board_version == _active.get("board_version"):
        _building.pop(gen_id, None)
        await _set_state(gen_id, "COMMITTED_NOOP", board_version=board_version,
                         committed_at=datetime.now(timezone.utc).isoformat())
        return True
    expected = _active.get("generation_id")
    now = datetime.now(timezone.utc).isoformat()
    revision = int(_active.get("revision") or 0) + 1
    won = True
    try:
        coll = _db()[COLL]
        res = await coll.find_one_and_update(
            {"_id": ACTIVE_ID, "$or": [{"generation_id": expected}, {"generation_id": {"$exists": False}}]},
            {"$set": {"generation_id": gen_id, "board_version": board_version, "committed_at": now,
                      "revision": revision, "pick_count": pick_count, "event_count": event_count,
                      "backend_revision": BACKEND_REVISION}},
            upsert=False,
        )
        if res is None:
            cur = await coll.find_one({"_id": ACTIVE_ID})
            if cur is None:
                await coll.update_one({"_id": ACTIVE_ID}, {"$setOnInsert": {
                    "generation_id": gen_id, "board_version": board_version, "committed_at": now,
                    "revision": revision, "pick_count": pick_count, "event_count": event_count}}, upsert=True)
            else:
                # Another valid generation already won the commit — do not overwrite blindly.
                won = False
                _active.update({k: cur.get(k) for k in ("generation_id", "board_version", "committed_at", "revision")})
    except Exception as exc:
        logger.warning("generation commit persist failed: %s", exc)
    _building.pop(gen_id, None)
    if won:
        _active.update({"generation_id": gen_id, "board_version": board_version, "committed_at": now,
                        "revision": revision, "state": "COMMITTED"})
        await _set_state(gen_id, "COMMITTED", committed_at=now, board_version=board_version,
                         pick_count=pick_count, event_count=event_count, revision=revision)
    else:
        await _set_state(gen_id, "SUPERSEDED")
    if won:
        # One visible transition: drop pinned A snapshots now that B is committed.
        try:
            from services.board_snapshot_cache import clear_cache
            clear_cache()
        except Exception:
            pass
    logger.info("board generation %s %s (rev=%s picks=%s events=%s)", gen_id,
                "COMMITTED" if won else "SUPERSEDED", revision, pick_count, event_count)
    return won


async def fail(gen_id: str, reason: str) -> None:
    """B failed → A REMAINS ACTIVE.  No snapshot invalidation."""
    _building.pop(gen_id, None)
    await _set_state(gen_id, "FAILED", failed_at=datetime.now(timezone.utc).isoformat(), reason=str(reason)[:300])
    logger.warning("board generation %s FAILED: %s — active generation retained", gen_id, reason)


def authority_fingerprint() -> dict[str, Any]:
    """Runtime authority fingerprint (no secrets): lets Web and Expo prove
    they consume the same environment + backend revision."""
    import hashlib
    env = os.environ.get("ENVIRONMENT_ID") or os.environ.get("EXPO_TUNNEL_SUBDOMAIN") or socket.gethostname()
    dbn = os.environ.get("DB_NAME") or ""
    return {"environment_id": env, "backend_revision": BACKEND_REVISION,
            "authority": hashlib.sha1(f"{env}|{dbn}|{BACKEND_REVISION}".encode()).hexdigest()[:12],
            "pid": os.getpid()}


def manifest(board_version: Optional[str], pick_count: int, event_count: int,
             generated_at: Optional[str] = None) -> dict[str, Any]:
    """Additive lightweight truth manifest for critical board responses."""
    a = active()
    state = "COMMITTED"
    if a.get("generation_id") is None:
        state = "LEGACY_UNTRACKED"   # before first tracked build — clients treat as committed
    return {
        "generation_id": a.get("generation_id"),
        "generation_state": state,
        "board_version": board_version,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "committed_at": a.get("committed_at"),
        "revision": a.get("revision") or 0,
        "pick_count": pick_count,
        "event_count": event_count,
        "schema_version": SCHEMA_VERSION,
        **authority_fingerprint(),
    }


__all__ = ["begin", "validate", "commit", "fail", "is_building", "active", "load_active",
           "manifest", "authority_fingerprint", "building_ids"]
