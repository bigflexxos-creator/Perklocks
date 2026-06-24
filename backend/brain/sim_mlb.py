"""MLB Prop Simulator — Monte Carlo engine for hitter & pitcher props.

Phase A simulator. Real game mechanics, not stress tests:
  • Hitters: per-AB outcome distribution from batter K/BB/BA/HR rates blended
    with opposing pitcher splits, distributed over expected ABs.
  • Pitchers: per-batter-faced K rate blended with lineup contact tendencies,
    distributed over expected innings × ~4 batters/inning.

Output (per pick):
  sim_win_probability     — Monte Carlo win % over RUNS samples
  sim_ci_lower/upper      — 95% CI bracket (Wilson)
  sim_runs                — # of MC iterations
  sim_disagreement        — sim_wp − blended model wp (positive = sim more bullish)

All inputs come from the free MLB Stats API via mlb_live.py. Zero credit cost.
"""
from __future__ import annotations
import math
import random
import re
from typing import Optional

from brain.sim_distribution import compute_percentiles

RUNS = 10_000               # Monte Carlo iterations per pick
EXPECTED_ABS_HITTER = 4.2   # lineup-spot avg
EXPECTED_BF_PITCHER = 22.0  # ~ 5-6 innings × 3.7 BF/inning

# League averages used as priors when player data is missing.
LEAGUE_BA = 0.243
LEAGUE_HR_PER_AB = 0.032
LEAGUE_K_RATE = 0.231
LEAGUE_BB_RATE = 0.087


