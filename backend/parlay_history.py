"""Parlay History — Save-on-Tap parlay tracker.

User taps "Save" on a generated parlay card → we persist it in
`parlay_history`. The settler resolves each saved parlay's status on
every settlement pass. Frontend can then list won / live / lost
parlays per user.

Collection schema (`parlay_history`):
  {
    id:           "p_<hash>",                # deterministic from leg set
    user_id:      "<userId>",                # owner
    created_at:   ISO 8601 UTC,
    mode:         "standard" | "advanced" | "high_risk" | "today",
    leg_ids:      [pick_id, pick_id, ...],
    legs:         [snapshot of pick objs],   # frozen at save time
    combined_odds: int (American moneyline),
    stake:        float (optional, default $1 unit),
    status:       "live" | "won" | "lost",
    legs_won:     int,
    legs_lost:    int,
    legs_pending: int,
    settled_at:   ISO or null,
    payout:       float or null,             # filled when won
  }
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("lockscore.parlay_history")


def _parlay_id(user_id: str, leg_ids: list[str]) -> str:
    """Deterministic id from sorted leg pick ids — prevents dupes if a
    user taps Save twice on the same combo."""
    raw = f"{user_id}|" + "|".join(sorted(leg_ids))
    return "p_" + hashlib.sha1(raw.encode()).hexdigest()[:14]


def _american_combine(odds: list[int]) -> int:
    """Combine American moneyline odds into one parlay number."""
    if not odds:
        return 0
    # Convert each to decimal, multiply, convert back to American.
    dec = 1.0
    for a in odds:
        if a >= 100:
            dec *= 1.0 + (a / 100.0)
        else:
            dec *= 1.0 + (100.0 / abs(a))
    if dec >= 2.0:
        return round((dec - 1.0) * 100.0)
    return -round(100.0 / (dec - 1.0))


def _payout_per_unit(combined_odds: int, stake: float = 1.0) -> float:
    """Profit (not return) on a 1-unit stake."""
    if not combined_odds:
        return 0.0
    if combined_odds >= 100:
        return round(stake * combined_odds / 100.0, 2)
    return round(stake * 100.0 / abs(combined_odds), 2)


async def save_parlay(db, *, user_id: str, legs: list[dict],
                       mode: str = "standard",
                       stake: float = 1.0) -> dict:
    """Persist a user-tapped parlay. Idempotent (same legs → same id)."""
    if not legs or len(legs) < 2:
        raise ValueError("parlay must have at least 2 legs")
    leg_ids = [str(p.get("id") or p.get("pick_id")) for p in legs if (p.get("id") or p.get("pick_id"))]
    if len(leg_ids) != len(legs):
        raise ValueError("every leg must have an id")
    pid = _parlay_id(user_id, leg_ids)
    existing = await db.parlay_history.find_one({"id": pid})
    if existing:
        return existing  # idempotent
    odds = [int(p.get("book_odds") or 0) for p in legs]
    combined = _american_combine(odds)
    # Snapshot only the fields we'll display so we don't drag huge pick docs.
    leg_snapshots = []
    for p in legs:
        leg_snapshots.append({
            "pick_id": str(p.get("id") or p.get("pick_id")),
            "sport": p.get("sport"),
            "league": p.get("league"),
            "event": p.get("event"),
            "event_time": p.get("event_time"),
            "market": p.get("market"),
            "selection": p.get("selection"),
            "book_odds": int(p.get("book_odds") or 0),
            "lock_score": p.get("lock_score"),
            "status": "pending",  # filled later by resolver
        })
    now = datetime.now(timezone.utc).isoformat()
    doc = {
        "id": pid,
        "user_id": user_id,
        "created_at": now,
        "mode": mode,
        "leg_ids": leg_ids,
        "legs": leg_snapshots,
        "combined_odds": combined,
        "stake": stake,
        "status": "live",
        "legs_won": 0,
        "legs_lost": 0,
        "legs_pending": len(leg_ids),
        "settled_at": None,
        "payout": None,
    }
    await db.parlay_history.insert_one(doc)
    logger.info("Saved parlay %s for user %s (%d legs, %+d)",
                pid, user_id, len(leg_ids), combined)
    return doc


async def resolve_saved_parlays(db) -> dict:
    """Walk all `live` parlays and update each leg's status from picks.

    Status logic:
      • Any leg `lost` or `void` (anything not won/pending) → parlay lost
      • All legs `won` → parlay won (compute payout)
      • Otherwise → still live

    Pick resilience: legs are first looked up by `pick_id`. If that pick has
    been deleted (e.g. by a dedup or alt-line cleanup), we FALL BACK to an
    identity match on the snapshotted (sport, event, market, selection)
    stored at save time. This makes parlay history resilient to pick churn
    — a leg can't get stuck "pending" forever just because its source
    pick was wiped from the picks collection.

    Scope (2026-07-04 fix): this resolver is now scoped to USER-SAVED
    parlays only (docs with `user_id` set — the Save-on-Tap flow). The
    auto-recorded `plearn_*` docs from `parlay_learning` use a different
    leg schema (`legs[i].pick_id`, no top-level `leg_ids`) and are
    settled by `parlay_learning.settle_parlays`. Previously the two
    resolvers fought over the same collection and any `plearn_*` doc
    with an empty `leg_ids` list was falsely marked WON because
    `wins == len(leg_ids) == 0` short-circuited the "all-legs-won"
    branch — corrupting the synergy learning feedback loop.
    """
    won = lost = updated = 0
    now = datetime.now(timezone.utc).isoformat()
    # ── Scope guardrail ── this collection is shared with parlay_learning
    # for its `plearn_*` auto-recorded rows. Only touch user-saved rows
    # (identified by `user_id` presence).
    cursor = db.parlay_history.find({
        "user_id": {"$exists": True, "$ne": None, "$ne": ""},
        "$or": [
            {"status": "live"},
            {"legs": {"$elemMatch": {"status": {"$in": [None, "pending", "live"]}}}},
        ],
    })
    async for parlay in cursor:
        leg_ids = parlay.get("leg_ids") or []
        legs_view = parlay.get("legs") or []
        # Empty-legs guardrail — never mark a legless parlay "won".
        # (Historic bug: 156 plearn_* rows got status="won" because
        # wins==len(leg_ids)==0 was true. Now guarded here too as a
        # defence-in-depth even though we already filtered by user_id.)
        if not leg_ids or len(leg_ids) < 2:
            continue

        # Phase 1: direct lookup by pick_id
        picks = await db.picks.find(
            {"id": {"$in": leg_ids}}, {"id": 1, "status": 1}
        ).to_list(length=len(leg_ids))
        status_by_id = {p["id"]: p.get("status") for p in picks}

        # Phase 2: for legs whose pick_id was deleted, fall back to
        # identity match against the leg's stored snapshot.
        # Phase 3 (added 2026-06-23 — user bug "Bets In parlay tab not
        # grading"): if BOTH the pick row is missing AND the snapshot
        # identity match returns nothing, settle directly from the
        # external game result (MLB Stats API for MLB, cached soccer
        # match results for Soccer). This is the only way to recover
        # legs whose source picks were wiped before the settler could
        # mark them won/lost. See `parlay_leg_settle.py`.
        leg_status: list[str] = []
        rescued = 0
        externally_settled = 0
        for i, lid in enumerate(leg_ids):
            s = status_by_id.get(lid)
            if s in ("won", "lost", "void", "push"):
                leg_status.append(s)
                continue
            if s is None or s == "pending":
                # Pick missing or still pending — try the snapshot fallback.
                snap = legs_view[i] if i < len(legs_view) else {}
                # ── EVENT-TIME PROXIMITY GUARD (2026-07-04 fix) ──
                # Same-teams matchups recur across dates (e.g. Yankees @
                # Red Sox play a 3-game series; Humbert vs Bergs plays on
                # 06-27 AND 06-28). Without a date filter the snapshot
                # match happily grabs the older completed game and
                # inflates a future leg to WON. Require the candidate
                # pick to fall within ±36h of the leg's stored event
                # time. If the leg has no event_time we can't safely
                # snapshot-match — leave pending.
                snap_time = snap.get("event_time")
                if not snap_time:
                    leg_status.append(s or "pending")
                    continue
                # Bail out if the event is still in the future — a
                # future game CANNOT already be won/lost regardless of
                # what any other pick says.
                if snap_time > now:
                    leg_status.append(s or "pending")
                    continue
                # ±36 h window around the stored event_time.
                from datetime import timedelta as _td
                try:
                    snap_dt = datetime.fromisoformat(snap_time.replace("Z", "+00:00"))
                except Exception:
                    leg_status.append(s or "pending")
                    continue
                lo = (snap_dt - _td(hours=36)).isoformat()
                hi = (snap_dt + _td(hours=36)).isoformat()
                match_q = {
                    "sport": snap.get("sport"),
                    "event": snap.get("event"),
                    "market": snap.get("market"),
                    "status": {"$in": ["won", "lost", "void", "push"]},
                    "event_time": {"$gte": lo, "$lte": hi},
                }
                # Selection narrows when present (helps with multi-pick events)
                if snap.get("selection"):
                    match_q["selection"] = snap.get("selection")
                if not all(match_q.get(k) for k in ("sport", "event", "market")):
                    leg_status.append(s or "pending")
                    continue
                match = await db.picks.find_one(match_q, {"status": 1})
                if match:
                    leg_status.append(match["status"])
                    rescued += 1
                    continue
                # Phase 3: external settlement adapter
                try:
                    from parlay_leg_settle import try_settle_leg_externally
                    ext_status = await try_settle_leg_externally(snap)
                except Exception as e:
                    logger.warning("External leg settle failed: %s", e)
                    ext_status = None
                if ext_status in ("won", "lost", "void", "push"):
                    leg_status.append(ext_status)
                    externally_settled += 1
                else:
                    leg_status.append(s or "pending")
            else:
                leg_status.append(s or "pending")

        if rescued or externally_settled:
            logger.info(
                "Parlay %s: rescued %d via snapshot, %d via external settle",
                parlay.get("id", "?")[:8], rescued, externally_settled,
            )

        # Update snapshot leg statuses for display.
        for i, lid in enumerate(leg_ids):
            if i < len(legs_view):
                legs_view[i]["status"] = leg_status[i]
        wins = sum(1 for s in leg_status if s == "won")
        loss = sum(1 for s in leg_status if s in ("lost", "void"))
        pending = sum(1 for s in leg_status if s not in ("won", "lost", "void", "push"))
        new_status = parlay["status"]
        settled_at = parlay.get("settled_at")
        payout = parlay.get("payout")
        if loss > 0:
            new_status = "lost"
            settled_at = now
            payout = 0.0
            lost += 1
        elif pending == 0 and wins == len(leg_ids):
            new_status = "won"
            settled_at = now
            payout = _payout_per_unit(parlay.get("combined_odds") or 0,
                                       parlay.get("stake") or 1.0)
            won += 1
        # Only persist if something changed.
        if (new_status != parlay["status"]
            or wins != parlay.get("legs_won")
            or loss != parlay.get("legs_lost")
            or pending != parlay.get("legs_pending")):
            await db.parlay_history.update_one(
                {"id": parlay["id"]},
                {"$set": {
                    "status": new_status,
                    "legs_won": wins,
                    "legs_lost": loss,
                    "legs_pending": pending,
                    "legs": legs_view,
                    "settled_at": settled_at,
                    "payout": payout,
                }},
            )
            updated += 1
    if won or lost:
        logger.info("Parlay resolver: %d updated, %d won, %d lost",
                    updated, won, lost)
    return {"updated": updated, "won": won, "lost": lost}


async def list_history(db, *, user_id: str, status_filter: Optional[str] = None,
                        limit: int = 50) -> list[dict]:
    q: dict = {"user_id": user_id}
    if status_filter and status_filter != "all":
        q["status"] = status_filter
    cursor = db.parlay_history.find(q, {"_id": 0}).sort("created_at", -1).limit(limit)
    rows = await cursor.to_list(length=limit)
    # Inject the live cash-out estimate for still-live tickets. Terminal
    # tickets (won/lost/push) don't need it — payout/loss already known.
    for r in rows:
        if r.get("status") == "live":
            try:
                r["cashout_estimate"] = _cashout_estimate(r)
            except Exception:
                r["cashout_estimate"] = None
        else:
            r["cashout_estimate"] = None
    return rows


async def get_parlay(db, *, user_id: str, parlay_id: str) -> Optional[dict]:
    return await db.parlay_history.find_one(
        {"id": parlay_id, "user_id": user_id}, {"_id": 0}
    )


async def delete_parlay(db, *, user_id: str, parlay_id: str) -> bool:
    r = await db.parlay_history.delete_one(
        {"id": parlay_id, "user_id": user_id}
    )
    return r.deleted_count > 0


# ─────────────────────── Cash-out estimator ────────────────────────────
# Live cash-out calc used by the parlay-history endpoint. We do NOT
# call live sportsbook APIs (would require paid market data); instead we
# compute a *fair-value* estimate from the leg snapshot odds and each
# leg's current live status. This gives users a directionally-correct
# "what's this ticket worth right now?" number without introducing an
# external dependency.
#
# Math:
#   fair_value = stake × decimal(combined_odds) × prod( implied(leg_odds)
#                                                        for leg in pending)
#   Won legs contribute a multiplier of 1.0 (locked in). Lost legs collapse
#   the whole ticket to 0. Push legs are treated as no-action (multiplier
#   1.0, and the parlay's effective decimal odds shrink accordingly, but
#   we don't recompute the combined here — the current combined_odds
#   value already reflects the original leg set, so we conservatively
#   leave the multiplier at 1.0 for pushes).
# Book hold: real books charge ~5-10 % on cash-out. We apply a 0.93
# multiplier so the number the user sees is on the conservative side of
# what a real book would offer.

CASHOUT_BOOK_HOLD = 0.93


def _american_to_decimal(american: int) -> float:
    if american == 0:
        return 1.0
    if american >= 100:
        return 1.0 + (american / 100.0)
    return 1.0 + (100.0 / abs(american))


def _american_to_implied(american: int) -> float:
    """American odds → implied probability (0..1)."""
    if american == 0:
        return 0.0
    if american > 0:
        return 100.0 / (american + 100.0)
    return abs(american) / (abs(american) + 100.0)


def _cashout_estimate(parlay: dict) -> Optional[float]:
    """Return the estimated cash-out value (fair value × book hold) in
    the same units as `stake` (typically 1u = $1). Returns None if the
    parlay isn't in a state where cash-out makes sense (already
    finalised or missing data)."""
    status = parlay.get("status")
    if status in ("won", "lost", "push"):
        return None
    stake = float(parlay.get("stake") or 1.0)
    combined = int(parlay.get("combined_odds") or 0)
    if not combined:
        return None
    legs = parlay.get("legs") or []
    if not legs:
        return None
    # If any leg has already lost / voided, ticket is dead — cash-out $0.
    for L in legs:
        if L.get("status") in ("lost", "void"):
            return 0.0
    # Compute pending-legs implied product.
    prob = 1.0
    for L in legs:
        s = L.get("status")
        if s == "won":
            continue  # locked in, contributes 1.0
        # pending / None → include implied prob
        try:
            american = int(L.get("book_odds") or 0)
        except (TypeError, ValueError):
            return None
        if not american:
            return None
        prob *= _american_to_implied(american)
    # Full return if won → stake × decimal(combined).
    payout_if_won = stake * _american_to_decimal(combined)
    fair_value = payout_if_won * prob
    return round(fair_value * CASHOUT_BOOK_HOLD, 2)


async def resettle_parlay(db, *, user_id: str, parlay_id: str) -> dict:
    """Force-run the resolver for a single parlay, walking picks →
    snapshot-match → external-adapter chain. Returns the updated
    document. Useful when the periodic loop hasn't picked it up yet or
    the user tapped "Force re-settle".
    """
    parlay = await db.parlay_history.find_one(
        {"id": parlay_id, "user_id": user_id}
    )
    if not parlay:
        raise ValueError("parlay not found")
    # Reuse the main resolver but scope it to this single parlay by
    # marking it as pending first (idempotent — it just re-checks).
    from datetime import datetime, timezone as _tz
    now = datetime.now(_tz.utc).isoformat()

    leg_ids = parlay.get("leg_ids") or []
    legs_view = parlay.get("legs") or []
    # Same body as `resolve_saved_parlays`, inlined for this one row so
    # we can return the fresh state without an extra round-trip.
    picks = await db.picks.find(
        {"id": {"$in": leg_ids}}, {"id": 1, "status": 1}
    ).to_list(length=len(leg_ids))
    status_by_id = {p["id"]: p.get("status") for p in picks}
    leg_status: list[str] = []
    from datetime import timedelta as _td
    for i, lid in enumerate(leg_ids):
        s = status_by_id.get(lid)
        if s in ("won", "lost", "void", "push"):
            leg_status.append(s)
            continue
        snap = legs_view[i] if i < len(legs_view) else {}
        # Event-time proximity guard (see resolve_saved_parlays comment).
        snap_time = snap.get("event_time")
        match = None
        if snap_time and snap_time <= now:
            try:
                snap_dt = datetime.fromisoformat(snap_time.replace("Z", "+00:00"))
                lo = (snap_dt - _td(hours=36)).isoformat()
                hi = (snap_dt + _td(hours=36)).isoformat()
                match_q = {
                    "sport": snap.get("sport"),
                    "event": snap.get("event"),
                    "market": snap.get("market"),
                    "status": {"$in": ["won", "lost", "void", "push"]},
                    "event_time": {"$gte": lo, "$lte": hi},
                }
                if snap.get("selection"):
                    match_q["selection"] = snap.get("selection")
                if all(match_q.get(k) for k in ("sport", "event", "market")):
                    match = await db.picks.find_one(match_q, {"status": 1})
            except Exception:
                match = None
        if match:
            leg_status.append(match["status"])
            continue
        # External adapter chain
        try:
            from parlay_leg_settle import try_settle_leg_externally
            ext_status = await try_settle_leg_externally(snap)
        except Exception as e:
            logger.warning("Force-resettle external failure: %s", e)
            ext_status = None
        leg_status.append(ext_status or (s or "pending"))
    # Roll up
    for i in range(min(len(legs_view), len(leg_status))):
        legs_view[i]["status"] = leg_status[i]
    wins = sum(1 for s in leg_status if s == "won")
    loss = sum(1 for s in leg_status if s in ("lost", "void"))
    pending = sum(1 for s in leg_status if s not in ("won", "lost", "void", "push"))
    new_status = parlay.get("status") or "live"
    settled_at = parlay.get("settled_at")
    payout = parlay.get("payout")
    if loss > 0:
        new_status = "lost"
        settled_at = now
        payout = 0.0
    elif pending == 0 and wins == len(leg_ids) and wins > 0:
        new_status = "won"
        settled_at = now
        payout = _payout_per_unit(
            parlay.get("combined_odds") or 0,
            parlay.get("stake") or 1.0,
        )
    await db.parlay_history.update_one(
        {"id": parlay_id, "user_id": user_id},
        {"$set": {
            "status": new_status,
            "legs_won": wins,
            "legs_lost": loss,
            "legs_pending": pending,
            "legs": legs_view,
            "settled_at": settled_at,
            "payout": payout,
            "last_resettled_at": now,
        }},
    )
    doc = await db.parlay_history.find_one(
        {"id": parlay_id, "user_id": user_id}, {"_id": 0}
    )
    return doc or {}
