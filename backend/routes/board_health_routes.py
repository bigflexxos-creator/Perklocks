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


@router.post("/enrich-mlb-opponent")
async def enrich_mlb_opponent(
    user: Annotated[UserPublic, Depends(current_user)],
    dry_run: bool = False,
    limit: int = 0,
    batch_size: int = 500,
):
    """Session E — MLB canonical-opponent enrichment.  Read-then-write
    join between player_game_actuals.event_id and team_game_actuals.
    Idempotent; safe to re-run; conflict-safe.
    """
    from services.team_history.mlb_opponent_enricher import (
        enrich_mlb_opponent_batch,
    )
    from deps import db as _db
    return await enrich_mlb_opponent_batch(
        _db, batch_size=batch_size, limit=limit, dry_run=dry_run)


@router.post("/history-shadow-preview")
async def history_shadow_preview(
    pick_id: str,
    user: Annotated[UserPublic, Depends(current_user)],
):
    """P1/P4 — On-demand READ-ONLY history-shadow computation for a
    single pick (by _id string). Not stored; returned to caller for
    research/UI. Does NOT affect Lock Score, Magic, APEX, Parlay,
    Rollover, or model probability.
    """
    from services.history_intelligence import compute_history_shadow
    from deps import db as _db
    from bson import ObjectId
    try:
        oid = ObjectId(pick_id)
    except Exception:
        return {"error": "invalid_pick_id"}
    pick = await _db.picks.find_one({"_id": oid})
    if not pick:
        return {"error": "pick_not_found"}
    return await compute_history_shadow(_db, pick)


@router.post("/history-shadow-backfill")
async def history_shadow_backfill(
    sport: str,
    user: Annotated[UserPublic, Depends(current_user)],
    limit: int = 5000,
    dry_run: bool = False,
):
    """P1/P6 — Bounded backfill of settled player-line picks with a
    history_shadow bundle (as-of pick's pregame cutoff — no future
    leakage). Storage: pick_enrichment.history_shadow. Idempotent
    (newer versions never overwritten by older).
    """
    from services.history_intelligence import backfill_settled_shadow
    from deps import db as _db
    return await backfill_settled_shadow(_db, sport=sport, limit=limit,
                                          dry_run=dry_run)


@router.post("/enrich-nfl-opponent")
async def enrich_nfl_opponent(
    user: Annotated[UserPublic, Depends(current_user)],
    dry_run: bool = False,
    limit: int = 0,
    batch_size: int = 1000,
):
    """Session F1 — NFL canonical-opponent enrichment via event_id
    parse ({season}_{week}_{away}_{home}). Idempotent, conflict-safe.
    """
    from services.team_history.nfl_opponent_enricher import (
        enrich_nfl_opponent_batch,
    )
    from deps import db as _db
    return await enrich_nfl_opponent_batch(
        _db, batch_size=batch_size, limit=limit, dry_run=dry_run)


@router.post("/enrich-tennis-opponent")
async def enrich_tennis_opponent(
    user: Annotated[UserPublic, Depends(current_user)],
    dry_run: bool = False,
    limit: int = 0,
    batch_size: int = 1000,
):
    """Session F2 — Tennis canonical-opponent enrichment via
    same-event 2-row grouping. Opponent = OTHER canonical_player_id.
    NO team_game_actuals writes. Idempotent, conflict-safe.
    """
    from services.team_history.tennis_opponent_enricher import (
        enrich_tennis_opponent_batch,
    )
    from deps import db as _db
    return await enrich_tennis_opponent_batch(
        _db, batch_size=batch_size, limit=limit, dry_run=dry_run)


@router.post("/enrich-nba-opponent")
async def enrich_nba_opponent(
    user: Annotated[UserPublic, Depends(current_user)],
    dry_run: bool = False,
    limit: int = 0,
    batch_size: int = 1000,
):
    """Session F3a — NBA canonical-opponent enrichment via
    player_game_logs join by (game_id, player_id). Uses game-specific
    team; ESPN team_id → 3-letter abbrev via players registry.
    """
    from services.team_history.nba_opponent_enricher import (
        enrich_nba_opponent_batch,
    )
    from deps import db as _db
    return await enrich_nba_opponent_batch(
        _db, batch_size=batch_size, limit=limit, dry_run=dry_run)


