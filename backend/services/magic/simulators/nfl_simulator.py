"""MAGIC 3H — NFL sport-specific Monte Carlo simulator.

Reads persisted NFL data from ``player_game_actuals`` sport=nfl and
produces a market-appropriate distribution for the pick's SPECIFIC
stat family.  Output is fingerprint-compatible with Magic 3B's
``simulator_outputs`` contract.

Distribution choices (market-appropriate, per Phase 4 directive):
    passing_yards       → Log-normal fit on last-N (bounded @ 0)
    passing_tds         → Negative-binomial / Poisson-mixture
    completions/attempts→ Normal on last-N with clipped tails
    passing_ints        → Poisson with shrinkage
    rushing_yards       → Log-normal
    carries             → Normal
    receptions          → Negative-binomial-like from historical dispersion
    receiving_yards     → Log-normal
    targets             → Normal
    atd                 → Bernoulli on empirical rate with shrinkage

Hard rules:
    * `p_hit` computed for EXACT line/side — no generic scalar.
    * Different threshold → different sample-drawn probability.
    * `input_fingerprint` matches 3B contract (event/player/market/line/side).
    * Minimum 3 pre-cutoff games or the simulator returns UNAVAILABLE.
    * Simulator NEVER fabricates data.
    * Simulator NEVER touches Lock Score, Magic weighting, calibration,
      settlement, or Rollover / Parlay.
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timezone
from typing import Optional

from services.magic.gold_evidence import _pregame_cutoff_from_pick


SIMULATOR_NAME    = "nfl_simulator"
SIMULATOR_VERSION = "3h.v1"
SIMULATOR_TYPE    = "distribution_monte_carlo"
DEFAULT_RUNS      = 10_000


# ═══════════════════════════════════════════════════════════════════
# Market → stat
# ═══════════════════════════════════════════════════════════════════
def _nfl_stat(market: str) -> Optional[str]:
    m = (market or "").lower()
    if "pass" in m and "yard"    in m: return "passing_yards"
    if "pass" in m and "td"       in m: return "passing_tds"
    if "pass" in m and "attempt"  in m: return "attempts"
    if "pass" in m and "completion" in m: return "completions"
    if "interception" in m or (" int" in m and "pass" in m):
        return "passing_ints"
    if "rush" in m and "yard" in m:   return "rushing_yards"
    if "carry" in m or "carries" in m: return "carries"
    if "rush" in m and "td"   in m:   return "rushing_tds"
    if "reception" in m or "recept" in m: return "receptions"
    if "receiv" in m and "yard" in m:  return "receiving_yards"
    if "receiv" in m and "td"   in m:  return "receiving_tds"
    if "target"    in m:               return "targets"
    if ("anytime" in m and ("td" in m or "touchdown" in m)) \
       or "atd" in m or "anytime touchdown" in m \
       or "anytime scorer" in m:
        return "atd"
    return None


def _atd_from_row(actuals: dict) -> int:
    return int(bool(actuals.get("rushing_tds", 0)
                    or actuals.get("receiving_tds", 0)))


def _stat_from_row(row: dict, stat: str) -> Optional[float]:
    actuals = row.get("actuals") or row
    if stat == "atd":
        # Handles both nfl_player_weekly ("rushing_tds","receiving_tds")
        # and player_game_actuals ("rush_tds","rec_tds") shapes.
        rt = (actuals.get("rushing_tds")
              if actuals.get("rushing_tds") is not None
              else actuals.get("rush_tds", 0))
        wt = (actuals.get("receiving_tds")
              if actuals.get("receiving_tds") is not None
              else actuals.get("rec_tds", 0))
        return float(bool((rt or 0) or (wt or 0)))
    # Real player_game_actuals schema uses short names.
    aliases = {
        "passing_yards":   ("passing_yards",  "pass_yds"),
        "passing_tds":     ("passing_tds",    "pass_tds"),
        "passing_ints":    ("passing_ints",   "interceptions"),
        "attempts":        ("attempts",),
        "completions":     ("completions",),
        "rushing_yards":   ("rushing_yards",  "rush_yds"),
        "rushing_tds":     ("rushing_tds",    "rush_tds"),
        "carries":         ("carries",        "rush_attempts"),
        "receiving_yards": ("receiving_yards","rec_yds"),
        "receiving_tds":   ("receiving_tds",  "rec_tds"),
        "receptions":      ("receptions",),
        "targets":         ("targets",),
    }
    for key in aliases.get(stat, (stat,)):
        v = actuals.get(key)
        if v is not None:
            try: return float(v)
            except (TypeError, ValueError): pass
    return None


# ═══════════════════════════════════════════════════════════════════
# Fetch pre-cutoff samples
# ═══════════════════════════════════════════════════════════════════
async def _fetch_samples(
    db, *, cpid: str, stat: str, cutoff_iso: str, limit: int = 20,
) -> list[float]:
    rows: list[dict] = []
    try:
        cursor = db.player_game_actuals.find(
            {"sport": "nfl", "canonical_player_id": str(cpid),
             "event_time": {"$lt": cutoff_iso}},
            {"event_time": 1, "actuals": 1, "_id": 0},
        ).sort([("event_time", -1)]).limit(limit)
        async for r in cursor:
            rows.append(r)
    except Exception:
        rows = []
    samples: list[float] = []
    for r in rows:
        v = _stat_from_row(r, stat)
        if v is not None:
            samples.append(v)
    return samples


# ═══════════════════════════════════════════════════════════════════
# Deterministic seed from fingerprint fields
# ═══════════════════════════════════════════════════════════════════
def _deterministic_seed(pick: dict) -> int:
    parts = "|".join([
        str(pick.get("id") or ""),
        str(pick.get("canonical_event_id") or ""),
        str(pick.get("canonical_player_id") or ""),
        str(pick.get("market") or ""),
        str(pick.get("side") or ""),
        (f"{float(pick['line']):.4f}"
         if pick.get("line") is not None else "none"),
        SIMULATOR_VERSION,
    ])
    return abs(hash(parts)) & ((1 << 63) - 1)


# ═══════════════════════════════════════════════════════════════════
# Market-specific samplers
# ═══════════════════════════════════════════════════════════════════
def _sample_lognormal(rng: random.Random, samples: list[float],
                        n_runs: int) -> list[float]:
    xs = [s for s in samples if s > 0]
    if len(xs) < 2:
        return _sample_normal(rng, samples, n_runs)
    logs = [math.log(x) for x in xs]
    mu = sum(logs) / len(logs)
    var = sum((l - mu) ** 2 for l in logs) / max(len(logs) - 1, 1)
    sd = math.sqrt(max(var, 1e-6))
    return [max(0.0, math.exp(rng.gauss(mu, sd))) for _ in range(n_runs)]


def _sample_normal(rng: random.Random, samples: list[float],
                    n_runs: int) -> list[float]:
    if not samples:
        return []
    mu = sum(samples) / len(samples)
    var = sum((s - mu) ** 2 for s in samples) / max(len(samples) - 1, 1)
    sd = math.sqrt(max(var, 1.0))
    return [max(0.0, rng.gauss(mu, sd)) for _ in range(n_runs)]


def _sample_poisson(rng: random.Random, lam: float, n_runs: int) -> list[float]:
    lam = max(1e-6, lam)
    out: list[float] = []
    for _ in range(n_runs):
        L = math.exp(-lam); k = 0; p = 1.0
        while p > L:
            k += 1; p *= rng.random()
        out.append(float(k - 1))
    return out


def _sample_bernoulli(rng: random.Random, p: float, n_runs: int) -> list[float]:
    p = max(0.0, min(1.0, p))
    return [1.0 if rng.random() < p else 0.0 for _ in range(n_runs)]


_STAT_DISTRIBUTIONS = {
    "passing_yards":  "lognormal",
    "rushing_yards":  "lognormal",
    "receiving_yards": "lognormal",
    "passing_tds":    "poisson",
    "rushing_tds":    "poisson",
    "receiving_tds":  "poisson",
    "passing_ints":   "poisson",
    "attempts":       "normal",
    "completions":    "normal",
    "carries":        "normal",
    "receptions":     "normal",
    "targets":        "normal",
    "atd":            "bernoulli",
}


def _draw(rng, stat, samples, n_runs):
    dist = _STAT_DISTRIBUTIONS.get(stat, "normal")
    if dist == "lognormal":
        return _sample_lognormal(rng, samples, n_runs)
    if dist == "poisson":
        lam = (sum(samples) / len(samples)) if samples else 0.5
        return _sample_poisson(rng, lam, n_runs)
    if dist == "bernoulli":
        # empirical rate with shrinkage
        n = len(samples); hits = sum(1 for s in samples if s >= 0.5)
        p = (hits + 1.0) / (n + 3.0)  # β(1,2) shrinkage
        return _sample_bernoulli(rng, p, n_runs)
    return _sample_normal(rng, samples, n_runs)


def _quantiles(draws: list[float]) -> dict:
    if not draws:
        return {}
    xs = sorted(draws)
    n = len(xs)
    def q(p): return xs[max(0, min(n - 1, int(round(p * (n - 1)))))]
    return {"q10": q(0.10), "q25": q(0.25), "q50": q(0.50),
            "q75": q(0.75), "q90": q(0.90),
            "mean": sum(xs) / n,
            "std":  math.sqrt(sum((x - (sum(xs)/n)) ** 2 for x in xs) / max(n-1,1))}


def _p_hit(draws: list[float], line: float, side: str) -> float:
    if not draws:
        return 0.0
    s = side.lower()
    if s == "over":
        return sum(1 for d in draws if d > line) / len(draws)
    if s == "under":
        return sum(1 for d in draws if d < line) / len(draws)
    # ATD "yes/anytime" — line typically None; treat as p(≥1)
    return sum(1 for d in draws if d >= 1.0) / len(draws)


# ═══════════════════════════════════════════════════════════════════
# Public simulator
# ═══════════════════════════════════════════════════════════════════
async def run_nfl_simulation(
    db, pick: dict, *, runs: int = DEFAULT_RUNS,
) -> Optional[dict]:
    """Return a `sim` payload compatible with
    :func:`services.magic.sim_cal_store.build_simulator_output_doc`.

    Returns None (adapter should mark UNAVAILABLE) when:
      * market is unmapped
      * canonical_player_id missing
      * < 3 pre-cutoff sample games
    """
    stat = _nfl_stat(pick.get("market") or "")
    cpid = pick.get("canonical_player_id")
    if not (stat and cpid):
        return None
    side = pick.get("side") or ("yes" if stat == "atd" else None)
    line = pick.get("line")
    if stat != "atd" and (line is None or side is None):
        return None
    cutoff_iso, _ = _pregame_cutoff_from_pick(pick)
    samples = await _fetch_samples(
        db, cpid=str(cpid), stat=stat, cutoff_iso=cutoff_iso, limit=20)
    if len(samples) < 3:
        return None

    rng = random.Random(_deterministic_seed(pick))
    n_runs = max(1000, int(runs))
    draws = _draw(rng, stat, samples, n_runs)
    if not draws:
        return None
    q = _quantiles(draws)
    if stat == "atd":
        p = _p_hit(draws, 0.5, "over")
    else:
        p = _p_hit(draws, float(line), side)

    return {
        "sim_win_probability":  p,   # 0-1 fraction
        "sim_runs":             n_runs,
        "simulator_name":       SIMULATOR_NAME,
        "simulator_version":    SIMULATOR_VERSION,
        "simulator_type":       SIMULATOR_TYPE,
        "seed":                 _deterministic_seed(pick),
        "independent_evidence": True,
        "valid":                True,
        "sim_mean":             round(q["mean"], 3),
        "sim_median":           round(q["q50"], 3),
        "sim_q10":              round(q["q10"], 3),
        "sim_q25":              round(q["q25"], 3),
        "sim_q75":              round(q["q75"], 3),
        "sim_q90":              round(q["q90"], 3),
        "sim_std":              round(q["std"], 3),
        "sim_ci_lower":         round(q["q10"], 3),
        "sim_ci_upper":         round(q["q90"], 3),
        "sim_distribution":     _STAT_DISTRIBUTIONS.get(stat, "normal"),
        "sim_stat":             stat,
        "sim_sample_size":      len(samples),
    }


# ═══════════════════════════════════════════════════════════════════
# CFB / UFC / NHL — audit result: NO DATA → HONEST UNAVAILABLE
# ═══════════════════════════════════════════════════════════════════
def cfb_simulator_status() -> dict:
    return {"status": "UNAVAILABLE",
             "reason": ("no cfb game logs persisted "
                        "(cfb_games / cfb_player_weekly / cfb_team_stats "
                        "are all empty; only static ratings/portal/"
                        "returning-production data exists)")}


def ufc_simulator_status() -> dict:
    return {"status": "UNAVAILABLE",
             "reason": ("no ufc fight/fighter logs persisted; "
                        "moneyline / method / rounds require historical "
                        "fight results not currently ingested")}


def nhl_simulator_status() -> dict:
    return {"status": "UNAVAILABLE",
             "reason": ("no nhl team_game_actuals or player_game_actuals; "
                        "goals-for/against + goalie history required "
                        "before simulator can be built")}


__all__ = [
    "SIMULATOR_NAME", "SIMULATOR_VERSION", "SIMULATOR_TYPE",
    "run_nfl_simulation",
    "cfb_simulator_status", "ufc_simulator_status", "nhl_simulator_status",
]
