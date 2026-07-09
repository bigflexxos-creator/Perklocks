"""Settlement-time Rollover History Tagging
=========================================

Purpose
-------
Stamps `on_rollover_at` on every pick that WAS actually on the /picks/
rollover top-3 board when the day was live.  Runs at settlement time,
after outcomes have been graded — so History → Rollover shows the
exact same picks the user saw earn wins/losses that day, not an
approximation based on later thresholds.

Why post-settlement is the right place
--------------------------------------
`board_validator.tag_rollover_picks` (the original approach) runs at
PICK GENERATION time and uses simple thresholds (lock ≥ 95, wp ≥ 0.80,
edge ≥ 4.0).  Those thresholds match a superset of what /picks/
rollover actually surfaced — the real rollover board applies V4 logic
(top-3 per day, market blacklist, quality gate, dead-zone exclusions).
Result: History → Rollover always showed too many picks (see user
complaint 2026-07-08: "picks in rollover sections are regular picks…
showing MLB alt totals when they wasn't on rollover").

By re-deriving the V4 top-3 from the FROZEN historical slate of each
graded date, we deterministically identify the exact 3 picks the user
saw on their Rollover tab — and stamp the tag onto ONLY those.

Idempotence
-----------
Runs once per date.  If a pick already has `on_rollover_at` we skip
it.  Dates with < 5 graded picks are skipped (probably an outage day
with no board activity).

Callers
-------
- `settlement_engine.settle_due_picks()` — post-settlement tail.
- `POST /admin/rollover/backfill-tags` — one-off catch-up for old
  dates the daily job never covered.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("lockscore.rollover_history_tagger")


# ── V4 rollover gate constants (mirror picks_routes.pick_rollover) ──
LOCK_FLOOR: float = 89.0
LOCK_DEAD_LO: float = 80.0
LOCK_DEAD_HI: float = 85.0
WP_FLOOR: float = 0.60
EDGE_FLOOR: float = 0.0
EDGE_CAP: float = 12.0
CHALK_CAP: float = -350.0
ODDS_DEAD_LO: float = -140.0
ODDS_DEAD_HI: float = -110.0

# Same market blacklist as /picks/rollover V4 (see picks_routes.py).
import re
_BLACKLIST_RE = re.compile(
    r"goal scorer|to score or assist|score or assist"
    r"|score and assist|score & assist"
    r"|to score 2|to score 3"
    r"|hat.?trick|first goal|last goal|winning goal|to assist"
    r"|nrfi|yrfi"
    r"|hits\s*\+\s*runs\s*\+\s*rbi|h\+r\+rbi|hits, runs.+rbi",
    re.I,
)

# Market whitelist multipliers (mirror /picks/rollover — kept in sync
# via docstring but not runtime-imported so this helper stays cheap).
_MARKET_BONUSES: tuple[tuple[re.Pattern, float], ...] = (
    (re.compile(r"win\s*or\s*draw|dnb", re.I),                 1.15),
    (re.compile(r"strikeouts?", re.I),                          1.10),
    (re.compile(r"moneyline", re.I),                            1.05),
    (re.compile(r"total\s*goals?", re.I),                       1.05),
    (re.compile(r"run\s*line|spread", re.I),                    1.02),
)


def _norm_prob(v: Any) -> float:
    try:
        f = float(v or 0)
    except Exception:
        return 0.0
    return f / 100.0 if f > 1.0 else f


def _passes_v4(p: dict) -> bool:
    """Same gate as /picks/rollover V4."""
    lock = float(p.get("lock_score") or 0)
    odds = float(p.get("book_odds") or -9999)
    edge = float(p.get("edge_percent") or 0)
    wp = _norm_prob(p.get("win_probability"))
    if lock < LOCK_FLOOR:
        return False
    if LOCK_DEAD_LO <= lock < LOCK_DEAD_HI:
        return False
    if wp < WP_FLOOR:
        return False
    if edge < EDGE_FLOOR:
        return False
    if edge > EDGE_CAP:
        return False
    if odds < CHALK_CAP:
        return False
    if ODDS_DEAD_LO <= odds < ODDS_DEAD_HI:
        return False
    market = p.get("market") or ""
    if _BLACKLIST_RE.search(market):
        return False
    if float(p.get("edge_percent") or 0) < 0:
        return False
    return True


def _composite_score(p: dict) -> float:
    """Same composite ranking used in /picks/rollover.
       0.55·wp + 0.20·sim + 0.15·edge_norm + 0.10·alt_bonus, then
       multiplied by market bonus and (optional) chalk penalty.
    """
    wp = _norm_prob(p.get("win_probability"))
    sim = float(p.get("simulation_win_rate") or wp * 100) / 100.0
    edge = float(p.get("edge_percent") or 0)
    edge_norm = min(edge / 10.0, 1.0)  # clip at 10% for ranking
    alt_bonus = 0.05 if p.get("is_alt") else 0.0
    composite = 0.55 * wp + 0.20 * sim + 0.15 * edge_norm + 0.10 * alt_bonus
    # Market multiplier
    market = p.get("market") or ""
    mult = 1.0
    for pat, m in _MARKET_BONUSES:
        if pat.search(market):
            mult = max(mult, m)  # highest matching multiplier wins
    return composite * mult


def _top_three_for_slate(picks: list[dict]) -> list[dict]:
    """Return the 3 highest-composite picks that pass V4 — one per event.

    Matches the "one leg per game" rule from /picks/rollover so two
    slaps at the same MLB Team Total (Over 3.5 + Under 5.5) don't
    consume both slots.
    """
    filtered = [p for p in picks if _passes_v4(p)]
    filtered.sort(key=_composite_score, reverse=True)
    top: list[dict] = []
    seen_events: set[str] = set()
    for p in filtered:
        ev = (p.get("event") or "").lower().strip()
        if ev in seen_events:
            continue
        top.append(p)
        seen_events.add(ev)
        if len(top) >= 3:
            break
    return top


async def stamp_rollover_history_tags(db, dates: list[str] | None = None) -> dict:
    """Recompute the V4 top-3 rollover slate for each date and stamp
    `on_rollover_at` onto ONLY those 3 picks.

    `dates` is a list of ISO YYYY-MM-DD strings.  If None, every date
    with graded picks in the last 60 days is processed.

    Returns:
      {"dates_processed": N, "picks_tagged": M, "picks_untagged": K}

    Untagged count is > 0 when a pick previously had the tag but is
    no longer in the top-3 (e.g. because a settlement fixed the win
    probability upstream).  This means the function is fully
    reconstructive — safe to re-run any time.
    """
    from itertools import groupby

    if dates is None:
        # Rebuild the tag for the last 60 days by default.
        pipeline = [
            {"$match": {
                "status": {"$in": ["won", "lost", "push"]},
                "pick_date": {"$exists": True, "$ne": None},
            }},
            {"$group": {"_id": "$pick_date"}},
            {"$sort": {"_id": -1}},
            {"$limit": 60},
        ]
        agg = await db.picks.aggregate(pipeline).to_list(60)
        dates = [row["_id"] for row in agg]

    now = datetime.now(timezone.utc).isoformat()
    tagged = 0
    untagged = 0
    dates_processed = 0

    for date in dates:
        # Pull the *entire* slate for that date — same query surface
        # /picks/rollover uses at generation time.
        slate = await db.picks.find({
            "pick_date": date,
            "no_bet": {"$ne": True},
            "edge_percent": {"$gte": 0},
        }, {"_id": 0}).to_list(length=1500)
        if len(slate) < 5:
            continue
        top_ids = {p["id"] for p in _top_three_for_slate(slate) if p.get("id")}
        # Clear tags on picks that AREN'T in the top-3.
        clear = await db.picks.update_many(
            {"pick_date": date, "id": {"$nin": list(top_ids)},
             "on_rollover_at": {"$exists": True}},
            {"$unset": {"on_rollover_at": ""}},
        )
        untagged += clear.modified_count
        # Set the tag on the top-3.
        if top_ids:
            set_res = await db.picks.update_many(
                {"pick_date": date, "id": {"$in": list(top_ids)},
                 "on_rollover_at": {"$exists": False}},
                {"$set": {"on_rollover_at": now}},
            )
            tagged += set_res.modified_count
        dates_processed += 1

    logger.info(
        "Rollover-history tag stamp complete — dates=%d tagged=%d cleared=%d",
        dates_processed, tagged, untagged,
    )
    return {
        "dates_processed": dates_processed,
        "picks_tagged": tagged,
        "picks_untagged": untagged,
    }
