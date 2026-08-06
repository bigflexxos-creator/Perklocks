"""ProviderBudget — Phase 2β distributed paid-credit budget.

Enforces the global Odds-API credit ceiling shared across every
worker and container.  Two Mongo collections back the primitive:

  • ``provider_budget_state`` — single document per (provider, YYYY-MM)
    holding daily/monthly counters + emergency-reserve accounting.
    All ``reserve/commit/release`` operations are single-document
    atomic updates so no two workers can overrun the ceiling even
    with tens of concurrent callers.

  • ``provider_request_intents`` — append-only reservation records.
    Each intent has an ``expires_at`` so orphaned reservations are
    reaped by ``sweep_expired_reservations`` and their capacity
    returned safely.

Reconciliation against the historical ``odds_api_request_log`` is
supported for after-the-fact audits.
"""
from __future__ import annotations

import hashlib
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

from services.job_coordinator import (
    _sanitize_metadata,
    _sanitize_error,
    _hash_token,
    _instance_id,
    _revision_info,
)

logger = logging.getLogger("lockscore.provider_budget")

BUDGET_STATE_COLL   = "provider_budget_state"
INTENTS_COLL        = "provider_request_intents"
AUDIT_COLL          = "job_audit_log"       # shared with JobCoordinator

# ── Configuration ───────────────────────────────────────────────────
DEFAULT_PROVIDER    = "odds_api"

# ── Outcome constants ───────────────────────────────────────────────
OUT_ALLOWED                  = "allowed"
OUT_BLOCKED_DAILY            = "blocked_daily_limit"
OUT_BLOCKED_MONTHLY          = "blocked_monthly_limit"
OUT_BLOCKED_JOB              = "blocked_job_limit"
OUT_BLOCKED_EMERGENCY_POLICY = "blocked_emergency_policy"
OUT_DUPLICATE                = "duplicate_reservation"
OUT_COMMITTED                = "committed"
OUT_RELEASED                 = "released"
OUT_EXPIRED                  = "expired"

# ── Intent lifecycle statuses ───────────────────────────────────────
INTENT_RESERVED     = "reserved"
INTENT_COMMITTED    = "committed"
INTENT_RELEASED     = "released"
INTENT_EXPIRED      = "expired"

# ── Reasons for emergency reserve ───────────────────────────────────
EMERGENCY_REASONS = {
    "board_missing",
    "board_critically_stale",
}

# Default expiry for a reservation that never commits/releases.
DEFAULT_RESERVATION_TTL_SECONDS = 900   # 15 minutes


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _day_key(dt: Optional[datetime] = None) -> str:
    dt = dt or _now()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _month_key(dt: Optional[datetime] = None) -> str:
    dt = dt or _now()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m")


def _env_int(name: str, default: int, *,
              allow_zero: bool = False) -> int:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    try:
        n = int(v)
        if allow_zero:
            if n < 0:
                return default
        elif n <= 0:
            return default
        return n
    except Exception:
        return default


def _daily_limit() -> int:
    return _env_int("ODDS_DAILY_CREDIT_LIMIT", 3000)


def _monthly_limit() -> int:
    return _env_int("ODDS_MONTHLY_CREDIT_LIMIT", 100_000)


def _emergency_reserve() -> int:
    # 0 is a legitimate value — the operator may choose to remove
    # the emergency reserve entirely.
    return _env_int("ODDS_EMERGENCY_RESERVE", 10_000, allow_zero=True)


