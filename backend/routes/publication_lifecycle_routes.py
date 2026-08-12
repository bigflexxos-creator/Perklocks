"""Admin routes — Publication Lifecycle (Session A 2026-06).

READ-ONLY endpoint that exposes publication lifecycle counters and
producer health telemetry so operators can diagnose stuck picks and
silent producer failures.

Endpoint
────────
GET /api/admin/publication/lifecycle
    Returns counters + oldest pending age + recent failures +
    rejection-reason counts + per-producer health.

Every response is admin-only.  No secrets, API keys, or raw provider
payloads are exposed.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, Query

from auth import UserPublic
from deps import current_admin, db as _db
from services.canonical_publication_boundary import PublicationState
from services.producer_health import summary as producer_health_summary


router = APIRouter(
    prefix="/api/admin/publication",
    tags=["publication_lifecycle"],
)


def _iso_age_seconds(iso_str: Optional[str]) -> Optional[int]:
    if not iso_str:
        return None
    try:
        # Accept trailing Z or +00:00.
        s = str(iso_str).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int((datetime.now(timezone.utc) - dt).total_seconds())
    except Exception:
        return None


@router.get("/lifecycle")
async def publication_lifecycle(
    user: Annotated[UserPublic, Depends(current_admin)],
    recent_limit: int = Query(20, ge=1, le=200),
):
    """Return a compact publication lifecycle observability payload.

    Admin-only.  Never exposes secrets, API keys, or raw provider
    payloads (see docstring at module top).
    """
    picks_col = _db.picks

    # ── Counters ──────────────────────────────────────────────────
    counts: dict[str, int] = {}
    for state in PublicationState:
        try:
            counts[state.value] = await picks_col.count_documents(
                {"publication_state": state.value},
            )
        except Exception:
            counts[state.value] = 0

    unmarked = 0
    try:
        unmarked = await picks_col.count_documents(
            {"publication_state": {"$exists": False}},
        )
    except Exception:
        pass
    counts["UNMARKED"] = int(unmarked)

    # ── Oldest pending age (seconds) ───────────────────────────────
    oldest_pending: dict[str, Any] = {"age_seconds": None, "id": None}
    try:
        cur = picks_col.find(
            {"publication_state":
                PublicationState.PUBLICATION_PENDING.value},
            projection={"id": 1, "publication_last_state_at": 1, "_id": 0},
        ).sort("publication_last_state_at", 1).limit(1)
        rows = await cur.to_list(length=1)
        if rows:
            r = rows[0]
            oldest_pending = {
                "id": r.get("id"),
                "age_seconds": _iso_age_seconds(
                    r.get("publication_last_state_at"),
                ),
                "since": r.get("publication_last_state_at"),
            }
    except Exception:
        pass

    # ── Recent failures (redacted) ────────────────────────────────
    recent_failures: list[dict] = []
    try:
        cur = picks_col.find(
            {"publication_state": PublicationState.FAILED.value},
            projection={
                "id": 1,
                "publication_source": 1,
                "publication_last_state_at": 1,
                "publication_last_error": 1,
                "publication_attempts": 1,
                "_id": 0,
            },
        ).sort("publication_last_state_at", -1).limit(int(recent_limit))
        recent_failures = await cur.to_list(length=int(recent_limit))
    except Exception:
        pass

    # ── Rejection reason counts ────────────────────────────────────
    reason_counts: dict[str, int] = {}
    try:
        pipeline = [
            {"$match": {
                "publication_state": PublicationState.REJECTED.value,
                "publication_rejection_reasons": {"$type": "array"},
            }},
            {"$unwind": "$publication_rejection_reasons"},
            {"$group": {
                "_id": "$publication_rejection_reasons",
                "count": {"$sum": 1},
            }},
            {"$sort": {"count": -1}},
            {"$limit": 30},
        ]
        async for row in picks_col.aggregate(pipeline):
            k = row.get("_id")
            if isinstance(k, str):
                reason_counts[k] = int(row.get("count") or 0)
    except Exception:
        pass

    # ── Producer health ────────────────────────────────────────────
    try:
        producers = await producer_health_summary(_db)
    except Exception:
        producers = []

    # Last successful publication by producer.
    last_success_by_producer: dict[str, Optional[str]] = {}
    for row in producers:
        src = row.get("publication_source")
        if src:
            last_success_by_producer[src] = row.get("last_success_at")

    # Session B — reconciler scheduler status (safe, in-memory only).
    try:
        from services.publication_reconciler_scheduler import (
            status as _reconciler_status,
        )
        reconciler_status = _reconciler_status()
    except Exception:
        reconciler_status = {"state": "unavailable"}

    # Session B — Soccer capability registry summary (read-only, no
    # secrets).  Full matrix available at
    # /api/admin/publication/soccer-capabilities (see below).
    try:
        from services.soccer_capability_registry import summary as _soccer_summary
        soccer_capability_summary = _soccer_summary()
    except Exception:
        soccer_capability_summary = {}

    return {
        "ok": True,
        "now": datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z"),
        "counts": {
            "pending":     counts.get(
                PublicationState.PUBLICATION_PENDING.value, 0),
            "published":   counts.get(
                PublicationState.PUBLISHED.value, 0),
            "rejected":    counts.get(
                PublicationState.REJECTED.value, 0),
            "failed":      counts.get(
                PublicationState.FAILED.value, 0),
            "unmarked":    counts.get("UNMARKED", 0),
        },
        "oldest_pending":         oldest_pending,
        "recent_failures":        recent_failures,
        "rejection_reason_counts": reason_counts,
        "producer_health":        producers,
        "last_success_by_producer": last_success_by_producer,
        "reconciler":             reconciler_status,
        "soccer_capability_summary": soccer_capability_summary,
    }


@router.get("/soccer-capabilities")
async def soccer_capability_matrix(
    user: Annotated[UserPublic, Depends(current_admin)],
):
    """Session B — full Soccer × Market capability matrix.

    Read-only.  No secrets.  Each league entry includes per-market
    capability status (REAL_VERIFIED / NO_CURRENT_EVENTS /
    UNAVAILABLE / CURRENT_PROVIDER_UNAVAILABLE / UNVERIFIED),
    the-odds-api sport-key (or null when the provider does not
    carry the league), fixture support, identity source, roster
    source, scorer/form source, player history, team history,
    sportsbook provider tag, verification timestamp, and free-form
    notes.  This is what operators consult before shipping a
    new soccer market or debugging why a producer isn't
    surfacing a league.
    """
    from services.soccer_capability_registry import matrix, MARKET_KEYS
    return {
        "ok":          True,
        "market_keys": list(MARKET_KEYS),
        "leagues":     matrix(),
    }
