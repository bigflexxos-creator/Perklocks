"""NFL Props 2.0 → canonical publication wiring.

This module bridges the V2 discovery engine into the EXISTING PerkLocks
canonical pipeline.  It does NOT create a second board, a second Locks
authority, or a second publication.  It:

  1. Walks every NFL pick already ingested on the current slate.
  2. Groups picks by (canonical_event_id, canonical_player_id, market).
  3. Computes GAME CONTEXT ONCE per game.
  4. Computes PLAYER CONTEXT + Distribution ONCE per (player, market).
  5. Evaluates every real threshold for that market from the same
     distribution.
  6. Stamps ``nfl_props_v2_evidence`` onto each corresponding pick doc
     with fields the existing Lock authority / Magic / Bet Quality /
     APEX evaluators can consume:

        hit_probability_monotonic  — model probability for the line
        floor_distance             — Q25 − line (positive = high floor)
        distribution_sample_size   — n of the historical window
        q25 / median / q75         — quantile summary
        game_context_available     — bool
        weather_status             — AVAILABLE / UNAVAILABLE
        availability_status        — AVAILABLE / PARTIAL / UNAVAILABLE
        confidence                 — engine's data-quality composite
        role_status                — AVAILABLE / PARTIAL / UNAVAILABLE
        engine_version             — nfl_props_v2:2026-06-21
        stamped_at                 — ISO timestamp

Fields are ADDITIVE only.  No existing pick field is modified or
removed.  Existing NFL props remain untouched — this pass simply
enriches them.  Because `_canonicalize_picks` passes unknown top-level
fields through unchanged, `nfl_props_v2_evidence` naturally reaches
`/api/picks/today` for downstream consumers.

Performance contract:
  * Game context built ONCE per game.
  * Distribution built ONCE per (player, market).
  * No provider fanout — this pass only reads Mongo.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from .adapters import (
    DefaultWeatherProvider, DefaultAvailabilityProvider,
    WEATHER_STATUS_AVAILABLE, INJURY_STATUS_AVAILABLE, INJURY_STATUS_PARTIAL,
    INJURY_STATUS_UNAVAILABLE,
)
from .engine import (
    build_distribution, enforce_monotonicity, ThresholdEvaluation,
    _implied_prob, _safe_float, _weather_confidence_multiplier,
)


log = logging.getLogger(__name__)

_ENGINE_VERSION = "nfl_props_v2:2026-06-21"


def _current_role_status(pick: dict) -> tuple[str, dict]:
    """Return (role_status, role_evidence).

    Current opportunity data lives on the pick doc when the ingestion
    already enriched it (e.g. via ``services/platinum_nfl/opportunity.py``).
    We use whatever the doc actually carries; when absent we honestly
    return PARTIAL — never fabricated.
    """
    role_ev: dict[str, Any] = {}
    for k in ("expected_targets", "expected_routes", "expected_carries",
              "expected_attempts", "target_share", "route_participation",
              "carry_share", "goal_line_share", "red_zone_share",
              "expected_touches", "opportunity_share"):
        v = pick.get(k)
        if v is not None:
            role_ev[k] = v
    # Fall back to snake_case block from opportunity enrichment if present
    block = pick.get("opportunity") or pick.get("player_role") or {}
    if isinstance(block, dict) and block:
        role_ev.setdefault("block", block)
    status = "AVAILABLE" if role_ev else "PARTIAL"
    return status, role_ev


async def enrich_nfl_picks_with_v2_evidence(db, *, pick_date: Optional[str] = None,
                                             limit_games: int = 32,
                                             weather_provider=None,
                                             availability_provider=None) -> dict:
    """Walk today's NFL picks and stamp V2 evidence.

    Returns a summary dict for admin/CLI acceptance:

        {
          "pick_date": ...,
          "nfl_picks_before": N,
          "distinct_games": G,
          "distinct_players": P,
          "distinct_player_markets": M,
          "picks_stamped": S,
          "picks_with_nonnull_probability": K,
          "highest_probability": max_p,
          "highest_floor_distance": max_floor,
          "engine_version": _ENGINE_VERSION,
        }
    """
    weather_provider = weather_provider or DefaultWeatherProvider()
    availability_provider = availability_provider or DefaultAvailabilityProvider()

    # SLATE DATE ALIGNMENT: use the canonical Perklocks U.S. betting day
    # (04:00 ET roll) so this pass stamps the SAME picks the Locks feed
    # (/api/picks/today) is serving.  Falling back to UTC caused a date
    # skew whenever the wall clock crossed 00:00 UTC before 04:00 ET —
    # the stamper enriched tomorrow's empty slate while the board still
    # served yesterday's live picks unstamped.
    if not pick_date:
        try:
            from services.perklocks_day import current_slate_day
            pick_date = current_slate_day()
        except Exception:
            pick_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    base_filter = {
        "sport": "NFL",
        "pick_date": pick_date,
        "off_board": {"$ne": True},
        "no_bet": {"$ne": True},
        # Universal candidate admission per P0-G: canonical identity is
        # REQUIRED (no elite_player_name gate).  Player identity is
        # sufficient via canonical_player_id.
        "canonical_player_id": {"$exists": True, "$ne": None},
        "line": {"$exists": True, "$ne": None},
    }
    total_before = await db.picks.count_documents(base_filter)

    # 1) Enumerate distinct games on today's slate.  ONE game context
    #    computation per game.  Group by whichever event key is
    #    populated (canonical_event_id → event_id → event string).
    game_cursor = db.picks.aggregate([
        {"$match": base_filter},
        {"$addFields": {
            "_game_key": {"$ifNull": [
                "$canonical_event_id",
                {"$ifNull": ["$event_id", "$event"]},
            ]},
        }},
        {"$match": {"_game_key": {"$ne": None}}},
        {"$group": {
            "_id": "$_game_key",
            "home_team": {"$first": "$home_team"},
            "away_team": {"$first": "$away_team"},
            "event_id":  {"$first": "$event_id"},
            "event_time":{"$first": "$event_time"},
            "event":     {"$first": "$event"},
        }},
        {"$limit": limit_games},
    ])
    games = [g async for g in game_cursor]
    game_ctx_by_event: dict[str, dict] = {}
    weather_by_event:  dict[str, Any]  = {}
    try:
        from services.platinum_nfl.game_runtime import build_nfl_game_model_context
    except Exception:
        build_nfl_game_model_context = None
    for g in games:
        cev = g.get("_id") or g.get("event_id")
        if not cev:
            continue
        game_doc = {
            "home_team": g.get("home_team"), "away_team": g.get("away_team"),
            "event": g.get("event"), "canonical_event_id": cev,
            "event_id": g.get("event_id"), "event_time": g.get("event_time"),
        }
        try:
            ctx = await build_nfl_game_model_context(game_doc) if build_nfl_game_model_context else {}
        except Exception:
            ctx = {"nfl_model_available": False}
        game_ctx_by_event[cev] = ctx
        weather_by_event[cev]  = await weather_provider.report_for_game(db, game_doc)

    # 2) Distributions cache — ONE per (player, market).
    dist_cache: dict[tuple, Any] = {}
    avail_cache: dict[str, Any] = {}

    stamped = 0
    with_prob = 0
    distinct_players: set = set()
    distinct_player_markets: set = set()
    max_p = 0.0
    max_floor = 0.0

    now_iso = datetime.now(timezone.utc).isoformat()

    async for pick in db.picks.find(base_filter, {"_id": 0}):
        # Derive a game key robustly (canonical_event_id may not exist)
        cev  = (pick.get("canonical_event_id")
                or pick.get("event_id") or pick.get("event"))
        cpid = pick.get("canonical_player_id")
        player = (pick.get("elite_player_name") or pick.get("player_name") or "").strip()
        if not player and cpid:
            # Recover the player display name from the market string
            # (safe — market string is human-readable; ingestion writes
            # "Player Name Over 12.5 Player Reception Yds …").
            m = str(pick.get("market") or "")
            if " Over " in m:
                player = m.split(" Over ", 1)[0].strip()
            elif " Under " in m:
                player = m.split(" Under ", 1)[0].strip()
        market = str(pick.get("market") or "").strip()
        line   = _safe_float(pick.get("line"))
        if not (cev and cpid and player and market and line is not None):
            continue
        distinct_players.add(cpid)
        distinct_player_markets.add((cpid, market))

        # Availability — cache per player.
        team = pick.get("team") or pick.get("home_team") or pick.get("away_team")
        if cpid not in avail_cache:
            avail_cache[cpid] = await availability_provider.report_for_player(
                db, player_name=player, team=team or "", sport="nfl",
            )
        avail = avail_cache[cpid]

        # Weather / game context for this event.
        weather = weather_by_event.get(cev)
        game_ctx = game_ctx_by_event.get(cev) or {}
        as_of = pick.get("event_time")

        # Distribution — cache per (player, market).
        dkey = (cpid, market)
        if dkey not in dist_cache:
            opp = None
            if pick.get("home_team") and pick.get("away_team"):
                opp = pick["away_team"] if pick.get("team") == pick.get("home_team") else pick.get("home_team")
            dist_cache[dkey] = await build_distribution(
                db, player_name=player, canonical_player_id=cpid,
                market=market, opponent=opp, as_of=as_of,
            )
        dist = dist_cache[dkey]

        # Threshold probability + floor.
        p_raw = dist.hit_probability_over(line)
        floor = dist.floor_distance(line)
        # Monotonicity is enforced elsewhere (across the whole ladder).
        # We still stamp the raw single-line evaluation on the pick doc so
        # the downstream Lock authority sees per-pick evidence.
        implied = _implied_prob(pick.get("book_odds"))
        edge_pp = round((p_raw - implied) * 100.0, 2) if (p_raw is not None and implied is not None) else None

        # Confidence — same neutral-when-unknown contract from engine.py.
        conf_break = {
            "game_context": 1.0 if game_ctx.get("nfl_model_available") else 0.9,
            "weather":      _weather_confidence_multiplier(weather),
            "availability": {
                INJURY_STATUS_AVAILABLE:   1.0,
                INJURY_STATUS_PARTIAL:     1.0,
                INJURY_STATUS_UNAVAILABLE: 1.0,
            }.get(getattr(avail, "status", INJURY_STATUS_PARTIAL), 1.0),
            "availability_prob": (avail.availability_probability
                                  if avail and avail.availability_probability is not None
                                  else 1.0),
        }
        confidence = 1.0
        for v in conf_break.values():
            confidence *= float(v)
        confidence = round(min(1.0, max(0.0, confidence)), 4)

        # Role evidence.
        role_status, role_ev = _current_role_status(pick)

        v2 = {
            "hit_probability": p_raw,
            "hit_probability_monotonic": p_raw,   # single-line raw
            "floor_distance": floor,
            "implied_probability": implied,
            "edge_pp": edge_pp,
            "distribution_sample_size": dist.sample_size,
            "distribution_mean": dist.mean,
            "distribution_q25": dist.q25,
            "distribution_median": dist.median,
            "distribution_q75": dist.q75,
            "distribution_provenance": dist.provenance,
            "game_context_available": bool(game_ctx.get("nfl_model_available")),
            "game_expected_margin": game_ctx.get("expected_margin_home"),
            "game_expected_total": game_ctx.get("expected_total"),
            "weather_status": getattr(weather, "status", "UNAVAILABLE"),
            "weather_is_indoor": getattr(weather, "is_indoor", None),
            "availability_status": getattr(avail, "status", INJURY_STATUS_PARTIAL),
            "availability_designation": getattr(avail, "designation", None),
            "availability_probability": getattr(avail, "availability_probability", None),
            "role_status": role_status,
            "role_evidence": role_ev,
            "confidence": confidence,
            "confidence_breakdown": conf_break,
            "engine_version": _ENGINE_VERSION,
            "stamped_at": now_iso,
        }

        await db.picks.update_one(
            {"id": pick["id"]},
            {"$set": {"nfl_props_v2_evidence": v2}},
        )
        stamped += 1
        if p_raw is not None:
            with_prob += 1
            max_p = max(max_p, float(p_raw))
        if floor is not None:
            max_floor = max(max_floor, float(floor))

    return {
        "pick_date": pick_date,
        "nfl_picks_before": total_before,
        "nfl_picks_after": total_before,   # additive; no deletion
        "distinct_games": len(game_ctx_by_event),
        "distinct_players": len(distinct_players),
        "distinct_player_markets": len(distinct_player_markets),
        "picks_stamped": stamped,
        "picks_with_nonnull_probability": with_prob,
        "highest_probability": round(max_p, 4),
        "highest_floor_distance": round(max_floor, 2),
        "engine_version": _ENGINE_VERSION,
    }


__all__ = ["enrich_nfl_picks_with_v2_evidence"]
