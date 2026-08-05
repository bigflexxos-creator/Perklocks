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


@router.get("/admin/odds-health")
async def admin_odds_health(
    user: Annotated[UserPublic, Depends(current_admin)],
):
    """Public odds-provider health & active-source snapshot (iter-93).

    Returns whether we're serving live Odds API data, or degraded to
    API-Sports / ESPN backup. Use this to monitor the Sat-Sun weekend
    when the Odds API subscription is expected to be down.
    """
    from services.odds_provider import status as _odds_status
    return await _odds_status()


# ═════════════════════════════════════════════════════════════════════
# Phase A/B — Odds API burn control endpoints (2026-08)
# ═════════════════════════════════════════════════════════════════════
@router.post("/admin/alt-lines/snapshot")
async def admin_alt_lines_snapshot(
    user: Annotated[UserPublic, Depends(current_admin)],
    picks_scope: bool = True,
    event_window_hours: int = 36,
):
    """Trigger a one-shot alt-lines snapshot immediately.

    Normal cadence is 3×/day (12:00 / 18:00 / 23:00 UTC).  Ops can
    fire an out-of-band snapshot from this endpoint — e.g. after a
    new game-day board drops.  `picks_scope=True` restricts fetching
    to sports/events that already have picks today.
    """
    from alt_lines_feed import refresh_alt_lines
    return await refresh_alt_lines(
        db, picks_scope=picks_scope,
        event_window_hours=max(6, min(96, event_window_hours)),
    )


@router.get("/admin/alt-lines/bad-markets")
async def admin_bad_market_registry(
    user: Annotated[UserPublic, Depends(current_admin)],
):
    """Return the current bad-market registry contents.

    Any (sport_key, market) pair here has returned a 422 from The
    Odds API in the last 24 h.  We skip these tuples during alt-line
    snapshots to avoid burning credits on markets Odds API doesn't
    support for that sport.  Entries auto-expire after 24 h.
    """
    from services.bad_market_registry import stats as bmr_stats
    return await bmr_stats(db)


@router.get("/admin/odds-usage")
async def admin_odds_usage_report(
    user: Annotated[UserPublic, Depends(current_admin)],
    hours: int = 24,
):
    """Aggregate the Odds API request log over the last `hours` hours.

    Returns:
      • total_requests, upstream_requests, cache_hit_rate
      • estimated_credits_used + monthly projection
      • by_endpoint / by_sport / top_callers breakdowns

    Powers the ops dashboard that shows whether the burn-reduction
    changes (picks-scope, snapshot cadence, bad-market registry) are
    delivering the expected savings.
    """
    from services.odds_cache import get_odds_usage_report
    return await get_odds_usage_report(hours=max(1, min(168, hours)))



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


@router.post("/admin/rollover/backfill-tags")
async def admin_backfill_rollover_tags(
    user: Annotated[UserPublic, Depends(current_admin)] = None,
    days: int = 60,
):
    """One-shot backfill: re-derives the V4 top-3 rollover slate for
    each of the last `days` graded dates and stamps `on_rollover_at`
    onto EXACTLY those 3 picks.  Any picks that had the tag but no
    longer belong in the top-3 (because a settlement re-fired and
    outcomes shifted) get UNTAGGED.  Fully reconstructive so safe to
    re-run.

    This is the fix for the History → Rollover mismatch reported
    2026-07-08 where MLB alt totals were showing as "rollover picks"
    even though they were never on the live Rollover tab.
    """
    from rollover_history_tagger import stamp_rollover_history_tags
    from datetime import date as _date, timedelta as _td
    cutoff = (_date.today() - _td(days=days)).isoformat()
    pipeline = [
        {"$match": {"status": {"$in": ["won", "lost", "push"]},
                    "pick_date": {"$gte": cutoff}}},
        {"$group": {"_id": "$pick_date"}},
    ]
    dates_docs = await db.picks.aggregate(pipeline).to_list(days * 2)
    dates = [r["_id"] for r in dates_docs]
    res = await stamp_rollover_history_tags(db, dates=dates)
    return {"ok": True, "days_range": days, "result": res}


