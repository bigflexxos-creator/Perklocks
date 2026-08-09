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


# ── Central boundary hook (Phase 1 Final Closure) ───────────────────
# One tiny helper called from ``pick_refresh_orchestrator`` right after
# ``publish_batch()``.  Emits the full lifecycle trail for the batch
# without scattering ``record_disposition`` calls across sport-specific
# writers.  Best-effort: never raises.
async def record_batch_dispositions(
    db,
    picks: list[dict],
    *,
    publication_summary: Optional[dict] = None,
) -> dict:
    """Emit ``evaluated``/``accepted``/``rejected``/``published``/
    ``board_eligible`` for every pick in ``picks`` — using the pick's
    own tags to determine which trail to write.

    Rules:
      * Every pick that reached this point gets ``evaluated``.
      * ``no_bet=True``          ⇒ ``rejected`` (reason=no_bet).
      * ``off_board=True``       ⇒ ``rejected`` (reason inferred from
                                    ``off_board_reasons`` — falls back
                                    to ``lock_score_below_board_threshold``).
      * Otherwise                ⇒ ``accepted``.
      * If the pick's ``id`` appears as a *successful* publish result
        in ``publication_summary``                              ⇒ ``published``.
        (When ``publication_summary`` is omitted we still emit
        ``published`` for every accepted pick since ``publish_batch``
        was called on the same list — the summary is used only to
        skip picks that publication genuinely rejected.)
      * If ``services.main_board_eligibility.is_main_board_eligible``
        returns True                                            ⇒ ``board_eligible``.

    Returns a summary dict for logging: counts per stage.
    """
    from services.main_board_eligibility import is_main_board_eligible  # local

    errored_ids: set = set()
    if publication_summary and isinstance(publication_summary, dict):
        for err in publication_summary.get("errors") or []:
            pid = err.get("prediction_id")
            if pid:
                errored_ids.add(str(pid))

    stats = {
        "evaluated": 0, "accepted": 0, "rejected": 0,
        "published": 0, "board_eligible": 0, "errors": 0,
    }
    for p in picks or []:
        try:
            pid = str(p.get("id") or p.get("prediction_id") or "")
            if not pid:
                continue
            sport = p.get("sport")
            market = p.get("market") or (
                (p.get("selection_v2") or {}).get("market", {}).get("family")
                if isinstance(p.get("selection_v2"), dict) else None
            )
            try:
                ls = float(p.get("lock_score") or 0.0)
            except (TypeError, ValueError):
                ls = 0.0

            # 1. evaluated — every candidate that reached the batch.
            await record_disposition(
                db, candidate_key=pid, stage=STAGE_EVALUATED,
                sport=sport, market=market, lock_score=ls,
            )
            stats["evaluated"] += 1

            # 2. rejected vs accepted (based on final pick tags).
            is_rejected = False
            reason = None
            detail = None
            if p.get("no_bet") is True:
                is_rejected = True
                reason = REASON_NO_BET
                detail = p.get("no_bet_reason")
            elif p.get("off_board") is True:
                is_rejected = True
                # Try to map an off_board_reason to a canonical enum;
                # otherwise fall back to the lock-floor code because
                # off_board almost always means "below the >85 board".
                reasons_list = p.get("off_board_reasons") or []
                first = (reasons_list[0] if reasons_list else "").lower()
                if first.startswith("lock<"):
                    reason = REASON_LOCK_SCORE_BELOW_FLOOR
                elif first == "no_bet":
                    reason = REASON_NO_BET
                elif first == "validation_block":
                    reason = REASON_MODEL_REJECTED
                elif first in {"chalk_trap", "longshot_trap"}:
                    reason = REASON_MODEL_REJECTED
                else:
                    reason = REASON_LOCK_SCORE_BELOW_FLOOR
                detail = ",".join(str(r) for r in reasons_list)[:200] or None
            elif p.get("validation_block") is True:
                is_rejected = True
                reason = REASON_MODEL_REJECTED
                detail = "validation_block"

            if is_rejected:
                await record_disposition(
                    db, candidate_key=pid, stage=STAGE_REJECTED,
                    sport=sport, market=market, lock_score=ls,
                    reason=reason, detail=detail,
                )
                stats["rejected"] += 1
                # Rejected candidates do not get published/board_eligible.
                continue

            # 3. accepted — cleared all in-batch gates.
            await record_disposition(
                db, candidate_key=pid, stage=STAGE_ACCEPTED,
                sport=sport, market=market, lock_score=ls,
            )
            stats["accepted"] += 1

            # 4. published — publish_batch attempted; skip if summary
            #    explicitly reports this candidate errored.
            if pid in errored_ids:
                await record_disposition(
                    db, candidate_key=pid, stage=STAGE_REJECTED,
                    sport=sport, market=market, lock_score=ls,
                    reason=REASON_PUBLICATION_FAILED,
                )
                stats["rejected"] += 1
                continue

            await record_disposition(
                db, candidate_key=pid, stage=STAGE_PUBLISHED,
                sport=sport, market=market, lock_score=ls,
            )
            stats["published"] += 1

            # 5. board_eligible — cleared the ``>85`` Locks contract.
            if is_main_board_eligible(p):
                await record_disposition(
                    db, candidate_key=pid, stage=STAGE_BOARD_ELIGIBLE,
                    sport=sport, market=market, lock_score=ls,
                )
                stats["board_eligible"] += 1
        except Exception:
            stats["errors"] += 1
            continue
    return stats


__all__ = [
    "record_disposition", "why_missing", "record_batch_dispositions",
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
