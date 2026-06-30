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


# ── ALT RULES (hard guardrails — user-locked 2026-06-29 v2) ──
#
# The original gate set (`probability ≥ 0.78`, `floor_p10 ≥ line`) was
# producing extreme chalk: Trevor Lawrence 149.5+ Passing Yds at -1250
# (93%) and Jonathan Taylor 1.5+ Receptions at -1200 (92%). Those are
# "lay $12.50 to win $1" lines that no sharp bettor would touch — high
# certainty, zero value.
#
# New mandate: SURFACE PROPS IN THE TRUE-VALUE BAND.
#   Target probability:    [0.67, 0.82]   →  -200 to -456 American odds
#   Acceptable secondary:  [0.62, 0.67)   →  -163 to -200 (mild stretch)
#   Reject hard-chalk:     ≥ 0.86         →  ≤ -614  (no juice >> value)
#
# Algorithm: for each (player, prop) walk the alt-line ladder, pick the
# HIGHEST line whose empirical hit rate lands inside the target band.
# This naturally avoids ultra-low lines (which inflate probability to
# 95%+ chalk) while still leveraging the player's recent form.
MIN_GAMES_SAMPLE = 5         # never recommend with fewer game logs
MIN_ATTEMPTS_TOTAL = 10      # volume floor
TARGET_PROB_MIN = 0.67       # ≈ -200 American
TARGET_PROB_MAX = 0.82       # ≈ -456 American
ACCEPTABLE_PROB_MIN = 0.62   # ≈ -163 (fallback band when target is empty)
HARD_CHALK_CUTOFF = 0.86     # any line with prob ≥ this is REJECTED outright
MAX_VOLATILITY_CV = 0.85
ONE_OUTLIER_DROP = 0.15

# Window we evaluate from each player's recent history (most recent N games).
EVAL_WINDOW = 17  # 1 full season

# Legacy aliases (still referenced by routes & rules block in response).
MIN_PROBABILITY = TARGET_PROB_MIN
PREF_PROBABILITY = TARGET_PROB_MAX


