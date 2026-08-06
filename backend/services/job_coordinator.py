"""JobCoordinator — Phase 2β distributed job coordination.

Single-owner atomic lease over a durable ``scheduled_jobs`` Mongo
collection.  Guarantees:

  • Exactly one process/container can own a named job at a time.
  • Rolling deployments never duplicate work — old + new revisions
    see each other via the shared collection.
  • Crashed leases expire on ``lease_until`` and become recoverable.
  • Every attempt is auditable via ``job_execution_log`` (30-day TTL
    on successful runs) and ``job_audit_log`` (180-day TTL — records
    every non-benign event: failures, lease theft attempts, denied
    budget requests, emergency-reserve usage).

Safety design (standalone MongoDB — no multi-doc transactions)
────────────────────────────────────────────────────────────
Atomicity comes from ``find_one_and_update`` with a filter that
matches EITHER "lease already expired" OR "no current owner". A
single-document update is atomic even without transactions.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import socket
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger("lockscore.job_coordinator")

# ── Collections ──────────────────────────────────────────────────────
COLLECTION           = "scheduled_jobs"
EXECUTION_LOG        = "job_execution_log"
AUDIT_LOG            = "job_audit_log"

# ── Status vocabulary ────────────────────────────────────────────────
STATUS_IDLE      = "idle"
STATUS_QUEUED    = "queued"
STATUS_RUNNING   = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED    = "failed"
STATUS_BLOCKED   = "blocked"
STATUS_EXPIRED   = "expired"

VALID_STATUSES = {
    STATUS_IDLE, STATUS_QUEUED, STATUS_RUNNING,
    STATUS_COMPLETED, STATUS_FAILED, STATUS_BLOCKED, STATUS_EXPIRED,
}

# ── Acquire result reasons (returned in the AcquireResult dict) ──────
ACQUIRE_OK               = "acquired"
ACQUIRE_BUSY             = "busy"
ACQUIRE_BLOCKED_INTERVAL = "blocked_min_interval"
ACQUIRE_BLOCKED_STATUS   = "blocked_status"

# ── TTLs ─────────────────────────────────────────────────────────────
EXECUTION_LOG_TTL_DAYS   = 30
AUDIT_LOG_TTL_DAYS       = 180

_INSTANCE_STAMP: Optional[str] = None
_REVISION_STAMP: Optional[dict[str, Any]] = None

# ── Metadata sanitizer ───────────────────────────────────────────────
_SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|token|secret|password|authorization|bearer|apikey)",
    re.IGNORECASE,
)
_MAX_STRING_LEN   = 2000
_MAX_ERROR_LEN    = 2000
_MAX_METADATA_KEYS = 40


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _revision_info() -> dict[str, Any]:
    """Return the deployment metadata dictionary for the current process.

    Cached at first call.  Keys populated when the corresponding env
    var exists so operators can query by revision without parsing
    ``owner_instance``.
    """
    global _REVISION_STAMP
    if _REVISION_STAMP is not None:
        return _REVISION_STAMP
    info: dict[str, Any] = {
        "hostname": socket.gethostname(),
        "pid": os.getpid(),
    }
    for env in ("GIT_SHA", "GIT_SHA_SHORT", "GIT_COMMIT", "SOURCE_VERSION",
                "DEPLOYMENT_ID", "K_REVISION", "HOSTNAME"):
        v = os.environ.get(env)
        if v:
            info[env.lower()] = v
    _REVISION_STAMP = info
    return info


def _instance_id() -> str:
    """Deterministic per-process identifier used for ``owner_instance``.

    Format: ``${HOSTNAME}:${PID}:${GIT_SHA_SHORT}``
    Fallback: ``${HOSTNAME}:${PID}``
    """
    global _INSTANCE_STAMP
    if _INSTANCE_STAMP is not None:
        return _INSTANCE_STAMP
    host = socket.gethostname()
    pid = os.getpid()
    sha = (
        os.environ.get("GIT_SHA_SHORT")
        or os.environ.get("GIT_SHA")
        or os.environ.get("SOURCE_VERSION")
        or ""
    )
    sha = sha[:12] if sha else ""
    _INSTANCE_STAMP = f"{host}:{pid}:{sha}" if sha else f"{host}:{pid}"
    return _INSTANCE_STAMP


def _hash_token(token: str) -> str:
    """Return SHA-256 hash of a lease token for safe storage in logs."""
    if not token:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _sanitize_error(error: Any) -> str:
    s = str(error) if error is not None else ""
    # crude secret redaction — anything that looks like an API key
    s = re.sub(
        r"([A-Za-z0-9_-]{20,})",
        lambda m: m.group(1)[:6] + "…redacted",
        s,
    )
    return s[:_MAX_ERROR_LEN]


def _sanitize_metadata(md: Any, *, depth: int = 0) -> Any:
    """Strip obvious secrets from caller-provided metadata."""
    if md is None:
        return None
    if depth > 4:
        return "…truncated"
    if isinstance(md, dict):
        out: dict[str, Any] = {}
        for i, (k, v) in enumerate(md.items()):
            if i >= _MAX_METADATA_KEYS:
                out["…truncated"] = True
                break
            key = str(k)[:120]
            if _SECRET_KEY_RE.search(key):
                out[key] = "***"
                continue
            out[key] = _sanitize_metadata(v, depth=depth + 1)
        return out
    if isinstance(md, (list, tuple)):
        return [_sanitize_metadata(x, depth=depth + 1) for x in list(md)[:50]]
    if isinstance(md, (str, bytes)):
        s = md.decode("utf-8", "replace") if isinstance(md, bytes) else md
        return s[:_MAX_STRING_LEN]
    if isinstance(md, (int, float, bool)) or md is None:
        return md
    return str(md)[:_MAX_STRING_LEN]


class AcquireResult(dict):
    """Simple dict-typed return value from ``acquire``. Truthy when
    the caller obtained the lease."""

    def __bool__(self) -> bool:  # type: ignore[override]
        return self.get("acquired") is True

    @property
    def lease_token(self) -> Optional[str]:
        return self.get("lease_token")


class JobCoordinator:
    """See module docstring."""

    def __init__(self, db: AsyncIOMotorDatabase, *,
                  default_lease_seconds: int = 300) -> None:
        self.db = db
        self.default_lease_seconds = default_lease_seconds

    # ─────────────────────────────────────────────────────────
    # Bootstrap
    # ─────────────────────────────────────────────────────────
    async def ensure_indices(self) -> None:
        """Phase 3C — delegate to the central index registry.  Kept as
        a compatibility wrapper so existing callers (server startup)
        continue to work unchanged."""
        try:
            from services import index_registry as _ir
            await _ir.ensure_collection(self.db, COLLECTION)
            await _ir.ensure_collection(self.db, EXECUTION_LOG)
            await _ir.ensure_collection(self.db, AUDIT_LOG)
        except Exception as e:  # pragma: no cover
            logger.debug("job_coordinator ensure_indices via registry: %s", e)

    # ─────────────────────────────────────────────────────────
    # Lease API
    # ─────────────────────────────────────────────────────────
    async def acquire(
        self, job_name: str, *,
        owner_instance: Optional[str] = None,
        lease_seconds: Optional[int] = None,
        min_interval_seconds: Optional[int] = None,
        caller: str = "unknown",
        reason: str = "",
        metadata: Optional[dict] = None,
    ) -> AcquireResult:
        """Attempt to acquire the lease for ``job_name``.

        Returns an ``AcquireResult`` — truthy on success.  On failure
        ``.get("reason")`` explains why (``busy``, ``blocked_min_interval``,
        ``blocked_status``).

        ``min_interval_seconds`` enforces ``next_eligible_at`` — if a
        prior successful run set ``next_eligible_at`` in the future,
        this call is rejected and the field stays intact.
        """
        now = _now()
        lease_secs = int(lease_seconds or self.default_lease_seconds)
        lease_until = now + timedelta(seconds=lease_secs)
        token = uuid.uuid4().hex
        owner = owner_instance or _instance_id()
        clean_md = _sanitize_metadata(metadata)

        # 1) Bootstrap the doc if missing so the following atomic
        #    filter has stable ground truth. Upsert is idempotent.
        await self.db[COLLECTION].update_one(
            {"job_name": job_name},
            {"$setOnInsert": {
                "job_name":       job_name,
                "status":         STATUS_IDLE,
                "created_at":     now,
                "updated_at":     now,
                "run_count":      0,
                "success_count":  0,
                "failure_count":  0,
            }},
            upsert=True,
        )

        # 2) Peek — if next_eligible_at is in the future, block.
        cur = await self.db[COLLECTION].find_one(
            {"job_name": job_name},
            {"next_eligible_at": 1, "status": 1,
             "lease_until": 1, "owner_instance": 1},
        )
        if cur:
            ne = cur.get("next_eligible_at")
            if isinstance(ne, datetime):
                if ne.tzinfo is None:
                    ne = ne.replace(tzinfo=timezone.utc)
                if ne > now:
                    return AcquireResult(
                        acquired=False, reason=ACQUIRE_BLOCKED_INTERVAL,
                        next_eligible_at=ne,
                    )
            # If currently running AND lease not yet expired, busy.
            if cur.get("status") == STATUS_RUNNING:
                lu = cur.get("lease_until")
                if isinstance(lu, datetime):
                    if lu.tzinfo is None:
                        lu = lu.replace(tzinfo=timezone.utc)
                    if lu > now:
                        return AcquireResult(
                            acquired=False, reason=ACQUIRE_BUSY,
                            current_owner=cur.get("owner_instance"),
                            lease_until=lu,
                        )

        # 3) Atomic take. Filter matches "not currently running" OR
        #    "lease expired".  find_one_and_update is atomic per doc.
        filter_ = {
            "job_name": job_name,
            "$or": [
                {"status": {"$ne": STATUS_RUNNING}},
                {"lease_until": {"$lt": now}},
            ],
        }
        update = {
            "$set": {
                "owner_instance":    owner,
                "status":            STATUS_RUNNING,
                "lease_token":       token,
                "lease_acquired_at": now,
                "lease_until":       lease_until,
                "last_started_at":   now,
                "caller":            str(caller)[:120],
                "reason":            str(reason)[:_MAX_STRING_LEN],
                "metadata":          clean_md or {},
                "revision":          _revision_info(),
                "updated_at":        now,
                # Preserve min-interval hint for later completion.
                "min_interval_seconds": (
                    int(min_interval_seconds) if min_interval_seconds else None
                ),
            },
            "$inc": {"run_count": 1},
        }
        doc = await self.db[COLLECTION].find_one_and_update(
            filter_, update, return_document=True,
        )
        if not doc or doc.get("lease_token") != token:
            return AcquireResult(acquired=False, reason=ACQUIRE_BUSY)

        # Log execution start — long-lived (no ttl_at) until complete
        # or fail records it.  The complete/fail hooks set ttl_at.
        exec_id = uuid.uuid4().hex
        try:
            await self.db[EXECUTION_LOG].insert_one({
                "execution_id":     exec_id,
                "job_name":         job_name,
                "owner_instance":   owner,
                "caller":           str(caller)[:120],
                "reason":           str(reason)[:_MAX_STRING_LEN],
                "status":           STATUS_RUNNING,
                "started_at":       now,
                "lease_token_hash": _hash_token(token),
                "revision":         _revision_info(),
                "metadata":         clean_md or {},
            })
        except Exception as e:  # pragma: no cover
            logger.warning("execution_log start write failed: %s", e)

        return AcquireResult(
            acquired=True, reason=ACQUIRE_OK,
            lease_token=token, execution_id=exec_id,
            lease_until=lease_until, owner_instance=owner,
        )

    async def heartbeat(self, job_name: str, lease_token: str, *,
                         extend_seconds: int = 60) -> bool:
        """Extend an active lease.  Returns True only if the caller
        still owns the lease.  Non-owner calls audit and return False."""
        if not lease_token:
            await self._audit(
                "heartbeat_missing_token", job_name=job_name,
                severity="warn",
            )
            return False
        now = _now()
        new_until = now + timedelta(seconds=extend_seconds)
        res = await self.db[COLLECTION].update_one(
            {"job_name": job_name, "lease_token": lease_token,
             "status": STATUS_RUNNING},
            {"$set": {"lease_until": new_until, "updated_at": now}},
        )
        if not res.modified_count:
            await self._audit(
                "heartbeat_denied", job_name=job_name,
                severity="warn", token_hash=_hash_token(lease_token),
            )
            return False
        return True

    async def complete(self, job_name: str, lease_token: str, *,
                        result_metadata: Optional[dict] = None,
                        next_eligible_at: Optional[datetime] = None,
                        ) -> bool:
        if not lease_token:
            return False
        now = _now()
        clean_md = _sanitize_metadata(result_metadata) or {}
        set_ = {
            "status":            STATUS_COMPLETED,
            "last_completed_at": now,
            "lease_until":       now,       # release ownership
            "result_metadata":   clean_md,
            "updated_at":        now,
        }
        # Enforce min-interval if the acquire supplied it and caller
        # didn't override.
        if next_eligible_at is None:
            existing = await self.db[COLLECTION].find_one(
                {"job_name": job_name, "lease_token": lease_token},
                {"min_interval_seconds": 1},
            )
            mis = (existing or {}).get("min_interval_seconds")
            if mis:
                next_eligible_at = now + timedelta(seconds=int(mis))
        if next_eligible_at is not None:
            if next_eligible_at.tzinfo is None:
                next_eligible_at = next_eligible_at.replace(
                    tzinfo=timezone.utc)
            set_["next_eligible_at"] = next_eligible_at

        res = await self.db[COLLECTION].update_one(
            {"job_name": job_name, "lease_token": lease_token},
            {"$set": set_, "$inc": {"success_count": 1}},
        )
        if not res.modified_count:
            await self._audit(
                "complete_denied", job_name=job_name,
                severity="warn", token_hash=_hash_token(lease_token),
            )
            return False
        # Close the execution log row and mark it for TTL.
        try:
            await self.db[EXECUTION_LOG].update_one(
                {
                    "job_name": job_name,
                    "lease_token_hash": _hash_token(lease_token),
                    "status": STATUS_RUNNING,
                },
                {"$set": {
                    "status":       STATUS_COMPLETED,
                    "completed_at": now,
                    "duration_ms":  None,   # computed below via aggregate if needed
                    "result_metadata": clean_md,
                    "ttl_at":       now + timedelta(days=EXECUTION_LOG_TTL_DAYS),
                }},
            )
        except Exception as e:  # pragma: no cover
            logger.warning("execution_log complete write failed: %s", e)
        return True

    async def fail(self, job_name: str, lease_token: str, *,
                    error: str, retry_after_seconds: Optional[int] = None,
                    ) -> bool:
        if not lease_token:
            return False
        now = _now()
        set_ = {
            "status":       STATUS_FAILED,
            "last_failed_at": now,
            "last_error":   _sanitize_error(error),
            "lease_until":  now,        # release ownership
            "updated_at":   now,
        }
        if retry_after_seconds is not None:
            set_["next_eligible_at"] = now + timedelta(
                seconds=int(retry_after_seconds))
        res = await self.db[COLLECTION].update_one(
            {"job_name": job_name, "lease_token": lease_token},
            {"$set": set_, "$inc": {"failure_count": 1}},
        )
        if not res.modified_count:
            await self._audit(
                "fail_denied", job_name=job_name,
                severity="warn", token_hash=_hash_token(lease_token),
            )
            return False
        # Retain failed executions FOREVER (no ttl_at) — spec §retention.
        try:
            await self.db[EXECUTION_LOG].update_one(
                {
                    "job_name": job_name,
                    "lease_token_hash": _hash_token(lease_token),
                    "status": STATUS_RUNNING,
                },
                {"$set": {
                    "status":         STATUS_FAILED,
                    "completed_at":   now,
                    "error_summary":  _sanitize_error(error),
                    # NOTE: no ttl_at → failed rows are retained.
                }},
            )
        except Exception as e:  # pragma: no cover
            logger.warning("execution_log fail write failed: %s", e)
        # Duplicate to audit log for ease of forensic queries.
        await self._audit(
            "job_failed", job_name=job_name,
            severity="warn", error=_sanitize_error(error),
            token_hash=_hash_token(lease_token),
        )
        return True

    async def release(self, job_name: str, lease_token: str) -> bool:
        if not lease_token:
            return False
        now = _now()
        res = await self.db[COLLECTION].update_one(
            {"job_name": job_name, "lease_token": lease_token},
            {"$set": {"status": STATUS_IDLE, "lease_until": now,
                       "updated_at": now}},
        )
        if not res.modified_count:
            await self._audit(
                "release_denied", job_name=job_name,
                severity="warn", token_hash=_hash_token(lease_token),
            )
            return False
        try:
            await self.db[EXECUTION_LOG].update_one(
                {
                    "job_name": job_name,
                    "lease_token_hash": _hash_token(lease_token),
                    "status": STATUS_RUNNING,
                },
                {"$set": {
                    "status":       STATUS_IDLE,
                    "completed_at": now,
                    "ttl_at":       now + timedelta(days=EXECUTION_LOG_TTL_DAYS),
                }},
            )
        except Exception:  # pragma: no cover
            pass
        return True

    async def recover_expired_leases(self) -> int:
        """Mark leases whose deadline has passed as expired so the
        next ``acquire()`` can safely reclaim them.  Idempotent."""
        now = _now()
        res = await self.db[COLLECTION].update_many(
            {"status": STATUS_RUNNING, "lease_until": {"$lt": now}},
            {"$set": {"status": STATUS_EXPIRED, "updated_at": now}},
        )
        n = int(res.modified_count)
        if n:
            await self._audit(
                "leases_expired", severity="info", count=n,
            )
        return n

    # Alias kept for backward-compat with prior module version.
    sweep_expired = recover_expired_leases

    async def get_status(self, job_name: str) -> Optional[dict]:
        return await self.db[COLLECTION].find_one(
            {"job_name": job_name}, {"_id": 0})

    async def list_statuses(
        self, *, limit: int = 500,
        job_names: Optional[Iterable[str]] = None,
    ) -> list[dict]:
        q: dict[str, Any] = {}
        if job_names:
            q["job_name"] = {"$in": list(job_names)}
        return await (
            self.db[COLLECTION]
                .find(q, {"_id": 0})
                .sort("updated_at", -1)
                .to_list(limit)
        )

    async def recent_executions(self, *, limit: int = 100,
                                 job_name: Optional[str] = None,
                                 ) -> list[dict]:
        q: dict[str, Any] = {}
        if job_name:
            q["job_name"] = job_name
        return await (
            self.db[EXECUTION_LOG]
                .find(q, {"_id": 0})
                .sort("started_at", -1)
                .to_list(limit)
        )

    # ─────────────────────────────────────────────────────────
    # Audit helpers
    # ─────────────────────────────────────────────────────────
    async def _audit(self, event_type: str, **fields: Any) -> None:
        now = _now()
        rec: dict[str, Any] = {
            "event_type": event_type,
            "created_at": now,
            "ttl_at":     now + timedelta(days=AUDIT_LOG_TTL_DAYS),
            "revision":   _revision_info(),
        }
        for k, v in fields.items():
            if k in {"metadata", "result_metadata", "params"}:
                rec[k] = _sanitize_metadata(v)
            else:
                rec[k] = _sanitize_metadata(v) if isinstance(v, dict) else v
        try:
            await self.db[AUDIT_LOG].insert_one(rec)
        except Exception as e:  # pragma: no cover
            logger.warning("audit write failed (%s): %s", event_type, e)

    async def audit(self, event_type: str, **fields: Any) -> None:
        """Public entry-point for external services (ProviderBudget,
        force-refresh guard, shadow-mode) to write to the same audit
        stream."""
        await self._audit(event_type, **fields)


__all__ = [
    "JobCoordinator", "AcquireResult",
    "COLLECTION", "EXECUTION_LOG", "AUDIT_LOG",
    "STATUS_IDLE", "STATUS_QUEUED", "STATUS_RUNNING",
    "STATUS_COMPLETED", "STATUS_FAILED", "STATUS_BLOCKED",
    "STATUS_EXPIRED", "VALID_STATUSES",
    "ACQUIRE_OK", "ACQUIRE_BUSY", "ACQUIRE_BLOCKED_INTERVAL",
    "ACQUIRE_BLOCKED_STATUS",
    "_instance_id", "_revision_info", "_hash_token",
    "_sanitize_metadata", "_sanitize_error",
]
