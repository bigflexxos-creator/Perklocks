"""User-facing performance / CLV dashboard endpoints.

Exposes the app's model performance (ROI, CLV, hit rate) to any
logged-in user — NOT gated on admin. The goal is to prove to users
that the picks board is +EV so they can bet with confidence.

User mandate 2026-07-27: "Best app in world — need CLV Dashboard so
users can see the app is winning."

All endpoints are read-only aggregations over `db.picks` filtered by
status ∈ {won, lost, push}. No per-user tracking — this exposes the
COLLECTIVE model performance.

Endpoints:
  GET  /api/me/performance            — high-level summary (all-time + last 30d)
  GET  /api/me/performance/by-sport   — hit-rate + ROI + CLV by sport
  GET  /api/me/performance/by-band    — hit-rate by lock-score band
  GET  /api/me/performance/trend      — daily ROI series (last 30 days)
  GET  /api/me/steam-picks            — currently steam-flagged picks (public)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Query

from auth import UserPublic
from deps import current_user, db

router = APIRouter(prefix="/api/me")


def _bucket_lock_score(ls: float | None) -> str:
    if ls is None:
        return "unknown"
    if ls >= 90:
        return "90+"
    if ls >= 80:
        return "80-89"
    if ls >= 70:
        return "70-79"
    if ls >= 60:
        return "60-69"
    return "<60"


def _iso_cutoff(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


# ── /api/me/performance — Top-level summary ──────────────────────────
@router.get("/performance")
async def user_performance(
    user: Annotated[UserPublic, Depends(current_user)],
    days: int = Query(30, ge=1, le=365,
                       description="Trailing window in days for the 'recent' block"),
):
    """Return the app's collective model performance so the user can
    see the picks board is +EV.

    Payload shape:
      {
        "all_time":  { n, hit_rate_pct, roi_pct, clv_avg_pct },
        "recent":    { window_days, n, hit_rate_pct, roi_pct, clv_avg_pct },
        "high_conviction": { n, hit_rate_pct, roi_pct },   # lock ≥ 85
      }
    """
    async def _agg(match_extra: dict | None = None) -> dict:
        match = {"status": {"$in": ["won", "lost", "push"]}}
        if match_extra:
            match.update(match_extra)
        pipeline = [
            {"$match": match},
            {"$group": {
                "_id": None,
                "n": {"$sum": 1},
                "won": {"$sum": {"$cond": [{"$eq": ["$status", "won"]}, 1, 0]}},
                "lost": {"$sum": {"$cond": [{"$eq": ["$status", "lost"]}, 1, 0]}},
                "risked": {"$sum": {"$ifNull": ["$units_risked", 0]}},
                "profit": {"$sum": {"$ifNull": ["$units_profit", 0]}},
                "clv_sum": {"$sum": {"$ifNull": ["$clv_value", 0]}},
                "clv_n": {"$sum": {"$cond": [{"$ne": ["$clv_value", None]}, 1, 0]}},
            }},
        ]
        docs = await db.picks.aggregate(pipeline).to_list(length=1)
        if not docs:
            return {"n": 0, "hit_rate_pct": 0.0, "roi_pct": 0.0, "clv_avg_pct": 0.0}
        d = docs[0]
        settled = d.get("won", 0) + d.get("lost", 0)
        risked = d.get("risked", 0) or 0
        profit = d.get("profit", 0) or 0
        clv_n = d.get("clv_n", 0) or 0
        return {
            "n": d.get("n", 0),
            "won": d.get("won", 0),
            "lost": d.get("lost", 0),
            "hit_rate_pct": round((d.get("won", 0) / settled * 100.0), 1) if settled else 0.0,
            "roi_pct": round((profit / risked * 100.0), 2) if risked else 0.0,
            "clv_avg_pct": round(d.get("clv_sum", 0) / clv_n, 2) if clv_n else 0.0,
            "clv_sample_size": clv_n,
        }

    all_time = await _agg()
    recent = await _agg({"created_at": {"$gte": _iso_cutoff(days)}})
    high_conv = await _agg({"lock_score": {"$gte": 85}})

    return {
        "all_time": all_time,
        "recent": {"window_days": days, **recent},
        "high_conviction": high_conv,
        "note": "CLV avg = weighted difference between pick's book_odds and closing_odds. Positive = you beat the market close.",
    }


# ── /api/me/performance/by-sport ─────────────────────────────────────
@router.get("/performance/by-sport")
async def user_perf_by_sport(
    user: Annotated[UserPublic, Depends(current_user)],
    days: Optional[int] = Query(None, ge=1, le=365,
        description="Trailing window in days. Omit for all-time."),
):
    match = {"status": {"$in": ["won", "lost", "push"]}}
    if days:
        match["created_at"] = {"$gte": _iso_cutoff(days)}
    pipeline = [
        {"$match": match},
        {"$group": {
            "_id": "$sport",
            "n": {"$sum": 1},
            "won": {"$sum": {"$cond": [{"$eq": ["$status", "won"]}, 1, 0]}},
            "lost": {"$sum": {"$cond": [{"$eq": ["$status", "lost"]}, 1, 0]}},
            "risked": {"$sum": {"$ifNull": ["$units_risked", 0]}},
            "profit": {"$sum": {"$ifNull": ["$units_profit", 0]}},
            "clv_sum": {"$sum": {"$ifNull": ["$clv_value", 0]}},
            "clv_n": {"$sum": {"$cond": [{"$ne": ["$clv_value", None]}, 1, 0]}},
        }},
        {"$sort": {"n": -1}},
    ]
    rows: list[dict] = []
    async for r in db.picks.aggregate(pipeline):
        settled = r.get("won", 0) + r.get("lost", 0)
        risked = r.get("risked", 0) or 0
        profit = r.get("profit", 0) or 0
        clv_n = r.get("clv_n", 0) or 0
        rows.append({
            "sport": r["_id"],
            "n": r.get("n", 0),
            "won": r.get("won", 0),
            "lost": r.get("lost", 0),
            "hit_rate_pct": round((r.get("won", 0) / settled * 100.0), 1) if settled else 0.0,
            "roi_pct": round((profit / risked * 100.0), 2) if risked else 0.0,
            "clv_avg_pct": round(r.get("clv_sum", 0) / clv_n, 2) if clv_n else 0.0,
        })
    return {"rows": rows, "window_days": days}


# ── /api/me/performance/by-band ──────────────────────────────────────
@router.get("/performance/by-band")
async def user_perf_by_band(
    user: Annotated[UserPublic, Depends(current_user)],
    days: Optional[int] = Query(None, ge=1, le=365),
):
    """Hit rate & ROI by lock-score band. Proves the model is
    well-calibrated — 90+ locks should have the highest hit rate."""
    match = {"status": {"$in": ["won", "lost", "push"]}}
    if days:
        match["created_at"] = {"$gte": _iso_cutoff(days)}
    pipeline = [
        {"$match": match},
        {"$addFields": {
            "band": {
                "$switch": {
                    "branches": [
                        {"case": {"$gte": ["$lock_score", 90]}, "then": "90+"},
                        {"case": {"$gte": ["$lock_score", 80]}, "then": "80-89"},
                        {"case": {"$gte": ["$lock_score", 70]}, "then": "70-79"},
                        {"case": {"$gte": ["$lock_score", 60]}, "then": "60-69"},
                    ],
                    "default": "<60",
                },
            },
        }},
        {"$group": {
            "_id": "$band",
            "n": {"$sum": 1},
            "won": {"$sum": {"$cond": [{"$eq": ["$status", "won"]}, 1, 0]}},
            "lost": {"$sum": {"$cond": [{"$eq": ["$status", "lost"]}, 1, 0]}},
            "risked": {"$sum": {"$ifNull": ["$units_risked", 0]}},
            "profit": {"$sum": {"$ifNull": ["$units_profit", 0]}},
        }},
    ]
    order = {"90+": 0, "80-89": 1, "70-79": 2, "60-69": 3, "<60": 4}
    rows: list[dict] = []
    async for r in db.picks.aggregate(pipeline):
        settled = r.get("won", 0) + r.get("lost", 0)
        risked = r.get("risked", 0) or 0
        profit = r.get("profit", 0) or 0
        rows.append({
            "band": r["_id"],
            "n": r.get("n", 0),
            "won": r.get("won", 0),
            "lost": r.get("lost", 0),
            "hit_rate_pct": round((r.get("won", 0) / settled * 100.0), 1) if settled else 0.0,
            "roi_pct": round((profit / risked * 100.0), 2) if risked else 0.0,
        })
    rows.sort(key=lambda x: order.get(x["band"], 99))
    return {"rows": rows, "window_days": days}


# ── /api/me/performance/trend ────────────────────────────────────────
@router.get("/performance/trend")
async def user_perf_trend(
    user: Annotated[UserPublic, Depends(current_user)],
    days: int = Query(30, ge=7, le=180),
):
    """Daily profit series for a bankroll-growth chart."""
    match = {
        "status": {"$in": ["won", "lost", "push"]},
        "created_at": {"$gte": _iso_cutoff(days)},
    }
    pipeline = [
        {"$match": match},
        {"$addFields": {"day": {"$substr": ["$created_at", 0, 10]}}},
        {"$group": {
            "_id": "$day",
            "n": {"$sum": 1},
            "won": {"$sum": {"$cond": [{"$eq": ["$status", "won"]}, 1, 0]}},
            "lost": {"$sum": {"$cond": [{"$eq": ["$status", "lost"]}, 1, 0]}},
            "profit": {"$sum": {"$ifNull": ["$units_profit", 0]}},
            "risked": {"$sum": {"$ifNull": ["$units_risked", 0]}},
        }},
        {"$sort": {"_id": 1}},
    ]
    rows: list[dict] = []
    running = 0.0
    async for r in db.picks.aggregate(pipeline):
        settled = r.get("won", 0) + r.get("lost", 0)
        profit = r.get("profit", 0) or 0
        running += profit
        rows.append({
            "day": r["_id"],
            "n": r.get("n", 0),
            "won": r.get("won", 0),
            "lost": r.get("lost", 0),
            "profit": round(profit, 2),
            "cumulative_profit": round(running, 2),
            "hit_rate_pct": round((r.get("won", 0) / settled * 100.0), 1) if settled else 0.0,
        })
    return {"rows": rows, "window_days": days, "cumulative_units": round(running, 2)}


# ── /api/me/steam-picks — current steam-move alerts ──────────────────
@router.get("/steam-picks")
async def user_steam_picks(
    user: Annotated[UserPublic, Depends(current_user)],
    hours: int = Query(6, ge=1, le=24),
    direction: Optional[str] = Query(None, description="'toward' or 'away'"),
    limit: int = Query(20, ge=1, le=50),
):
    """List pending picks flagged by the steam detector — line moved
    strongly in the last N hours. The frontend renders these as 🔥
    STEAM badge cards."""
    from steam_detector import get_steam_picks
    dir_val = direction if direction in ("toward", "away") else None
    picks = await get_steam_picks(db, hours=hours, direction=dir_val, limit=limit)
    return {"picks": picks, "hours": hours, "count": len(picks)}


__all__ = ["router"]