class ProviderBudget:
    """See module docstring."""

    def __init__(self, db: AsyncIOMotorDatabase, *,
                  provider: str = DEFAULT_PROVIDER) -> None:
        self.db = db
        self.provider = provider

    # ─────────────────────────────────────────────────────────
    # Bootstrap
    # ─────────────────────────────────────────────────────────
    async def ensure_indices(self) -> None:
        try:
            await self.db[BUDGET_STATE_COLL].create_index(
                [("provider", 1), ("month_key", 1)],
                name="provider_month_uniq",
                unique=True,
            )
        except Exception as e:  # pragma: no cover
            logger.debug("budget_state index: %s", e)
        try:
            await self.db[INTENTS_COLL].create_index(
                "intent_id", name="intent_id_uniq", unique=True)
            await self.db[INTENTS_COLL].create_index(
                "request_key", name="request_key_idx",
                partialFilterExpression={
                    "request_key": {"$exists": True, "$ne": None}
                },
            )
            await self.db[INTENTS_COLL].create_index(
                [("provider", 1), ("status", 1)], name="prov_status_idx")
            await self.db[INTENTS_COLL].create_index(
                "expires_at", name="expires_at_idx")
            await self.db[INTENTS_COLL].create_index(
                "created_at", name="created_at_idx")
        except Exception as e:  # pragma: no cover
            logger.debug("intents index: %s", e)

    # ─────────────────────────────────────────────────────────
    # Snapshot helpers
    # ─────────────────────────────────────────────────────────
    async def _state_doc(self, month_key: str) -> dict:
        d = await self.db[BUDGET_STATE_COLL].find_one(
            {"provider": self.provider, "month_key": month_key},
            {"_id": 0},
        )
        return d or {}

    def _remaining(self, doc: dict, day_key: str,
                    emergency: bool) -> dict:
        days = (doc.get("days") or {}).get(day_key) or {}
        month = doc.get("month") or {}
        day_used     = int(days.get("used", 0))
        day_reserved = int(days.get("reserved", 0))
        mon_used     = int(month.get("used", 0))
        mon_reserved = int(month.get("reserved", 0))
        mon_emerg    = int(month.get("emergency_used", 0))
        dlim = _daily_limit()
        mlim = _monthly_limit()
        er   = _emergency_reserve()
        normal_month_cap = max(0, mlim - er)
        # For non-emergency requests, the effective ceiling is the
        # monthly cap minus the emergency reserve.
        if emergency:
            month_cap = mlim
        else:
            month_cap = normal_month_cap
        return {
            "day_used":         day_used,
            "day_reserved":     day_reserved,
            "day_remaining":    max(0, dlim - day_used - day_reserved),
            "month_used":       mon_used,
            "month_reserved":   mon_reserved,
            "month_remaining":  max(0, month_cap - mon_used - mon_reserved),
            "emergency_used":   mon_emerg,
            "emergency_remaining": max(0, er - mon_emerg),
            "daily_limit":      dlim,
            "monthly_limit":    mlim,
            "emergency_reserve": er,
            "normal_month_cap": normal_month_cap,
        }

    async def get_daily_usage(self, day_key: Optional[str] = None) -> dict:
        dk = day_key or _day_key()
        mk = dk[:7]
        return self._remaining(await self._state_doc(mk), dk, emergency=False)

    async def get_monthly_usage(self,
                                 month_key: Optional[str] = None) -> dict:
        mk = month_key or _month_key()
        return self._remaining(await self._state_doc(mk), _day_key(),
                                emergency=False)

    async def get_remaining(self, *, emergency: bool = False) -> dict:
        dk = _day_key()
        mk = _month_key()
        return self._remaining(await self._state_doc(mk), dk, emergency)

    async def get_budget_status(self) -> dict:
        dk = _day_key()
        mk = _month_key()
        doc = await self._state_doc(mk)
        normal = self._remaining(doc, dk, emergency=False)
        emerg  = self._remaining(doc, dk, emergency=True)
        return {
            "provider":       self.provider,
            "day_key":        dk,
            "month_key":      mk,
            "normal":         normal,
            "with_emergency": emerg,
            "state_present":  bool(doc),
        }

    def can_use_emergency_reserve(self, *, caller: str, reason: str,
                                    job_name: Optional[str] = None) -> bool:
        """Policy gate — emergency capacity may only be requested from
        board-recovery contexts.  Normal user actions, refreshes,
        retries, and admin refreshes are forbidden."""
        if (reason or "").lower() not in EMERGENCY_REASONS:
            return False
        c = (caller or "").lower()
        # Blocklist common user/routine callers even if caller says a
        # magic reason — belt & suspenders.
        for banned in (
            "user_refresh", "user_read", "user_action",
            "focus_refetch", "retry",
        ):
            if banned in c:
                return False
        return True

    # ─────────────────────────────────────────────────────────
    # Reserve / Commit / Release
    # ─────────────────────────────────────────────────────────
    async def check_allowance(
        self, *, estimated_credits: int, caller: str, job_name: str,
        emergency_requested: bool = False, reason: str = "",
    ) -> dict:
        """Pure predicate — would ``reserve`` succeed right now?
        Does NOT reserve any capacity (used by shadow-mode)."""
        dk = _day_key()
        mk = _month_key()
        emergency = bool(emergency_requested) and \
            self.can_use_emergency_reserve(
                caller=caller, reason=reason, job_name=job_name)
        rem = self._remaining(await self._state_doc(mk), dk, emergency)
        est = int(estimated_credits or 0)
        if emergency_requested and not emergency:
            return {
                "allowed": False,
                "outcome": OUT_BLOCKED_EMERGENCY_POLICY,
                "estimated_credits": est,
                **rem,
            }
        if est > rem["day_remaining"]:
            return {
                "allowed": False,
                "outcome": OUT_BLOCKED_DAILY,
                "estimated_credits": est,
                **rem,
            }
        if est > rem["month_remaining"]:
            return {
                "allowed": False,
                "outcome": OUT_BLOCKED_MONTHLY,
                "estimated_credits": est,
                **rem,
            }
        return {
            "allowed": True,
            "outcome": OUT_ALLOWED,
            "estimated_credits": est,
            "emergency": emergency,
            **rem,
        }

    async def reserve(
        self, *,
        estimated_credits: int,
        endpoint_type: str,
        caller: str,
        job_name: str,
        sport: Optional[str] = None,
        market: Optional[str] = None,
        emergency_requested: bool = False,
        reason: str = "",
        request_key: Optional[str] = None,
        ttl_seconds: int = DEFAULT_RESERVATION_TTL_SECONDS,
        metadata: Optional[dict] = None,
    ) -> dict:
        """Atomically reserve ``estimated_credits``.  Returns
        {"intent_id": ..., "outcome": ...}.  Truthy outcome is
        ``allowed``.  If the same ``request_key`` was already
        reserved, this call returns the prior intent unchanged."""
        now = _now()
        est = int(estimated_credits or 0)
        if est < 0:
            est = 0
        dk = _day_key(now)
        mk = _month_key(now)
        clean_md = _sanitize_metadata(metadata) or {}

        # Idempotency: request_key already reserved?
        if request_key:
            existing = await self.db[INTENTS_COLL].find_one(
                {"provider": self.provider, "request_key": request_key,
                 "status": {"$in": [INTENT_RESERVED, INTENT_COMMITTED]}},
                {"_id": 0},
            )
            if existing:
                return {
                    "outcome":   OUT_DUPLICATE,
                    "allowed":   True,
                    "intent_id": existing["intent_id"],
                    "reused":    True,
                    "estimated_credits": existing.get("estimated_credits"),
                }

        emergency = bool(emergency_requested) and \
            self.can_use_emergency_reserve(
                caller=caller, reason=reason, job_name=job_name)
        if emergency_requested and not emergency:
            await self._audit_denied(
                OUT_BLOCKED_EMERGENCY_POLICY,
                caller=caller, job_name=job_name,
                estimated_credits=est, reason=reason,
            )
            return {"outcome": OUT_BLOCKED_EMERGENCY_POLICY,
                    "allowed": False}

        dlim = _daily_limit()
        mlim = _monthly_limit()
        er   = _emergency_reserve()
        # Effective monthly cap: normal callers cannot dip into the
        # emergency reserve.
        month_cap = mlim if emergency else max(0, mlim - er)

        # Atomic conditional increment on the single (provider, month)
        # doc.  Using $expr on aggregated fields lets us guard both
        # daily and monthly ceilings simultaneously.
        day_used_path      = f"days.{dk}.used"
        day_reserved_path  = f"days.{dk}.reserved"
        day_used_expr      = {"$ifNull": [f"${day_used_path}", 0]}
        day_reserved_expr  = {"$ifNull": [f"${day_reserved_path}", 0]}
        mon_used_expr      = {"$ifNull": ["$month.used", 0]}
        mon_reserved_expr  = {"$ifNull": ["$month.reserved", 0]}
        expr = {
            "$and": [
                {"$lte": [
                    {"$add": [day_used_expr, day_reserved_expr, est]},
                    dlim,
                ]},
                {"$lte": [
                    {"$add": [mon_used_expr, mon_reserved_expr, est]},
                    month_cap,
                ]},
            ]
        }
        filt = {
            "provider":  self.provider,
            "month_key": mk,
            "$expr":     expr,
        }
        # We do not upsert on the guarded update — first bootstrap the
        # doc with a plain upsert so subsequent conditional updates
        # have known baseline fields.
        await self.db[BUDGET_STATE_COLL].update_one(
            {"provider": self.provider, "month_key": mk},
            {"$setOnInsert": {
                "provider":  self.provider,
                "month_key": mk,
                "created_at": now,
                "month": {"used": 0, "reserved": 0, "emergency_used": 0},
                "days":  {},
            }},
            upsert=True,
        )
        inc: dict[str, Any] = {
            day_reserved_path: est,
            "month.reserved":  est,
        }
        set_ = {
            "provider":   self.provider,
            "month_key":  mk,
            "updated_at": now,
        }
        res = await self.db[BUDGET_STATE_COLL].find_one_and_update(
            filt, {"$inc": inc, "$set": set_}, return_document=True,
        )
        if res is None:
            # Determine which limit blocked.
            state = await self._state_doc(mk)
            rem_e = self._remaining(state, dk, emergency=True)
            rem_n = self._remaining(state, dk, emergency=False)
            if est > rem_n["day_remaining"]:
                outcome = OUT_BLOCKED_DAILY
            elif not emergency and est > rem_n["month_remaining"]:
                outcome = OUT_BLOCKED_MONTHLY
            elif emergency and est > rem_e["month_remaining"]:
                outcome = OUT_BLOCKED_MONTHLY
            else:
                outcome = OUT_BLOCKED_MONTHLY
            await self._audit_denied(
                outcome, caller=caller, job_name=job_name,
                estimated_credits=est, reason=reason,
                emergency=emergency,
            )
            return {"outcome": outcome, "allowed": False}

        # Persist the intent record.
        intent_id = uuid.uuid4().hex
        intent = {
            "intent_id":          intent_id,
            "provider":           self.provider,
            "endpoint_type":      str(endpoint_type)[:80],
            "caller":             str(caller)[:120],
            "job_name":           str(job_name)[:200],
            "sport":              sport,
            "market":             market,
            "estimated_credits":  est,
            "emergency_requested": bool(emergency_requested),
            "emergency_granted":  bool(emergency),
            "reason":             str(reason)[:2000],
            "request_key":        request_key,
            "status":             INTENT_RESERVED,
            "day_key":            dk,
            "month_key":          mk,
            "created_at":         now,
            "expires_at":         now + timedelta(seconds=int(ttl_seconds)),
            "metadata":           clean_md,
            "revision":           _revision_info(),
        }
        try:
            await self.db[INTENTS_COLL].insert_one(intent)
        except Exception as e:
            # Roll back the reservation to avoid a permanent leak.
            logger.warning("intent insert failed, rolling back: %s", e)
            await self.db[BUDGET_STATE_COLL].update_one(
                {"provider": self.provider, "month_key": mk},
                {"$inc": {day_reserved_path: -est,
                           "month.reserved": -est}},
            )
            return {"outcome": OUT_BLOCKED_MONTHLY, "allowed": False,
                    "error": _sanitize_error(e)}

        if emergency:
            await self._audit(
                "emergency_reserve_used",
                caller=caller, job_name=job_name,
                intent_id=intent_id,
                estimated_credits=est, reason=reason,
                sport=sport, market=market,
            )
        return {
            "outcome":   OUT_ALLOWED,
            "allowed":   True,
            "intent_id": intent_id,
            "day_key":   dk,
            "month_key": mk,
            "emergency": emergency,
            "estimated_credits": est,
        }

    async def top_up(self, intent_id: str, *, extra: int,
                       emergency_requested: bool = False,
                       reason: str = "actual_over_estimate") -> dict:
        """Atomically extend an existing reservation by ``extra``
        credits.  Concurrency-safe — the same ``$expr`` filter used
        by ``reserve`` guards daily/monthly caps.

        Returns::

            {"ok": True,  "outcome": "allowed", "extra": N}
            {"ok": False, "outcome": "blocked_daily_limit"  ...}
            {"ok": False, "outcome": "blocked_monthly_limit" ...}
            {"ok": False, "outcome": "intent_not_reserved"}
        """
        extra = int(extra or 0)
        if extra <= 0:
            return {"ok": True, "outcome": "no_op", "extra": 0}
        intent = await self.db[INTENTS_COLL].find_one(
            {"intent_id": intent_id}, {"_id": 0})
        if not intent or intent.get("status") != INTENT_RESERVED:
            return {"ok": False, "outcome": "intent_not_reserved"}
        dk = intent["day_key"]
        mk = intent["month_key"]
        emergency = bool(emergency_requested) or bool(
            intent.get("emergency_granted"))
        dlim = _daily_limit()
        mlim = _monthly_limit()
        er   = _emergency_reserve()
        month_cap = mlim if emergency else max(0, mlim - er)
        day_used_path      = f"days.{dk}.used"
        day_reserved_path  = f"days.{dk}.reserved"
        expr = {
            "$and": [
                {"$lte": [
                    {"$add": [
                        {"$ifNull": [f"${day_used_path}", 0]},
                        {"$ifNull": [f"${day_reserved_path}", 0]},
                        extra,
                    ]}, dlim,
                ]},
                {"$lte": [
                    {"$add": [
                        {"$ifNull": ["$month.used", 0]},
                        {"$ifNull": ["$month.reserved", 0]},
                        extra,
                    ]}, month_cap,
                ]},
            ],
        }
        now = _now()
        res = await self.db[BUDGET_STATE_COLL].find_one_and_update(
            {"provider": self.provider, "month_key": mk, "$expr": expr},
            {"$inc": {day_reserved_path: extra,
                       "month.reserved": extra},
             "$set": {"updated_at": now}},
            return_document=True,
        )
        if res is None:
            # Determine which limit blocked.
            state = await self._state_doc(mk)
            rem = self._remaining(state, dk, emergency)
            if extra > rem["day_remaining"]:
                outcome = OUT_BLOCKED_DAILY
            else:
                outcome = OUT_BLOCKED_MONTHLY
            await self._audit_denied(
                outcome, caller="top_up",
                job_name=intent.get("job_name"),
                estimated_credits=extra, reason=reason,
                emergency=emergency, intent_id=intent_id,
            )
            return {"ok": False, "outcome": outcome, "extra": extra}
        # Update the intent record so subsequent commit uses the new
        # reserved total.
        await self.db[INTENTS_COLL].update_one(
            {"intent_id": intent_id, "status": INTENT_RESERVED},
            {"$inc": {"estimated_credits": extra},
             "$push": {"top_ups": {
                 "extra": extra, "reason": reason, "at": now,
             }}},
        )
        await self._audit(
            "budget_top_up", caller="top_up",
            intent_id=intent_id, extra=extra,
            job_name=intent.get("job_name"), reason=reason,
        )
        return {"ok": True, "outcome": OUT_ALLOWED, "extra": extra}

    async def commit(self, intent_id: str, *,
                      actual_credits: Optional[int] = None,
                      response_metadata: Optional[dict] = None) -> dict:
        """Convert a reservation to committed usage.  Idempotent:
        second commit returns the existing final state without
        double-counting.

        If ``actual_credits > estimated_credits`` the caller SHOULD
        first invoke ``top_up(intent_id, extra=actual-estimated)`` so
        the extra capacity is reserved atomically before commit.  If
        the top-up fails there is not enough budget for the actual
        cost — the caller should log the overage and stop follow-up
        fan-out.  ``commit`` itself is safe to call with a larger
        actual than reserved but no atomicity guarantee is provided
        for the delta in that path.
        """
        now = _now()
        intent = await self.db[INTENTS_COLL].find_one(
            {"intent_id": intent_id}, {"_id": 0},
        )
        if not intent:
            return {"outcome": OUT_EXPIRED, "committed": False,
                    "error": "intent_not_found"}
        if intent.get("status") == INTENT_COMMITTED:
            # Idempotent no-op.
            return {"outcome": OUT_COMMITTED, "committed": True,
                    "idempotent": True,
                    "actual_credits": intent.get("actual_credits")}
        if intent.get("status") != INTENT_RESERVED:
            return {"outcome": intent.get("status") or OUT_EXPIRED,
                    "committed": False,
                    "error": "intent_not_reserved"}

        est = int(intent.get("estimated_credits") or 0)
        if actual_credits is None:
            actual = est
        else:
            actual = int(actual_credits)
            if actual < 0:
                actual = 0
        dk = intent["day_key"]
        mk = intent["month_key"]
        emergency = bool(intent.get("emergency_granted"))
        day_used_path     = f"days.{dk}.used"
        day_reserved_path = f"days.{dk}.reserved"

        inc: dict[str, Any] = {
            day_used_path:      actual,
            day_reserved_path:  -est,
            "month.used":       actual,
            "month.reserved":   -est,
        }
        if emergency:
            inc["month.emergency_used"] = actual
        await self.db[BUDGET_STATE_COLL].update_one(
            {"provider": self.provider, "month_key": mk},
            {"$inc": inc, "$set": {"updated_at": now}},
        )
        # Only mark committed if still reserved (race guard).
        res = await self.db[INTENTS_COLL].update_one(
            {"intent_id": intent_id, "status": INTENT_RESERVED},
            {"$set": {
                "status":            INTENT_COMMITTED,
                "committed_at":      now,
                "actual_credits":    actual,
                "delta_credits":     actual - est,
                "response_metadata": _sanitize_metadata(response_metadata) or {},
            }},
        )
        if not res.modified_count:
            # Lost the race — undo our budget change.
            undo: dict[str, Any] = {
                day_used_path:      -actual,
                day_reserved_path:  est,
                "month.used":       -actual,
                "month.reserved":   est,
            }
            if emergency:
                undo["month.emergency_used"] = -actual
            await self.db[BUDGET_STATE_COLL].update_one(
                {"provider": self.provider, "month_key": mk},
                {"$inc": undo},
            )
            return {"outcome": OUT_COMMITTED, "committed": True,
                    "idempotent": True}
        return {"outcome": OUT_COMMITTED, "committed": True,
                "actual_credits": actual, "estimated_credits": est,
                "delta": actual - est}

    async def release(self, intent_id: str, *, reason: str = "") -> dict:
        """Cancel a reservation and return capacity."""
        now = _now()
        intent = await self.db[INTENTS_COLL].find_one(
            {"intent_id": intent_id}, {"_id": 0},
        )
        if not intent:
            return {"outcome": OUT_EXPIRED, "released": False}
        if intent.get("status") != INTENT_RESERVED:
            return {"outcome": intent.get("status"),
                    "released": False, "idempotent": True}
        est = int(intent.get("estimated_credits") or 0)
        dk = intent["day_key"]
        mk = intent["month_key"]
        res = await self.db[INTENTS_COLL].update_one(
            {"intent_id": intent_id, "status": INTENT_RESERVED},
            {"$set": {"status": INTENT_RELEASED,
                       "released_at": now,
                       "release_reason": str(reason)[:2000]}},
        )
        if not res.modified_count:
            return {"outcome": OUT_RELEASED, "released": True,
                    "idempotent": True}
        await self.db[BUDGET_STATE_COLL].update_one(
            {"provider": self.provider, "month_key": mk},
            {"$inc": {f"days.{dk}.reserved": -est,
                       "month.reserved": -est},
             "$set": {"updated_at": now}},
        )
        return {"outcome": OUT_RELEASED, "released": True,
                "estimated_credits": est}

    async def sweep_expired_reservations(self) -> int:
        """Reap intents that never committed or released.  Their
        reserved capacity is returned to the budget."""
        now = _now()
        expired_intents = await self.db[INTENTS_COLL].find(
            {"provider": self.provider, "status": INTENT_RESERVED,
             "expires_at": {"$lt": now}},
        ).to_list(500)
        n = 0
        for intent in expired_intents:
            # Best-effort atomic transition
            res = await self.db[INTENTS_COLL].update_one(
                {"intent_id": intent["intent_id"],
                 "status": INTENT_RESERVED},
                {"$set": {"status": INTENT_EXPIRED,
                           "expired_at": now}},
            )
            if not res.modified_count:
                continue
            est = int(intent.get("estimated_credits") or 0)
            dk = intent.get("day_key")
            mk = intent.get("month_key")
            await self.db[BUDGET_STATE_COLL].update_one(
                {"provider": self.provider, "month_key": mk},
                {"$inc": {f"days.{dk}.reserved": -est,
                           "month.reserved": -est},
                 "$set": {"updated_at": now}},
            )
            n += 1
        if n:
            await self._audit(
                "reservations_expired", count=n,
            )
        return n

    # ─────────────────────────────────────────────────────────
    # Reconciliation against odds_api_request_log
    # ─────────────────────────────────────────────────────────
    async def reconcile_from_request_log(
        self, *, day_key: Optional[str] = None,
        assume_credits_per_request: int = 1,
    ) -> dict:
        """Compare committed-intent totals with the historical
        ``odds_api_request_log`` for the day.  Does **not** mutate
        state — read-only audit.
        """
        dk = day_key or _day_key()
        start_iso = f"{dk}T00:00:00+00:00"
        # end of day
        y, m, d = [int(x) for x in dk.split("-")]
        end_dt = datetime(y, m, d, tzinfo=timezone.utc) + timedelta(days=1)
        end_iso = end_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        try:
            request_count = await self.db.odds_api_request_log.count_documents({
                "ts": {"$gte": start_iso, "$lt": end_iso},
                "upstream_called": True,
            })
        except Exception as e:
            request_count = None
            logger.warning("reconcile: request_log count failed: %s", e)

        # Committed intents for this day.
        committed = await self.db[INTENTS_COLL].aggregate([
            {"$match": {"provider": self.provider, "day_key": dk,
                          "status": INTENT_COMMITTED}},
            {"$group": {"_id": None,
                         "actual": {"$sum": "$actual_credits"},
                         "count":  {"$sum": 1}}},
        ]).to_list(1)
        committed_total = int((committed or [{}])[0].get("actual", 0))
        committed_count = int((committed or [{}])[0].get("count", 0))

        est_from_log = (
            None if request_count is None
            else request_count * int(assume_credits_per_request)
        )
        return {
            "day_key":               dk,
            "committed_intents":     committed_count,
            "committed_credits":     committed_total,
            "request_log_upstream":  request_count,
            "estimated_log_credits": est_from_log,
            "delta":                 (
                None if est_from_log is None
                else committed_total - est_from_log
            ),
            "assume_credits_per_request": assume_credits_per_request,
        }

    async def recent_blocked(self, *, limit: int = 50) -> list[dict]:
        return await self.db[AUDIT_COLL].find(
            {"event_type": {"$in": [
                "budget_denied",
                "emergency_reserve_used",
            ]}},
            {"_id": 0},
        ).sort("created_at", -1).to_list(limit)

    # ─────────────────────────────────────────────────────────
    # Audit
    # ─────────────────────────────────────────────────────────
    async def _audit(self, event_type: str, **fields: Any) -> None:
        now = _now()
        rec: dict[str, Any] = {
            "event_type": event_type,
            "created_at": now,
            "ttl_at":     now + timedelta(days=180),
            "revision":   _revision_info(),
            "provider":   self.provider,
        }
        for k, v in fields.items():
            rec[k] = _sanitize_metadata(v) if isinstance(v, dict) else v
        try:
            await self.db[AUDIT_COLL].insert_one(rec)
        except Exception as e:  # pragma: no cover
            logger.warning("budget audit write failed: %s", e)

    async def _audit_denied(self, outcome: str, **fields: Any) -> None:
        await self._audit("budget_denied", outcome=outcome, **fields)


__all__ = [
    "ProviderBudget",
    "OUT_ALLOWED", "OUT_BLOCKED_DAILY", "OUT_BLOCKED_MONTHLY",
    "OUT_BLOCKED_JOB", "OUT_BLOCKED_EMERGENCY_POLICY",
    "OUT_DUPLICATE", "OUT_COMMITTED", "OUT_RELEASED", "OUT_EXPIRED",
    "INTENT_RESERVED", "INTENT_COMMITTED", "INTENT_RELEASED",
    "INTENT_EXPIRED",
    "EMERGENCY_REASONS",
    "BUDGET_STATE_COLL", "INTENTS_COLL", "AUDIT_COLL",
    "DEFAULT_PROVIDER",
    "_day_key", "_month_key",
    "_daily_limit", "_monthly_limit", "_emergency_reserve",
]