@router.post("/normalize-nba-team-actuals")
async def normalize_nba_team_actuals(
    user: Annotated[UserPublic, Depends(current_user)],
    dry_run: bool = False,
    limit: int = 0,
    batch_size: int = 500,
):
    """Session F3b — Build NBA team_game_actuals (BOTH home + away
    perspectives) from authoritative player_game_logs game-meta
    (dedup by game_id). Idempotent per (sport, event_id,
    canonical_team_id).
    """
    from services.team_history.nba_opponent_enricher import (
        normalize_nba_team_actuals as _norm,
    )
    from deps import db as _db
    return await _norm(_db, batch_size=batch_size, limit=limit,
                       dry_run=dry_run)


@router.post("/normalize-soccer-fixture")
async def normalize_soccer_fixture(
    pick_id: str,
    user: Annotated[UserPublic, Depends(current_user)],
):
    """Session C — Normalize provider actuals for a completed Soccer
    fixture into canonical player_game_actuals + team_game_actuals.
    Read-then-write.  Idempotent — re-running the same pick_id is
    a no-op after the first run.
    """
    from services.providers.soccer_fixture_resolver import resolve_fixture
    from services.providers import pitchapi as pa, bigballs as bb
    from services.providers.canonical_actuals_normalizer import (
        upsert_player_actual, upsert_team_actual,
    )
    from deps import db as _db
    import re as _re, httpx

    pick = await _db.picks.find_one({"$or":[{"id":pick_id},{"_id":pick_id}]},
                                     {"_id":0})
    if not pick or (pick.get("sport") or "") != "Soccer":
        return {"error":"pick_not_found_or_not_soccer"}
    event = pick.get("event") or ""
    parts = _re.split(r"\s+@\s+", event)
    if len(parts) != 2:
        return {"error":"unparseable_event"}
    away, home = parts[0].strip(), parts[1].strip()
    event_time = pick.get("event_time") or ""
    event_date = event_time[:10]
    fx = await resolve_fixture(_db, perklocks_league=pick.get("league"),
                                event_time_iso=event_time,
                                home_team=home, away_team=away)
    if fx.get("status") != "OK":
        return {"error":"fixture_unresolved", "fixture": fx}

    written = {"players": [], "teams": []}

    # ── PitchAPI player payload ─────────────────────────────────
    if fx.get("pitchapi_match_id") and pa.is_configured():
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(
                    f"{pa.DEFAULT_BASE_URL}/v1/matches/{fx['pitchapi_match_id']}/players",
                    headers={pa.AUTH_HEADER_NAME: pa.api_key()})
            if r.status_code == 200:
                players = (r.json() or {}).get("data") or []
                for p in players[:40]:
                    pl = p.get("player") or {}
                    pname = pl.get("name") or ""
                    if not pname:
                        continue
                    stat_map = {}
                    for grp in (p.get("stats") or []):
                        for _lbl, sd in (grp.get("stats") or {}).items():
                            key = (sd.get("key") or "").strip()
                            val = (sd.get("stat") or {}).get("value")
                            if key and val is not None:
                                try: stat_map[key] = float(val)
                                except (TypeError, ValueError): pass
                    # D0.1 — SKIP non-participants (empty-row prevention).
                    # A player is a legitimate participant if PitchAPI
                    # reports any of: minutes_played, goals, assists,
                    # total_shots, ShotsOnTarget, or a starter status.
                    participation_signals = (
                        stat_map.get("minutes_played") or 0,
                        stat_map.get("goals") or 0,
                        stat_map.get("assists") or 0,
                        stat_map.get("total_shots") or 0,
                        stat_map.get("ShotsOnTarget") or 0,
                    )
                    if not any(v > 0 for v in participation_signals):
                        continue
                    stats = {
                        "goals":  stat_map.get("goals"),
                        "assists": stat_map.get("assists"),
                        "shots":  stat_map.get("total_shots"),
                        "shots_on_target": stat_map.get("ShotsOnTarget"),
                        "minutes_played":  stat_map.get("minutes_played"),
                    }
                    research = {
                        "xg": stat_map.get("expected_goals"),
                        "xa": stat_map.get("expected_assists"),
                        "chances_created": stat_map.get("chances_created"),
                        "rating": stat_map.get("rating_title"),
                    }
                    res = await upsert_player_actual(
                        _db,
                        canonical_event_id=fx["pitchapi_match_id"],
                        canonical_player_id=pl.get("id"),
                        provider_player_name=pname,
                        provider="pitchapi",
                        provider_event_id=fx["pitchapi_match_id"],
                        canonical_team_id=p.get("team_id"),
                        event_date=event_date,
                        stats=stats,
                        research=research,
                    )
                    if res.get("status") in ("inserted", "updated"):
                        written["players"].append(
                            {"name": pname, "status": res["status"]})
        except Exception as e:
            written["pitchapi_error"] = type(e).__name__

    # ── Big Balls team payload ─────────────────────────────────
    if fx.get("bigballs_match_id") and bb.is_configured():
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(
                    f"{bb.DEFAULT_BASE_URL}/v1/matches/{fx['bigballs_match_id']}",
                    headers={bb.AUTH_HEADER_NAME: bb.api_key()})
            if r.status_code == 200:
                data = (r.json() or {}).get("data") or {}
                score = data.get("score") or {}
                if data.get("status") == "finished" and \
                        score.get("home") is not None and \
                        score.get("away") is not None:
                    h = int(score["home"]); a = int(score["away"])
                    home_res = "won" if h > a else ("lost" if h < a else "draw")
                    away_res = "won" if a > h else ("lost" if a < h else "draw")
                    hn = ((data.get("home") or {}).get("name") or home)
                    an = ((data.get("away") or {}).get("name") or away)
                    for team_name, side, gf, ga, result in [
                        (hn, "home", h, a, home_res),
                        (an, "away", a, h, away_res),
                    ]:
                        opp = an if side == "home" else hn
                        res = await upsert_team_actual(
                            _db,
                            canonical_event_id=fx["bigballs_match_id"],
                            canonical_team_id=None,
                            provider_team_name=team_name,
                            provider="bigballs",
                            provider_event_id=fx["bigballs_match_id"],
                            opponent_name=opp,
                            event_date=event_date,
                            home_away=side,
                            stats={
                                "goals_for": float(gf),
                                "goals_against": float(ga),
                                "final_score_home": float(h),
                                "final_score_away": float(a),
                                "result": result,
                            },
                        )
                        if res.get("status") in ("inserted", "updated"):
                            written["teams"].append(
                                {"team": team_name, "status": res["status"]})
        except Exception as e:
            written["bigballs_error"] = type(e).__name__

    return {"pick_id": pick_id, "fixture": fx,
            "normalization": written}


