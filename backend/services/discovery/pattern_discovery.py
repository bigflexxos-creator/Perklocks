"""Pattern Discovery Engine (2026-07-28).

Searches player history for statistically meaningful splits — home
vs away, rest days, recent-form buckets, opponent tiers, etc — that
lift or depress the player's stat output.

**No sportsbook odds.** Every pattern is derived from actual game
outcomes vs the split feature.

Public API
──────────
    patterns = await discover_patterns(
        db, sport="NFL", player="Joe Burrow",
        stat="passing_yards",
        min_samples=8, min_lift_pct=15,
    )

    → list[{
        pattern_id, factor, split, sample_size, hit_rate_over_median,
        avg_stat_split, avg_stat_baseline, lift_pct, wilson_lb,
        confidence, grade, note,
      }]

The engine walks a small, well-known set of factors so results are
interpretable and reproducible.  It does NOT do infinite subgroup
mining (which would produce spurious signals).
"""
from __future__ import annotations

import logging
from typing import Optional

from .confidence_system import (
    wilson_lower_bound, confidence_grade, confidence_label,
    passes_sample_gate,
)

logger = logging.getLogger("lockscore.services.discovery.pattern_discovery")


async def _load_nfl_rows(db, player: str) -> list[dict]:
    q = {"$or": [{"player_display_name": player},
                   {"player_name": player}]}
    return [r async for r in db.nfl_player_weekly.find(q, {"_id": 0})]


def _split_home_away(rows: list[dict]) -> dict[str, list[dict]]:
    home, away, unknown = [], [], []
    for r in rows:
        gid = r.get("game_id") or ""
        team = r.get("team") or ""
        if isinstance(gid, str) and team:
            parts = gid.split("_")
            if len(parts) >= 4:
                if team == parts[-1]:
                    home.append(r); continue
                if team == parts[-2]:
                    away.append(r); continue
        unknown.append(r)
    return {"home": home, "away": away}


def _split_by_rest_days(rows: list[dict]) -> dict[str, list[dict]]:
    """Bucket rest days: short (<7), normal (7), long (>7)."""
    short, normal, long_ = [], [], []
    by_player_season: dict[tuple, list] = {}
    for r in rows:
        by_player_season.setdefault(
            (r.get("player_id"), r.get("season")), [],
        ).append(r)
    for key, gs in by_player_season.items():
        gs.sort(key=lambda r: r.get("week", 0))
        prev_week = None
        for r in gs:
            w = r.get("week") or 0
            if prev_week is not None:
                delta = w - prev_week
                if delta <= 0:
                    prev_week = w; continue
                if delta < 1: prev_week = w; continue
                # Approximate rest in days.
                rd = delta * 7
                if rd < 7:      short.append(r)
                elif rd == 7:   normal.append(r)
                else:           long_.append(r)
            prev_week = w
    return {"short_rest": short, "normal_rest": normal, "long_rest": long_}