# ────────────────────── Rationale re-enrichment (v2 pitcher props) ──────────────────────
@router.post("/admin/picks/re-enrich-rationale")
async def admin_reenrich_rationale(
    user: Annotated[UserPublic, Depends(current_admin)] = None,
    scope: str = "today",   # "today" | "week" | "all"
    sport: Optional[str] = None,
):
    """Force re-enrich `pick_rationale` on existing picks so a newly-
    deployed rationale layer (e.g. the 2026-07-07 stat-specific
    pitcher-props builder) rewrites stale in-DB rationales without
    waiting for the 06:00 UTC daily rebuild.

    Use case: after a code fix that changes what evidence a market
    surfaces (Pitcher-Outs no longer showing K's, Cheatsheet gaining
    streak / trend facts, etc.) — the production DB still has picks
    with pre-fix rationale blocks stuck to them.  This endpoint runs
    the enricher over the picks matching `scope` / `sport` and
    persists the refreshed `pick_rationale` + `recent_form`.

    Scope:
      • "today" (default) — only today's `pick_date` picks
      • "week"            — last 7 days
      • "all"             — every pick in the collection (SLOW, admin
                             confirmation required)
    """
    import asyncio
    from datetime import date as _date, timedelta as _td
    from pick_enrichment import enrich_picks_with_active_registry
    from motor.motor_asyncio import AsyncIOMotorClient  # noqa: F401
    q: dict = {}
    today = _date.today().isoformat()
    if scope == "today":
        q["pick_date"] = today
    elif scope == "week":
        cutoff = (_date.today() - _td(days=7)).isoformat()
        q["pick_date"] = {"$gte": cutoff}
    elif scope != "all":
        raise HTTPException(400, f"unknown scope: {scope}")
    if sport:
        q["sport"] = sport
    cursor = db.picks.find(q)
    batch: list = []
    BATCH = 200
    seen = enriched_ct = updated = 0

    async def _flush(batch: list) -> None:
        nonlocal enriched_ct, updated
        if not batch:
            return
        counts = enrich_picks_with_active_registry(batch)
        enriched_ct += counts.get("enriched", 0) + counts.get("skipped_team_pick", 0)
        for pick in batch:
            if isinstance(pick.get("pick_rationale"), dict) and pick["pick_rationale"]:
                await db.picks.update_one(
                    {"id": pick["id"]},
                    {"$set": {"pick_rationale": pick["pick_rationale"]}},
                )
                updated += 1

    async for doc in cursor:
        seen += 1
        doc.pop("_id", None)
        batch.append(doc)
        if len(batch) >= BATCH:
            await _flush(batch)
            batch = []
    await _flush(batch)
    return {
        "ok": True,
        "scope": scope,
        "sport": sport,
        "scanned": seen,
        "enriched": enriched_ct,
        "db_updated": updated,
        "message": (
            f"Re-enriched {enriched_ct}/{seen} picks. Pitcher-Outs cards "
            "now show outs stats; Cheatsheet L5/L10/L20 chip uses the "
            "correct per-market stat."
        ),
    }


