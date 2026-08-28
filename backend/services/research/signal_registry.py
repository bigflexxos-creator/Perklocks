"""Signal Registry — Strategy Lab 10X §5.

Persistent registry of discovered SHADOW signals with a formal lifecycle:

    DISCOVERED -> TESTING -> VALIDATED -> VERIFIED -> DEGRADED -> RETIRED

Storage: MongoDB collection `lab_signal_registry`.
Every write is idempotent (upsert on `signal_id`). No writes ever
touch the production pick/lock/settlement collections.

SHADOW signals CANNOT modify live probability — this registry is a
research-lifecycle store only.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

from deps import db

log = logging.getLogger("lockscore.research.signal_registry")

STATUSES = ("DISCOVERED", "TESTING", "VALIDATED", "VERIFIED",
            "DEGRADED", "RETIRED")

COLLECTION = "lab_signal_registry"


def make_signal_id(sport: str, market_family: str, conditions: dict[str, Any]) -> str:
    key = f"{sport}|{market_family}|" + "|".join(
        f"{k}={conditions[k]}" for k in sorted(conditions.keys())
    )
    return "sig_" + hashlib.sha1(key.encode()).hexdigest()[:16]


async def upsert(
    sport: str,
    market_family: str,
    conditions: dict[str, Any],
    metrics: dict[str, Any],
    status: str = "DISCOVERED",
    signal_id: str | None = None,
) -> dict[str, Any]:
    if status not in STATUSES:
        raise ValueError(f"invalid status: {status}")
    sid = signal_id or make_signal_id(sport, market_family, conditions)
    now = datetime.now(timezone.utc).isoformat()
    doc = {
        "signal_id": sid,
        "sport": sport,
        "market_family": market_family,
        "conditions": conditions,
        # Windows / samples
        "train_window": metrics.get("train_window"),
        "validation_window": metrics.get("validation_window"),
        "test_window": metrics.get("test_window"),
        "train_n": int(metrics.get("train_n") or 0),
        "validation_n": int(metrics.get("validation_n") or 0),
        "test_n": int(metrics.get("test_n") or 0),
        "unique_events": int(metrics.get("unique_events") or 0),
        # Probabilities / metrics
        "baseline_probability": metrics.get("baseline_probability"),
        "observed_probability": metrics.get("observed_probability"),
        "lift": metrics.get("lift"),
        "brier": metrics.get("brier"),
        "log_loss": metrics.get("log_loss"),
        "calibration_gap": metrics.get("calibration_gap"),
        "confidence_interval": metrics.get("confidence_interval"),
        "stability": metrics.get("stability"),
        "false_discovery": metrics.get("false_discovery"),
        "wilson_lower": metrics.get("wilson_lower"),
        # Lifecycle
        "status": status,
        "last_evaluated": now,
        "provenance": "SHADOW_SIGNAL",
    }
    existing = await db[COLLECTION].find_one({"signal_id": sid}, {"_id": 0})
    if existing:
        # preserve created_at / validated_at where applicable
        doc["created_at"] = existing.get("created_at") or now
        if status in ("VALIDATED", "VERIFIED") and not existing.get("validated_at"):
            doc["validated_at"] = now
        elif existing.get("validated_at"):
            doc["validated_at"] = existing["validated_at"]
    else:
        doc["created_at"] = now
        if status in ("VALIDATED", "VERIFIED"):
            doc["validated_at"] = now
    await db[COLLECTION].update_one(
        {"signal_id": sid}, {"$set": doc}, upsert=True,
    )
    return doc


async def list_signals(
    sport: str | None = None,
    status: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    q: dict[str, Any] = {}
    if sport: q["sport"] = sport
    if status: q["status"] = status
    cursor = db[COLLECTION].find(q, {"_id": 0}).sort("last_evaluated", -1).limit(limit)
    return await cursor.to_list(length=limit)


async def transition(signal_id: str, status: str,
                     reason: str | None = None) -> dict[str, Any] | None:
    if status not in STATUSES:
        raise ValueError(status)
    now = datetime.now(timezone.utc).isoformat()
    update: dict[str, Any] = {"status": status, "last_evaluated": now}
    if reason: update["transition_reason"] = reason
    if status in ("VALIDATED", "VERIFIED"):
        update["validated_at"] = now
    await db[COLLECTION].update_one(
        {"signal_id": signal_id}, {"$set": update}
    )
    return await db[COLLECTION].find_one({"signal_id": signal_id}, {"_id": 0})
