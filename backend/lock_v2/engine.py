"""Lock Engine V2 — shadow scoring computation.

Given a pick dict (as returned by sports_engine._build_pick), compute a
battery of opposing-case + survivability + simulation scores, then a
shadow `lock_score_v2` mapped into the 90-99 branding range.

SAFETY: this module ONLY reads from a pick and returns a sidecar dict
of extra fields. It never mutates the existing `lock_score` / `grade`.
The caller decides how / if to merge the shadow fields into the pick.
"""
from __future__ import annotations

import math
import os
import datetime as _dt
from typing import Any

V2_ENABLED: bool = (
    os.environ.get("ENABLE_COUNTER_ENGINE", "true").lower() in ("true", "1", "yes", "on")
)

# ---------------------------------------------------------------------------
# Tier mapping
# ---------------------------------------------------------------------------
TIER_BANDS = [
    (90, 92, "Elite Setup"),
    (93, 95, "Strong Lock"),
    (96, 98, "Rare Lock"),
    (99, 99, "Apex Lock"),
]


def _tier(lock_v2: float) -> str:
    v = int(round(lock_v2))
    for lo, hi, name in TIER_BANDS:
        if lo <= v <= hi:
            return name
    return "Below Threshold" if v < 90 else "Apex Lock"


# ---------------------------------------------------------------------------
# Sport-specific variance baselines (higher = more chaotic)
# ---------------------------------------------------------------------------
_VARIANCE_BASE = {
    "MLB":    35.0,   # baseball variance is high — single AB swings outcomes
    "Soccer": 32.0,   # low-scoring, 1 goal = the whole game
    "Tennis": 22.0,   # generally most predictable head-to-head
    "UFC":    40.0,   # KO sport, huge variance
    "NBA":    20.0,   # high possession count = stable
    "NFL":    28.0,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _factor_avg(factors: dict, names: list[str]) -> float | None:
    """Return mean of any factor whose name contains any of `names` (substring)."""
    if not factors:
        return None
    hits = []
    for k, v in factors.items():
        kl = (k or "").lower()
        for n in names:
            if n in kl:
                try:
                    hits.append(float(v))
                except Exception:
                    pass
                break
    if not hits:
        return None
    return sum(hits) / len(hits)


def _is_chalk(implied: float) -> bool:
    return implied >= 0.80


def _is_heavy_chalk(implied: float) -> bool:
    return implied >= 0.88


# ---------------------------------------------------------------------------
# Counter Engine — opposing case strength (0-100, higher = more pushback)
# ---------------------------------------------------------------------------
def _counter_score(pick: dict[str, Any]) -> tuple[float, list[tuple[str, str, str]]]:
    sport = pick.get("sport") or ""
    market = (pick.get("market") or "").lower()
    factors = pick.get("factors") or {}
    win_prob = float(pick.get("win_probability") or 0) / 100.0
    implied = float(pick.get("implied_probability") or 0) / 100.0
    edge = float(pick.get("edge_percent") or 0)
    is_alt = bool(pick.get("is_alt"))
    is_long_shot = bool(pick.get("is_long_shot"))
    lock_old = float(pick.get("lock_score") or 0)
    reasons: list[tuple[str, str, str]] = []
    score = 0.0

    # 1. Recent form regression (uses Recent Form / Form / Streak / L10 factors)
    form = _factor_avg(factors, ["recent form", "form", "streak", "l10", "l5"])
    if form is not None and form < 0.45:
        d = (0.45 - form) * 40  # up to 18 pts
        score += d
        reasons.append(("warn", "Recent form", f"Form trend weak ({form*100:.0f}/100)"))

    # 2. Market overpricing — implied is high but our model edge is thin
    if implied >= 0.75 and edge < 2.0 and not is_long_shot:
        d = _clamp((implied - 0.75) * 60 + (2.0 - edge) * 2, 0, 18)
        score += d
        reasons.append(("warn", "Market overpriced",
                        f"Book {implied*100:.0f}% implied with only {edge:+.1f}% edge"))

    # 3. Variance — sport baseline + extra for over/under props
    base_var = _VARIANCE_BASE.get(sport, 25.0)
    if "over" in market or "under" in market:
        base_var += 5.0
    if is_alt:
        base_var -= 3.0  # alt lines are buffered by lower thresholds
    var_pts = _clamp(base_var * 0.4, 0, 18)
    score += var_pts
    if base_var >= 35:
        reasons.append(("warn", "Sport variance",
                        f"{sport} carries elevated outcome variance"))

    # 4. Public bias — heavy chalk that the model isn't equally convinced about
    if _is_chalk(implied) and win_prob < implied - 0.05:
        d = _clamp((implied - win_prob) * 50, 0, 14)
        score += d
        reasons.append(("warn", "Public bias",
                        f"Public on chalk; model {win_prob*100:.0f}% vs book {implied*100:.0f}%"))

    # 5. Schedule spots — deep-night UTC = late-game / cross-country
    et = pick.get("event_time")
    if et:
        try:
            ts = _dt.datetime.fromisoformat(str(et).replace("Z", "+00:00"))
            hour_utc = ts.astimezone(_dt.timezone.utc).hour
            if hour_utc in (3, 4, 5):  # ~10pm-1am ET starts — typical fatigue spot
                score += 6
                reasons.append(("warn", "Schedule spot",
                                "Late-window kickoff often produces dropoffs"))
        except Exception:
            pass

    # 6. Opponent style mismatch — if we have Matchup factor and it's weak
    matchup = _factor_avg(factors, ["matchup", "style", "h2h"])
    if matchup is not None and matchup < 0.50:
        d = (0.50 - matchup) * 30
        score += d
        reasons.append(("warn", "Matchup risk",
                        f"Matchup factor only {matchup*100:.0f}/100"))

    # 7. Lock inflation flag — old lock 96+ but win_prob unexceptional
    if lock_old >= 96 and win_prob < 0.60 and implied < 0.80:
        score += 8
        reasons.append(("warn", "Lock inflation",
                        f"Old lock {lock_old:.0f} but win_prob only {win_prob*100:.0f}%"))

    # 8-10. Reserved for future data sources:
    #   - Surface/environment (needs court / weather feed)
    #   - Fatigue/travel (needs team travel log)
    #   - Injury uncertainty (needs injury feed)
    # Default contribution: 0. These factors stay at 0 until plumbed.

    return _clamp(score, 0, 100), reasons


# ---------------------------------------------------------------------------
# Survivability — edge-removal resilience (0-100)
# ---------------------------------------------------------------------------
def _survival_score(pick: dict[str, Any]) -> tuple[float, list[tuple[str, str, str]], float]:
    """Run 5 edge-removal scenarios and measure pick resilience.

    Returns (survival_score 0-100, reasons, simulation_pass_rate 0-100).
    Each scenario "passes" if (win_prob - penalty) > (implied - 0.02) —
    i.e. the pick still has near-positive expectation without that edge.
    """
    win_prob = float(pick.get("win_probability") or 0) / 100.0
    implied = float(pick.get("implied_probability") or 0) / 100.0
    threshold = implied - 0.02

    # Each scenario removes one edge type and applies a penalty to win_prob
    scenarios = [
        ("Surface edge",  0.025),
        ("Form edge",     0.030),
        ("Matchup edge",  0.025),
        ("Market edge",   0.020),
        ("Recent streak", 0.020),
    ]
    passes: list[tuple[str, bool, float]] = []
    for name, penalty in scenarios:
        adj = win_prob - penalty
        ok = adj > threshold
        passes.append((name, ok, adj))

    pass_count = sum(1 for _, ok, _ in passes)
    sim_rate = (pass_count / len(scenarios)) * 100

    # Survival = avg residual confidence across scenarios, normalized
    avg_residual = sum(adj for _, _, adj in passes) / len(passes)
    # Map avg_residual [0.40, 0.90] -> [40, 100]
    survival = _clamp(40 + (avg_residual - 0.40) * 120, 0, 100)

    reasons: list[tuple[str, str, str]] = []
    for name, ok, adj in passes:
        mark = "ok" if ok else "warn"
        verb = "Wins" if ok else "Fragile"
        reasons.append((mark, name, f"{verb} without {name.lower()} ({adj*100:.0f}%)"))

    return survival, reasons, sim_rate


# ---------------------------------------------------------------------------
# Evidence + Conviction
# ---------------------------------------------------------------------------
def _evidence_score(pick: dict[str, Any]) -> float:
    """Sum positive factors minus a fixed negative baseline. Returns 0-100."""
    factors = pick.get("factors") or {}
    if not factors:
        return 60.0  # neutral
    vals = [float(v) for v in factors.values() if isinstance(v, (int, float))]
    if not vals:
        return 60.0
    positive_total = sum(vals) / len(vals)  # 0-100 average factor strength
    return _clamp(positive_total, 0, 100)


def _agreement_score(pick: dict[str, Any]) -> float:
    """Model-vs-book agreement. Higher when win_prob ≈ implied."""
    win_prob = float(pick.get("win_probability") or 0) / 100.0
    implied = float(pick.get("implied_probability") or 0) / 100.0
    if implied <= 0:
        return 50.0
    diff = abs(win_prob - implied)
    # diff 0 -> 100, diff 0.10 -> 80, diff 0.20 -> 60
    return _clamp(100 - diff * 200, 0, 100)


# ---------------------------------------------------------------------------
# Variance score (separate from counter — used directly in gates)
# ---------------------------------------------------------------------------
def _variance_score(pick: dict[str, Any]) -> float:
    sport = pick.get("sport") or ""
    market = (pick.get("market") or "").lower()
    base = _VARIANCE_BASE.get(sport, 25.0)
    if "over" in market or "under" in market:
        base += 5
    if pick.get("is_long_shot"):
        base += 10
    return _clamp(base, 0, 100)


# ---------------------------------------------------------------------------
# Apex gates (only path to 99)
# ---------------------------------------------------------------------------
APEX_GATES = {
    "win_prob":  0.54,
    "edge":      7.0,
    "counter":   10.0,    # MUST be <=
    "survival":  90.0,    # MUST be >=
    "variance":  15.0,    # MUST be <=
    "sim":       90.0,    # MUST be >=
    "agreement": 85.0,    # MUST be >=
}


def _check_apex(metrics: dict[str, float]) -> tuple[bool, list[str]]:
    fails = []
    if metrics["win_prob"] < APEX_GATES["win_prob"]:
        fails.append(f"win_prob {metrics['win_prob']*100:.0f}% < 54%")
    if metrics["edge"] < APEX_GATES["edge"]:
        fails.append(f"edge {metrics['edge']:.1f}% < 7%")
    if metrics["counter"] > APEX_GATES["counter"]:
        fails.append(f"counter {metrics['counter']:.0f} > 10")
    if metrics["survival"] < APEX_GATES["survival"]:
        fails.append(f"survival {metrics['survival']:.0f} < 90")
    if metrics["variance"] > APEX_GATES["variance"]:
        fails.append(f"variance {metrics['variance']:.0f} > 15")
    if metrics["sim"] < APEX_GATES["sim"]:
        fails.append(f"sim {metrics['sim']:.0f}% < 90%")
    if metrics["agreement"] < APEX_GATES["agreement"]:
        fails.append(f"agreement {metrics['agreement']:.0f} < 85")
    return (len(fails) == 0), fails


# ---------------------------------------------------------------------------
# Main API: compute_v2_shadow(pick) -> sidecar dict
# ---------------------------------------------------------------------------
def compute_v2_shadow(pick: dict[str, Any]) -> dict[str, Any]:
    """Compute Lock V2 shadow fields. Pure: does not mutate the pick.

    Returns a dict that the caller can `pick.update(...)` to attach the
    shadow scores. Never raises — on any error returns an empty dict.
    """
    try:
        evidence = _evidence_score(pick)
        counter, counter_reasons = _counter_score(pick)
        survival, survival_reasons, sim_pass = _survival_score(pick)
        agreement = _agreement_score(pick)
        variance = _variance_score(pick)

        # raw_confidence per user spec: positive - negative
        raw_confidence = _clamp(evidence - counter * 0.5, 0, 100)

        # conviction = (agreement × survivability × sim_pass) / 10000
        # normalised to 0-100 by taking geometric-style avg
        conviction = (agreement + survival + sim_pass) / 3

        # Lock V2 = raw_confidence*0.55 + conviction*0.45 — result in [0, 100]
        raw = raw_confidence * 0.55 + conviction * 0.45

        # Map raw [0, 100] to the 90-99 branding range with a piecewise curve
        # that keeps tier counts realistic (Apex rare, Rare uncommon, Strong
        # common, Elite default). Calibration anchors:
        #   raw  ≤ 50  → 90.0  (Elite floor)
        #   raw 50-70  → 90-92 (Elite Setup)
        #   raw 70-82  → 92-95 (Strong Lock)
        #   raw 82-92  → 95-98 (Rare Lock)
        #   raw ≥ 92   → 98.0  (Apex gate required to advance to 99)
        if raw <= 50:
            lock_v2 = 90.0
        elif raw <= 70:
            lock_v2 = 90.0 + (raw - 50) * (2.0 / 20.0)
        elif raw <= 82:
            lock_v2 = 92.0 + (raw - 70) * (3.0 / 12.0)
        elif raw <= 92:
            lock_v2 = 95.0 + (raw - 82) * (3.0 / 10.0)
        else:
            lock_v2 = 98.0

        # Apex gating: cap at 98 unless all 7 gates pass
        win_prob = float(pick.get("win_probability") or 0) / 100.0
        edge = float(pick.get("edge_percent") or 0)
        is_apex, fail_reasons = _check_apex({
            "win_prob":  win_prob,
            "edge":      edge,
            "counter":   counter,
            "survival":  survival,
            "variance":  variance,
            "sim":       sim_pass,
            "agreement": agreement,
        })
        if not is_apex and lock_v2 >= 98.5:
            lock_v2 = 98.0  # block 99 unless apex gates pass

        lock_v2_rounded = round(lock_v2, 1)
        tier = _tier(lock_v2_rounded)

        # Build positive evidence reasons from factors
        evidence_reasons: list[tuple[str, str, str]] = []
        for k, v in (pick.get("factors") or {}).items():
            try:
                fv = float(v)
                if fv >= 75:
                    evidence_reasons.append(("ok", k, f"{int(fv)}/100"))
            except Exception:
                continue
        evidence_reasons = evidence_reasons[:4]

        return {
            "evidence_score":    round(evidence, 1),
            "conviction_score":  round(conviction, 1),
            "counter_score":     round(counter, 1),
            "survival_score":    round(survival, 1),
            "variance_score":    round(variance, 1),
            "simulation_pass":   round(sim_pass, 1),
            "agreement_score":   round(agreement, 1),
            "lock_score_v2":     lock_v2_rounded,
            "tier_v2":           tier,
            "is_apex":           bool(is_apex),
            "apex_blockers":     fail_reasons if not is_apex else [],
            "v2_reasons": {
                "evidence": evidence_reasons,
                "counter":  counter_reasons,
                "survival": survival_reasons,
            },
            "v2_engine_version": "1.0.0",
        }
    except Exception as e:
        # Shadow mode — never block production picks on a v2 error
        return {"v2_error": str(e)[:200]}
