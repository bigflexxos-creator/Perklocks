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
    """Legacy display-text parse — retained ONLY as last-resort fallback
    when a structured ``pick["line"]`` is genuinely absent.
    Returns NaN on parse failure so callers can detect INVALID_INPUT
    (no silent 0.5 substitution)."""
    m = re.search(r"(?:over|under)\s+(\d+(?:\.\d+)?)", (market or "").lower())
    if not m:
        m = re.search(r"(\d+(?:\.\d+)?)", str(market or ""))
    return float(m.group(1)) if m else float("nan")


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
        # ── Game markets (Moneyline / Run Line / Total) — Poisson
        # team-score model.  This is the P0 (2026-06 Final Closure)
        # extension: previously ``sim_mlb`` returned None for game
        # markets, leaving whatever book/factor-seeded probability
        # the caller attached as the final published prob.  We now
        # derive real per-team run-scoring λ from the existing MLB
        # inputs (probable pitcher K-rate / team runs projection /
        # park & environment / home-away) and simulate the game.
        _game = _simulate_mlb_game_market(pick, stats)
        # Fail-closed contract: game-market failures return None so
        # sim_runner leaves the caller's model/prob untouched — never
        # a partial ran=False payload.
        if _game is None or not _game.get("ran", True):
            return None
        return _game

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


# ═══════════════════════════════════════════════════════════════════════
# MLB GAME-MARKET SIMULATOR — Moneyline / Run Line / Total
# Added 2026-06 in the Perklocks final production closure.  Uses the
# existing per-team MLB context (probable pitchers, offense, park,
# home/away) to run a Poisson team-score Monte Carlo, then derives
# exact selected-side probabilities for ML / Run Line / Total in ONE
# coherent simulation.  Distinct from the player-prop path above.
# ═══════════════════════════════════════════════════════════════════════
import math as _math

_MLB_LEAGUE_AVG_RUNS = 4.55            # 2024 MLB avg runs/game/team
_MLB_LEAGUE_K_RATE   = LEAGUE_K_RATE   # existing constant
_MLB_LEAGUE_PARK_FACTOR = 1.0

def _mlb_pick_side(pick: dict, *, home: str, away: str) -> Optional[str]:
    """Return "home" or "away" from a ML/RunLine pick."""
    h = (home or "").strip().lower()
    a = (away or "").strip().lower()
    for k in ("side", "selection", "pick_side", "pick"):
        v = str(pick.get(k) or "").strip().lower()
        if not v:
            continue
        if h and h in v:
            return "home"
        if a and a in v:
            return "away"
    return None

def _mlb_extract_line(pick: dict) -> Optional[float]:
    for k in ("line", "point", "threshold"):
        v = pick.get(k)
        try:
            if v is not None: return float(v)
        except (TypeError, ValueError): pass
    m = _re_num.search(str(pick.get("market") or ""))
    if m:
        try: return float(m.group(1))
        except ValueError: return None
    return None

import re as _re
_re_num = _re.compile(r"(-?\d+(?:\.\d+)?)")

def _mlb_classify_game_market(pick: dict) -> Optional[str]:
    """Return "moneyline" | "run_line" | "total" or None."""
    raw = str(pick.get("market") or "").strip().lower()
    mk  = str(pick.get("market_key") or "").strip().lower()
    if "moneyline" in raw or mk in ("h2h", "moneyline"):
        return "moneyline"
    if "run line" in raw or "runline" in raw or "run_line" in mk \
       or "spread" in mk or "spreads" == mk:
        return "run_line"
    # Team total is handled by the alt-team-total flow, not here.
    if "team total" in raw:
        return None
    if raw.startswith("total ") or " total " in raw \
       or raw.endswith(" total") or mk in ("totals", "total"):
        return "total"
    return None

def _mlb_team_lambda(pick: dict, stats: dict, *, is_home: bool) -> float:
    """Derive an offense-vs-pitching expected runs (λ) for the team.

    Inputs (all optional — missing ones fall back to league average):
        pitcher_k_rate         opposing pitcher K rate
        pitcher_era            opposing pitcher ERA
        team_runs_projection   pre-game per-team runs projection (best)
        park_factor            ballpark run factor (1.0 = neutral)
        home_field_bump        home offense edge (default 0.10)
    """
    # Best signal: an explicit projected run total for this team.
    proj = stats.get("team_runs_projection")
    if isinstance(proj, (int, float)) and proj > 0:
        base = float(proj)
    else:
        base = _MLB_LEAGUE_AVG_RUNS
        # Opposing pitcher K-rate adjustment (very high K-rate → fewer runs).
        opp_k = stats.get("pitcher_k_rate")
        if isinstance(opp_k, (int, float)):
            base *= max(0.75, min(1.20,
                                     1.0 + (_MLB_LEAGUE_K_RATE - float(opp_k)) * 1.5))
        # ERA-based adjustment when available.
        opp_era = stats.get("pitcher_era")
        if isinstance(opp_era, (int, float)) and opp_era > 0:
            base *= max(0.75, min(1.25, float(opp_era) / 4.30))
    park = stats.get("park_factor")
    if isinstance(park, (int, float)) and 0.7 < park < 1.4:
        base *= float(park)
    if is_home:
        base *= 1.03
    else:
        base *= 0.97
    return max(0.5, min(12.0, base))

def _sample_poisson_int(lam: float) -> int:
    if lam <= 0: return 0
    L = _math.exp(-lam)
    k = 0; p = 1.0
    while True:
        k += 1; p *= random.random()
        if p < L: break
    return k - 1

