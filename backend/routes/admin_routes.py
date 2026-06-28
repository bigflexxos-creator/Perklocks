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


# ──── Multi-Season Backfill (5+ year historical ingestion) ────
class MultiSeasonBackfillRequest(BaseModel):
    sports: list[str] | None = None      # default: ['mlb','nba','nfl','soccer','tennis','cfb']
    seasons: list[int] | None = None     # explicit season list; else use lookback
    lookback: int = 5                    # default: 5 seasons back
    skip_if_done: bool = True            # skip (sport, season) marked done


@router.post("/admin/historical/backfill-seasons")
async def historical_backfill_seasons(
    req: MultiSeasonBackfillRequest,
    user: Annotated[UserPublic, Depends(current_admin)] = None,
):
    """Backfill multiple historical seasons for one or more sports.

    Phase 1 wires MLB end-to-end. Other sports return a "no multi-season
    client yet" stub until Phase 2 ports them. Resumable via the
    `historical_ingestion_state` collection — interrupted runs pick up
    where they left off.

    Heads up: MLB full 5-year backfill walks ~1,200 days × ~15 games/day
    = ~18k boxscore fetches paced at 5/sec. Roughly 60 minutes on cold DB.
    Re-runs that hit `skip_if_done` finish in seconds.
    """
    try:
        from historical.multi_season import backfill_seasons
    except Exception as e:
        raise HTTPException(500, f"Multi-season engine not loaded: {e}")

    # Kick off in background — the HTTP request returns immediately so
    # the admin doesn't hit a gateway timeout on a 60-minute ingest.
    import asyncio
    asyncio.create_task(backfill_seasons(
        db,
        sports=req.sports,
        seasons=req.seasons,
        lookback=max(1, min(10, int(req.lookback))),
        skip_if_done=bool(req.skip_if_done),
    ))
    return {
        "queued": True,
        "sports": req.sports or ["mlb", "nba", "nfl", "soccer", "tennis", "cfb"],
        "seasons": req.seasons,
        "lookback": req.lookback,
        "skip_if_done": req.skip_if_done,
        "note": "Backfill runs in background. Poll /api/admin/historical/ingestion-status.",
    }


@router.get("/admin/historical/ingestion-status")
async def historical_ingestion_status(
    user: Annotated[UserPublic, Depends(current_admin)] = None,
):
    """Per-(sport, season) ingestion state. Used by ops to verify
    progress of a long-running multi-season backfill."""
    try:
        from historical.multi_season import get_ingestion_status
        return await get_ingestion_status(db)
    except Exception as e:
        raise HTTPException(500, f"ingestion status failed: {e}")


# ──── Player Props Engine (derives hit-rates from game logs) ────
@router.get("/admin/historical/props/catalog")
async def historical_props_catalog(
    sport: Optional[str] = None,
    user: Annotated[UserPublic, Depends(current_admin)] = None,
):
    """List every supported player-prop market across all sports.

    Pass `?sport=mlb` to scope to one sport. Returned shape matches
    PLAYER_PROPS_CATALOG entries (key, sport, label, stat, default_lines,
    direction, role_filter).
    """
    from historical.props_engine import get_catalog
    rows = get_catalog(sport)
    return {"total": len(rows), "sport_filter": sport, "props": rows}


@router.get("/admin/historical/props/hitrate")
async def historical_props_hitrate(
    sport: str,
    player_id: str,
    stat: str,
    line: float,
    window: int = 10,
    user: Annotated[UserPublic, Depends(current_admin)] = None,
):
    """Compute the L`window` hit-rate for (player, stat, line).

    Reads from `player_game_logs`. Example:
      GET /api/admin/historical/props/hitrate?sport=mlb&player_id=605141
                                              &stat=hits&line=0.5&window=10
    """
    from historical.props_engine import compute_player_hitrate
    # MLB Stats API uses int player IDs — coerce when possible.
    try:
        pid: object = int(player_id)
    except (TypeError, ValueError):
        pid = player_id
    out = await compute_player_hitrate(
        db, player_id=pid, sport=sport, stat=stat,
        line=float(line), window=max(1, min(50, int(window))),
    )
    return out


@router.get("/admin/historical/props/summary")
async def historical_props_summary(
    sport: str,
    player_id: str,
    user: Annotated[UserPublic, Depends(current_admin)] = None,
):
    """Full L5/L10/L20/season hit-rate matrix for one player across all
    catalog props in their sport. Reads from `props_history` if a recent
    snapshot exists; falls back to live computation otherwise.
    """
    from historical.props_engine import (
        get_player_props_snapshot, compute_player_props_summary,
    )
    try:
        pid: object = int(player_id)
    except (TypeError, ValueError):
        pid = player_id
    snap = await get_player_props_snapshot(db, player_id=pid, sport=sport)
    if snap:
        return {**snap, "from_cache": True}
    # No snapshot yet — compute live (slower).
    live = await compute_player_props_summary(db, player_id=pid, sport=sport)
    return {**live, "from_cache": False}


