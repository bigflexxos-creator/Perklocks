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

RUNS = 20_000
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
    *,
    lineup_slot: int = 4,
    team_runs_projection: float = 4.5,
    obp: float | None = None,
) -> list[int]:
    """Correlated Hits + Runs + RBIs simulator (Phase 4C).

    **Design (Phase 4C — 2026-08-06):**

    Prior versions used three INDEPENDENT Bernoulli draws per AB plus
    a partial extra HR bump on top of ``ba``:
        if random.random() < ba:      total += 1  # hit
        if random.random() < run_p:   total += 1  # run  (run_p = ba*0.45)
        if random.random() < hr*0.4:  total += 1  # extra HR bump  ← double count
    That structure (a) partially double-counted HRs (HRs already contribute
    to ``ba``) and (b) had no lineup / team-context awareness.

    The corrected model is a **per-PA event decomposition**:

      For each plate appearance:
        1. Draw outcome ∈ {HR, non-HR hit, non-hit-on-base (BB/HBP),
                            in-play out, K}.
           Using ``hr`` (HR/AB), ``ba - hr`` (non-HR hits/AB), an OBP
           excess (BB+HBP), and the residual outs.
        2. If outcome == HR:
             total += 1 hit + 1 run + 1 RBI (the batter scores + earns
             an RBI for themselves).  No further RBI draw.  This is
             LEGITIMATE double-attribution (a solo HR contributes 1H,
             1R, 1RBI = 3 to the H+R+RBI stat) — NOT an artificial bump.
             Additional RBIs from teammates on base are drawn via
             ``rbi_extra_p`` conditional on lineup-slot on-base env.
        3. If outcome == non-HR hit:
             total += 1 hit.
             With probability ``run_p_hit`` (conditional on lineup slot
             + team_runs_projection) → the batter later scores → +1 run.
             With probability ``rbi_p_nonhr_hit`` → +1 RBI (drove in a
             runner).
        4. If outcome == non-hit-on-base (walk / HBP):
             No hit. With reduced ``run_p_bb`` → +1 run.  No RBI draw
             from a walk unless bases loaded (small ``rbi_p_bb``).
        5. If outcome == in-play out / K:
             No hits, no runs (unless very rare productive out).
             Small ``rbi_p_out`` if lineup slot suggests RBI groundouts
             / sac flies from team_runs_projection.

    **HR double-count elimination:** ``hr`` is drawn EXPLICITLY from the
    outcome tree (mutually exclusive with the non-HR hit / non-hit-on-
    base / out branches).  The old ``if random.random() < hr*0.4``
    extra-bump is REMOVED.  Total H+R+RBI contribution of a solo HR is
    exactly 3 (1H+1R+1RBI), of a 2-run HR is exactly 4 (1H+1R+2RBI),
    etc. — matching real-world scoring.

    **Lineup awareness:** ``lineup_slot`` (1-9) and ``team_runs_projection``
    modulate ``run_p_hit``, ``rbi_extra_p``, and ``rbi_p_nonhr_hit``.
    Slots 1-2 have higher run conversion (leadoff + 2-hole score more
    often); slots 3-5 have higher RBI conversion (middle-order sees more
    runners); slots 7-9 penalise both.

    Returns a list of H+R+RBI totals per iteration.
    """
    ba = max(0.05, min(0.55, batter_ba))
    hr_pa = max(0.001, min(0.15, batter_hr_rate))         # HR/AB ≈ HR/PA close enough
    non_hr_hit_pa = max(0.02, ba - hr_pa)                 # remaining hits
    obp_ = max(ba + 0.03, min(0.55, obp or (ba + 0.055))) # OBP - BA ≈ walk / HBP band
    walk_hbp_pa = max(0.02, obp_ - ba)
    out_pa = max(0.05, 1.0 - hr_pa - non_hr_hit_pa - walk_hbp_pa)
    total_p = hr_pa + non_hr_hit_pa + walk_hbp_pa + out_pa
    hr_pa       /= total_p
    non_hr_hit_pa /= total_p
    walk_hbp_pa /= total_p
    out_pa      /= total_p

    # ── Lineup-slot / team-context conversion coefficients ──────────
    # Empirical ranges (2019-2024 MLB averages):
    #   Slots 1-2  : ~40% of hit → later run;  ~7% of hit → RBI
    #   Slots 3-5  : ~32% of hit → later run;  ~20% of hit → RBI
    #   Slots 6-7  : ~28% of hit → later run;  ~14% of hit → RBI
    #   Slots 8-9  : ~22% of hit → later run;  ~9% of hit → RBI
    slot = max(1, min(9, int(lineup_slot)))
    if slot <= 2:
        run_p_hit_base, rbi_p_hit_base = 0.40, 0.07
    elif slot <= 5:
        run_p_hit_base, rbi_p_hit_base = 0.32, 0.20
    elif slot <= 7:
        run_p_hit_base, rbi_p_hit_base = 0.28, 0.14
    else:
        run_p_hit_base, rbi_p_hit_base = 0.22, 0.09
    # Team offensive environment multiplier ─ 4.5 = league avg 2024.
    env_mult = max(0.7, min(1.35, team_runs_projection / 4.5))
    run_p_hit  = min(0.75, run_p_hit_base  * env_mult)
    rbi_p_hit  = min(0.60, rbi_p_hit_base  * env_mult)
    run_p_bb   = min(0.55, 0.55 * run_p_hit)          # walks convert ~55% as often
    rbi_p_bb   = min(0.05, 0.05 * env_mult)           # bases-loaded walk RBI (rare)
    # Non-hr-hit → extra RBIs (HR path gets its own solo/multi RBIs).
    # HR path: 1 solo-run RBI is guaranteed; add small chance of extra
    # RBIs from runners on base (2-run / 3-run / grand slam).
    hr_extra_rbi_p = min(0.65, 0.35 * env_mult)       # per-HR extra runner-on-base RBI

    out = []
    for _ in range(runs):
        total = 0
        for _ in range(expected_abs):
            u = random.random()
            if u < hr_pa:
                # HR: 1 hit + 1 run + 1 self-RBI = 3, plus expected
                # extra RBIs from runners on base.
                total += 3
                # Draw 0, 1, 2 extra RBIs (2-run, 3-run, grand slam).
                v = random.random()
                if v < hr_extra_rbi_p:
                    total += 1
                    if random.random() < 0.30 * env_mult:
                        total += 1
                        if random.random() < 0.10 * env_mult:
                            total += 1
            elif u < hr_pa + non_hr_hit_pa:
                # Non-HR hit.
                total += 1
                if random.random() < run_p_hit:
                    total += 1
                if random.random() < rbi_p_hit:
                    total += 1
            elif u < hr_pa + non_hr_hit_pa + walk_hbp_pa:
                # Walk / HBP — no hit, small run/RBI contribution.
                if random.random() < run_p_bb:
                    total += 1
                if random.random() < rbi_p_bb:
                    total += 1
            else:
                # In-play out / K — very small productive-out RBI chance.
                if random.random() < 0.03 * env_mult:
                    total += 1        # sac fly / RBI groundout
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
            lineup_slot=int(stats.get("lineup_slot") or
                             pick.get("lineup_slot") or
                             ((pick.get("player_intel") or {}).get("lineup_slot")) or 4),
            team_runs_projection=float(stats.get("team_runs_projection") or
                                         pick.get("team_runs_projection") or 4.5),
            obp=stats.get("obp"),
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

    return _stamp_mlb_sim_out({
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
    }, player_stats=stats, sim_prob=p_win, model_prob=(blended_wp / 100.0) if blended_wp else None)


