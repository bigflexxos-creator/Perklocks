"""User Bets Routes — personal bet tracking + user-scoped analytics.

USER MANDATE (2026-07-21): "Users can only see their own saved bets.
Users can track only picks they personally selected. Users can view:
Straight bets, Parlays, Units risked, Wins/losses, Personal ROI,
Personal performance by sport and market, Betting history."

DESIGN:
    • New Mongo collection `user_bets` — one document per bet tracked
      by a user. Users insert on tap, delete on untap. Auto-settled
      when the linked pick's status flips to won/lost/push (handled by
      the settlement propagator below).
    • Every endpoint here is scoped to `current_user.id` at the DB
      query level — impossible for a user to see another user's bets
      even by tampering with the request.
    • Admin analytics (in `analytics_routes.py`) remain admin-only via
      `require_admin_user`. This module intentionally does NOT expose
      any aggregated cross-user analytics.

COLLECTION SCHEMA (`user_bets`):
    {
      id:            str  (UUID, primary key)
      user_id:       str  (foreign key → users.id)
      pick_id:       str  (foreign key → picks.id)
      bet_type:      "straight" | "parlay"
      parlay_legs:   list[pick_id]  (only for bet_type=parlay)
      stake_units:   float  (0.25 / 0.5 / 1.0 / 1.5 / 2.0 / custom)
      odds_at_bet:   int    (American odds at time of tracking)
      status:        "pending" | "won" | "lost" | "push"
      pnl_units:     float  (0 while pending; set on settle)
      sport:         str    (denormalized from pick for filtering)
      market:        str    (denormalized from pick for filtering)
      event:         str    (denormalized display string)
      selection:     str    (denormalized display string)
      created_at:    datetime  (UTC)
      settled_at:    datetime | None
      notes:         str | None  (optional user note, capped 500 chars)
    }

INDEXES (created lazily at first insert):
    • (user_id, status, created_at DESC)   — /user/bets list + history
    • (user_id, sport)                     — by-sport analytics
    • (pick_id)                             — settlement propagator
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from auth import UserPublic
from deps import current_user, db
from services import user_bet_ledger as UBL

logger = logging.getLogger("lockscore.user_bets")
router = APIRouter(prefix="/api")

_INDEXES_ENSURED = False


async def _ensure_indexes() -> None:
    """Create the user_bets indexes once per process."""
    global _INDEXES_ENSURED
    if _INDEXES_ENSURED:
        return
    try:
        await db.user_bets.create_index([("user_id", 1), ("status", 1), ("created_at", -1)])
        await db.user_bets.create_index([("user_id", 1), ("sport", 1)])
        await db.user_bets.create_index([("user_id", 1), ("market", 1)])
        await db.user_bets.create_index([("pick_id", 1)])
        _INDEXES_ENSURED = True
    except Exception as e:  # noqa: BLE001
        logger.warning("user_bets index creation failed (non-fatal): %s", e)


# ── Request / response schemas ───────────────────────────────────────
class TrackBetRequest(BaseModel):
    pick_id: str = Field(..., min_length=1, max_length=200)
    bet_type: str = Field(default="straight", pattern=r"^(straight|parlay)$")
    stake_units: float = Field(default=1.0, ge=0.05, le=100.0)
    parlay_legs: list[str] = Field(default_factory=list, max_length=20)
    notes: Optional[str] = Field(default=None, max_length=500)
    # Phase 3G Step 6 — optional idempotency handle. Old clients that
    # omit this field fall back to server-computed idempotency_key.
    client_bet_id: Optional[str] = Field(default=None, min_length=1, max_length=200)


class TrackedBet(BaseModel):
    id: str
    user_id: str
    pick_id: str
    bet_type: str
    parlay_legs: list[str]
    stake_units: float
    odds_at_bet: Optional[int]
    status: str
    pnl_units: float
    sport: Optional[str]
    market: Optional[str]
    event: Optional[str]
    selection: Optional[str]
    created_at: datetime
    settled_at: Optional[datetime]
    notes: Optional[str]


# ── Helpers ──────────────────────────────────────────────────────────
def _american_to_profit(odds: Optional[int], stake: float) -> float:
    """Profit on `stake` units at American odds `odds`. 0 if invalid."""
    if odds is None:
        return 0.0
    try:
        o = float(odds)
    except (TypeError, ValueError):
        return 0.0
    if o >= 100:
        return stake * (o / 100.0)
    if o <= -100:
        return stake * (100.0 / (-o))
    return 0.0


def _parlay_combined_odds(leg_odds: list[int]) -> int:
    """Combine leg odds into a single American parlay price."""
    dec = 1.0
    for o in leg_odds:
        if o >= 100:
            dec *= (o / 100.0) + 1.0
        else:
            dec *= (100.0 / -o) + 1.0
    if dec <= 1.0:
        return 100
    if dec >= 2.0:
        return int(round((dec - 1.0) * 100.0))
    return int(round(-100.0 / (dec - 1.0)))


# ── Endpoints ────────────────────────────────────────────────────────
@router.post("/user/bets/track", response_model=TrackedBet)
async def track_bet(
    payload: TrackBetRequest,
    user: Annotated[UserPublic, Depends(current_user)],
) -> dict[str, Any]:
    """Track a bet the user is placing. Fetches the pick(s) from the
    board to snapshot odds/sport/market at bet time (so a later re-
    price on the book doesn't change historical ROI)."""
    await _ensure_indexes()

    # Load the primary pick (straight bet) or first leg (parlay)
    primary = await db.picks.find_one({"id": payload.pick_id}, {"_id": 0})
    if not primary:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Pick not found")

    # Handle parlay: fetch all legs
    parlay_leg_docs: list[dict] = []
    combined_odds: Optional[int] = primary.get("book_odds")
    combined_event = primary.get("event")
    combined_market = primary.get("market")
    combined_selection = primary.get("selection")

    if payload.bet_type == "parlay":
        if not payload.parlay_legs or len(payload.parlay_legs) < 2:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="Parlays require at least 2 legs (pick IDs)",
            )
        parlay_leg_docs = await db.picks.find(
            {"id": {"$in": payload.parlay_legs}}, {"_id": 0}
        ).to_list(20)
        if len(parlay_leg_docs) != len(payload.parlay_legs):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=f"Some parlay legs not found ({len(parlay_leg_docs)}/{len(payload.parlay_legs)})",
            )
        leg_prices = [int(p.get("book_odds") or 0) for p in parlay_leg_docs if p.get("book_odds")]
        if leg_prices:
            combined_odds = _parlay_combined_odds(leg_prices)
        combined_event = " + ".join(p.get("event", "") for p in parlay_leg_docs[:3])
        combined_market = f"{len(parlay_leg_docs)}-leg parlay"
        combined_selection = " · ".join(p.get("selection", "") for p in parlay_leg_docs[:3])

    # Legacy alias fields the frontend/reader expects (preserved
    # verbatim from the pre-Step-6 shape).
    legacy_aliases: dict[str, Any] = {
        "pick_id":     payload.pick_id,
        "bet_type":    payload.bet_type,
        "parlay_legs": payload.parlay_legs if payload.bet_type == "parlay" else [],
        "stake_units": float(payload.stake_units),
        "odds_at_bet": int(combined_odds) if combined_odds is not None else None,
        "pnl_units":   0.0,
        "sport":       primary.get("sport"),
        "market":      combined_market,
        "event":       combined_event,
        "selection":   combined_selection,
        "notes":       payload.notes,
    }

    # ─── Phase 3G Step 6 — canonical write through UserBetLedger ────
    if payload.bet_type == "parlay":
        req = UBL.UserBetCreateRequest(
            user_id=user.id,
            wager_type=UBL.WAGER_TYPE_PARLAY,
            stake_amount=float(payload.stake_units),
            stake_units=float(payload.stake_units),
            odds=(int(combined_odds) if combined_odds is not None else None),
            combined_odds=(int(combined_odds) if combined_odds is not None else None),
            client_bet_id=payload.client_bet_id,
            source="user_track",
            mode=None,
            sport_key=primary.get("sport"),
            prediction_id=None,
            notes=payload.notes,
            legs=[
                UBL.UserBetLeg(
                    prediction_id=(p.get("id") or None),
                    sport_key=p.get("sport"),
                    market=p.get("market"),
                    selection=p.get("selection"),
                    side=p.get("selection"),
                    original_odds=(int(p.get("book_odds")) if p.get("book_odds") is not None else None),
                    line=(float(p.get("line")) if p.get("line") is not None else None),
                    event_id=p.get("event_id"),
                )
                for p in parlay_leg_docs
            ],
        )
        result = await UBL.create_parlay(req)
    else:
        req = UBL.UserBetCreateRequest(
            user_id=user.id,
            wager_type=UBL.WAGER_TYPE_STRAIGHT,
            stake_amount=float(payload.stake_units),
            stake_units=float(payload.stake_units),
            odds=(int(combined_odds) if combined_odds is not None else None),
            client_bet_id=payload.client_bet_id,
            source="user_track",
            prediction_id=payload.pick_id,
            sport_key=primary.get("sport"),
            notes=payload.notes,
        )
        result = await UBL.create_bet(req)

    # Stamp the legacy alias fields onto the canonical row so the
    # existing reader response envelope is byte-for-byte preserved.
    # Uses ``$setOnInsert``-style semantics per field: an update guarded
    # by the LEGACY key's absence, so retries never clobber values that
    # a settler / admin later modified.
    for k, v in legacy_aliases.items():
        await db.user_bets.update_one(
            {"user_bet_id": result.bet.user_bet_id,
             "$or": [{k: {"$exists": False}}, {k: None}]},
            {"$set": {k: v}},
        )
    # ``id`` is the primary legacy alias — mirror it to the canonical
    # user_bet_id so legacy DELETE by id keeps working.
    await db.user_bets.update_one(
        {"user_bet_id": result.bet.user_bet_id,
         "$or": [{"id": {"$exists": False}}, {"id": None}]},
        {"$set": {"id": result.bet.user_bet_id}},
    )

    # Read the row back so the response envelope is 100 % byte-parity
    # with the pre-Step-6 behaviour.
    doc = await db.user_bets.find_one(
        {"user_bet_id": result.bet.user_bet_id}, {"_id": 0}
    ) or {}
    return doc


