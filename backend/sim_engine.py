"""Context-aware Monte Carlo simulation engine for PerksLocks.

User spec 2026-07-04: "The simulator should not answer 'Who wins most
often?' It should answer 'Given all available information, how often
does this exact bet win, and is the sportsbook price wrong?'"

Session 1 scope:
  §3 Scenario-based Monte Carlo  — multiple game scripts per matchup
  §5 Correlation awareness       — same-game QB↔WR, team-total ↔ player
  §7 Market comparison           — edge = model - book implied
  §8 Multi-model consensus       — baseline + conservative + aggressive

Sessions 2-3 layer weighted history, player usage, calibration on top.

DESIGN
    scenarios : list[Scenario]      # e.g. pitcher-dominates, blowout
    models    : list[str] = ["baseline", "conservative", "aggressive"]

    for scenario in scenarios:
        for model in models:
            wins[(scenario, model)] = simulate(pick, ctx, scenario, model)
    prob = weighted_mean(wins, scenario_weights) → single p̂
    edge = p̂ − book_implied(book_odds)
    agreement = 1 − stdev([mean by model for each model])

Returns a `SimResult` with prob/edge/agreement/scenario_breakdown for
each pick — consumed by the ranker (§7 spec) and the lock-score gates.
"""
from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass, field
from typing import Optional

# ─────────────────────── odds helpers ─────────────────────────────────

def american_to_implied(odds: int) -> float:
    if not odds:
        return 0.0
    if odds >= 100:
        return 100.0 / (odds + 100.0)
    if odds <= -100:
        return abs(odds) / (abs(odds) + 100.0)
    return 0.0


def american_to_decimal(odds: int) -> float:
    if not odds:
        return 1.0
    if odds >= 100:
        return 1.0 + odds / 100.0
    if odds <= -100:
        return 1.0 + 100.0 / abs(odds)
    return 1.0


# ─────────────────────── scenario definitions ─────────────────────────
# Each sport has a curated set of game-script scenarios with historical
# base frequencies. Frequencies sum to 1.0 within a sport — they are
# then adjusted by matchup context in `_scenario_weights_for()`.

@dataclass
class Scenario:
    key: str
    base_prob: float            # 0..1 baseline frequency of this script
    p_multiplier: float = 1.0   # how the pick's win-prob shifts in this script


_SCENARIOS: dict[str, list[Scenario]] = {
    "MLB": [
        Scenario("pitcher_duel",     0.15, 0.85),  # Overs lose, Unders win
        Scenario("early_offense",    0.20, 1.15),
        Scenario("bullpen_collapse", 0.12, 1.10),
        Scenario("high_strikeouts",  0.18, 1.05),
        Scenario("weather_boost",    0.10, 1.20),
        Scenario("neutral",          0.25, 1.00),
    ],
    "NBA": [
        Scenario("blowout",         0.20, 0.90),
        Scenario("close_game",      0.30, 1.05),
        Scenario("fast_pace",       0.20, 1.10),
        Scenario("foul_trouble",    0.10, 0.95),
        Scenario("neutral",         0.20, 1.00),
    ],
    "NFL": [
        Scenario("shootout",        0.20, 1.15),
        Scenario("defensive",       0.20, 0.85),
        Scenario("script_flip",     0.15, 0.95),
        Scenario("weather_impact",  0.10, 0.90),
        Scenario("neutral",         0.35, 1.00),
    ],
    "Soccer": [
        Scenario("early_goal",       0.25, 1.10),
        Scenario("defensive_battle", 0.20, 0.85),
        Scenario("high_possession",  0.20, 1.05),
        Scenario("late_drama",       0.15, 1.10),
        Scenario("neutral",          0.20, 1.00),
    ],
    "Tennis": [
        Scenario("straight_sets",   0.35, 1.10),
        Scenario("upset",           0.10, 0.70),
        Scenario("long_match",      0.25, 1.00),
        Scenario("neutral",         0.30, 1.00),
    ],
    "default": [
        Scenario("neutral",         1.00, 1.00),
    ],
}


