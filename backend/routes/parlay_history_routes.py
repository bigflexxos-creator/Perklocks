"""HTTP routes for the Parlay-History (Save-on-Tap) feature.

Extracted from server.py during the 2026-06-24 monolith decomposition.
The data-layer module (`parlay_history.py`) is unchanged — these route
handlers just wire its functions onto FastAPI endpoints.

Mounted by `server.py` via `app.include_router(parlay_history_routes.router)`.
"""
from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import UserPublic
from deps import current_user, db, logger, strip_mongo

router = APIRouter(prefix="/api")


class SaveParlayRequest(BaseModel):
    legs: list[dict]                # the full pick objects from /api/picks/parlay
    mode: str = "standard"
    stake: float = 1.0
    # Phase 3G Step 6 — optional idempotency handle.  Old clients that
    # omit this field fall back to the ledger's deterministic
    # idempotency_key (computed from user_id + sorted leg identities).
    client_bet_id: Optional[str] = None


@router.post("/parlay/save")
async def parlay_save(
    req: SaveParlayRequest,
    user: Annotated[UserPublic, Depends(current_user)],
):
    """Save a parlay ticket.

    Phase 3G Step 7 semantics (final cutover):
      1. Canonical write via ``UserBetLedger.create_parlay`` — only.
      2. The compatibility mirror into ``parlay_history`` is SUNSET —
         no rows are written there for new user-owned parlays.
      3. Response envelope is identical to the pre-Step-6 shape,
         serialized directly from the canonical row via
         :func:`user_bet_ledger.serialize_parlay_history_row`.
      4. Legacy alias fields (``pick_id``, ``bet_type``,
         ``parlay_legs``, ``stake_units``, ``odds_at_bet``,
         ``pnl_units``, denormalized ``sport``/``market``/
         ``event``/``selection``) are stamped onto the canonical
         ``user_bets`` document so the existing settlement engine
         and per-user analytics endpoints keep working byte-for-byte.

    Learning-loop rows (``plearn_*``) are UNTOUCHED — never mirrored,
    never migrated, never resolved by this route.
    """
    try:
        from services import user_bet_ledger as _UBL

        # Validate input at the same threshold the legacy path used.
        if not isinstance(req.legs, list) or len(req.legs) < 2:
            raise HTTPException(400, "Parlays require at least 2 legs")

        # ── Step 1 — canonical write via UserBetLedger ─────────────
        # Extract leg identities (pick_id, sport, market, selection,
        # book_odds).  Never require display strings for identity.
        canonical_legs = []
        for L in req.legs:
            if not isinstance(L, dict):
                continue
            canonical_legs.append(_UBL.UserBetLeg(
                prediction_id=(L.get("id") or L.get("pick_id")),
                sport_key=L.get("sport"),
                market=L.get("market"),
                selection=L.get("selection"),
                side=L.get("selection"),
                original_odds=(int(L.get("book_odds")) if L.get("book_odds") is not None else None),
                line=(float(L.get("line")) if L.get("line") is not None else None),
                event_id=L.get("event_id"),
            ))
        canonical_req = _UBL.UserBetCreateRequest(
            user_id=user.id,
            wager_type=_UBL.WAGER_TYPE_PARLAY,
            stake_amount=float(req.stake),
            stake_units=float(req.stake),
            client_bet_id=req.client_bet_id,
            source="user_track",
            mode=req.mode,
            legs=canonical_legs,
        )
        canonical_result = await _UBL.create_parlay(canonical_req)

        # ── Step 2 (Step 7 cutover) — mirror SUNSET ────────────────
        # New user-owned parlays no longer write to parlay_history.
        # Existing legacy p_* rows and pre-Step-7 mirror rows in
        # parlay_history remain untouched.  Learning-loop plearn_*
        # rows are untouched (parlay_learning writes them directly).
        #
        # ── Legacy-alias stamping (Step 7 settlement/analytics parity)
        # The settlement propagator (:func:`propagate_pick_settlement`)
        # and per-user analytics endpoints predate the canonical schema
        # and match/aggregate on legacy field names.  We stamp those
        # aliases on the fresh canonical row so both paths keep working
        # byte-for-byte against parlays created via this route.  We use
        # a conditional update so we NEVER clobber a value a settler /
        # admin has already written (idempotent + race-safe).
        leg_pick_ids = [L.prediction_id for L in canonical_result.bet.legs
                        if L.prediction_id]
        leg_first = (req.legs[0] if isinstance(req.legs, list) and req.legs else {}) or {}
        legs_ct = len(leg_pick_ids)
        legacy_aliases = {
            "id":          canonical_result.bet.user_bet_id,
            "pick_id":     (leg_pick_ids[0] if leg_pick_ids else None),
            "bet_type":    "parlay",
            "parlay_legs": leg_pick_ids,
            "stake_units": float(req.stake),
            "odds_at_bet": (int(canonical_result.bet.combined_odds)
                            if canonical_result.bet.combined_odds is not None else None),
            "pnl_units":   0.0,
            "sport":       leg_first.get("sport"),
            "market":      f"{legs_ct}-leg parlay",
            "event":       " + ".join(str(L.get("event", "") or "")
                                       for L in req.legs[:3] if isinstance(L, dict)),
            "selection":   " · ".join(str(L.get("selection", "") or "")
                                       for L in req.legs[:3] if isinstance(L, dict)),
        }
        for k, v in legacy_aliases.items():
            await db.user_bets.update_one(
                {"user_bet_id": canonical_result.bet.user_bet_id,
                 "$or": [{k: {"$exists": False}}, {k: None}]},
                {"$set": {k: v}},
            )

        # Return the canonical wager serialized in the exact legacy
        # response envelope so the frontend contract is preserved.
        return _UBL.serialize_parlay_history_row(canonical_result.bet)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.exception("save_parlay failed")
        raise HTTPException(500, f"save failed: {e}")


