"""HTTP routes for admin / ops endpoints.

Covers:
  • Soccer player form refresh trigger (Understat scrape)
  • Tennis Elo ledger backfill
  • Historical Sports Intelligence Engine (backfill / status / lookup)

Extracted from server.py during the 2026-06-24 monolith decomposition.
No behavior change — only relocation. Mounted by `server.py` via
`app.include_router(admin_routes.router)`.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import UserPublic
from deps import current_admin, db

router = APIRouter(prefix="/api")


# ────────────────────── Soccer / Tennis ops ──────────────────────
@router.get("/admin/pick-evidence/{pick_id}")
async def admin_pick_evidence(
    pick_id: str,
    user: Annotated[UserPublic, Depends(current_admin)],
):
    """Inspector for the Universal Evidence System (Phase 1).

    Returns the full evidence_breakdown for a given pick — every
    feature with its envelope (value / sample_size / lookback_days /
    source / freshness / reliability / tier / passes_governor), the
    raw and governed lock scores, the evidence multiplier, and any
    insights that got dropped because the evidence didn't support
    them. If the pick has no `evidence_score` yet (legacy pick),
    governs it on-the-fly so the inspector still works.
    """
    pick = await db.picks.find_one({"id": pick_id}, {"_id": 0})
    if not pick:
        raise HTTPException(status_code=404, detail="Pick not found")
    # Apply the SAME canonicalization the public /api/picks/{id} endpoint
    # uses so the inspector can't disagree with what the user sees on the
    # deep-dive screen (iter-50 finding #1).
    try:
        from server import _canonicalize_lock_score
        pick = _canonicalize_lock_score(pick)
    except Exception:
        pass
    # On-the-fly governance for old picks. We DON'T persist the change
    # here — settled picks must keep their stored lock score so history
    # stays immutable.
    if pick.get("evidence_score") is None:
        try:
            from evidence_engine import build_features_from_pick, govern_pick
            govern_pick(pick, build_features_from_pick(pick))
        except Exception as e:
            raise HTTPException(500, f"Evidence governance failed: {e}")
    return {
        "pick_id":              pick.get("id"),
        "sport":                pick.get("sport"),
        "market":               pick.get("market"),
        "player_name":          pick.get("player_name"),
        "event":                pick.get("event"),
        # The 4 separated metrics — rule 6.
        "probability_pct":      pick.get("win_probability"),
        "edge_pct":             pick.get("edge_percent"),
        "evidence_score":       pick.get("evidence_score"),
        "lock_score":           pick.get("lock_score"),
        "lock_score_raw":       pick.get("lock_score_raw"),
        # Full audit trail — rule 8.
        "evidence_breakdown":   pick.get("evidence_breakdown") or {},
        "key_insights":         pick.get("key_insights") or [],
        "status":               pick.get("status") or "pending",
    }


@router.post("/admin/refresh-soccer-player-form")
async def admin_refresh_soccer_player_form(
    user: Annotated[UserPublic, Depends(current_admin)],
):
    """Manually kick the Understat scrape job. Used by ops + initial
    seed. Returns the same summary dict the background loop logs every
    12h. Guarded by auth — any logged-in user can trigger because the
    cost is bounded (5 Understat POSTs).
    """
    from soccer_player_form import refresh_soccer_player_form
    return await refresh_soccer_player_form(db)


@router.post("/admin/backfill-tennis-elo")
async def admin_backfill_tennis_elo(
    user: Annotated[UserPublic, Depends(current_admin)],
    days_back: int = 30,
):
    """One-shot ops tool to seed the tennis_extra Elo + form ledger
    from the last `days_back` days of ESPN ATP/WTA results.

    SAFE TO RE-RUN ONCE on a fresh DB. Re-running on a populated DB
    will double-count form W/L (since `update_after_match` is
    `$inc`-based) — only re-trigger if you've reset the
    `tennis_players` collection first.
    """
    from espn_settlement import backfill_tennis_elo
    return await backfill_tennis_elo(db, days_back=max(1, min(60, days_back)))


# ────────────────────── Historical Sports Intelligence Engine ──────────────────────
class HistoricalBackfillRequest(BaseModel):
    sports: list[str] | None = None  # default: all 5 sports
    mode: str = "backfill"           # "backfill" | "incremental"
    days: int | None = None          # incremental: how many days back (default 3)


@router.post("/admin/historical/backfill")
async def historical_backfill(
    req: HistoricalBackfillRequest,
    user: Annotated[UserPublic, Depends(current_admin)] = None,
):
    """Trigger a current-season backfill (or incremental sync) for one
    or more sports. Returns per-sport summary.

    Note: Soccer (football-data.org) is paced at 6.5s/req due to the
    strict 10 req/min free-tier limit — a full backfill of 8
    competitions can take ~3-5 minutes. Other sports complete much
    faster.
    """
    try:
        from historical.orchestrator import backfill_current_season, incremental_sync
    except Exception as e:
        raise HTTPException(500, f"Historical engine not loaded: {e}")
    sports = req.sports or ["mlb", "nba", "nfl", "nhl", "soccer"]
    if req.mode == "incremental":
        since_override = None
        if req.days and req.days > 0:
            since_override = datetime.now(timezone.utc) - timedelta(days=int(req.days))
        out = await incremental_sync(sports=sports, since_override=since_override)
    else:
        out = await backfill_current_season(sports=sports)
    return {"mode": req.mode, "sports": sports, "results": out}


@router.get("/admin/historical/status")
async def historical_status(
    user: Annotated[UserPublic, Depends(current_admin)] = None,
):
    """Quick summary of what's stored in the historical engine."""
    counts = {}
    for col in ("players", "games", "player_game_logs", "season_totals", "team_form"):
        try:
            counts[col] = await db[col].estimated_document_count()
        except Exception:
            counts[col] = -1
    last_syncs = {}
    async for doc in db.historical_meta.find({}):
        last_syncs[doc.get("_id")] = doc.get("last_sync")
    return {"collections": counts, "last_syncs": last_syncs}