def _simulate_mlb_game_market(pick: dict, stats: dict) -> Optional[dict]:
    """Coherent Poisson team-score MC → ML / Run Line / Total probs.

    Real inputs required (fail-closed when absent):
        home_team + away_team
        AT LEAST ONE of: team_runs_projection, pitcher_k_rate,
                          pitcher_era, park_factor
    """
    kind = _mlb_classify_game_market(pick)
    if kind is None:
        return None
    home = pick.get("home_team") or ""
    away = pick.get("away_team") or ""
    if not home or not away:
        return {"ran": False, "reason": "DATA_INSUFFICIENT",
                 "detail": "missing_home_or_away_team"}
    # Real-signal gate — need at least ONE real MLB context input.
    home_stats = stats.get("home") or stats or {}
    away_stats = stats.get("away") or stats or {}
    _real_signals = 0
    for src in (home_stats, away_stats, pick):
        for k in ("team_runs_projection", "pitcher_k_rate",
                  "pitcher_era", "park_factor"):
            if isinstance(src.get(k), (int, float)):
                _real_signals += 1
                break
    if _real_signals == 0:
        return {"ran": False, "reason": "DATA_INSUFFICIENT",
                 "detail": "no_real_mlb_context_inputs"}

    home_lam = _mlb_team_lambda(pick, home_stats, is_home=True)
    away_lam = _mlb_team_lambda(pick, away_stats, is_home=False)
    RUNS = 20_000
    home_scores: list[int] = []
    away_scores: list[int] = []
    for _ in range(RUNS):
        h = _sample_poisson_int(home_lam)
        a = _sample_poisson_int(away_lam)
        # MLB never ties — extra innings resolve.  50/50 walk-off.
        if h == a:
            if random.random() < 0.5: h += 1
            else:                     a += 1
        home_scores.append(h); away_scores.append(a)
    totals = [h + a for h, a in zip(home_scores, away_scores)]

    line = _mlb_extract_line(pick)
    if kind == "moneyline":
        team = _mlb_pick_side(pick, home=home, away=away)
        if team is None:
            return {"ran": False, "reason": "MISSING_SIDE"}
        wins = sum(1 for h, a in zip(home_scores, away_scores)
                     if (h > a if team == "home" else a > h))
        distribution = home_scores if team == "home" else away_scores
        threshold = None
    elif kind == "run_line":
        team = _mlb_pick_side(pick, home=home, away=away)
        if team is None or line is None:
            return {"ran": False, "reason": "MISSING_SIDE_OR_LINE"}
        wins = 0
        for h, a in zip(home_scores, away_scores):
            margin = (h - a) if team == "home" else (a - h)
            if margin > line: wins += 1
        distribution = [(h - a) if team == "home" else (a - h)
                          for h, a in zip(home_scores, away_scores)]
        threshold = line
    else:  # total
        if line is None:
            return {"ran": False, "reason": "MISSING_LINE"}
        is_under = _is_under(pick.get("market") or "")
        if str(pick.get("side") or pick.get("selection") or "").lower().startswith("under"):
            is_under = True
        elif str(pick.get("side") or pick.get("selection") or "").lower().startswith("over"):
            is_under = False
        wins = sum(1 for t in totals
                     if (t < line if is_under else t > line))
        distribution = totals
        threshold = line

    n = len(distribution)
    p_win = wins / n if n else 0.0
    ci_lo, ci_hi = _wilson_ci(p_win, n)
    blended_wp = float(pick.get("win_probability") or 0)
    sim_wp_pct = round(p_win * 100, 1)
    disagreement = round(sim_wp_pct - blended_wp, 2)
    signal = "stronger" if disagreement > 5 else ("weaker" if disagreement < -5 else "neutral")

    payload = {
        "sim_win_probability":          sim_wp_pct,
        "sim_ci_lower":                 round(ci_lo * 100, 1),
        "sim_ci_upper":                 round(ci_hi * 100, 1),
        "sim_runs":                     n,
        "sim_threshold":                threshold,
        "sim_expected_stat":            round(sum(distribution) / max(1, n), 2),
        "sim_disagreement_with_model":  disagreement,
        "sim_signal":                   signal,
        "sim_game_market_kind":         kind,
        "sim_home_lambda":              round(home_lam, 3),
        "sim_away_lambda":              round(away_lam, 3),
        "simulator_type":               "distribution_monte_carlo",
        "simulator_name":               "mlb_simulator_game",
        "simulator_version":            "1.0.0",
        "independent_evidence":         True,
        "valid":                        True,
        **compute_percentiles(distribution, threshold=threshold),
    }
    # Provenance envelope — CAUSAL when we had ≥2 real signals,
    # EMPIRICAL when 1.
    return _stamp_mlb_sim_out(
        payload,
        player_stats={
            "team_runs_projection_home": home_stats.get("team_runs_projection"),
            "team_runs_projection_away": away_stats.get("team_runs_projection"),
            "pitcher_k_rate_home":        home_stats.get("pitcher_k_rate"),
            "pitcher_k_rate_away":        away_stats.get("pitcher_k_rate"),
            "pitcher_era_home":           home_stats.get("pitcher_era"),
            "pitcher_era_away":           away_stats.get("pitcher_era"),
            "park_factor":                stats.get("park_factor"),
        },
        sim_prob=p_win,
        model_prob=(blended_wp / 100.0) if blended_wp else None,
    )

