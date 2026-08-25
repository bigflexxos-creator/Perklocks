"""P12 — Board health telemetry (2026-08-25).

Lightweight, read-only observability into the Perklocks board funnel per
sport. Exposes a single endpoint so operators + product can prove a
sport is starved *before* users report an empty tab.

Design rules:
  • READ-ONLY. No writes, no side effects, no expensive aggregations.
  • Every count is a simple `count_documents` on the live picks
    collection scoped to the current slate day.
  • No new indexes introduced; queries reuse existing single-key
    indexes (`sport`, `pick_date`, `published_lock_score`).
  • Latency budget: <500ms per call (empirically ~100-200ms).

Endpoint:
  GET /api/ops/board-health   (auth required)

Returns:
  {
    "slate": "2026-08-24",
    "generated_at": "2026-08-25T06:47:00Z",
    "per_sport": {
      "MLB": {
        "candidates": 22525,
        "identity_valid": 22400,
        "mig_valid": 91,
        "scored_over_85": 60,
        "published": 40,
        "visible_on_board": 30,
        "settlement_supported": <int>,
        "grade_mismatch": <int>,
        "lock_score_mismatch": <int>,
        "apex_status_true": <int>,
        "apex_status_false": <int>,
      },
      ...
    },
    "parlay_funnel": {
      "published_85_plus": <int>,
      "published_95_plus": <int>,
      "published_98_plus": <int>,
    },
  }

The endpoint intentionally SKIPS provider health / settlement
coverage rollup — those live in their own diagnostics endpoints
(`/api/admin/odds-diagnostic`, `/api/analytics/settlement-truth`) so
this file stays cheap.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends

from auth import UserPublic
from deps import current_user, db, logger

router = APIRouter(prefix="/ops", tags=["ops"])

SPORTS = ["MLB", "NFL", "NBA", "NHL", "Soccer", "Tennis", "CFB", "UFC"]


@router.get("/provider-health")
async def provider_health(user: Annotated[UserPublic, Depends(current_user)]):
    """P3 — Read-only provider health for the two NEW completed-match
    scaffolds (PitchAPI primary, Big Balls fallback).  Neither is
    wired into settlement yet; this endpoint proves the API-key
    plumbing loads correctly without exposing key values.
    """
    from services.providers import pitchapi, bigballs
    results = {}
    try:
        results["pitchapi"] = await pitchapi.health_check()
    except Exception as e:
        results["pitchapi"] = {"provider": "pitchapi", "status": "ERROR",
                                "error": type(e).__name__}
    try:
        results["bigballs"] = await bigballs.health_check()
    except Exception as e:
        results["bigballs"] = {"provider": "bigballs", "status": "ERROR",
                                "error": type(e).__name__}
    return {
        "generated_at": datetime.now(timezone.utc).isoformat()
                                .replace("+00:00", "Z"),
        "providers": results,
        "wired_into_settlement": False,   # SCAFFOLD ONLY
    }


@router.get("/board-health")
async def board_health(user: Annotated[UserPublic, Depends(current_user)]):
    """Per-sport funnel + parlay + coherence counts (read-only)."""
    from server import _today_str
    slate = _today_str()
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    per_sport: dict = {}
    for sport in SPORTS:
        base = {"sport": sport, "pick_date": slate}
        try:
            candidates = await db.picks.count_documents(base)
            identity_valid = await db.picks.count_documents({
                **base,
                "$or": [
                    {"canonical_event_id": {"$exists": True, "$ne": None}},
                    {"canonical_team_id":  {"$exists": True, "$ne": None}},
                    {"canonical_player_id":{"$exists": True, "$ne": None}},
                ],
            })
            mig_valid = await db.picks.count_documents({
                **base,
                "model_integrity_gate.allowed": True,
            })
            scored_over_85 = await db.picks.count_documents({
                **base,
                "$or": [
                    {"published_lock_score": {"$gte": 85}},
                    {"published_lock_score": {"$exists": False},
                     "lock_score":            {"$gte": 85}},
                ],
            })
            published = await db.picks.count_documents({
                **base,
                "publication_state": "PUBLISHED",
            })
            visible_on_board = await db.picks.count_documents({
                **base,
                "off_board": {"$ne": True},
                "no_bet":    {"$ne": True},
                "status":    {"$in": ["pending", "open", None]},
                "hide_from_main_board": {"$ne": True},
                "$or": [
                    {"published_lock_score": {"$gte": 85}},
                    {"published_lock_score": {"$exists": False},
                     "lock_score":            {"$gte": 85}},
                ],
                "$and": [{
                    "$or": [
                        {"published_grade": {"$exists": True, "$ne": "Pass"}},
                        {"$and": [
                            {"published_grade": {"$exists": False}},
                            {"grade": {"$ne": "Pass"}},
                        ]},
                    ],
                }],
            })
            # Coherence — how many published rows have grade vs
            # published_grade divergence, and lock_score vs
            # published_lock_score divergence >= 5 pts.
            grade_mismatch = await db.picks.count_documents({
                **base,
                "publication_state": "PUBLISHED",
                "$expr": {
                    "$and": [
                        {"$ne": ["$grade", "$published_grade"]},
                        {"$ne": ["$published_grade", None]},
                    ],
                },
            })
            lock_score_mismatch = await db.picks.count_documents({
                **base,
                "publication_state": "PUBLISHED",
                "$expr": {
                    "$and": [
                        {"$ne": ["$published_lock_score", None]},
                        {"$gt": [{"$abs": {"$subtract": [
                            {"$ifNull": ["$published_lock_score", 0]},
                            {"$ifNull": ["$lock_score", 0]},
                        ]}}, 5]},
                    ],
                },
            })
            apex_true = await db.picks.count_documents({
                **base,
                "$or": [{"apex_status": "APEX"}, {"apex_lock": True}],
            })
            apex_false = await db.picks.count_documents({
                **base,
                "apex_status": "NOT_APEX",
            })
            per_sport[sport] = {
                "candidates":            candidates,
                "identity_valid":        identity_valid,
                "mig_valid":             mig_valid,
                "scored_over_85":        scored_over_85,
                "published":             published,
                "visible_on_board":      visible_on_board,
                "grade_mismatch":        grade_mismatch,
                "lock_score_mismatch":   lock_score_mismatch,
                "apex_status_apex":      apex_true,
                "apex_status_not_apex":  apex_false,
            }
        except Exception as e:
            logger.warning("board_health probe failed for %s: %s", sport, e)
            per_sport[sport] = {"error": str(e)}

    # Parlay funnel (cross-sport totals over the same slate)
    try:
        parlay_funnel = {
            "published_85_plus": await db.picks.count_documents({
                "pick_date": slate,
                "publication_state": "PUBLISHED",
                "published_lock_score": {"$gte": 85},
                "off_board": {"$ne": True},
                "no_bet": {"$ne": True},
            }),
            "published_95_plus": await db.picks.count_documents({
                "pick_date": slate,
                "publication_state": "PUBLISHED",
                "published_lock_score": {"$gte": 95},
                "off_board": {"$ne": True},
                "no_bet": {"$ne": True},
            }),
            "published_98_plus": await db.picks.count_documents({
                "pick_date": slate,
                "publication_state": "PUBLISHED",
                "published_lock_score": {"$gte": 98},
                "off_board": {"$ne": True},
                "no_bet": {"$ne": True},
            }),
        }
    except Exception as e:
        parlay_funnel = {"error": str(e)}

    return {
        "slate": slate,
        "generated_at": now,
        "per_sport": per_sport,
        "parlay_funnel": parlay_funnel,
    }