class PropsRecomputeRequest(BaseModel):
    sport: Optional[str] = None
    limit: Optional[int] = None


@router.post("/admin/historical/props/recompute")
async def historical_props_recompute(
    req: PropsRecomputeRequest,
    user: Annotated[UserPublic, Depends(current_admin)] = None,
):
    """Bulk-recompute the props snapshot for every player with logs.

    `sport` scopes to one sport; omit for all. `limit` caps how many
    players we hit this run (useful for nightly incremental refreshes).
    """
    from historical.props_engine import recompute_all_props
    return await recompute_all_props(db, sport=req.sport, limit=req.limit)


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



# ────────────────────── Odds API circuit / diagnostics ──────────────────────
@router.get("/admin/odds-diagnostic")
async def odds_diagnostic(
    user: Annotated[UserPublic, Depends(current_admin)] = None,
):
    """Production health probe for The Odds API.

    Exposes everything the operator needs to triage a "no picks" outage
    WITHOUT having to dig through container logs:

      • Whether THE_ODDS_API_KEY is loaded (and the last 4 chars so they
        can confirm which key is active vs. their dashboard).
      • Circuit-breaker state (open/closed + the exact reason it tripped).
      • Streak + total counters so they can see WHY it tripped (e.g.
        2 consecutive 401s = auth, 8 timeouts = network outage).
      • Counts of picks in the DB scoped to today so the operator can
        immediately tell whether the issue is generation (no new picks)
        or surfacing (picks exist but filters hide them).

    Returned object is intentionally JSON-safe with no PII. Admin-only.
    """
    from sports_engine import get_odds_api_status
    status = get_odds_api_status()
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    total_today = await db.picks.count_documents({"pick_date": today_str})
    high_lock_today = await db.picks.count_documents({
        "pick_date": today_str,
        "$or": [
            {"lock_score": {"$gte": 85}},
            {"lock_score_v2": {"$gte": 85}},
        ],
    })
    # Latest 3 picks across the DB so the operator can spot-check freshness
    latest = []
    cursor = db.picks.find(
        {},
        {"_id": 0, "sport": 1, "market": 1, "lock_score": 1,
         "lock_score_v2": 1, "created_at": 1, "event_time": 1, "pick_date": 1},
    ).sort("created_at", -1).limit(3)
    async for p in cursor:
        latest.append(p)
    return {
        "odds_api": status,
        "picks_today_total": total_today,
        "picks_today_high_lock": high_lock_today,
        "today_utc": today_str,
        "latest_picks_sample": latest,
    }


@router.post("/admin/odds-circuit/reset")
async def odds_circuit_reset(
    user: Annotated[UserPublic, Depends(current_admin)] = None,
):
    """Re-arm the Odds API circuit breaker.

    Call this AFTER rotating `THE_ODDS_API_KEY` in production secrets.
    Without this, the next `/api/picks/today` refresh will still skip
    every Odds API call because the breaker is latched OPEN from the
    failures with the previous key.

    Returns the post-reset status so the operator can verify the breaker
    is closed before triggering a refresh.
    """
    from sports_engine import reset_odds_api_circuit
    return reset_odds_api_circuit()


@router.post("/admin/picks/force-refresh")
async def admin_force_refresh(
    user: Annotated[UserPublic, Depends(current_admin)] = None,
):
    """Admin-only emergency refresh.

    Bypasses the per-user 1-hour cooldown enforced by `/api/picks/refresh`
    AND auto-resets the Odds API circuit breaker first. Use this as the
    one-shot fix after rotating `THE_ODDS_API_KEY` in production secrets:

        1. Push new key to production env vars
        2. Restart backend (or wait for the new pod to come up)
        3. POST /api/admin/picks/force-refresh   ← this endpoint
        4. Wait ~45s, then GET /api/admin/odds-diagnostic to verify
           `total_ok > 0` and `picks_today_total > 0`

    Returns immediately; the refresh runs in the background.
    """
    import asyncio
    from sports_engine import reset_odds_api_circuit
    # Re-arm the breaker first — pointless to refresh if it's still open.
    pre_state = reset_odds_api_circuit()
    # Lazy import to avoid circular dep at module load.
    from server import _refresh_picks, _today_str
    asyncio.create_task(_refresh_picks(_today_str()))
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    existing = await db.picks.count_documents({"pick_date": today_str})
    return {
        "queued": True,
        "date": today_str,
        "existing_count": existing,
        "circuit_state_after_reset": pre_state,
        "message": "Refresh queued. Poll /api/admin/odds-diagnostic in ~45s.",
    }