@router.get("/parlay/history")
async def parlay_history_list(
    user: Annotated[UserPublic, Depends(current_user)],
    filter: Optional[str] = None,
    limit: int = 50,
):
    """List the user's saved parlays. `filter` = won | live | lost | all.

    Phase 3G Step 7: reads from the canonical UserBetLedger.
    plearn_* rows can never appear here (user_bets scope only).
    Migrated legacy parlays already inserted into user_bets show up
    exactly once; the legacy parlay_history row (if any) is suppressed
    from the response to avoid duplicate display.
    """
    try:
        from services import user_bet_ledger as _UBL
        rows = await _UBL.list_parlays_history_shape(
            db, user_id=user.id, status_filter=filter, limit=limit,
        )
        return {"parlays": rows, "count": len(rows)}
    except Exception as e:
        raise HTTPException(500, f"list failed: {e}")


@router.get("/parlay/{parlay_id}")
async def parlay_detail(
    parlay_id: str,
    user: Annotated[UserPublic, Depends(current_user)],
):
    from parlay_history import get_parlay
    doc = await get_parlay(db, user_id=user.id, parlay_id=parlay_id)
    if not doc:
        raise HTTPException(404, "parlay not found")
    return doc


@router.delete("/parlay/{parlay_id}")
async def parlay_remove(
    parlay_id: str,
    user: Annotated[UserPublic, Depends(current_user)],
):
    from parlay_history import delete_parlay
    ok = await delete_parlay(db, user_id=user.id, parlay_id=parlay_id)
    return {"deleted": ok}


@router.post("/parlay/{parlay_id}/resettle")
async def parlay_resettle(
    parlay_id: str,
    user: Annotated[UserPublic, Depends(current_user)],
):
    """Force-run the leg-resolution chain (pick lookup → snapshot match
    → external adapter) for a single parlay. Returns the updated doc.

    Useful when the user sees a stuck 'pending' leg and doesn't want to
    wait for the periodic settler."""
    try:
        from parlay_history import resettle_parlay
        doc = await resettle_parlay(db, user_id=user.id, parlay_id=parlay_id)
        return strip_mongo(doc)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        logger.exception("resettle_parlay failed")
        raise HTTPException(500, f"resettle failed: {e}")


# ═════════════════════════════════════════════════════════════════════
# Phase 5 · Parlay Intelligence backtest snapshot
# ═════════════════════════════════════════════════════════════════════
@router.get("/parlay/intelligence/backtest")
async def parlay_intelligence_backtest(
    user: Annotated[UserPublic, Depends(current_user)],
    days: int = 60,
    persist: bool = False,
):
    """Return the parlay intelligence backtest report.

    Aggregates the last N days of settled parlays from `parlay_history`
    into win-rate, best-combo, common-losing-leg, and confidence
    calibration metrics. Read-only unless `persist=True` (admin snapshot)."""
    try:
        from services.parlay_intelligence import backtest_parlays, summarize_backtest
        days_clamped = max(7, min(365, int(days or 60)))
        report = await backtest_parlays(
            db, days=days_clamped, persist=bool(persist),
        )
        return {
            "report": report,
            "bullets": summarize_backtest(report),
        }
    except Exception as e:
        logger.exception("parlay backtest failed")
        raise HTTPException(500, f"backtest failed: {e}")


@router.get("/parlay/intelligence/reliability")
async def parlay_intelligence_reliability(
    user: Annotated[UserPublic, Depends(current_user)],
    min_samples: int = 5,
):
    """Return the current parlay leg reliability map (per sport/family).
    Populated by the learning loop as parlays settle."""
    try:
        rows = await db.parlay_leg_reliability.find(
            {}, {"_id": 0},
        ).to_list(length=500)
        # Filter down to rows past the sample gate
        filtered = [r for r in rows
                    if int(r.get("n_total") or 0) >= int(min_samples or 0)]
        filtered.sort(key=lambda r: (r.get("hit_rate") or 0), reverse=True)
        return {"rows": filtered, "count": len(filtered)}
    except Exception as e:
        raise HTTPException(500, f"reliability read failed: {e}")