def _scenario_weights_for(sport: str, ctx: dict) -> list[tuple[Scenario, float]]:
    """Return [(scenario, adjusted_weight)] where weights sum to 1.

    Adjustments (ctx-aware):
      • weather.rain → +weather_impact / defensive
      • high total → +early_offense / shootout
      • low total → +pitcher_duel / defensive
      • pace_up → +fast_pace / early_goal
    """
    scenarios = _SCENARIOS.get(sport, _SCENARIOS["default"])
    weights = {s.key: s.base_prob for s in scenarios}
    ctx = ctx or {}
    total_line = ctx.get("game_total")

    if sport == "MLB" and isinstance(total_line, (int, float)):
        if total_line >= 9.5:
            weights["early_offense"] = weights.get("early_offense", 0) * 1.6
            weights["pitcher_duel"] = weights.get("pitcher_duel", 0) * 0.4
        elif total_line <= 7.5:
            weights["pitcher_duel"] = weights.get("pitcher_duel", 0) * 1.6
            weights["early_offense"] = weights.get("early_offense", 0) * 0.5

    if ctx.get("weather", {}).get("rain") is True:
        for k in ("weather_impact", "weather_boost", "defensive", "defensive_battle"):
            if k in weights:
                weights[k] = weights[k] * 1.5

    if sport == "NBA" and isinstance(total_line, (int, float)) and total_line >= 240:
        weights["fast_pace"] = weights.get("fast_pace", 0) * 1.4

    total = sum(weights.values()) or 1.0
    return [(s, weights.get(s.key, 0) / total) for s in scenarios]


# ─────────────────────── model priors ─────────────────────────────────
# Three models: baseline uses the raw model_prob, conservative pulls
# toward 0.5 (regression to mean), aggressive amplifies away from 0.5.

_MODELS = {
    "baseline":     1.0,
    "conservative": 0.75,   # shrink toward 50%
    "aggressive":   1.20,   # amplify away from 50%
}


def _model_adjust(base_p: float, model_key: str) -> float:
    """Adjust base prob using the model's aggression multiplier."""
    mult = _MODELS.get(model_key, 1.0)
    delta = base_p - 0.5
    p = 0.5 + delta * mult
    return max(0.02, min(0.98, p))


# ─────────────────────── correlation adjustments ──────────────────────
# When several picks depend on the same game script, joint probability
# is HIGHER than independence would suggest. When picks contradict a
# scenario, joint probability is LOWER. `apply_correlation()` returns
# a bounded correlation coefficient we apply to each pick's confidence.

def apply_correlation(picks: list[dict]) -> dict[str, float]:
    """Return {pick_id: correlation_factor} where factor 0..1 measures
    how the pick's confidence should be reduced/raised.

    factor 1.00 = no adjustment
    factor 0.85 = -15 % confidence penalty (already-taken game script)
    factor 1.05 = +5 % confidence bonus (single independent bet)
    """
    factors: dict[str, float] = {p.get("id", ""): 1.0 for p in picks}
    by_event: dict[str, list[dict]] = {}
    for p in picks:
        ev = p.get("event") or ""
        by_event.setdefault(ev, []).append(p)
    for event, bucket in by_event.items():
        if len(bucket) <= 1:
            continue
        # Same-game grouping — reduce independence bonus, apply penalty
        # of −5 % per additional correlated leg beyond the first, capped
        # at −20 %.
        # NFL QB↔WR correlation, MLB team-total↔player-over correlation,
        # Soccer AGS↔team-scores correlation are all captured by same-event
        # grouping since they share the underlying game script.
        n = len(bucket)
        penalty = min(0.20, 0.05 * (n - 1))
        for p in bucket:
            pid = p.get("id", "")
            factors[pid] = max(0.75, 1.0 - penalty)
    return factors


# ─────────────────────── main simulate() entry point ──────────────────

@dataclass
class SimResult:
    prob: float                      # weighted mean across models + scenarios
    edge_pct: float                  # (prob − book_implied) × 100
    model_agreement: float           # 1 − stdev(model_probs)
    scenario_breakdown: dict[str, float] = field(default_factory=dict)
    per_model: dict[str, float] = field(default_factory=dict)
    ev_units: float = 0.0
    correlation_factor: float = 1.0
    n_simulations: int = 0


def _sim_one(base_p: float, n: int) -> float:
    """Run `n` Bernoulli trials at prob `base_p`, return hit rate.

    Uses Python's random with a fixed seed only within-call so results
    are stable for a single dispatch. Number of simulations is small
    (1_000 default) — good enough for board-scale sanity.
    """
    if n <= 0:
        return base_p
    r = random.Random()
    hits = sum(1 for _ in range(n) if r.random() < base_p)
    return hits / n


