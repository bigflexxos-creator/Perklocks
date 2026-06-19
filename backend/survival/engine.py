"""Conditional-hit-rate engine.

Pure functions — no I/O. Take the already-fetched game logs and
produce a ranked list of candidate hitters whose hits historically
co-occurred with the primary's MISS games.

Formula (from spec):
  conditional_hit_rate =
      other_hit_when_H_missed / games_where_H_missed

  score = recent10 * 0.50 + recent30 * 0.30 + season * 0.20

Guard rails (from spec):
  • Skip if games_where_H_missed < 5 (overfit prevention)
  • Streak display capped at recent 10
  • Never emit 100% language — max label is "Strong cover (5/5)".
"""
from __future__ import annotations

from typing import Any

MIN_MISS_SAMPLE = 5
STREAK_CAP      = 10


def _index_by_date(log: list[dict]) -> dict[str, dict]:
    return {row["date"]: row for row in log if row.get("qualifying")}


def _miss_dates(primary_log: list[dict]) -> list[str]:
    """Return dates the primary hitter PLAYED but recorded 0 hits.

    Most-recent-first ordering preserved.
    """
    return [r["date"] for r in primary_log
            if r.get("qualifying") and r.get("hits") == 0]


def _conditional_rate_on(dates: list[str],
                         candidate_index: dict[str, dict]) -> tuple[int, int]:
    """Count (hits_on_dates, games_played_on_dates) for the candidate.

    "games_played" only counts dates where the candidate qualified;
    if they were inactive/IL we drop the date so the denominator is
    fair.
    """
    hit = 0
    n   = 0
    for d in dates:
        row = candidate_index.get(d)
        if not row:
            continue
        n += 1
        if (row.get("hits") or 0) >= 1:
            hit += 1
    return hit, n


def rank_candidates(primary_log: list[dict],
                    candidates: list[dict]) -> list[dict]:
    """Rank candidate hitters by composite conditional-hit score.

    `candidates` is a list of {"id", "name", "position", "log":[...]}.
    Returns a list sorted highest score first — already filtered to
    those with sufficient sample.
    """
    miss = _miss_dates(primary_log)
    if len(miss) < MIN_MISS_SAMPLE:
        # Insufficient miss sample globally — caller surfaces a low-data
        # warning instead of suspect rankings.
        return []

    last10 = miss[:10]
    last30 = miss[:30]
    season = miss

    out: list[dict] = []
    for c in candidates:
        idx = _index_by_date(c.get("log") or [])

        h10, n10 = _conditional_rate_on(last10, idx)
        h30, n30 = _conditional_rate_on(last30, idx)
        hsn, nsn = _conditional_rate_on(season, idx)

        # Need at least 5 dates with both players playing for season.
        if nsn < MIN_MISS_SAMPLE:
            continue

        r10 = h10 / n10 if n10 else 0.0
        r30 = h30 / n30 if n30 else 0.0
        rsn = hsn / nsn if nsn else 0.0

        score = r10 * 0.50 + r30 * 0.30 + rsn * 0.20

        streak_str = (
            f"{min(h10, STREAK_CAP)}/{min(n10, STREAK_CAP)}"
            if n10 else f"{hsn}/{nsn}"
        )

        # Anti-overfit labelling — never claim 100%.
        if score >= 0.85 and n10 >= 5:
            label = "🔥 Strong cover"
        elif score >= 0.70:
            label = "🔥 Solid cover"
        elif score >= 0.55:
            label = "✨ Decent cover"
        else:
            label = "— Soft cover"

        out.append({
            "id":         c.get("id"),
            "name":       c.get("name"),
            "position":   c.get("position"),
            "score":      round(score, 4),
            "streak":     streak_str,           # capped at last 10
            "last10":     {"hit": h10, "n": n10, "rate": round(r10, 3)},
            "last30":     {"hit": h30, "n": n30, "rate": round(r30, 3)},
            "season":     {"hit": hsn, "n": nsn, "rate": round(rsn, 3)},
            "label":      label,
        })

    out.sort(key=lambda r: r["score"], reverse=True)
    return out


def survival_index(ranked: list[dict]) -> float:
    """Optional rollup: weighted average of top-5 conditional rates.

    Heavier weight on the very top candidates so a slate full of
    mediocre covers doesn't inflate the index. Returns 0-100.
    """
    if not ranked:
        return 0.0
    top = ranked[:5]
    weights = [0.40, 0.25, 0.15, 0.12, 0.08][: len(top)]
    s = sum(c["score"] * w for c, w in zip(top, weights))
    total_w = sum(weights) or 1.0
    return round((s / total_w) * 100.0, 1)


def reliability(miss_dates: list[str]) -> str:
    """Sample-size confidence label."""
    n = len(miss_dates)
    if n >= 15: return "High Sample"
    if n >= 8:  return "Medium Sample"
    return "Low Sample"