@router.get("/user/bets")
async def list_user_bets(
    user: Annotated[UserPublic, Depends(current_user)],
    status_filter: Optional[str] = None,
    sport: Optional[str] = None,
    limit: int = 100,
) -> dict[str, Any]:
    """List the caller's tracked bets. Optional filters: status, sport."""
    await _ensure_indexes()
    q: dict[str, Any] = {"user_id": user.id}
    if status_filter in ("pending", "won", "lost", "push"):
        q["status"] = status_filter
    if sport:
        q["sport"] = sport
    limit = max(1, min(limit, 500))
    cursor = db.user_bets.find(q, {"_id": 0}).sort("created_at", -1).limit(limit)
    bets = await cursor.to_list(limit)
    return {"bets": bets, "count": len(bets)}


@router.delete("/user/bets/{bet_id}")
async def delete_user_bet(
    bet_id: str,
    user: Annotated[UserPublic, Depends(current_user)],
) -> dict[str, Any]:
    """Delete a tracked bet. Only pending bets can be un-tracked —
    settled bets are historical record and stay."""
    bet = await db.user_bets.find_one({"id": bet_id, "user_id": user.id})
    if not bet:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Bet not found")
    if bet.get("status") != "pending":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="Can't delete a settled bet (historical record)",
        )
    await db.user_bets.delete_one({"id": bet_id, "user_id": user.id})
    return {"ok": True, "deleted_id": bet_id}


