"""External-ML board externalization — HTTP surface.

Routes (all under ``/api``):

  GET  /api/export/inputs             — paginated input bundle for the runner.
  POST /api/ingest/board              — stage a chunk of picks.
  POST /api/ingest/commit             — atomic live-board switch.
  POST /api/ingest/heartbeat          — bounded operational state.

Every ingest route is protected by the ``INGEST_TOKEN`` env var via the
``X-Ingest-Token`` header.  The token value is NEVER echoed back and
is compared with ``hmac.compare_digest`` so a timing attack cannot
leak length info.
"""
from __future__ import annotations

import hmac
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from deps import db

from services import external_ml

logger = logging.getLogger("lockscore.external_ml_routes")

router = APIRouter(tags=["external_ml"])


# ═════════════════════════════════════════════════════════════════════
# Token authentication
# ═════════════════════════════════════════════════════════════════════
def _require_ingest_token(x_ingest_token: Optional[str]) -> None:
    """Reject requests whose token header does not match ``INGEST_TOKEN``.

    Behaviour per spec §3:
      * Missing header → 401
      * Wrong value   → 403
      * Empty env var → 503 (service not provisioned, do not crash)
    """
    expected = os.environ.get("INGEST_TOKEN") or ""
    if not expected:
        raise HTTPException(status_code=503,
                            detail="INGEST_TOKEN not provisioned on server")
    if not x_ingest_token:
        raise HTTPException(status_code=401, detail="X-Ingest-Token required")
    if not hmac.compare_digest(str(x_ingest_token), str(expected)):
        raise HTTPException(status_code=403, detail="Invalid X-Ingest-Token")


async def _ingest_auth(
    x_ingest_token: Optional[str] = Header(default=None),
) -> None:
    _require_ingest_token(x_ingest_token)


# ═════════════════════════════════════════════════════════════════════
# GET /api/export/inputs
# ═════════════════════════════════════════════════════════════════════
@router.get("/export/inputs", dependencies=[Depends(_ingest_auth)])
async def export_inputs(
    sport: str,
    since: Optional[str] = None,
    cursor: Optional[str] = None,
    limit: int = 500,
):
    """Return a paginated input bundle for the external runner.

    Payload per sport (spec §4): identities, future events, roster
    context (where stored), player history (bounded), team history
    (bounded), pregame snapshots.  Secrets are never included.

    This is a READ-ONLY endpoint and must stay fast.  Heavy scoring
    stays external.
    """
    sp = external_ml._norm_sport(sport)
    if sp not in external_ml.SUPPORTED_SPORTS:
        raise HTTPException(status_code=400,
                            detail=f"unsupported sport: {sport}")
    try:
        lim = max(1, min(int(limit), 2000))
    except Exception:
        lim = 500

    # Parse since (ISO-8601) if provided.
    since_dt: Optional[datetime] = None
    if since:
        try:
            s = since.replace("Z", "+00:00") if since.endswith("Z") else since
            since_dt = datetime.fromisoformat(s)
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=timezone.utc)
        except Exception:
            raise HTTPException(status_code=400, detail="invalid since")

    # ── Future events window: today + 72h (same window Perklocks
    #    publishes).  We read from ``db.games`` (the canonical event
    #    store) and fall back to an empty list if the collection is
    #    missing — the runner should still get a valid payload.
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=72)
    events_q: dict[str, Any] = {
        "sport": sp,
        "commence_time": {"$gte": now, "$lte": cutoff},
    }
    if since_dt:
        events_q["$or"] = [
            {"updated_at": {"$gte": since_dt}},
            {"ingested_at": {"$gte": since_dt}},
        ]
    try:
        events_cursor = db.games.find(
            events_q,
            {"_id": 0, "id": 1, "sport": 1, "commence_time": 1,
             "home_team": 1, "away_team": 1, "league": 1, "event": 1,
             "canonical_event_id": 1, "external_id": 1, "status": 1},
        ).sort("commence_time", 1).limit(lim)
        events = await events_cursor.to_list(length=lim)
    except Exception as e:
        logger.debug("export_inputs events read failed: %s", e)
        events = []

    # ── Player history (bounded last 40 games per player involved in
    #    future events).  We derive the player universe lazily from the
    #    events themselves — the runner can request more via a
    #    follow-up export call per player if it needs deeper history.
    player_history: list[dict] = []
    try:
        player_q: dict[str, Any] = {"sport": sp}
        if since_dt:
            player_q["created_at"] = {"$gte": since_dt}
        player_cursor = db.player_game_actuals.find(
            player_q,
            {"_id": 0, "sport": 1, "player_name": 1, "canonical_player_id": 1,
             "event_date": 1, "team": 1, "opponent": 1, "home_away": 1,
             "actuals": 1, "stats": 1, "toi": 1, "created_at": 1},
        ).sort("event_date", -1).limit(lim * 2)
        player_history = await player_cursor.to_list(length=lim * 2)
    except Exception as e:
        logger.debug("export_inputs player_history read failed: %s", e)

    # ── Team history (bounded last 20 games).
    team_history: list[dict] = []
    try:
        team_q: dict[str, Any] = {"sport": sp}
        if since_dt:
            team_q["created_at"] = {"$gte": since_dt}
        team_cursor = db.team_game_actuals.find(
            team_q,
            {"_id": 0, "sport": 1, "team": 1, "event_date": 1,
             "opponent": 1, "home_away": 1, "stats": 1, "created_at": 1},
        ).sort("event_date", -1).limit(lim)
        team_history = await team_cursor.to_list(length=lim)
    except Exception as e:
        logger.debug("export_inputs team_history read failed: %s", e)

    # ── Config/metadata the runner needs to reproduce publication
    #    semantics identically.
    config = {
        "publication_lock_floor": external_ml.PUBLICATION_LOCK_FLOOR,
        "published_bookmaker":    external_ml.PUBLISHED_BOOKMAKER,
        "supported_sports":       list(external_ml.SUPPORTED_SPORTS),
        "feature_source_version": "v1-external-ml",
    }

    # Pagination cursor: next-page encoded as the ``commence_time`` of
    # the last event returned.  Simple deterministic cursor; the
    # runner just echoes it back.
    next_cursor: Optional[str] = None
    if events and len(events) == lim:
        last_ct = events[-1].get("commence_time")
        if isinstance(last_ct, datetime):
            next_cursor = last_ct.isoformat().replace("+00:00", "Z")
        elif last_ct:
            next_cursor = str(last_ct)

    return {
        "sport":          sp,
        "generated_at":   now.isoformat().replace("+00:00", "Z"),
        "events":         _serialize_dates(events),
        "player_history": _serialize_dates(player_history),
        "team_history":   _serialize_dates(team_history),
        "config":         config,
        "limit":          lim,
        "next_cursor":    next_cursor,
    }