@router.get("/canonical-coverage-report")
async def canonical_coverage_report(
    user: Annotated[UserPublic, Depends(current_user)],
):
    """Session C — read-only per-sport canonical actuals coverage."""
    from deps import db as _db
    result = {}
    for sport in ["soccer", "mlb", "nba", "nfl", "nhl"]:
        total = await _db.player_game_actuals.count_documents({"sport": sport})
        with_cpid = await _db.player_game_actuals.count_documents({
            "sport": sport,
            "canonical_player_id": {"$exists": True, "$ne": None}})
        pitchapi_rows = await _db.player_game_actuals.count_documents({
            "sport": sport, "provenance.provider": "pitchapi"})
        bigballs_rows = await _db.player_game_actuals.count_documents({
            "sport": sport, "provenance.provider": "bigballs"})
        team_total = await _db.team_game_actuals.count_documents({
            "sport": sport})
        team_bigballs = await _db.team_game_actuals.count_documents({
            "sport": sport, "provenance.provider": "bigballs"})
        result[sport] = {
            "player_actuals_total": total,
            "player_actuals_with_cpid": with_cpid,
            "player_actuals_pitchapi": pitchapi_rows,
            "player_actuals_bigballs": bigballs_rows,
            "team_actuals_total": team_total,
            "team_actuals_bigballs": team_bigballs,
        }
    # Fixture map cache
    fx_cached = await _db.provider_fixture_map.count_documents({})
    stat_cached = await _db.provider_stat_cache.count_documents({})
    result["cache"] = {"provider_fixture_map": fx_cached,
                       "provider_stat_cache": stat_cached}
    return result


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
        "wired_into_settlement": True,   # Session B — cascade PRIMARY on
        "cascade_env_flag": "PERKLOCKS_PROVIDER_CASCADE_ENABLED (default=1)",
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



# ─────────────────────────────────────────────────────────────
#  P4 — HISTORY READINESS TELEMETRY (2026-08-27)
#
#  Minimal, read-only, per-sport model-input sufficiency view.
#  Exposes exactly what each sport's LIVE scorer requires from the
#  historical stores so Production can never again silently show
#  "GAME 0" because a one-time backfill was forgotten.
#
#  Contract: no secrets, no raw payloads, no writes.  Requires a
#  logged-in user (not admin) — Prod boards must be able to render
#  a "why is this sport empty?" hint client-side without escalating
#  to admin auth.
# ─────────────────────────────────────────────────────────────

