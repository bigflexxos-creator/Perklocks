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
    ID. Logic lives in _cooldown_payload() in server.py."""
    from server import _cooldown_payload  # lazy
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
    """Top 3 safest bets of the day — the user picks which one to roll.

    Rollover V2 (default) — "Survivability Mode":
      • HARD floors: odds ≥ -350, edge ≥ +5%, win_prob ≥ implied + 5pts
      • Risk-adjusted ranking (chalk penalty + edge multiplier + historical
        consistency bonus + alt-line penalty) replaces pure win_prob sort
      • At most ONE alt-line pick in the trio
      • Soccer goalscorer markets always blocked

    Modes:
      • `mode=v2` (default) — single best pick + 2 alternatives
      • `mode=split` — return 2 uncorrelated picks for split-stake bankroll
      • `mode=v1` — legacy ranking (no floors)

    Rules:
      - Today's slate only (kickoff within 24h)
      - Lock score >= 90 (progressive floor)
      - NO Soccer by default — but if the user explicitly picks
        `sport=Soccer` we honour their choice.
      - Prefers player props over team moneylines
      - Diversifies: at most one pick per game / per sport
      - `line_type`: "main" / "alt" / "both" (default).
    """
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

    # Always exclude Soccer goalscorer markets from Rollover.
    existing_market_q = base_q.pop("market", None)
    goalscorer_block = {
        "market": {"$not": {"$regex": r"goal scorer|to score or assist", "$options": "i"}},
    }
    if existing_market_q:
        base_q["$and"] = [{"market": existing_market_q}, goalscorer_block]
    else:
        base_q["market"] = goalscorer_block["market"]

    floors = [90, 85, 80, 75, 70]
    chalk_cap_strict = -350
    chalk_cap_relaxed = -400

    def _implied_prob(odds: float) -> float:
        try:
            o = float(odds)
        except Exception:
            return 0.0
        if o == 0:
            return 0.0
        return (-o) / ((-o) + 100.0) if o < 0 else 100.0 / (o + 100.0)

    def _survivability_ok(p: dict, *, strict: bool = True) -> bool:
        odds = p.get("book_odds") or -9999
        edge = float(p.get("edge_percent") or 0)
        wp = float(p.get("win_probability") or 0)
        cap = chalk_cap_strict if strict else chalk_cap_relaxed
        if odds < cap:
            return False
        if strict and edge < 5.0:
            return False
        if not strict and edge < 3.0:
            return False
        implied_pct = _implied_prob(odds) * 100.0
        cushion = 5.0 if strict else 3.0
        if wp < (implied_pct + cushion):
            return False
        return True

    picks: list = []
    candidates: list = []
    floor_used: int = 90
    strict_mode = True
    for f in floors:
        q = {**base_q, "lock_score": {"$gte": f}}
        cursor = db.picks.find(q, {"_id": 0})
        candidates = await cursor.to_list(length=500)
        picks = [p for p in candidates if _survivability_ok(p, strict=True)]
        if len({p.get("event") for p in picks}) >= 3:
            floor_used = f
            break
        floor_used = f
    if len({p.get("event") for p in picks}) < 3:
        strict_mode = False
        q = {**base_q, "lock_score": {"$gte": 75}}
        candidates = await db.picks.find(q, {"_id": 0}).to_list(length=500)
        picks = [p for p in candidates if _survivability_ok(p, strict=False)]
    if not picks:
        q = {**base_q, "lock_score": {"$gte": 70}}
        candidates = await db.picks.find(q, {"_id": 0}).to_list(length=500)
        picks = [
            p for p in candidates
            if (p.get("book_odds") or -9999) >= -500
            and float(p.get("edge_percent") or 0) >= 2.0
        ]

    picks = _filter_in_play_window(picks)
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
        return {"picks": [], "pick": None, "total_evaluated": 0}

    def _ev_score(p: dict) -> float:
        wp = float(p.get("win_probability") or 0)
        edge = float(p.get("edge_percent") or 0)
        odds = float(p.get("book_odds") or -100)
        if odds <= -200:
            chalk_pen = min(0.30, (abs(odds) - 200) / 500.0)
        else:
            chalk_pen = 0.0
        if edge >= 10:
            edge_mult = 1.20
        elif edge >= 7:
            edge_mult = 1.10
        else:
            edge_mult = 1.0
        sig = p.get("historical_signal") or {}
        consistency = float(sig.get("consistency") or 0)
        if sig.get("label") == "hot" and consistency >= 0.7:
            hist_bonus = 1.05
        elif sig.get("label") == "cold":
            hist_bonus = 0.95
        else:
            hist_bonus = 1.0
        alt_pen = 0.92 if p.get("is_alt") else 1.0
        return wp * (1.0 - chalk_pen) * edge_mult * hist_bonus * alt_pen

    ranked = sorted(pool, key=_ev_score, reverse=True)

    seen_events: set = set()
    seen_sports: set = set()
    primary: list = []
    secondary: list = []
    alts_in_trio: int = 0
    MAX_ALTS = 1
    for p in ranked:
        ev = p.get("event")
        sp = p.get("sport") or ""
        if ev in seen_events:
            continue
        if p.get("is_alt") and alts_in_trio >= MAX_ALTS:
            secondary.append(p)
            continue
        if sp in seen_sports:
            secondary.append(p)
            continue
        seen_events.add(ev)
        seen_sports.add(sp)
        if p.get("is_alt"):
            alts_in_trio += 1
        primary.append({**p, "composite_rank": round(_ev_score(p), 2)})
        if len(primary) >= 3:
            break
    top = primary
    if len(top) < 3:
        for p in secondary:
            ev = p.get("event")
            if ev in seen_events:
                continue
            seen_events.add(ev)
            top.append({**p, "composite_rank": round(p.get("win_probability", 0) or 0, 1)})
            if len(top) >= 3:
                break
    return {
        "picks": _canonicalize_picks(top),
        "pick": _canonicalize_lock_score(top[0]) if top else None,
        "composite_rank": top[0]["composite_rank"] if top else None,
        "total_evaluated": len(pool),
        "scoped_to_today": bool(today_picks),
        "rollover_version": "v2",
        "survivability": {
            "mode": "strict" if strict_mode else "relaxed",
            "odds_floor": chalk_cap_strict if strict_mode else chalk_cap_relaxed,
            "edge_floor": 5.0 if strict_mode else 3.0,
            "ev_cushion_pts": 5.0 if strict_mode else 3.0,
            "alt_cap": MAX_ALTS,
            "lock_floor_used": floor_used,
            "rejected_chalk": sum(
                1 for p in candidates
                if (p.get("book_odds") or -9999) < chalk_cap_strict
            ) if candidates else 0,
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
        # ── Board-floor gate (added 2026-06-23). Picks for many
        # markets that the LIVE feed filters out for low lock scores
        # (Bosnia vs Switzerland "Score or Assist" picks at lock
        # 67-75 etc.) settle and then leak into PICK HISTORY even
        # though the user never saw them. Result: a Lost record
        # that pollutes the hit-rate.
        #
        # Fix: only show in history picks that ACTUALLY crossed the
        # surfacing floor (lock_score ≥ 80, matching the lowest
        # carve-out floor used by /picks/today). Use raw_lock_score
        # when present so the calibration overlay (which can lower
        # the display number for pending picks) doesn't accidentally
        # hide legitimate history rows.
        "$or": [
            {"lock_score": {"$gte": 80}},
            {"raw_lock_score": {"$gte": 80}},
            # Carve-out: elite-pitcher override picks were intentionally
            # surfaced even at lock<80 with strong edge — preserve them.
            {"elite_pitcher_override": True},
            {"is_alt": True, "lock_score": {"$gte": 75}},
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
    rollover_picks = [p for p in settled if (p.get("lock_score") or 0) >= 90]
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


# ─────────────── /{pick_id} parameterized routes ───────────────
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
    # Canonicalize lock_score → max(v1, v2) so detail view matches the
    # home feed card. Single source of truth.
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
