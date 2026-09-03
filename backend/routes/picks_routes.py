"""Picks routes — incremental extraction from server.py.

Phase 1 (2026-06-25) extracted the lowest-coupling endpoints:
  • GET  /api/picks/all
  • GET  /api/picks/nrfi-yrfi
  • GET  /api/picks/markets/{sport}
  • GET  /api/picks/refresh-status

Phase 2 (2026-06-27) — THIS DROP — moves the medium-coupling routes
that share helpers (`_ensure_today_picks`, `_canonicalize_picks`,
`_filter_in_play_window`, `_market_regex`) and the picks-detail
enrichment endpoints (`/{pick_id}/*`):

  • GET  /api/picks/under-of-the-day
  • GET  /api/picks/rollover
  • POST /api/picks/settle
  • GET  /api/picks/history
  • GET  /api/picks/{pick_id}                — detail
  • POST /api/picks/{pick_id}/ai-explain
  • POST /api/picks/{pick_id}/loss-analysis
  • GET  /api/picks/{pick_id}/probability
  • GET  /api/picks/{pick_id}/player-form
  • GET  /api/picks/{pick_id}/pitcher-h2h
  • GET  /api/picks/{pick_id}/simulation

Phase 3 (next) will extract the two largest endpoints:
  • GET  /api/picks/today    (~600 lines)
  • GET  /api/picks/parlay   (~330 lines)
  • POST /api/picks/refresh

DESIGN RULES (same as Phase 1, preserved here for safety):
  1. Lazy import every helper from `server.py` INSIDE the handler. This
     keeps module-load order acyclic: server.py imports this module via
     `app.include_router(router)` near the bottom of its own file, so
     this module cannot top-level-import server.py without crashing
     uvicorn boot.
  2. Static segments (`/under-of-the-day`, `/rollover`, `/settle`,
     `/history`, plus the Phase-1 set) MUST be registered BEFORE the
     parameterized `/{pick_id}` route, otherwise FastAPI matches
     `pick_id="under-of-the-day"` etc. and 404s.
  3. Every endpoint must round-trip identically to its old server.py
     version — payload shapes, query-param semantics, error codes.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone, timedelta
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException

from auth import UserPublic
from deps import current_user, db, logger
from rate_limit import rate_limit

router = APIRouter(prefix="/picks", tags=["picks"])


def _parse_event_dt(et: str) -> Optional[datetime]:
    """Robust ISO-8601 parser for `pick.event_time`.

    Handles ALL formats we've seen in the wild:
      • `2026-07-12T00:41:00Z`             (Odds API, MLB, Soccer)
      • `2026-07-14T08:00:00+00:00`        (tennis_extra, tennis_real_odds)
      • `2026-07-13T14:00:00.000Z`         (SportDB, occasional)
      • `2026-07-13T14:00:00+02:00`        (leagues in local time)

    Returns a timezone-aware UTC datetime or None on failure.

    Before this helper existed (2026-07-13 bug report: "Sort feature not
    working when you put time soon to late it's show early games but
    not the earliest") the codebase used a strict `strptime` with format
    `%Y-%m-%dT%H:%M:%SZ` in FOUR places. All non-`Z`-suffixed picks
    (every tennis pick — 80/307 of the daily slate) failed to parse and
    got bucketed at `datetime.max`, so they piled at the sort's bottom
    instead of being interleaved chronologically with soccer/MLB picks.
    """
    if not et or not isinstance(et, str):
        return None
    try:
        # Normalise 'Z' → '+00:00' so fromisoformat handles it uniformly.
        s = et.strip().replace("Z", "+00:00") if et.endswith("Z") else et
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


# Per-user 30/min throttle for the AI-explain endpoint (SEC-002,
# 2026-06-26). Mirrors the `_compute_throttle` instance in server.py —
# building our own here avoids importing server.py at module-load and
# keeps both routers safely decoupled. Rate limiter state is stored on
# the underlying dependency object, so the two instances are
# independent which is the desired behaviour: the public detail flow
# (this module) and any private internal flows (still in server.py) get
# separate budgets.
_compute_throttle = rate_limit(rate_per_min=30, burst=10, scope="user")


# ───────────────────────── Phase 1 (kept) ─────────────────────────

@router.get("/all")
async def picks_all(
    user: Annotated[UserPublic, Depends(current_user)],
    sport: Optional[str] = None,
):
    """P0.2d — canonical Locks board projection.

    Consumes ``BoardProjectionService`` which applies the same
    canonical eligibility contract (``is_main_board_eligible``),
    canonical dedupe, deterministic sort, and lifecycle window as
    ``/picks/today``.  Previously this endpoint returned an unfiltered
    ``db.picks.find(pick_date=today)`` result which meant sport tabs
    and the `all` view could disagree with the main board on
    membership — that divergence is now closed.
    """
    from server import (
        _ensure_today_picks, _today_str, _filter_in_play_window,
        _canonicalize_picks,
    )
    from services.board_projection_service import BoardProjectionService
    await _ensure_today_picks()
    raw = await db.picks.find(
        {"pick_date": _today_str()}, {"_id": 0},
    ).to_list(length=1000)
    svc = BoardProjectionService()
    projected = svc.project(
        raw, sport=sport,
        lifecycle_filter=_filter_in_play_window,
    )
    return {"picks": _canonicalize_picks(projected[:200])}


@router.get("/nrfi-yrfi")
async def picks_nrfi_yrfi(user: Annotated[UserPublic, Depends(current_user)]):
    """Dedicated MLB NRFI/YRFI feed — these picks are intentionally
    excluded from the main /picks/today board (`hide_from_main_board`
    flag). Returns today's slate with full model audit-trail so the UI
    can show λ₁, pitcher/lineup/park factors per pick."""
    from server import _today_str, _filter_in_play_window, _canonicalize_lock_score  # lazy
    q = {
        "pick_date": _today_str(),
        "category": "nrfi_yrfi",
    }
    cursor = db.picks.find(q, {"_id": 0}).sort("lock_score", -1).limit(50)
    rows = await cursor.to_list(length=50)
    rows = _filter_in_play_window(rows)
    rows = [_canonicalize_lock_score(r) for r in rows]
    return {"picks": rows, "count": len(rows), "category": "nrfi_yrfi"}


@router.get("/markets/{sport}")
async def markets_for_sport(
    user: Annotated[UserPublic, Depends(current_user)],
    sport: str,
):
    """Return the dynamic market list + active leagues for a given sport.
    Used by the Locks tab to populate the MarketSelector + League pills.

    Critically, the league `count` MUST be computed from the SAME pick
    universe that `/picks/today` serves — i.e. after `_filter_in_play_window`
    drops games that have already started.

    ── 2026-06 μ-closure — resilient markets contract ──────────────
    The configured market catalog MUST always be returned so the
    frontend tabs render even if the auxiliary DB league-count query
    fails.  On any error we return the configured catalog with an
    empty `leagues` array — never an empty `markets` array.
    """
    from server import SPORT_MARKETS, _today_str, _filter_in_play_window  # lazy
    from services.main_board_eligibility import is_main_board_eligible  # lazy
    markets = SPORT_MARKETS.get(sport, [])
    try:
        raw = await db.picks.find(
            {"sport": sport, "pick_date": _today_str()},
            {"_id": 0,
             "league": 1, "event_time": 1,
             "lock_score": 1, "lock_score_v2": 1, "published_lock_score": 1,
             "is_under_lock": 1, "no_bet": 1, "off_board": 1,
             "edge_percent": 1, "elite_player": 1,
             # Fields required by is_main_board_eligible() —
             # missing projection previously caused silent False.
             "book_odds": 1, "implied_probability": 1},
        ).to_list(length=1000)

        # Phase 1 (2026-08-11): league counts must reflect the SAME
        # eligibility rule as the main Locks board — strict `>85` on the
        # authoritative published Lock Score, no elite bypass, no
        # `edge >= 0` gate (real-line integrity: edge=None ≠ 0).  Uses the
        # central `is_main_board_eligible` helper so this endpoint can
        # never drift out of sync with `/picks/today`.
        def _qualifies(p: dict) -> bool:
            if p.get("no_bet") is True:
                return False
            if p.get("off_board") is True:
                return False
            return is_main_board_eligible(p)

        raw = [p for p in raw if _qualifies(p)]
        raw = _filter_in_play_window(raw)
        counts: dict[str, int] = {}
        for p in raw:
            lg = p.get("league")
            if not lg:
                continue
            counts[lg] = counts.get(lg, 0) + 1
        leagues = [{"name": name, "count": c}
                   for name, c in sorted(counts.items(), key=lambda kv: -kv[1])]
    except Exception as _e:
        import logging as _log
        _log.getLogger("lockscore.picks_routes").warning(
            "markets_for_sport league count failed for %s: %s — "
            "returning configured markets with empty leagues", sport, _e,
        )
        leagues = []
    return {"sport": sport, "markets": markets, "leagues": leagues}


@router.get("/refresh-status")
async def refresh_status_pre(user: Annotated[UserPublic, Depends(current_user)]):
    """Return the user's current refresh cooldown WITHOUT triggering a
    refresh (zero Odds API cost). Declared BEFORE /picks/{pick_id} so
    FastAPI's route matching doesn't capture the literal segment as an
    ID. Uses the module-local `_cooldown_payload` defined in the
    Phase-3 block at the bottom of this file."""
    now = datetime.now(timezone.utc)
    user_doc = await db.users.find_one(
        {"id": user.id}, {"_id": 0, "last_refresh_at": 1},
    )
    last_iso = (user_doc or {}).get("last_refresh_at")
    return _cooldown_payload(last_iso, now)


# ───────────────────────── Phase 2 (new) ─────────────────────────
# Static segments first — every endpoint with a fixed path must be
# registered BEFORE `/{pick_id}` so FastAPI doesn't capture the
# literal segment as an ID.

@router.get("/under-of-the-day")
async def under_of_the_day(
    user: Annotated[UserPublic, Depends(current_user)],
    line_type: Optional[str] = None,
    sort: Optional[str] = "time",
    sport: Optional[str] = None,
    market: Optional[str] = None,
    league: Optional[str] = None,
):
    """The single safest Under lock across all sports.

    `line_type`:
      - "main": main-line totals only
      - "alt":  alt-prop Unders only
      - "both" / None: unrestricted (default)
    `sort`: "lock" (default), "time", or "edge"
    `sport` / `market` / `league`: same semantics as /picks/today.
    """
    from server import (  # lazy
        _ensure_today_picks, _today_str, _filter_in_play_window,
        _canonicalize_lock_score, _canonicalize_picks, _market_regex,
    )
    await _ensure_today_picks()
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=24)
    q: dict = {"pick_date": _today_str(), "is_under_lock": True,
               "no_bet": {"$ne": True}}
    lt = (line_type or "").lower()
    if lt == "main":
        q["is_alt"] = {"$ne": True}
    elif lt == "alt":
        q["is_alt"] = True
    if sport and sport.lower() != "all":
        q["sport"] = sport
    if market:
        regex = _market_regex(market)
        if regex:
            q["market"] = {"$regex": regex, "$options": "i"}
    if league:
        # SEC-004 — re.escape user input before stuffing into $regex.
        q["league"] = {"$regex": re.escape(str(league)), "$options": "i"}
    s = (sort or "lock").lower()
    if s == "time":
        cursor = db.picks.find(q, {"_id": 0}).sort("event_time", 1).limit(50)
    elif s == "edge":
        cursor = db.picks.find(q, {"_id": 0}).sort("edge_percent", -1).limit(50)
    else:
        cursor = db.picks.find(q, {"_id": 0}).sort("lock_score", -1).limit(50)
    picks = await cursor.to_list(length=50)
    # Drop picks for games that have already started.
    picks = _filter_in_play_window(picks)

    def starts_today(p: dict) -> bool:
        dt = _parse_event_dt(p.get("event_time") or "")
        if dt is None:
            return False
        return now <= dt <= cutoff

    today_picks = [p for p in picks if starts_today(p)]
    pool = today_picks if today_picks else picks
    if not pool:
        return {"pick": None, "alternates": [], "total_evaluated": 0}

    # Rank by win probability (the higher, the safer the Under)
    pool.sort(
        key=lambda p: (p.get("win_probability", 0), p.get("lock_score", 0)),
        reverse=True,
    )
    return {
        "pick": _canonicalize_lock_score(pool[0]),
        "alternates": _canonicalize_picks(pool[1:6]),  # 5 backup alt-Under locks
        "total_evaluated": len(pool),
        "scoped_to_today": bool(today_picks),
    }


@router.get("/rollover")
async def pick_rollover(
    user: Annotated[UserPublic, Depends(current_user)],
    line_type: Optional[str] = None,
    sport: Optional[str] = None,
    market: Optional[str] = None,
    league: Optional[str] = None,
    mode: str = "v2",
):
    """Data-Driven Rollover (V4) — the top 3 highest-conviction picks.

    2026-07-01 spec, refined after full-history audit of 1,441 settled
    picks. V4 replaces V3's blanket "no pitchers" rule with a learned
    market whitelist/blacklist.

    Data used (all figures from user's own graded picks):
      • Tennis 69.9% · NBA 69.6% · Soccer 65.6% · MLB 59.7%
      • Lock 99=78%, 95-98=67%, 89-94=65%, 85-88=59%, **80-84=48% (drop)**
      • Odds ≤-300 = 71%, -300 to -200 = 68%, -140 to -110 = **48% (drop)**
      • Edge 0-3%=67%, 3-5%=66%, 5-8%=66%, 8-12%=58%, **>12% = 51% (drop)**
      • Alt-lines 67.5% > Main 60.8% → alts get ranking boost
      • Market whitelist winners:
          - Soccer Win-or-Draw       80.0%  → 1.15× boost
          - MLB Strikeouts (pitcher) 72.4%  → 1.10× boost (un-banned)
          - Tennis Moneyline         66.7%  → 1.05× boost
          - Soccer Total Goals       65.7%  → 1.05× boost
          - MLB Run Line / Spread    63.5%  → 1.02× boost
          - MLB Hits                 59.6%  → 1.00×
      • Market BLACKLIST (permanent):
          - MLB H+R+RBI              35.6%
          - MLB NRFI/YRFI            41.5%
          - Soccer FGS / AGS / To-Score-or-Assist / Hat-tricks

    Rules:
      • Top 3 legs — one per game (no sport quota; genuinely 3 best)
      • Lock score ≥ 89 (89-99 all hit 65-78%)
      • **Exclude lock band 80-84 explicitly** (47.6% inverted calibration)
      • Odds range: -350 ≤ odds AND NOT in (-140 to -110) dead zone
      • Edge 0% to 12% (>12% is inverted signal at 51%)
      • Alt-lines ranked +5% higher (67.5% vs 60.8% historically)
      • Market whitelist gets 1.02-1.15× ranking boost
      • Composite: (0.55·wp + 0.20·sim + 0.15·edge_norm + 0.10·alt_bonus)
        × market_multiplier × chalk_penalty × hot/cold

    Filters honoured: `sport`, `market`, `league`, `line_type`."""
    from server import (  # lazy
        _ensure_today_picks, _today_str, _filter_in_play_window,
        _canonicalize_lock_score, _canonicalize_picks, _market_regex,
    )
    await _ensure_today_picks()

    # ── 2026-07-27 STICKY ROLLOVER (bug: bets shuffled every visit) ────
    # Rollover is meant to be the "3 safest bets of the day" — it must
    # be STABLE across tab visits. Previously every call recomputed the
    # ranking, so live changes to `signal_score`, `historical_signal`
    # hot/cold labels, or fresh edge_percent from odds movement kept
    # shuffling which picks won the top-3 slots.
    #
    # Fix: cache the emitted top-3 pick IDs per (date, line_type, sport,
    # market, league) filter tuple for 4 hours. On subsequent calls,
    # look up the cached IDs, refetch the pick docs, validate they're
    # still qualifying (not off_board, not settled, still in play
    # window), and return them AS-IS. Only recompute when a slot is
    # invalidated OR the cache expires.
    #
    # Cache is process-local (no Redis) — good enough for typical usage
    # and cleared on backend restart.
    global _ROLLOVER_STICKY_CACHE  # noqa: PLW0603
    if "_ROLLOVER_STICKY_CACHE" not in globals():
        _ROLLOVER_STICKY_CACHE = {}    # type: ignore
    _sticky_key = (
        _today_str(),
        (line_type or "").lower(),
        (sport or "all").lower(),
        (market or "").lower(),
        (league or "").lower(),
    )
    _now_ts = datetime.now(timezone.utc)
    is_sticky_hit = False
    cached = _ROLLOVER_STICKY_CACHE.get(_sticky_key)
    if cached and (_now_ts - cached["at"]).total_seconds() < 14400:  # 4h TTL
        cached_ids = cached.get("ids") or []
        if cached_ids:
            docs = await db.picks.find(
                {"id": {"$in": cached_ids}, "no_bet": {"$ne": True},
                 "off_board": {"$ne": True}, "status": {"$in": ["pending", None]}},
                {"_id": 0},
            ).to_list(length=len(cached_ids))
            # Preserve the original ranked order
            by_id = {d["id"]: d for d in docs if "id" in d}
            docs_ordered = [by_id[i] for i in cached_ids if i in by_id]
            # Filter to picks still in play window
            docs_ordered = _filter_in_play_window(docs_ordered)
            if len(docs_ordered) == len(cached_ids):
                # All cached picks still valid — return sticky result
                is_sticky_hit = True
                return {
                    "picks": _canonicalize_picks(docs_ordered),
                    "pick": _canonicalize_lock_score(docs_ordered[0]) if docs_ordered else None,
                    "composite_rank": None,
                    "total_evaluated": len(docs_ordered),
                    "scoped_to_today": True,
                    "rollover_version": "v4-sticky",
                    "sticky": True,
                    "survivability": cached.get("survivability", {"mode": "sticky_hit"}),
                }
    # Sticky miss or expired — proceed with full recompute below.

    # ── μ-closure P4 (2026-06) — DB-FROZEN MEMBERSHIP RESTORE ────────
    # The RAM sticky cache above is cleared on backend restart / worker
    # rotation, but the ``on_rollover_at`` + ``rollover_selection_rank``
    # stamps in ``db.picks`` are IMMUTABLE for the product day.  If we
    # find frozen top-3 picks for today's date that still qualify
    # (not off_board, not settled, still in-window), we return those
    # in their frozen rank order — the display SURVIVES cache clears
    # and process restarts.
    #
    # Only runs when the caller has not supplied narrowing filters that
    # would change the eligible universe (sport / market / league /
    # non-empty line_type).  For filtered slices we fall through to
    # the fresh recompute so per-filter picks reflect the requested
    # slice — those are ranked/cached separately by ``_sticky_key``.
    #
    # 2026-06 live-crash fix: derive `sport_filter_active` INLINE here
    # so the frozen-restore block never crashes on the first request
    # (previously it referenced the variable before it was assigned
    # further down during base_q construction).
    _sport_active_here = bool(sport and sport.lower() != "all")
    _no_filters = (
        not _sport_active_here
        and not (market or "").strip()
        and not (league or "").strip()
        and (line_type or "").lower() in ("", "both")
    )
    if _no_filters:
        try:
            frozen_docs = await db.picks.find(
                {"pick_date": _today_str(),
                 "on_rollover_at": {"$exists": True},
                 "rollover_frozen_source": "picks_route_live",
                 "rollover_selection_rank": {"$in": [1, 2, 3]},
                 "no_bet": {"$ne": True},
                 "off_board": {"$ne": True},
                 "status": {"$in": ["pending", None]}},
                {"_id": 0},
            ).to_list(length=16)
            if frozen_docs:
                # Deduplicate — earliest stamp wins (guards against any
                # historical double-stamp).
                by_rank: dict[int, dict] = {}
                for d in frozen_docs:
                    r = int(d.get("rollover_selection_rank") or 0)
                    if r in (1, 2, 3) and r not in by_rank:
                        by_rank[r] = d
                ordered = [by_rank[r] for r in (1, 2, 3) if r in by_rank]
                # Must have all 3 ranks AND survive the play-window
                # gate to serve from frozen membership.  Missing ranks
                # (e.g. only #1+#2 stamped from a partial earlier
                # response) fall through to full recompute so the
                # user gets a complete top-3.
                ordered = _filter_in_play_window(ordered)
                if len(ordered) == 3:
                    logger.info(
                        "Rollover DB-frozen restore hit: 3 picks recovered "
                        "for %s (survived cache clear)", _today_str(),
                    )
                    _ROLLOVER_STICKY_CACHE[_sticky_key] = {
                        "ids":            [p.get("id") for p in ordered if p.get("id")],
                        "at":             _now_ts,
                        "survivability":  {"mode": "db_frozen_restore"},
                    }
                    return {
                        "picks": _canonicalize_picks(ordered),
                        "pick":  _canonicalize_lock_score(ordered[0]),
                        "composite_rank": None,
                        "total_evaluated": len(ordered),
                        "scoped_to_today": True,
                        "rollover_version": "v4-db-frozen",
                        "sticky": True,
                        "survivability": {"mode": "db_frozen_restore"},
                    }
        except Exception as _frozen_err:
            logger.debug("DB-frozen rollover restore skipped: %s", _frozen_err)
    # DB-frozen miss — full recompute path.

    base_q: dict = {"pick_date": _today_str(), "no_bet": {"$ne": True}}
    # PHASE 1D (2026-06) — Shared Product Source contract.  Rollover
    # 2.0 consumes canonical-eligible picks only: real book line
    # present, not off_board, not model-only.  See
    # ``services.main_board_eligibility.is_canonical_eligible``
    # for the Python-side helper used by the parlay engine.
    base_q["off_board"]        = {"$ne": True}
    base_q["no_real_book_line"] = {"$ne": True}
    base_q["model_only"]       = {"$ne": True}
    base_q["book_odds"]        = {"$exists": True, "$ne": None}
    lt = (line_type or "").lower()
    if lt == "main":
        base_q["is_alt"] = {"$ne": True}
    elif lt == "alt":
        base_q["is_alt"] = True
    sport_filter_active = bool(sport and sport.lower() != "all")
    if sport_filter_active:
        base_q["sport"] = sport
    if market:
        regex = _market_regex(market)
        if regex:
            base_q["market"] = {"$regex": regex, "$options": "i"}
    if league:
        base_q["league"] = {"$regex": re.escape(str(league)), "$options": "i"}  # SEC-004

    # Require POSITIVE expected value — negative-edge picks must never
    # appear in Rollover regardless of lock_score.
    base_q["edge_percent"] = {"$gte": 0}

    # ── V4 MARKET BLACKLIST ──────────────────────────────────────────
    # These families are BANNED from rollover based on the user's own
    # 1,441-pick settled history (2026-07-01 audit):
    #   • Soccer goalscorer / assist family → 4-15% hit rate
    #   • MLB H+R+RBI                       → 35.6%
    #   • MLB NRFI / YRFI                   → 41.5%
    # NOTE: MLB Strikeouts REMOVED from blacklist — data shows they hit
    # at 72.4% and are actually one of the strongest markets.
    existing_market_q = base_q.pop("market", None)
    excluded_markets_block = {
        "market": {
            "$not": {
                "$regex": (
                    # Soccer goalscorer / assist family (banned)
                    r"goal scorer|to score or assist|score or assist"
                    r"|score and assist|score & assist"
                    r"|to score 2|to score 3"
                    r"|hat.?trick|first goal|last goal|winning goal|to assist"
                    # MLB banned family (data-driven)
                    r"|nrfi|yrfi"
                    r"|hits\s*\+\s*runs\s*\+\s*rbi|h\+r\+rbi|hits, runs.+rbi"
                ),
                "$options": "i",
            },
        },
    }
    if existing_market_q:
        base_q["$and"] = [{"market": existing_market_q}, excluded_markets_block]
    else:
        base_q["market"] = excluded_markets_block["market"]

    # ─── V4 FLOORS + WINDOWS (data-driven) ───────────────────────────
    LOCK_FLOOR       = 89       # 89-99 all hit 65-78% historically
    LOCK_DEAD_LO     = 80       # exclude 80-84 band (47.6% inverted)
    LOCK_DEAD_HI     = 85
    WP_FLOOR         = 0.60     # 60% WP — the natural floor of the 89+ tier
    EDGE_FLOOR       = 0.0      # any positive edge
    EDGE_CAP         = 12.0     # >12% is inverted signal (51%)
    CHALK_CAP        = -350     # payout viability
    ODDS_DEAD_LO     = -140     # exclude -140 to -110 (47.6% coin flip)
    ODDS_DEAD_HI     = -110
    MAX_LEGS         = 3

    # Market whitelist multipliers (applied in ranking, not filter)
    MARKET_BOOSTS = [
        (r"win or draw|double chance",              1.15),  # 80.0%
        (r"\bstrikeouts?\b",                        1.10),  # 72.4%
        (r"total goals",                            1.05),  # 65.7%
        (r"tennis moneyline|match winner",          1.05),  # 66.7% Tennis ML
        (r"run line|spread|handicap",               1.02),  # 63.5%
        (r"\bhits\b(?!.*runs.*rbi)",                1.00),  # 59.6%
    ]

    def _norm_prob(v) -> float:
        if v is None: return 0.0
        try: f = float(v)
        except Exception: return 0.0
        return f / 100.0 if f > 1.0 else f

    def _market_multiplier(market: str) -> float:
        m = (market or "").lower()
        for pat, boost in MARKET_BOOSTS:
            if re.search(pat, m):
                return boost
        return 1.0

    def _passes_v4(p: dict) -> tuple[bool, str]:
        """Returns (accept, reject_reason). All V4 rules applied here.

        μ-closure LIVE (2026-06) — Rollover canonical-score precedence:
        Read authoritative ``published_lock_score`` when present and
        fall back to legacy ``lock_score`` only when the published
        value is absent.  Pre-fix used ``lock_score`` unconditionally
        which pre-filtered the entire candidate pool to zero when
        publication had shifted the frozen score to the ``published_``
        field.
        """
        lock = float(
            p.get("published_lock_score")
            if p.get("published_lock_score") is not None
            else (p.get("lock_score") or 0)
        )
        odds = float(p.get("book_odds") or -9999)
        edge = float(p.get("edge_percent") or 0)
        wp   = _norm_prob(p.get("win_probability"))
        if lock < LOCK_FLOOR:
            return False, "lock<89"
        if LOCK_DEAD_LO <= lock < LOCK_DEAD_HI:
            return False, "lock_dead_zone_80-84"
        if wp < WP_FLOOR:
            return False, "wp<0.60"
        if edge < EDGE_FLOOR:
            return False, "edge_negative"
        if edge > EDGE_CAP:
            return False, "edge>12_inverted"
        if odds < CHALK_CAP:
            return False, "odds<-350_chalk"
        if ODDS_DEAD_LO <= odds < ODDS_DEAD_HI:
            return False, "odds_dead_zone_-140_-110"
        return True, ""

    # Pull qualifying candidates (V4 filter)
    # ── Universal Flow Recovery (2026-06) — canonical score
    #    precedence at the Mongo pre-filter.
    #    Rollover admission MUST honor ``published_lock_score`` when
    #    present; a pick with published_lock_score=92 and legacy
    #    lock_score=87 was previously starved before ``_passes_v4``
    #    could evaluate it.  Use $expr with $ifNull so the Mongo
    #    predicate matches the same coalesce logic in _passes_v4.
    q = {**base_q, "$expr": {"$gte": [
        {"$ifNull": ["$published_lock_score", "$lock_score"]},
        LOCK_FLOOR,
    ]}}
    candidates: list = await db.picks.find(q, {"_id": 0}).to_list(length=800)
    # ── Root Closure §A — Rollover BASE = canonical Locks eligibility ─
    # Rollover's V4 rules (Lock ≥89, market whitelist, edge band,
    # etc.) run AFTER this gate, never in place of it.  This ensures
    # Rollover's candidate universe is a strict SUBSET of Locks
    # (SAME publication authority, SAME real-book requirement, SAME
    # settlement capability, SAME synthetic/contradiction/dup gates).
    try:
        from services.locks_eligibility import apply_canonical_locks_eligibility_gate
        _cand_before = len(candidates)
        candidates, _canon_dropped = apply_canonical_locks_eligibility_gate(candidates)
        if _cand_before != len(candidates):
            import logging as _lg
            _lg.getLogger("lockscore.rollover").info(
                "Rollover canonical eligibility gate: %d → %d (dropped=%s)",
                _cand_before, len(candidates), _canon_dropped,
            )
    except Exception:
        pass
    reject_reasons: dict[str, int] = {}
    picks: list = []
    for p in candidates:
        ok, reason = _passes_v4(p)
        if ok:
            picks.append(p)
        else:
            reject_reasons[reason] = reject_reasons.get(reason, 0) + 1
    total_candidates = len(candidates)
    rejected_by_gate = total_candidates - len(picks)

    picks = _filter_in_play_window(picks)
    # ── Quality Gate (2026-06-29) — same backtest-driven filter as
    # /picks/today. Rollover is supposed to be our SAFEST pick of the
    # day, so this layer is even more critical here.
    try:
        from quality_gate import apply_quality_gate
        picks, qg_blocked = apply_quality_gate(picks)
        if qg_blocked:
            import logging
            logging.getLogger("lockscore").info(
                "QualityGate blocked on /picks/rollover: %s", qg_blocked,
            )
    except Exception as qg_err:
        import logging
        logging.getLogger("lockscore").warning("QualityGate skipped (rollover): %s", qg_err)
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=24)

    def starts_today(p: dict) -> bool:
        dt = _parse_event_dt(p.get("event_time") or "")
        if dt is None:
            return False
        return now <= dt <= cutoff

    today_picks = [p for p in picks if starts_today(p)]
    pool = today_picks if today_picks else picks
    if not pool:
        # V4: no qualifying picks. Return empty bundle rather than dilute
        # with medium-conviction gap-fill.
        return {
            "picks": [], "pick": None, "total_evaluated": 0,
            "rollover_version": "v4",
            "survivability": {
                "mode": "data_driven",
                "lock_floor": LOCK_FLOOR, "wp_floor": WP_FLOOR,
                "edge_floor": EDGE_FLOOR, "edge_cap": EDGE_CAP,
                "odds_floor": CHALK_CAP,
                "candidates_scanned": total_candidates,
                "rejected_by_gate": rejected_by_gate,
                "reject_reasons": reject_reasons,
            },
        }

    def _ev_score(p: dict) -> float:
        """V4 composite ranker (data-driven weights):
          Base = 0.55·wp + 0.20·sim + 0.15·edge_norm + 0.10·alt_bonus
          × market_multiplier (1.00–1.15)
          × chalk_penalty × hot/cold multiplier"""
        wp = _norm_prob(p.get("win_probability"))
        sim = _norm_prob(p.get("sim_win_probability")) or wp
        edge = float(p.get("edge_percent") or 0)
        odds = float(p.get("book_odds") or -100)
        edge_norm = max(0.0, min(1.0, edge / 8.0))  # normalise at +8pp
        alt_bonus = 1.0 if p.get("is_alt") else 0.0  # alts historically +7pp
        base = 0.55 * wp + 0.20 * sim + 0.15 * edge_norm + 0.10 * alt_bonus
        # Market whitelist boost
        mkt_mult = _market_multiplier(p.get("market") or "")
        # Chalk penalty for extreme favourites (-200 or worse)
        if odds <= -200:
            chalk_pen = min(0.30, (abs(odds) - 200) / 500.0)
        else:
            chalk_pen = 0.0
        # Historical hot/cold multiplier
        sig = p.get("historical_signal") or {}
        if sig.get("label") == "hot" and float(sig.get("consistency") or 0) >= 0.7:
            hist_mult = 1.05
        elif sig.get("label") == "cold":
            hist_mult = 0.95
        else:
            hist_mult = 1.0
        # Signal Engine multiplier (Phase A, 2026-07-12) — persisted
        # 0-100 Signal Score from services/signal_engine. Bounded to
        # ±8% so signals nudge the ranking rather than dominate it.
        ss = p.get("signal_score")
        try:
            sig_mult = (1.0 + ((float(ss) - 50.0) / 50.0) * 0.08) if ss is not None else 1.0
        except (TypeError, ValueError):
            sig_mult = 1.0
        return base * mkt_mult * (1.0 - chalk_pen) * hist_mult * sig_mult

    ranked = sorted(pool, key=_ev_score, reverse=True)

    # ─── Bundle assembly: TOP 3 legs, ONE per game ───
    # V4: no alt-line cap — data shows alts hit 67.5% vs mains 60.8%,
    # so we PREFER them via the ranking bonus rather than capping them.
    seen_events: set = set()
    top: list = []
    for p in ranked:
        ev = p.get("event")
        if ev in seen_events:
            continue
        seen_events.add(ev)
        top.append({**p, "composite_rank": round(_ev_score(p), 2)})
        if len(top) >= MAX_LEGS:
            break
    # ── Persist sticky cache before returning (2026-07-27) ────────────
    _survivability = {
        "mode": "data_driven",
        "lock_floor": LOCK_FLOOR,
        "lock_dead_zone": [LOCK_DEAD_LO, LOCK_DEAD_HI],
        "wp_floor": WP_FLOOR,
        "edge_floor": EDGE_FLOOR,
        "edge_cap": EDGE_CAP,
        "odds_floor": CHALK_CAP,
        "odds_dead_zone": [ODDS_DEAD_LO, ODDS_DEAD_HI],
        "max_legs": MAX_LEGS,
        "market_boosts": [{"pattern": p, "multiplier": m} for p, m in MARKET_BOOSTS],
        "candidates_scanned": total_candidates,
        "rejected_by_gate": rejected_by_gate,
        "reject_reasons": reject_reasons,
    }
    if top:
        _ROLLOVER_STICKY_CACHE[_sticky_key] = {
            "ids":            [p.get("id") for p in top if p.get("id")],
            "at":             _now_ts,
            "survivability":  _survivability,
        }
    # ── PHASE 3 (2026-06) — FROZEN ROLLOVER MEMBERSHIP ─────────────
    # Stamp ``on_rollover_at`` on the top-3 the moment the user first
    # sees them.  Once stamped a pick's membership is IMMUTABLE — the
    # settlement-time reconstruction pass (rollover_history_tagger)
    # will refuse to clear/move a frozen tag, so History → Rollover
    # always reflects the LIVE board even after results come in.
    if top:
        try:
            _tagged_ids = [p.get("id") for p in top if p.get("id")]
            if _tagged_ids:
                _stamp_at = datetime.now(timezone.utc).isoformat()
                # PHASE 7 §7W (2026-06) — Rollover Snapshot metadata.
                # Stamp selection_rank + selector version so History /
                # Analytics can reproduce the exact live selection
                # without postgame reconstruction.
                for _rank, _pid in enumerate(_tagged_ids, start=1):
                    await db.picks.update_many(
                        {"id": _pid,
                         "on_rollover_at": {"$exists": False}},
                        {"$set": {
                            "on_rollover_at":          _stamp_at,
                            "rollover_frozen_source":  "picks_route_live",
                            "rollover_selection_rank": _rank,
                            "rollover_selector_version": "rollover2.picks_route.v1",
                        }},
                    )
        except Exception as _tag_err:
            logger.debug("frozen rollover stamp skipped: %s", _tag_err)

    return {
        "picks": _canonicalize_picks(top),
        "pick": _canonicalize_lock_score(top[0]) if top else None,
        "composite_rank": top[0]["composite_rank"] if top else None,
        "total_evaluated": len(pool),
        "scoped_to_today": bool(today_picks),
        "rollover_version": "v4",
        "sticky": is_sticky_hit,
        "survivability": _survivability,
    }


@router.post("/settle")
async def trigger_settle(user: Annotated[UserPublic, Depends(current_user)]):
    """Manually trigger settlement (also runs every 30 min in background)."""
    from settlement_engine import settle_due_picks  # lazy
    return await settle_due_picks(db)


@router.get("/history")
async def picks_history(
    user: Annotated[UserPublic, Depends(current_user)],
    days: int = 30,
    rollover_only: bool = False,
):
    """Settled picks from the last N days, newest first.

    ── Final Production μ-closure (2026-06) — B1 fix ─────────────────
    The UI copy says "Pull to refresh to trigger a settlement check".
    Prior implementation only re-fetched history without invoking the
    settlement pipeline — so completed canonical picks that had never
    yet been graded would keep showing "TOTAL 0" forever in
    Production even though the settlement scheduler was running.  We
    now kick a fire-and-forget settlement pass at the start of every
    history request so a pull-to-refresh actually converges the
    canonical W/L/PUSH state before we project it.

    Fire-and-forget is intentional — the response DOES NOT block on
    settlement completion (which can take seconds when the queue is
    non-trivial).  The next pull-to-refresh will see any newly
    graded picks.  Failure is swallowed to keep read-only guarantees.
    """
    # ── Block 4F μ-closure — SINGLE HISTORY SETTLEMENT TRIGGER ────
    # PRIOR DEFECT: every /history GET fired an unconditional
    # create_task(settle_due_picks(db)) — a rapid pull-to-refresh
    # or a concurrent frontend + POST /picks/settle could launch
    # multiple overlapping settlement passes doing the same work.
    #
    # NEW: bounded module-level single-flight guard.  If a
    # settlement task from a recent history refresh is still in
    # flight (or ran within a small cooldown window), we skip
    # the fire-and-forget.  Failure paths never block the read.
    try:
        import asyncio as _aio, time as _t
        from settlement_engine import settle_due_picks
        global _HIST_SETTLE_COOLDOWN_UNTIL, _HIST_SETTLE_INFLIGHT
        _now_ts = _t.monotonic()
        _cool_until = globals().get("_HIST_SETTLE_COOLDOWN_UNTIL", 0.0)
        _inflight  = globals().get("_HIST_SETTLE_INFLIGHT", None)
        _still_running = bool(_inflight and not _inflight.done())
        if _still_running or _now_ts < _cool_until:
            pass    # Skip — recent trigger still owns the pass.
        else:
            async def _wrapped():
                try:
                    await settle_due_picks(db)
                finally:
                    # 30s cooldown after finish → prevents thrash.
                    import time as _tt
                    globals()["_HIST_SETTLE_COOLDOWN_UNTIL"] = (
                        _tt.monotonic() + 30.0)
            globals()["_HIST_SETTLE_INFLIGHT"] = _aio.create_task(_wrapped())
    except Exception:
        pass  # settlement failure must NEVER break the history read
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    # ─── P0.5 (2026-08) Canonical Published-Results Truth ─────────────
    # History MUST derive from the same canonical published population
    # as Analytics.  Outcome (won/lost) NEVER participates in dedupe,
    # current off_board/lock_score can NEVER erase historical
    # publication, unresolved/void are first-class visible states.
    #
    # The heavy lifting (provenance gate, outcome-neutral dedupe,
    # publication_snapshot join) lives in the truth service.
    from services.published_results_truth import (
        PublishedResultsTruthService,
    )
    _truth = PublishedResultsTruthService(db)
    picks = await _truth.load(days=days,
                                exclude_ambiguous_legacy=True,
                                include_pending=True)
    # ─── P0.2c (2026-08-13) Canonical History Projection ──────────
    # History is now a deterministic projection of canonical settlement
    # truth (`settlement_events`) + frozen prediction snapshots.  The
    # projection service enriches each pick with settlement lineage,
    # preserves frozen pregame values, and refuses to project WON/LOST
    # for picks that lack an active canonical settlement event.
    #
    # Read-only: this join does NOT mutate `db.picks` or the ledger.
    try:
        from services.history_projection_service import (
            HistoryProjectionService,
        )
        _proj = HistoryProjectionService(db)
        picks = await _proj.project_many(picks)
    except Exception as _pe:
        # Non-fatal: fall back to the pre-P0.2c mirror-only view.
        # A rogue-writer test still catches direct mutations.
        import logging as _lg
        _lg.getLogger("lockscore.history").warning(
            "history projection fallback: %s", _pe)
    # ── History Zero μ-fix (route cap, 2026-06) ───────────────────
    # Prior route order:
    #   1. sort by event_time DESC
    #   2. slice [:2000]
    #   3. filter settled
    # Root cause: thousands of newer PENDING / future canonical
    # picks (with event_time > all older settled picks) filled the
    # 2000-row cap and evicted legitimate settled WIN/LOSS rows from
    # the response — the History UI showed "Total 0" even though the
    # loader had correctly returned the settled slice.
    #
    # New route order (starvation-proof at the route boundary):
    #   1. Split into HISTORY-VISIBLE (settled) + PENDING
    #   2. Sort settled newest-first (settled_at → event_time)
    #   3. Apply the 2000-row cap to the SETTLED slice ONLY
    #   4. Summarise / expose stats over the returned settled records
    #   5. Pending picks remain available inside `_truth.summarise` for
    #      diagnostic counts but never consume the response payload.
    _HISTORY_STATES = ("won", "lost", "push", "void", "unresolved")
    history_visible = [p for p in picks
                        if p.get("status") in _HISTORY_STATES]
    # Sort history-visible newest-first — prefer settled_at (canonical
    # grading timestamp) then fall back to event_time.
    history_visible.sort(
        key=lambda p: (p.get("settled_at") or p.get("event_time") or ""),
        reverse=True,
    )
    # Apply the 2000-row response cap to the settled slice ONLY.
    picks = history_visible[:2000]

    # ─── §8 explicit canonical-state visibility ────────────────────
    _summary = _truth.summarise(picks)
    settled = [p for p in picks
                if p.get("status") in _HISTORY_STATES]
    won         = _summary["won"]
    lost        = _summary["lost"]
    push        = _summary["push"]
    void_ct     = _summary["void"]
    unresolved  = _summary["unresolved"]
    decided     = won + lost
    hit_rate    = _summary["hit_rate_pct"] or 0.0
    # ─── §9 sweep-validity check available to callers ──────────────
    _sweep = _truth.verify_sweep(picks)
    # Rollover V4 (2026-07-08): Rollover history filters STRICTLY on
    # the `on_rollover_at` publish-time tag now stamped by
    # `rollover_history_tagger` at settlement time.  The tag is
    # re-derived deterministically from the V4 top-3 slate per date,
    # so History → Rollover matches the exact picks the user saw on
    # the live Rollover tab (see rollover_history_tagger.py).
    #
    # The previous "fallback threshold" path (lock ≥ 95 AND wp ≥ 0.80
    # AND edge ≥ 4.0) inflated the tab with alt-line MLB totals that
    # were never on the live Rollover board — user complaint 2026-07-08:
    # "picks in rollover sections are regular picks…showing MLB alt
    # totals when they wasn't on rollover in general".  We drop that
    # fallback entirely; if no picks are tagged we return an empty set
    # rather than fabricate a superset.
    rollover_picks = [p for p in settled if p.get("on_rollover_at")]
    ro_won = sum(1 for p in rollover_picks if p.get("status") == "won")
    ro_lost = sum(1 for p in rollover_picks if p.get("status") == "lost")
    ro_push = sum(1 for p in rollover_picks if p.get("status") == "push")
    ro_decided = ro_won + ro_lost
    ro_hit_rate = round(ro_won / ro_decided * 100, 1) if ro_decided else 0.0
    if rollover_only:
        # Stats must reflect the SAME scope as the returned picks list.
        settled = rollover_picks
        # ─── P0.5 §8 canonical states applied to rollover scope ────
        _ro_summary = _truth.summarise(rollover_picks)
        _ro_sweep   = _truth.verify_sweep(rollover_picks)
        return {
            "picks": settled,
            "stats": {
                "total": len(settled),
                "won": ro_won,
                "lost": ro_lost,
                "push": ro_push,
                "void": _ro_summary["void"],
                "unresolved": _ro_summary["unresolved"],
                "verified_decisions": ro_won + ro_lost + ro_push,
                "hit_rate": ro_hit_rate,
                "rollover_hit_rate": ro_hit_rate,
                "rollover_decided": ro_decided,
                "sweep_valid": _ro_sweep["is_valid_sweep"],
                "sweep_reasons": _ro_sweep["reasons"],
            },
        }
    return {
        "picks": settled,
        "stats": {
            "total": len(settled),
            "won": won,
            "lost": lost,
            "push": push,
            # ─── P0.5 §8 explicit canonical states ─────────────────
            "void": void_ct,
            "unresolved": unresolved,
            "verified_decisions": won + lost + push,
            "hit_rate": hit_rate,
            "rollover_hit_rate": ro_hit_rate,
            "rollover_decided": ro_decided,
            # ─── P0.5 §9 sweep validity (never claim false sweep) ─
            "sweep_valid": _sweep["is_valid_sweep"],
            "sweep_reasons": _sweep["reasons"],
        },
        # ─── PERKLOCKS-MAIN 34 · P0D — Settlement freshness ────────
        # Explicit hint to the client so it can auto-refresh after a
        # settlement pass triggered by this request completes. Prevents
        # "refresh #1 stale, refresh #2 different" without indication.
        "settlement_freshness": {
            # Task is running in the background right now.
            "settlement_in_flight": bool(
                globals().get("_HIST_SETTLE_INFLIGHT")
                and not globals().get("_HIST_SETTLE_INFLIGHT").done()
            ),
            # Cooldown window still active — a recent pass just ran.
            "settlement_cooldown_until": float(
                globals().get("_HIST_SETTLE_COOLDOWN_UNTIL", 0.0)
            ),
            # Recommended client re-poll delay (seconds) when a task is
            # still in-flight; None otherwise.
            "recommended_repoll_seconds": (
                4 if (globals().get("_HIST_SETTLE_INFLIGHT")
                       and not globals().get("_HIST_SETTLE_INFLIGHT").done())
                else None
            ),
            # Server-side count of picks whose canonical settlement
            # event has not landed yet (unresolved-with-past-event ⇒
            # more grading is due). Client can surface this diagnostic.
            "unresolved_with_past_event": sum(
                1 for _p in picks
                if _p.get("status") == "unresolved"
                and (_p.get("event_time") or "") < datetime.now(timezone.utc).isoformat()
            ),
        },
    }



# ───────────────────────── Phase 3 (2026-06-27) ─────────────────────────
# The three big endpoints — the home-feed primary, the deprecated bet-
# killer stub, the parlay optimizer, and the manual refresh — all share
# enough helpers with the rest of the picks router to live here. Now
# that THIS module owns every /picks/* route, the include_router() call
# in server.py was moved back to the top of the file (right after the
# `api = APIRouter()` declaration) where it belongs.
#
# Same lazy-import discipline as Phase 1+2: every helper from server.py
# is imported INSIDE the handler so the router stays importable even if
# server.py is mid-bootstrap.

REFRESH_COOLDOWN_SECONDS = 3600  # 1 hour — matches scheduler cadence


def _cooldown_payload(last_iso, now: datetime) -> dict:
    """Compute cooldown state for the refresh rate-limiter.

    Returns dict with `can_refresh`, `cooldown_seconds` (remaining),
    `next_refresh_at` (ISO string, or None), `last_refresh_at`.
    Safe against missing/malformed timestamps.

    NOTE: A duplicate of this function still lives in server.py (used by
    /picks/refresh-status above which also lives in this module — that
    one ALSO imports `_cooldown_payload` lazily from server). To avoid
    two slightly-different implementations drifting, we override the
    Phase-1 lazy import locally — refresh-status now reads THIS impl
    via deps.py rather than reaching back into server.py. See refresh-
    status handler at top of file for the indirection.
    """
    if not last_iso:
        return {
            "can_refresh": True,
            "cooldown_seconds": 0,
            "next_refresh_at": None,
            "last_refresh_at": None,
        }
    try:
        last_dt = datetime.fromisoformat(last_iso)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
    except Exception:
        return {
            "can_refresh": True,
            "cooldown_seconds": 0,
            "next_refresh_at": None,
            "last_refresh_at": None,
        }
    elapsed = (now - last_dt).total_seconds()
    remaining = max(0, REFRESH_COOLDOWN_SECONDS - int(elapsed))
    next_dt = last_dt + timedelta(seconds=REFRESH_COOLDOWN_SECONDS)
    return {
        "can_refresh": remaining <= 0,
        "cooldown_seconds": remaining,
        "next_refresh_at": next_dt.isoformat() if remaining > 0 else None,
        "last_refresh_at": last_dt.isoformat(),
    }


# ─── /picks/signal-rank/refresh (admin trigger) ──────────────────────
# Forces a slate-wide signal-score backfill + percentile-rank pass.
# Handy for debugging when the front-end reports "signal filter is
# there just no picks" — one call and every pick on today's board is
# ranked 0-100 on its raw Signal Engine output.
@router.post("/signal-rank/refresh")
async def refresh_signal_rank(user: Annotated[UserPublic, Depends(current_user)]):
    from server import _today_str
    from services.signal_engine import refresh_slate_signal_rank
    return await refresh_slate_signal_rank(db, _today_str(), force=True)


# ─── /picks/today (main home feed) ───────────────────────────────────
# Lifted verbatim from server.py — see git history for the change log.
# Only the decorator was rewritten (`@api.get("/picks/today")` →
# `@router.get("/today")`).
@router.get("/today")
async def picks_today(user: Annotated[UserPublic, Depends(current_user)],
                      sport: Optional[str] = None,
                      sports: Optional[str] = None,          # NEW: CSV multi-select
                      grade: Optional[str] = None,
                      day_offset: Optional[int] = None,
                      line_type: Optional[str] = None,
                      sort: Optional[str] = "time",
                      direction: Optional[str] = "desc",
                      min_lock: Optional[float] = None,
                      min_signal: Optional[float] = None,
                      min_implied: Optional[float] = None,
                      max_implied: Optional[float] = None,
                      market: Optional[str] = None,
                      markets: Optional[str] = None,         # NEW: CSV multi-select
                      league: Optional[str] = None,
                      leagues: Optional[str] = None,         # NEW: CSV multi-select
                      game_ids: Optional[str] = None,        # NEW: CSV multi-select
                      events: Optional[str] = None,          # NEW: pipe-separated multi-select
                      search: Optional[str] = None,          # NEW: free-text search
                      lite: Optional[bool] = False):
    """Top picks from today's 72-hour window (lock score >= 85).

    Filtering supports BOTH legacy singular params and the new multi-select
    CSV params (added 2026-06-26 for the unified filter store):

      Single (legacy)        Multi-select (new)
      ---------------        ------------------
      sport=MLB              sports=MLB,Soccer,Tennis
      league=EPL             leagues=EPL,La Liga
      market=Hits            markets=Hits,RBIs
      (n/a)                  game_ids=evt_abc,evt_def
      (n/a)                  events=Yankees @ Red Sox|Dodgers @ Mets
      (n/a)                  q=jefferson         (free-text contains)

    When both are present, results are the UNION (`$in` query). Empty
    arrays = no filter.
    """
    # Lazy import every helper from server.py used in this handler.
    # server.py itself `include_router`s this module so a top-level
    # `from server import ...` would deadlock the bootstrap.
    from server import (
        _ensure_today_picks, _today_str, _filter_in_play_window,
        _canonicalize_picks, _market_regex,
        _dedupe_game_outcome_picks, _dedupe_goalscorer_per_event,
        _collapse_cross_book_duplicates,
        _decorate_with_player_form, _decorate_with_understat_form,
        _decorate_with_espn_meta,
        _strip_for_lite,
    )
    await _ensure_today_picks()

    # ── Slate-wide Signal Score percentile ranking (2026-07-17) ─────
    # Coverage sweep + percentile-rank pass. Ranks are persisted on
    # disk by `services.signal_engine.rank.refresh_slate_signal_rank`,
    # invoked by the scheduler tick and by `_refresh_picks` post-
    # ingestion. This request never blocks on the rank pass; the
    # `min_signal` filter runs against whatever ranks are already in
    # the DB, and freshly-ingested picks missing the field are
    # transparently treated as neutral 50 (see the query builder).
    #
    # ── 2026-07-18 (mobile stability) ────────────────────────────────
    # Previous versions ran a "hybrid" path here that awaited the
    # rank refresh on the first request after a backend restart or
    # after the 3-min TTL expired. That could block the handler for
    # 3-13s while mongo hummed under scheduler load, and Expo Go
    # users hit the client-side 20s ceiling more often than not on
    # cellular. User feedback: "Expo Go home tab still not loading
    # picks and saying connection hiccup".
    #
    # Fix: fire-and-forget in ALL cases. The rank refresh is
    # idempotent, TTL-cached at 3 min, and cheap enough that
    # bouncing it every request in the background is a no-op after
    # the first tick. If the first tick has never run (very cold
    # boot) the filter degrades gracefully — picks with no
    # `signal_score` are included as neutral (see min_signal branch
    # above) so the board is never empty just because ranking is
    # still catching up.
    try:
        from services.signal_engine import refresh_slate_signal_rank
        asyncio.create_task(refresh_slate_signal_rank(db, _today_str()))
    except Exception as _rank_err:
        logger.warning("Signal-rank slate refresh skipped: %s", _rank_err)
    # ── Normalise the multi-select params to lists, merging with legacy ──
    def _split_csv(s: Optional[str]) -> list[str]:
        if not s:
            return []
        return [x.strip() for x in s.split(",") if x.strip()]

    def _split_pipe(s: Optional[str]) -> list[str]:
        if not s:
            return []
        return [x.strip() for x in s.split("|") if x.strip()]

    # ── HARD GUARD (2026-06-28): when an explicit single `sport=` is
    # provided in the query string, it ALWAYS overrides the multi-select
    # `sports=` array. This protects users on stale client bundles where
    # a leftover `sports=["Soccer"]` (or any sport) in their persisted
    # filter store was silently overriding the sport-tab they just
    # tapped, leaving e.g. the MLB tab showing nothing because the
    # backend was filtering to "Soccer" regardless.
    # User report: "now only showing soccer please fix".
    if sport:
        sport_list = [sport]
    else:
        sport_list = _split_csv(sports)
    league_list = _split_csv(leagues) or ([league] if league else [])
    market_list = _split_csv(markets) or ([market] if market else [])
    game_id_list = _split_csv(game_ids)
    event_list = _split_pipe(events)
    # Treat as filtered if EITHER the legacy single value OR the new
    # multi-select array is populated. This drives the default-floor relax.
    has_market_filter = bool(market_list)
    has_league_filter = bool(league_list)
    # Phase 1 Final Closure (2026-08-11): the Locks contract is TRUE
    # `> 85` and governs EVERY Locks view — main board, market-filtered,
    # and alt-line — without exception.  Filters narrow the qualifying
    # >85 pool; filters must NEVER lower the Locks threshold.
    #
    # The previous per-view lowerings (75 for market-filtered, 55 for
    # alt) have been retired.  User-supplied ``min_lock`` above 85 still
    # narrows further; ``min_lock`` below or equal to 85 is clamped up
    # to the base contract.
    #
    # See: services/main_board_eligibility.py for the central helper.
    from services.main_board_eligibility import (
        MAIN_BOARD_LOCK_FLOOR_EXCLUSIVE,
        main_board_lock_score_query,
    )
    lt = (line_type or "").lower()
    default_floor = MAIN_BOARD_LOCK_FLOOR_EXCLUSIVE  # 85.0 (INCLUSIVE >=)
    # User-supplied min_lock only takes effect when it *narrows* the pool
    # (> 85).  A min_lock ≤ 85 falls through to the base >85 contract so
    # a stale client filter cannot re-open the board.
    if min_lock is not None:
        try:
            _ml = float(min_lock)
        except (TypeError, ValueError):
            _ml = default_floor
        floor = max(default_floor, _ml)
    else:
        floor = default_floor
    # ── Phase-1: main-board thin-slate fallback DISABLED ───────────────
    # The 85 → 75 → 65 → 55 auto-relax was retired 2026-08-08 per
    # Phase 1 requirements ("prove no active main-board fallback lowers
    # the threshold to 75 / 65 / 55").  If the slate is genuinely thin
    # at >85, the main board correctly shows fewer picks — we do NOT
    # inflate the board with sub-eligibility candidates.  The auto-
    # relax logic is preserved below solely for legacy line_type="alt"
    # and market-filtered sub-tabs, which are NOT the main Locks
    # eligibility contract.
    auto_relaxed_from: Optional[float] = None
    _is_main_board_view = (
        min_lock is None and not has_market_filter and not has_league_filter
        and not game_id_list and not event_list and not search
        and not day_offset and not grade and lt != "alt"
    )
    if False and _is_main_board_view:   # main-board relax disabled
        pass
        try:
            _td = _today_str()
            count_at_floor = await db.picks.count_documents({
                "pick_date": _td,
                "$or": [
                    {"lock_score": {"$gte": floor}},
                    {"lock_score_v2": {"$gte": floor}},
                ],
                "no_bet": {"$ne": True},
                "edge_percent": {"$gte": 0},
            })
            # Try lower floors only if the strict slate is meaningfully thin.
            # 8 is the cutoff because below that you can't even build a
            # 3-leg parlay variety, which defeats the product purpose.
            if count_at_floor < 8:
                for relaxed in (75.0, 65.0, 55.0):
                    if relaxed >= floor:
                        continue
                    relaxed_count = await db.picks.count_documents({
                        "pick_date": _td,
                        "$or": [
                            {"lock_score": {"$gte": relaxed}},
                            {"lock_score_v2": {"$gte": relaxed}},
                        ],
                        "no_bet": {"$ne": True},
                        "edge_percent": {"$gte": 0},
                    })
                    if relaxed_count >= 8:
                        auto_relaxed_from = floor
                        floor = relaxed
                        logger.info(
                            "Home feed auto-relaxed floor %s → %s (was %d picks, now %d)",
                            auto_relaxed_from, floor, count_at_floor, relaxed_count,
                        )
                        break
                else:
                    # Even at 55 it's thin; pin floor at 55 anyway so the
                    # user at least sees everything in the DB.
                    if floor > 55.0:
                        auto_relaxed_from = floor
                        floor = 55.0
                        logger.info(
                            "Home feed auto-relaxed floor %s → 55 (slate thin across all bands)",
                            auto_relaxed_from,
                        )
        except Exception as _relax_err:
            logger.debug("Auto-relax probe failed (non-fatal): %s", _relax_err)
    # Two-bucket query:
    #  • Standard picks: must pass lock floor + edge >= 0 + not no_bet
    #  • Elite-player anchors (Mbappé, Haaland, Messi, Kane, Ronaldo synth FGS
    #    etc.): bypass lock floor + edge filter — they're reputation-locked
    #    Elite tier even when raw math is borderline. Still must not be NO-BET.
    #
    # NOTE: We deliberately DO NOT exclude `is_under_lock` here anymore.
    # Under-style locks (e.g. "Total Games Under 28.5") are still high-
    # confidence picks the user expects to see when filtering by sport. The
    # Bet Killer / Under-of-the-Day tabs surface them separately too, but
    # users found their absence from the main sport tab confusing.
    # Floor check must consider EITHER lock_score (legacy/validator-drifted)
    # OR lock_score_v2 (canonical, refreshed every cycle). The pick_validator
    # can write a stale low value into lock_score while v2 still holds the
    # correct high score — without this OR, the home feed hides those picks
    # silently. The serializer (`_canonicalize_lock_score`) then promotes
    # whichever is higher before returning, so the user sees the right number.
    # Phase 1 Final Closure (2026-08-11): the primary Locks predicate is
    # now delegated to the central helper.  The helper prefers
    # ``published_lock_score`` (canonical) and only falls back to
    # ``lock_score`` / ``lock_score_v2`` for pre-Phase-1c rows that have
    # not been snapshot-published yet.  This closes the stale-legacy
    # override loophole: a canonically de-locked pick with a lingering
    # high ``lock_score_v2`` can no longer sneak back onto the board.
    #
    # ``lock_score_raw`` and ``lock_score_peak`` were pre-canonical
    # shadow fields; they are intentionally NOT consulted here — the
    # canonical published value is authoritative.
    _primary_lock_predicate = main_board_lock_score_query(
        min_lock=floor if floor > MAIN_BOARD_LOCK_FLOOR_EXCLUSIVE else None,
    )

    # ── PERKLOCKS CANONICAL 85+ LOCKS REACHABILITY CLOSURE (2026-06) ────
    # Local helper: canonical Lock Score predicate at an arbitrary floor
    # for the sport-specific carve-outs below (elite / model-only /
    # tennis / MLB K / MLB hitter / soccer scorer / high-lock bypass).
    #
    # Contract:
    #   1. Prefer canonical ``published_lock_score`` when present.
    #   2. Fall back to legacy ``lock_score`` / ``lock_score_v2`` only
    #      when the canonical field is absent on the doc.
    #
    # This closes the "stale legacy score overrides canonical" loophole:
    # a canonically-published 85+ pick can no longer be blocked by a
    # lingering low ``lock_score`` on the row, and a canonically-
    # de-locked pick can no longer sneak back onto the board via a
    # stale high ``lock_score_v2``.
    def _canonical_lock_or(floor_: float) -> dict:
        return {"$or": [
            {"published_lock_score": {"$gte": float(floor_)}},
            {
                "$and": [
                    {"published_lock_score": {"$exists": False}},
                    {"$or": [
                        {"lock_score":    {"$gte": float(floor_)}},
                        {"lock_score_v2": {"$gte": float(floor_)}},
                    ]},
                ]
            },
        ]}
    standard_q = {
        "no_bet": {"$ne": True},
        # PERKLOCKS Canonical 85+ Reachability (2026-06): the main board
        # is gated by CANONICAL LOCK SCORE only.  Sportsbook edge is
        # kept as a display/analytics signal but MUST NOT gate board
        # visibility — the prior ``edge_percent >= -8`` clause was
        # suppressing canonically-locked 85+ picks whenever heavy-juice
        # favorites priced below implied.  Chalk-trap / chalk-verified
        # / ESPN-fallback bypasses stay in the score OR so those
        # explicit product carve-outs still surface.
        "$or": [
            # Canonical Locks predicate (published_lock_score first).
            _primary_lock_predicate,
            # ── Chalk Trap picks (2026-07-21) ─────────────────────
            # User: "I still want the 200 picks for options" —
            # chalk-trapped picks have lock=72 (below floor) BUT must
            # stay visible so the ⚠️ TRAP warning surfaces. Match on
            # `chalk_trap_meta.original_lock` (the pre-demotion score)
            # so a pick that was Elite/Strong Lock before the trap
            # still passes floor.
            {"chalk_trap": True, "chalk_trap_meta.original_lock": {"$gte": floor}},
            {"chalk_verified": True},
            # ── ESPN fallback bypass (iter-97, 2026-07-26) ────────
            # Lower-tier soccer leagues (CSL, Sweden, Norway, Finland)
            # are covered by `espn_soccer_fixtures` while The Odds
            # API is 401ing. ESPN scoreboards have thin odds data so
            # these picks land at lock 50-75 (below the default 85
            # floor). Bypass the floor for `source=espn_fallback` so
            # these leagues remain visible; the pick payload carries
            # `odds_source=espn_fallback` + `confidence_penalty=-8`
            # so the frontend can flag it as a soft/fallback pick.
            {"source": "espn_fallback"},
        ],
        # ── ITF Tennis carve-out (2026-07-16) ────────────────────────
        # ITF Futures picks (source=tennis_extra, league contains
        # itf/futures/m15/m25/w15/w25/w35) must NOT enter through
        # standard_q. They're routed exclusively through tennis_extra_q
        # which enforces a strict 95+ lock floor per user mandate:
        # "It's always a lot of ITF I want the locks 95-99 lock scores
        # only so we don't get a lot of noise". Without this NOR,
        # ITF picks landing at 84-86 lock via the tennis calibration
        # slip through standard_q's 80/85 floor and pollute the board.
        "$nor": [
            {
                "sport": "Tennis",
                "league": {"$regex": r"itf|futures|m15|m25|w15|w25|w35", "$options": "i"},
            },
        ],
    }
    # Elite-player anchor query — marquee names (Mbappé, Haaland, Messi,
    # Kane, Ronaldo etc.) skip the strict edge ≥ 0 filter so a
    # reputation-locked superstar appears in the feed even when the
    # market doesn't quite price them as +EV. BUT we still apply a soft
    # lock-floor (≥ 80) so the user never sees the 58-67 lock garbage
    # they reported ("why is app showing 57 56 lock scores"). 80 means
    # "almost Playable" — close enough to the action band that the user
    # treats it as a marquee-name reference rather than a clearly
    # unactionable pick.
    elite_q = {
        "elite_player": True,
        "no_bet": {"$ne": True},
        # PERKLOCKS: canonical score precedence (published_lock_score first).
        **_canonical_lock_or(80.0),
    }
    # ── Model-only (SportDB synth scorer / lower-tier league) carve-out ──
    # Picks generated by `sportdb_player_scorer` for CSL / MLS / J-League /
    # Veikkausliiga / Brasileirão B etc. have no real bookmaker line —
    # there's nothing for the engine to compute an honest `edge_percent`
    # against, so the validator returns a meaningless negative number
    # (typically -7 to -8). That negative edge then collides with the
    # `edge_percent >= 0` gate in `standard_q` and silently hides picks
    # like Silva Felipe (98 lock), Guy Mbenza (91), Wei Shihao (91), etc.
    # User report 2026-06-26: "Something is still blocking CSL goalscorers
    # from board". Carve out so model-only picks surface on lock_score
    # alone (≥75 floor — they're tagged with the MODEL badge in UI).
    model_only_q = {
        "no_bet": {"$ne": True},
        "$or": [
            {"is_model_only": True},
            {"is_synthetic_scorer": True},
            {"source": {"$regex": "^sportdb_scorer"}},
        ],
        # Use a slightly looser lock floor than the standard 85 — model-
        # only picks land at 75-99 depending on career tier and we want
        # the user to see the full lower-league slate.
        # PERKLOCKS: canonical score precedence.
        "$and": [_canonical_lock_or(75.0)],
    }
    # ── Tennis Moneyline carve-out (bandit-hot exception) ──────────────
    # User report 2026-06-22: "Why I got so many tennis overs instead of
    # moneyline?" + "Still no money line tennis in app see spreads and
    # I see money lines on website". The bandit told us Tennis ML is our
    # HOTTEST arm (+13% ROI, Sharpe +1.11) but the edge ≥ 0 gate cuts
    # most of them because chalk tennis MLs (-200/-400) often produce
    # small negative edge vs the sharp market. Carve out: any Tennis ML
    # with a strong lock (≥ 80) gets through with edge ≥ -3, so the
    # bandit's actual winning market surfaces consistently with the book.
    #
    # Additional carve-out: `tennis_extra` picks are book-anchored
    # scrapes (TennisExplorer) with NO independent model — their reported
    # "edge_percent" comes from a self-heal validator pass that compares
    # win_prob vs book_implied, but those are intentionally equal upstream
    # so the validator's negative-edge number is meaningless. Surface
    # tennis_extra ML picks based purely on lock_score (≥ 80) so the
    # 48-hour scraped slate shows up in the feed.
    # 2026-07-16 user mandate: "It's always a lot of ITF I want the
    # locks 95-99 lock scores only so we don't get a lot of noise" — but
    # ONLY for ITF Futures. Main tour ATP/WTA/Challenger keep their
    # standard 80-lock floor. ITF gets a strict 95+ floor so only the
    # sharpest low-tier edges surface.
    tennis_ml_q = {
        "sport": "Tennis",
        "market": {"$regex": "moneyline", "$options": "i"},
        "no_bet": {"$ne": True},
        # PERKLOCKS: edge no longer gates board admission; canonical
        # score precedence replaces legacy lock_score / lock_score_v2.
        "$or": [
            # Path 1: standard Odds-API tennis ML (main tour only)
            {
                # Exclude ITF from Path 1 — routed through Path 2 with strict 95 floor.
                "league": {"$not": {"$regex": r"itf|futures|m15|m25|w15|w25|w35", "$options": "i"}},
                "$or": [
                    {"published_lock_score": {"$gte": 80.0}},
                    {"$and": [
                        {"published_lock_score": {"$exists": False}},
                        {"$or": [
                            {"lock_score":    {"$gte": 80.0}},
                            {"lock_score_v2": {"$gte": 80.0}},
                        ]},
                    ]},
                    {"bandit_lift": {"$gt": 0}},
                ],
            },
            # Path 2: tennis_extra scraped picks — book-anchored
            {
                "source": {"$in": ["tennis_extra", "tennis_extra_model", "tennis_real_odds"]},
                # ITF: require 95+, main tour: 80+
                "$or": [
                    {
                        "league": {"$regex": r"itf|futures|m15|m25|w15|w25|w35", "$options": "i"},
                        **_canonical_lock_or(95.0),
                    },
                    {
                        "league": {"$not": {"$regex": r"itf|futures|m15|m25|w15|w25|w35", "$options": "i"}},
                        **_canonical_lock_or(80.0),
                    },
                ],
            },
        ],
    }
    tennis_extra_q = {
        "sport": "Tennis",
        "source": {"$in": ["tennis_extra", "tennis_extra_model"]},
        "no_bet": {"$ne": True},
        # PERKLOCKS: canonical score precedence.
        "$or": [
            # ITF/Futures — strict 95+ floor
            {
                "league": {"$regex": r"itf|futures|m15|m25|w15|w25|w35", "$options": "i"},
                **_canonical_lock_or(95.0),
            },
            # Main tour (ATP/WTA/Challenger) — 75 floor
            {
                "league": {"$not": {"$regex": r"itf|futures|m15|m25|w15|w25|w35", "$options": "i"}},
                **_canonical_lock_or(75.0),
            },
        ],
    }
    tennis_alt_q = {
        "sport": "Tennis",
        "no_bet": {"$ne": True},
        # PERKLOCKS: edge removed from Locks admission; canonical
        # score precedence for the alt-lock floor.
        "$or": [
            {"is_alt_prop": True},
            {"market": {"$regex": r"\(alt\)|[+\-]\d+(\.\d+)?\s+spread|\bspread\b|total games|games over|games under", "$options": "i"}},
        ],
        "$and": [_canonical_lock_or(70.0)],
    }
    # User report 2026-06-22: "I'm not seeing no strikeout bets" + Gerrit
    # Cole strikeout pick had lock=73.7 (strong) but edge=-6.87 (chalk-priced
    # against). Elite pitchers' K-line markets are often priced sharp, but
    # the lock score reflects the underlying probability accurately. Surface
    # these when lock >= 70 even with slight negative edge.
    #
    # 2026-06-25 update: Re-widened edge floor from -8 → -12 because chalk
    # K props like Seth Lugo Over 2.5 K's (lock 94.5, edge -8.4) and Bryce
    # Miller Over 3.5 K's (lock 94.8, edge -7.2) were appearing in the MLB
    # Lab but missing from the home board. User: "not putting lock bets on
    # board but they in the lab like strikeouts." Lab uses lock >= 78 with
    # no edge gate — board now matches that universe for the K market.
    #
    # 2026-07-21 update — ROI DATA reversed the -12 decision:
    # Analysis of 300 settled K picks + 35 board-visible K picks showed
    # -43.8% ROI on board-visible K picks, with 89% having edge < -5%.
    # The wide edge floor was inviting -400 to -750 chalk alt-locks with
    # negative edge onto the board, where they consistently lost money.
    # Tightening back to edge >= 0 (positive-edge only) kills the entire
    # bleeding segment. Chalk-trap catches any residual chalk K picks
    # that manage positive edge (still requires 8pp + DD confirmation).
    mlb_k_q = {
        "sport": "MLB",
        "market": {"$regex": "strikeout", "$options": "i"},
        "no_bet": {"$ne": True},
        # PERKLOCKS: edge no longer gates board admission; canonical
        # score precedence for the K-market floor.
        **_canonical_lock_or(70.0),
    }
    # ── Soccer Goal Scorer / Score-or-Assist carve-out ──────────────────
    # User report 2026-06-23: "Goalscorers showing on soccer lab but not
    # on the board — shouldn't Harry Kane and Ronaldo be on board?"
    # Diagnosis: Ronaldo S-or-A lock=89.4 edge=-4.68%; Kane S-or-A
    # lock=89.0 edge=-2.0%. Both have strong locks (≥85) but slightly
    # negative edge because the book prices star strikers sharp. The
    # standard_q `edge ≥ 0` gate erases them entirely. Mirrors the
    # tennis ML / MLB strikeout chalk-pricing problem. Carve-out:
    # surface Anytime Goal Scorer + First Goal Scorer + Score-or-Assist
    # picks with strong locks (≥ 85) even at edge ≥ -6% so Kane,
    # Ronaldo, Watkins, etc. land on the board alongside the Soccer
    # Lab — same source of truth.
    soccer_scorer_q = {
        "sport": "Soccer",
        "market": {"$regex": "goal scorer|score or assist|score & assist", "$options": "i"},
        "no_bet": {"$ne": True},
        # PERKLOCKS: edge no longer gates board admission; canonical
        # score precedence for the scorer-market floor.
        **_canonical_lock_or(85.0),
    }
    # ── MLB Hitter alt-lock carve-out ──────────────────────────────
    # User report 2026-06-24: "Where are the hitters at for ATL/SD —
    # I don't see no 1H, HR, RBI". Diagnosis: Hits / Hits+Runs+RBIs /
    # HR / RBI / Total Bases ALT-LOCK picks (Over 0.5 lines) are
    # generated with implied prob ~94% (chalky -1500ish odds), but
    # the standard lock formula scores chalky alt-locks at 55-61 —
    # well below the board's 80 floor. Net effect: zero hitter alt-
    # locks ever surface. Same shape as the MLB pitcher-K and Soccer-
    # scorer carve-outs: surface alt-lock hitter props at lock ≥ 70
    # with edge tolerance to -3% (alt locks are chalky by design).
    #
    # 2026-06-25 update: Re-widened edge floor from -3 → -10 because
    # chalk Hits+Runs+RBIs alt-locks (e.g. Yandy Diaz Over 0.5 HRR at
    # lock 94.8, edge -7.7) were visible in the MLB Lab but missing from
    # the home board. Lab universe is the source of truth — board now
    # matches it for hitter props.
    mlb_hitter_q = {
        "sport": "MLB",
        "market": {
            "$regex": r"hits\s*\+\s*runs\s*\+\s*rbis?|\bhits?\b|home runs|\brbis?\b|total bases",
            "$options": "i",
        },
        # Exclude pitcher markets so the regex above doesn't accidentally
        # double-count strikeouts (already covered by mlb_k_q).
        "$nor": [{"market": {"$regex": "strikeout|outs recorded", "$options": "i"}}],
        "no_bet": {"$ne": True},
        # PERKLOCKS: edge no longer gates board admission; canonical
        # score precedence for the hitter-market floor.
        **_canonical_lock_or(70.0),
    }
    # ── Universal high-lock bypass ───────────────────────────────────
    # Any pick with lock_score ≥ 90 (or v2 ≥ 90) surfaces on the board
    # regardless of edge sign. Rationale: lock_score is the canonical
    # confidence signal — a Lock-band pick (90+) is by definition one
    # we're highly confident in, and chalk-priced props (negative edge
    # by construction) shouldn't be hidden just because the book agrees
    # with us. This mirrors the Soccer/MLB Lab universe (lock ≥ 78, no
    # edge gate) so the board stops disagreeing with the Lab.
    # User report 2026-06-25: "not putting lock bets on board but they
    # in the lab like strikeouts".
    # 2026-07-21 update: EXCLUDE negative-edge MLB strikeout props from
    # this bypass. The high_lock_bypass was letting -100/-249 K props with
    # lock 90+ but edge < 0 through the mlb_k_q's new edge >= 0 gate.
    # Historical Elite Lock 97+ K picks had 0% win rate on 5 picks. Even
    # at 90+ lock, K props with negative edge are structurally losing.
    high_lock_bypass_q = {
        "no_bet": {"$ne": True},
        # PERKLOCKS: canonical score precedence at the 90 bypass floor,
        # and edge-based exclusions have been removed (edge is no longer
        # a Locks admission signal — it remains in the payload as
        # display/analytics context only).
        **_canonical_lock_or(90.0),
    }
    # 2026-07-21 (USER REPORT): "why didn't JL Struff and Cerundolo make
    # board — they play today". Root cause: their tennis picks had
    # pick_date=2026-07-19 (when first ingested) but their Kitzbühel
    # first-round matches shifted to 2026-07-21. The `pick_date == today`
    # filter excluded them. FIX: widen the date match to include picks
    # whose event_time falls in today's UTC window, even if pick_date is
    # stale. This handles rescheduled matches, timezone-crossing events,
    # and ingest lag where a pick was created 1-3 days ahead.
    _today = _today_str()
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    _now = _dt.now(_tz.utc)
    # Widen to -30h / +30h: matches sometimes get rescheduled by a full
    # day (weather delays, schedule adjustments). Struff & Cerundolo at
    # Kitzbühel are the case that motivated this — their picks had
    # event_time=2026-07-20T09Z but the actual match was 2026-07-21T09Z
    # (24-hour reschedule). Pending-status + off_board filters below
    # keep this from resurrecting settled picks or explicit no-bet flags.
    #
    # 2026-08-27 NFL PARITY FIX (universal). User reported NFL invisible
    # on Expo Go even though 29 valid canonical Locks were published for
    # this Thursday/Sunday's slate. Root cause: NFL games Thu 08-28 and
    # Sun 08-30 have `event_time` 46-72h from now, which fell OUTSIDE
    # the ±30h `_win_end` and were only visible when `pick_date == today`.
    # Since the generator tags pick_date as the date the pick was CREATED
    # (08-26), and today is 08-27, both criteria failed for 20 of the 29
    # NFL picks. Widening `_win_end` from +30h to match `_horizon_end`
    # (+72h) keeps the pick_date=today rescue path intact AND lets any
    # game up to 3 days out surface immediately upon publication. The
    # 72h horizon still bounds far-future leaks (line 1667). This
    # equally helps CFB Saturday slate (48-96h out) and MLS/NBA weekend
    # cards — a universal fix, not NFL-specific.
    _win_start = (_now - _td(hours=30)).isoformat().replace("+00:00", "Z")
    _win_end   = (_now + _td(hours=72)).isoformat().replace("+00:00", "Z")
    # ── 72-hour board horizon (2026-07-26) ─────────────────────────────
    # Hide picks for games starting > 72h from now regardless of
    # `pick_date`. Fixes user report: Soccer tab timing out because
    # UEFA/CFB injectors were tagging today's `pick_date` on games 4-5
    # days away (1082 Soccer picks on the board vs realistic ~200).
    _horizon_end = (_now + _td(hours=72)).isoformat().replace("+00:00", "Z")

    q: dict = {
        # Accept picks matching EITHER pick_date=today OR event_time in
        # today's UTC window. Both must still pass the pending/no_bet/
        # off_board filters below, so this only rescues genuinely-
        # upcoming picks with stale pick_date, never resurrects settled
        # picks or picks that got explicitly no_bet-flagged.
        "$and": [
            {"$or": [
                {"pick_date": _today},
                {"event_time": {"$gte": _win_start, "$lte": _win_end}},
            ]},
            # 72-hour horizon — a hard upper bound on event_time so
            # far-future picks (mis-tagged with today's pick_date by
            # the ingest pipeline) can never leak onto the board.
            # Missing event_time is allowed through so picks without
            # a scheduled time (rare — usually MLB DH game 2 etc.)
            # aren't silently hidden.
            {"$or": [
                {"event_time": {"$lte": _horizon_end}},
                {"event_time": {"$in": [None, ""]}},
                {"event_time": {"$exists": False}},
            ]},
            # Canonical-first grade filter (see comment block above).
            # Prefer `published_grade` (immutable Phase-1c snapshot).
            # Fall back to legacy `grade` only when the row has never
            # been snapshot-published (`published_grade` absent).
            {"$or": [
                {"published_grade": {"$exists": True, "$ne": "Pass"}},
                {"$and": [
                    {"published_grade": {"$exists": False}},
                    {"grade": {"$ne": "Pass"}},
                ]},
            ]},
        ],
        # Exclude special-tab markets (NRFI/YRFI lives in its own MLB
        # sub-tab — user explicitly asked to keep these off the main board).
        "hide_from_main_board": {"$ne": True},
        # 2026-07-16 — never show "Pass" grade picks on the main board.
        # Pass = pick failed lock-tier thresholds. User: "pass should not
        # make the board". Also filter no_bet globally so tennis-dropped
        # picks with stale shadow lock fields can't leak through.
        #
        # 2026-08-24 FALSE-GATE-BLOCKER FIX (MLB game markets):
        # The legacy `grade` field is being live-overwritten by the APEX
        # gate to "Pass" (with `apex_block_reason=magic_tier_not_aligned_
        # strong:INSUFFICIENT_EVIDENCE`) even when the canonical publication
        # snapshot graded the pick as Lock / Strong Lock / Elite Lock.
        # Proof: today's slate has 20 published MLB game-market picks
        # (ML / RL / Totals, pub_lock 90-93, publication_state=PUBLISHED)
        # with `published_grade="Lock"` but `grade="Pass"` — the current
        # filter dropped ALL of them (only 4 MLB picks survived where 61
        # were canonical-eligible). Fix: prefer the CANONICAL
        # `published_grade` (Phase 1c snapshot, immutable) with a legacy
        # fallback to `grade` for pre-canonical rows that never received
        # a snapshot. Preserves the "no Pass grade on board" intent
        # exactly — just against the authoritative field.
        # Legacy `"grade": {"$ne": "Pass"}` migrated below into the
        # $and clause so the compound predicate isn't overwritten by
        # the top-level $or on line ~1711.
        "no_bet": {"$ne": True},
        # 2026-07-21 — trapped picks (chalk_trap / longshot_trap) are
        # tagged off_board by board_visibility. Filter them out here so
        # they never surface on the main board even if a sub-query
        # would otherwise match (e.g. mlb_k_q would let a -400 alt K
        # through if the trap wasn't run, but with the trap running
        # they now get off_board=True and are hidden).
        "off_board": {"$ne": True},
        # Only show still-open picks — safety net so a stale pick_date
        # from 3 days ago can't resurrect a settled pick.
        "status": {"$in": ["pending", "open", None]},
        "$or": [standard_q, elite_q, model_only_q, tennis_ml_q, tennis_alt_q, tennis_extra_q, mlb_k_q, mlb_hitter_q, soccer_scorer_q, high_lock_bypass_q],
    }
    # ── P0-1 CANONICAL PUBLICATION GATE (2026-08-08) ────────────────────
    # Enforce the publication contract at the board read layer.  Per
    # PUBLICATION_CONTRACT.md, a prediction is user-board eligible only
    # after `PredictionPublicationService` has emitted an immutable
    # snapshot AND dual-written `published_*` fields onto `db.picks`.
    #
    # Historically `/picks/today` had NO filter on `publication_source`,
    # so ingest paths that write directly to `db.picks` (e.g.
    # soccer_hot_scorers, ufc_espn_ingest, espn_soccer_fixtures) or
    # any legacy row that pre-dates the publication service could leak
    # onto the board without ever passing the canonical write barrier.
    #
    # The gate is a single-key Mongo filter merged into the base query
    # (existence of `publication_source`).  It does NOT change ranking,
    # lock scores, Magic Tier, or any downstream logic — only which
    # rows are eligible to appear.  Presentation / enrichment fields
    # remain sourced from the same `db.picks` document (its `id` is
    # the stable identity that also lives as `prediction_id` on
    # `prediction_snapshots`).
    #
    # Emergency bypass: set the env var
    # `LOCKSCORE_REQUIRE_CANONICAL_PUBLICATION=false` (see
    # `services/canonical_board_source.py`).  Default is ON.
    try:
        from services.canonical_board_source import (
            canonical_publication_filter,
            is_canonical_publication_required,
        )
        _canon_filter = canonical_publication_filter()
        if _canon_filter:
            # Merge as a top-level required condition.  This coexists
            # with the existing `$or` of sub-queries above because
            # Mongo AND-s all top-level keys implicitly.
            q.update(_canon_filter)
        if is_canonical_publication_required():
            logger.debug("canonical publication gate: ENFORCED on /picks/today")
        else:
            logger.warning(
                "canonical publication gate: BYPASSED via %s env var — "
                "non-canonical picks may appear on the board.",
                "LOCKSCORE_REQUIRE_CANONICAL_PUBLICATION",
            )
    except Exception as _canon_err:
        # Phase B1 μ-closure (2026-06) — canonical gate MUST FAIL
        # CLOSED on error.  Previous behavior logged and continued,
        # exposing noncanonical picks on the Locks board.  We now
        # inject a filter that MATCHES ZERO documents so the board
        # is EMPTY rather than contaminated — degraded UX is strictly
        # preferable to a canonical-truth violation.
        logger.error(
            "canonical publication gate FAILED CLOSED due to error: %s",
            _canon_err,
        )
        q["__canonical_gate_error__"] = {"$exists": True}  # matches 0 docs
    # ── P0-2 GLOBAL LOCKS THRESHOLD ENFORCEMENT (2026-08-11) ──────────
    # Every sub-query above (elite_q, model_only_q, tennis_ml_q,
    # tennis_alt_q, tennis_extra_q, mlb_k_q, mlb_hitter_q,
    # soccer_scorer_q, high_lock_bypass_q, standard_q with its
    # chalk_verified / espn_fallback bypasses) declares its own lock
    # floor tuned to the historical chalk-pricing of that market
    # slice.  Several of those internal floors are below 85 (75, 70,
    # etc.) — they were originally intended to surface chalk-priced
    # sharps that the strict >85 board would otherwise hide.
    #
    # The main Locks board must NEVER fall below `>85`.  We AND a
    # global canonical predicate over the union so that regardless of
    # which sub-query a pick matches, its FINAL Lock Score must clear
    # the `>85` contract.  Filtered / bypass-carve-out surfaces don't
    # get to lower the threshold; they only refine WHICH high-lock
    # picks appear.
    #
    # ``main_board_lock_score_query`` prefers ``published_lock_score``
    # over legacy shadow fields (Phase 1 Final Closure canonical-
    # source guarantee) and clamps ``min_lock`` values ≤ 85 up to
    # the base ``>85`` contract.  A user-supplied ``min_lock > 85``
    # narrows further via ``$gte``.
    _global_lock_gate = main_board_lock_score_query(
        min_lock=(float(min_lock) if min_lock is not None
                  and float(min_lock) > MAIN_BOARD_LOCK_FLOOR_EXCLUSIVE
                  else None)
    )
    q["$and"] = (q.get("$and") or []) + [_global_lock_gate]

    # ── User-supplied min_lock floor — NARROW ONLY ─────────────────
    # The global gate above already applies ``>85`` (or ``>=min_lock``
    # when user asked for a strictly higher floor).  This block is
    # therefore a **NO-OP for values ≤ 85** — the user cannot lower
    # the board below the base contract.  We keep it here only to
    # preserve any legacy call-site that expects an explicit
    # min_lock clause in the query trace.
    #
    # P0-2 bug fix (2026-08-11): the previous ``q["$and"] = [...]``
    # assignment OVERWROTE the existing ``$and`` (date + 72h horizon
    # window), silently dropping the horizon guard.  Now APPEND
    # via ``(q.get("$and") or []) + [...]`` so every previously
    # merged clause survives.
    if min_lock is not None and float(min_lock) > MAIN_BOARD_LOCK_FLOOR_EXCLUSIVE:
        user_floor = float(min_lock)
        q["$and"] = (q.get("$and") or []) + [{"$or": [
            {"lock_score":     {"$gte": user_floor}},
            {"lock_score_v2":  {"$gte": user_floor}},
        ]}]
    if sport_list:
        # Multi-select sports: accept ANY of the provided values, but only
        # if the caller isn't asking for the wildcard "All". Filtering to
        # ["All"] is a no-op (legacy behaviour).
        scoped = [s for s in sport_list if s and s.lower() != "all"]
        if len(scoped) == 1:
            q["sport"] = scoped[0]
        elif len(scoped) > 1:
            q["sport"] = {"$in": scoped}
    if grade:
        q["grade"] = grade
    lt = (line_type or "").lower()
    if lt == "main":
        q["is_alt"] = {"$ne": True}
    elif lt == "alt":
        q["is_alt"] = True
    if min_implied is not None or max_implied is not None:
        imp_q: dict = {}
        if min_implied is not None:
            imp_q["$gte"] = float(min_implied)
        if max_implied is not None:
            imp_q["$lte"] = float(max_implied)
        q["implied_probability"] = imp_q
    # ── Signal Score floor (2026-07-17) ─────────────────────────────
    # User request: filter by signal_score so picks below the calibrated
    # signal threshold are hidden. Signal Score is the composite
    # evidence weight (form/matchup/volume/injury/market/value) that
    # underpins Lock Score. Users want to surface picks where the
    # underlying signal is strong even if lock happens to be mid-band.
    #
    # ── Signal filter fix (2026-07-21) ───────────────────────────────
    # CRITICAL: The LockPickCard displays `signal_score_raw` (the
    # absolute 0-100 conviction from services/signal_engine/engine.py)
    # NOT `signal_score` (the slate-wide percentile rank). Filter MUST
    # match what the card shows or the "80+" filter looks broken:
    # user sees "75" on a card in an "80+" filtered view. Query only
    # on signal_score_raw; fall back to signal_score only when raw is
    # missing (picks awaiting decoration).
    if min_signal is not None and float(min_signal) > 0:
        _min_sig = float(min_signal)
        raw_clause = {"signal_score_raw": {"$gte": _min_sig}}
        # Fallback for picks where signal_score_raw hasn't been written
        # yet (mid-refresh) — use the percentile as a proxy so they
        # don't drop off the board silently.
        fallback_clause = {
            "signal_score_raw": {"$in": [None]},
            "signal_score": {"$gte": _min_sig},
        }
        if _min_sig <= 50.0:
            # At low thresholds also allow "no signal yet" picks through
            # so a fresh slate doesn't render blank while enrichment
            # catches up.
            q["$and"] = (q.get("$and") or []) + [{
                "$or": [
                    raw_clause,
                    fallback_clause,
                    {"signal_score_raw": {"$exists": False}, "signal_score": {"$exists": False}},
                ],
            }]
        else:
            # High threshold — must have a real raw score meeting the
            # floor. Percentile-only picks are the fallback fringe.
            q["$and"] = (q.get("$and") or []) + [{
                "$or": [raw_clause, fallback_clause],
            }]
    # Market family filter — uses the same labelling we use in analytics so
    # the same token works on every sport (e.g. "moneyline", "spread",
    # "game_total", "btts", "1x2", "goalscorer", "player_points", etc.).
    if market_list:
        # Multi-select markets: combine each market token's regex with
        # an OR. Falls back to the legacy single `_market_regex` path if
        # only one market is supplied.
        regexes = [r for r in (_market_regex(m) for m in market_list) if r]
        if len(regexes) == 1:
            q["market"] = {"$regex": regexes[0], "$options": "i"}
        elif len(regexes) > 1:
            q["market"] = {"$regex": "|".join(f"(?:{r})" for r in regexes), "$options": "i"}
    if league_list:
        # SEC-004: re.escape user input so metacharacters can't trigger
        # catastrophic regex backtracking (ReDoS) against MongoDB.
        # Multi-select: OR-regex across all chosen leagues.
        league_patterns = [re.escape(str(lg)) for lg in league_list]
        if len(league_patterns) == 1:
            q["league"] = {"$regex": league_patterns[0], "$options": "i"}
        elif len(league_patterns) > 1:
            q["league"] = {"$regex": "|".join(league_patterns), "$options": "i"}
    # Multi-game filter — game_ids / events come from the new UI's
    # multi-game selection on the event-grouped view.
    if game_id_list:
        # `event_id` on picks is the canonical join key (sportsbook event).
        q["$and"] = (q.get("$and") or []) + [{
            "$or": [
                {"event_id": {"$in": game_id_list}},
                {"game_id": {"$in": game_id_list}},
            ],
        }]
    if event_list:
        # Display-string fallback (e.g. "Yankees @ Red Sox"). Useful when
        # the upstream feed didn't include a stable event_id.
        q["$and"] = (q.get("$and") or []) + [{"event": {"$in": event_list}}]
    # Free-text search across player name, event, market label. Case-
    # insensitive, regex-escaped to defang ReDoS.
    if search:
        s_pat = re.escape(str(search).strip())
        if s_pat:
            q["$and"] = (q.get("$and") or []) + [{
                "$or": [
                    {"player_name": {"$regex": s_pat, "$options": "i"}},
                    {"event":       {"$regex": s_pat, "$options": "i"}},
                    {"market":      {"$regex": s_pat, "$options": "i"}},
                    {"selection":   {"$regex": s_pat, "$options": "i"}},
                ],
            }]
    # ── Sort by CANONICAL lock score, not the stale v1 ────────────────
    # Picks like Silva Felipe (v1=55 due to validator drift but v2=98)
    # were getting sorted to the BOTTOM of the result limit and
    # silently truncated off the home feed even though their canonical
    # API lock is 98. Sort by lock_score_v2 DESC, lock_score DESC
    # tiebreaker — v2 is updated every refresh cycle to the true
    # current score so this matches what the user will see in the UI.
    #
    # LIMIT raised to 2000 (2026-06-26, user: "bring the pick limit up"):
    # the daily slate hovers ~500-700 picks across all sports + tabs,
    # and per-sport tabs (Soccer = 200+) plus lower-tier league synth
    # picks need headroom or CSL/MLS/Veikkausliiga get truncated. 2000
    # is well above max-realistic and removes the silent-cut footgun.
    cursor = db.picks.find(q, {"_id": 0}).sort(
        [("lock_score_v2", -1), ("lock_score", -1)]
    ).limit(2000)
    # length=2000 matches the cursor.limit() above. Earlier this was
    # length=200 which silently capped the result list even when the
    # cursor was configured for 2000 — that's the actual reason CSL /
    # MLS / Veikkausliiga synth scorers were missing from the home
    # feed (2026-06-26).
    picks = await cursor.to_list(length=2000)
    # Hide picks for games that have already started (see _filter_in_play_window).
    picks = _filter_in_play_window(picks)
    # ── Quality Gate (2026-06-29) ───────────────────────────────────────
    # Backtest over 1,499 graded picks revealed three categories
    # dragging headline win % from ~72% down to 47.2%:
    #   • Soccer goalscorers (anytime/first/last) — 4.8% win
    #   • Lock-score band 65-74 — 12.8% win (calibration is INVERTED;
    #     the 50-64 band is at 59.9%)
    #   • MLB Moneyline / NRFI / YRFI — sub-50% historical
    # Filtering them at the read layer is the cheap "stop the bleeding"
    # patch; the underlying generation models will be recalibrated in a
    # later pass. Logged counts surface in `quality_gate_blocked`.
    # UNIVERSAL_RUNTIME_AUTHORITY_CONSOLIDATION (2026-09) — the
    # quality gate at READ TIME is ENRICHMENT_ONLY.  Picks that
    # WOULD have been blocked receive `quality_gate_block_reason`
    # + `consumer_disposition="DISPLAY_HIDDEN_BY_QUALITY_GATE"` but
    # remain in the response.  Canonical eligibility was already
    # decided pre-publication by the ingester / BoardProjection.
    # Read endpoints project that truth; they do not re-model.
    try:
        from quality_gate import apply_quality_gate, validate_against_live_alt_lines
        picks, qg_blocked = apply_quality_gate(picks, enforce=False)
        if qg_blocked:
            import logging
            logging.getLogger("lockscore").info(
                "QualityGate blocked on /picks/today: %s", qg_blocked,
            )
        # Live alt-line validation (2026-06-30 user mandate — no synthetic
        # lines). Soft-fail if feed empty so we don't accidentally dump
        # the entire board during a feed outage.
        picks, alt_stats = await validate_against_live_alt_lines(picks, db)
        rejected_alt = sum(v for k, v in alt_stats.items()
                           if k in ("line_not_found", "market_removed",
                                    "stale_odds", "invalid_alt_mapping"))
        if rejected_alt:
            import logging
            logging.getLogger("lockscore").info(
                "AltLineValidator on /picks/today: %s", alt_stats,
            )
    except Exception as qg_err:
        import logging
        logging.getLogger("lockscore").warning("QualityGate skipped: %s", qg_err)
    # ── Cross-pipeline GAME OUTCOME dedupe ───────────────────────────────
    # The main pipeline (sports_engine.py) and the soccer pipeline
    # (soccer/predictor.py) BOTH write into `picks`. They can produce
    # picks on opposite sides of the same 3-way h2h market — e.g. main
    # pipeline writes "Sweden Moneyline" while soccer pipeline writes
    # "Netherlands Win or Draw" for the same game. Those bets are
    # MUTUALLY EXCLUSIVE (if Sweden wins, NL W-or-D loses) and showing
    # both makes the app look broken. Collapse to the single highest-
    # confidence side per game, preferring Win-or-Draw / Double Chance
    # over straight Moneyline (draw safety net = lower variance).
    # ── SOCCER_REGRESSION_RUNTIME §4 — cross-book consumer dedupe ──
    # Collapse same (event, market, selection, line) across multiple
    # sportsbooks into a single consumer card BEFORE we deduplicate
    # mutually-exclusive game outcomes.  This ensures the board
    # represents unique betting opportunities, not sportsbook copies.
    picks = _collapse_cross_book_duplicates(picks)
    picks = _dedupe_game_outcome_picks(picks)
    # ── PHASE 0 §9-§11 (2026-06) — Board Utility Layer ─────────────
    # Extreme-juice utility + alt-line ladder collapse.  Both are
    # READ-TIME projections that tag picks with an explicit
    # ``consumer_disposition`` (EXTREME_JUICE / DISPLAY_LADDER_
    # SUPERSEDED) and set ``hide_from_main_board=True``.  Canonical
    # eligibility is UNCHANGED so Parlay 2.0 can still use these
    # picks as legs.
    try:
        from services.board_utility_layer import apply_board_utility_layer
        _bul_stats = apply_board_utility_layer(picks)
        if _bul_stats.get("picks_hidden_total"):
            logger.info("BoardUtilityLayer: %s", _bul_stats)
    except Exception as _bul_err:
        logger.warning("BoardUtilityLayer skipped: %s", _bul_err)
    # Phase C4 μ-closure (2026-06) — restore multi-scorer eligibility.
    # Prior code capped goalscorer picks at ``top_n=1`` per event,
    # silently removing legitimate secondary scorers who cleared the
    # canonical eligibility gates (real-line + settlement-supported +
    # ≥85 Lock).  Confirmed defect: audit found real qualified
    # goalscorer candidates hidden despite passing every canonical
    # check.  We now surface up to 3 unique scorer candidates per
    # event — dedupe by canonical identity still applied downstream.
    picks = _dedupe_goalscorer_per_event(picks, top_n=3)
    # ── Goalscorer Matchup Engine v3 (2026-06-30) ──────────────────
    # Matchup-first ranking layer on top of the curated/synth/book-derived
    # goalscorer picks. Applies the user-mandated weights
    # (35% matchup / 30% opportunity / 20% form / 15% historical),
    # confidence penalties (bench / minutes / market disagreement /
    # missing data), national-team squad gate (Toney filtered for
    # England if not in current 26-man squad), and explainability fields
    # (`matchup_score`, `why_this_pick`, `starter_probability`, etc.).
    #
    # Picks with low confidence / not in announced squad are DROPPED
    # at this stage — eliminates the "random scorer names" the user
    # reported (Gyökeres/Isak/Toney appearing for fixtures they aren't
    # in). Elite-protected picks (curated CSL synth seeds, etc.) bypass
    # the drop guard via the `elite_protect` flag.
    # UNIVERSAL_RUNTIME_AUTHORITY_CONSOLIDATION (2026-09) — matchup
    # is ENRICHMENT_ONLY at read time.  The annotator still runs so
    # every pick receives matchup_score / matchup_grade / why_this_
    # pick / starter_probability fields, but it MUST NOT silently
    # veto canonically-eligible picks.  The old `apply_drop=True`
    # was the primary read-time canonical-eligibility mutator — it
    # made 3 upstream-qualified anytime-scorer picks disappear from
    # /api/picks/today with no visible reason.  A single canonical
    # eligibility decision now lives pre-publication (real-line
    # ingester + BoardProjectionService); read endpoints project
    # that truth, they do not re-decide it.
    #
    # Picks whose matchup engine still recommends drop are tagged
    # with `matchup_recommends_drop=True` + `consumer_disposition`
    # so an explicit product-specific selection layer (opt-in) can
    # act on it — but the default consumer response no longer
    # silently removes them.
    try:
        from goalscorer_matchup import annotate_picks_async
        from deps import db as _matchup_db  # async motor handle
        picks = await annotate_picks_async(
            picks, _matchup_db, apply_drop=False,
        )
    except Exception as me:
        import logging
        logging.getLogger("lockscore").warning(
            "Goalscorer matchup engine skipped (continuing): %s", me
        )
    # ── Canonicalize lock_score (V2 → primary) BEFORE sorting ──────────
    # Without this, the sort uses the legacy V1 lock_score baked at pick
    # creation time. But `_canonicalize_lock_score` (called at the very
    # end) promotes lock_score_v2 to the displayed lock_score for ~25%
    # of picks — so by the time the user sees them, they're labelled
    # with HIGHER lock_scores than their position implies. The result:
    # an MLB pick at displayed lock 93.8 ends up below a Soccer pick at
    # displayed lock 92.5 — because the SORT keyed on the pick's stale
    # V1 score of e.g. 80, not its displayed-V2 of 93.8. 63/124 sort
    # inversions in the wild traced back to this exact ordering bug.
    picks = _canonicalize_picks(picks)

    # ── Lazy Evidence Governance ── (Phase 1, 2026-06-24)
    # Apply the Universal Evidence System to any pick missing an
    # `evidence_score` — typically picks generated before the engine
    # shipped. ONLY governs PENDING picks (we never re-write history
    # by adjusting a settled pick's lock score post-hoc).
    #
    # CARVE-OUT (2026-06-26): same skip list as the validator carve-out
    # — elite players, sim-anchored picks, model-only SportDB scorers
    # have their lock_score determined by a non-evidence anchor (career
    # history tier, 20K-run sim consensus, or rep-based floor). The
    # evidence governor's multiplier would silently demote these picks
    # on every request, undoing the anchor each refresh. Skip them.
    try:
        from evidence_engine import build_features_from_pick, govern_pick
        _gov_count = 0
        for _p in picks:
            if _p.get("evidence_score") is not None:
                continue
            if (_p.get("status") or "pending") != "pending":
                continue
            if (
                _p.get("elite_player")
                or _p.get("lock_anchored_to_sim")
                or _p.get("is_model_only")
                or _p.get("is_synthetic_scorer")
                or (_p.get("source") or "").startswith("sportdb_scorer")
                # ── SOCCER_UNIVERSAL_RUNTIME (2026-08-15) ────────────
                # Real-line ingest sources ship their own
                # `evidence_score` derived from the actual bridge/
                # game-model factor stack (see
                # services/real_line_scorer_ingest.py).  We ONLY skip
                # the governor when the pick already carries an
                # explicit `evidence_score` — the governor then
                # trusts the ingester's assessment rather than
                # re-deriving it from a stale feature snapshot.
                # If a real-line pick ever lands WITHOUT
                # `evidence_score`, the governor runs normally.
                or (_p.get("source") in (
                    "real_line_alt_scorer_v1",
                    "real_line_soccer_v2",
                ) and _p.get("evidence_score") is not None)
            ):
                continue
            try:
                govern_pick(_p, build_features_from_pick(_p))
                _gov_count += 1
            except Exception:
                pass
        if _gov_count:
            logger.debug("Lazy evidence governance applied to %d picks", _gov_count)
    except Exception as _lazy_ev_err:
        logger.warning("Lazy evidence governance failed (continuing): %s", _lazy_ev_err)

    # Re-canonicalize AFTER governance so any v2 promotion that the
    # governance step might have stale-overwritten is restored.
    picks = _canonicalize_picks(picks)

    if day_offset is not None:
        target_day = (datetime.now(timezone.utc).date() + timedelta(days=day_offset)).isoformat()
        picks = [p for p in picks if (p.get("event_time") or "").startswith(target_day)]
    else:
        # Default ordering: today's games first (kickoff within 24h), then later
        # games — within each bucket, sorted by lock_score desc. Keeps the
        # "best bet for the day" front-and-center even if a 2-day-out game
        # has a higher base lock_score.
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(hours=24)

        def _bucket(p: dict) -> int:
            dt = _parse_event_dt(p.get("event_time") or "")
            if dt is None:
                return 1
            return 0 if now <= dt <= cutoff else 1

        # Apply the user's sort preference. Default "lock": highest lock_score
        # first. "time": soonest kickoff first. "edge": biggest model edge
        # first. Direction (asc/desc) flips numerical sorts so the user can
        # find weakest picks too without having to scroll all the way down.
        s = (sort or "lock").lower()
        asc = (direction or "desc").lower() == "asc"
        # Multiplier for numerical sort fields: -1 = desc (highest first),
        # +1 = asc (lowest first). Time sort uses its own direction logic.
        m = 1 if asc else -1
        def _event_dt(p: dict) -> datetime:
            dt = _parse_event_dt(p.get("event_time") or "")
            if dt is None:
                return datetime.max.replace(tzinfo=timezone.utc)
            return dt
        # Elite-player anchor: float elite picks to the top within their
        # bucket — but ONLY for the default lock-desc view. When the user
        # has explicitly asked for asc / win / edge / time, respect their
        # chosen ordering without re-shuffling Mbappé/Haaland/Messi/Kane
        # to the top.
        def _elite_rank(p: dict) -> int:
            return 0 if p.get("elite_player") else 1
        # ── Soccer market-family tiebreaker (iter-93 fix) ───────
        # Applied inside EVERY sort mode as the FINAL tiebreaker so
        # goal-scorer / involvement / score-or-assist picks beat pure
        # assist picks on ties. iter-92 only patched sort=lock; this
        # fixes the frontend default sort=time where lock is only a
        # secondary key — previously higher-lock scorer picks got
        # buried under lower-lock assist picks whenever alphabetical
        # fallback kicked in.
        def _soccer_family_rank(p: dict) -> int:
            if (p.get("sport") or "") != "Soccer":
                return 0
            mm = (p.get("market") or "")
            if "Anytime Goal Scorer" in mm:                     return 0
            if "Goal Involvement" in mm:                        return 1
            if "To Score or Assist" in mm or "Score or Assist" in mm: return 1
            if "First Goal Scorer" in mm or "Last Goal Scorer" in mm: return 2
            if "Anytime Assist" in mm:                          return 3
            return 4

        if s == "time":
            # Pure chronological — earliest kickoff first by default;
            # latest first when asc=False reversed (we treat time asc as
            # earliest→latest, which is the natural meaning, so flip
            # signature only when direction explicitly says desc).
            # Default 'time' direction is "soonest first" which is asc by
            # natural time ordering — keep that as the default.
            if asc:
                picks.sort(key=lambda p: (_event_dt(p), -p.get("lock_score", 0), _soccer_family_rank(p)))
            else:
                # iter-94 fix: `reverse=True` would flip EVERY key
                # including the family tiebreaker (rank 4 first, rank 0
                # last). Negate the primary time key instead so only
                # the kickoff direction reverses; -lock and family_rank
                # stay in their natural (higher-first / rank-0-first)
                # order. Convert dt → timestamp so we can sign it.
                def _neg_dt(p: dict) -> float:
                    dt = _event_dt(p)
                    try:
                        return -dt.timestamp()
                    except Exception:
                        return 0.0
                picks.sort(key=lambda p: (_neg_dt(p), -p.get("lock_score", 0), _soccer_family_rank(p)))
        elif s == "edge":
            # Pure edge sort — no today-first bucket so highest edges
            # always at top regardless of date.
            picks.sort(key=lambda p: (m * p.get("edge_percent", 0), -p.get("lock_score", 0), _soccer_family_rank(p)))
        elif s == "win":
            # Win % sort — model win_probability highest first by default.
            picks.sort(key=lambda p: (m * p.get("win_probability", 0), -p.get("lock_score", 0), _soccer_family_rank(p)))
        elif s == "implied":
            picks.sort(key=lambda p: (m * p.get("implied_probability", 0), -p.get("lock_score", 0), _soccer_family_rank(p)))
        else:  # "lock" (default)
            # Pure lock_score sort with the shared Soccer family tiebreaker.
            if asc:
                picks.sort(key=lambda p: (p.get("lock_score", 0), _soccer_family_rank(p)))
            else:
                picks.sort(key=lambda p: (-p.get("lock_score", 0), _soccer_family_rank(p)))
    picks = await _decorate_with_player_form(picks)
    picks = await _decorate_with_understat_form(picks)
    picks = await _decorate_with_espn_meta(picks)
    # ── Signal Engine (Phase A, 2026-07-12) ─────────────────────────
    # Six universal signals (form/matchup/volume/injury/market/value)
    # combined into a 0-100 Signal Score + signal-driven why bullets.
    # Runs AFTER espn meta so injury/form/record items are available.
    # Persists to db.picks so the Rollover ranker reads signal_score.
    try:
        from services.signal_engine import decorate_signals_bulk
        picks = await decorate_signals_bulk(db, picks, persist=True)
    except Exception as _sig_err:
        logger.warning("Signal Engine decoration skipped: %s", _sig_err)
    # ── Real-streak override (2026-06-30) ───────────────────────────────
    # The legacy `_decorate_with_player_form` reads `current_streak` from
    # `player_profiles_v2`, which was calculated from PICK win/loss
    # history. That store was poisoned by earlier grading bugs
    # (DNP = LOSS, header goals missed, lottery FGS picks) so elite
    # scorers like Mbappé, Haaland, Kane were getting tagged "COLD · 15L"
    # despite scoring live in the real world. This step replaces the
    # streak (and any stale understat label) with values derived from
    # REAL match data in `soccer_player_form` / `auto_elite_players`,
    # or hides the chip entirely when no real data exists.
    try:
        from scorer_streak import enrich_picks_with_real_streaks  # lazy
        _streak_stats = await enrich_picks_with_real_streaks(picks, db)
        if _streak_stats.get("real_data_applied") or _streak_stats.get("hidden_no_data"):
            logger.info("Real-streak override on /picks/today: %s", _streak_stats)
    except Exception as _se:
        logger.warning("Real-streak enrichment skipped on /picks/today: %s", _se)
    canonical = _canonicalize_picks(picks)
    # ── Per-sport cap of 100 (2026-07-26, μ-closed 2026-06 Phase C3) ─
    # Mobile home + soccer tabs were timing out because Soccer alone
    # returned 400+ picks in a ~2MB response. Cap each sport to the
    # TOP 100 by lock_score so the wire payload stays under ~500KB.
    #
    # ── Phase C3 μ-closure (2026-06) — Lock 85+ safety valve ────────
    # Prior safety-valve floor of 90 silently dropped canonical
    # eligible Lock 85-89 records past position #100 per sport.  Now
    # anchored at 85 (the canonical main-board floor) so every
    # canonical-eligible pick remains reachable regardless of
    # per-sport position.  Only sub-85 (below main-board contract)
    # picks past position #100 are trimmed for payload safety.
    #
    # ── Phase B6 μ-closure (2026-06) — canonical Lock Score read ────
    # Prior code read ``lock_score_v2 or lock_score`` (V2-first) for
    # the cap-selection decision.  We now read
    # ``published_lock_score or lock_score`` so the canonical /
    # published score drives cap ordering; V2 (shadow) is never used
    # for authoritative selection.
    _PER_SPORT_CAP = 100
    _SAFETY_VALVE_LOCK = 85.0
    if canonical:
        _cap_counts: dict[str, int] = {}
        _cap_diag: dict[str, dict[str, int]] = {}
        _capped: list[dict] = []
        _dropped_by_cap = 0
        for p in canonical:
            sp = str(p.get("sport") or "").strip() or "Unknown"
            lk = float(p.get("published_lock_score") or p.get("lock_score") or 0)
            if _cap_counts.get(sp, 0) >= _PER_SPORT_CAP:
                # Safety-valve: keep if lock >= 85 (canonical floor).
                if lk >= _SAFETY_VALVE_LOCK:
                    _capped.append(p)
                    _cap_counts[sp] = _cap_counts.get(sp, 0) + 1
                    diag = _cap_diag.setdefault(sp, {"safety_valve_kept": 0,
                                                     "dropped_ge90": 0,
                                                     "dropped_85_89": 0,
                                                     "dropped_lt85": 0})
                    diag["safety_valve_kept"] = diag.get("safety_valve_kept", 0) + 1
                    continue
                _dropped_by_cap += 1
                diag = _cap_diag.setdefault(sp, {"safety_valve_kept": 0,
                                                 "dropped_ge90": 0,
                                                 "dropped_85_89": 0,
                                                 "dropped_lt85": 0})
                if   lk >= 90: diag["dropped_ge90"]  = diag.get("dropped_ge90", 0) + 1
                elif lk >= 85: diag["dropped_85_89"] = diag.get("dropped_85_89", 0) + 1
                else:          diag["dropped_lt85"]  = diag.get("dropped_lt85", 0) + 1
                continue
            _cap_counts[sp] = _cap_counts.get(sp, 0) + 1
            _capped.append(p)
        if _dropped_by_cap:
            logger.info(
                "picks_today per-sport cap: kept=%d dropped=%d counts=%s diag=%s",
                len(_capped), _dropped_by_cap, _cap_counts, _cap_diag,
            )
            # μ-closure C3 invariant: no Lock >= 85 pick may be dropped
            # by the per-sport cap.  If this ever triggers it's a bug
            # in ordering or the valve — WARN so we notice.
            _bad = sum((v.get("dropped_ge90", 0) + v.get("dropped_85_89", 0))
                        for v in _cap_diag.values())
            if _bad > 0:
                logger.warning(
                    "picks_today per-sport cap DROPPED %d Lock>=85 picks — "
                    "canonical eligibility violation. diag=%s", _bad, _cap_diag,
                )
        canonical = _capped
    # ── Team Total suppression (2026-07-19) ───────────────────────────
    # User request: "get rid of team total it confuses me I just total
    # for the game to generate". Strip any legacy Team Total picks from
    # the response (already disabled at ingest time in
    # sports_engine._build_mlb_alt_picks). Left generation off + read-
    # time filter = clean board even during the cache-drain window.
    canonical = [
        p for p in canonical
        if "team total" not in (p.get("market") or "").lower()
    ]
    # SLICE 1.2B (2026-09-02) — the lite projection was previously
    # applied here, but downstream decorators (Statcast, umpire, MLB
    # usage, form) mutate picks IN PLACE and were leaking heavy blobs
    # (statcast_batter, ump_zone, stuff_plus, home_meta) back onto the
    # wire. The projection now runs at the FINAL step, right before
    # `return`, so any post-strip decoration is naturally discarded.

    # ── Phase 4E follow-up (2026-08-06) — final eligibility filter ─────
    # The DB-level `grade != "Pass"` filter runs BEFORE the response
    # pipeline applies:
    #   • espn_signal_engine.apply_signals (mutates lock_score)
    #   • learning_system_v2.apply_v2_to_picks (RE-DERIVES grade from
    #     the freshly-mutated lock_score at line 632)
    #   • services.magic_tier_policy.apply_magic_tier (Phase 4E.3 cap)
    #   • services.odds_provider.decorate_pick (soft-docks lock_score
    #     when the primary is degraded)
    # Any of these can DEMOTE a pick to "Pass" (or to `off_board`) AFTER
    # the DB gate. Without a post-processing filter, Pass/off_board
    # picks reach the visible Locks board — user report 2026-08-06:
    # "I personally observed Lock 40 and Lock 46 picks displayed on
    # the Locks board".
    #
    # This filter runs on the FULLY DECORATED payload and enforces the
    # same eligibility contract the DB query aims for — it never
    # changes lock scores, grades, or tiers; it only drops picks that
    # post-processing has already flagged as non-eligible.
    _pre_final = len(canonical)
    def _canonical_not_pass(p: dict) -> bool:
        """SLICE 1 (2026-08-26) — Final-response canonical authority.

        Mirror the DB gate at line ~1695: prefer immutable
        `published_grade` (Phase-1c canonical snapshot); fall back to
        legacy mutable `grade` ONLY when a row has never been
        snapshot-published. Fixes the mutable-grade demotion regression
        where APEX/scorer_gate/lifecycle can rewrite `grade` to Pass
        AFTER canonical publication, dropping legit published Locks
        from the final response even though the DB query included
        them via `published_grade`.
        """
        pg = p.get("published_grade")
        if pg is not None:            # canonically published — trust it
            return (str(pg).strip() != "Pass")
        return (p.get("grade") or "").strip() != "Pass"

    canonical = [
        p for p in canonical
        if _canonical_not_pass(p)
        and not p.get("off_board")
        and not p.get("hide_from_main_board")
        and not p.get("no_bet")
    ]
    _dropped_final = _pre_final - len(canonical)
    if _dropped_final:
        try:
            logger.info(
                "picks_today final-eligibility filter dropped %d/%d picks "
                "(Pass/off_board/hide/no_bet demotions applied by "
                "post-processing).", _dropped_final, _pre_final,
            )
        except Exception:
            pass

    # ── Defect A runtime wire (2026-09-02) — spread supersession ─
    # Live evidence showed `/api/picks/today` serving both Wake
    # Forest -24.5 AND Akron +24.5 as ACTIVE.  Root cause: the
    # provider-live projection path that produced these rows never
    # invoked `enforce_single_active_spread`.  Wire it here at the
    # response projection stage — this is the SAME canonical guard
    # used by `pick_refresh_orchestrator.py`, NOT a new competing
    # authority.
    #
    # Authority order respected:
    #   1. canonical publication revision/state — the guard reads
    #      `revision_state` and NEVER touches rows already marked
    #      SUPERSEDED_IN_RUN / off_board by upstream authority.
    #   2. explicit current/superseded state — losers get their
    #      revision_state stamped to SUPERSEDED_IN_RUN + off_board=True.
    #   3. canonical prediction authority — already applied above
    #      (edge_percent + model_probability come from `hydrate()`).
    #   4. deterministic tiebreak (edge → mp → lock → pick_id) —
    #      only fires among otherwise-equal canonical winners.
    try:
        from services.spread_truth_guard import enforce_single_active_spread
        _spread_stats = enforce_single_active_spread(canonical)
        # After the guard, re-filter losers just marked off_board.
        canonical = [p for p in canonical if not p.get("off_board")]
        if _spread_stats.get("superseded", 0) > 0:
            logger.info(
                "picks_today spread_truth_guard superseded=%d keys=%d",
                _spread_stats["superseded"],
                _spread_stats["keys_stamped"],
            )
    except Exception as _sge:
        logger.warning("picks_today spread guard fail-open: %s", _sge)

    # ── P0 FINAL (2026-06) — CANONICAL WAGER DEDUPE on response.
    # The /picks/today response was previously assembled from
    # `db.picks` via a Mongo union of sub-queries (elite / model_only /
    # tennis_ml / tennis_extra / mlb_k / soccer_scorer / high_lock /
    # standard), then decorated / capped / eligibility-filtered — but
    # NEVER passed through the canonical wager dedupe.  When multiple
    # producers store the SAME semantic wager under different display
    # names (e.g. "Tjen J." vs "Janice Tjen", "Vekic D." vs
    # "Donna Vekic") each row survived every filter above and both
    # cards rendered on the board.
    #
    # We collapse right here — after all filtering, before the final
    # payload is serialised — using ``services.board_projection_service
    # .dedupe_canonical`` (the same helper /picks/all uses via
    # ``BoardProjectionService.project``).  Contract:
    #   * Best-quality row wins the collapse (lock DESC → time earlier
    #     → deterministic id tie-break — see ``_sort_key_lock_desc``).
    #   * Sportsbook quotes / book_odds / win_probability are NEVER
    #     recomputed — the winning row is emitted verbatim.
    #   * Distinct (event, participant, market_family, side, line)
    #     tuples remain distinct.
    #   * Stale / legacy rows that never had a canonical_wager_key
    #     stamped are STILL collapsed via on-the-fly
    #     ``canonical_wager_identity`` — no blind DB deletion.
    try:
        from services.board_projection_service import dedupe_canonical
        _pre_dedupe = len(canonical)
        canonical = dedupe_canonical(canonical)
        _dropped_dedupe = _pre_dedupe - len(canonical)
        if _dropped_dedupe:
            logger.info(
                "picks_today canonical wager dedupe collapsed %d "
                "duplicate row(s) (kept %d canonical wagers).",
                _dropped_dedupe, len(canonical),
            )
    except Exception as _dd_err:
        logger.warning(
            "picks_today canonical wager dedupe skipped: %s", _dd_err,
        )

    # ── Alt-line availability diagnostic (2026-07-13) ─────────────────
    # When the client asks for `line_type=alt` and gets zero (or very
    # few) tennis picks back, the reason is almost always that The Odds
    # API doesn't publish alt lines for the ATP/WTA 250 tour that our
    # TennisExplorer scrape surfaces. Rather than force the UI to guess
    # from the empty picks list, surface an honest diagnostic block the
    # frontend can render as a friendly empty-state.
    lt_lower = (line_type or "").lower()
    alt_availability = None
    if lt_lower == "alt":
        # If the response is dominated by tennis_extra tournaments and
        # returned no alt-tagged picks, tell the client why.
        # (Response payload is always small — this is a cheap tag.)
        sport_lc = (sport or "").lower() if sport else ""
        if sport_lc == "tennis" and len(canonical) == 0:
            alt_availability = {
                "supported":    False,
                "reason":       "book_coverage_gap",
                "message":      (
                    "Alt-line pricing is only published by sportsbooks "
                    "for Grand Slams and Masters 1000 events. This "
                    "week's tour is running the ATP/WTA 250 circuit "
                    "(Umag, Bastad, Gstaad, Iasi WTA, Athens WTA, "
                    "Kitzbühel WTA) — which the book doesn't carry."
                ),
                "suggestion":   "Switch to MAIN for moneyline picks.",
            }
    # ── Board-visibility stamping (2026-07-13 permanent history fix) ──
    # Every pick actually returned to the user gets `on_main_board_at`
    # stamped fire-and-forget. The `/history` endpoint requires this
    # stamp (or a similar surface-timestamp like `on_rollover_at`) for
    # any pick whose `pick_date >= 2026-07-13`. This guarantees the
    # History tab shows ONLY picks the user actually saw on their
    # board — no more "60 Nordic picks I never had on my slate"
    # leaks from below-floor Total Goals / Win-or-Draw / etc.
    if canonical:
        pick_ids = [p.get("id") for p in canonical if p.get("id")]
        if pick_ids:
            try:
                stamp_iso = datetime.now(timezone.utc).isoformat()
                # setOnInsert-style: only set if not already set, so the
                # FIRST time a pick surfaces is preserved (not overwritten
                # by every subsequent refresh).
                await db.picks.update_many(
                    {"id": {"$in": pick_ids},
                     "on_main_board_at": {"$exists": False}},
                    {"$set": {"on_main_board_at": stamp_iso}},
                )
            except Exception as e:
                logger.debug("board-visibility stamping skipped: %s", e)

    # Phase 0.1 — Attach no-vig fair implied % on-read for any pick that
    # doesn't already carry it. Cheap (pure math, no IO) and idempotent,
    # so it's safe to run on every request. Ensures the value_signal
    # calculator can grade against fair-market implied %, not book %.
    try:
        from services.devig import devig_pick
        for _p in canonical:
            if _p.get("no_vig_implied_pct") is None:
                devig_pick(_p)
    except Exception as e:
        logger.debug("on-read devig pass failed: %s", e)

    # Phase 1.3 + 1.5 — MLB batting-order + pitcher fatigue enrichment.
    # Only runs if at least one MLB pick is present AND the pick doesn't
    # already carry lineup/fatigue markers. Uses a batched fetch so a
    # full slate of Yankees hitter Overs only pulls the lineup once.
    try:
        mlb_picks_missing_usage = [
            _p for _p in canonical
            if (_p.get("sport") or "").upper() == "MLB"
            and _p.get("lineup_posted") is None
            and _p.get("pitcher_fatigue_flag") is None
        ]
        if mlb_picks_missing_usage:
            from services.mlb_usage import enrich_picks_with_usage_bulk
            await asyncio.wait_for(
                enrich_picks_with_usage_bulk(mlb_picks_missing_usage), timeout=5.0)
    except (asyncio.TimeoutError, Exception) as e:
        logger.debug("on-read MLB usage enrichment failed/timeout: %s", e)

    # Phase 1.1 — On-read Statcast attach. Very cheap (single Mongo
    # find_one per distinct player) so we run unconditionally on MLB
    # picks. Falls through as no-op if the daily refresh hasn't
    # populated the cache yet.
    try:
        mlb_picks_missing_statcast = [
            _p for _p in canonical
            if (_p.get("sport") or "").upper() == "MLB"
            and _p.get("statcast_batter") is None
            and _p.get("statcast_pitcher") is None
        ]
        if mlb_picks_missing_statcast:
            from services.mlb_statcast import enrich_picks_with_statcast_bulk
            await asyncio.wait_for(
                enrich_picks_with_statcast_bulk(db, mlb_picks_missing_statcast), timeout=5.0)
    except (asyncio.TimeoutError, Exception) as e:
        logger.debug("on-read Statcast enrichment failed/timeout: %s", e)

    # Phase 1.4 — On-read umpire K-zone attach for pitcher K props.
    try:
        mlb_k_picks_missing_ump = [
            _p for _p in canonical
            if (_p.get("sport") or "").upper() == "MLB"
            and "strikeouts" in (_p.get("market") or "").lower()
            and _p.get("ump_zone") is None
        ]
        if mlb_k_picks_missing_ump:
            from services.mlb_umpire import enrich_picks_with_umpire_bulk
            await asyncio.wait_for(
                enrich_picks_with_umpire_bulk(db, mlb_k_picks_missing_ump), timeout=3.0)
    except (asyncio.TimeoutError, Exception) as e:
        logger.debug("on-read umpire enrichment failed/timeout: %s", e)

    # Phase 1.2 — On-read Stuff+/Location+/Pitching+ attach for pitcher
    # props (strikeouts, outs recorded, earned runs, hits allowed). Sourced
    # from Baseball Savant pitch-arsenal-stats and cached daily. Adds
    # ~1 DB read per unique pitcher on the slate.
    try:
        mlb_pitcher_picks = [
            _p for _p in canonical
            if (_p.get("sport") or "").upper() == "MLB"
            and _p.get("stuff_plus") is None
        ]
        if mlb_pitcher_picks:
            from services.mlb_stuff_plus import enrich_picks_with_stuff_plus_bulk
            await asyncio.wait_for(
                enrich_picks_with_stuff_plus_bulk(db, mlb_pitcher_picks), timeout=5.0)
    except (asyncio.TimeoutError, Exception) as e:
        logger.debug("on-read Stuff+ enrichment failed/timeout: %s", e)

    # Phase 4 — NFL nflverse usage attach for skill-position props
    # (WR / RB / TE / QB). Adds target share, snap %, WOPR, aDOT, YPRR.
    # Cheap (dedupe cache per unique player). No-op during pre-season
    # if no NFL picks are on the slate.
    try:
        nfl_picks_missing_usage = [
            _p for _p in canonical
            if (_p.get("sport") or "").upper() == "NFL"
            and _p.get("nfl_usage") is None
        ]
        if nfl_picks_missing_usage:
            from services.nfl_nflfastr import enrich_picks_with_nfl_usage_bulk
            await asyncio.wait_for(
                enrich_picks_with_nfl_usage_bulk(db, nfl_picks_missing_usage), timeout=5.0)
    except (asyncio.TimeoutError, Exception) as e:
        logger.debug("on-read NFL usage enrichment failed/timeout: %s", e)

    # Phase 3 — Tennis Sackmann/TML on-read attach. Adds surface-specific
    # rolling serve/return stats + career H2H for tennis moneyline / totals /
    # spread picks. Dedupes per-player lookup so a full slate of
    # `Alcaraz Moneyline` alt-lines only hits Mongo once.
    #
    # PERF FIX (2026-07-16): This block did 189 sequential Mongo reads
    # per /picks/today call (63 tennis picks × 2 players + h2h),
    # causing 5-6s response times. Moved to the Deep Dive detail
    # endpoint `/picks/{pick_id}` where users actually see this data.
    # The list view already carries `tennis_components` from
    # tennis_engine (surface_fit, serve_return, etc.) which is enough
    # for the card badge/lock score computation.
    pass

    # Phase 2b — Soccer recent-form attach (using multi-source cache
    # populated by services.soccer). Adds `soccer_form` dict with home/
    # away last-5 W/D/L, GF, GA, and position from cached standings.
    #
    # PRODUCTION HANG FIX 2026-07-15: the previous regex query pattern
    # `{"home_team": {"$regex": name, "$options": "i"}}` was UNANCHORED
    # and matched anywhere in the string, forcing MongoDB into a full
    # collection scan for every team lookup. On preview (25K matches,
    # SSD, no other load) this cost ~20ms/team. On production with a
    # larger backing collection + shared cluster resources the pattern
    # piled up to >100s cumulative and blew past the Cloudflare 100s
    # gateway timeout → `/api/picks/today` returned 504 while every
    # other endpoint stayed fast. Fix:
    #   1) Use an anchored ^-prefix regex so an index on `home_team` /
    #      `away_team` can serve prefix matches without a full scan.
    #   2) Wrap the whole block in asyncio.wait_for(...) with a hard
    #      3s cap so even a pathological slowdown can't stall the
    #      response — enrichment is BEST-EFFORT, missing form on a
    #      few picks is much better than a 504.
    # PERF FIX (2026-07-16): This block did N sequential Mongo reads
    # per soccer pick (one per team). On the mid-July slate that's
    # ~130 queries × ~30ms = 4s, blowing past the 3s timeout and
    # dropping soccer form for many picks. Batch query with $in
    # collapses it to ONE Mongo call regardless of team count.
    async def _enrich_soccer_form():
        soccer_picks = [p for p in canonical if (p.get("sport") or "").upper() == "SOCCER"
                        and p.get("soccer_form") is None]
        if not soccer_picks:
            return
        # Collect ALL unique team names first
        pick_teams: list[tuple[str, str]] = []
        team_names: set[str] = set()
        for p in soccer_picks:
            event = p.get("event") or ""
            if "@" not in event:
                continue
            away, home = [x.strip() for x in event.split("@", 1)]
            if home:
                team_names.add(home)
            if away:
                team_names.add(away)
            pick_teams.append((home, away))
        if not team_names:
            return
        # Build ONE anchored $regex $or query for all teams
        import re as _re
        or_clauses = []
        for name in team_names:
            esc = _re.escape(name)
            or_clauses.append({"home_team": {"$regex": f"^{esc}", "$options": "i"}})
            or_clauses.append({"away_team": {"$regex": f"^{esc}", "$options": "i"}})
        q = {"$or": or_clauses, "status": "finished"}
        # Get last 20 matches per team via one big query, sort desc, limit
        # generously so each team gets its ~10 recent matches. 20 teams
        # × 10 matches = 200 rows worst-case.
        matches = await db.soccer_matches.find(
            q, {"_id": 0, "home_team": 1, "away_team": 1,
                "home_score": 1, "away_score": 1, "date": 1},
        ).sort("date", -1).limit(min(len(team_names) * 10, 500)).to_list(length=500)
        # Group matches per team (up to 10 most recent each)
        recent_cache: dict[str, dict] = {}
        team_matches: dict[str, list] = {n: [] for n in team_names}
        for m in matches:
            ht = (m.get("home_team") or "").lower()
            at = (m.get("away_team") or "").lower()
            for name in team_names:
                low = name.lower()
                if low in ht or low in at:
                    if len(team_matches[name]) < 10:
                        team_matches[name].append(m)
        for name, name_matches in team_matches.items():
            wins = draws = losses = 0
            gf = ga = 0
            form_str = ""
            for m in name_matches:
                hn = (m.get("home_team") or "").lower()
                is_home = name.lower() in hn
                fs = m.get("home_score") if is_home else m.get("away_score")
                as_ = m.get("away_score") if is_home else m.get("home_score")
                if fs is None or as_ is None:
                    continue
                gf += fs
                ga += as_
                if fs > as_: wins += 1; form_str += "W"
                elif fs < as_: losses += 1; form_str += "L"
                else: draws += 1; form_str += "D"
            n = wins + draws + losses
            recent_cache[name] = {
                "n_matches": n,
                "wins": wins, "draws": draws, "losses": losses,
                "gf_avg": round(gf / n, 2) if n else None,
                "ga_avg": round(ga / n, 2) if n else None,
                "form": form_str[:5],
            }
        # Attach to picks
        for p, (home, away) in zip(soccer_picks, pick_teams):
            form_data = {}
            if home and home in recent_cache:
                form_data["home"] = recent_cache[home]
            if away and away in recent_cache:
                form_data["away"] = recent_cache[away]
            if form_data:
                p["soccer_form"] = form_data
    try:
        await asyncio.wait_for(_enrich_soccer_form(), timeout=3.0)
    except asyncio.TimeoutError:
        logger.warning("on-read Soccer form enrichment TIMED OUT — skipping to keep response fast")
    except Exception as e:
        logger.debug("on-read Soccer form enrichment failed: %s", e)

    # ── Cross-match dedupe (2026-07-16) ──────────────────────────────
    # For tennis moneyline picks, keep only ONE side of each match on
    # the board. Two opposing sides can't both be locks (mathematical
    # impossibility — the market's implied probs must sum to ~100%).
    # User report 2026-07-16: "How is this possible?" — same match's
    # Shcherbinina AND Lu J. both showed as LOCK 92. Deduping by
    # sorted(player_pair) + pick_date catches "A vs B" and "B vs A"
    # rows from separate ingest sources (Odds API + tennis_extra).
    try:
        def _tennis_match_key(pick):
            if (pick.get("sport") or "").lower() != "tennis":
                return None
            market = str(pick.get("market") or "").lower()
            if "moneyline" not in market:
                return None
            event = pick.get("event") or ""
            for sep in (" vs ", " vs. ", " @ "):
                if sep in event:
                    a, b = event.split(sep, 1)
                    pair = tuple(sorted([a.strip().lower(), b.strip().lower()]))
                    return (pick.get("pick_date"), pair)
            return None
        best_by_match: dict = {}
        for p in canonical:
            k = _tennis_match_key(p)
            if k is None:
                continue
            cur = best_by_match.get(k)
            if cur is None or (p.get("lock_score") or 0) > (cur.get("lock_score") or 0):
                best_by_match[k] = p
        # Filter canonical: keep non-tennis picks + only best side of each tennis match
        canonical = [
            p for p in canonical
            if _tennis_match_key(p) is None
            or best_by_match.get(_tennis_match_key(p)) is p
        ]
    except Exception as e:
        logger.debug("cross-match dedupe failed: %s", e)

    # ── Payload slimming (2026-07-16) ────────────────────────────────
    # /picks/today ships 3.2MB when heavy Deep Dive fields (sportsbook
    # mapping, signal engine, evidence breakdown, etc.) are inline for
    # 270 picks. Strip those from the list response so the mobile app
    # loads fast; Deep Dive fetches the full pick via /picks/{id}.
    #
    # Result on the mid-July slate: 3.2MB → ~700 KB (5x faster).
    _HEAVY_LIST_FIELDS = (
        "sportsbook_mapping",   # 626 KB across 271 picks
        "signal_engine",        # 390 KB
        "evidence_breakdown",   # 367 KB
        "snapshot",             # 194 KB
        "selection_v2",         # 109 KB
        "v2_reasons",           # 106 KB
        "sim_result",           # 96 KB
        "brain",                # 82 KB
        # NOTE (2026-07-21): `pick_rationale` was previously stripped
        # here but that broke the "Why this pick?" card toggle for
        # every pick that hit /picks/today. It's now slimmed inline
        # below via `_slim_rationale` — keeps the top summary/lean/
        # evidence bullet (140B avg) so the collapsed card renders
        # the toggle, while the deep blocks ride on /picks/{id}.
        "factors",              # 50 KB
        "learning",             # 42 KB
        "player_intel_full",    # variable, sometimes huge
        "sackmann_snapshot",    # tennis calibration payload
        "evidence_dropped_insights",
        "mlb_stuff_plus_snapshot",
        "nflfastr_snapshot",
        "soccer_head_to_head",
    )
    # Slim (not strip) `pick_rationale` so the card can render the
    # "Why this pick?" toggle while keeping the payload small.
    try:
        from server import _slim_rationale as _slim_pr
    except Exception:
        _slim_pr = None
    # ── H2H compact-summary attach (2026-02) ────────────────────────
    # Compute a one-liner H2H summary (see `services.h2h_enricher`) for
    # each returned pick. The full bundle stays behind `/picks/{id}/h2h`
    # so /picks/today doesn't balloon. Wrapped in try/except so a single
    # failure never blocks the response. Bounded to top-200 picks by the
    # canonical list size — beyond that the marginal UX value doesn't
    # justify the DB churn.
    try:
        from services.h2h_enricher import build_h2h_bundle
        _h2h_budget = 200
    except Exception:
        build_h2h_bundle = None  # type: ignore
        _h2h_budget = 0
    # ── First pass: heavy fields cleanup + why_this_pick fallback ───
    for _slim in canonical:
        for _f in _HEAVY_LIST_FIELDS:
            _slim.pop(_f, None)
        if _slim_pr and isinstance(_slim.get("pick_rationale"), dict):
            _slim["pick_rationale"] = _slim_pr(_slim["pick_rationale"])
        # ── Why this pick fallback (2026-07-17) ─────────────────────
        # Tennis picks & most non-soccer sports don't set why_this_pick
        # (only Soccer goalscorer + MLB HR pipelines do). Fall back to
        # the always-populated key_insights bullets so the "Why This
        # Pick" UI section renders for every sport.
        if not _slim.get("why_this_pick"):
            ki = _slim.get("key_insights") or []
            if ki:
                _slim["why_this_pick"] = ki[:6]

    # ── Second pass: parallel H2H attach ────────────────────────────
    # Cold-start /picks/today was 13s+ when batter H2H ran sequentially
    # (each MLB Stats API call ~200-800ms × ~30 batters). Fan out with a
    # semaphore-bounded asyncio.gather so we stay under the 20s frontend
    # timeout even on a cold cache.
    if build_h2h_bundle is not None and _h2h_budget > 0 and canonical:
        import asyncio as _asyncio
        _sem = _asyncio.Semaphore(8)

        async def _attach_h2h(slim: dict) -> None:
            async with _sem:
                try:
                    bundle = await build_h2h_bundle(db, slim, fast_mode=True)
                except Exception:
                    return
                summary = (bundle or {}).get("summary") or ""
                if summary:
                    slim["h2h_summary"] = summary
                    team_b = (bundle or {}).get("team_h2h") or {}
                    player_b = (bundle or {}).get("player_h2h") or {}
                    slim["h2h_compact"] = {
                        "record":   team_b.get("record"),
                        "meetings": team_b.get("meetings"),
                        "player_display": player_b.get("primary_value_display"),
                        "player_sample":  player_b.get("sample_size"),
                    }
                    # ── H2H → Why this pick bullet (iter-91) ────
                    # When H2H shows a meaningful tailwind/headwind
                    # (>=30 pts of BA vs season avg), surface it into
                    # the pick's reasoning bullets so the H2H data
                    # actually informs the user's read of the pick.
                    insight = (player_b or {}).get("h2h_insight")
                    if insight:
                        ki = slim.get("why_this_pick") or []
                        if isinstance(ki, list) and insight not in ki:
                            slim["why_this_pick"] = ki + [insight]
                    # Also expose the raw edge (bp = basis points of BA
                    # difference) so downstream sorting / calibration
                    # can consume it if we wire it into the model later.
                    edge_bp = (player_b or {}).get("h2h_edge_bp")
                    if edge_bp:
                        slim["h2h_edge_bp"] = edge_bp
        try:
            await _asyncio.wait_for(
                _asyncio.gather(*(_attach_h2h(p) for p in canonical[:_h2h_budget])),
                timeout=15.0,
            )
        except _asyncio.TimeoutError:
            # Never let H2H enrichment block the response — cold slate
            # may partially populate; subsequent calls hit the 6h cache.
            pass

    # ── Odds provider fallback decoration (iter-93) ─────────────────
    # Tag every outbound pick with odds_source / odds_status /
    # confidence_penalty so the frontend can label backup data and
    # the ROI math skips picks without real odds. Never generates
    # synthetic odds — when the primary Odds API is degraded, edge_
    # percent is nulled and lock_score is docked by 10.
    try:
        from services.odds_provider import status as _odds_status, decorate_pick as _odds_decorate
        _health = await _odds_status()
        # Only decorate when we're actually degraded — the live path
        # tags picks with odds_source=odds_api / status=live / penalty=0
        # (cheap: no-op mutation), degraded path applies the penalty.
        for _slim in canonical:
            # Skip v3 goal-scorer picks — they carry authoritative
            # `odds_source=model_derived` and null edge_percent set by
            # the v3 engine writer. The odds-fallback decorator would
            # incorrectly stamp them as `odds_api` and could recompute
            # a fabricated edge.
            if (_slim.get("source") == "goal_scorer_v3"
                    or _slim.get("odds_source") == "model_derived"):
                continue
            _odds_decorate(_slim)
        # Surface the odds-provider state on the envelope so the UI
        # (or an admin dashboard) can show a subtle indicator.
        _odds_envelope = {
            "state": _health.get("state"),
            "active_source": _health.get("active_source"),
        }
    except Exception as _odds_err:
        logger.debug("odds fallback decoration skipped: %s", _odds_err)
        _odds_envelope = None

    # ── PERKLOCKS UNIVERSAL LOCKS-ELIGIBILITY UNION RESCUE (2026-06) ───
    # Root Closure §1-§4: there is ONE canonical eligibility answer per
    # prediction, and once a pick clears the publication boundary NO
    # downstream consumer may re-qualify it away.  Even a single leaky
    # filter anywhere in the ~30-step response pipeline can strand
    # legit Lock ≥85 picks — repeatedly observed in the wild (Walker
    # Jenkins Over 0.5 Hits published_lock 91.5 dropped from
    # /api/picks/today while its Pick Breakdown card still rendered).
    #
    # This block enforces the invariant DECLARATIVELY at the response
    # boundary: after every filter/dedupe/decorator has run, we
    # re-query the CANONICAL ELIGIBLE UNIVERSE straight from db.picks
    # (`publication_state=PUBLISHED` ∧ `published_lock_score ≥ 85` ∧
    # `off_board != True` ∧ `no_bet != True` ∧
    # `hide_from_main_board != True` ∧ future event) and re-inject
    # any missing ids.  Each rescued row carries an explicit
    # `locks_eligibility` object so:
    #   * the frontend can render it identically to native rows,
    #   * ops can trace exactly why a pick is here,
    #   * a permanent regression test can assert
    #     `ELIGIBLE_BUT_MISSING == 0`.
    #
    # NEVER fabricates a pick (rescue-load is a straight DB read of
    # already-frozen predictions).  NEVER lowers Lock Scores.  NEVER
    # creates a second board — the union is over IDs on the SAME
    # published_lock_score+publication_state contract enforced upstream.
    try:
        from services.locks_eligibility import (
            compute_locks_eligibility, rescue_missing_eligible,
        )
        _served_ids = {p.get("id") for p in canonical if p.get("id")}
        _now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        # Root Closure §20 — UNIVERSAL rescue.  No horizon cap, no
        # sport whitelist, no market whitelist.  Every currently
        # published Lock ≥85 pick with a future event MUST reach
        # Locks regardless of days-out.  Legitimate 3-day-out CFB /
        # UEFA / NFL picks all included.
        _rescue_query = {
            "publication_state": "PUBLISHED",
            "published_lock_score": {"$gte": 85},
            "off_board":            {"$ne": True},
            "no_bet":               {"$ne": True},
            "hide_from_main_board": {"$ne": True},
            "excluded_from_history":{"$ne": True},
            "status":               {"$in": ["pending", "open", None]},
            "event_time":           {"$gt": _now_iso},
        }
        if sport and sport.lower() not in ("all", "any"):
            _rescue_query["sport"] = {"$regex": f"^{re.escape(sport)}$",
                                        "$options": "i"}
        rescued, ebm_ids, rescue_rejected = await rescue_missing_eligible(
            db, _served_ids, _rescue_query,
        )
        # Stamp canonical eligibility marker on EVERY row (served + rescued)
        # so consumers have ONE authoritative eligibility field, not four
        # loosely-related legacy fields.
        for _p in canonical:
            _p["locks_eligibility"] = compute_locks_eligibility(_p)
        for _r in rescued:
            _r["locks_eligibility"] = compute_locks_eligibility(_r)
            _r["locks_eligibility_rescued"] = True
        if rescued:
            logger.info(
                "picks_today eligibility-union rescue: injected %d "
                "canonically-eligible picks dropped by pipeline "
                "(EBM=%d, rescue_rejected=%s).  Root Closure invariant holds.",
                len(rescued), len(ebm_ids), rescue_rejected,
            )
            # SLICE 1.2B — rescued picks are extended into `canonical`
            # and pass through the final Lightweight Board DTO projection
            # at the return site (no double strip needed here).
            canonical.extend(rescued)
            # Root Closure Part-2 §6 — the merged set must be dedupe-clean
            # against the SAME canonical wager identity every downstream
            # consumer uses.  Prevents e.g. 18 rows of "Dynamo Dresden ML"
            # from different books re-appearing after rescue.
            try:
                from services.board_projection_service import dedupe_canonical
                before = len(canonical)
                canonical = dedupe_canonical(canonical)
                if before != len(canonical):
                    logger.info("picks_today post-rescue dedupe collapsed %d duplicate wagers",
                                 before - len(canonical))
            except Exception as _dd:
                logger.warning("post-rescue dedupe skipped: %s", _dd)
        elif rescue_rejected:
            logger.info(
                "picks_today eligibility-union rescue: no EBM rescued; "
                "rescue_rejected=%s (invalid rows correctly held back).",
                rescue_rejected,
            )
    except Exception as _ebm_err:
        logger.warning("Eligibility union rescue skipped: %s", _ebm_err)

    # SLICE 1.2B — Apply the Lightweight Board DTO projection AS THE
    # FINAL step, AFTER every on-read decoration (Statcast, umpire,
    # MLB usage, ESPN meta, form, rescue…). This guarantees that
    # mutating post-strip decorators (which re-attach heavy blobs like
    # statcast_batter, ump_zone, stuff_plus, etc.) can't leak them back
    # into the wire payload. The rescue path already applied its own
    # projection above, so a second pass on rescued rows is a no-op —
    # `_strip_for_lite` is idempotent on already-projected dicts.
    if lite:
        canonical = [_strip_for_lite(_p) for _p in canonical]

    return {"picks": canonical, "alt_availability": alt_availability,
             "odds_provider": _odds_envelope}


@router.get("/bet-killer", deprecated=True)
async def picks_bet_killer(user: Annotated[UserPublic, Depends(current_user)],
                           sport: Optional[str] = None):
    """DEPRECATED — Bet Killer was replaced by Under-of-the-Day.
    Returns an empty payload. Will be removed in a future release."""
    return {"picks": []}


# ─── /picks/parlay moved to routes/parlay_routes.py (2026-06-28) ─────
# Optimizer V1.1 was the largest endpoint left here (~335 lines). It
# lives standalone now; URL is unchanged (`/api/picks/parlay`). See
# routes/parlay_routes.py.



# ─── /picks/refresh (Phase 2β — DB-only for normal users) ───────────
# Historically this endpoint kicked off `_refresh_picks(today)` which
# fanned out ~250-400 credits of Odds API calls per invocation.  Any
# authenticated user could trigger it.  Phase 2β removes the paid
# generation capability from ordinary users:
#
#   • Response shape is UNCHANGED (frontend compatibility preserved).
#   • Zero paid API calls.  We simply return the current DB state.
#   • No _refresh_picks call.  No background generation.
#   • No emergency-reserve consumption.
#
# Admins retain a separate paid-refresh path via
# `POST /api/admin/picks/force-refresh`, which is guarded by
# JobCoordinator + ProviderBudget.  See routes/admin_routes.py.
@router.post("/refresh")
async def force_refresh(user: Annotated[UserPublic, Depends(current_user)]):
    """DB-only refresh — Phase 2β.  Returns the latest published picks
    from Mongo without triggering any paid third-party work.  The
    response envelope matches the pre-2β shape so the mobile client
    continues to update its state on tap.  Global paid refresh is now
    an admin-only operation gated by JobCoordinator + ProviderBudget.
    """
    from server import _today_str
    now = datetime.now(timezone.utc)
    today = _today_str()
    # Still enforce the 1h user-scoped rate limit — protects against
    # tap-mashing hitting Mongo unnecessarily, and preserves the
    # cooldown response fields the mobile client already handles.
    user_doc = await db.users.find_one({"id": user.id}, {"_id": 0, "last_refresh_at": 1})
    last_iso = (user_doc or {}).get("last_refresh_at")
    cd = _cooldown_payload(last_iso, now)
    existing = await db.picks.count_documents({"pick_date": today})
    if not cd["can_refresh"]:
        remaining_min = (cd["cooldown_seconds"] // 60) + (
            1 if cd["cooldown_seconds"] % 60 else 0)
        return {
            "refreshed": False,
            "rate_limited": True,
            "retry_after_minutes": remaining_min,
            "cooldown_seconds": cd["cooldown_seconds"],
            "next_refresh_at": cd["next_refresh_at"],
            "last_refresh_at": cd["last_refresh_at"],
            "count": existing,
            "date": today,
            "message": f"Refresh available in {remaining_min} min.",
        }
    await db.users.update_one(
        {"id": user.id},
        {"$set": {"last_refresh_at": now.isoformat()}},
    )
    # Audit-log the user-triggered call so ops can prove no paid work
    # was performed.  Best-effort; failures don't affect the response.
    try:
        from services.job_coordinator import JobCoordinator
        await JobCoordinator(db).audit(
            "user_refresh_db_only",
            caller=f"user:{user.id}",
            reason="picks_refresh_db_only",
            metadata={"date": today, "count": existing},
        )
    except Exception:
        pass
    next_dt = now + timedelta(seconds=REFRESH_COOLDOWN_SECONDS)
    # ── Phase C5 μ-closure (2026-06) — truthful refresh semantics ──
    # ``count`` is preserved for backward-compat clients that already
    # render it, but we now surface an explicit truthful breakdown so
    # the UI (or any consumer) can render "X existing records" vs
    # "X new picks generated" honestly.  This endpoint is DB-only —
    # it NEVER generates picks — so ``actually_generated`` is always 0.
    canonical_today = 0
    try:
        canonical_today = await db.picks.count_documents(
            {"pick_date": today,
             # Block 3A μ-closure: strong canonical publication predicate.
             "$or": [
                 {"publication_state": "PUBLISHED"},
                 {"publication_state": {"$exists": False},
                  "publication_source": {"$exists": True, "$ne": None}},
             ]}
        )
    except Exception:
        canonical_today = 0
    return {
        "refreshed": True,
        "queued": False,                   # no background job — Phase 2β
        "db_only": True,                   # explicit signal to any UI hook
        # Legacy field — total DB records for today (existing only).
        "count": existing,
        # ── B/C μ-closure — truthful refresh label decomposition ──
        "existing_records":     existing,
        "actually_generated":   0,          # DB-only endpoint never generates
        "canonical_published":  canonical_today,
        "date": today,
        "refresh_timestamp": now.isoformat(),
        "cooldown_seconds": REFRESH_COOLDOWN_SECONDS,
        "next_refresh_at": next_dt.isoformat(),
        "last_refresh_at": now.isoformat(),
        "note": "DB-only refresh — 0 new picks generated. Response "
                 "reflects the currently-published canonical slate. "
                 "New picks arrive on the scheduled snapshot cadence.",
    }


# ─────────────── /{pick_id} parameterized routes (RELOCATED) ───
# These MUST come AFTER every static segment in the router so
# FastAPI doesn't capture a literal segment ("today", "parlay",
# "refresh", "history", "settle", etc.) as a pick_id. Relocated
# to the bottom of the file 2026-06-27 during the Phase-3 extraction
# so the new /today, /bet-killer, /parlay, /refresh routes — appended
# above this block — actually resolve correctly. The original section
# header is preserved verbatim below for git-history continuity.
# These MUST come AFTER all static routes above so FastAPI doesn't
# capture a literal segment ("history", "settle", etc.) as an ID.

@router.get("/{pick_id}")
async def pick_detail(
    pick_id: str,
    user: Annotated[UserPublic, Depends(current_user)],
):
    """Pick detail with lazy evidence governance + canonicalization."""
    pick = await db.picks.find_one({"id": pick_id}, {"_id": 0})
    if not pick:
        raise HTTPException(status_code=404, detail="Pick not found")
    from server import _canonicalize_lock_score  # lazy
    # ── Quality-gate caps BEFORE canonicalize (2026-06-30 fix) ──────────
    # The detail endpoint must run the same cap pipeline as /picks/today,
    # otherwise the displayed Lock 60 on the home card "magically"
    # turns back into Lock 95 in the detail view. Sequence mirrors
    # `apply_quality_gate` in quality_gate.py:
    #   1. _apply_elite_scorer_anchor — sets anchor win_prob / edge
    #      for elite scorers on Anytime markets.
    #   2. _apply_display_cap — Anytime calibration cap (75) for non-
    #      elite picks; sets coherence_cap_ceiling.
    #   3. _apply_lockscore_coherence — neg-edge cap (60/70) +
    #      low-wp cap (75) + no-form-data cap (78); sets
    #      coherence_cap_ceiling.
    # After this, `_canonicalize_lock_score` will honour the ceiling
    # via its `min(max(...), ceiling)` clamp added in fix #2.
    try:
        from quality_gate import (
            _apply_display_cap, _apply_elite_scorer_anchor,
            _apply_lockscore_coherence,
        )
        _apply_elite_scorer_anchor(pick)
        _apply_display_cap(pick)
        _apply_lockscore_coherence(pick)
    except Exception as _qg_err:
        logger.debug("Quality-gate caps failed on /picks/{id}: %s", _qg_err)
    # Canonicalize lock_score → max(v1, v2) clamped by coherence ceiling
    # so detail view matches the home feed card. Single source of truth.
    pick = _canonicalize_lock_score(pick)
    # Lazy evidence governance — see /api/picks/today for context.
    # Phase-3 trigger (2026-06-25): also re-govern when the pick was
    # generated PRE-shrinkage (i.e. has no `win_probability_raw` yet).
    # Without this, existing DB picks never get the new shrinkage math
    # applied — they'd carry the un-shrunk model probability until
    # the next refresh cycle deletes and re-creates them.
    needs_govern = (
        (pick.get("evidence_score") is None and (pick.get("status") or "pending") == "pending")
        or (
            pick.get("win_probability") is not None
            and pick.get("win_probability_raw") is None
            and pick.get("implied_probability") is not None
        )
    )
    if needs_govern:
        try:
            from evidence_engine import build_features_from_pick, govern_pick
            govern_pick(pick, build_features_from_pick(pick))
            # govern_pick can DEMOTE lock_score / lock_score_v2 below
            # the raw/peak shadows (CSL synthetic goalscorers hit this
            # — they land at lock=99 raw/peak but govern_pick recomputes
            # ~77 against a missing book line). Re-canonicalize so the
            # detail view matches the home feed card again.
            pick = _canonicalize_lock_score(pick)
        except Exception as _ev_err:
            logger.debug("Evidence governance failed in detail view: %s", _ev_err)
    if not pick.get("explanation"):
        from ai_engine import _fallback_explanation
        # Every pick reaching the UI is a recommended pick — always
        # use the "why to BET" fallback. Legacy bet-killer warning path
        # retired.
        pick["explanation"] = _fallback_explanation(pick)
        pick["ai_pending"] = True
    else:
        pick["ai_pending"] = False
    # ── Real-streak override (2026-06-30) ───────────────────────────────
    # Same fix as /picks/today: replace the poisoned pick-history streak
    # on `player_form` with values derived from REAL match data (or
    # hide the chip when no real data exists). Wrapped as a 1-item list
    # so we can reuse the same enrichment helper.
    try:
        from scorer_streak import enrich_picks_with_real_streaks  # lazy
        await enrich_picks_with_real_streaks([pick], db)
    except Exception as _se:
        logger.debug("Real-streak enrichment failed on /picks/{id}: %s", _se)
    # ── Signal Engine (Phase A, 2026-07-12) ─────────────────────────
    # ESPN meta decoration first (injury/form/record items feed the
    # calculators AND makes the detail win_probability match the home
    # card, which also runs the espn signal layer), then compute the
    # 0-100 Signal Score + signal-driven why bullets.
    try:
        from server import _decorate_with_espn_meta  # lazy
        from services.signal_engine import decorate_signals_bulk
        await _decorate_with_espn_meta([pick])
        pick = _canonicalize_lock_score(pick)  # espn layer may move lock
        await decorate_signals_bulk(db, [pick], persist=True)
    except Exception as _sig_err:
        logger.debug("Signal Engine failed on /picks/{id}: %s", _sig_err)
    # ── Fusion Enrichment (2026-07-28, Phase-1 wire-up) ──────────────
    # Attach the "Why This Pick" fusion block. Uses only existing
    # engines (Trained ML, Similar Matchup, Player H2H, MC Simulator)
    # via the Prediction Fusion Engine. NEVER modifies lock_score,
    # win_probability, or simulator math. Falls back silently on any
    # engine hiccup. Persists to `fusion_predictions` for backtesting.
    try:
        from services.pick_fusion_decorator import enrich_pick_with_fusion
        await enrich_pick_with_fusion(db, pick, persist=True)
    except Exception as _fusion_err:
        logger.debug("Fusion enrichment failed on /picks/{id}: %s",
                     _fusion_err)
    # ── PERKLOCKS-MAIN 34 · STEP 1 ─────────────────────────────────────
    # Attach the immutable `published_pick_contract` so Pick Breakdown
    # (and every other downstream consumer) can read frozen canonical
    # wager truth from ONE place rather than re-parsing raw pick fields.
    # Adds ~350 B to the detail payload; carries `_provenance` so a
    # regression that lets a mutable alias outrank a canonical value
    # is immediately visible in the wire response.
    try:
        from services.published_pick_contract import PublishedPickContract
        _contract = PublishedPickContract.from_pick(pick)
        pick["published_pick_contract"] = _contract.as_dict()
        pick["published_pick_contract_provenance"] = _contract.provenance()
    except Exception as _c_err:
        logger.debug("PublishedPickContract attach failed: %s", _c_err)
    return pick


@router.post("/{pick_id}/ai-explain")
async def pick_ai_explain(
    pick_id: str,
    user: Annotated[UserPublic, Depends(current_user)],
    _throttle: None = Depends(_compute_throttle),
):
    """Generate (or fetch cached) Claude Sonnet 4.5 explanation for a pick.

    Frontend calls this after the initial pick_detail render so the
    spinner stays scoped to the AI box only.

    SEC-002 (2026-06-26): per-user `_compute_throttle` (30/min) prevents
    spamming the LLM with `?id=...&id=...` and draining EMERGENT_LLM_KEY
    budget. Cache fast-path still serves repeat calls cheaply.
    """
    pick = await db.picks.find_one({"id": pick_id}, {"_id": 0})
    if not pick:
        raise HTTPException(status_code=404, detail="Pick not found")
    # If we already cached a real AI explanation, scrub any stale lock-
    # score / win-probability NUMERIC references from it before
    # returning. The numbers can shift via the Evidence Governor and
    # read-time V2 canonicalization, so the live values come from the
    # response payload itself — never from cached narrative text
    # (iter-50 finding #2).
    cached = pick.get("explanation_ai")
    if cached:
        scrubbed = re.sub(
            r"\b(Lock(?:\s*Score)?|Win(?:\s*Probability)?|Edge)\s*[:=]?\s*"
            r"[\-+]?\d+(?:\.\d+)?\s*%?",
            "",
            cached,
            flags=re.IGNORECASE,
        )
        scrubbed = re.sub(r"\s{2,}", " ", scrubbed).strip(" |·,;-")
        return {"explanation": scrubbed or cached, "source": "cached"}
    # All picks reaching the UI are recommended picks (NO_BET filter
    # removed the bad ones). Always generate the "why to BET"
    # explanation.
    from ai_engine import explain_pick  # lazy
    text, real = await explain_pick(pick)
    if real:
        await db.picks.update_one(
            {"id": pick_id},
            {"$set": {"explanation": text, "explanation_ai": text}},
        )
    return {"explanation": text, "source": "live" if real else "fallback"}


@router.post("/{pick_id}/loss-analysis")
async def pick_loss_analysis(
    pick_id: str,
    user: Annotated[UserPublic, Depends(current_user)],
):
    """AI 'Why It Lost' breakdown for a losing pick."""
    pick = await db.picks.find_one({"id": pick_id}, {"_id": 0})
    if not pick:
        raise HTTPException(status_code=404, detail="Pick not found")
    if pick.get("status") != "lost":
        return {
            "analysis": "This pick wasn't recorded as a loss. No analysis available.",
            "source": "skip",
        }
    if pick.get("loss_analysis"):
        return {"analysis": pick["loss_analysis"], "source": "cached"}
    from ai_engine import analyze_loss  # lazy
    text, real = await analyze_loss(pick)
    if real:
        await db.picks.update_one(
            {"id": pick_id},
            {"$set": {"loss_analysis": text, "loss_analysis_ai": text}},
        )
    return {"analysis": text, "source": "live" if real else "fallback"}


@router.get("/{pick_id}/probability")
async def picks_probability(
    pick_id: str,
    user: Annotated[UserPublic, Depends(current_user)],
):
    """Unified Probability Engine breakdown for a single pick.

    Same source of truth as the inline `pick.probability` block
    attached to every pick by `_canonicalize_lock_score` — calling
    this endpoint is functionally identical to reading
    `/api/picks/today` and inspecting that pick's `probability` field.
    Provided as a standalone endpoint for clients that only want the
    breakdown without re-fetching the full pick payload.
    """
    pick = await db.picks.find_one({"id": pick_id}, {"_id": 0})
    if not pick:
        raise HTTPException(status_code=404, detail="Pick not found")
    from probability_engine import unified_probability_report  # lazy
    return unified_probability_report(pick)


@router.get("/{pick_id}/player-form")
async def pick_player_form(
    pick_id: str,
    user: Annotated[UserPublic, Depends(current_user)],
):
    """Soccer goalscorer-market player form panel.

    Surfaces the Understat-derived per-player season metrics (xG/90,
    npxG/90, goals over xG, shots/90 + form classification) for the
    player named in the goalscorer pick. Returns 404 cleanly if:
      - The pick isn't a soccer goalscorer market
      - The player isn't yet in the form DB (refresh hasn't run, or
        player isn't in the Top 5 European leagues)
    """
    pick = await db.picks.find_one({"id": pick_id}, {"_id": 0})
    if not pick:
        raise HTTPException(status_code=404, detail="Pick not found")
    from soccer_player_form import (  # lazy
        is_goalscorer_market, get_player_form, compute_form_lift,
    )
    if not is_goalscorer_market(pick):
        raise HTTPException(
            status_code=404,
            detail="Player form available for soccer goalscorer markets only",
        )
    # Extract player name from the market string (strips suffixes like
    # "Anytime Goal Scorer"). Goalscorer-pick `selection` is usually
    # "Yes" / "No" so we rely on the resolver as the primary source.
    try:
        from player_intel.resolver import extract_player_from_market
        player_name = extract_player_from_market(pick.get("market", "") or "") or ""
    except Exception:
        player_name = ""
    if not player_name:
        # Fallback chain — covers older picks where the player is in
        # `player`/`bet`/`selection` and the market string has no name.
        player_name = (
            pick.get("player") or pick.get("bet")
            or pick.get("selection") or ""
        ).strip()
    if not player_name or player_name.lower() in {"yes", "no", "over", "under"}:
        raise HTTPException(status_code=404, detail="Pick has no resolvable player name")
    form_doc = await get_player_form(db, player_name)
    if not form_doc:
        raise HTTPException(
            status_code=404,
            detail=f"No form data for {player_name} (not in Top 5 leagues, "
                   "or form refresh hasn't completed yet)",
        )
    # Strip Mongo ObjectId-equivalent fields and serialise datetimes
    updated_at = form_doc.get("updated_at")
    if hasattr(updated_at, "isoformat"):
        updated_at = updated_at.isoformat()
    return {
        "player_name":     form_doc.get("player_name"),
        "team":            form_doc.get("team"),
        "league":          form_doc.get("league"),
        "season":          form_doc.get("season"),
        "position":        form_doc.get("position"),
        "games":           form_doc.get("games"),
        "minutes":         form_doc.get("minutes"),
        "goals":           form_doc.get("goals"),
        "xg":              form_doc.get("xg"),
        "npxg":            form_doc.get("npxg"),
        "assists":         form_doc.get("assists"),
        "xa":              form_doc.get("xa"),
        "shots":           form_doc.get("shots"),
        "key_passes":      form_doc.get("key_passes"),
        "xg_per_90":       form_doc.get("xg_per_90"),
        "npxg_per_90":     form_doc.get("npxg_per_90"),
        "goals_per_90":    form_doc.get("goals_per_90"),
        "shots_per_90":    form_doc.get("shots_per_90"),
        "goals_over_xg":   form_doc.get("goals_over_xg"),
        "form_label":      form_doc.get("form_label"),
        "form_score":      form_doc.get("form_score"),
        "form_lift":       compute_form_lift(form_doc),
        "updated_at":      updated_at,
        "source":          form_doc.get("source") or "understat",
    }


@router.get("/{pick_id}/h2h")
async def pick_h2h(
    pick_id: str,
    user: Annotated[UserPublic, Depends(current_user)],
):
    """Unified head-to-head bundle for a single pick.

    Returns a normalised H2H shape (see `services.h2h_enricher.build_h2h_bundle`
    for the contract) that the deep-dive `/pick/[id]` screen renders.

    Works across MLB (pitcher-vs-team), Tennis (player-vs-player career),
    Soccer (player-vs-opponent hit rate), and team-level H2H for every
    sport that has final scores logged in our own settled picks history.
    """
    pick = await db.picks.find_one({"id": pick_id}, {"_id": 0})
    if not pick:
        raise HTTPException(status_code=404, detail="Pick not found")
    from services.h2h_enricher import build_h2h_bundle
    try:
        return await build_h2h_bundle(db, pick)
    except Exception as e:
        # Never 500 on an enrichment call — the deep-dive should still
        # render even if the H2H pipeline hits a transient hiccup.
        return {"ok": False, "error": str(e), "sport": pick.get("sport")}


@router.get("/{pick_id}/pitcher-h2h")
async def pick_pitcher_h2h(
    pick_id: str,
    user: Annotated[UserPublic, Depends(current_user)],
):
    """MLB strikeout pick → pitcher's historical K performance vs the
    opposing team. Returns season K avg, vs-team K avg, last 5 starts
    vs the opposing team (date, opp, K count, IP). Only resolves for
    MLB strikeout markets."""
    pick = await db.picks.find_one({"id": pick_id}, {"_id": 0})
    if not pick:
        raise HTTPException(status_code=404, detail="Pick not found")
    if (pick.get("sport") or "") != "MLB" or "strikeout" not in (pick.get("market") or "").lower():
        raise HTTPException(
            status_code=404,
            detail="Pitcher H2H available for MLB strikeout picks only",
        )
    market_str = pick.get("market") or ""
    m = re.match(r"^([A-Z][^()]+?)\s*\(", market_str)
    if not m:
        # Fallback to selection field which is just the pitcher's name
        sel = pick.get("selection") or ""
        if not sel:
            raise HTTPException(status_code=404, detail="Could not parse pitcher name")
        pitcher = sel.strip()
    else:
        pitcher = m.group(1).strip()
    # Opposing team — resolve via abbreviation in market parens
    event = pick.get("event") or ""
    pteam_m = re.search(r"\(([A-Z]{2,4})\)", market_str)
    pteam = pteam_m.group(1) if pteam_m else ""
    from mlb_pitcher_h2h import fetch_pitcher_h2h, resolve_opp_team_name  # lazy
    opp_team = resolve_opp_team_name(event, pteam) if pteam else None
    if not opp_team:
        # Last-resort: just default to the 2nd team in the event string
        parts = re.split(r"\s+(?:@|vs)\s+", event)
        opp_team = (parts[1].strip() if len(parts) == 2 else "").strip()
    if not opp_team:
        raise HTTPException(status_code=404, detail="Could not parse opponent team")
    return await fetch_pitcher_h2h(pitcher, opp_team)


@router.get("/{pick_id}/simulation")
async def pick_simulation(
    pick_id: str,
    user: Annotated[UserPublic, Depends(current_user)],
):
    """Run Monte Carlo on a single pick on demand. Returns sim output
    dict with sim_win_probability, 95% Wilson CI, runs, market
    category, disagreement vs blended model. Supports MLB, Soccer,
    NBA, Tennis."""
    pick = await db.picks.find_one({"id": pick_id}, {"_id": 0})
    if not pick:
        raise HTTPException(status_code=404, detail="Pick not found")
    sport = pick.get("sport") or ""
    if sport not in {"MLB", "Soccer", "NBA", "Tennis"}:
        raise HTTPException(
            status_code=404,
            detail=f"Simulation not yet available for {sport or 'this sport'}",
        )
    from brain.sim_runner import simulate_pick  # lazy
    sim = simulate_pick(pick)
    if not sim:
        raise HTTPException(status_code=404, detail="Simulator could not route this market")
    return sim


@router.get("/{pick_id}/matchup")
async def pick_matchup(
    pick_id: str,
    user: Annotated[UserPublic, Depends(current_user)],
):
    """Historical player-vs-opponent matchup intelligence for a pick.

    Returns a rich payload with:
      • matchup_grade (A+..F)
      • sample_confidence (high|medium|low|none)
      • threshold_hit_rate against the pick's over/under line
      • career_vs_opponent, recent_vs_similar, overall_last_10, overall_season
      • NFL only: position, last_meeting, per-stat threshold breakdowns

    Works for MLB (K/H/HR/RBI/TB/HRRI), NFL (QB/RB/WR/TE), NBA, and
    Tennis prop markets. Team/moneyline markets return
    `{ supported: false }` and the frontend should hide the badge.

    Zero writes. Zero HTTP calls. Never 500s — engine errors are
    reported inside the payload's `notes` list.
    """
    pick = await db.picks.find_one({"id": pick_id}, {"_id": 0})
    if not pick:
        raise HTTPException(status_code=404, detail="Pick not found")
    from services.pick_matchup_wiring import build_matchup_payload  # lazy
    payload = await build_matchup_payload(db, pick)
    payload["pick_id"] = pick_id
    return payload