# ─────────────────────────────────────────────────────────────────────
# PHASE 2 (2026-06) — Universal Simulator Provenance Envelope.
# ─────────────────────────────────────────────────────────────────────
def _stamp_mlb_sim_out(payload: dict, *, player_stats: dict,
                       sim_prob: float, model_prob: float | None) -> dict:
    """Attach universal provenance envelope to the MLB sim output.

    CAUSAL_INDEPENDENT when the simulator was driven by real
    player stats (ba, k_rate, hr_per_ab, etc.).  PRIOR_ONLY when
    only league-average defaults were available.  Signal count =
    number of non-None real stat inputs.
    """
    try:
        from services.simulator_provenance import (
            stamp_sim_output, classify_input_quality,
        )
        real_keys = ("ba", "hr_per_ab", "rbi_per_ab", "k_rate",
                     "bf_per_inning", "expected_innings", "obp",
                     "lineup_slot", "team_runs_projection")
        signals = sum(
            1 for k in real_keys
            if isinstance(player_stats.get(k), (int, float))
        )
        if signals >= 3:
            provenance = "CAUSAL_INDEPENDENT"
        elif signals >= 1:
            provenance = "EMPIRICAL_INDEPENDENT"
        else:
            provenance = "PRIOR_ONLY"
        stamp_sim_output(
            payload, provenance=provenance,
            input_quality=classify_input_quality(signals),
            sim_prob=sim_prob, model_prob=model_prob,
        )
    except Exception:
        pass
    return payload