# ── Alternate line ladders ─────────────────────────────────────────────
# Expanded toward HIGHER lines so we can find the true-value zone for
# elite players (a 17/17 hitter at 149.5 might be 11/17 at 224.5 = 65%
# = -185 American = excellent value).
ALT_LINES: dict[str, list[float]] = {
    "rushing_yards":    [9.5, 14.5, 19.5, 24.5, 34.5, 49.5, 64.5, 79.5, 99.5],
    "receiving_yards":  [9.5, 14.5, 19.5, 24.5, 34.5, 49.5, 64.5, 79.5, 99.5],
    "receptions":       [0.5, 1.5, 2.5, 3.5, 4.5, 5.5, 6.5],
    "passing_yards":    [149.5, 174.5, 199.5, 224.5, 249.5, 274.5, 299.5],
    "passing_tds":      [0.5, 1.5, 2.5],
    "any_td":           [0.5],   # anytime TD — single line
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
    """Compute hit probability for a (values, line) set.

    NOTE (2026-06-29 v2): The previous gates (`median ≥ line`,
    `floor_p10 ≥ line`, `probability ≥ 0.78`) were the ROOT CAUSE of the
    extreme chalk we were surfacing. Forcing `floor_p10 ≥ line` mechanically
    pushes the selected line DOWN to whatever a player hits ~100% of the
    time (Trevor Lawrence p10 floor = 167.6 → 149.5+ at 17/17 = -1250).
    The new pipeline evaluates the raw probability and lets the caller
    decide which BAND to surface — gates here only reject obvious-junk
    (sample, volume, volatility, outlier-driven).
    """
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

    # Empirical hit rate (this is our headline true-probability number).
    hits = sum(1 for v in values if v > line)
    prob_empirical = hits / n

    # Bayesian shrink — Beta(8, 2) prior so 5/5 doesn't bluff to 100%.
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


def _prob_to_american(p: float) -> int:
    """Convert hit probability → no-vig American odds (sportsbook layer
    will add ~5-7% juice on top)."""
    p = max(0.01, min(0.99, p))
    if p >= 0.5:
        return int(round(-100 * p / (1 - p)))
    return int(round(100 * (1 - p) / p))


def _select_value_line(
    values: list[float],
    opportunities: list[int],
    lines: list[float],
) -> Optional[dict]:
    """Pick the line that lands inside the TRUE-VALUE BAND.

    Strategy:
      1. Evaluate EVERY line in the ladder.
      2. Reject hard-chalk (prob ≥ HARD_CHALK_CUTOFF) — those are -600+
         juice traps the user explicitly asked us to filter out.
      3. Prefer lines inside the TARGET band [0.67, 0.82].
         If multiple lines fit, take the HIGHEST line (more informative,
         less juice, biggest payout per $).
      4. If nothing fits the target band, fall back to ACCEPTABLE
         [0.62, 0.67) — a slight value-stretch but still safe.
      5. Otherwise no pick for this player+prop.
    """
    target: list[dict] = []
    acceptable: list[dict] = []
    rejected_chalk = 0
    rejected_low = 0
    last_reject_reason: Optional[str] = None

    for line in sorted(lines):  # walk low → high
        ev = _evaluate(values, opportunities, line)
        if not ev:
            continue
        if ev.get("reject"):
            last_reject_reason = ev["reject"]
            continue
        p = ev["probability"]
        # Hard floor — never surface picks below ACCEPTABLE_PROB_MIN.
        if p < ACCEPTABLE_PROB_MIN:
            rejected_low += 1
            continue
        # Hard chalk — explicit user mandate: NO -450+ juice on safe locks.
        # Anything in [HARD_CHALK_CUTOFF, 1.0) is dropped from the pool.
        if p >= HARD_CHALK_CUTOFF:
            rejected_chalk += 1
            continue
        ev["line"] = line
        # TARGET band is BOUNDED on both sides: [TARGET_PROB_MIN, TARGET_PROB_MAX].
        # Anything above TARGET_PROB_MAX (but still below hard chalk) is
        # considered "stretch chalk" — it's allowed in the pool but ranked
        # BELOW the target band so we prefer true value.
        if TARGET_PROB_MIN <= p <= TARGET_PROB_MAX:
            ev["band_score"] = 2  # best
            target.append(ev)
        elif p < TARGET_PROB_MIN:
            ev["band_score"] = 1  # acceptable stretch (more variance)
            acceptable.append(ev)
        else:
            ev["band_score"] = 0  # stretch-chalk — only used if nothing better
            acceptable.append(ev)

    pool = target or acceptable
    if not pool:
        return {"reject": last_reject_reason or "no_line_in_value_band"}

    # Within the chosen pool, prefer (a) higher band_score, then
    # (b) HIGHEST line (max payout / information density), then
    # (c) lowest CV (most stable performer).
    pool.sort(
        key=lambda r: (r.get("band_score", 0), r["line"], -r["volatility_cv"]),
        reverse=True,
    )
    chosen = pool[0]
    chosen["band"] = (
        "target" if chosen.get("band_score") == 2
        else "acceptable" if chosen.get("band_score") == 1
        else "stretch_chalk"
    )
    chosen["rejected_chalk_lines"] = rejected_chalk
    chosen["rejected_low_lines"] = rejected_low
    return chosen


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


def _why(stats: dict, prop: str, line: float, american: int) -> str:
    """Compact rationale chip for the card UI.

    Format keeps it ≤ ~70 chars: `L17 12/17 (71%) · -243 · med 244 · CV 0.18 · 305 opps`
    """
    n = stats["sample_size"]
    hits = stats["hits"]
    pct = round(stats["probability"] * 100)
    cv = stats["volatility_cv"]
    med = stats["median"]
    opps = stats["total_attempts"]
    odds_str = f"+{american}" if american > 0 else str(american)
    return (
        f"L{n} {hits}/{n} ({pct}%) · {odds_str} · med {med} · CV {cv:.2f} · {opps} opps"
    )


async def compute_safe_bets(
    db,
    *,
    limit: int = 10,
    min_probability: float = TARGET_PROB_MIN,
) -> dict:
    """Main entry point.

    Returns top-N highest-confidence NFL player-prop locks. NEW (v2):
    surfaces picks in the TRUE-VALUE BAND (-200 to -450) instead of
    extreme chalk. Caller-provided `min_probability` is honored as a
    soft floor — anything below `ACCEPTABLE_PROB_MIN` is still rejected.
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

    # Effective floor — never go below ACCEPTABLE_PROB_MIN regardless of
    # caller-provided min_probability.
    effective_min = max(ACCEPTABLE_PROB_MIN, float(min_probability or 0))

    for (pid, prop), meta in candidates.items():
        logs = await _gather_player_logs(db, pid, prop)
        if len(logs) < MIN_GAMES_SAMPLE:
            rejects["too_few_logs"] = rejects.get("too_few_logs", 0) + 1
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

        best = _select_value_line(vals, opps, lines)
        if not best:
            continue
        if best.get("reject"):
            key = best["reject"].split("(")[0]
            rejects[key] = rejects.get(key, 0) + 1
            continue

        # Final probability filter (cap-honored).
        if best["probability"] < effective_min:
            continue

        american = _prob_to_american(best["probability"])
        best.update({
            **meta,
            "prop": prop,
            "market": _market_label(prop, best["line"]),
            "reason": _reason(best, prop, best["line"]),
            "why": _why(best, prop, best["line"], american),
            "implied_american_odds": american,
        })
        results.append(best)

    # Sort by: (1) preference for TARGET band over acceptable, then
    # (2) confidence desc, then (3) probability desc.
    band_rank = {"target": 1, "acceptable": 0}
    results.sort(
        key=lambda r: (band_rank.get(r.get("band", "acceptable"), 0),
                       r["confidence"], r["probability"]),
        reverse=True,
    )
    return {
        "total_candidates": len(candidates),
        "passed_filters": len(results),
        "rejected": rejects,
        "rules": {
            "min_games_sample": MIN_GAMES_SAMPLE,
            "min_attempts_total": MIN_ATTEMPTS_TOTAL,
            "target_prob_min": TARGET_PROB_MIN,
            "target_prob_max": TARGET_PROB_MAX,
            "acceptable_prob_min": ACCEPTABLE_PROB_MIN,
            "hard_chalk_cutoff": HARD_CHALK_CUTOFF,
            "max_volatility_cv": MAX_VOLATILITY_CV,
            "one_outlier_drop": ONE_OUTLIER_DROP,
        },
        "picks": results[: max(1, int(limit))],
    }
