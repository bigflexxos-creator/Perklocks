"""Safe stale-lease inspection + scoped recovery (2026-10-20 P0-2).

Used by :mod:`services.pick_refresh_orchestrator` when the per-sport
refresh lease (``db.scheduler_leases``, key
``picks_refresh:{date_str}:{sport_filter or 'ALL'}``) blocks a fresh
acquisition.  The helper:

* INSPECTS the existing lease — returns owner, acquired_at,
  expires_at, TTL state, age, and (when present) heartbeat.
* CLASSIFIES it as ``ACTIVE`` or ``STALE`` using strict rules that
  never force-delete a healthy lease:

  * ``expires_at < now - GRACE_SEC``  → **STALE**  (Mongo TTL granularity
    is up to 60 s, so a lease whose expires_at has passed by more than
    the grace is unambiguously abandoned).
  * ``now - acquired_at > 2 × TTL``   → **STALE**  (something crashed —
    the owner can't still be holding this lease legitimately).
  * ``heartbeat_at < now - 2 × HEARTBEAT_SEC`` → **STALE**  (if a
    heartbeat is present, missing two intervals in a row means the
    owner is dead; absent heartbeat is NOT treated as stale on its
    own because the current writer does not emit one yet).
  * otherwise → **ACTIVE**.

* If ``STALE`` and ``allow_force=True``, performs a **scoped**
  compare-and-delete: deletes exactly one document matching
  ``lease_key`` + ``owner`` + ``acquired_at`` so another instance that
  took over in the meantime cannot be clobbered.  Returns the next
  acquisition attempt to the caller (orchestrator) — this helper
  itself never re-acquires.

Guard-rails (match the user's stated contract):

* Never blind-deletes an active lease.
* Scoped to the exact ``lease_key`` the caller passes in; cannot leak
  across sports or lease families (JobCoordinator leases live in a
  different collection and are untouched).
* Idempotent / safe to rerun: compare-and-delete uses the owner +
  acquired_at tuple, so a second call becomes a no-op once the stale
  document is gone.
* Fail-closed: all exceptions degrade to classification ``UNKNOWN``
  which the caller treats as "do not force release".
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

logger = logging.getLogger("lockscore.lease_recovery")

# Mongo TTL index on ``expires_at`` is set to expireAfterSeconds=0 by
# the orchestrator at acquire time, but TTL granularity is up to 60s.
# We require more grace than the TTL sweep to call a lease stale.
STALE_EXPIRES_GRACE_SEC = 90

# Heartbeat window — writers that opt-in to heartbeat may stamp
# ``heartbeat_at`` periodically.  Current refresh writer does not emit
# heartbeats, so this check is only exercised when a future writer does.
HEARTBEAT_WINDOW_SEC = 120

LEASE_COLLECTION = "scheduler_leases"


@dataclass
class LeaseInspection:
    """Result of :func:`inspect_lease` — always safe to serialize."""
    lease_key: str
    found: bool
    owner: Optional[str] = None
    acquired_at: Optional[str] = None
    expires_at: Optional[str] = None
    heartbeat_at: Optional[str] = None
    cycle_date: Optional[str] = None
    sport_filter: Optional[str] = None
    age_sec: Optional[float] = None
    ttl_remaining_sec: Optional[float] = None
    classification: str = "UNKNOWN"
    classification_reason: Optional[str] = None
    now_iso: Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    try:
        return dt.replace(tzinfo=timezone.utc).isoformat() if dt.tzinfo is None \
            else dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return None


async def inspect_lease(db, lease_key: str) -> LeaseInspection:
    """Inspect a scheduler lease without modifying it.

    Returns a :class:`LeaseInspection` with the lease metadata and a
    classification of ``ACTIVE`` / ``STALE`` / ``MISSING`` / ``UNKNOWN``.
    """
    now = datetime.now(timezone.utc)
    insp = LeaseInspection(lease_key=lease_key, found=False,
                           now_iso=now.isoformat())
    try:
        doc = await db[LEASE_COLLECTION].find_one({"lease_key": lease_key})
    except Exception as e:
        logger.warning("lease_recovery.inspect: DB error %s", e)
        insp.classification = "UNKNOWN"
        insp.classification_reason = f"db_error:{type(e).__name__}"
        return insp
    if not doc:
        insp.classification = "MISSING"
        insp.classification_reason = "no_lease_document"
        return insp
    insp.found = True
    insp.owner = doc.get("owner")
    acquired_at = doc.get("acquired_at")
    expires_at = doc.get("expires_at")
    heartbeat_at = doc.get("heartbeat_at")
    insp.acquired_at = _iso(acquired_at)
    insp.expires_at = _iso(expires_at)
    insp.heartbeat_at = _iso(heartbeat_at)
    insp.cycle_date = doc.get("cycle_date")
    insp.sport_filter = doc.get("sport_filter")
    try:
        if acquired_at:
            if acquired_at.tzinfo is None:
                acquired_at = acquired_at.replace(tzinfo=timezone.utc)
            insp.age_sec = (now - acquired_at).total_seconds()
    except Exception:
        pass
    try:
        if expires_at:
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            insp.ttl_remaining_sec = (expires_at - now).total_seconds()
    except Exception:
        pass

    # Classification — strict order, fail-closed.
    reasons: list[str] = []
    is_stale = False
    if expires_at is not None and insp.ttl_remaining_sec is not None:
        if insp.ttl_remaining_sec < -STALE_EXPIRES_GRACE_SEC:
            is_stale = True
            reasons.append(
                f"expires_at_passed_by_{abs(insp.ttl_remaining_sec):.0f}s"
            )
    # Heartbeat: only call stale if heartbeat PRESENT and stale.
    try:
        if heartbeat_at is not None:
            if heartbeat_at.tzinfo is None:
                heartbeat_at = heartbeat_at.replace(tzinfo=timezone.utc)
            hb_age = (now - heartbeat_at).total_seconds()
            if hb_age > 2 * HEARTBEAT_WINDOW_SEC:
                is_stale = True
                reasons.append(f"heartbeat_stale_{hb_age:.0f}s")
    except Exception:
        pass
    # Fallback: lease older than 2× TTL means something crashed.
    try:
        if (insp.age_sec is not None
                and expires_at is not None
                and acquired_at is not None):
            # 2× the configured TTL window.
            orig_ttl = (expires_at - acquired_at).total_seconds()
            if orig_ttl > 0 and insp.age_sec > 2 * orig_ttl:
                is_stale = True
                reasons.append(
                    f"age_exceeds_2x_ttl_age={insp.age_sec:.0f}s_ttl={orig_ttl:.0f}s"
                )
    except Exception:
        pass

    insp.classification = "STALE" if is_stale else "ACTIVE"
    insp.classification_reason = ",".join(reasons) if reasons else (
        "within_ttl_window"
    )
    return insp


async def force_release_if_stale(db, lease_key: str,
                                  caller: Optional[str] = None,
                                  dry_run: bool = False) -> dict:
    """Scoped force-release the lease ONLY if classified STALE.

    Compare-and-delete uses the exact owner + acquired_at tuple so a
    concurrent fresh acquisition cannot be clobbered.  Returns a dict
    with ``acted`` (bool), ``classification``, and the full inspection.

    This helper is intentionally scoped to a SINGLE ``lease_key`` and
    performs at most ONE delete.  It never touches other sports' leases
    and does not reach into the JobCoordinator lease family.
    """
    insp = await inspect_lease(db, lease_key)
    out: dict[str, Any] = {
        "lease_key": lease_key,
        "classification": insp.classification,
        "inspection": insp.to_dict(),
        "acted": False,
        "deleted_count": 0,
        "dry_run": dry_run,
        "caller": caller,
    }
    if insp.classification != "STALE":
        return out
    if dry_run:
        out["acted"] = False
        out["note"] = "stale_detected_dry_run_no_delete"
        return out
    # Scoped compare-and-delete.
    try:
        parsed_acquired = None
        if insp.acquired_at:
            try:
                parsed_acquired = datetime.fromisoformat(
                    insp.acquired_at.replace("Z", "+00:00"))
            except Exception:
                parsed_acquired = None
        filt: dict[str, Any] = {"lease_key": lease_key}
        if insp.owner is not None:
            filt["owner"] = insp.owner
        if parsed_acquired is not None:
            filt["acquired_at"] = parsed_acquired
        res = await db[LEASE_COLLECTION].delete_one(filt)
        out["acted"] = res.deleted_count > 0
        out["deleted_count"] = int(res.deleted_count)
        logger.info(
            "lease_recovery.force_release: lease=%s stale_reason=%s "
            "owner=%s acquired_at=%s deleted=%d caller=%s",
            lease_key, insp.classification_reason, insp.owner,
            insp.acquired_at, res.deleted_count, caller,
        )
    except Exception as e:
        logger.warning("lease_recovery.force_release failed: %s", e)
        out["error"] = f"{type(e).__name__}:{e}"
    return out


__all__ = [
    "LEASE_COLLECTION",
    "STALE_EXPIRES_GRACE_SEC",
    "HEARTBEAT_WINDOW_SEC",
    "LeaseInspection",
    "inspect_lease",
    "force_release_if_stale",
]