def _split_by_opp_tier(rows: list[dict], stat: str) -> dict[str, list[dict]]:
    """Bucket by whether the opponent is a top-10 / middle / bottom-10
    defense against the stat — using the ROW's own opponent_team +
    the LEAGUE-wide allowance ranking in the row's season."""
    # Group all rows by opponent+season to compute allowance.
    by_opp_season: dict[tuple, list] = {}
    for r in rows:
        by_opp_season.setdefault(
            (r.get("opponent_team"), r.get("season")), []
        ).append(r.get(stat, 0) or 0)
    # Convert to average allowance per opp-season, then rank per season.
    per_season_rank: dict[int, dict[str, int]] = {}
    for (opp, season), vals in by_opp_season.items():
        if season is None or not opp:
            continue
        avg = sum(vals) / len(vals) if vals else 0
        per_season_rank.setdefault(season, {})[opp] = avg
    ranks_by_season: dict[int, list[tuple]] = {
        s: sorted(d.items(), key=lambda kv: kv[1])
        for s, d in per_season_rank.items()
    }
    opp_rank: dict[tuple, int] = {}
    for s, ordered in ranks_by_season.items():
        for i, (opp, _) in enumerate(ordered):
            opp_rank[(s, opp)] = i + 1
    total_per_season = {s: len(v) for s, v in ranks_by_season.items()}
    top, mid, bot = [], [], []
    for r in rows:
        s = r.get("season"); opp = r.get("opponent_team")
        rank = opp_rank.get((s, opp))
        n_teams = total_per_season.get(s, 32)
        if rank is None:
            continue
        if rank <= max(3, n_teams // 3):
            top.append(r)
        elif rank <= 2 * n_teams // 3:
            mid.append(r)
        else:
            bot.append(r)
    return {"vs_top_defenses": top, "vs_mid_defenses": mid,
             "vs_bottom_defenses": bot}


def _split_by_recency(rows: list[dict], stat: str,
                       window: int = 5) -> dict[str, list[dict]]:
    """Bucket rows by whether the player was HOT (avg last-N > overall
    avg) or COLD in the games leading up to that row."""
    hot, cold = [], []
    for pid_key, gs in _by_player(rows).items():
        gs.sort(key=lambda r: (r.get("season") or 0, r.get("week") or 0))
        vals = [r.get(stat, 0) or 0 for r in gs]
        overall = sum(vals) / len(vals) if vals else 0
        for i, r in enumerate(gs):
            if i < window:
                continue
            prior = vals[i - window:i]
            recent_avg = sum(prior) / window if prior else overall
            (hot if recent_avg >= overall else cold).append(r)
    return {"hot_streak": hot, "cold_streak": cold}


def _by_player(rows: list[dict]) -> dict:
    d: dict = {}
    for r in rows:
        d.setdefault(r.get("player_id"), []).append(r)
    return d


def _summarise_split(name: str, rows: list[dict], stat: str,
                      baseline_avg: float, threshold: float,
                      factor: str) -> Optional[dict]:
    vals = [float(r.get(stat) or 0.0) for r in rows if r.get(stat) is not None]
    n = len(vals)
    if n == 0:
        return None
    avg = sum(vals) / n
    hits = sum(1 for v in vals if v > threshold)
    hit_rate = hits / n
    lb = wilson_lower_bound(hits, n)
    lift_pct = ((avg - baseline_avg) / baseline_avg * 100.0) \
                 if baseline_avg > 0 else 0.0
    grade = confidence_grade(hits, n, expected_p=0.5)
    return {
        "pattern_id":            f"{factor}:{name}",
        "factor":                factor,
        "split":                 name,
        "sample_size":           n,
        "hit_rate_over_median":  round(hit_rate, 4),
        "avg_stat_split":        round(avg, 3),
        "avg_stat_baseline":     round(baseline_avg, 3),
        "lift_pct":              round(lift_pct, 2),
        "wilson_lb":             round(lb, 4),
        "confidence":            confidence_label(n),
        "grade":                 grade,
    }


async def discover_patterns(
    db,
    *,
    sport: str,
    player: str,
    stat: str,
    min_samples: int = 8,
    min_lift_pct: float = 15.0,
) -> list[dict]:
    """Enumerate a curated set of factor splits and return the ones
    that clear the sample-size + lift gates."""
    sport_u = (sport or "").upper()
    if sport_u != "NFL":
        # Pattern discovery is currently NFL-only. MLB/Tennis need
        # opponent/date fields we don't reliably have in player_game_logs.
        return []
    rows = await _load_nfl_rows(db, player)
    if not rows:
        return []
    all_vals = [float(r.get(stat) or 0.0) for r in rows
                if r.get(stat) is not None]
    if not all_vals:
        return []
    baseline_avg = sum(all_vals) / len(all_vals)
    # Threshold = median — a natural pivot for "did they beat their
    # typical output".
    baseline_median = sorted(all_vals)[len(all_vals) // 2]

    patterns: list[dict] = []
    for splitter, factor in (
        (_split_home_away,      "home_away"),
        (_split_by_rest_days,   "rest_days"),
        (_split_by_opp_tier,    "opponent_tier"),
        (lambda rs: _split_by_recency(rs, stat), "recent_form"),
    ):
        try:
            groups = splitter(rows) if factor != "opponent_tier" \
                      else splitter(rows, stat)
        except Exception as e:
            logger.debug("splitter %s failed: %s", factor, e)
            continue
        for name, subset in groups.items():
            summary = _summarise_split(
                name, subset, stat, baseline_avg,
                baseline_median, factor,
            )
            if not summary:
                continue
            # Gates.
            if not passes_sample_gate(summary["sample_size"],
                                        min_samples=min_samples):
                continue
            if abs(summary["lift_pct"]) < min_lift_pct \
               and summary["grade"] not in {"A+", "A"}:
                continue
            summary["note"] = (
                f"{player} in {name.replace('_', ' ')} — averages "
                f"{summary['avg_stat_split']} vs baseline "
                f"{summary['avg_stat_baseline']} "
                f"({summary['lift_pct']:+}%, "
                f"{summary['sample_size']} games)"
            )
            patterns.append(summary)
    patterns.sort(key=lambda p: (p["wilson_lb"], p["sample_size"]),
                    reverse=True)
    return patterns


__all__ = ["discover_patterns"]