@router.post("/admin/scorer-backfill")
async def admin_scorer_backfill(
    user: Annotated[UserPublic, Depends(current_admin)] = None,
    days: int = 120,
    max_players: int = 25,
    purge: bool = True,
):
    """Fire the ESPN Anytime-Goal-Scorer backfill in a background task.

    The Cheatsheets module requires ≥ 5 settled Anytime Goal Scorer
    picks per elite player to surface a card.  Production picks come
    in organically at 1-2 per week per player, so it can take months
    before Kane / Haaland / Messi appear.  This endpoint mines ESPN's
    public soccer summaries for the last `days` days and synthesises
    settled "won"/"lost" picks in the `picks` collection with
    `backfilled: true`.

    v2 (2026-07-07) uses roster-verified inserts — matches where the
    player wasn't actually on the ESPN team roster are SKIPPED, not
    recorded as losses.  This eliminates the "Kane 25%" bug where
    "New England Revolution" MLS matches were being credited as Kane
    appearances.

    Query params:
      • days=120       days back to scan (default 120)
      • max_players=25 cap on players (default 25)
      • purge=true     wipe legacy `backfilled: true` rows first
    """
    import asyncio
    import httpx
    from scripts.backfill_scorer_picks import (
        _get_elite_scorers, _backfill_player, _prefetch_scoreboards,
        _purge_stale_backfill,
    )

    async def _runner():
        if purge:
            await _purge_stale_backfill()
        players = await _get_elite_scorers(max_players)
        totals = {"inserted": 0, "updated": 0, "skipped": 0, "players": 0}
        async with httpx.AsyncClient() as client:
            scoreboard_cache = await _prefetch_scoreboards(client, days)
            summary_cache: dict = {}
            for p in players:
                name = p["player_name"]
                nat = p.get("national_aliases") or []
                club = p.get("club_aliases") or []
                if not nat and not club:
                    continue
                try:
                    r = await _backfill_player(
                        client, name, nat, club, days,
                        scoreboard_cache, summary_cache,
                    )
                    totals["inserted"] += r["inserted"]
                    totals["updated"] += r["updated"]
                    totals["skipped"] += r["skipped"]
                    totals["players"] += 1
                except Exception:
                    pass

    asyncio.create_task(_runner())
    return {
        "queued": True,
        "days": days,
        "max_players": max_players,
        "purge": purge,
        "message": (
            "Backfill queued in background. Takes ~3-5 minutes for 25 "
            "players over 120 days. Refresh Cheatsheets after."
        ),
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


@router.post("/admin/uefa-espn-refresh")
async def admin_uefa_espn_refresh(
    user: Annotated[UserPublic, Depends(current_admin)],
    days: int = 7,
):
    """Force-refresh the UEFA ESPN fallback ingest (Champions League,
    Europa League, Conference League + qualification rounds).

    ESPN's public scoreboard carries these matches days before The Odds
    API populates them, so we ingest from there and dedupe against
    football-data-backed picks. Runs on a 30-min loop normally.
    """
    from uefa_espn_ingest import sync_uefa_espn_picks
    return await sync_uefa_espn_picks(db, days_ahead=days)


@router.post("/admin/ufc-espn-refresh")
async def admin_ufc_espn_refresh(
    user: Annotated[UserPublic, Depends(current_admin)],
    days: int = 21,
):
    """Force-refresh the UFC / PFL / Bellator ESPN ingest."""
    from ufc_espn_ingest import sync_ufc_espn_picks
    return await sync_ufc_espn_picks(db, days_ahead=days)


@router.post("/admin/espn-team-meta-refresh")
async def admin_espn_team_meta_refresh(
    user: Annotated[UserPublic, Depends(current_admin)],
):
    """Rehydrate the `espn_team_meta` collection (logos + colors) for
    every sport. Runs on a 6h background loop; hit this after a team
    rebrand to see the new crest immediately."""
    from services.espn_team_meta import refresh_all_teams
    return await refresh_all_teams(db)


@router.post("/admin/espn-injury-refresh")
async def admin_espn_injury_refresh(
    user: Annotated[UserPublic, Depends(current_admin)],
):
    """Rehydrate `espn_injury_notes` collection for NFL / CFB / NBA.
    Powers the small red 🚑 chip on pick cards + the injuries panel
    inside `Why This Pick?`."""
    from services.espn_injury_notes import refresh_all_injuries
    return await refresh_all_injuries(db)


@router.post("/admin/espn-form-refresh")
async def admin_espn_form_refresh(
    user: Annotated[UserPublic, Depends(current_admin)],
):
    """Refresh the `espn_form_cache` collection. Feeds recent-form
    strings ('LLLWL') into the Signal Engine so form deltas actually
    move the pick probability."""
    from services.espn_form_cache import refresh_all_forms
    return await refresh_all_forms(db)


@router.post("/admin/wiki-record-refresh")
async def admin_wiki_record_refresh(
    user: Annotated[UserPublic, Depends(current_admin)],
    limit_teams: int = 200,
):
    """Refresh Wikipedia season-record cache for soccer teams in
    recent picks. Deep W/D/L history feeds the Season-Record Signal
    (up to ±4pp) so niche-league teams like Mornar (20W-9D-7L) actually
    get recognized instead of getting judged on ESPN's 5-game window."""
    from services.wikipedia_team_record import bulk_refresh_soccer
    return await bulk_refresh_soccer(db, limit_teams=limit_teams)


@router.post("/admin/wiki-top-scorers-refresh")
async def admin_wiki_top_scorers_refresh(
    user: Annotated[UserPublic, Depends(current_admin)],
):
    """Refresh Wikipedia top-scorer cache for every covered league.
    Feeds the Soccer Hot Scorers module, which emits stats-driven
    Anytime-Goal-Scorer picks for niche leagues (Allsvenskan,
    Eliteserien) where sportsbook coverage is patchy."""
    from services.wiki_top_scorers import refresh_top_scorers
    return await refresh_top_scorers(db)


@router.post("/admin/soccer-hot-scorers-refresh")
async def admin_soccer_hot_scorers_refresh(
    user: Annotated[UserPublic, Depends(current_admin)],
    days: int = 4,
):
    """Force a Hot Scorers emission for all upcoming soccer fixtures
    in covered leagues."""
    from soccer_hot_scorers import sync_hot_scorers
    return await sync_hot_scorers(db, days_ahead=days)


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


# ──────────────────── MLB Hitter Prop Intelligence Engine ────────────────────
@router.get("/admin/mlb-hitter-intel")
async def admin_mlb_hitter_intel(
    user: Annotated[UserPublic, Depends(current_admin)],
    batter_id: int,
    pitcher_id: int,
    ballpark: Optional[str] = None,
    batting_order: Optional[int] = None,
    is_home: bool = True,
    season: Optional[int] = None,
):
    """Run the MLB Hitter Prop Intelligence engine for an arbitrary
    batter × pitcher matchup. Returns the full rationale block (splits,
    pitcher quality, ballpark factor, recent form, multipliers, final
    hit prob, confidence). Lets you eyeball any matchup before slate
    lock — answers the user's "show me WHY this pick made the board".
    """
    from services import mlb_hitter_intel as engine
    m = await engine.build_matchup(
        db, batter_id, pitcher_id,
        ballpark=ballpark, batting_order=batting_order,
        is_home=is_home, season=season,
    )
    return m.to_rationale()


@router.get("/admin/mlb-hitter-lean")
async def admin_mlb_hitter_lean(
    user: Annotated[UserPublic, Depends(current_admin)],
    batter_id: int,
    pitcher_id: int,
    market_implied_prob: float,
    line: float = 0.5,
    ballpark: Optional[str] = None,
    batting_order: Optional[int] = None,
    is_home: bool = True,
    season: Optional[int] = None,
):
    """Run the engine AND return an OVER/UNDER lean + edge vs the given
    sportsbook implied probability. `line` defaults to 0.5 (Anytime Hit
    prop). Pass 1.5 for "Over 1.5 Hits", etc."""
    from services import mlb_hitter_intel as engine
    m = await engine.build_matchup(
        db, batter_id, pitcher_id,
        ballpark=ballpark, batting_order=batting_order,
        is_home=is_home, season=season,
    )
    lean = engine.lean_and_edge(m, market_implied_prob, line=line)
    return {**lean, "rationale": m.to_rationale()}


@router.post("/admin/services-soccer-refresh")
async def admin_services_soccer_refresh(
    user: Annotated[UserPublic, Depends(current_admin)],
):
    """Force a soccer refresh across Understat (top-5 European leagues),
    ESPN public (18 other competitions), and FotMob (best-effort).
    Returns per-league row counts so we can audit Understat decoding
    failures vs ESPN coverage."""
    from services import soccer_ingest
    return await soccer_ingest.refresh(db)


# ──────────────────── CFB (CollegeFootballData.com) ──────────────────
@router.post("/admin/services-cfb-refresh")
async def admin_services_cfb_refresh(
    user: Annotated[UserPublic, Depends(current_admin)],
    year: Optional[int] = None,
):
    """Force a CFB ingestion sweep against the CollegeFootballData free
    public API. Caches:
      • Returning Production per team
      • Transfer Portal entries
      • SP+ ratings (overall / off / def / SoS)
      • FBS team metadata
    Returns per-dataset row counts so we can confirm the API key is
    working before relying on the rationale enrichment downstream."""
    from services import cfb_ingest
    return await cfb_ingest.refresh_all(db, year=year)


@router.get("/admin/services-cfb-team")
async def admin_services_cfb_team(
    user: Annotated[UserPublic, Depends(current_admin)],
    team: str,
    year: Optional[int] = None,
):
    """Debug lookup: returns the cached CFBD profile for a given school
    (SP+ ratings, returning production, team metadata). Use to audit
    why a CFB pick produced (or didn't produce) the rationale you
    expected on the home card."""
    from services import cfb_ingest
    return await cfb_ingest.get_team_record(db, team, year=year)


@router.get("/admin/services-cfb-rationale")
async def admin_services_cfb_rationale(
    user: Annotated[UserPublic, Depends(current_admin)],
    team: str,
    opponent: Optional[str] = None,
    player: Optional[str] = None,
    year: Optional[int] = None,
):
    """Build the full CFB rationale dict for an arbitrary team/opponent/
    player. Lets us preview what the pick card will show before the
    season opens (since no live CFB picks land on the board until
    August)."""
    from services import cfb_rationale
    return await cfb_rationale.build_cfb_rationale(
        db, team, opponent=opponent, player_name=player, year=year,
    )


# ── Phase 2 — Soccer multi-source ingest diagnostics ─────────────────
@router.get("/admin/soccer/status")
async def soccer_ingest_status(user: Annotated[UserPublic, Depends(current_admin)]):
    """Diagnostic snapshot of the soccer multi-source cache.

    Returns per-source counts, per-league counts, coverage of closing
    odds, and the last ingest run for each provider. Use this to verify
    every provider is contributing and none has silently gone stale.
    """
    result: dict = {"providers": {}, "totals": {}, "coverage": {}, "top_leagues": []}

    # Aggregate matches by source
    async for d in db.soccer_matches.aggregate([
        {"$group": {"_id": "$source", "n": {"$sum": 1}}},
        {"$sort": {"n": -1}},
    ]):
        result["providers"].setdefault(d["_id"] or "unknown", {})["matches"] = d["n"]

    # Aggregate teams by source
    async for d in db.soccer_teams.aggregate([
        {"$group": {"_id": "$source", "n": {"$sum": 1}}},
    ]):
        result["providers"].setdefault(d["_id"] or "unknown", {})["teams"] = d["n"]

    # Standings + fixtures by source
    async for d in db.soccer_standings.aggregate([
        {"$group": {"_id": "$source", "n": {"$sum": 1}}},
    ]):
        result["providers"].setdefault(d["_id"] or "unknown", {})["standings"] = d["n"]

    async for d in db.soccer_fixtures.aggregate([
        {"$group": {"_id": "$source", "n": {"$sum": 1}}},
    ]):
        result["providers"].setdefault(d["_id"] or "unknown", {})["fixtures"] = d["n"]

    result["totals"] = {
        "matches":   await db.soccer_matches.count_documents({}),
        "teams":     await db.soccer_teams.count_documents({}),
        "standings": await db.soccer_standings.count_documents({}),
        "fixtures":  await db.soccer_fixtures.count_documents({}),
    }

    # Coverage: how many matches have closing odds
    total = result["totals"]["matches"]
    with_close = await db.soccer_matches.count_documents(
        {"home_odds_close": {"$ne": None}})
    result["coverage"]["matches_with_closing_odds"] = with_close
    result["coverage"]["closing_odds_pct"] = (
        round(with_close * 100 / total, 2) if total else 0.0
    )

    # Top leagues by match count
    async for d in db.soccer_matches.aggregate([
        {"$group": {"_id": "$league", "n": {"$sum": 1}}},
        {"$sort": {"n": -1}},
        {"$limit": 15},
    ]):
        result["top_leagues"].append({"league": d["_id"], "matches": d["n"]})

    # Last run per source
    result["last_runs"] = {}
    async for d in db.soccer_ingest_log.aggregate([
        {"$sort": {"at": -1}},
        {"$group": {"_id": "$source", "last": {"$first": "$$ROOT"}}},
    ]):
        result["last_runs"][d["_id"]] = {
            "at":     d["last"].get("at"),
            "kind":   d["last"].get("kind"),
            "result": d["last"].get("result"),
        }

    return result


@router.post("/admin/soccer/refresh")
async def soccer_refresh_now(
    user: Annotated[UserPublic, Depends(current_admin)],
    seasons: str = "2024-25,2023-24",
):
    """Manual trigger for a full soccer multi-source refresh. Non-blocking
    (returns immediately with the run started in background)."""
    import asyncio
    from services.soccer import refresh_all_leagues

    seasons_list = [s.strip() for s in seasons.split(",") if s.strip()]

    async def _run():
        try:
            r = await refresh_all_leagues(db, seasons=tuple(seasons_list))
            return r
        except Exception as e:
            return {"error": str(e)}

    asyncio.create_task(_run())
    return {"status": "started", "seasons": seasons_list,
            "hint": "Poll GET /api/admin/soccer/status for progress"}


@router.get("/admin/soccer/team/{team_name}")
async def soccer_team_lookup(
    team_name: str,
    user: Annotated[UserPublic, Depends(current_admin)],
    days: int = 90,
):
    """Recent-form snapshot for a team from the multi-source cache.

    Returns the last N days of finished matches involving `team_name`,
    including closing odds when available. Powers the recent-form
    signal in signal_engine.soccer_deep_signal at scoring time.
    """
    from datetime import datetime, timezone, timedelta
    since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    q = {
        "$or": [
            {"home_team": {"$regex": team_name, "$options": "i"}},
            {"away_team": {"$regex": team_name, "$options": "i"}},
        ],
        "status": "finished",
        "date":   {"$gte": since},
    }
    matches: list[dict] = []
    async for m in db.soccer_matches.find(q, {"_id": 0}).sort("date", -1).limit(20):
        # Which side is our team on?
        home = (m.get("home_team") or "").lower()
        away = (m.get("away_team") or "").lower()
        needle = team_name.lower()
        is_home = needle in home
        for_score = m.get("home_score") if is_home else m.get("away_score")
        against_score = m.get("away_score") if is_home else m.get("home_score")
        if for_score is None or against_score is None:
            outcome = "?"
        elif for_score > against_score:
            outcome = "W"
        elif for_score < against_score:
            outcome = "L"
        else:
            outcome = "D"
        matches.append({
            "date":        m.get("date"),
            "league":      m.get("league"),
            "opponent":    m.get("away_team") if is_home else m.get("home_team"),
            "venue":       "H" if is_home else "A",
            "score":       f"{for_score}-{against_score}" if for_score is not None else None,
            "outcome":     outcome,
            "closing_odds": {
                "for":   m.get("home_odds_close") if is_home else m.get("away_odds_close"),
                "draw":  m.get("draw_odds_close"),
                "against": m.get("away_odds_close") if is_home else m.get("home_odds_close"),
            },
            "source":      m.get("source"),
        })
    if not matches:
        return {"team": team_name, "matches": [], "form": None, "note": "No cached matches"}
    form = "".join(m["outcome"] for m in matches[:5])
    wins = sum(1 for m in matches if m["outcome"] == "W")
    draws = sum(1 for m in matches if m["outcome"] == "D")
    losses = sum(1 for m in matches if m["outcome"] == "L")
    return {
        "team": team_name,
        "days": days,
        "matches": matches,
        "form_last_5": form,
        "record": {"W": wins, "D": draws, "L": losses,
                    "n": wins + draws + losses},
    }



# ────────────── Player Prop Intelligence System (Phase 2) ──────────────
# Debug endpoint to inspect what the archetype engine + 3 market models
# predict for any given player. Useful for validating model behaviour on
# real MLS / EPL rosters before / after tuning.
#
#   GET /api/admin/player-props/analyze/{player_name}?opponent=Nashville%20SC
#
# Returns: unified PlayerStats, computed Archetype, and outputs of
# goalscorer / assist / goal_involvement models.
@router.get("/admin/player-props/analyze/{player_name}")
async def admin_player_props_analyze(
    player_name: str,
    user: Annotated[UserPublic, Depends(current_admin)],
    opponent: Optional[str] = None,
    league_hint: Optional[str] = None,
):
    """Return the Player Prop Intelligence model outputs for one player."""
    from services.player_props import (
        get_player_stats, get_matchup_split,
        classify_archetype,
        predict_goal, predict_assist, predict_goal_involvement,
    )
    stats = await get_player_stats(player_name, league_hint=league_hint)
    if not stats:
        raise HTTPException(status_code=404, detail=(
            f"No stats found for '{player_name}'. Searched: "
            f"soccer_player_form, espn_mls_stats, wiki_top_scorers."
        ))
    split = None
    if opponent:
        split = await get_matchup_split(player_name, opponent)

    archetype = classify_archetype(stats)
    goal_rec = predict_goal(stats, split, archetype)
    assist_rec = predict_assist(stats, split, archetype)
    gi_rec = predict_goal_involvement(stats, split, archetype)

    return {
        "player_name": stats.player_name,
        "stats": {
            "source": stats.source,
            "league": stats.league,
            "team": stats.team,
            "season": stats.season,
            "games": stats.games,
            "minutes": stats.minutes,
            "goals": stats.goals,
            "assists": stats.assists,
            "goals_per_90": stats.goals_per_90,
            "assists_per_90": stats.assists_per_90,
            "shots_per_90": stats.shots_per_90,
            "key_passes_per_90": stats.key_passes_per_90,
            "npxg_per_90": stats.npxg_per_90,
            "form_score": stats.form_score,
            "form_label": stats.form_label,
            "position": stats.position,
        },
        "matchup_split": ({
            "opponent": split.opponent,
            "matches": split.matches,
            "goals": split.goals,
            "assists": split.assists,
            "scored_matches": split.scored_matches,
            "assist_matches": split.assist_matches,
            "gpm": round(split.gpm(), 3),
            "apm": round(split.apm(), 3),
            "gi_rate": round(split.gi_rate(), 3),
        } if split else None),
        "archetype": {
            "code": archetype.value,
            "display": archetype.display(),
        },
        "models": {
            "anytime_goal_scorer":    goal_rec.to_dict(),
            "anytime_assist":         assist_rec.to_dict(),
            "anytime_goal_involvement": gi_rec.to_dict(),
        },
    }


# Batch endpoint — classify every player currently in `espn_mls_stats`
# so admins can spot-check the archetype distribution + flag any weird
# thresholds needing tuning.
@router.get("/admin/player-props/mls-archetypes")
async def admin_mls_archetypes(
    user: Annotated[UserPublic, Depends(current_admin)],
    limit: int = 200,
):
    """Return archetype distribution across every MLS scorer in
    `espn_mls_stats` — quick QA for the classifier.
    """
    from services.player_props import get_player_stats, classify_archetype

    scorers = await db.espn_mls_stats.find({}).to_list(length=limit)
    rows: list[dict] = []
    dist: dict[str, int] = {}
    for sc in scorers:
        stats = await get_player_stats(sc.get("name", ""), league_hint="MLS")
        if not stats:
            continue
        arch = classify_archetype(stats)
        dist[arch.value] = dist.get(arch.value, 0) + 1
        rows.append({
            "name": stats.player_name,
            "team": stats.team,
            "goals": stats.goals,
            "assists": stats.assists,
            "games": stats.games,
            "g90": stats.goals_per_90,
            "a90": stats.assists_per_90,
            "archetype": arch.value,
            "archetype_display": arch.display(),
        })
    # sort by (archetype, -g90) for easier scanning.
    rows.sort(key=lambda r: (r["archetype"], -r["g90"]))
    return {
        "total_scorers": len(scorers),
        "classified": len(rows),
        "distribution": dist,
        "players": rows,
    }



# ═══════════════════════════════════════════════════════════════════
#  GoalScorer Engine v3 — diagnostics & sample predictions
# ═══════════════════════════════════════════════════════════════════
@router.get("/admin/goalscorer/v3/status")
async def goalscorer_v3_status(
    admin: Annotated[UserPublic, Depends(current_admin)],
) -> dict:
    """Return engine version + per-league team-strength diagnostics."""
    from services.player_props import (
        GS_V3_VERSION, get_league_strength, clear_cache,
    )
    leagues = ["EPL", "LaLiga", "SerieA", "Bundesliga", "Ligue1", "MLS"]
    out: dict = {
        "engine_version": GS_V3_VERSION,
        "leagues": {},
    }
    for lg in leagues:
        lg_strength, teams = await get_league_strength(db, lg)
        out["leagues"][lg] = {
            "mean_home_goals":     lg_strength.mean_home_goals,
            "mean_away_goals":     lg_strength.mean_away_goals,
            "mean_total_goals":    lg_strength.mean_total_goals,
            "home_advantage_mult": round(lg_strength.home_advantage_mult, 3),
            "matches_used":        lg_strength.matches_used,
            "seasons_used":        lg_strength.seasons_used,
            "teams_indexed":       len(teams),
            "sample_teams":        [
                {"team": t.team, "matches": t.matches,
                 "atk_home": t.lam_attack_home, "atk_away": t.lam_attack_away,
                 "def_home": t.lam_defense_home, "def_away": t.lam_defense_away}
                for t in list(teams.values())[:5]
            ],
        }
    return out


@router.post("/admin/goalscorer/v3/refresh")
async def goalscorer_v3_refresh(
    admin: Annotated[UserPublic, Depends(current_admin)],
) -> dict:
    """Force-refresh the team-strength cache from mongo."""
    from services.player_props import get_league_strength, clear_cache
    clear_cache()
    leagues = ["EPL", "LaLiga", "SerieA", "Bundesliga", "Ligue1", "MLS"]
    refreshed = {}
    for lg in leagues:
        lg_strength, teams = await get_league_strength(db, lg, force_refresh=True)
        refreshed[lg] = {
            "teams_indexed": len(teams),
            "matches_used": lg_strength.matches_used,
        }
    return {"ok": True, "refreshed": refreshed}


class V3PredictReq(BaseModel):
    player: str
    opponent: str
    league_hint: Optional[str] = None
    sport_key: Optional[str] = None
    is_home: bool = True
    lineup_status: str = "unknown"


@router.post("/admin/goalscorer/v3/predict")
async def goalscorer_v3_predict(
    body: V3PredictReq,
    admin: Annotated[UserPublic, Depends(current_admin)],
) -> dict:
    """One-shot v3 prediction for a (player, opponent) pair."""
    from services.player_props import (
        predict_goal_v3, LineupInfo, get_player_stats, classify_archetype,
    )
    stats = await get_player_stats(body.player, league_hint=body.league_hint)
    if not stats:
        raise HTTPException(404, f"no stats for player '{body.player}'")

    archetype = classify_archetype(stats)
    out = await predict_goal_v3(
        db, stats, body.opponent,
        sport_key=body.sport_key or "",
        is_home=body.is_home,
        lineup=LineupInfo(status=body.lineup_status),
        archetype=archetype,
    )
    return {
        "player": stats.player_name,
        "team":   stats.team,
        "league": stats.league,
        "archetype": archetype.value,
        "opponent": body.opponent,
        "is_home": body.is_home,
        "prediction": {
            "p_anytime":   out.p_anytime,
            "p_first":     out.p_first,
            "p_2plus":     out.p_2plus,
            "lam_player":  out.lam_player,
            "lam_team":    out.lam_team,
            "lam_opponent": out.lam_opponent,
            "expected_minutes": out.expected_minutes,
            "goal_share":  out.goal_share,
            "confidence":  out.confidence,
            "ensemble":    out.ensemble_components,
            "evidence":    out.evidence,
            "concerns":    out.concerns,
            "debug":       out.debug,
        },
        "engine_version": out.engine_version,
    }



# ═══════════════════════════════════════════════════════════════════
#  /picks/today per-sport cap diagnostic (2026-07-26)
# ═══════════════════════════════════════════════════════════════════
@router.get("/admin/picks-today/cap-diagnostic")
async def picks_today_cap_diagnostic(
    admin: Annotated[UserPublic, Depends(current_admin)],
    pick_date: Optional[str] = None,
) -> dict:
    """Introspect the /picks/today per-sport cap.

    Returns per-sport counts of picks in the candidate pool BEFORE and
    AFTER the 100-pick cap, plus a breakdown of dropped picks by lock
    band (≥90, 85-89, <85). Use this to verify the cap isn't hiding
    legit high-lock picks on heavy MLB / Soccer slates.

    Args:
        pick_date: Optional YYYY-MM-DD. When supplied, the diagnostic
            runs against that historical `pick_date`'s slate instead
            of today (useful for backtesting the cap against a heavy
            Saturday MLB slate). Note: the event_time window is still
            anchored at NOW ± 30h in today mode; in historical mode it
            widens to ±3d so all games from that pick_date qualify.

    Response shape:
        {
          "pick_date":       "2026-07-19",
          "mode":            "historical" | "today",
          "cap":             100,
          "safety_valve":    90.0,
          "sports": {
             "MLB":    {"total": 152, "kept": 100, "dropped": 52, ...},
             ...
          }
        }
    """
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    now = _dt.now(_tz.utc)
    today_str = now.strftime("%Y-%m-%d")
    is_historical = bool(pick_date) and pick_date != today_str
    target_date = pick_date or today_str

    if is_historical:
        # Historical mode: anchor the event-time window on the pick_date
        # itself (00:00 UTC ± 30h) so we replay the cap against past
        # slates.
        try:
            anchor = _dt.strptime(target_date, "%Y-%m-%d").replace(tzinfo=_tz.utc)
        except ValueError as e:
            raise HTTPException(400, f"invalid pick_date '{pick_date}': {e}")
        win_start = (anchor - _td(hours=6)).isoformat().replace("+00:00", "Z")
        win_end   = (anchor + _td(hours=54)).isoformat().replace("+00:00", "Z")
        horizon   = (anchor + _td(hours=72)).isoformat().replace("+00:00", "Z")
    else:
        horizon   = (now + _td(hours=72)).isoformat().replace("+00:00", "Z")
        win_start = (now - _td(hours=30)).isoformat().replace("+00:00", "Z")
        win_end   = (now + _td(hours=30)).isoformat().replace("+00:00", "Z")

    # Match the /picks/today outer filters (before market-specific carve-outs).
    q: dict = {
        "$and": [
            {"$or": [
                {"pick_date": target_date},
                {"event_time": {"$gte": win_start, "$lte": win_end}},
            ]},
            {"$or": [
                {"event_time": {"$lte": horizon}},
                {"event_time": {"$in": [None, ""]}},
                {"event_time": {"$exists": False}},
            ]},
        ],
        "hide_from_main_board": {"$ne": True},
        "grade":     {"$ne": "Pass"},
        "no_bet":    {"$ne": True},
        "off_board": {"$ne": True},
    }
    # In historical mode the picks are already settled, so we can't
    # filter by pending/open. In today mode we do (matches production).
    if not is_historical:
        q["status"] = {"$in": ["pending", "open", None]}

    CAP    = 100
    VALVE  = 90.0
    result: dict = {
        "pick_date":     target_date,
        "mode":          "historical" if is_historical else "today",
        "cap":           CAP,
        "safety_valve":  VALVE,
        "generated_at":  now.isoformat(),
        "sports":        {},
    }

    sports = await db.picks.distinct("sport", q)
    for sport in sorted([s for s in sports if s]):
        cursor = db.picks.find(
            {**q, "sport": sport},
            {"lock_score": 1, "lock_score_v2": 1, "selection": 1,
             "event": 1, "market_type": 1, "source": 1, "_id": 0},
        ).sort([("lock_score_v2", -1), ("lock_score", -1)]).limit(500)
        picks = await cursor.to_list(length=500)
        total = len(picks)
        if total == 0:
            continue

        def _lk(p: dict) -> float:
            return float(p.get("lock_score_v2") or p.get("lock_score") or 0)

        kept = picks[:CAP]
        beyond = picks[CAP:]
        # Safety-valve rescues from `beyond`
        rescued = [p for p in beyond if _lk(p) >= VALVE]
        dropped = [p for p in beyond if _lk(p) < VALVE]

        top_lock = round(_lk(picks[0]), 1) if picks else None
        cap_boundary_lock = round(_lk(picks[CAP - 1]), 1) if total > CAP else None

        # Break down dropped by lock band
        drop_ge90 = sum(1 for d in dropped if _lk(d) >= 90)  # should always be 0 (rescued)
        drop_85_89 = sum(1 for d in dropped if 85 <= _lk(d) < 90)
        drop_lt85  = sum(1 for d in dropped if _lk(d) < 85)

        sample_dropped = [
            {"selection": d.get("selection"), "event": d.get("event"),
             "lock": round(_lk(d), 1),
             "market": d.get("market_type") or d.get("market"),
             "source": d.get("source")}
            for d in dropped[:5]
        ]
        sample_rescued = [
            {"selection": r.get("selection"), "event": r.get("event"),
             "lock": round(_lk(r), 1)}
            for r in rescued[:5]
        ]

        result["sports"][sport] = {
            "total":               total,
            "kept":                len(kept) + len(rescued),
            "dropped":             len(dropped),
            "top_lock":            top_lock,
            "cap_boundary_lock":   cap_boundary_lock,
            "dropped_ge90":        drop_ge90,
            "dropped_85_89":       drop_85_89,
            "dropped_lt85":        drop_lt85,
            "safety_valve_kept":   len(rescued),
            "sample_dropped":      sample_dropped,
            "sample_safety_valve": sample_rescued,
        }
    return result


# ─────────────────────────────────────────────────────────────────────
# Odds API Usage & Cache Report (Phase optim: 2026-06)
# Public visibility for the SWR odds-cache in services/odds_cache.py
# ─────────────────────────────────────────────────────────────────────
@router.get("/admin/odds-usage")
async def admin_odds_usage(
    user: Annotated[UserPublic, Depends(current_admin)],
    hours: int = 24,
):
    """Return SWR odds-cache statistics + credit-usage projection.

    Fields:
      window_hours, total_requests, upstream_requests, cache_hits,
      cache_hit_rate_percent, estimated_credits_used,
      projected_monthly_credits, projected_monthly_at_10x,
      by_endpoint (top 15), by_sport (top 15), top_callers (top 10).
    """
    from services.odds_cache import get_odds_usage_report
    return await get_odds_usage_report(hours=int(hours))


@router.get("/admin/odds-cache-stats")
async def admin_odds_cache_stats(
    user: Annotated[UserPublic, Depends(current_admin)],
):
    """Raw size + freshness snapshot of the persisted odds cache."""
    from datetime import datetime as _dt, timezone as _tz
    cache_count = await db.odds_api_cache.count_documents({})
    log_count   = await db.odds_api_request_log.count_documents({})
    now = _dt.now(_tz.utc).timestamp()
    # Fresh vs stale vs expired
    from services.odds_cache import _TTL_POLICY
    fresh = stale = expired = 0
    async for d in db.odds_api_cache.find({}, {"refreshed_at": 1, "endpoint_type": 1}):
        age = now - float(d.get("refreshed_at") or 0)
        ep = d.get("endpoint_type") or "generic"
        f_ttl, s_ttl = _TTL_POLICY.get(ep, _TTL_POLICY["generic"])
        if age <= f_ttl: fresh += 1
        elif age <= s_ttl: stale += 1
        else: expired += 1
    return {
        "cached_entries": cache_count,
        "request_log_entries": log_count,
        "fresh": fresh,
        "stale": stale,
        "expired": expired,
    }



# ═════════════════════════════════════════════════════════════════════
# Phase 8 — Alt-Line Magic Tier (on-demand computation)
# ═════════════════════════════════════════════════════════════════════
@router.get("/alt-lines/{pick_id}")
async def alt_lines_for_pick(pick_id: str):
    """Compute ranked alt-line opportunities for a given pick.

    Reads the pick from `db.picks`, extracts (sport, player, stat,
    opponent), and returns a ranked list of alt lines with:
      • win probability (from ML model)
      • edge vs market implied prob (when book alt lines available)
      • historical bucket ROI (from learning_snapshots)
      • simulation stability
      • plain-English explanation
    """
    from services.alt_line_engine import generate_alt_lines
    from services.pick_fusion_decorator import _parse_pick
    pick = await db.picks.find_one({"id": pick_id}, {"_id": 0})
    if not pick:
        raise HTTPException(404, "pick not found")
    parsed = _parse_pick(pick)
    if not parsed:
        return {"pick_id": pick_id, "supported": False,
                "reason": "pick market not supported for alt lines"}
    # Fetch market alt lines if we've already cached them.
    market_alt: list[dict] = []
    try:
        from services.odds_cache import _get_db as _oc_db
        # Look for a cached event_alt_lines payload matching this event.
        event_id = pick.get("event_id") or pick.get("id")
        if event_id:
            doc = await db.odds_api_cache.find_one(
                {"endpoint_type": "event_alt_lines",
                  "url": {"$regex": event_id}},
                {"body": 1},
            )
            if doc and isinstance(doc.get("body"), dict):
                for bk in doc["body"].get("bookmakers", []):
                    for mkt in bk.get("markets", []):
                        for outcome in mkt.get("outcomes", []):
                            if outcome.get("name") in ("Over", "Under"):
                                market_alt.append({
                                    "line":       outcome.get("point"),
                                    "side":       outcome.get("name"),
                                    "american":   outcome.get("price"),
                                    "bookmaker":  bk.get("key"),
                                })
    except Exception:
        pass
    bundle = await generate_alt_lines(
        db,
        sport=parsed["sport"],
        player=parsed["player"],
        stat=parsed["stat"],
        opponent=parsed.get("opponent"),
        market_alt_lines=market_alt or None,
    )
    return {
        "pick_id":  pick_id,
        "bundle":   bundle.to_dict(),
    }


@router.get("/alt-lines/board")
async def alt_lines_board(sport: str, limit: int = 20):
    """Compute alt-line bundles for every open pick in a sport.

    Bulk endpoint for Lab / Parlay. Read-only, cache-first.
    """
    from services.alt_line_engine import generate_alt_lines
    from services.pick_fusion_decorator import _parse_pick
    cursor = db.picks.find(
        {"sport": {"$regex": sport, "$options": "i"},
          "status": {"$in": [None, "pending", "open"]}},
        {"_id": 0},
    ).limit(int(limit))
    out = []
    async for pick in cursor:
        parsed = _parse_pick(pick)
        if not parsed:
            continue
        bundle = await generate_alt_lines(
            db,
            sport=parsed["sport"],
            player=parsed["player"],
            stat=parsed["stat"],
            opponent=parsed.get("opponent"),
        )
        if bundle.alt_lines:
            out.append({"pick_id": pick.get("id"),
                          "bundle": bundle.to_dict()})
    return {"sport": sport, "n": len(out), "boards": out}
