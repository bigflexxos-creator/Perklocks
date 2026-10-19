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

────────────────────────────────────────────────────────────────────
2026-06  P0 CROSS-POD AUTHORITY FIX (Master Surgical Fix Build)
────────────────────────────────────────────────────────────────────
Support RCA confirmed Production had TWO active pods whose in-memory
``_active`` dicts drifted from the shared DB active-generation doc
while committing newer generations locally — the DB pointer was
stuck on an Oct 6 generation.  The CAS at line 170 was reading its
"expected" value from the pod-local in-memory snapshot that is only
refreshed every 8 s by ``middlewares/board_version_header.py`` on
inbound ``/api/picks`` traffic.  A pod whose HTTP traffic is routed
elsewhere drifts arbitrarily far from the DB.  Combined with:

  • a ``$setOnInsert`` fallback that silently no-ops when another
    pod inserts the doc between our ``find_one`` and ``update_one``,
  • a no-op short-circuit that trusts stale local ``board_version``,
  • no ``revision`` guard in the CAS filter,
  • no retry when a legitimately fresh build loses the first CAS,

the net result was that the shared DB active-generation pointer
could stop advancing while pods happily updated their own local
state.  The fix below:

  1. ALWAYS re-reads the DB active-generation document fresh at the
     top of ``commit()`` — the expected prior comes from the DB,
     NEVER from the pod-local cache.
  2. Includes ``revision`` in the CAS filter so advances are
     strictly monotonic.
  3. Verifies the ``$setOnInsert`` branch actually inserted
     (``upserted_id`` must be present) — if another pod won the
     insert race we treat it as a legitimate CAS loss.
  4. Retries the CAS ONCE with the freshly-adopted expected after
     a first-attempt miss, so a pod whose build is still valid can
     carry it over the newer DB state instead of silently dropping.
  5. Enforces an empty-board belt-and-suspenders check in
     ``commit()`` itself (``validate()`` already guards this but
     maintenance scripts bypass it).
  6. Exports ``load_active_cached()`` with a 2.5 s TTL so the
     ``/api/picks/today`` read path can consult the DB directly
     without hammering it.
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

# Stable per-process pod identifier used in anomaly logs + /api/picks/today
# diagnostics so cross-pod drift is traceable.
POD_ID = (
    os.environ.get("INSTANCE_ID")
    or os.environ.get("HOSTNAME")
    or os.environ.get("POD_ID")
    or socket.gethostname()
    or f"pod-{uuid.uuid4().hex[:8]}"
)

_building: dict[str, dict] = {}          # generation_id -> {scope, started}
_active: dict[str, Any] = {"generation_id": None, "board_version": None,
                           "committed_at": None, "revision": 0, "state": "COMMITTED"}

# Short-TTL cache for the DB active-generation document — lets the
# ``/api/picks/today`` read path cross-check pod-local state against
# the authoritative shared pointer at low cost.  2.5 s is well below
# the human-perceptible freshness threshold and keeps DB hit rate
# bounded on hot paths.
_LOAD_ACTIVE_CACHE_TTL_S = 2.5
_load_active_cache: dict[str, Any] = {"ts": 0.0, "doc": None}

# P0 LOCKS-STARVATION FIX (2026-06) — orphan-generation reaper.
# ``_refresh_picks`` schedules ``begin() → build() → validate() → commit()``.
# If the coroutine is cancelled (asyncio.CancelledError inherits from
# BaseException in Py 3.8+, bypassing the ``except Exception`` handler),
# or if any step above raises before commit()/fail() runs, the entry in
# ``_building`` leaks forever.  Once ``_building`` is non-empty
# ``is_building()`` returns True indefinitely, which pins the board
# snapshot cache to empty ⇒ every ``/api/picks/today?lite=true`` request
# skips the cache and runs the full 3-6 s canonical pipeline.
#
# The reaper below treats any entry older than
# ``_STALE_GENERATION_SEC`` as dead and drops it silently.  A normal
# build completes in seconds; five minutes is a very safe threshold
# above the 25-minute lease TTL guard downstream — no legitimate build
# is ever mistaken for an orphan.
_STALE_GENERATION_SEC = int(os.environ.get("BOARD_GEN_STALE_SEC", "300"))


