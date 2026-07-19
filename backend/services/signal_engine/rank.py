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

    # ── Non-blocking lock acquisition (2026-07-18) ─────────────────
    # iter78 flagged 5.8s p99 under 5-concurrent load — the lock
    # serialized ALL background refreshes on a single date, so
    # concurrent /picks/today handlers piled up waiting for their
    # turn. Since the caller is already fire-and-forget in
    # picks_routes.py and the persisted ranks stay valid across
    # refreshes, a second caller that finds the lock held can just
    # exit cleanly — one refresh per TTL window is sufficient and
    # any concurrent caller can safely no-op instead of blocking on
    # the pool.
    if not force and _LOCK.locked():
        return {"ok": True, "cached": False, "skipped": "another_refresh_in_flight"}

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
                "lock_score_peak": 1, "lock_score_raw": 1,
                "always_starter_floor_applied": 1,
                "always_starter_name": 1,
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
                # Elite-player conviction inputs (2026-07-18):
                # `services.signal_engine.engine.compute_signals` reads
                # these when applying the star-player signal floor —
                # without them in the projection, Mbappe / Kane / etc.
                # look "non-elite" to the rank pass even though the
                # elite_players.py pipeline tagged them.
                # (`player_name` is already projected above.)
                "is_elite": 1, "elite_boost": 1, "elite_striker": 1,
                "player_tags": 1,
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

        # 1) Ensure every pick has a raw signal computed. `compute_signals`
        #    is idempotent within `_REFRESH_SECS` (default ~1h) — it
        #    short-circuits when a fresh `signal_engine` block already
        #    exists. That's the right default for the request path but
        #    means slate-wide rank refreshes cannot pick up newly-added
        #    engine logic (e.g. the 2026-07-18 elite-player floor)
        #    until every cached block ages out. Force a fresh
        #    recompute here by dropping the stale `signal_engine`
        #    block first — cheap because we're already re-scoring
        #    the whole slate and the calculators run in-memory.
        n_computed_raw = 0
        for p in picks:
            had_before = isinstance(p.get("signal_engine"), dict) and \
                         p["signal_engine"].get("score") is not None
            p.pop("signal_engine", None)   # force compute_signals to rescore
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
        #
        # ── Per-sport ranking (2026-07-17 v2) ────────────────────────
        # User feedback: "for tennis you only putting bad signal on
        # picks not the good one". Root cause: a global slate-wide
        # rank punished tennis picks unfairly because many tennis
        # picks (moneylines with no player_form / injury data) get
        # raw=50 neutral, while MLB pitchers get raw=60-80 from rich
        # matchup data. Ranking them TOGETHER meant every tennis
        # pick ended up in the bottom 20-40% of the slate — even
        # the genuine "good" ones (Sinner favorites, etc.).
        #
        # Fix: rank WITHIN each sport bucket. Tennis's best picks
        # now score 90+, MLB's best score 90+, Soccer's best score
        # 90+ — the slider is meaningful per-sport instead of
        # cross-penalising smaller / lower-signal-data leagues.
        # Sports with <5 picks fall back to a shared "misc" bucket
        # so a lone UFC pick doesn't automatically become the
        # top-of-slate (n=1 → 60 by default) or the bottom-of-slate.
        MIN_BUCKET = 5
        # Group indexes by sport for grouped ranking.
        sport_buckets: dict[str, list[int]] = {}
        misc_indices: list[int] = []
        raw_scores: list[float] = []
        for i, p in enumerate(picks):
            raw = p.get("signal_score_raw")
            if raw is None:
                raw = p.get("signal_score")
            try:
                raw_scores.append(float(raw) if raw is not None else 50.0)
            except (TypeError, ValueError):
                raw_scores.append(50.0)
        # First pass: bucket by sport
        by_sport: dict[str, list[int]] = {}
        for i, p in enumerate(picks):
            sport_key = str(p.get("sport") or "Other")
            by_sport.setdefault(sport_key, []).append(i)
        # Second pass: sports with fewer than MIN_BUCKET picks are
        # merged into "_misc" so they don't get degenerate rank distributions.
        for sport_key, idxs in by_sport.items():
            if len(idxs) < MIN_BUCKET:
                misc_indices.extend(idxs)
            else:
                sport_buckets[sport_key] = idxs
        if misc_indices:
            sport_buckets["_misc"] = misc_indices

        # Rank inside each bucket and stitch back to the master array.
        # ── Tie-breaker fix (2026-07-19) ──────────────────────────────
        # User feedback: "everything 71/100 that's good ... before we had
        # 80+ that was hitting". Root cause: on the current slate 67 of
        # 80 Tennis picks share the exact same raw score (56.0) because
        # the tennis calculators can't differentiate doubles picks (no
        # per-player Sackmann / Elo / form data). Percentile ranking
        # averages tied ranks, so those 67 picks ALL collapse to rank 71
        # and no tennis pick ever hits 90+ regardless of lock_score.
        #
        # Fix: add a sub-integer lock_score tie-breaker (~ +0.001 per
        # lock point) to the sort key. Doesn't change which picks are
        # "better" \u2014 raw score still dominates \u2014 but breaks
        # calculator-zero ties by conviction so a lock=93.6 pick ranks
        # above a lock=92.7 pick. Spreads the compressed cluster back
        # across the 20-99 band the user expects.
        ranks: list[int] = [50] * len(picks)
        for sport_key, idxs in sport_buckets.items():
            bucket_scores = []
            for i in idxs:
                base = raw_scores[i]
                # Prefer max(lock_score, lock_score_v2, lock_score_peak)
                # to match the conviction floor already applied in
                # engine.py. Fall back to 50 when none are present.
                lock_vals = []
                for k in ("lock_score", "lock_score_v2", "lock_score_peak"):
                    v = picks[i].get(k)
                    try:
                        if v is not None:
                            lock_vals.append(float(v))
                    except (TypeError, ValueError):
                        pass
                lock_v = max(lock_vals) if lock_vals else 50.0
                # +0.001 per lock point \u2014 large enough to break the
                # tie, small enough that a 3-point raw gap always wins.
                bucket_scores.append(base + lock_v * 0.001)
            bucket_ranks = _percentile_rank(bucket_scores)
            for pos, i in enumerate(idxs):
                ranks[i] = bucket_ranks[pos]

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