@router.get("/admin/historical/player-form")
async def historical_player_form(
    sport: str,
    name: str,
    market: Optional[str] = None,
    user: Annotated[UserPublic, Depends(current_admin)] = None,
):
    """Look up the stored form summary for a player (debug + transparency)."""
    try:
        from historical.lookup import get_player_form
        out = await get_player_form(sport, name, market_hint=market)
        return out or {"found": False, "sport": sport, "name": name}
    except Exception as e:
        raise HTTPException(500, f"lookup failed: {e}")


# ────────────────────── Scorer-coverage audit ──────────────────────
@router.get("/admin/scorer-audit")
async def admin_scorer_audit(
    event: Optional[str] = None,
    user: Annotated[UserPublic, Depends(current_admin)] = None,
):
    """Per-event soccer scorer-board coverage audit.

    Answers the user request "no eligible Anytime Goal player silently
    disappears from Score or Assist". For every player evaluated in
    the most recent refresh of the slate, returns:

        player, team, event,
        p_goal, p_assist, p_score_or_assist,
        edge_goal, edge_score_or_assist,
        anytime_survived, soa_survived,
        reason_excluded,
        anytime_market, soa_market.

    Pass `?event=Manchester United @ Arsenal` to scope to a single
    match — defaults to the entire current slate audit.
    """
    try:
        from server import get_scorer_coverage_audit
        return get_scorer_coverage_audit(event=event)
    except Exception as e:
        raise HTTPException(500, f"scorer audit failed: {e}")


# ────────────────────── GoalScorer Engine v2 ──────────────────────
@router.get("/admin/gs-engine-v2/preview")
async def gs_engine_v2_preview(
    player: str,
    team: str,
    opponent: str,
    league: str = "",
    user: Annotated[UserPublic, Depends(current_admin)] = None,
):
    """Run the v2 engine on-demand for one player.

    Pulls features from soccer_player_form + a few priors and returns
    the full prop suite plus feature provenance. Read-only — useful
    when comparing the shadow engine to the live board.
    """
    from goal_scorer_engine_v2 import (
        PlayerFeatures, compute_probabilities, get_calibration_factor,
    )
    form = await db.soccer_player_form.find_one({"name_canonical": player.lower()})
    f = PlayerFeatures(
        player=player, team=team, opponent=opponent, league=league,
        xG=float((form or {}).get("xg") or 0.0),
        xA=float((form or {}).get("xa") or 0.0),
        shot_volume=float((form or {}).get("shots_per_90") or 0.0),
        shot_quality=float((form or {}).get("xg_per_90") or 0.0)
                     / max(0.01, float((form or {}).get("shots_per_90") or 0.01)),
        minutes_played=int((form or {}).get("minutes") or 0),
        games_played=int((form or {}).get("games") or 0),
        starts=int((form or {}).get("games") or 0),
        position=str((form or {}).get("position") or "FW"),
        recent_form=float((form or {}).get("form_score") or 50) / 100.0,
    )
    cal = await get_calibration_factor(db, league=league or "GLOBAL", market="p_anytime")
    out = compute_probabilities(f, calibration_mult=cal)
    return {
        "player":         player,
        "team":           team,
        "opponent":       opponent,
        "calibration":    cal,
        "outputs":        {
            "p_anytime":         out.p_anytime,
            "p_first":           out.p_first,
            "p_last":            out.p_last,
            "p_2plus":           out.p_2plus,
            "p_score_or_assist": out.p_score_or_assist,
        },
        "lam_player":       out.lam_player,
        "lam_match":        out.lam_match,
        "expected_minutes": out.expected_minutes,
        "goal_share":       out.goal_share,
        "team_xG":          out.team_xG,
        "feature_snapshot": out.feature_snapshot,
        "feature_source":   out.feature_source,
        "engine_version":   out.engine_version,
        "calibration_version": out.calibration_version,
    }


@router.post("/admin/gs-engine-v2/grade")
async def gs_engine_v2_grade(
    user: Annotated[UserPublic, Depends(current_admin)] = None,
):
    """Trigger the FotMob-backed grading + calibration refit on demand."""
    from goal_scorer_engine_v2 import grade_pending_predictions
    return await grade_pending_predictions(db)


@router.get("/admin/gs-engine-v2/calibration")
async def gs_engine_v2_calibration(
    user: Annotated[UserPublic, Depends(current_admin)] = None,
):
    rows = []
    async for r in db.gs_v2_calibration.find({}, {"_id": 0}):
        rows.append(r)
    return {"rows": rows, "n": len(rows)}


@router.get("/admin/gs-engine-v2/residual")
async def gs_engine_v2_residual(
    league: Optional[str] = None,
    market: str = "p_anytime",
    days_back: int = 30,
    user: Annotated[UserPublic, Depends(current_admin)] = None,
):
    from goal_scorer_engine_v2 import market_residual_report
    return await market_residual_report(db, league=league, market=market,
                                         days_back=days_back)
