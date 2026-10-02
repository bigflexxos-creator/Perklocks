"""canonical_worker_lease — MongoDB-backed distributed lease that
elects EXACTLY ONE Production backend instance as the canonical
background mutation authority.

Why this exists
---------------
Production will soon run multiple backend replicas against a shared
MongoDB (Atlas).  Simply gating workers on
``BACKGROUND_WORKERS_ENABLED=true`` would allow every replica to run
its own ingestion / settlement / publication loops against the same
shared canonical collections.  That is unsafe.

The lease solves this at the database layer:

* a unique Mongo document with ``lease_name`` as the primary key
* atomic acquisition via ``find_one_and_update`` with upsert fallback
* lease owner is the only replica allowed to run the protected
  canonical background workers
* heartbeat renews the lease every ``heartbeat_s`` seconds
* if heartbeat/renewal fails or ownership is lost, the local
  ``is_owner`` flag flips to False and every protected task
  registered via :func:`register_protected_task` is cancelled

Preview is NOT eligible — the lease fails closed when any of
``DATA_AUTHORITY != "production"``, ``CANONICAL_WRITE_ENABLED=false``
or ``BACKGROUND_WORKERS_ENABLED=false`` is in effect.

The lease intentionally does NOT replace existing per-write guards
(idempotent upserts, canonical IDs, settlement idempotency, local
single-flight).  It is an additional cross-instance coordination
authority that sits on top of them.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Set

from pymongo.errors import DuplicateKeyError
from pymongo import ReturnDocument

from services.data_authority import (
    authority_mode,
    background_workers_enabled,
    canonical_write_enabled,
)

logger = logging.getLogger("lockscore.canonical_worker_lease")

LEASE_COLLECTION_NAME = "canonical_worker_leases"
GLOBAL_LEASE_NAME = "canonical_background_authority"

DEFAULT_TTL_SECONDS = 90
DEFAULT_HEARTBEAT_SECONDS = 30


# ─── Instance identity ────────────────────────────────────────────────
def _derive_instance_id() -> str:
    """Pick a stable per-process instance identity.

    Preference order (first non-empty wins):

    1. ``INSTANCE_ID`` env var (lets Kubernetes inject the pod name).
    2. ``HOSTNAME`` env var (default in most container runtimes).
    3. a fresh ``uuid4()`` string.

    A ``:<short-uuid>`` suffix is appended to case (1)/(2) so even if
    two replicas accidentally share the same hostname they still get
    distinct lease identities for the lifetime of each process.
    """
    seed = os.environ.get("INSTANCE_ID") or os.environ.get("HOSTNAME") or ""
    seed = seed.strip()
    tag = uuid.uuid4().hex[:8]
    if seed:
        return f"{seed}:{tag}"
    return f"instance:{uuid.uuid4().hex}"


_PROCESS_INSTANCE_ID: str = _derive_instance_id()


def process_instance_id() -> str:
    """The stable per-process instance identity used for lease rows."""
    return _PROCESS_INSTANCE_ID


# ─── Eligibility ──────────────────────────────────────────────────────
def is_eligible_for_canonical_lease() -> bool:
    """Return True only when this pod is allowed to attempt lease
    acquisition.  All three gates must be true at acquisition / renew
    time — the lease fails closed otherwise."""
    return (
        authority_mode() == "production"
        and canonical_write_enabled()
        and background_workers_enabled()
    )


# ─── Lease core ───────────────────────────────────────────────────────
def _utc_now() -> datetime:
    # Return a NAIVE UTC datetime so comparisons with values returned
    # from Mongo (which strips tzinfo by default under Motor) stay
    # consistent — the alternative is enabling ``tz_aware=True`` on
    # every AsyncIOMotorClient, which would be a global behavioural
    # change we are explicitly not making in this pass.
    return datetime.utcnow()


class CanonicalWorkerLease:
    """A single named Mongo lease.

    One lease name per logical authority — the default is
    :data:`GLOBAL_LEASE_NAME` which protects every registered
    canonical background worker.  Callers may build additional leases
    for specialised workers if ever needed, but the simplest safe
    architecture is a single global lease (see Perklocks spec §11).
    """

    def __init__(
        self,
        db,
        *,
        lease_name: str = GLOBAL_LEASE_NAME,
        instance_id: Optional[str] = None,
        ttl_s: int = DEFAULT_TTL_SECONDS,
        heartbeat_s: int = DEFAULT_HEARTBEAT_SECONDS,
        collection_name: str = LEASE_COLLECTION_NAME,
        eligibility_fn=is_eligible_for_canonical_lease,
    ) -> None:
        if heartbeat_s >= ttl_s:
            raise ValueError(
                "heartbeat_s must be strictly less than ttl_s "
                f"(got heartbeat={heartbeat_s}, ttl={ttl_s})"
            )
        self._db = db
        self._coll = db[collection_name]
        self.lease_name = lease_name
        self.instance_id = instance_id or process_instance_id()
        self.ttl_s = int(ttl_s)
        self.heartbeat_s = int(heartbeat_s)
        self._eligibility_fn = eligibility_fn

        self._is_owner_local: bool = False
        self._last_heartbeat_at: Optional[datetime] = None
        self._last_expires_at: Optional[datetime] = None

        self._heartbeat_task: Optional[asyncio.Task] = None
        self._stop_requested: asyncio.Event = asyncio.Event()
        self._protected_tasks: Set[asyncio.Task] = set()

    # ---- index --------------------------------------------------------
    async def ensure_index(self) -> None:
        """Create the unique index on ``lease_name``.  Idempotent."""
        try:
            await self._coll.create_index("lease_name", unique=True, name="lease_name_unique")
        except Exception as e:                                  # pragma: no cover
            logger.warning("lease: ensure_index failed: %s", e)

    # ---- state --------------------------------------------------------
    def owns_local(self) -> bool:
        """Return the cached local ownership flag (updated by acquire/
        renew/heartbeat).  Workers should prefer this flag because it
        is cheap, but the Mongo-authoritative :meth:`owns` is the
        source of truth when correctness matters."""
        return bool(self._is_owner_local)

    def last_heartbeat_at(self) -> Optional[datetime]:
        return self._last_heartbeat_at

    def last_expires_at(self) -> Optional[datetime]:
        return self._last_expires_at

    # ---- acquire ------------------------------------------------------
    async def acquire(self) -> bool:
        """Attempt to acquire (or renew) the lease atomically.

        Returns True iff this instance now owns a valid lease.  Fails
        closed when the pod is not eligible.
        """
        if not self._eligibility_fn():
            # Fail closed — never let an ineligible pod obtain the
            # canonical background authority.
            self._is_owner_local = False
            logger.debug(
                "lease.acquire refused: not eligible (mode=%s, canonical_write=%s, workers=%s)",
                authority_mode(), canonical_write_enabled(), background_workers_enabled(),
            )
            return False

        now = _utc_now()
        expires_at = now + timedelta(seconds=self.ttl_s)

        # Step 1: update-only path.  Match either (a) a lease row we
        # already own, or (b) a stale lease (expired at <= now).  This
        # is a single atomic Mongo operation.
        try:
            doc = await self._coll.find_one_and_update(
                {
                    "lease_name": self.lease_name,
                    "$or": [
                        {"owner_instance_id": self.instance_id},
                        {"expires_at": {"$lte": now}},
                    ],
                },
                {
                    "$set": {
                        "owner_instance_id": self.instance_id,
                        "acquired_at":       now,
                        "heartbeat_at":      now,
                        "expires_at":        expires_at,
                    },
                    "$inc": {"lease_version": 1},
                    "$setOnInsert": {"lease_name": self.lease_name},
                },
                return_document=ReturnDocument.AFTER,
            )
            if doc is not None:
                self._is_owner_local = True
                self._last_heartbeat_at = doc.get("heartbeat_at", now)
                self._last_expires_at   = doc.get("expires_at",  expires_at)
                return True
        except Exception as e:                                  # pragma: no cover
            logger.warning("lease.acquire step-1 raised: %s", e)
            self._is_owner_local = False
            return False

        # Step 2: no existing doc matched our acquisition filter.  Try
        # to insert a fresh lease row.  If another instance beat us to
        # it, Mongo's unique index raises DuplicateKeyError — we lost.
        try:
            await self._coll.insert_one({
                "lease_name":        self.lease_name,
                "owner_instance_id": self.instance_id,
                "acquired_at":       now,
                "heartbeat_at":      now,
                "expires_at":        expires_at,
                "lease_version":     1,
            })
            self._is_owner_local = True
            self._last_heartbeat_at = now
            self._last_expires_at   = expires_at
            return True
        except DuplicateKeyError:
            # Someone else owns a live lease.  Fail closed.
            self._is_owner_local = False
            return False
        except Exception as e:                                  # pragma: no cover
            logger.warning("lease.acquire step-2 raised: %s", e)
            self._is_owner_local = False
            return False

    # ---- renew --------------------------------------------------------
    async def renew(self) -> bool:
        """Extend the lease.  Only the current owner may renew, and
        only while the lease has not yet expired.  Returns True iff
        renewal succeeded; on failure the local ownership flag flips
        to False immediately."""
        if not self._eligibility_fn():
            self._is_owner_local = False
            return False

        now = _utc_now()
        expires_at = now + timedelta(seconds=self.ttl_s)
        try:
            doc = await self._coll.find_one_and_update(
                {
                    "lease_name":        self.lease_name,
                    "owner_instance_id": self.instance_id,
                    "expires_at":        {"$gt": now},
                },
                {
                    "$set": {
                        "heartbeat_at": now,
                        "expires_at":   expires_at,
                    },
                },
                return_document=ReturnDocument.AFTER,
            )
        except Exception as e:                                  # pragma: no cover
            logger.warning("lease.renew raised: %s", e)
            self._is_owner_local = False
            return False

        if doc is None:
            # Ownership lost — somebody else holds the lease, or TTL
            # expired before we got here.  Flip ownership + stop
            # protected tasks (handled by supervisor loop).
            self._is_owner_local = False
            return False

        self._is_owner_local = True
        self._last_heartbeat_at = doc.get("heartbeat_at", now)
        self._last_expires_at   = doc.get("expires_at",  expires_at)
        return True

    # ---- Mongo-authoritative ownership check --------------------------
    async def owns(self) -> bool:
        """Return True iff Mongo currently records this instance as
        the owner AND the lease has not expired.  This is the
        strictly correct, non-cached check."""
        now = _utc_now()
        try:
            doc = await self._coll.find_one({
                "lease_name":        self.lease_name,
                "owner_instance_id": self.instance_id,
                "expires_at":        {"$gt": now},
            })
        except Exception as e:                                  # pragma: no cover
            logger.warning("lease.owns raised: %s", e)
            return False
        is_owner = doc is not None
        self._is_owner_local = is_owner
        if is_owner:
            self._last_heartbeat_at = doc.get("heartbeat_at")
            self._last_expires_at   = doc.get("expires_at")
        return is_owner

    # ---- release ------------------------------------------------------
    async def release(self) -> None:
        """Graceful release — remove the lease row iff we still own
        it.  TTL expiration remains the ultimate crash-recovery
        authority so correctness never depends on this call."""
        try:
            await self._coll.delete_one({
                "lease_name":        self.lease_name,
                "owner_instance_id": self.instance_id,
            })
        except Exception as e:                                  # pragma: no cover
            logger.warning("lease.release raised: %s", e)
        self._is_owner_local = False

    # ---- protected task tracking --------------------------------------
    def register_protected_task(self, task: asyncio.Task) -> None:
        """Track a task started under the lease so the supervisor can
        cancel it the instant ownership is lost."""
        if task is None:
            return
        self._protected_tasks.add(task)
        task.add_done_callback(self._protected_tasks.discard)

    def protected_task_count(self) -> int:
        return len(self._protected_tasks)

    async def _cancel_protected_tasks(self, reason: str) -> None:
        if not self._protected_tasks:
            return
        logger.warning(
            "lease: cancelling %d protected canonical task(s) — reason=%s",
            len(self._protected_tasks), reason,
        )
        for t in list(self._protected_tasks):
            if not t.done():
                t.cancel()
        # Let cancellation propagate.
        for t in list(self._protected_tasks):
            try:
                await asyncio.wait_for(t, timeout=5.0)
            except Exception:
                pass
        self._protected_tasks.clear()

    # ---- supervisor loop ----------------------------------------------
    async def _supervisor_loop(self) -> None:
        """Periodic heartbeat.  Renews the lease every
        ``heartbeat_s`` seconds; on any renewal failure flips local
        ownership False and cancels every protected task so canonical
        background mutation stops *immediately*."""
        logger.info(
            "lease.supervisor started (lease=%s instance=%s ttl=%ss heartbeat=%ss)",
            self.lease_name, self.instance_id, self.ttl_s, self.heartbeat_s,
        )
        try:
            while not self._stop_requested.is_set():
                try:
                    await asyncio.wait_for(
                        self._stop_requested.wait(),
                        timeout=self.heartbeat_s,
                    )
                    # Stop was requested — leave the loop.
                    break
                except asyncio.TimeoutError:
                    pass  # heartbeat interval elapsed — proceed to renew

                ok = await self.renew()
                if not ok:
                    logger.warning(
                        "lease.supervisor: renewal FAILED for %s (instance=%s) — "
                        "stopping protected canonical workers",
                        self.lease_name, self.instance_id,
                    )
                    await self._cancel_protected_tasks(reason="lease_lost")
                    # Keep looping — another acquire attempt may succeed
                    # later (failover without restart).  But do not
                    # restart previously-protected tasks here; that is
                    # the application's responsibility.
                else:
                    logger.debug(
                        "lease.supervisor: renewed %s (expires=%s)",
                        self.lease_name, self._last_expires_at,
                    )
        except asyncio.CancelledError:
            pass
        finally:
            logger.info("lease.supervisor stopped (lease=%s)", self.lease_name)

    def start_supervisor(self) -> Optional[asyncio.Task]:
        """Start the background heartbeat loop (idempotent).  Returns
        the task handle, or None if a supervisor is already running."""
        if self._heartbeat_task is not None and not self._heartbeat_task.done():
            return None
        self._stop_requested.clear()
        self._heartbeat_task = asyncio.create_task(
            self._supervisor_loop(),
            name=f"lease_supervisor:{self.lease_name}",
        )
        return self._heartbeat_task

    async def stop_supervisor(self) -> None:
        """Signal the heartbeat loop to stop and wait briefly for it
        to drain.  Does NOT release the lease — call :meth:`release`
        separately for graceful shutdown."""
        self._stop_requested.set()
        t = self._heartbeat_task
        self._heartbeat_task = None
        if t is not None and not t.done():
            try:
                await asyncio.wait_for(t, timeout=5.0)
            except Exception:
                pass

    # ---- safe diagnostics ---------------------------------------------
    async def safe_snapshot(self) -> dict[str, Any]:
        """Diagnostics suitable for admin surfaces.  Never includes
        Mongo credentials.  Reads the live lease row to show the
        actual current owner (which may be a different instance)."""
        now = _utc_now()
        doc: dict[str, Any] = {}
        try:
            doc = await self._coll.find_one({"lease_name": self.lease_name}) or {}
        except Exception:
            doc = {}
        current_owner = doc.get("owner_instance_id")
        expires_at    = doc.get("expires_at")
        heartbeat_at  = doc.get("heartbeat_at")
        is_live = bool(expires_at and expires_at > now)
        this_instance_owns = bool(
            is_live and current_owner == self.instance_id
        )
        # Keep the local flag honest with the DB.
        self._is_owner_local = this_instance_owns
        return {
            "worker_lease_name":        self.lease_name,
            "worker_lease_required":    True,
            "worker_lease_owner":       current_owner if is_live else None,
            "this_instance_owns_lease": this_instance_owns,
            "lease_heartbeat_at":       heartbeat_at.isoformat() if heartbeat_at else None,
            "lease_expires_at":         expires_at.isoformat()   if expires_at   else None,
            "lease_ttl_seconds":        self.ttl_s,
            "lease_heartbeat_seconds":  self.heartbeat_s,
            "instance_id":              self.instance_id,
        }


# ─── Process-wide singleton ───────────────────────────────────────────
_SINGLETON: Optional[CanonicalWorkerLease] = None


def get_lease_authority(db=None) -> CanonicalWorkerLease:
    """Return the process singleton :class:`CanonicalWorkerLease`.

    The first caller that passes a non-None ``db`` wins; subsequent
    callers get the same instance.  Tests can bypass this entirely by
    instantiating :class:`CanonicalWorkerLease` directly against a
    scratch collection.
    """
    global _SINGLETON
    if _SINGLETON is None:
        if db is None:
            from services.database import get_database
            db = get_database()
        _SINGLETON = CanonicalWorkerLease(db)
    return _SINGLETON


def reset_lease_authority_for_testing() -> None:
    """Drop the singleton so tests can re-build with a fresh db."""
    global _SINGLETON
    _SINGLETON = None


# ─── Admin surface ────────────────────────────────────────────────────
async def status() -> dict[str, Any]:
    """Return the safe lease status for the ``/admin/data-authority/
    status`` endpoint.  When the pod is not eligible to run the
    canonical background authority (Preview), the response still
    includes ``worker_lease_required``/``this_instance_owns_lease``
    so operators can confirm Preview is dormant."""
    base = {
        "worker_lease_name":        GLOBAL_LEASE_NAME,
        "worker_lease_required":    True,
        "lease_backend":            "mongodb",
        "lease_collection":         LEASE_COLLECTION_NAME,
        "instance_id":              process_instance_id(),
        "owner_instance_id":        process_instance_id(),
        "eligible_for_lease":       is_eligible_for_canonical_lease(),
        "active_leases":            [],
        "this_instance_owned_leases": [],
    }
    try:
        from services.database import get_database, is_initialized
        if not is_initialized():
            base.update({
                "worker_lease_owner":       None,
                "this_instance_owns_lease": False,
                "lease_heartbeat_at":       None,
                "lease_expires_at":         None,
            })
            return base
        db = get_database()
        lease = get_lease_authority(db)
        snap = await lease.safe_snapshot()
        base.update(snap)
        # Enumerate every live, non-expired lease row in the shared
        # authority collection — not just this instance's.  Multiple
        # leases per responsibility are supported (see spec §11); the
        # Perklocks default is the single ``canonical_background_
        # authority`` lease but we don't want the status endpoint to
        # lie if additional leases ever appear.
        import datetime as _dt
        now = _dt.datetime.utcnow()
        try:
            cur = db[LEASE_COLLECTION_NAME].find(
                {"expires_at": {"$gt": now}},
                {"lease_name": 1, "owner_instance_id": 1,
                 "heartbeat_at": 1, "expires_at": 1, "lease_version": 1,
                 "_id": 0},
            )
            rows = await cur.to_list(length=50)
            active = []
            owned = []
            for r in rows:
                safe_row = {
                    "lease_name":        r.get("lease_name"),
                    "owner_instance_id": r.get("owner_instance_id"),
                    "lease_version":     r.get("lease_version"),
                    "heartbeat_at":      r["heartbeat_at"].isoformat()
                        if isinstance(r.get("heartbeat_at"), _dt.datetime) else None,
                    "expires_at":        r["expires_at"].isoformat()
                        if isinstance(r.get("expires_at"), _dt.datetime) else None,
                }
                active.append(safe_row)
                if r.get("owner_instance_id") == process_instance_id():
                    owned.append(safe_row["lease_name"])
            base["active_leases"] = active
            base["this_instance_owned_leases"] = owned
        except Exception as _le:
            base["active_leases"] = []
            base["this_instance_owned_leases"] = []
            base["active_leases_error"] = str(_le)[:160]
        return base
    except Exception as e:                                      # pragma: no cover
        logger.warning("lease.status raised: %s", e)
        base.update({
            "worker_lease_owner":       None,
            "this_instance_owns_lease": False,
            "lease_heartbeat_at":       None,
            "lease_expires_at":         None,
            "lease_status_error":       str(e)[:160],
        })
        return base