@router.get("/user/analytics/summary")
async def user_analytics_summary(
    user: Annotated[UserPublic, Depends(current_user)],
) -> dict[str, Any]:
    """Personal ROI summary — W-L-P, units risked, PnL, ROI %."""
    await _ensure_indexes()
    bets = await db.user_bets.find(
        {"user_id": user.id}, {"_id": 0}
    ).to_list(5000)

    total = len(bets)
    won = lost = push = pending = 0
    units_risked = 0.0
    pnl = 0.0
    for b in bets:
        st = b.get("status")
        stake = float(b.get("stake_units") or 0.0)
        p = float(b.get("pnl_units") or 0.0)
        if st == "won":
            won += 1
            units_risked += stake
            pnl += p
        elif st == "lost":
            lost += 1
            units_risked += stake
            pnl += p  # already negative
        elif st == "push":
            push += 1
        elif st == "pending":
            pending += 1

    settled = won + lost
    hit_rate = (won / settled * 100.0) if settled > 0 else 0.0
    roi = (pnl / units_risked * 100.0) if units_risked > 0 else 0.0

    return {
        "total_bets": total,
        "pending": pending,
        "won": won,
        "lost": lost,
        "push": push,
        "hit_rate_pct": round(hit_rate, 1),
        "units_risked": round(units_risked, 2),
        "pnl_units": round(pnl, 2),
        "roi_pct": round(roi, 2),
    }