def _reap_stale() -> None:
    if not _building:
        return
    _now = time.time()
    _dead = [gid for gid, info in _building.items()
             if _now - float(info.get("started") or _now) > _STALE_GENERATION_SEC]
    for gid in _dead:
        _building.pop(gid, None)
        logger.warning(
            "board_generation: reaped orphan BUILDING entry %s "
            "(age > %ds — likely task cancellation or unhandled exception)",
            gid, _STALE_GENERATION_SEC,
        )


def _db():
    from server import db as _d
    return _d


def is_building() -> bool:
    # P0 LOCKS-STARVATION FIX (2026-06) — reap stale orphans before
    # answering so a leaked BUILDING entry cannot pin the snapshot
    # cache indefinitely.  Cheap: hot path is a length check.
    _reap_stale()
    return bool(_building)


def building_ids() -> list[str]:
    _reap_stale()
    return list(_building)


def active() -> dict[str, Any]:
    return dict(_active)


async def load_active() -> dict[str, Any]:
    doc = await _db()[COLL].find_one({"_id": ACTIVE_ID}, {"_id": 0})
    if doc:
        _active.update({k: doc.get(k) for k in ("generation_id", "board_version", "committed_at", "revision")})
        _active["state"] = "COMMITTED"
    return active()


async def load_active_cached(ttl_s: float = _LOAD_ACTIVE_CACHE_TTL_S) -> dict[str, Any]:
    """DB-authoritative read of the active-generation doc with a short
    TTL cache.  Used by ``/api/picks/today`` to cross-check pod-local
    state against the shared source of truth without a DB hit on every
    request.  Returns a dict snapshot (never None); refreshes the
    module-level ``_active`` as a side effect so the next header-only
    read reflects the authoritative value.
    """
    now = time.time()
    cached = _load_active_cache.get("doc")
    if cached is not None and (now - float(_load_active_cache.get("ts") or 0)) < ttl_s:
        return dict(cached)
    try:
        doc = await _db()[COLL].find_one({"_id": ACTIVE_ID}, {"_id": 0})
    except Exception as exc:
        logger.debug("load_active_cached DB read failed: %s", exc)
        doc = None
    if doc:
        snap = {k: doc.get(k) for k in ("generation_id", "board_version",
                                        "committed_at", "revision",
                                        "pick_count", "event_count")}
        snap["state"] = "COMMITTED"
        _load_active_cache["doc"] = snap
        _load_active_cache["ts"] = now
        # Mirror into the module-level cache so middleware + downstream
        # readers stay in sync without an extra round-trip.
        _active.update({k: snap.get(k) for k in ("generation_id", "board_version",
                                                 "committed_at", "revision")})
        _active["state"] = "COMMITTED"
        return dict(snap)
    # No doc yet: fall through to the local ``_active`` so the first-
    # ever commit path is unaffected.
    return active()


