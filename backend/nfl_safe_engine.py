"""NFL High-Hit-Rate Engine.

Ranks NFL player-prop opportunities by TRUE WIN PROBABILITY — not EV,
not edge. Optimised for "boring locks": -600 American odds or shorter
where the underlying probability is genuinely high.

Reads from `player_game_logs` (populated by historical/nfl.py).

Pipeline:
  1. Aggregate per-player rolling distribution (mean / median / std / p10)
  2. Convert to hit-probability across alternate lines using empirical CDF
  3. Apply STRICT filters (ALT RULES):
       • games_used    ≥ 5
       • attempts/opps ≥ 10
       • median ≥ line (no mean-inflated picks)
       • floor (p10)  ≥ line (no one-game outlier wins)
       • probability  ≥ MIN_PROBABILITY (default 0.78)
       • volatility (CV = std/mean) ≤ MAX_CV
  4. Confidence score:
       confidence = probability − volatility_penalty − uncertainty_penalty
  5. Sort descending by confidence; return top N.

NOT modeled here:
  • EV, edge, expected ROI
  • Betting market lines (engine is line-agnostic; it tells you raw
    probability, ops/UI converts to American odds threshold)
"""
from __future__ import annotations

import logging
import math
from statistics import median, pstdev
from typing import Any, Optional

logger = logging.getLogger("lockscore.nfl_safe")


# ── ALT RULES (hard guardrails — user-locked) ──
MIN_GAMES_SAMPLE = 5      # never recommend with fewer game logs
MIN_ATTEMPTS_TOTAL = 10   # never recommend < 10 opportunities total (volume floor)
MIN_PROBABILITY = 0.78    # ≈ -355 American odds floor
PREF_PROBABILITY = 0.857  # ≈ -600 American odds — "preferred" zone
MAX_VOLATILITY_CV = 0.85  # std / mean — reject erratic players
ONE_OUTLIER_DROP = 0.15   # if removing top value drops hit-rate by >15%, reject

# Window we evaluate from each player's recent history (most recent N games).
EVAL_WINDOW = 17  # 1 full season


# ── Alternate line ladders (the "safe" zone — much lower than book book) ──
# Engine outputs the safest hit per ladder. UI / odds layer matches against
# actual sportsbook offerings.

ALT_LINES: dict[str, list[float]] = {
    "rushing_yards":    [9.5, 14.5, 19.5, 24.5, 34.5, 49.5],
    "receiving_yards":  [9.5, 14.5, 19.5, 24.5, 34.5, 49.5],
    "receptions":       [0.5, 1.5, 2.5, 3.5],
    "passing_yards":    [149.5, 174.5, 199.5, 224.5, 249.5],
    "passing_tds":      [0.5, 1.5],
    "any_td":           [0.5],   # anytime TD
}


# ── Stat parsing from raw ESPN labels (logs stored fields as strings) ──

def _to_float(v: Any) -> Optional[float]:
    if v in (None, "", "—", "-"):
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _to_int(v: Any) -> Optional[int]:
    f = _to_float(v)
    if f is None:
        return None
    try:
        return int(f)
    except (TypeError, ValueError):
        return None


def _parse_pass_attempts(c_att: Any) -> Optional[int]:
    """Pass-block ESPN label `c/att` is "21/34" → returns 34."""
    if not c_att:
        return None
    s = str(c_att)
    if "/" not in s:
        return _to_int(s)
    try:
        return int(s.split("/")[1].strip())
    except (ValueError, IndexError):
        return None