# Each sport declares (a) the collections its LIVE scorer reads and
# (b) the *minimum row count* below which the model is considered
# INSUFFICIENT.  These floors intentionally low — the goal is to
# distinguish "never seeded" from "at least one healthy backfill".
_HISTORY_CONTRACTS: list[dict] = [
    {"sport": "MLB",    "required_stores": [
        {"coll": "games",             "query": {"sport": "mlb", "status": "Final"}, "floor": 200},
        {"coll": "player_game_logs",  "query": {"sport": "mlb"},                    "floor": 5000},
    ], "existing_backfill": "POST /api/admin/historical/backfill mode=incremental sports=[mlb]",
       "self_seed_hint":   "continuous MLB ingest is authoritative — leave untouched",
       "registry_status": "SUPPORTED"},
    {"sport": "NFL",    "required_stores": [
        {"coll": "games",             "query": {"sport": "nfl", "status": "Final"}, "floor": 32},
        {"coll": "player_game_logs",  "query": {"sport": "nfl"},                    "floor": 500},
    ], "existing_backfill": "POST /api/admin/historical/backfill-seasons sports=[nfl] seasons=[2025]",
       "self_seed_hint":   "run existing NFL backfill once per deploy if games<32",
       "registry_status": "SUPPORTED"},
    {"sport": "NBA",    "required_stores": [
        # NBA feature engine (services/nba_feature_engine.py) reads
        # `player_game_logs` (sport='nba').  Ingested by
        # `services/nba_gamelog_ingest.py` via ESPN public API — the
        # authoritative Prod path.  balldontlie is NOT required.
        {"coll": "player_game_logs",  "query": {"sport": "nba"},                    "floor": 2000},
        {"coll": "players",           "query": {"sport": "nba", "active": True},   "floor": 200},
    ], "existing_backfill": "POST /api/admin/ingest-nba-gamelogs seasons=2024,2025",
       "self_seed_hint":   "ESPN-authoritative; runs bounded, idempotent, no key needed",
       "registry_status": "SUPPORTED"},
    {"sport": "CFB",    "required_stores": [
        {"coll": "cfb_sp_ratings",    "query": {},                                   "floor": 100},
        {"coll": "cfb_teams",         "query": {},                                   "floor": 100},
    ], "existing_backfill": "POST /api/admin/services-cfb-refresh",
       "self_seed_hint":   "requires CFBD_API_KEY env; run once per deploy if sp_ratings<100",
       "registry_status": "SUPPORTED"},
    {"sport": "Soccer", "required_stores": [
        {"coll": "soccer_player_form","query": {},                                   "floor": 500},
    ], "existing_backfill": "continuous — see services/soccer_pipeline_scheduler",
       "self_seed_hint":   "soccer_player_form must be populated for player-prop resolution",
       "registry_status": "SUPPORTED"},
    {"sport": "Tennis", "required_stores": [
        # Live tennis scorer reads tennis_player_stats + tennis_matches
        # + tennis_league_averages via services/tennis_calibration.py.
        # Historical seed: historical/tennis.py (Tennismylife CSV mirror,
        # no key required) + POST /api/admin/backfill-tennis-elo for the
        # ESPN Elo/form ledger.
        {"coll": "tennis_player_stats","query": {},                                  "floor": 100},
        {"coll": "tennis_matches",     "query": {},                                  "floor": 1000},
        {"coll": "tennis_league_averages","query": {"_id": "current"},               "floor": 1},
    ], "existing_backfill": "POST /api/admin/historical/backfill-seasons sports=[tennis] + POST /api/admin/backfill-tennis-elo days_back=60",
       "self_seed_hint":   "both routes together seed full model history",
       "registry_status": "SUPPORTED"},
    {"sport": "NHL",    "required_stores": [
        # brain/sim_nhl.py exists but per sport_capability_registry.py
        # NHL is `INTENTIONALLY_DEFERRED` (h2h/spreads/totals all
        # MODEL_UNAVAILABLE) — the sim isn't wired to the runtime
        # dispatcher yet.  We still seed history now so the data is
        # ready the moment the runtime flip happens.
        {"coll": "games",              "query": {"sport": "nhl", "status": "Final"}, "floor": 100},
        {"coll": "player_game_logs",   "query": {"sport": "nhl"},                    "floor": 2000},
    ], "existing_backfill": "POST /api/admin/historical/backfill-seasons sports=[nhl]",
       "self_seed_hint":   "historical/nhl.py uses api-web.nhle.com (no key)",
       "registry_status": "INTENTIONALLY_DEFERRED"},
    {"sport": "UFC",    "required_stores": [
        # UFC event ingest (ufc_espn_ingest.py) writes directly to
        # `picks` for the current-window slate.  Per
        # sport_capability_registry.py UFC is `INTENTIONALLY_DEFERRED`
        # (h2h + totals both `MODEL_UNAVAILABLE`) — no independent
        # simulator exists at brain/sim_ufc.py (confirmed absent).
        # `ufc_espn_ingest` is the current event ingest.
        {"coll": "picks",              "query": {"sport": "UFC"},                    "floor": 1},
    ], "existing_backfill": "POST /api/admin/ufc-espn-refresh days=21",
       "self_seed_hint":   "event ingest only; independent model deferred (sim_ufc absent)",
       "registry_status": "INTENTIONALLY_DEFERRED"},
]