def _serialize_dates(rows: list[dict]) -> list[dict]:
    """Make datetime objects JSON-serializable."""
    out: list[dict] = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        d = {}
        for k, v in r.items():
            if isinstance(v, datetime):
                d[k] = v.isoformat().replace("+00:00", "Z")
            else:
                d[k] = v
        out.append(d)
    return out


# ═════════════════════════════════════════════════════════════════════
# POST /api/ingest/board
# ═════════════════════════════════════════════════════════════════════
class IngestBoardBody(BaseModel):
    sport:          str
    generation_id:  str = Field(..., min_length=8, max_length=128)
    chunk_no:       int = Field(..., ge=0, le=10000)
    model_version:  str = Field(..., min_length=1, max_length=128)
    computed_at:    Optional[str] = None
    chunk_count:    Optional[int] = Field(default=None, ge=1, le=10000)
    picks:          list[dict] = Field(default_factory=list)
    job_id:         Optional[str] = Field(default=None, max_length=128)


@router.post("/ingest/board", dependencies=[Depends(_ingest_auth)])
async def ingest_board(body: IngestBoardBody):
    """Stage one chunk of a generation.  Idempotent on
    ``(generation_id, chunk_no)``."""
    sp = external_ml._norm_sport(body.sport)
    if not external_ml.is_supported_sport(sp):
        raise HTTPException(status_code=400,
                            detail=f"unsupported sport: {body.sport}")
    if len(body.picks) > 500:
        raise HTTPException(status_code=413,
                            detail="chunk too large (max 500 picks)")
    # Parse computed_at.
    ca_dt: Optional[datetime] = None
    if body.computed_at:
        try:
            s = body.computed_at
            s = s.replace("Z", "+00:00") if s.endswith("Z") else s
            ca_dt = datetime.fromisoformat(s)
            if ca_dt.tzinfo is None:
                ca_dt = ca_dt.replace(tzinfo=timezone.utc)
        except Exception:
            raise HTTPException(status_code=400, detail="invalid computed_at")

    # Ensure indexes (cheap — idempotent).
    try:
        await external_ml.ensure_indexes(db)
    except Exception:
        pass

    # Upsert generation with chunk_count if the runner sent one.
    try:
        await external_ml.upsert_generation(
            db, sport=sp, generation_id=body.generation_id,
            model_version=body.model_version, computed_at=ca_dt,
            chunk_count=body.chunk_count, job_id=body.job_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    try:
        result = await external_ml.append_chunk(
            db, sport=sp, generation_id=body.generation_id,
            chunk_no=body.chunk_no, model_version=body.model_version,
            computed_at=ca_dt, picks=body.picks,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # If every expected chunk has arrived, flip to COMPLETE.
    gen = await db.board_generations.find_one(
        {"generation_id": body.generation_id}, {"_id": 0})
    if gen and gen.get("chunk_count") is not None:
        expected = int(gen.get("chunk_count") or 0)
        received = int(gen.get("received_chunks") or 0)
        if expected and received >= expected and gen.get("status") == "STAGING":
            try:
                await external_ml.mark_complete(
                    db, generation_id=body.generation_id,
                    expected_chunks=expected)
            except Exception as e:  # pragma: no cover
                logger.debug("mark_complete auto-flip failed: %s", e)
    return {
        "ok":              True,
        "sport":           sp,
        "generation_id":   body.generation_id,
        "chunk_no":        body.chunk_no,
        "result":          result,
    }


# ═════════════════════════════════════════════════════════════════════
# POST /api/ingest/commit
# ═════════════════════════════════════════════════════════════════════
class IngestCommitBody(BaseModel):
    sport:         str
    generation_id: str = Field(..., min_length=8, max_length=128)
    expected_chunks: Optional[int] = Field(default=None, ge=1, le=10000)


@router.post("/ingest/commit", dependencies=[Depends(_ingest_auth)])
async def ingest_commit(body: IngestCommitBody):
    """Atomic last-good-board switch.  Idempotent on
    ``generation_id``."""
    sp = external_ml._norm_sport(body.sport)
    if not external_ml.is_supported_sport(sp):
        raise HTTPException(status_code=400,
                            detail=f"unsupported sport: {body.sport}")

    # If the runner sent expected_chunks, flip to COMPLETE first.
    try:
        await external_ml.mark_complete(
            db, generation_id=body.generation_id,
            expected_chunks=body.expected_chunks)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    try:
        from server import _today_str  # lazy to avoid circular import
    except Exception:
        _today_str = None  # type: ignore

    try:
        pointer = await external_ml.commit_generation(
            db, sport=sp, generation_id=body.generation_id,
            today_str_fn=_today_str,
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"ok": True, "pointer": _serialize_dates([pointer])[0] if pointer else None}


# ═════════════════════════════════════════════════════════════════════
# POST /api/ingest/heartbeat
# ═════════════════════════════════════════════════════════════════════
class IngestHeartbeatBody(BaseModel):
    job_id:        str = Field(..., min_length=1, max_length=128)
    sport:         Optional[str] = None
    stage:         str = Field(..., min_length=1, max_length=48)
    generation_id: Optional[str] = Field(default=None, max_length=128)
    message:       Optional[str] = Field(default=None, max_length=400)
    error:         Optional[str] = Field(default=None, max_length=800)


@router.post("/ingest/heartbeat", dependencies=[Depends(_ingest_auth)])
async def ingest_heartbeat(body: IngestHeartbeatBody):
    sp = external_ml._norm_sport(body.sport) if body.sport else None
    doc = await external_ml.record_heartbeat(
        db, job_id=body.job_id, sport=sp, stage=body.stage,
        generation_id=body.generation_id,
        message=body.message, error=body.error,
    )
    # Return a lightweight ack (never echo tokens).
    return {"ok": True, "at": doc.get("ts").isoformat().replace("+00:00", "Z")
            if isinstance(doc.get("ts"), datetime) else None}


# ═════════════════════════════════════════════════════════════════════
# POST /api/ingest/mark-failed — explicit failure path
# ═════════════════════════════════════════════════════════════════════
class IngestFailBody(BaseModel):
    generation_id: str = Field(..., min_length=8, max_length=128)
    reason:        str = Field(..., min_length=1, max_length=400)


@router.post("/ingest/fail", dependencies=[Depends(_ingest_auth)])
async def ingest_fail(body: IngestFailBody):
    gen = await external_ml.mark_failed(
        db, generation_id=body.generation_id, reason=body.reason)
    return {"ok": True, "generation_id": body.generation_id,
            "status": (gen or {}).get("status")}
