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


@router.get("/settlement-probe")
async def settlement_probe(
    pick_id: str,
    user: Annotated[UserPublic, Depends(current_user)],
):
    """Session B — Dry-run of the PitchAPI → Big Balls settlement
    cascade against a REAL Perklocks pick.  Read-only.  Never writes
    to db.picks / settlement_events / user_bets.

    Returns:
      {
        pick_id, sport, market, selection, event, event_time, league,
        fixture_resolution: {...},   # PitchAPI + Big Balls IDs
        provider_actuals: {
          pitchapi:  {status, actual, latency_ms, provenance},
          bigballs:  {status, actual, latency_ms, provenance},
        },
        cascade_final: {status, actual, chosen_provider, provenance},
        would_settle_as: "won" | "lost" | "push" | "DATA_UNAVAILABLE",
      }
    """
    from services.providers.soccer_fixture_resolver import resolve_fixture
    from services.providers import pitchapi as pa, bigballs as bb
    import re as _re
    from deps import db as _db

    pick = await _db.picks.find_one({"$or": [{"id": pick_id},
                                              {"_id": pick_id}]},
                                     {"_id": 0})
    if not pick:
        return {"error": "pick_not_found", "pick_id": pick_id}
    if (pick.get("sport") or "") != "Soccer":
        return {"error": "not_soccer", "sport": pick.get("sport")}

    event = pick.get("event") or ""
    parts = _re.split(r"\s+@\s+", event)
    if len(parts) != 2:
        return {"error": "unparseable_event", "event": event}
    away, home = parts[0].strip(), parts[1].strip()

    fx = await resolve_fixture(
        _db, perklocks_league=pick.get("league"),
        event_time_iso=pick.get("event_time"),
        home_team=home, away_team=away,
    )

    # ── Map the pick's market → provider market_family ────────
    market = (pick.get("market") or "").lower()
    selection = (pick.get("selection") or "").strip()
    if "anytime goal scorer" in market:
        mf = "soccer_goalscorer"
    elif "to score or assist" in market or "score & assist" in market:
        mf = "soccer_score_or_assist"
    elif "shots on target" in market:
        mf = "soccer_player_shots_on_target"
    elif "shots" in market and "on target" not in market:
        mf = "soccer_player_shots"
    elif "total corners" in market or "corners" in market:
        mf = "soccer_team_corners"
    elif "cards" in market or "booking" in market:
        mf = "soccer_cards"
    elif "assists" in market:
        mf = "soccer_assists"
    elif "goal" in market and "scorer" not in market:
        mf = "soccer_goals"
    else:
        mf = None

    result = {
        "pick_id":    pick.get("id"),
        "sport":      pick.get("sport"),
        "market":     pick.get("market"),
        "market_family_resolved": mf,
        "selection":  selection,
        "event":      event,
        "event_time": pick.get("event_time"),
        "league":     pick.get("league"),
        "fixture_resolution": fx,
        "provider_actuals": {},
    }

    if not mf:
        result["cascade_final"] = {
            "status": "MARKET_UNSUPPORTED",
            "provenance": {"reason": "market not in scaffold whitelist"},
        }
        result["would_settle_as"] = "MARKET_UNSUPPORTED"
        return result

    # PitchAPI attempt
    pa_actual = None
    if fx.get("pitchapi_match_id"):
        r = await pa.get_completed_actual(
            _db, sport="soccer",
            canonical_event_id=fx["pitchapi_match_id"],
            market_family=mf,
            player_name=selection,
        )
        pa_actual = {
            "status": r.status, "actual": r.actual,
            "latency_ms": r.latency_ms, "error_detail": r.error_detail,
            "provider_event_id": r.provider_event_id,
        }
    result["provider_actuals"]["pitchapi"] = pa_actual or {
        "status": "NO_FIXTURE_ID",
    }

    # Big Balls attempt (always run so we can prove the cascade)
    bb_actual = None
    if fx.get("bigballs_match_id"):
        r = await bb.get_completed_actual(
            _db, sport="soccer",
            canonical_event_id=str(fx["bigballs_match_id"]),
            market_family=mf,
            canonical_player_id=None,
        )
        bb_actual = {
            "status": r.status, "actual": r.actual,
            "latency_ms": r.latency_ms, "error_detail": r.error_detail,
            "provider_event_id": r.provider_event_id,
        }
    result["provider_actuals"]["bigballs"] = bb_actual or {
        "status": "NO_FIXTURE_ID",
    }

    # ── Cascade decision (PitchAPI primary, Big Balls fallback) ──
    chosen = None
    if pa_actual and pa_actual["status"] == "OK" and \
            pa_actual["actual"] is not None:
        chosen = ("pitchapi", pa_actual)
    elif bb_actual and bb_actual["status"] == "OK" and \
            bb_actual["actual"] is not None:
        chosen = ("bigballs", bb_actual)

    if chosen:
        provider, res = chosen
        result["cascade_final"] = {
            "status": "OK",
            "actual": res["actual"],
            "chosen_provider": provider,
            "provenance": {"provider": provider,
                            "market_family": mf,
                            "fixture": fx.get(f"{provider}_match_id")},
        }
        # ── Grade preview (no write) ────────────────────────────
        # NOTE: we don't touch settlement_events / picks — this is
        # a dry-run preview only.  The would_settle_as is computed
        # exactly as the existing settler would grade a scorer/
        # score-or-assist market.
        actual = res["actual"]
        if mf in ("soccer_goalscorer", "soccer_score_or_assist"):
            result["would_settle_as"] = "won" if bool(actual) else "lost"
        else:
            # Numeric market — needs a line; extract from market string
            m = _re.search(r"(\d+(?:\.\d+)?)", pick.get("market") or "")
            if m:
                line = float(m.group(1))
                try:
                    val = float(actual)
                    if "under" in market:
                        result["would_settle_as"] = (
                            "push" if abs(val - line) < 1e-9 else
                            ("won" if val < line else "lost"))
                    else:
                        result["would_settle_as"] = (
                            "push" if abs(val - line) < 1e-9 else
                            ("won" if val > line else "lost"))
                except (TypeError, ValueError):
                    result["would_settle_as"] = "DATA_UNAVAILABLE"
            else:
                result["would_settle_as"] = "DATA_UNAVAILABLE"
    else:
        result["cascade_final"] = {
            "status": "DATA_UNAVAILABLE",
            "chosen_provider": None,
            "provenance": {
                "pitchapi_status": (pa_actual or {}).get("status"),
                "bigballs_status": (bb_actual or {}).get("status"),
            },
        }
        result["would_settle_as"] = "DATA_UNAVAILABLE"

    return result


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