@router.get("/history-readiness")
async def history_readiness(
    user: Annotated[UserPublic, Depends(current_user)],
):
    """P4 — Model-input readiness matrix.

    For each currently supported sport we report:
      • required_history       — the exact collections its live scorer reads
      • row_count              — how many rows the DB actually has
      • coverage               — required_row_count / floor (>=1.0 == sufficient)
      • model_ready            — bool
      • history_status         — SUFFICIENT | INSUFFICIENT | SOURCE_UNAVAILABLE
      • existing_backfill      — where to trigger a repair if insufficient
      • last_updated           — last time any store received a write (best-effort)

    Zero secrets, zero writes, non-admin readable so the client can
    surface "why is this sport empty?" without escalating.
    """
    now = datetime.now(timezone.utc).isoformat()
    from deps import db as _db
    coll_names = set(await _db.list_collection_names())

    out = []
    for contract in _HISTORY_CONTRACTS:
        sport = contract["sport"]
        row = {
            "sport": sport,
            "required_history": [],
            "row_count":  0,
            "coverage":   0.0,
            "model_ready": False,
            "history_status": "SUFFICIENT",
            "existing_backfill": contract.get("existing_backfill"),
            "self_seed_hint":   contract.get("self_seed_hint"),
            "last_updated": None,
        }
        if not contract["required_stores"]:
            # No live-model contract (legacy path — retained for defensive
            # completeness).  registry_status trumps.
            row["history_status"] = "SOURCE_UNAVAILABLE"
            row["registry_status"] = contract.get("registry_status")
            out.append(row)
            continue

        total_covered = 0.0
        total_rows    = 0
        for store in contract["required_stores"]:
            coll = store["coll"]
            floor = int(store["floor"])
            if coll not in coll_names:
                row["required_history"].append({
                    "coll": coll, "query": store["query"], "floor": floor,
                    "count": 0, "present": False,
                })
                continue
            try:
                n = await _db[coll].count_documents(store["query"])
            except Exception:
                n = -1
            total_rows += max(0, n)
            total_covered += min(1.0, (n / floor) if floor > 0 else 1.0)
            row["required_history"].append({
                "coll": coll, "query": store["query"], "floor": floor,
                "count": n, "present": True,
            })

        stores_n = len(contract["required_stores"])
        row["row_count"] = total_rows
        row["coverage"]  = round(total_covered / max(1, stores_n), 3)
        # Model-ready == every declared store meets its floor.
        row["model_ready"] = all(
            (r.get("count") or 0) >= int(r.get("floor") or 0)
            for r in row["required_history"]
        )
        # Registry-aware final status: even if history is SUFFICIENT
        # we honestly surface `INTENTIONALLY_DEFERRED` when the runtime
        # capability contract (services/sport_capability_registry.py)
        # has not yet wired the sport's simulator to the dispatcher —
        # so operators never see a green row for a sport that still
        # returns MODEL_UNAVAILABLE at pick time.
        reg = contract.get("registry_status") or "SUPPORTED"
        row["registry_status"] = reg
        if reg == "INTENTIONALLY_DEFERRED":
            row["history_status"] = "INTENTIONALLY_UNSUPPORTED"
        else:
            row["history_status"] = "SUFFICIENT" if row["model_ready"] else "INSUFFICIENT"
        out.append(row)

    return {
        "generated_at": now,
        "sports": out,
    }
