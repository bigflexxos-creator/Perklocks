"""ATD Champion (v1) vs Challenger (v2) — historical backtest harness.

Walks a full REG season game-by-game.  For every player-week combo with
sufficient prior history, computes:

    * v1 (Champion) probability with the multi-stat-block bug fixed.
    * v2 (Challenger) probability from the xTD split-λ model.

Actual outcome:  ``(rushing_tds + receiving_tds) ≥ 1`` in that
player-week.  We record probabilities in memory then compute:

    * Brier score           = mean( (p - y)^2 )
    * Log loss              = mean( -(y·log p + (1-y)·log(1-p)) )
    * Calibration buckets   = actual TD-rate inside [0.0-0.1), [0.1-0.2)…

Run:
    python /app/backend/scripts/atd_backtest.py --season 2024 --limit-weeks 17

No writes to the DB — read-only.
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import sys
from collections import defaultdict
from typing import Optional

sys.path.insert(0, "/app/backend")
from dotenv import load_dotenv  # noqa: E402
load_dotenv("/app/backend/.env")

from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402
from nfl_atd_engine import (  # noqa: E402
    _predict_player_atd_v1,
    _predict_player_atd_v2,
    _LEAGUE_CACHE,
    _LEAGUE_CACHE_V2,
)


BUCKETS = [(0.0, 0.10), (0.10, 0.20), (0.20, 0.30), (0.30, 0.40),
           (0.40, 0.50), (0.50, 0.60), (0.60, 0.70), (0.70, 0.80),
           (0.80, 0.90), (0.90, 1.001)]


def _brier(items: list[tuple[float, int]]) -> float:
    if not items:
        return float("nan")
    return sum((p - y) ** 2 for p, y in items) / len(items)


def _logloss(items: list[tuple[float, int]]) -> float:
    if not items:
        return float("nan")
    eps = 1e-6
    s = 0.0
    for p, y in items:
        p = max(eps, min(1 - eps, p))
        s += -(y * math.log(p) + (1 - y) * math.log(1 - p))
    return s / len(items)


def _calibration(items: list[tuple[float, int]]) -> list[dict]:
    rows = []
    for lo, hi in BUCKETS:
        bucket = [(p, y) for p, y in items if lo <= p < hi]
        if not bucket:
            rows.append({"bucket": f"[{lo:.2f}-{hi:.2f})", "n": 0,
                         "avg_pred": None, "actual_rate": None,
                         "abs_error": None})
            continue
        avg_pred = sum(p for p, _ in bucket) / len(bucket)
        actual = sum(y for _, y in bucket) / len(bucket)
        rows.append({"bucket": f"[{lo:.2f}-{hi:.2f})", "n": len(bucket),
                     "avg_pred": round(avg_pred, 3),
                     "actual_rate": round(actual, 3),
                     "abs_error": round(abs(avg_pred - actual), 3)})
    return rows


def _wilson_ece(items: list[tuple[float, int]]) -> float:
    """Expected calibration error (weighted average |avg_pred − actual|
    across non-empty buckets)."""
    total = 0
    weighted = 0.0
    for row in _calibration(items):
        n = row["n"]
        if not n or row["abs_error"] is None:
            continue
        total += n
        weighted += n * row["abs_error"]
    return weighted / total if total else float("nan")


async def _iter_test_examples(db, season: int, min_week: int,
                              max_week: int, min_history_games: int,
                              per_week_cap: Optional[int]):
    """Yield (player_id, season, week, actual_y) rows to test.

    Only players with ≥ ``min_history_games`` prior REG-season rows and
    at least one of (5 carries OR 3 targets) that week are considered
    candidates.  This mirrors the pipeline's real filtering.
    """
    q = {
        "season": season, "season_type": "REG",
        "week": {"$gte": min_week, "$lte": max_week},
        "position": {"$in": ["RB", "WR", "TE", "QB"]},
        "$or": [{"carries": {"$gte": 5}}, {"targets": {"$gte": 3}}],
    }
    cursor = db.nfl_player_weekly.find(
        q, {"_id": 0, "player_id": 1, "season": 1, "week": 1,
            "rushing_tds": 1, "receiving_tds": 1, "position": 1}
    ).sort([("week", 1)])
    per_week_seen: defaultdict = defaultdict(int)
    async for r in cursor:
        wk = r["week"]
        if per_week_cap and per_week_seen[wk] >= per_week_cap:
            continue
        # Prior history gate — count prior weekly rows.
        n_prior = await db.nfl_player_weekly.count_documents({
            "player_id": r["player_id"], "season_type": "REG",
            "$or": [
                {"season": {"$lt": r["season"]}},
                {"season": r["season"], "week": {"$lt": r["week"]}},
            ],
        })
        if n_prior < min_history_games:
            continue
        rush_td = int(r.get("rushing_tds") or 0)
        rec_td = int(r.get("receiving_tds") or 0)
        y = 1 if (rush_td + rec_td) >= 1 else 0
        per_week_seen[wk] += 1
        yield (r["player_id"], r["season"], r["week"], y)


async def _score_v1_for_cutoff(db, *, player_id: str,
                                 cutoff_season: int, cutoff_week: int) -> Optional[float]:
    """V1 doesn't take a cutoff — we simulate by writing a restricted
    view.  Cheapest path: temporarily filter ``nfl_player_weekly`` at
    query-time.  V1's ``_player_profile_from_weekly`` queries the
    collection directly; we can't easily inject a filter.  So we
    materialise the pre-cutoff rows in memory and pass them through a
    tiny mock ``db`` object exposing only the fields v1's profile
    builder uses.

    NOTE: The v1 impl also queries ``player_game_logs`` FIRST.  For
    backtest purity we DISABLE that path (it doesn't have a cutoff
    concept and would leak future data anyway).  We accomplish this by
    injecting an empty player_game_logs stub.
    """
    class _Cursor:
        def __init__(self, rows): self._rows = rows

        def sort(self, *a, **k): return self

        def limit(self, n):
            self._rows = self._rows[:n]
            return self

        def __aiter__(self):
            async def _gen():
                for r in self._rows:
                    yield r
            return _gen()

    class _Coll:
        def __init__(self, rows): self._rows = rows

        def find(self, q, proj=None):
            return _Cursor(list(self._rows))

        def aggregate(self, *a, **k):  # for _league_means fallback
            return _Cursor([])

        async def find_one(self, q, **kw):
            return self._rows[0] if self._rows else None

        async def count_documents(self, q):
            return len(self._rows)

    # Pre-cutoff rows for THIS player.
    rows = [d async for d in db.nfl_player_weekly.find({
        "player_id": player_id, "season_type": "REG",
        "$or": [
            {"season": {"$lt": cutoff_season}},
            {"season": cutoff_season, "week": {"$lt": cutoff_week}},
        ],
    }, {"_id": 0}).sort([("season", -1), ("week", -1)]).limit(40)]
    if len(rows) < 5:
        return None

    class _MockDB:
        def __init__(self, weekly_rows, real_db):
            self.nfl_player_weekly = _Coll(weekly_rows)
            self.player_game_logs = _Coll([])  # force v1 into weekly path
            self.games = real_db.games  # for _league_means opp lookup

    mock = _MockDB(rows, db)
    # V1 uses _LEAGUE_CACHE at module scope — we reuse the real cache
    # populated by the live db.  Prime it if empty.
    if _LEAGUE_CACHE.get("data") is None:
        try:
            from nfl_atd_engine import _league_means
            await _league_means(db)
        except Exception:
            pass
    out = await _predict_player_atd_v1(mock, player_id=player_id)
    if out.get("reject"):
        return None
    return out.get("td_probability")


async def _score_v2_for_cutoff(db, *, player_id: str,
                                 cutoff_season: int, cutoff_week: int) -> Optional[float]:
    out = await _predict_player_atd_v2(
        db, player_id=player_id,
        cutoff_season=cutoff_season, cutoff_week=cutoff_week,
    )
    if out.get("reject"):
        return None
    return out.get("td_probability")


async def run(season: int, min_week: int, max_week: int,
              min_history_games: int, per_week_cap: Optional[int]):
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = client[os.environ.get("DB_NAME", "test_database")]

    # Prime league caches ONCE using the FULL (post-season) data so
    # both models get league means from the same reference frame.  This
    # is a small optimism bias but symmetric across both models.
    from nfl_atd_engine import _league_means, _league_means_v2
    await _league_means(db)
    await _league_means_v2(db)

    v1_items: list[tuple[float, int]] = []
    v2_items: list[tuple[float, int]] = []
    both_items_v1: list[tuple[float, int]] = []  # only rows scored by BOTH
    both_items_v2: list[tuple[float, int]] = []
    n_examples = 0
    n_v1_scored = 0
    n_v2_scored = 0

    async for pid, ss, wk, y in _iter_test_examples(
        db, season, min_week, max_week, min_history_games, per_week_cap,
    ):
        n_examples += 1
        p1 = await _score_v1_for_cutoff(db, player_id=pid,
                                         cutoff_season=ss, cutoff_week=wk)
        p2 = await _score_v2_for_cutoff(db, player_id=pid,
                                         cutoff_season=ss, cutoff_week=wk)
        if p1 is not None:
            v1_items.append((p1, y))
            n_v1_scored += 1
        if p2 is not None:
            v2_items.append((p2, y))
            n_v2_scored += 1
        if p1 is not None and p2 is not None:
            both_items_v1.append((p1, y))
            both_items_v2.append((p2, y))
        if n_examples % 200 == 0:
            print(f"  … processed {n_examples} candidates "
                  f"(v1 scored {n_v1_scored}, v2 scored {n_v2_scored})")

    print()
    print("=" * 78)
    print(f"ATD CHAMPION (v1) vs CHALLENGER (v2) — season {season} "
          f"weeks {min_week}-{max_week}")
    print("=" * 78)
    print(f"Total candidates examined:  {n_examples}")
    print(f"V1 scored:                  {n_v1_scored}")
    print(f"V2 scored:                  {n_v2_scored}")
    print(f"Both scored (fair set):     {len(both_items_v1)}")
    print()

    def _rate(items):
        return sum(y for _, y in items) / len(items) if items else float("nan")

    print("── Full-set metrics (each model on its own scored rows) ──")
    print(f"  v1 Brier:    {_brier(v1_items):.5f}   log-loss: {_logloss(v1_items):.5f}   ECE: {_wilson_ece(v1_items):.4f}   base-rate: {_rate(v1_items):.3f}")
    print(f"  v2 Brier:    {_brier(v2_items):.5f}   log-loss: {_logloss(v2_items):.5f}   ECE: {_wilson_ece(v2_items):.4f}   base-rate: {_rate(v2_items):.3f}")
    print()
    print("── Fair-set metrics (only rows both models scored) ──")
    print(f"  v1 Brier:    {_brier(both_items_v1):.5f}   log-loss: {_logloss(both_items_v1):.5f}   ECE: {_wilson_ece(both_items_v1):.4f}   base-rate: {_rate(both_items_v1):.3f}")
    print(f"  v2 Brier:    {_brier(both_items_v2):.5f}   log-loss: {_logloss(both_items_v2):.5f}   ECE: {_wilson_ece(both_items_v2):.4f}   base-rate: {_rate(both_items_v2):.3f}")
    print()

    def _print_cal(name, items):
        print(f"  {name} calibration:")
        print(f"    {'bucket':<15} {'n':>5} {'avg_pred':>10} {'actual':>10} {'|err|':>7}")
        for row in _calibration(items):
            ap = f"{row['avg_pred']:.3f}" if row['avg_pred'] is not None else "—"
            ar = f"{row['actual_rate']:.3f}" if row['actual_rate'] is not None else "—"
            er = f"{row['abs_error']:.3f}" if row['abs_error'] is not None else "—"
            print(f"    {row['bucket']:<15} {row['n']:>5} {ap:>10} {ar:>10} {er:>7}")

    print("── Calibration curves (fair set) ──")
    _print_cal("v1", both_items_v1)
    print()
    _print_cal("v2", both_items_v2)
    print()

    v1_b = _brier(both_items_v1)
    v2_b = _brier(both_items_v2)
    verdict = "CHALLENGER WINS" if v2_b < v1_b else "CHAMPION HOLDS"
    print("=" * 78)
    print(f"VERDICT: {verdict}  (v1 Brier {v1_b:.5f} vs v2 Brier {v2_b:.5f}, "
          f"Δ = {v1_b - v2_b:+.5f})")
    print("=" * 78)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, default=2024)
    ap.add_argument("--min-week", type=int, default=6,
                    help="Skip early weeks where prior history is thin.")
    ap.add_argument("--max-week", type=int, default=18)
    ap.add_argument("--min-history-games", type=int, default=8)
    ap.add_argument("--per-week-cap", type=int, default=None)
    args = ap.parse_args()
    asyncio.run(run(
        season=args.season, min_week=args.min_week,
        max_week=args.max_week, min_history_games=args.min_history_games,
        per_week_cap=args.per_week_cap,
    ))
