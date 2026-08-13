"""Canonical SettlementService (P0.2a hardening, 2026-06).

Single owner of settlement truth.  All settle-* adapters must call
`SettlementService.record()` instead of mutating `picks.status` directly.

Architecture:
    - `settlement_events` is an append-only, immutable ledger.  Prior
      active rows are deactivated (`is_active=False`) — never deleted —
      when a correction lands.
    - `prediction_snapshots` provides the frozen pregame snapshot; the
      active snapshot_version is stamped onto every settlement event for
      audit-grade provenance.

P0.2a hardening deltas:
    1. Central FINAL-event barrier inside `record()`.  Callers can no
       longer bypass the barrier — LIVE events refuse to settle.
    2. Deterministic idempotency fingerprint (SHA-256 over the canonical
       final-truth tuple).  Re-processing identical truth returns
       ALREADY_SETTLED_IDENTICAL and does NOT inflate the version.
    3. Explicit versioning: `settlement_id`, `settlement_version`,
       `supersedes_settlement_id`, `is_active`, `grader_version`.
    4. Correction contract — v2 supersedes v1 without destroying v1;
       old_result / new_result / correction_reason / corrected_at are
       recorded on v2.
    5. PUSH != VOID — the compatibility mirror now maps `push` → `"push"`
       (not `"void"`).
    6. Wrong-identity fail-closed — `expected_pick_id`, `expected_event_id`,
       `expected_market`, `expected_side`, `expected_line` refuse
       settlement on mismatch.
    7. Compatibility mirror is owned SOLELY by this service.  Adapters
       must never write `pick.status` outside this method.

The append-only immutable ledger (`settlement_events`) remains
authoritative; the `picks` compatibility mirror is derivative only.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger("lockscore.settlement")

COLLECTION = "settlement_events"
VALID_RESULTS = ("won", "lost", "void", "push", "cancelled")

# ── Canonical grader version ──────────────────────────────────────────
GRADER_VERSION = "settlement_service.v2.0"   # bumped from v1 by P0.2a

# ── Refusal / status vocabulary returned by `record()` ────────────────
REFUSAL_LIVE                     = "REFUSED_EVENT_NOT_FINAL"
REFUSAL_MISSING_ACTUAL           = "REFUSED_MISSING_ACTUAL"
REFUSAL_IDENTITY_MISMATCH        = "REFUSED_IDENTITY_MISMATCH"
REFUSAL_INVALID_RESULT           = "REFUSED_INVALID_RESULT"
REFUSAL_MISSING_SOURCE           = "REFUSED_MISSING_SOURCE"
ALREADY_SETTLED_IDENTICAL        = "ALREADY_SETTLED_IDENTICAL"
CORRECTION_APPLIED               = "CORRECTION_APPLIED"
NEW_SETTLEMENT                   = "NEW_SETTLEMENT"


def _fingerprint(*, canonical_pick_id: str, canonical_event_id: str,
                   market: str, side: str, line: Any,
                   actual_result: Any, event_final_source: str,
                   grader_version: str = GRADER_VERSION) -> str:
    """Deterministic settlement fingerprint over the canonical final
    truth tuple.  Two settlements with identical final truth share the
    same fingerprint — driving idempotency."""
    payload = json.dumps({
        "pid":  canonical_pick_id,
        "eid":  canonical_event_id,
        "mkt":  market,
        "side": side,
        "line": line,
        "act":  actual_result,
        "src":  event_final_source,
        "gv":   grader_version,
    }, sort_keys=True, default=str).encode()
    return hashlib.sha256(payload).hexdigest()


def _pick_status_from_result(result: str) -> str:
    """Map canonical result → legacy `pick.status`.  P0.2a: PUSH stays PUSH."""
    return {
        "won":       "won",
        "lost":      "lost",
        "void":      "void",
        "push":      "push",         # ← P0.2a fix (was "void")
        "cancelled": "void",
    }.get(result, "pending")


class SettlementService:
    """Single owner of settlement decisions."""

    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self.db = db

    async def ensure_indices(self) -> None:
        try:
            from services import index_registry as _ir
            await _ir.ensure_collection(self.db, COLLECTION)
        except Exception as e:
            logger.debug("settlement_service ensure_indices: %s", e)

    async def record(
        self, *,
        prediction_id: str,
        result: str,
        source: str,
        actual_result: Optional[dict] = None,
        # P0.2a — central FINAL barrier inputs (all required for
        # standard game/player-prop settlement):
        authoritative_event_final: bool = False,
        canonical_event_id: Optional[str] = None,
        market: Optional[str] = None,
        side: Optional[str] = None,
        line: Any = None,
        # Wrong-identity fail-closed inputs:
        expected_pick_id: Optional[str] = None,
        expected_event_id: Optional[str] = None,
        expected_market: Optional[str] = None,
        expected_side: Optional[str] = None,
        expected_line: Any = None,
        # Correction bookkeeping:
        correction_reason: Optional[str] = None,
        # Backward-compat mirror toggle:
        compat_write_to_picks: bool = True,
    ) -> dict:
        """Record a settlement decision.

        Returns a dict with `status ∈ {NEW_SETTLEMENT, ALREADY_SETTLED_IDENTICAL,
        CORRECTION_APPLIED, REFUSED_*}` plus the resulting event row when
        applicable.
        """
        # ── 1.  Validate result vocabulary ─────────────────────────
        if result not in VALID_RESULTS:
            return {"status": REFUSAL_INVALID_RESULT, "result": result,
                     "reason": f"must be one of {VALID_RESULTS}"}

        # ── 2.  Wrong-identity fail-closed ─────────────────────────
        for want, got, tag in (
            (expected_pick_id,  prediction_id,       "pick_id"),
            (expected_event_id, canonical_event_id,  "event_id"),
            (expected_market,   market,              "market"),
            (expected_side,     side,                "side"),
            (expected_line,     line,                "line"),
        ):
            if want is not None and got is not None and want != got:
                return {"status": REFUSAL_IDENTITY_MISMATCH,
                         "field": tag, "expected": want, "got": got}

        # ── 3.  Source required ────────────────────────────────────
        if not source:
            return {"status": REFUSAL_MISSING_SOURCE}

        # ── 4.  Central FINAL barrier (only for outcome results) ──
        outcome_results = ("won", "lost", "push")
        if result in outcome_results:
            if not authoritative_event_final:
                return {"status": REFUSAL_LIVE,
                         "reason": "authoritative_event_final=False"}
            if actual_result is None:
                return {"status": REFUSAL_MISSING_ACTUAL}

        # ── 5.  Idempotency fingerprint ────────────────────────────
        fp = _fingerprint(
            canonical_pick_id  = prediction_id,
            canonical_event_id = canonical_event_id or "",
            market             = market or "",
            side               = side or "",
            line               = line,
            actual_result      = actual_result,
            event_final_source = source,
        )
        active = await self.db[COLLECTION].find_one(
            {"prediction_id": prediction_id, "is_active": True},
            {"_id": 0},
        )
        if active and active.get("fingerprint") == fp:
            return {"status": ALREADY_SETTLED_IDENTICAL, "event": active}

        # ── 6.  Read the active snapshot to lock the version in ────
        try:
            from services.prediction_publication_service import (
                SNAPSHOT_COLLECTION,
            )
            snap = await self.db[SNAPSHOT_COLLECTION].find_one(
                {"prediction_id": prediction_id, "is_active": True},
                {"snapshot_version": 1, "_id": 0},
            )
            snap_ver = snap.get("snapshot_version") if snap else None
        except Exception:
            snap_ver = None

        # ── 7.  Versioning + correction linkage ────────────────────
        settlement_id = str(uuid.uuid4())
        prior_version = (active or {}).get("settlement_version") or 0
        new_version   = prior_version + 1 if active else 1
        is_correction = bool(active)   # any 2nd+ landing is a correction

        # Deactivate the prior active event (non-destructive).
        if active:
            try:
                await self.db[COLLECTION].update_many(
                    {"prediction_id": prediction_id, "is_active": True},
                    {"$set": {"is_active": False}},
                )
            except Exception as e:
                logger.debug("deactivate prior events: %s", e)

        now = datetime.now(timezone.utc).isoformat()
        event = {
            "settlement_id":          settlement_id,
            "event_id":               settlement_id,   # alias
            "prediction_id":          prediction_id,
            "canonical_event_id":     canonical_event_id,
            "market":                 market,
            "side":                   side,
            "line":                   line,
            "snapshot_version":       snap_ver,
            "settlement_version":     new_version,
            "supersedes_settlement_id": (
                active.get("settlement_id") if is_correction else None
            ),
            "result":                 result,
            "actual_result":          actual_result or {},
            "authoritative_event_final": bool(authoritative_event_final),
            "source":                 source,
            "grader_version":         GRADER_VERSION,
            "fingerprint":            fp,
            "settled_at":             now,
            "created_at":             now,
            "is_active":              True,
            "compat_write":           bool(compat_write_to_picks),
        }
        if is_correction:
            event["old_result"] = active.get("result")
            event["new_result"] = result
            event["old_actual"] = active.get("actual_result")
            event["new_actual"] = actual_result or {}
            event["correction_reason"] = correction_reason or "authoritative_update"
            event["corrected_at"] = now

        await self.db[COLLECTION].insert_one(event)

        # ── 8.  Compatibility mirror (SOLE writer) ─────────────────
        # Upsert so the mirror remains the sole, deterministic reflection
        # of canonical settlement truth even when the legacy pick row is
        # missing (defensive; keeps PUSH/VOID/WON/LOST atomically visible).
        if compat_write_to_picks:
            try:
                await self.db.picks.update_one(
                    {"id": prediction_id},
                    {"$set": {
                        "id":                  prediction_id,
                        "status":              _pick_status_from_result(result),
                        "result":              result,
                        "settled_at":          now,
                        "settlement_result":   result,
                        "settlement_source":   source,
                        "settlement_version":  new_version,
                        "_compat_settlement":  True,
                    }},
                    upsert=True,
                )
            except Exception as e:
                logger.warning("settlement compat write err: %s", e)

        return {
            "status": CORRECTION_APPLIED if is_correction else NEW_SETTLEMENT,
            "event":  event,
        }

    async def get_active_event(self, prediction_id: str) -> Optional[dict]:
        return await self.db[COLLECTION].find_one(
            {"prediction_id": prediction_id, "is_active": True},
            {"_id": 0},
        )


# ── Adapter call contract (for P0.2b migration) ────────────────────────
#
# Every settle-* adapter must call SettlementService like this:
#
#   await svc.record(
#       prediction_id             = pick["id"],                    # required
#       result                    = "won" | "lost" | "push" | "void",
#       source                    = "espn" | "fotmob" | "mlb_stats" | ...,
#       actual_result             = {"actual": 6, "line": 4.5, ...},
#       authoritative_event_final = True,     # ← must prove FINAL
#       canonical_event_id        = pick["event_id"],
#       market                    = pick["market"],
#       side                      = pick["side"],
#       line                      = pick["line"],
#       expected_pick_id          = pick["id"],
#       expected_event_id         = pick["event_id"],
#       expected_market           = pick["market"],
#       expected_side             = pick["side"],
#       expected_line             = pick["line"],
#   )
#
# Adapters MUST NOT set `pick.status` directly.  The service is the
# sole owner of the compatibility mirror.

__all__ = [
    "SettlementService", "COLLECTION", "VALID_RESULTS", "GRADER_VERSION",
    "REFUSAL_LIVE", "REFUSAL_MISSING_ACTUAL", "REFUSAL_IDENTITY_MISMATCH",
    "REFUSAL_INVALID_RESULT", "REFUSAL_MISSING_SOURCE",
    "ALREADY_SETTLED_IDENTICAL", "CORRECTION_APPLIED", "NEW_SETTLEMENT",
    "_pick_status_from_result", "_fingerprint",
]