@router.get("/admin/picks/heal")
async def admin_picks_heal(
    user: Annotated[UserPublic, Depends(current_admin)] = None,
):
    """One-click healing endpoint (GET so it works from a browser URL bar).

    Performs the full triage sequence in a single call:
      1. Snapshot Odds API state (so operator can see WHY it was broken).
      2. Reset the circuit breaker.
      3. Queue a background refresh of today's picks.
      4. Return the pre-state + queued status.

    Designed for the "production board is empty, fix it NOW" scenario.
    Hit this URL while logged in as admin in the browser; the response
    JSON tells you exactly what was wrong. Poll `/api/admin/odds-diagnostic`
    ~45s later to see the result.
    """
    import asyncio
    from sports_engine import get_odds_api_status, reset_odds_api_circuit
    pre_state = get_odds_api_status()
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    pre_count = await db.picks.count_documents({"pick_date": today_str})
    # Reset breaker and queue refresh
    reset_odds_api_circuit()
    from server import _refresh_picks
    asyncio.create_task(_refresh_picks(today_str))
    return {
        "healing_queued": True,
        "date": today_str,
        "pre_state": {
            "odds_api": pre_state,
            "picks_today_count": pre_count,
        },
        "next_step": "Wait 45s, then refresh app or check /api/admin/odds-diagnostic",
        "common_causes": [
            "THE_ODDS_API_KEY missing/wrong in production secrets",
            "Quota exhausted (free tier = 500/month)",
            "Network blip caused the breaker to trip (this endpoint resets it)",
        ],
    }


# ────────────────────── CSL ESPN Live (retired-player filter) ──────────────────────
@router.get("/admin/csl-espn-status")
async def admin_csl_espn_status(
    user: Annotated[UserPublic, Depends(current_admin)],
):
    """Inspect the in-memory CSL ESPN cache used to block retired players
    from synth goal-scorer picks. Includes the top-10 current-season
    scorers with their ESPN active flag — verifies live data is flowing.

    Wired 2026-06-27 in response to user feedback that retired players
    (e.g. Guy Mbenza after his transfer) were landing on the board.
    """
    import csl_espn_live
    return csl_espn_live.snapshot_state()


class _CSLRefreshOut(BaseModel):
    ok: bool
    season: Optional[str] = None
    teams: Optional[int] = None
    players_active: Optional[int] = None
    scorer_rows: Optional[int] = None
    inactive_seen: Optional[int] = None
    elapsed_sec: Optional[float] = None
    refreshed_at: Optional[str] = None
    reason: Optional[str] = None


@router.post("/admin/csl-espn-refresh", response_model=_CSLRefreshOut)
async def admin_csl_espn_refresh(
    user: Annotated[UserPublic, Depends(current_admin)],
):
    """Force an immediate refresh of the CSL ESPN cache (bypasses 12-h
    scheduler). Use after a known transfer / retirement is reported."""
    import csl_espn_live
    summary = await csl_espn_live.refresh(db)
    return _CSLRefreshOut(**summary)


@router.get("/admin/csl-active-check")
async def admin_csl_active_check(
    user: Annotated[UserPublic, Depends(current_admin)],
    name: str,
):
    """Quick lookup: is this player currently active in CSL per ESPN?
    Returns the verdict + raw match info so we can debug false-positive
    or false-negative blocks during seed-data audits."""
    import csl_espn_live
    verdict = csl_espn_live.is_player_currently_active(name)
    live = csl_espn_live.get_live_form(name)
    return {
        "query": name,
        "active": verdict,                       # True / False / None
        "interpretation": (
            "active in CSL" if verdict is True
            else "NOT active / retired / transferred out" if verdict is False
            else "unknown (data missing or stale)"
        ),
        "live_form": live,
    }


# ──────────────────── services/ multi-source ingestion ────────────────────
@router.get("/admin/services-registry-status")
async def admin_services_registry_status(
    user: Annotated[UserPublic, Depends(current_admin)],
):
    """Inspect the unified active-player registry that backs the
    `services/` multi-source ingestion layer (NBA + NFL today; soccer
    coming in Phase 2). Reports per-sport totals and which free sources
    successfully reported in the last refresh."""
    from services import active_registry
    return active_registry.snapshot_state()


@router.post("/admin/services-nba-refresh")
async def admin_services_nba_refresh(
    user: Annotated[UserPublic, Depends(current_admin)],
):
    """Force an NBA refresh across ESPN + BBR + nba.com/stats. Logs the
    per-source row counts; useful after adding a residential proxy or
    confirming BBR/PFR scraping headers still work."""
    from services import nba_ingest
    return await nba_ingest.refresh(db)


@router.post("/admin/services-nfl-refresh")
async def admin_services_nfl_refresh(
    user: Annotated[UserPublic, Depends(current_admin)],
):
    """Force an NFL refresh across ESPN + nfl.com + PFR."""
    from services import nfl_ingest
    return await nfl_ingest.refresh(db)


@router.get("/admin/services-active-check")
async def admin_services_active_check(
    user: Annotated[UserPublic, Depends(current_admin)],
    sport: str,
    name: str,
):
    """Debug: ask the unified registry whether a specific player is
    currently active for the given sport. Useful during seed audits."""
    from services import active_registry
    verdict = active_registry.is_active(sport, name)
    rec = active_registry.get_record(sport, name)
    return {
        "sport": sport,
        "query": name,
        "active": verdict,
        "interpretation": (
            "active" if verdict is True
            else "not active / retired / never seen" if verdict is False
            else "unknown (data missing or stale)"
        ),
        "record": rec,
    }