def _extract_stat(log: dict, prop: str) -> tuple[Optional[float], Optional[int], Optional[int]]:
    """Return (value, opportunity, td_scored?) for one log row.

    Opportunity = attempts/carries/targets — used for the volume gate.
    td_scored is used for ATD modeling: a log row implies the player
    played; td > 0 means they scored that game.
    """
    if prop == "rushing_yards":
        return (_to_float(log.get("rushing_yards") or log.get("nfl_yds")),
                _to_int(log.get("nfl_car")),
                _to_int(log.get("nfl_td")))
    if prop == "receiving_yards":
        return (_to_float(log.get("receiving_yards") or log.get("nfl_yds")),
                _to_int(log.get("nfl_tgts")),
                _to_int(log.get("nfl_td")))
    if prop == "receptions":
        return (_to_float(log.get("receptions") or log.get("nfl_rec")),
                _to_int(log.get("nfl_tgts")),
                _to_int(log.get("nfl_td")))
    if prop == "passing_yards":
        return (_to_float(log.get("passing_yards") or log.get("nfl_yds")),
                _parse_pass_attempts(log.get("nfl_c/att")),
                _to_int(log.get("nfl_td")))
    if prop == "passing_tds":
        return (_to_float(log.get("passing_tds") or log.get("nfl_td")),
                _parse_pass_attempts(log.get("nfl_c/att")),
                _to_int(log.get("nfl_td")))
    if prop == "any_td":
        # Convert to {0,1} per game — did the player score?
        td_r = _to_int(log.get("nfl_td"))
        if td_r is None and "any_td" in log:
            td_r = _to_int(log.get("any_td"))
        val = 1.0 if (td_r or 0) > 0 else 0.0
        # Opportunity proxy: any touch (carry or target)
        opp = (_to_int(log.get("nfl_car")) or 0) + (_to_int(log.get("nfl_tgts")) or 0)
        return (val, opp if opp else None, td_r)
    return (None, None, None)


