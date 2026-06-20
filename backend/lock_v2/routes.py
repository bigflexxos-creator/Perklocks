"""FastAPI router for the Lock Engine V2 shadow-mode reports.

Mounted under /api in server.py. Adds two endpoints:
  * GET /api/lock-v2/report            — side-by-side current vs shadow stats
  * GET /api/picks/{id}/lock-breakdown — per-pick v2 evidence / counter / survival
"""
from __future__ import annotations

import datetime as _dt

from fastapi import APIRouter, Depends, HTTPException

from .engine import V2_ENABLED, compute_v2_shadow

router = APIRouter(tags=["lock_v2"])


def _get_db():
    from server import db
    return db


def _require_auth():
    from server import current_user
    return current_user


def _odds_to_decimal(american) -> float:
    try:
        a = float(american)
    except Exception:
        return 1.0
    if a == 0:
        return 1.0
    return 1.0 + (a / 100.0) if a > 0 else 1.0 + (100.0 / -a)


def _bucket_lock(v) -> str:
    if v is None:
        return "unknown"
    try:
        v = float(v)
    except Exception:
        return "unknown"
    if v >= 99:  return "99"
    if v >= 96:  return "96-98"
    if v >= 93:  return "93-95"
    if v >= 90:  return "90-92"
    return "<90"


async def _build_report(db, days_back: int) -> dict:
    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days_back)
    cutoff_iso = cutoff.isoformat()

    match = {
        "$or": [
            {"event_time":  {"$gte": cutoff_iso}},
            {"created_at":  {"$gte": cutoff_iso}},
            {"settled_at":  {"$gte": cutoff_iso}},
        ],
        "lock_score_v2": {"$exists": True},
    }
    current = {
        "picks": 0, "settled": 0, "wins": 0, "losses": 0,
        "stake": 0.0, "return": 0.0, "lock_sum": 0.0,
        "distribution": {}, "apex_hit": 0, "apex_total": 0,
    }
    shadow = {
        "picks": 0, "settled": 0, "wins": 0, "losses": 0,
        "stake": 0.0, "return": 0.0, "lock_sum": 0.0,
        "distribution": {}, "apex_hit": 0, "apex_total": 0,
    }

    async for p in db.picks.find(match, {
        "_id": 0, "lock_score": 1, "lock_score_v2": 1, "status": 1,
        "book_odds": 1, "is_apex": 1, "tier_v2": 1,
    }):
        old_lock = p.get("lock_score")
        new_lock = p.get("lock_score_v2")
        status = (p.get("status") or "pending").lower()
        odds = p.get("book_odds")
        is_apex = bool(p.get("is_apex"))

        current["picks"] += 1; shadow["picks"] += 1
        current["lock_sum"] += float(old_lock or 0)
        shadow["lock_sum"]  += float(new_lock or 0)
        ob = _bucket_lock(old_lock); current["distribution"][ob] = current["distribution"].get(ob, 0) + 1
        nb = _bucket_lock(new_lock); shadow["distribution"][nb]  = shadow["distribution"].get(nb, 0) + 1

        if old_lock and float(old_lock) >= 99: current["apex_total"] += 1
        if is_apex: shadow["apex_total"] += 1

        if status not in ("won", "lost", "push"):
            continue
        decimal = _odds_to_decimal(odds)
        current["settled"] += 1; shadow["settled"] += 1
        current["stake"] += 1.0; shadow["stake"] += 1.0
        if status == "won":
            current["wins"] += 1; shadow["wins"] += 1
            current["return"] += decimal; shadow["return"] += decimal
            if old_lock and float(old_lock) >= 99: current["apex_hit"] += 1
            if is_apex: shadow["apex_hit"] += 1
        elif status == "lost":
            current["losses"] += 1; shadow["losses"] += 1
        else:
            current["return"] += 1.0; shadow["return"] += 1.0

    def finalize(d: dict) -> dict:
        n = d["picks"] or 1
        return {
            "picks":        d["picks"],
            "settled":      d["settled"],
            "wins":         d["wins"],
            "losses":       d["losses"],
            "hit_pct":      round((d["wins"] / d["settled"]) * 100, 1) if d["settled"] else None,
            "roi_pct":      round(((d["return"] - d["stake"]) / d["stake"]) * 100, 1) if d["stake"] else None,
            "avg_lock":     round(d["lock_sum"] / n, 2),
            "distribution": d["distribution"],
            "apex_total":   d["apex_total"],
            "apex_hit":     d["apex_hit"],
            "apex_hit_pct": round((d["apex_hit"] / d["apex_total"]) * 100, 1) if d["apex_total"] else None,
        }

    cur = finalize(current); sha = finalize(shadow)

    notes: list[str] = []
    can_cutover = False
    if sha["settled"] < 500:
        notes.append(f"Shadow only {sha['settled']} settled — need ≥ 500")
    if cur["roi_pct"] is not None and sha["roi_pct"] is not None:
        delta = sha["roi_pct"] - cur["roi_pct"]
        if delta >= 5:
            can_cutover = True
            notes.append(f"Shadow ROI {sha['roi_pct']}% beats current {cur['roi_pct']}% by ≥5 pts")
        else:
            notes.append(f"Shadow ROI delta {delta:+.1f}pts")
    if cur["apex_hit_pct"] is not None and sha["apex_hit_pct"] is not None:
        if sha["apex_hit_pct"] > cur["apex_hit_pct"]:
            notes.append(f"99-lock hit% improved {cur['apex_hit_pct']}% → {sha['apex_hit_pct']}%")

    return {
        "window_days":   days_back,
        "v2_enabled":    V2_ENABLED,
        "current":       cur,
        "shadow":        sha,
        "cutover_ready": bool(can_cutover and sha["settled"] >= 500),
        "notes":         notes,
    }


@router.get("/lock-v2/report")
async def lock_v2_report(
    days: int = 14,
    user=Depends(_require_auth()),
):
    if days < 1 or days > 60:
        raise HTTPException(400, "days must be 1-60")
    db = _get_db()
    return await _build_report(db, days)


@router.get("/picks/{pick_id}/lock-breakdown")
async def pick_lock_breakdown(
    pick_id: str,
    user=Depends(_require_auth()),
):
    db = _get_db()
    pick = await db.picks.find_one({"id": pick_id}, {"_id": 0})
    if not pick:
        raise HTTPException(404, "pick not found")
    shadow_keys = (
        "evidence_score", "conviction_score", "counter_score",
        "survival_score", "variance_score", "simulation_pass",
        "agreement_score", "lock_score_v2", "tier_v2",
        "is_apex", "apex_blockers", "v2_reasons",
    )
    if pick.get("lock_score_v2") is not None:
        return {
            "pick_id":    pick_id,
            "v2_enabled": V2_ENABLED,
            "shadow":     {k: pick.get(k) for k in shadow_keys},
            "live_computed": False,
        }
    live = compute_v2_shadow(pick)
    return {
        "pick_id":       pick_id,
        "v2_enabled":    V2_ENABLED,
        "shadow":        live,
        "live_computed": True,
    }