def _invalidate_load_active_cache() -> None:
    """Reset the short-TTL cache so the next read goes straight to DB.
    Called from ``commit()`` on a successful advance so sibling pods
    hitting their next ``load_active_cached()`` after an 8 s middleware
    tick skip the stale cache and converge immediately."""
    _load_active_cache["ts"] = 0.0
    _load_active_cache["doc"] = None


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
    the identical population is a no-op commit (A stays, no churn).

    2026-06 P0 CROSS-POD AUTHORITY FIX — see module docstring.
    The CAS now:
      * Re-reads the shared DB active-generation document FRESH at
        the top of commit — the expected prior is the DB value, not
        the pod-local snapshot (which can drift by minutes on a pod
        receiving no ``/api/picks`` traffic).
      * Includes ``revision`` in the filter for strictly monotonic
        advances (no two pods can clobber each other).
      * Verifies the "doc missing" branch actually inserted the doc
        via ``upserted_id`` — if a sibling pod won the insert race,
        we treat it as a legitimate CAS loss, adopt the DB value,
        and RETRY once with the fresh expected so a legitimately
        newer build isn't silently dropped.
      * Belt-and-suspenders empty-board check so maintenance scripts
        that skip ``validate()`` cannot promote a zero-pick generation.
      * Emits a high-signal anomaly log whenever the pod-local
        ``_active`` disagreed with the DB value we just adopted —
        operators grep for ``POD_DB_GEN_MISMATCH`` to spot drift.
    """
    # ── Belt-and-suspenders empty-board guard.  ``validate()`` already
    # refuses to commit an empty full-board build, but maintenance
    # scripts (``scripts/maintenance/cfb_signfix_v3_rescore.py`` et al.)
    # call ``commit()`` without going through ``validate()``.  Any
    # commit path must honour the "never promote an empty generation"
    # contract.
    scope = (_building.get(gen_id) or {}).get("scope") or "ALL"
    if scope == "ALL" and (pick_count is None or int(pick_count) <= 0):
        logger.warning("board generation %s REFUSED: empty full-board commit "
                       "(scope=%s pick_count=%s) — authority pointer unchanged.",
                       gen_id, scope, pick_count)
        await _set_state(gen_id, "FAILED", failed_at=datetime.now(timezone.utc).isoformat(),
                         reason="empty-board commit refused by belt-and-suspenders")
        _building.pop(gen_id, None)
        return False

    if board_version is None:
        board_version, pick_count, event_count = await board_state()
        # Re-run the empty-board guard after lazy board_state resolve.
        if scope == "ALL" and (pick_count is None or int(pick_count) <= 0):
            logger.warning("board generation %s REFUSED after lazy resolve: empty full-board",
                           gen_id)
            await _set_state(gen_id, "FAILED",
                             failed_at=datetime.now(timezone.utc).isoformat(),
                             reason="empty-board commit refused after board_state resolve")
            _building.pop(gen_id, None)
            return False

    coll = _db()[COLL]

    # ── CROSS-POD AUTHORITY: re-read the DB active doc FRESH.  Never
    # trust the pod-local ``_active`` for the CAS expected value.
    try:
        fresh_db = await coll.find_one({"_id": ACTIVE_ID})
    except Exception as exc:
        logger.warning("commit: DB fresh-read failed, aborting commit: %s", exc)
        _building.pop(gen_id, None)
        await _set_state(gen_id, "FAILED", failed_at=datetime.now(timezone.utc).isoformat(),
                         reason=f"fresh db read error: {str(exc)[:200]}")
        return False

    db_gen       = (fresh_db or {}).get("generation_id")
    db_revision  = int((fresh_db or {}).get("revision") or 0)
    db_board_ver = (fresh_db or {}).get("board_version")

    # Loud anomaly if pod-local drifted away from the DB.  This fires
    # exactly when "pods are committing newer generations in memory
    # while DB is stuck".
    local_gen = _active.get("generation_id")
    local_rev = int(_active.get("revision") or 0)
    if db_gen != local_gen or db_revision != local_rev:
        logger.error(
            "POD_DB_GEN_MISMATCH pod=%s local_gen=%s local_rev=%s "
            "db_gen=%s db_rev=%s — reconciling to DB authority.",
            POD_ID, local_gen, local_rev, db_gen, db_revision,
        )
        # Adopt DB truth into the local cache so NOOP short-circuit
        # below operates on authoritative state.
        _active.update({"generation_id": db_gen, "revision": db_revision,
                        "board_version": db_board_ver,
                        "committed_at": (fresh_db or {}).get("committed_at"),
                        "state": "COMMITTED"})

    # No-op short-circuit — safe now that _active reflects DB truth.
    if board_version is not None and board_version == db_board_ver:
        _building.pop(gen_id, None)
        await _set_state(gen_id, "COMMITTED_NOOP", board_version=board_version,
                         committed_at=datetime.now(timezone.utc).isoformat())
        return True

    now = datetime.now(timezone.utc).isoformat()

    # Attempt the CAS up to 2 times — on a first-attempt miss we may
    # have been racing a sibling pod whose newer gen we should now
    # layer on top of (our build is still a valid monotonic advance).
    for attempt in (1, 2):
        expected_gen = db_gen
        expected_rev = db_revision
        new_revision = expected_rev + 1
        try:
            # Strict monotonic CAS: filter must match BOTH generation_id
            # AND revision — a sibling pod advancing the pointer by any
            # amount makes our filter miss and forces a fresh adopt.
            if expected_gen is None:
                # First-ever commit path: use insert_one for strict
                # single-writer semantics.  A DuplicateKeyError (a
                # sibling pod inserted first) is treated as CAS miss.
                try:
                    await coll.insert_one({
                        "_id": ACTIVE_ID, "generation_id": gen_id,
                        "board_version": board_version, "committed_at": now,
                        "revision": new_revision, "pick_count": pick_count,
                        "event_count": event_count,
                        "backend_revision": BACKEND_REVISION,
                        "pod_id": POD_ID,
                    })
                    res_match = True
                except Exception as ins_exc:
                    # DuplicateKey or similar → sibling won; adopt fresh.
                    logger.info("commit insert_one lost race (%s); refreshing DB view", ins_exc)
                    res_match = False
            else:
                upd = await coll.update_one(
                    {"_id": ACTIVE_ID,
                     "generation_id": expected_gen,
                     "revision": expected_rev},
                    {"$set": {"generation_id": gen_id, "board_version": board_version,
                              "committed_at": now, "revision": new_revision,
                              "pick_count": pick_count, "event_count": event_count,
                              "backend_revision": BACKEND_REVISION,
                              "pod_id": POD_ID}},
                    upsert=False,
                )
                res_match = (getattr(upd, "modified_count", 0) == 1)
        except Exception as exc:
            logger.warning("commit CAS attempt %d persist failed: %s", attempt, exc)
            res_match = False

        if res_match:
            # WON.  Update local mirror + invalidate the short-TTL
            # cache so sibling pods converge on their next read.
            _active.update({"generation_id": gen_id, "board_version": board_version,
                            "committed_at": now, "revision": new_revision,
                            "state": "COMMITTED"})
            _invalidate_load_active_cache()
            _building.pop(gen_id, None)
            await _set_state(gen_id, "COMMITTED", committed_at=now,
                             board_version=board_version,
                             pick_count=pick_count, event_count=event_count,
                             revision=new_revision, pod_id=POD_ID)
            try:
                from services.board_snapshot_cache import clear_cache
                clear_cache()
            except Exception:
                pass
            logger.info("board generation %s COMMITTED pod=%s rev=%s picks=%s events=%s",
                        gen_id, POD_ID, new_revision, pick_count, event_count)
            return True

        # CAS miss — re-read DB truth and retry ONCE with the fresh
        # expected (unless we already retried).
        try:
            fresh_db = await coll.find_one({"_id": ACTIVE_ID})
        except Exception as exc:
            logger.warning("commit CAS-miss re-read failed: %s", exc)
            fresh_db = None
        new_db_gen = (fresh_db or {}).get("generation_id")
        new_db_rev = int((fresh_db or {}).get("revision") or 0)
        new_db_ver = (fresh_db or {}).get("board_version")

        # Adopt whatever is in DB regardless of outcome.
        _active.update({"generation_id": new_db_gen, "revision": new_db_rev,
                        "board_version": new_db_ver,
                        "committed_at": (fresh_db or {}).get("committed_at"),
                        "state": "COMMITTED"})
        _invalidate_load_active_cache()

        # If the DB has moved on to the SAME board_version we were
        # going to publish, treat as NOOP convergence — sibling
        # already published equivalent content.
        if new_db_ver is not None and new_db_ver == board_version:
            _building.pop(gen_id, None)
            await _set_state(gen_id, "COMMITTED_NOOP", board_version=board_version,
                             committed_at=now)
            logger.info("commit %s converged NOOP after race (board_version match)", gen_id)
            return True

        if attempt == 1:
            # Loop body will retry with fresh expected.
            db_gen = new_db_gen
            db_revision = new_db_rev
            db_board_ver = new_db_ver
            logger.info("commit %s CAS retry attempt=2 fresh_db_gen=%s rev=%s",
                        gen_id, new_db_gen, new_db_rev)
            continue

        # Final loss after one retry — mark SUPERSEDED cleanly.
        _building.pop(gen_id, None)
        await _set_state(gen_id, "SUPERSEDED")
        logger.info("board generation %s SUPERSEDED pod=%s (lost CAS to db_gen=%s rev=%s)",
                    gen_id, POD_ID, new_db_gen, new_db_rev)
        return False

    # Unreachable — the for-loop always returns.
    return False


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
            "pid": os.getpid(), "pod_id": POD_ID}


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
           "load_active_cached", "manifest", "authority_fingerprint", "building_ids",
           "POD_ID", "BACKEND_REVISION"]