def _breakdown(bets: list[dict], key: str) -> list[dict]:
    from collections import defaultdict
    buckets: dict[str, dict[str, float]] = defaultdict(
        lambda: {"n": 0, "won": 0, "lost": 0, "push": 0, "pending": 0, "units_risked": 0.0, "pnl": 0.0}
    )
    for b in bets:
        k = b.get(key) or "Unknown"
        st = b.get("status") or "pending"
        stake = float(b.get("stake_units") or 0.0)
        p = float(b.get("pnl_units") or 0.0)
        buckets[k]["n"] += 1
        buckets[k][st] = buckets[k].get(st, 0) + 1
        if st in ("won", "lost"):
            buckets[k]["units_risked"] += stake
            buckets[k]["pnl"] += p
    rows = []
    for k, v in buckets.items():
        settled = v["won"] + v["lost"]
        hit = (v["won"] / settled * 100.0) if settled else 0.0
        roi = (v["pnl"] / v["units_risked"] * 100.0) if v["units_risked"] else 0.0
        rows.append({
            key: k,
            "n": int(v["n"]),
            "won": int(v["won"]),
            "lost": int(v["lost"]),
            "hit_rate_pct": round(hit, 1),
            "units_risked": round(v["units_risked"], 2),
            "pnl_units": round(v["pnl"], 2),
            "roi_pct": round(roi, 2),
        })
    rows.sort(key=lambda r: -r["n"])
    return rows


@router.get("/user/analytics/by-sport")
async def user_analytics_by_sport(
    user: Annotated[UserPublic, Depends(current_user)],
) -> dict[str, Any]:
    """Personal ROI grouped by sport."""
    bets = await db.user_bets.find({"user_id": user.id}, {"_id": 0}).to_list(5000)
    return {"rows": _breakdown(bets, "sport")}


@router.get("/user/analytics/by-market")
async def user_analytics_by_market(
    user: Annotated[UserPublic, Depends(current_user)],
) -> dict[str, Any]:
    """Personal ROI grouped by market (moneyline, spread, totals, props, parlays)."""
    bets = await db.user_bets.find({"user_id": user.id}, {"_id": 0}).to_list(5000)
    return {"rows": _breakdown(bets, "market")}


