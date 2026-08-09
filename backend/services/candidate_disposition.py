"""Lightweight candidate-disposition accountability (Phase 1).

Purpose
-------
When a legitimate betting opportunity does not reach the main Locks
board, we need to be able to answer *why* without opening a code
audit.  Every stage of the candidate lifecycle can call
:func:`record_disposition` with a concise stage + reason and, if the
candidate was rejected, a stable reason code.

Design decisions
----------------
* One tiny Mongo collection ``candidate_dispositions`` (bounded
  capped-collection-friendly shape).
* One document per stage transition.  We do NOT overwrite the row —
  we append, so the full lineage is auditable.
* We deliberately do NOT store full pick payloads (Phase 1 mandate:
  "Do not store massive debug payloads").
* Rejection reason codes are a fixed enum; free-form ``detail`` is
  capped at 240 chars.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

# ── Lifecycle stages ─────────────────────────────────────────────────
STAGE_DISCOVERED     = "discovered"       # provider returned an event/market
STAGE_INGESTED       = "ingested"         # raw doc written to a staging store
STAGE_NORMALIZED     = "normalized"       # market string / line resolved
STAGE_EVALUATED      = "evaluated"        # model + lock_score computed
STAGE_ACCEPTED       = "accepted"         # cleared all gates for insertion
STAGE_REJECTED       = "rejected"         # dropped somewhere in pipeline
STAGE_PUBLISHED      = "published"        # PredictionPublicationService emitted
STAGE_BOARD_ELIGIBLE = "board_eligible"   # cleared >85 + all board filters


# ── Standard rejection reason codes (Phase-1 dictionary) ─────────────
REASON_MARKET_NOT_DISCOVERED   = "market_not_discovered"
REASON_UNSUPPORTED_MARKET      = "unsupported_market"
REASON_NORMALIZATION_FAILED    = "normalization_failed"
REASON_INSUFFICIENT_DATA       = "insufficient_data"
REASON_MODEL_REJECTED          = "model_rejected"
REASON_LINE_STALE              = "line_stale"
REASON_LINEUP_UNCERTAIN        = "lineup_uncertain"
REASON_LOCK_SCORE_BELOW_FLOOR  = "lock_score_below_board_threshold"
REASON_PUBLICATION_FAILED      = "publication_failed"
REASON_DUPLICATE               = "duplicate"
REASON_NO_BET                  = "no_bet"

ALL_REASONS = {
    REASON_MARKET_NOT_DISCOVERED, REASON_UNSUPPORTED_MARKET,
    REASON_NORMALIZATION_FAILED, REASON_INSUFFICIENT_DATA,
    REASON_MODEL_REJECTED, REASON_LINE_STALE, REASON_LINEUP_UNCERTAIN,
    REASON_LOCK_SCORE_BELOW_FLOOR, REASON_PUBLICATION_FAILED,
    REASON_DUPLICATE, REASON_NO_BET,
}

ALL_STAGES = {
    STAGE_DISCOVERED, STAGE_INGESTED, STAGE_NORMALIZED, STAGE_EVALUATED,
    STAGE_ACCEPTED, STAGE_REJECTED, STAGE_PUBLISHED, STAGE_BOARD_ELIGIBLE,
}


async def record_disposition(
    db,
    *,
    candidate_key: str,
    stage: str,
    sport: Optional[str] = None,
    market: Optional[str] = None,
    lock_score: Optional[float] = None,
    reason: Optional[str] = None,
    detail: Optional[str] = None,
) -> None:
    """Append one lightweight disposition record.

    ``candidate_key`` is any stable string that identifies the
    opportunity across stages — usually the pick id, or a pre-pick
    hash of (event_id, market_key, selection).

    Never raises: disposition logging is best-effort.
    """
    if stage not in ALL_STAGES:
        stage = STAGE_REJECTED
    if reason is not None and reason not in ALL_REASONS:
        # Unknown reason still recorded but tagged for later cleanup.
        reason = f"other:{reason[:60]}"
    doc = {
        "candidate_key": candidate_key,
        "stage": stage,
        "sport": sport,
        "market": (market or None) and str(market)[:120],
        "lock_score": lock_score,
        "reason": reason,
        "detail": (detail or None) and str(detail)[:240],
        "ts": datetime.now(timezone.utc),
    }
    try:
        await db.candidate_dispositions.insert_one(doc)
    except Exception:
        return


async def why_missing(db, candidate_key: str) -> list[dict]:
    """Return the ordered disposition trail for a candidate.
    Convenience for ops debugging.  Newest last."""
    try:
        cur = db.candidate_dispositions.find(
            {"candidate_key": candidate_key}, {"_id": 0}
        ).sort("ts", 1)
        return [d async for d in cur]
    except Exception:
        return []


__all__ = [
    "record_disposition", "why_missing",
    "STAGE_DISCOVERED", "STAGE_INGESTED", "STAGE_NORMALIZED",
    "STAGE_EVALUATED", "STAGE_ACCEPTED", "STAGE_REJECTED",
    "STAGE_PUBLISHED", "STAGE_BOARD_ELIGIBLE",
    "REASON_MARKET_NOT_DISCOVERED", "REASON_UNSUPPORTED_MARKET",
    "REASON_NORMALIZATION_FAILED", "REASON_INSUFFICIENT_DATA",
    "REASON_MODEL_REJECTED", "REASON_LINE_STALE",
    "REASON_LINEUP_UNCERTAIN", "REASON_LOCK_SCORE_BELOW_FLOOR",
    "REASON_PUBLICATION_FAILED", "REASON_DUPLICATE", "REASON_NO_BET",
    "ALL_REASONS", "ALL_STAGES",
]