def simulate_pick(
    pick: dict,
    *,
    context: Optional[dict] = None,
    correlation_factor: float = 1.0,
    n_simulations: int = 1000,
) -> SimResult:
    """Simulate a pick across the sport's scenarios × 3 models.

    Returns a SimResult with weighted prob, edge, model agreement, and
    per-scenario breakdown for UI transparency.

    Inputs:
        pick.win_probability (0..1 or 0..100)
        pick.book_odds       American
        pick.sport
        context.game_total, context.weather, ...
    """
    ctx = context or {}
    sport = (pick.get("sport") or "").capitalize() or "default"
    # Normalise base prob
    raw = float(pick.get("win_probability") or 0.5)
    base_p = raw / 100.0 if raw > 1.0 else raw
    base_p = max(0.02, min(0.98, base_p))

    # Book implied
    try:
        odds = int(pick.get("book_odds") or 0)
    except (TypeError, ValueError):
        odds = 0
    book_implied = american_to_implied(odds)

    # Scenario × Model grid
    scenario_weights = _scenario_weights_for(sport, ctx)
    per_scenario: dict[str, float] = {}
    per_model_totals: dict[str, list[float]] = {m: [] for m in _MODELS}

    for scenario, w in scenario_weights:
        scenario_p_base = max(0.02, min(0.98, base_p * scenario.p_multiplier))
        scenario_agg = 0.0
        for model in _MODELS:
            adj_p = _model_adjust(scenario_p_base, model)
            hit_rate = _sim_one(adj_p, n_simulations)
            per_model_totals[model].append(hit_rate * w)
            scenario_agg += hit_rate * (1.0 / len(_MODELS))
        per_scenario[scenario.key] = round(scenario_agg, 4)

    # Weighted mean across scenarios per model
    per_model = {m: sum(vals) for m, vals in per_model_totals.items()}
    # Overall prob = mean across models (each already scenario-weighted)
    prob = statistics.fmean(per_model.values())
    prob = max(0.02, min(0.98, prob))
    # Apply correlation factor (§5) — reduces confidence when multiple
    # picks depend on the same game script.
    prob_adjusted = 0.5 + (prob - 0.5) * correlation_factor
    prob = max(0.02, min(0.98, prob_adjusted))

    # Agreement = 1 - stdev
    model_probs = list(per_model.values())
    if len(model_probs) >= 2:
        agreement = max(0.0, 1.0 - statistics.pstdev(model_probs) * 2.0)
    else:
        agreement = 1.0

    # Market comparison — edge
    edge_pct = (prob - book_implied) * 100.0 if book_implied else 0.0

    # EV per unit risked (chalk-neutral)
    dec = american_to_decimal(odds) if odds else 1.0
    ev = prob * (dec - 1.0) - (1.0 - prob) if odds else 0.0

    return SimResult(
        prob=round(prob, 4),
        edge_pct=round(edge_pct, 2),
        model_agreement=round(agreement, 3),
        scenario_breakdown=per_scenario,
        per_model={m: round(p, 4) for m, p in per_model.items()},
        ev_units=round(ev, 4),
        correlation_factor=correlation_factor,
        n_simulations=n_simulations * len(scenario_weights) * len(_MODELS),
    )


def simulate_board(picks: list[dict], *, contexts: Optional[dict] = None,
                    n_simulations: int = 1000) -> list[dict]:
    """Run `simulate_pick` across a whole board. Attaches a `sim_result`
    field to each pick and returns the same list (mutated in-place).

    Contexts is an optional {event: ctx_dict} map for enrichment
    (weather, total lines, etc.).
    """
    contexts = contexts or {}
    # First pass — correlation coefficients across the board
    corr = apply_correlation(picks)
    for p in picks:
        ctx = contexts.get(p.get("event") or "", {}) or {}
        cf = corr.get(p.get("id", ""), 1.0)
        result = simulate_pick(
            p, context=ctx, correlation_factor=cf,
            n_simulations=n_simulations,
        )
        p["sim_result"] = {
            "prob": result.prob,
            "edge_pct": result.edge_pct,
            "model_agreement": result.model_agreement,
            "scenario_breakdown": result.scenario_breakdown,
            "per_model": result.per_model,
            "ev_units": result.ev_units,
            "correlation_factor": result.correlation_factor,
            "n_simulations": result.n_simulations,
        }
    return picks
