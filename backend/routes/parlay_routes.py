"""Parlay-builder routes — extracted from picks_routes.py (2026-06-28).

Carries the highest-coupling endpoint we'd previously kept inside the
big picks router:

  * GET /api/picks/parlay — Parlay Optimizer V1.1

The handler is heavy (window sizing, lock-score floors, learned-synergy
weighting, fallback-window auto-expansion, alternate-leg generation,
history recording). Splitting it out:

  • Keeps picks_routes.py focused on raw pick CRUD + detail enrichment.
  • Lets the parlay handler grow without making picks_routes.py harder
    to read or test.
  • Mirrors `parlay_history_routes.py` which already owns the SAVE /
    LIST endpoints for user-bookmarked parlays.

The URL stays `/api/picks/parlay` — no frontend change needed. We use
prefix="/picks" on this router and FastAPI happily merges the routes
under the same prefix as picks_routes.py.

DESIGN RULES (carried forward from picks_routes.py):
  1. Lazy-import every helper from server.py INSIDE the handler so
     module-load order stays acyclic.
  2. Mount BEFORE picks_routes in server.py so the literal `/parlay`
     segment resolves before the `/{pick_id}` catch-all in picks_routes.

Author: PerkLocks AI · 2026-06-28
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone, timedelta
from typing import Annotated, Optional

from fastapi import APIRouter, Depends

from auth import UserPublic
from deps import current_user, db, logger

router = APIRouter(prefix="/picks", tags=["picks"])


# ─── /picks/parlay (optimizer V1.1) ──────────────────────────────────
@router.get("/parlay")
async def pick_parlay(user: Annotated[UserPublic, Depends(current_user)],
                     legs: int = 3,
                     mode: str = "standard",
                     sport: Optional[str] = None,
                     line_type: Optional[str] = None,
                     exclude_sports: Optional[str] = None,
                     include_sports: Optional[str] = None,
                     sport_mode: str = "auto",
                     window_hours: int = 24,
                     market: Optional[str] = None,
                     league: Optional[str] = None,
                     rank: int = 1,
                     locked_ids: Optional[str] = None,
                     refresh_nonce: int = 0,
                     advanced_sub: str = "ev"):
    """Parlay Optimizer V1.1 — highest-probability parlay builder.

    Mode (`mode`):
      - standard: Lock≥88, Edge≥+3%, ROI non-negative. Target 2-5 legs.
      - high_risk: Lock≥75, Edge≥+1%. Target 10-20 legs.

    Sport selection (`sport_mode`):
      - auto: use everything (default).
      - custom: limit pool to `include_sports` (comma-separated list).
      - single: limit pool to one `sport` value AND bypass same-sport
        diversification (so 100% same sport is allowed).

    Time window (`window_hours`): only consider events with commence_time
    inside the next N hours. Defaults to 24h.

    Refresh: pass `rank=2,3,4…` to cycle through next-best candidates.
    Pin legs: pass `locked_ids` (comma-separated pick IDs).
    """
    from parlay_optimizer import (
        build_top_parlays, parlay_to_payload,
    )
    # Lazy import every helper from server.py used in this handler.
    # See /picks/today for the rationale (circular-import avoidance).
    from server import (
        _ensure_today_picks, _today_str, _market_regex,
        _canonicalize_picks, _historical_winrates,
    )
    await _ensure_today_picks()
    is_high_risk = (mode or "").lower() == "high_risk"
    # "1-5H Today" is now a WINDOW overlay (not its own mode) — it works under
    # any active mode (Standard / Advanced / High Risk). Triggered whenever the
    # requested window is short (≤8h). Applies a 30-min start floor (so we
    # don't show games already starting) + auto-expand fallback if the tight
    # window is empty. The mode's lock floor / leg target rules still apply.
    is_today_window = (mode or "").lower() == "today_window" or int(window_hours or 24) <= 8
    is_advanced = (mode or "").lower() == "advanced"
    advanced_sub_norm = (advanced_sub or "ev").lower()
    if advanced_sub_norm not in ("safer", "ev"):
        advanced_sub_norm = "ev"
    mode_lower = (sport_mode or "auto").lower()
    is_single_sport = mode_lower == "single"

    # ─── Sport filter ───
    sport_filter: dict = {}
    sport_q = (sport or "").strip()
    if mode_lower == "single" and sport_q and sport_q.lower() not in ("mix", "all"):
        sport_filter = {"sport": sport_q}
    elif mode_lower == "custom" and include_sports:
        wanted = [s.strip() for s in include_sports.split(",") if s.strip()]
        if wanted:
            sport_filter = {"sport": {"$in": wanted}}
    else:
        # AUTO mode (or fallback): honour legacy exclude_sports if provided.
        if exclude_sports:
            excluded = [s.strip() for s in exclude_sports.split(",") if s.strip()]
            if excluded:
                sport_filter = {"sport": {"$nin": excluded}}

    lt = (line_type or "").lower()
    line_filter: dict = {}
    if lt == "main":
        line_filter = {"is_alt": {"$ne": True}}
    elif lt == "alt":
        line_filter = {"is_alt": True}
    market_filter: dict = {}
    if market:
        regex = _market_regex(market)
        if regex:
            market_filter = {"market": {"$regex": regex, "$options": "i"}}
    league_filter: dict = {}
    if league:
        league_filter = {"league": {"$regex": re.escape(str(league)), "$options": "i"}}  # SEC-004

    target_legs = (
        max(10, min(20, max(1, int(legs or 10)))) if is_high_risk else
        max(2, min(4, max(1, int(legs or 3)))) if is_today_window else
        # Advanced: SAFER caps at 4 legs (hit rate), EV up to 6 (more shots).
        max(2, min(4 if advanced_sub_norm == "safer" else 6, max(1, int(legs or 3))))
        if is_advanced else
        max(2, min(8, max(1, int(legs or 3))))
    )
    rank = max(1, min(20, int(rank or 1)))  # clamp refresh cursor to 1-20

    # ─── Time window filter ───
    if is_today_window:
        # "Today" mode = next 1-5 hours only. Lower bound 30 min from now
        # (give the user time to lock in) up to 5h cap.
        window_hours = 5
    else:
        window_hours = max(1, min(720, int(window_hours)))  # 1h .. 30d
    now_utc = datetime.now(timezone.utc)
    window_cap_iso = (now_utc + timedelta(hours=window_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    floor_delta = timedelta(minutes=30) if is_today_window else timedelta(minutes=-30)
    window_floor_iso = (now_utc + floor_delta).strftime("%Y-%m-%dT%H:%M:%SZ")
    time_filter = {"event_time": {"$gte": window_floor_iso, "$lte": window_cap_iso}}

    # ─── Fetch candidate pool ───
    # ── Phase B9 μ-closure (2026-06) — canonical eligibility gate ──
    # Parlay pool MUST originate from canonical published truth.  A
    # pick with a high legacy or V2 score is NEVER admitted unless
    # PredictionPublicationService has dual-written the canonical
    # ``publication_source`` marker on its pick document.
    from services.canonical_board_source import (
        canonical_publication_filter,
    )
    _canon_filt = canonical_publication_filter()
    base_q = {
        "pick_date": _today_str(),
        "no_bet": {"$ne": True},
        "is_under_lock": {"$ne": True},
        "off_board": {"$ne": True},
        "settlement_block": {"$ne": True},
        **sport_filter, **line_filter, **market_filter, **league_filter,
        **time_filter,
        **_canon_filt,   # canonical publication gate (fail-closed default)
    }
    # Lock floor by mode. Advanced.safer is the strictest (92),
    # high_risk the loosest (70).
    if is_high_risk:
        lock_floor_val = 70
    elif is_advanced and advanced_sub_norm == "safer":
        lock_floor_val = 92
    elif is_advanced and advanced_sub_norm == "ev":
        lock_floor_val = 85
    elif is_today_window:
        lock_floor_val = 85
    else:
        lock_floor_val = 85
    base_q.pop("lock_score", None)
    # ── Phase B9 μ-closure — canonical Lock Score read ─────────────
    # Prior code admitted candidates via
    #   {"$or": [{"lock_score": …}, {"lock_score_v2": …}]}
    # allowing shadow V2 to leak unpublished picks into Parlay.  We
    # now prefer canonical ``published_lock_score`` first (frozen at
    # publication), then legacy ``lock_score``.  ``lock_score_v2`` is
    # NEVER used to admit a Parlay candidate.
    base_q["$or"] = [
        {"published_lock_score": {"$gte": lock_floor_val}},
        {"lock_score":            {"$gte": lock_floor_val}},
    ]
    pool = await db.picks.find(base_q, {"_id": 0}).sort("lock_score", -1).limit(400).to_list(length=400)
    pool = _canonicalize_picks(pool)

    # ─── Bucket-map ROI ───
    raw_buckets = await _historical_winrates()
    bucket_map: dict = {}
    for k, v in raw_buckets.items():
        if k == "__global__":
            continue
        winrate = v.get("winrate", 0.0)
        n = v.get("n", 0)
        proxy_roi = (winrate - 0.524) / 0.524 if winrate > 0 else 0.0
        bucket_map[k] = {"roi": proxy_roi, "n": n}

    # ─── Locked picks ───
    locked_picks: list[dict] = []
    if locked_ids:
        wanted_ids = [s.strip() for s in locked_ids.split(",") if s.strip()]
        if wanted_ids:
            locked_picks = await db.picks.find(
                {"id": {"$in": wanted_ids}, "pick_date": _today_str()},
                {"_id": 0},
            ).to_list(length=len(wanted_ids))

    # ─── Load learned parlay synergy map ───
    synergy_map: dict = {}
    try:
        from parlay_learning import load_synergy_map
        synergy_map = await load_synergy_map(db)
    except Exception as _sm_err:
        logger.warning("Parlay synergy map load failed: %s", _sm_err)

    # ─── Build ───
    # `build_top_parlays` is CPU-heavy (nested O(candidates × target_legs
    # × pool) plus an auto-expand ladder that can rerun it 5×). Running
    # it directly on the event loop was blocking every concurrent
    # request during peak. Offload to a worker thread so other sockets
    # keep flowing while we score parlays.
    top = await asyncio.to_thread(
        build_top_parlays,
        pool, target_legs=target_legs, high_risk=is_high_risk,
        bucket_map=bucket_map, rank=max(1, rank),
        locked_picks=locked_picks if locked_picks else None,
        single_sport_mode=is_single_sport,
        refresh_nonce=int(refresh_nonce or 0),
        synergy_map=synergy_map,
    )

    # ─── HIGH-RISK / TODAY SAFETY NET: auto-expand window if empty ───
    auto_expanded_to: Optional[int] = None
    expandable = is_high_risk or is_today_window
    if not top and expandable and window_hours < 168:
        ladder = (8, 12, 24, 72, 168) if is_today_window else (72, 168)
        for fallback_window in ladder:
            if fallback_window <= window_hours:
                continue
            fb_cap = (now_utc + timedelta(hours=fallback_window)).strftime("%Y-%m-%dT%H:%M:%SZ")
            fb_q = {**base_q}
            fb_q["event_time"] = {"$gte": window_floor_iso, "$lte": fb_cap}
            fb_pool = await db.picks.find(fb_q, {"_id": 0}).sort("lock_score", -1).limit(400).to_list(length=400)
            if len(fb_pool) < 5:
                continue
            fb_top = await asyncio.to_thread(
                build_top_parlays,
                fb_pool, target_legs=target_legs, high_risk=is_high_risk,
                bucket_map=bucket_map, rank=max(1, rank),
                locked_picks=locked_picks if locked_picks else None,
                single_sport_mode=is_single_sport,
                refresh_nonce=int(refresh_nonce or 0),
                synergy_map=synergy_map,
            )
            if fb_top:
                top = fb_top
                auto_expanded_to = fallback_window
                logger.info(
                    "%s parlay auto-expanded window %dh → %dh (%d candidate picks)",
                    "Today" if is_today_window else "High-risk",
                    window_hours, fallback_window, len(fb_pool),
                )
                break

    if not top:
        hints = []
        if mode_lower == "single" and sport_q:
            hints.append(f"in {sport_q}")
        elif mode_lower == "custom" and include_sports:
            hints.append(f"in {include_sports}")
        if window_hours != 24:
            hints.append(f"within {window_hours}h")
        hint_str = (" " + " ".join(hints)) if hints else ""
        return {
            "parlay": None,
            "parlays": [],
            "reason": (
                f"Not enough qualifying picks today{hint_str} to build a "
                f"{target_legs}-leg parlay (need Lock>=88, Edge>=+3%, "
                f"positive ROI)."
            ),
            "rank": rank,
            "locked_ids": [p.get("id") for p in locked_picks],
            "window_hours": window_hours,
            "sport_mode": mode_lower,
        }

    payloads = [parlay_to_payload(p, bucket_map) for p in top]
    for _card in payloads:
        if isinstance(_card.get("legs"), list):
            _card["legs"] = _canonicalize_picks(_card["legs"])

    # ─── Phase 5 · Intelligence Enrichment (non-destructive) ─────────
    # Attach `intelligence` block per card (leg rankings, correlation
    # report, mode metadata). Never modifies existing keys — pure
    # additive. Safe to disable via `?intelligence=0`.
    try:
        from services.parlay_intelligence.api import enrich_parlays
        _mode_for_intel = (
            "aggressive" if is_high_risk else
            "safe"       if is_today_window or (is_advanced and
                                                advanced_sub_norm == "safer")
            else "balanced"
        )
        payloads = enrich_parlays(payloads, mode=_mode_for_intel)
    except Exception as _intel_err:
        logger.warning("parlay intelligence enrichment skipped: %s", _intel_err)

    # ─── Substitute / Combination support ─────────────────────────────
    used_event_ids_per_card = [
        {leg.get("event_id") for leg in (c.get("legs") or []) if leg.get("event_id")}
        for c in payloads
    ]
    used_pick_ids_per_card = [
        {leg.get("id") for leg in (c.get("legs") or [])}
        for c in payloads
    ]
    canonical_pool = _canonicalize_picks(pool)
    for idx, card in enumerate(payloads):
        used_events = used_event_ids_per_card[idx]
        used_ids = used_pick_ids_per_card[idx]
        alternates = [
            p for p in canonical_pool
            if p.get("id") not in used_ids
            and p.get("event_id") not in used_events
        ]
        alternates.sort(key=lambda p: -(p.get("lock_score") or 0))
        card["alternates"] = alternates[:5]
        card["alternates_count"] = len(card["alternates"])
    # Persist this parlay slate into history so the learning loop has
    # data to settle and aggregate from. Cheap — dedupes by signature.
    try:
        from parlay_learning import record_parlay_shown
        for card in payloads:
            await record_parlay_shown(
                db, card, mode=mode or "standard", sport_mode=mode_lower,
            )
    except Exception as _rec_err:
        logger.warning("record_parlay_shown skipped: %s", _rec_err)
    legacy = payloads[1] if len(payloads) > 1 else payloads[0]
    return {
        "parlay": {
            "legs": legacy["legs"],
            "leg_count": legacy["leg_count"],
            "combined_decimal_odds": legacy["combined_decimal_odds"],
            "combined_american_odds": legacy["combined_american_odds"],
            "combined_win_probability": legacy["survival_pct"],
            "payout_on_100": legacy["payout_on_100"],
            "profit_on_100": legacy["profit_on_100"],
        },
        "parlays": payloads,
        "rank": rank,
        "locked_ids": [p.get("id") for p in locked_picks],
        "window_hours": window_hours,
        "auto_expanded_to": auto_expanded_to,
        "sport_mode": mode_lower,
    }
