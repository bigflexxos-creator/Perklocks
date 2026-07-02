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
    from server import _ensure_today_picks, _today_str, _canonicalize_picks  # lazy
    await _ensure_today_picks()
    q: dict = {"pick_date": _today_str()}
    if sport and sport.lower() != "all":
        q["sport"] = sport
    cursor = db.picks.find(q, {"_id": 0}).sort("lock_score", -1).limit(200)
    return {"picks": _canonicalize_picks(await cursor.to_list(length=200))}


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
    """
    from server import SPORT_MARKETS, _today_str, _filter_in_play_window  # lazy
    markets = SPORT_MARKETS.get(sport, [])
    raw = await db.picks.find(
        {"sport": sport, "pick_date": _today_str()},
        {"_id": 0, "league": 1, "event_time": 1, "lock_score": 1,
         "is_under_lock": 1, "no_bet": 1, "edge_percent": 1,
         "elite_player": 1},
    ).to_list(length=1000)

    def _qualifies(p: dict) -> bool:
        if p.get("no_bet") is True:
            return False
        elite = bool(p.get("elite_player"))
        lock = float(p.get("lock_score") or 0)
        edge = float(p.get("edge_percent") or 0)
        if elite:
            return True
        return lock >= 85 and edge >= 0

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
        et = p.get("event_time") or ""
        try:
            dt = datetime.strptime(et, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            return now <= dt <= cutoff
        except Exception:
            return False

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
    base_q: dict = {"pick_date": _today_str(), "no_bet": {"$ne": True}}
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
        """Returns (accept, reject_reason). All V4 rules applied here."""
        lock = float(p.get("lock_score") or 0)
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
    q = {**base_q, "lock_score": {"$gte": LOCK_FLOOR}}
    candidates: list = await db.picks.find(q, {"_id": 0}).to_list(length=800)
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
        et = p.get("event_time") or ""
        try:
            dt = datetime.strptime(et, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            return now <= dt <= cutoff
        except Exception:
            return False

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
        return base * mkt_mult * (1.0 - chalk_pen) * hist_mult

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
    return {
        "picks": _canonicalize_picks(top),
        "pick": _canonicalize_lock_score(top[0]) if top else None,
        "composite_rank": top[0]["composite_rank"] if top else None,
        "total_evaluated": len(pool),
        "scoped_to_today": bool(today_picks),
        "rollover_version": "v4",
        "survivability": {
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
        },
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

    Applies the same correlated-pick dedup as the live picks endpoint
    so the History tab doesn't show "Player Over 0.5 Hits" AND
    "Player Over 0.5 Total Bases" as two separate losses — they're
    one logical bet.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    q: dict = {
        "settled_at": {"$gte": cutoff},
        # Hide voided picks (legacy soccer goalscorer payloads etc.)
        # Voided picks are kept in the DB for the learning engine but
        # never shown in the user-facing History tab or counted toward
        # W/L stats.
        "status": {"$nin": ["void"]},
        "excluded_from_history": {"$ne": True},
        # ── Board-floor gate (2026-07-01 update). Picks for many markets
        # that the LIVE feed filters out for low lock scores settle and
        # then leak into PICK HISTORY even though the user never saw them.
        # Result: a Lost record that pollutes the hit-rate.
        #
        # Fix (per user 2026-07-01 "89 lowers shouldn't be graded because
        # it's never on board"): only show in history picks that ACTUALLY
        # crossed the surfacing floor (lock_score ≥ 89 for standard picks;
        # alt-line carve-outs at ≥85). AND exclude off-scope markets:
        #   • First / Last Goal Scorer — retired from product
        #   • KBO / Korean baseball — out of scope
        #   • Anytime Goal Scorer < 85 — never surfaced to top-3
        "$and": [
            {"market": {"$not": {"$regex":
                r"First Goal Scorer|Last Goal Scorer", "$options": "i"}}},
            {"league": {"$not": {"$regex": r"KBO|Korean", "$options": "i"}}},
            {"$or": [
                {"market": {"$not": {"$regex": r"Anytime Goal Scorer",
                                      "$options": "i"}}},
                {"lock_score": {"$gte": 85}},
            ]},
            {"$or": [
                {"lock_score": {"$gte": 89}},
                {"raw_lock_score": {"$gte": 89}},
                # Carve-out: elite-pitcher override picks were intentionally
                # surfaced even at lower lock with strong edge — preserve them.
                {"elite_pitcher_override": True},
                {"is_alt": True, "lock_score": {"$gte": 85}},
            ]},
        ],
    }
    cursor = db.picks.find(q, {"_id": 0}).sort("settled_at", -1).limit(2000)
    picks = await cursor.to_list(length=2000)

    # ─── Dedupe correlated historical picks ───
    # Same logic as sports_engine.generate_all_picks. Group by
    # (sport, event, selection, line_threshold) and keep the preferred one:
    #   1) Market family — Hits > anything > Total Bases
    #   2) Settled status outcome consistency (prefer won > lost > push > pending)
    #   3) Higher lock_score, then better odds.
    def _key(p: dict) -> tuple:
        market = p.get("market") or ""
        m = re.search(r"(-?\d+\.\d+)", market)
        return (
            p.get("sport"), p.get("event"), p.get("selection") or "",
            m.group(1) if m else "",
        )

    def _market_priority(market: str) -> int:
        m = (market or "").lower()
        if "hits" in m:
            return 0
        if "win or draw" in m or "double chance" in m:
            return 0
        if "moneyline" in m:
            return 2
        if "total bases" in m:
            return 2
        return 1

    _STATUS_RANK = {"won": 0, "lost": 1, "push": 2, "pending": 3}

    best: dict = {}
    for p in picks:
        k = _key(p)
        ex = best.get(k)
        if ex is None:
            best[k] = p
            continue
        new_pri = _market_priority(p.get("market"))
        old_pri = _market_priority(ex.get("market"))
        if new_pri != old_pri:
            if new_pri < old_pri:
                best[k] = p
            continue
        new_stat = _STATUS_RANK.get(p.get("status") or "pending", 4)
        old_stat = _STATUS_RANK.get(ex.get("status") or "pending", 4)
        if new_stat != old_stat:
            if new_stat < old_stat:
                best[k] = p
            continue
        if (p.get("lock_score") or 0) > (ex.get("lock_score") or 0):
            best[k] = p
        elif (p.get("lock_score") or 0) == (ex.get("lock_score") or 0):
            if (p.get("book_odds") or -9999) > (ex.get("book_odds") or -9999):
                best[k] = p
    picks = sorted(best.values(), key=lambda p: p.get("settled_at") or "", reverse=True)

    settled = [p for p in picks if p.get("status") in ("won", "lost", "push")]
    won = sum(1 for p in settled if p.get("status") == "won")
    lost = sum(1 for p in settled if p.get("status") == "lost")
    push = sum(1 for p in settled if p.get("status") == "push")
    decided = won + lost
    hit_rate = round(won / decided * 100, 1) if decided else 0.0
    # Rollover V3 (2026-07-01): analytics must mirror the endpoint's
    # HIGH-CONVICTION floors — lock_score ≥ 95 AND win_prob ≥ 0.80 AND
    # edge_percent ≥ 4.0. Anything looser inflates rollover_hit_rate
    # with picks that never actually surfaced on the rollover tab.
    # `win_probability` is stored as 0-100 in some pipelines and 0-1 in
    # others — normalise before comparing.
    def _wp_frac(v) -> float:
        try:
            f = float(v or 0)
        except Exception:
            return 0.0
        return f / 100.0 if f > 1.0 else f
    rollover_picks = [
        p for p in settled
        if (p.get("lock_score") or 0) >= 95
        and _wp_frac(p.get("win_probability")) >= 0.80
        and float(p.get("edge_percent") or 0) >= 4.0
    ]
    ro_won = sum(1 for p in rollover_picks if p.get("status") == "won")
    ro_lost = sum(1 for p in rollover_picks if p.get("status") == "lost")
    ro_push = sum(1 for p in rollover_picks if p.get("status") == "push")
    ro_decided = ro_won + ro_lost
    ro_hit_rate = round(ro_won / ro_decided * 100, 1) if ro_decided else 0.0
    if rollover_only:
        # Stats must reflect the SAME scope as the returned picks list.
        settled = rollover_picks
        return {
            "picks": settled,
            "stats": {
                "total": len(settled),
                "won": ro_won,
                "lost": ro_lost,
                "push": ro_push,
                "hit_rate": ro_hit_rate,
                "rollover_hit_rate": ro_hit_rate,
                "rollover_decided": ro_decided,
            },
        }
    return {
        "picks": settled,
        "stats": {
            "total": len(settled),
            "won": won,
            "lost": lost,
            "push": push,
            "hit_rate": hit_rate,
            "rollover_hit_rate": ro_hit_rate,
            "rollover_decided": ro_decided,
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
        _decorate_with_player_form, _decorate_with_understat_form,
        _strip_for_lite,
    )
    await _ensure_today_picks()
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
    # When the user explicitly filters by a single market, relax the default
    # 85+ lock floor — they're narrowing the pool themselves and want to see
    # everything that matches their selection.
    #
    # Also relax for the ALT line-type tab — alt lines like soccer
    # Over 1.5 / Under 3.5 are intentionally lower-confidence chalkier
    # OR longer-shot variations of the main consensus, so a strict 85
    # floor zeroes-out the tab entirely. User feedback: "soccer still
    # not showing alt on website or app" — drop floor to 55 for alt so
    # the synthesized lines surface.
    lt = (line_type or "").lower()
    default_floor = 75.0 if has_market_filter else (55.0 if lt == "alt" else 85.0)
    floor = max(default_floor, float(min_lock)) if min_lock is not None else default_floor
    # ── Auto-relax floor when the slate is genuinely thin ──────────────
    # User complaint 2026-06-26: "only 1 game showing up". Root cause was
    # genuine low slate (off-season for several sports, only 14 picks in
    # DB on that day, 3 high-lock, in-play filter cuts most of those).
    # Rather than show a near-empty board, count how many picks pass the
    # strict floor; if it's <8, progressively relax to 75 → 65 → 55 so
    # the user always sees at least some actionable picks. We only do
    # this on the unfiltered "All" feed (no market / league / day_offset
    # narrowing) where the user expects the full slate.
    auto_relaxed_from: Optional[float] = None
    if (min_lock is None and not has_market_filter and not has_league_filter
            and not game_id_list and not event_list and not search
            and not day_offset and not grade and lt != "alt"):
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
    standard_q = {
        "$or": [
            # Filter compares against ALL canonical lock fields so the
            # min_lock = 99 user filter doesn't hide Neymar/Memphis/etc.
            # whose DB lock_score has drifted down to 64 while v2/peak
            # still hold 99. User report 2026-06-26: "when I select 99
            # nothing populate but 99 are on board". This OR mirrors the
            # `_canonicalize_lock_score` max() at read time so what the
            # UI sees and what the filter matches are consistent.
            {"lock_score": {"$gte": floor}},
            {"lock_score_v2": {"$gte": floor}},
            {"lock_score_raw": {"$gte": floor}},
            {"lock_score_peak": {"$gte": floor}},
        ],
        "no_bet": {"$ne": True},
        # Hide negative-edge picks from the main feed entirely.
        # Picks where model_WP < book_implied are by definition bad
        # bets (book is sharper than us). The Locks tab is for
        # actionable +EV picks only.
        "edge_percent": {"$gte": 0},
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
        "$or": [
            {"lock_score": {"$gte": 80.0}},
            {"lock_score_v2": {"$gte": 80.0}},
        ],
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
        "$and": [{
            "$or": [
                {"lock_score": {"$gte": 75.0}},
                {"lock_score_v2": {"$gte": 75.0}},
            ],
        }],
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
    tennis_ml_q = {
        "sport": "Tennis",
        "market": {"$regex": "moneyline", "$options": "i"},
        "no_bet": {"$ne": True},
        "$or": [
            # Path 1: standard Odds-API tennis ML — must clear -3 edge floor + strong lock
            {
                "edge_percent": {"$gte": -3.0},
                "$or": [
                    {"lock_score": {"$gte": 80.0}},
                    {"lock_score_v2": {"$gte": 80.0}},
                    {"bandit_lift": {"$gt": 0}},
                ],
            },
            # Path 2: tennis_extra scraped picks — book-anchored, edge is meaningless
            {
                "source": {"$in": ["tennis_extra", "tennis_extra_model", "tennis_real_odds"]},
                "$or": [
                    {"lock_score": {"$gte": 80.0}},
                    {"lock_score_v2": {"$gte": 80.0}},
                ],
            },
        ],
    }
    # ── Tennis Extended Coverage carve-out (user request 2026-06-25) ─
    # Surface ALL tennis_extra picks on the main board (not just
    # Moneyline) so users see the full slate at a glance. The frontend
    # renders an "EXT" badge on these so it's obvious the line came
    # from a TennisExplorer scrape rather than a US sportsbook.
    tennis_extra_q = {
        "sport": "Tennis",
        "source": {"$in": ["tennis_extra", "tennis_extra_model"]},
        "no_bet": {"$ne": True},
        "$or": [
            {"lock_score": {"$gte": 75.0}},
            {"lock_score_v2": {"$gte": 75.0}},
        ],
    }
    # ── Tennis Alt-Line carve-out (chalk-ladder exception) ────────────
    # User report 2026-06-23: "you deleted all the alt spread tennis
    # after I told you to fix simulator add a tab under tennis (alt)
    # that all alt spread tennis picks". Alt-spread + alt-total tennis
    # picks (e.g. "Fritz -3.0 Spread", "Svitolina -3.5 Spread", "Under
    # 21.0 Games (Alt)") are chalk-ladder bets where the FAVORED side
    # is priced at -300 to -800. By construction these will register
    # tiny / slightly-negative edge against the sharp market, so the
    # default `edge_percent >= 0` gate erases them entirely. Surface
    # them under a relaxed gate (edge >= -8 + lock >= 70) so the user's
    # new "Alt" tab shows the chalkiest acceptable lines and the
    # synthesized chalk-totals the sports engine builds for them.
    tennis_alt_q = {
        "sport": "Tennis",
        "no_bet": {"$ne": True},
        "$or": [
            {"is_alt_prop": True},
            {"market": {"$regex": r"\(alt\)|[+\-]\d+(\.\d+)?\s+spread|\bspread\b|total games|games over|games under", "$options": "i"}},
        ],
        "edge_percent": {"$gte": -8.0},
        "$and": [{"$or": [
            {"lock_score": {"$gte": 70.0}},
            {"lock_score_v2": {"$gte": 70.0}},
        ]}],
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
    mlb_k_q = {
        "sport": "MLB",
        "market": {"$regex": "strikeout", "$options": "i"},
        "no_bet": {"$ne": True},
        "edge_percent": {"$gte": -12.0},
        "$or": [
            {"lock_score": {"$gte": 70.0}},
            {"lock_score_v2": {"$gte": 70.0}},
        ],
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
        "edge_percent": {"$gte": -6.0},
        "$or": [
            {"lock_score": {"$gte": 85.0}},
            {"lock_score_v2": {"$gte": 85.0}},
        ],
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
        "edge_percent": {"$gte": -10.0},
        "$or": [
            {"lock_score": {"$gte": 70.0}},
            {"lock_score_v2": {"$gte": 70.0}},
        ],
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
    high_lock_bypass_q = {
        "no_bet": {"$ne": True},
        "$or": [
            {"lock_score":    {"$gte": 90.0}},
            {"lock_score_v2": {"$gte": 90.0}},
        ],
    }
    q: dict = {
        "pick_date": _today_str(),
        # Exclude special-tab markets (NRFI/YRFI lives in its own MLB
        # sub-tab — user explicitly asked to keep these off the main board).
        "hide_from_main_board": {"$ne": True},
        "$or": [standard_q, elite_q, model_only_q, tennis_ml_q, tennis_alt_q, tennis_extra_q, mlb_k_q, mlb_hitter_q, soccer_scorer_q, high_lock_bypass_q],
    }
    # ── User-supplied min_lock floor (global enforcement) ────────────
    # Each sub-query above uses its own lock floor (70 for tennis ML,
    # 85 for soccer scorers, 80 for elite anchors, etc.) tuned to its
    # carve-out's chalk-pricing reality. But when the user EXPLICITLY
    # slides the Min Lock filter to e.g. 95, those carve-out floors
    # would silently leak 70-94 picks back into the feed. To honour
    # the user's slider, we AND a global `lock_score >= min_lock`
    # condition over every sub-query. Check both `lock_score` and
    # `lock_score_v2` (same OR-of-both pattern used by every
    # sub-query) so picks where V2 has caught up but V1 hasn't yet
    # don't get filtered out wrongly. Default `floor` was already
    # applied per-sub-query, so this is purely about the user's
    # explicit override.
    if min_lock is not None and float(min_lock) > 0:
        user_floor = float(min_lock)
        q["$and"] = [{"$or": [
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
    try:
        from quality_gate import apply_quality_gate, validate_against_live_alt_lines
        picks, qg_blocked = apply_quality_gate(picks)
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
    picks = _dedupe_game_outcome_picks(picks)
    # Goalscorer pick cap — per match, surface at most the TOP 1 unique
    # player per (team × market_family). Was top_n=4 — but the backtest
    # over 397 graded goalscorer picks showed elite players win Anytime
    # at ~27% while the 2nd/3rd/4th-best options bottom out under 10%.
    # Surfacing only the single mathematically-best candidate per team
    # is the user's mandate (2026-06-29).
    picks = _dedupe_goalscorer_per_event(picks, top_n=1)
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
    try:
        from goalscorer_matchup import annotate_picks_async
        from deps import db as _matchup_db  # async motor handle
        picks = await annotate_picks_async(picks, _matchup_db, apply_drop=True)
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
            et = p.get("event_time") or ""
            try:
                dt = datetime.strptime(et, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                return 0 if now <= dt <= cutoff else 1
            except Exception:
                return 1

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
            try:
                return datetime.strptime(
                    p.get("event_time") or "", "%Y-%m-%dT%H:%M:%SZ",
                ).replace(tzinfo=timezone.utc)
            except Exception:
                return datetime.max.replace(tzinfo=timezone.utc)
        # Elite-player anchor: float elite picks to the top within their
        # bucket — but ONLY for the default lock-desc view. When the user
        # has explicitly asked for asc / win / edge / time, respect their
        # chosen ordering without re-shuffling Mbappé/Haaland/Messi/Kane
        # to the top.
        def _elite_rank(p: dict) -> int:
            return 0 if p.get("elite_player") else 1
        if s == "time":
            # Pure chronological — earliest kickoff first by default;
            # latest first when asc=False reversed (we treat time asc as
            # earliest→latest, which is the natural meaning, so flip
            # signature only when direction explicitly says desc).
            # Default 'time' direction is "soonest first" which is asc by
            # natural time ordering — keep that as the default.
            if asc:
                picks.sort(key=lambda p: (_event_dt(p), -p.get("lock_score", 0)))
            else:
                picks.sort(key=lambda p: (_event_dt(p), -p.get("lock_score", 0)), reverse=True)
        elif s == "edge":
            # Pure edge sort — no today-first bucket so highest edges
            # always at top regardless of date.
            picks.sort(key=lambda p: (m * p.get("edge_percent", 0), -p.get("lock_score", 0)))
        elif s == "win":
            # Win % sort — model win_probability highest first by default.
            picks.sort(key=lambda p: (m * p.get("win_probability", 0), -p.get("lock_score", 0)))
        elif s == "implied":
            picks.sort(key=lambda p: (m * p.get("implied_probability", 0), -p.get("lock_score", 0)))
        else:  # "lock" (default)
            # Pure lock_score sort. The user's explicit ask: "It should
            # take highest score" — sorting by Lock Score should be a
            # strict ordering with no elite-player anchor, no league
            # round-robin, no bucket pre-sort. If a smaller-league pick
            # has a higher lock, it should win the top slot. Period.
            #
            # (League diversification still exists as a separate
            # affordance via the explicit league filter — surfacing
            # smaller leagues is the league pill's job, not the sort's.)
            if asc:
                picks.sort(key=lambda p: p.get("lock_score", 0))
            else:
                picks.sort(key=lambda p: -p.get("lock_score", 0))
    picks = await _decorate_with_player_form(picks)
    picks = await _decorate_with_understat_form(picks)
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
    if lite:
        canonical = [_strip_for_lite(p) for p in canonical]
    return {"picks": canonical}


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



# ─── /picks/refresh (1h rate-limited manual refresh) ─────────────────
@router.post("/refresh")
async def force_refresh(user: Annotated[UserPublic, Depends(current_user)]):
    """Manually refresh today's picks. Rate-limited to 1× per hour per user
    to prevent button-mashing that burns The Odds API credits
    (each refresh costs ~250-400 credits)."""
    # Lazy import — see /picks/today for the rationale.
    from server import _today_str, _refresh_picks
    now = datetime.now(timezone.utc)
    # Check last refresh time for this user (stored in user doc).
    user_doc = await db.users.find_one({"id": user.id}, {"_id": 0, "last_refresh_at": 1})
    last_iso = (user_doc or {}).get("last_refresh_at")
    cd = _cooldown_payload(last_iso, now)
    if not cd["can_refresh"]:
        remaining_min = (cd["cooldown_seconds"] // 60) + (1 if cd["cooldown_seconds"] % 60 else 0)
        existing = await db.picks.count_documents({"pick_date": _today_str()})
        return {
            "refreshed": False,
            "rate_limited": True,
            "retry_after_minutes": remaining_min,
            "cooldown_seconds": cd["cooldown_seconds"],
            "next_refresh_at": cd["next_refresh_at"],
            "last_refresh_at": cd["last_refresh_at"],
            "count": existing,
            "date": _today_str(),
            "message": f"Picks were refreshed recently. Try again in {remaining_min} min — saves API credits.",
        }
    # Fire-and-forget: kick off the actual refresh in the background.
    # `_refresh_picks` takes ~45 s end-to-end (Odds API fetch +
    # generation + brain filter + validator) which exceeds mobile HTTP
    # timeouts, so the user's app would show "Refresh failed" even
    # when the refresh actually succeeded. We now mark cooldown
    # immediately, return instantly, and let the user's existing
    # focus-refetch (30 s) pull the new picks once they land.
    await db.users.update_one(
        {"id": user.id},
        {"$set": {"last_refresh_at": now.isoformat()}},
    )
    asyncio.create_task(_refresh_picks(_today_str()))
    existing = await db.picks.count_documents({"pick_date": _today_str()})
    next_dt = now + timedelta(seconds=REFRESH_COOLDOWN_SECONDS)
    return {
        "refreshed": True,
        "queued": True,
        "count": existing,                   # current count; new count lands soon
        "date": _today_str(),
        "cooldown_seconds": REFRESH_COOLDOWN_SECONDS,
        "next_refresh_at": next_dt.isoformat(),
        "last_refresh_at": now.isoformat(),
        "note": "Refresh started in background (~45 s). New picks will appear automatically on the next focus-refetch.",
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