def _percentile(values: list[float], q: float) -> float:
    """Linear-interpolated percentile. q in [0, 1]."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return float(s[0])
    pos = q * (len(s) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return float(s[lo])
    return float(s[lo] + (s[hi] - s[lo]) * (pos - lo))


# ── Stat-block selector per prop ──
_STAT_BLOCK = {
    "rushing_yards":   "rushing",
    "receiving_yards": "receiving",
    "receptions":      "receiving",
    "passing_yards":   "passing",
    "passing_tds":     "passing",
    # any_td reads from BOTH rushing and receiving blocks (handled separately).
    "any_td":          None,
}


async def _gather_player_logs(
    db, player_id: str, prop: str, *, window: int = EVAL_WINDOW,
) -> list[dict]:
    """Pull last `window` logs for a (player, prop) pair."""
    block = _STAT_BLOCK.get(prop)
    if prop == "any_td":
        # Pull both rushing + receiving blocks
        cursor = db.player_game_logs.find(
            {"player_id": player_id, "sport": "nfl",
             "stat_block": {"$in": ["rushing", "receiving"]}},
            {"_id": 0},
        ).sort("date", -1).limit(window * 2)
        rows = [d async for d in cursor]
        # Collapse per game_id: ATD is True if ANY block had TD
        by_game: dict = {}
        for r in rows:
            gid = r.get("game_id")
            if not gid:
                continue
            existing = by_game.get(gid)
            td = _to_int(r.get("nfl_td")) or 0
            tgts = _to_int(r.get("nfl_tgts")) or 0
            car = _to_int(r.get("nfl_car")) or 0
            opp = tgts + car
            if not existing:
                by_game[gid] = {
                    "game_id": gid, "date": r.get("date"),
                    "nfl_td": td, "nfl_car": car, "nfl_tgts": tgts,
                    "stat_block": "atd_synth",
                }
            else:
                existing["nfl_td"] = max(existing["nfl_td"], td)
                existing["nfl_car"] = max(existing["nfl_car"], car)
                existing["nfl_tgts"] = max(existing["nfl_tgts"], tgts)
        # Sort by date desc, keep last `window`
        out = list(by_game.values())
        out.sort(key=lambda r: r.get("date") or "", reverse=True)
        return out[:window]
    cursor = db.player_game_logs.find(
        {"player_id": player_id, "sport": "nfl", "stat_block": block},
        {"_id": 0},
    ).sort("date", -1).limit(window)
    return [d async for d in cursor]


def _evaluate(
    values: list[float],
    opportunities: list[int],
    line: float,
) -> Optional[dict]:
    """Apply ALT RULES + compute hit probability for a (values, line) set."""
    n = len(values)
    if n < MIN_GAMES_SAMPLE:
        return {"reject": f"sample_too_small({n})"}
    total_opp = sum(o for o in opportunities if o)
    if total_opp < MIN_ATTEMPTS_TOTAL:
        return {"reject": f"volume_too_low({total_opp})"}

    mean_v = sum(values) / n
    med_v = float(median(values))
    floor_v = _percentile(values, 0.10)   # p10 = soft floor

    # Reject if mean-inflated by an outlier: drop top value and re-check rate
    if n >= 6:
        sorted_v = sorted(values, reverse=True)
        without_top = sorted_v[1:]
        rate_full = sum(1 for v in values if v > line) / n
        rate_no_top = sum(1 for v in without_top if v > line) / max(1, len(without_top))
        if rate_full - rate_no_top > ONE_OUTLIER_DROP:
            return {"reject": "outlier_inflated"}

    if med_v < line:
        return {"reject": f"median_below_line({med_v:.1f}<{line})"}
    if floor_v < line:
        return {"reject": f"floor_below_line(p10={floor_v:.1f}<{line})"}

    # Empirical hit rate (this is our headline true-probability number).
    hits = sum(1 for v in values if v > line)
    prob_empirical = hits / n

    # Apply a small Bayesian shrink to the empirical rate so 5/5 doesn't
    # bluff to 100%. Beta(8, 2) prior — mildly optimistic for picks that
    # already passed every other gate.
    a, b = 8.0, 2.0
    prob_shrunk = (hits + a) / (n + a + b)
    # Take the SMALLER of the two so we don't over-promise.
    probability = min(prob_empirical, prob_shrunk)

    # Volatility penalty (coefficient of variation, capped).
    std_v = pstdev(values) if n > 1 else 0.0
    cv = (std_v / mean_v) if mean_v > 0 else 0.0
    if cv > MAX_VOLATILITY_CV:
        return {"reject": f"volatility_high(cv={cv:.2f})"}
    volatility_penalty = min(0.15, cv * 0.18)

    # Uncertainty penalty — standard error of the rate scaled.
    se = math.sqrt(max(0.0, prob_empirical * (1 - prob_empirical) / n))
    uncertainty_penalty = min(0.12, se * 0.8)

    confidence = max(0.0, probability - volatility_penalty - uncertainty_penalty)

    if probability < MIN_PROBABILITY:
        return {"reject": f"probability_below_threshold({probability:.2f})"}

    return {
        "probability": round(probability, 4),
        "probability_empirical": round(prob_empirical, 4),
        "confidence": round(confidence, 4),
        "median": round(med_v, 2),
        "mean": round(mean_v, 2),
        "floor_p10": round(floor_v, 2),
        "std": round(std_v, 2),
        "volatility_cv": round(cv, 3),
        "sample_size": n,
        "hits": hits,
        "min_attempts": min((o for o in opportunities if o), default=0),
        "total_attempts": total_opp,
    }


def _market_label(prop: str, line: float) -> str:
    pretty = {
        "rushing_yards":   "Rushing Yards",
        "receiving_yards": "Receiving Yards",
        "receptions":      "Receptions",
        "passing_yards":   "Passing Yards",
        "passing_tds":     "Passing TDs",
        "any_td":          "Anytime TD",
    }.get(prop, prop)
    if prop == "any_td":
        return "Anytime TD"
    return f"{pretty} {line}+"


def _reason(stats: dict, prop: str, line: float) -> str:
    parts = [
        f"L{stats['sample_size']} {stats['hits']}/{stats['sample_size']} hit ≥ {line}",
        f"median {stats['median']}",
        f"floor (p10) {stats['floor_p10']}",
        f"{stats['total_attempts']} opps",
    ]
    return " · ".join(parts)


async def compute_safe_bets(
    db,
    *,
    limit: int = 10,
    min_probability: float = MIN_PROBABILITY,
) -> dict:
    """Main entry point.

    Returns top-N highest-confidence NFL player-prop locks across rushing,
    receiving, receptions, passing yards, passing TDs, and ATD markets.
    """
    # Find players with logs in our active blocks.
    blocks = ["rushing", "receiving", "passing"]
    cursor = db.player_game_logs.aggregate([
        {"$match": {"sport": "nfl", "stat_block": {"$in": blocks}}},
        {"$group": {
            "_id": {"player_id": "$player_id", "name": "$name", "team": "$team",
                    "stat_block": "$stat_block"},
            "n": {"$sum": 1},
        }},
        {"$match": {"n": {"$gte": MIN_GAMES_SAMPLE}}},
    ], allowDiskUse=True)

    # Build a set of (player_id, prop) candidates to evaluate.
    candidates: dict[tuple[str, str], dict] = {}
    async for row in cursor:
        k = row.get("_id", {})
        pid = k.get("player_id")
        name = k.get("name")
        team = k.get("team")
        sb = k.get("stat_block")
        if not pid or not name:
            continue
        prop_keys = {
            "rushing":   ["rushing_yards"],
            "receiving": ["receiving_yards", "receptions"],
            "passing":   ["passing_yards", "passing_tds"],
        }.get(sb, [])
        for pk in prop_keys:
            candidates[(pid, pk)] = {"player_id": pid, "player_name": name, "team": team}
        # Any-TD is implicit from any rushing / receiving line
        if sb in ("rushing", "receiving"):
            candidates[(pid, "any_td")] = {"player_id": pid, "player_name": name, "team": team}

    results: list[dict] = []
    rejects: dict[str, int] = {}

    for (pid, prop), meta in candidates.items():
        logs = await _gather_player_logs(db, pid, prop)
        if len(logs) < MIN_GAMES_SAMPLE:
            rejects[f"too_few_logs"] = rejects.get("too_few_logs", 0) + 1
            continue
        vals: list[float] = []
        opps: list[int] = []
        for log in logs:
            v, opp, _ = _extract_stat(log, prop)
            if v is None:
                continue
            vals.append(v)
            opps.append(opp or 0)
        if len(vals) < MIN_GAMES_SAMPLE:
            rejects["values_missing"] = rejects.get("values_missing", 0) + 1
            continue
        lines = ALT_LINES.get(prop) or []
        # Walk lines from highest to lowest; keep the best (highest line)
        # that still passes ALL gates — that's the most informative line
        # for a "safe" pick (UI can derive lower lines from this one).
        best: Optional[dict] = None
        for line in sorted(lines, reverse=True):
            ev = _evaluate(vals, opps, line)
            if not ev or ev.get("reject"):
                if ev:
                    rejects[ev["reject"].split("(")[0]] = (
                        rejects.get(ev["reject"].split("(")[0], 0) + 1
                    )
                continue
            # We have a pass. Take the HIGHEST passing line.
            best = {**ev, "prop": prop, "line": line, **meta}
            break

        if not best:
            continue

        # Apply min-probability cutoff (caller-overridable).
        if best["probability"] < min_probability:
            continue

        best["market"] = _market_label(prop, best["line"])
        best["reason"] = _reason(best, prop, best["line"])
        # Sportsbook-friendly American odds threshold equivalence.
        p = best["probability"]
        if p >= 0.5:
            best["implied_american_odds"] = int(round(-100 * p / (1 - p)))
        else:
            best["implied_american_odds"] = int(round(100 * (1 - p) / p))
        results.append(best)

    # Sort by confidence desc, break ties by probability desc.
    results.sort(key=lambda r: (r["confidence"], r["probability"]), reverse=True)
    return {
        "total_candidates": len(candidates),
        "passed_filters": len(results),
        "rejected": rejects,
        "rules": {
            "min_games_sample": MIN_GAMES_SAMPLE,
            "min_attempts_total": MIN_ATTEMPTS_TOTAL,
            "min_probability": min_probability,
            "preferred_probability": PREF_PROBABILITY,
            "max_volatility_cv": MAX_VOLATILITY_CV,
            "one_outlier_drop": ONE_OUTLIER_DROP,
            "median_must_exceed_line": True,
            "floor_p10_must_exceed_line": True,
        },
        "picks": results[: max(1, int(limit))],
    }
