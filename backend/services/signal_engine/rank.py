"""Slate-wide Signal Score percentile ranker.

Problem this solves (2026-07-17, user report "signal filter is there just
no picks"):

    Historical /picks/today decoration only ran the Signal Engine on the
    picks that survived the query filters, so ~65% of today's slate
    (MLB: 5% coverage, Tennis: 26%, UFC: 0%) had NO `signal_score` field
    at all. When the frontend passed `min_signal=N` MongoDB silently
    excluded every doc missing the field, and the board vanished the
    instant the user nudged the slider off zero.

    Compounding the coverage gap, the amplification in `engine.py`
    compressed raw scores into a 45-86 band with a std-dev of 5.6 —
    even for picks that HAD a signal, only 2 picks in 656 ever
    crossed the 70 mark. The slider was functionally dead above ~55.

Fix: at most once every _TTL_SECS per date, sweep every pick for the
given `pick_date`, run `compute_signals` so the raw score exists on
every doc, then re-map `signal_score` to its slate-wide percentile
rank (0-100). This guarantees:

    • 100% coverage — no more silent exclusions.
    • Filter is meaningful at every threshold: top 10% → ≥90,
      top 25% → ≥75, median → ≥50.
    • Missing-field docs (new picks ingested between sweeps) can be
      treated as neutral (50) at query time so they don't disappear.

Persistence writes two fields:
    signal_score       — 0-100 percentile rank (what the filter uses)
    signal_score_raw   — the raw 0-100 amplified score (audit trail)
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from pymongo import UpdateOne

from .engine import compute_signals

logger = logging.getLogger("lockscore.signal_rank")

# Refresh at most once every 3 min per pick_date. Below that, /picks/today
# reuses the previously persisted ranks. The scheduler's own refresh
# cycle also invalidates this cache (see picks_routes / scheduler tasks).
_TTL_SECS = 180
_MAX_CACHE_ENTRIES = 14   # ~2 weeks of pick_date keys; older = evicted
_LAST_RUN: dict[str, float] = {}
_LOCK = asyncio.Lock()


def _prune_cache() -> None:
    """Evict oldest cache entries when we exceed `_MAX_CACHE_ENTRIES`.

    Prevents the module-level `_LAST_RUN` dict from growing indefinitely
    across days/weeks of uptime. Keeps the most-recent 14 pick_date
    keys (older dates are unlikely to be requested again anyway — the
    /picks/today handler only ever asks for the current UTC date).
    """
    if len(_LAST_RUN) <= _MAX_CACHE_ENTRIES:
        return
    # Sort by timestamp (oldest first), evict until we're back under
    # the limit. Cheap because the dict stays small in practice.
    for k in sorted(_LAST_RUN, key=lambda x: _LAST_RUN[x])[: len(_LAST_RUN) - _MAX_CACHE_ENTRIES]:
        _LAST_RUN.pop(k, None)


def _percentile_rank(values: list[float]) -> list[int]:
    """Return the percentile rank (0-100 → visible 20-99) of every value.

    Uses the "average rank on ties" convention so a uniform slate maps
    into a clean spread without gaps. Handles empty / single-item lists
    gracefully.

    ── UX floor (2026-07-17) ────────────────────────────────────────
    A raw percentile rank of 0-100 has a nasty side effect: when ~23%
    of the slate lands on exactly raw=50 (neutral, no component
    deltas), the picks that are just slightly below that pile get
    ranked in the single digits and the UI shows "4/100" or "0/100"
    on picks with strong Lock Score. User feedback 2026-07-17: "I
    see 4/100 on strikeout with high locks score" — reads as
    "signal is broken".

    Fix: map percentile rank into a visible band of [20, 99]. Bottom
    pick of the slate now shows 20 (below-average), the median lands
    at 60 (feels neutral), and the very top pick hits 99 (elite).
    Ordering is preserved so the filter surfaces the same relative
    picks — just with a friendlier scale. Filter thresholds:
        slider ≥ 90 → top ~12%
        slider ≥ 70 → top ~37%
        slider ≥ 50 → top ~62%
    """
    FLOOR = 20
    CEIL = 99
    n = len(values)
    if n == 0:
        return []
    if n == 1:
        return [FLOOR + (CEIL - FLOOR) // 2]
    indexed = sorted(range(n), key=lambda i: values[i])
    # Walk the sorted order and average tied ranks so equal raw scores
    # get equal percentiles.
    ranks: list[float] = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[indexed[j + 1]] == values[indexed[i]]:
            j += 1
        # positions i..j are tied; average their (1-indexed) positions
        avg_pos = (i + j) / 2.0 + 1.0  # +1 for 1-indexed rank
        for k in range(i, j + 1):
            ranks[indexed[k]] = avg_pos
        i = j + 1
    span = float(CEIL - FLOOR)
    scaled = [
        int(round(FLOOR + (r - 1) / (n - 1) * span))
        for r in ranks
    ]
    return scaled


async def refresh_slate_signal_rank(
    db: Any,
    pick_date: str,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Compute + persist percentile-ranked `signal_score` for every pick
    on `pick_date`. Cached per-date with a `_TTL_SECS` TTL; call with
    `force=True` to bypass the cache (used by admin trigger).

    Returns a small stats dict for logging / admin visibility.
    """
    if not pick_date:
        return {"ok": False, "reason": "no_date"}
    now = time.time()
    cached_at = _LAST_RUN.get(pick_date, 0.0)
    if not force and (now - cached_at) < _TTL_SECS:
        return {"ok": True, "cached": True, "age_sec": int(now - cached_at)}

    async with _LOCK:
        # Re-check inside the lock so two concurrent requests don't both
        # rebuild the ranks (thundering-herd guard).
        cached_at = _LAST_RUN.get(pick_date, 0.0)
        if not force and (time.time() - cached_at) < _TTL_SECS:
            return {"ok": True, "cached": True, "age_sec": int(time.time() - cached_at)}

        # Fetch every pick for the slate. We need the raw docs (not just
        # ids) so `compute_signals` has enough context. Keep the
        # projection small — the calculators only look at a fixed set
        # of fields.
        cursor = db.picks.find(
            {"pick_date": pick_date},
            {
                "id": 1, "sport": 1, "market": 1, "selection": 1,
                "player_name": 1, "event": 1, "event_id": 1,
                "book_odds": 1, "win_probability": 1,
                "implied_probability": 1, "edge_percent": 1,
                "lock_score": 1, "lock_score_v2": 1,
                "signal_engine": 1, "signal_score": 1,
                "signal_score_raw": 1,
                "pick_rationale": 1, "player_form": 1,
                "understat_form": 1, "injury_chip": 1,
                "espn_signals": 1, "historical_signal": 1,
                "tennis_deep": 1, "mlb_deep": 1, "soccer_deep": 1,
                "matchup_score": 1, "opportunity_subscore": 1,
                "form_subscore": 1, "historical_subscore": 1,
                "commence_time": 1, "event_time": 1,
                "home_team": 1, "away_team": 1,
                "team": 1, "opponent": 1,
                "is_alt": 1, "line": 1, "grade": 1,
            },
        )
        picks: list[dict] = []
        async for p in cursor:
            picks.append(p)

        n_total = len(picks)
        if n_total == 0:
            _LAST_RUN[pick_date] = time.time()
            _prune_cache()
            return {"ok": True, "n_total": 0}

        # 1) Ensure every pick has a raw signal computed. This is a
        #    no-op for picks that already have a fresh block (idempotent).
        n_computed_raw = 0
        for p in picks:
            had_before = isinstance(p.get("signal_engine"), dict) and \
                         p["signal_engine"].get("score") is not None
            try:
                await compute_signals(db, p)
                if not had_before:
                    n_computed_raw += 1
            except Exception as e:
                logger.debug("compute_signals failed for %s: %s", p.get("id"), e)
                # Fall back to neutral 50 so the pick still participates
                # in the ranking (better than silently excluding it).
                p["signal_score"] = 50
                p["signal_engine"] = {"score": 50, "grade": "Moderate",
                                       "components": [], "why": [],
                                       "breakdown": "",
                                       "computed_at": datetime.now(timezone.utc).isoformat(),
                                       "fallback": True}

        # 2) Rank by raw score. Prefer `signal_score_raw` (post-2026-07-17
        #    split); fall back to `signal_score` for docs decorated by
        #    the older engine. Either way, `signal_score` will be
        #    overwritten below with the percentile rank.
        raw_scores: list[float] = []
        for p in picks:
            raw = p.get("signal_score_raw")
            if raw is None:
                raw = p.get("signal_score")
            try:
                raw_scores.append(float(raw) if raw is not None else 50.0)
            except (TypeError, ValueError):
                raw_scores.append(50.0)
        ranks = _percentile_rank(raw_scores)

        # 3) Persist rank + raw in one bulk_write.
        ops: list[UpdateOne] = []
        for p, raw, rank in zip(picks, raw_scores, ranks):
            if not p.get("id"):
                continue
            ops.append(UpdateOne(
                {"id": p["id"]},
                {"$set": {
                    "signal_score": int(rank),
                    "signal_score_raw": round(float(raw), 2),
                    "signal_rank_computed_at": datetime.now(timezone.utc).isoformat(),
                }},
            ))
        n_persisted = 0
        if ops:
            try:
                res = await db.picks.bulk_write(ops, ordered=False)
                n_persisted = int(getattr(res, "modified_count", 0) or 0)
            except Exception as e:
                logger.warning("signal rank persist failed: %s", e)

        _LAST_RUN[pick_date] = time.time()
        _prune_cache()

        # Simple summary bands for logging so we can eyeball whether the
        # spread looks healthy (top 10% should be ≥90, etc.).
        bands = {"90+": 0, "75+": 0, "50+": 0, "25+": 0}
        for r in ranks:
            if r >= 90: bands["90+"] += 1
            if r >= 75: bands["75+"] += 1
            if r >= 50: bands["50+"] += 1
            if r >= 25: bands["25+"] += 1
        summary = {
            "ok": True,
            "cached": False,
            "n_total": n_total,
            "n_computed_raw": n_computed_raw,
            "n_persisted": n_persisted,
            "bands": bands,
        }
        logger.info("Signal-rank refresh for %s: %s", pick_date, summary)
        return summary


def invalidate(pick_date: str | None = None) -> None:
    """Drop the TTL cache — used by the scheduler after ingestion so the
    next /picks/today re-computes ranks against the freshly-ingested slate.
    """
    if pick_date is None:
        _LAST_RUN.clear()
    else:
        _LAST_RUN.pop(pick_date, None)