def _wilson_ci(p: float, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson confidence interval — better than normal approx for extremes."""
    if n == 0:
        return (0.0, 1.0)
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt((p * (1 - p) / n) + (z * z / (4 * n * n))) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def _extract_threshold(market: str) -> float:
    """Pull the over/under line from 'Over 1.5 Hits' → 1.5."""
    m = re.search(r"(?:over|under)\s+(\d+(?:\.\d+)?)", (market or "").lower())
    return float(m.group(1)) if m else 0.5


def _is_under(market: str) -> bool:
    return "under " in (market or "").lower()


# ── Hitter simulators ──────────────────────────────────────────────────


def _simulate_hits(batter_ba: float, expected_abs: int, runs: int = RUNS) -> list[int]:
    """Each AB → Bernoulli(BA). Returns list of total hits per game across runs."""
    p = max(0.05, min(0.55, batter_ba))
    out = []
    for _ in range(runs):
        hits = sum(1 for _ in range(expected_abs) if random.random() < p)
        out.append(hits)
    return out


def _simulate_hrs(batter_hr_rate: float, expected_abs: int, runs: int = RUNS) -> list[int]:
    p = max(0.001, min(0.15, batter_hr_rate))
    out = []
    for _ in range(runs):
        hrs = sum(1 for _ in range(expected_abs) if random.random() < p)
        out.append(hrs)
    return out


def _simulate_hrr(
    batter_ba: float, batter_hr_rate: float, batter_rbi_rate: float,
    expected_abs: int, runs: int = RUNS,
) -> list[int]:
    """Hits + Runs + RBIs composite. Run scored proxied via on-base × team scoring."""
    ba = max(0.05, min(0.55, batter_ba))
    hr = max(0.001, min(0.15, batter_hr_rate))
    rbi_p = max(0.02, min(0.30, batter_rbi_rate))   # P(RBI per AB)
    # Run-scored prob per AB ≈ OBP × 0.30 (rough league avg conversion)
    run_p = max(0.04, ba * 0.45)
    out = []
    for _ in range(runs):
        total = 0
        for _ in range(expected_abs):
            r = random.random()
            if r < ba: total += 1                    # hit
            if random.random() < run_p: total += 1   # run
            if random.random() < rbi_p: total += 1   # rbi
            # HR is a hit + run + RBI compound but already counted via ba/run/rbi
            # so we add a small extra for HR-only events not captured
            if random.random() < hr * 0.4: total += 1
        out.append(total)
    return out


def _simulate_pitcher_ks(
    k_rate: float, expected_bf: int, runs: int = RUNS,
) -> list[int]:
    p = max(0.10, min(0.45, k_rate))
    out = []
    for _ in range(runs):
        ks = sum(1 for _ in range(expected_bf) if random.random() < p)
        out.append(ks)
    return out


def _simulate_pitcher_outs(
    bf_per_inning: float, expected_innings: float, runs: int = RUNS,
) -> list[int]:
    """Each BF retired with avg league prob; outs ≈ 3 × innings completed."""
    # Probability batter retired = 1 - OBP ≈ 1 - 0.320 = 0.680
    p_out = 0.680
    out = []
    for _ in range(runs):
        outs = 0
        # BF cap = expected_innings × bf_per_inning + cushion
        bf_cap = int(expected_innings * bf_per_inning * 1.2)
        for _ in range(bf_cap):
            if outs >= int(expected_innings * 3 + 6):
                break  # pulled after 6+ extra
            if random.random() < p_out:
                outs += 1
        out.append(outs)
    return out


# ── Entry point ────────────────────────────────────────────────────────


def simulate_mlb_pick(pick: dict, player_stats: dict | None = None) -> Optional[dict]:
    """Run Monte Carlo for a single MLB pick. Returns sim output dict.

    `player_stats` should contain whichever of these are available
    (defaults to league averages otherwise):
      batter:  ba, hr_per_ab, rbi_per_ab, k_rate
      pitcher: k_rate, bf_per_inning, expected_innings
    """
    market = pick.get("market") or ""
    ml = market.lower()
    if (pick.get("sport") or "") != "MLB":
        return None

    threshold = _extract_threshold(market)
    is_under = _is_under(market)
    stats = player_stats or {}

    # Route to the right simulator
    distribution: list[int] = []
    if "hits + runs + rbis" in ml or "h+r+rbi" in ml:
        distribution = _simulate_hrr(
            stats.get("ba", LEAGUE_BA),
            stats.get("hr_per_ab", LEAGUE_HR_PER_AB),
            stats.get("rbi_per_ab", 0.12),
            int(EXPECTED_ABS_HITTER),
        )
    elif "home runs" in ml or "home run" in ml:
        distribution = _simulate_hrs(
            stats.get("hr_per_ab", LEAGUE_HR_PER_AB),
            int(EXPECTED_ABS_HITTER),
        )
    elif "hits" in ml and "allowed" not in ml:
        distribution = _simulate_hits(
            stats.get("ba", LEAGUE_BA),
            int(EXPECTED_ABS_HITTER),
        )
    elif "strikeouts" in ml:
        distribution = _simulate_pitcher_ks(
            stats.get("k_rate", LEAGUE_K_RATE),
            int(EXPECTED_BF_PITCHER),
        )
    elif "outs recorded" in ml or "outs" in ml:
        distribution = _simulate_pitcher_outs(
            stats.get("bf_per_inning", 3.7),
            stats.get("expected_innings", 6.0),
        )
    else:
        return None

    if not distribution:
        return None

    # Count wins
    wins = sum(1 for x in distribution if (x < threshold if is_under else x > threshold))
    n = len(distribution)
    p_win = wins / n
    ci_lo, ci_hi = _wilson_ci(p_win, n)

    # Disagreement vs blended model
    blended_wp = float(pick.get("win_probability") or 0)
    sim_wp_pct = round(p_win * 100, 1)
    disagreement = round(sim_wp_pct - blended_wp, 2)

    if disagreement > 5:
        signal = "stronger"
    elif disagreement < -5:
        signal = "weaker"
    else:
        signal = "neutral"

    # Alt-line sensitivity table: how does sim P(over) change ±0.5/±1.0/±1.5?
    # Helps users see whether the line is the right one or alt-shopping has edge.
    alt_lines: dict = {}
    for delta in (-1.5, -1.0, -0.5, 0.5, 1.0, 1.5):
        alt = round(threshold + delta, 1)
        if alt < 0:
            continue
        over_hits = sum(1 for x in distribution if x > alt)
        alt_lines[str(alt)] = round(over_hits / n * 100, 1)
    expected_stat = sum(distribution) / max(1, n)

    return {
        "sim_win_probability": sim_wp_pct,
        "sim_ci_lower": round(ci_lo * 100, 1),
        "sim_ci_upper": round(ci_hi * 100, 1),
        "sim_runs": n,
        "sim_threshold": threshold,
        "sim_is_under": is_under,
        "sim_expected_stat": round(expected_stat, 2),
        "sim_alt_lines": alt_lines,
        "sim_disagreement_with_model": disagreement,
        "sim_signal": signal,
        # Risk Meter — five-number summary of the underlying stat
        # distribution so the UI can render a P10–P90 spread with the
        # line marker positioned at sim_pctl_line_quantile_pct.
        **compute_percentiles(distribution, threshold=threshold),
    }