@router.get("/user/analytics/history")
async def user_analytics_history(
    user: Annotated[UserPublic, Depends(current_user)],
    limit: int = 50,
    status_filter: Optional[str] = None,
) -> dict[str, Any]:
    """Chronological betting history — settled bets first, most recent."""
    q: dict[str, Any] = {"user_id": user.id}
    if status_filter in ("pending", "won", "lost", "push"):
        q["status"] = status_filter
    limit = max(1, min(limit, 500))
    cursor = db.user_bets.find(q, {"_id": 0}).sort("created_at", -1).limit(limit)
    bets = await cursor.to_list(limit)
    return {"history": bets, "count": len(bets)}


# ── Settlement propagator ────────────────────────────────────────────
# Called by the existing settlement modules whenever a pick's status
# flips to won/lost/push. Updates every user_bet linked to that pick
# so personal ROI reflects the outcome. Safe to call idempotently —
# already-settled user_bets are skipped.
async def propagate_pick_settlement(pick_id: str, pick_status: str,
                                    book_odds: Optional[int] = None) -> int:
    """Propagate a pick's settlement to all user_bets that reference it.

    Straight bets:
        Match by (pick_id == pick.id, bet_type='straight'). Settle
        directly with pick_status and PnL computed from odds_at_bet.

    Parlays:
        Match by (parlay_legs contains pick.id). Only settle the parlay
        when ALL legs are settled — if any leg is still pending, skip.
        Parlay wins iff all legs won; loses if any leg lost; pushes if
        any leg pushed and none lost.

    Returns count of user_bets updated.
    """
    if pick_status not in ("won", "lost", "push"):
        return 0
    now = datetime.now(timezone.utc)
    updated = 0

    # ── Straight bets ────────────────────────────────────────────────
    cursor = db.user_bets.find({
        "pick_id": pick_id,
        "bet_type": "straight",
        "status": "pending",
    })
    async for bet in cursor:
        stake = float(bet.get("stake_units") or 0.0)
        odds = bet.get("odds_at_bet") or book_odds
        if pick_status == "won":
            pnl = _american_to_profit(odds, stake)
        elif pick_status == "lost":
            pnl = -stake
        else:  # push
            pnl = 0.0
        await db.user_bets.update_one(
            {"id": bet["id"]},
            {"$set": {
                "status": pick_status,
                "pnl_units": round(pnl, 3),
                "settled_at": now,
            }},
        )
        updated += 1

    # ── Parlays containing this pick ─────────────────────────────────
    parlay_cursor = db.user_bets.find({
        "parlay_legs": pick_id,
        "bet_type": "parlay",
        "status": "pending",
    })
    async for pbet in parlay_cursor:
        legs = pbet.get("parlay_legs") or []
        leg_status = await db.picks.find(
            {"id": {"$in": legs}}, {"id": 1, "status": 1, "_id": 0}
        ).to_list(20)
        status_map = {p["id"]: p.get("status") for p in leg_status}
        # If any leg still pending → skip this parlay for now.
        if any(s in (None, "pending") for s in status_map.values()):
            continue
        # All legs settled — grade parlay:
        stake = float(pbet.get("stake_units") or 0.0)
        odds = pbet.get("odds_at_bet")
        results = list(status_map.values())
        if "lost" in results:
            new_status, pnl = "lost", -stake
        elif "push" in results and "lost" not in results:
            # One push, others won → parlay treats push as neutral, still wins
            if all(s in ("won", "push") for s in results):
                new_status = "won"
                pnl = _american_to_profit(odds, stake)
            else:
                new_status, pnl = "push", 0.0
        elif all(s == "won" for s in results):
            new_status, pnl = "won", _american_to_profit(odds, stake)
        else:
            new_status, pnl = "push", 0.0
        await db.user_bets.update_one(
            {"id": pbet["id"]},
            {"$set": {
                "status": new_status,
                "pnl_units": round(pnl, 3),
                "settled_at": now,
            }},
        )
        updated += 1

    if updated:
        logger.info("user_bets: propagated pick %s (%s) → %d bets", pick_id, pick_status, updated)
    return updated
