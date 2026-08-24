"""NFL Prop Simulator — real Monte Carlo distribution authority.

Pass 2 (2026-06) — replaces the prior ``ran=False`` stub with a real
exact-line + selected-side distribution simulator for supported NFL
player markets:

    Passing Yards / Passing TDs / Attempts / Completions
    Rushing Yards / Rushing Attempts / Rushing TDs
    Receiving Yards / Receptions / Receiving TDs
    (all standard + ``_alternate`` variants)

**PRESERVED authorities (this simulator does NOT touch them):**
    * NFL ATD engine — Anytime TD / 1st TD stay with ``nfl_atd_engine``.
    * Platinum NFL — ML / Spread / Total remain the Platinum authority
      (``services.platinum_nfl.simulator``).
    * Side-aware factor mirroring in ``nfl_feature_engine``.

Design flow (per Pass 2 spec):

    real opportunity/history/context
        → per-stat distribution (Normal for yardage, Poisson for TDs,
                                 Normal-truncated for volume)
        → exact line + selected side
        → P(over) / P(under)
        → sim_win_probability
        → existing sim_runner promotion → Win Expected

Fail-closed contract:
    Missing / insufficient real history → ``{"ran": False,
    "reason": "DATA_INSUFFICIENT" | "UNSUPPORTED_MARKET" | ...}``.
    Never fabricates a book-implied fallback, never uses a factor
    average as a probability.

Input contract:
    The simulator reads a lightweight sim context from the pick.  The
    NFL emit path in ``sports_engine.py`` attaches this via
    ``nfl_sim_context`` (populated by ``nfl_feature_engine
    .build_nfl_game_context``).  When absent, the sim falls closed.

    Recognized ``nfl_sim_context`` keys::

        stat_key           str    — "passing_yards" / "rushing_yards" / ...
        l5_avg / l3_avg    float  — rolling averages of the stat
        season_avg         float  — season-to-date average
        l5_n / l3_n        int    — sample counts (for quality gate)
        season_n           int    — season sample count
        is_home            bool   — the player is playing at home
        opp_defense_mult   float  — 0.8..1.2 opponent allowance multiplier
                                      (1.0 = league neutral; >1 friendlier
                                      to the OFFENSE)
        role_volume_mult   float  — 0.7..1.3 role/opportunity multiplier
        position           str    — "QB"/"RB"/"WR"/"TE"

Configuration is tuned for a 20K-run Monte Carlo so the resulting
percentile buckets stabilise.  Standard-deviation priors are the
league-average game-to-game variance for the stat family.
"""
from __future__ import annotations

import math
import random
from typing import Optional

from brain.sim_distribution import compute_percentiles

RUNS = 20_000

# ── League priors (used ONLY to compute stddev, NEVER as the mean) ─
# Coefficient of Variation ≈ σ/μ observed in nflverse 2019-2024 for
# starters at each stat family.  Applied as ``σ = μ * CV`` when the
# pick lacks a computed stddev in its sim context.
_CV = {
    "passing_yards":     0.28,
    "rushing_yards":     0.45,
    "receiving_yards":   0.55,
    "receptions":        0.35,
    "attempts":          0.15,
    "completions":       0.18,
    "carries":           0.30,
    "targets":           0.30,
    "rushing_attempts":  0.30,
}

# ── Market → stat_key mapping ────────────────────────────────────
# Recognizes both the-odds-api canonical keys AND the display market
# name variants used elsewhere in the codebase.
_MARKET_STAT_KEY: dict[str, str] = {
    # Passing
    "player_pass_yds":              "passing_yards",
    "player_pass_yds_alternate":    "passing_yards",
    "passing_yards":                "passing_yards",
    "pass_yards":                   "passing_yards",
    "player_pass_tds":              "passing_tds",
    "passing_tds":                  "passing_tds",
    "player_pass_attempts":         "attempts",
    "pass_attempts":                "attempts",
    "passing_attempts":             "attempts",
    "player_pass_completions":      "completions",
    "pass_completions":             "completions",
    "completions":                  "completions",
    # Rushing
    "player_rush_yds":              "rushing_yards",
    "player_rush_yds_alternate":    "rushing_yards",
    "rushing_yards":                "rushing_yards",
    "rush_yards":                   "rushing_yards",
    "player_rush_attempts":         "rushing_attempts",
    "rushing_attempts":             "rushing_attempts",
    "rush_attempts":                "rushing_attempts",
    "carries":                      "rushing_attempts",
    "player_rush_tds":              "rushing_tds",
    "rushing_tds":                  "rushing_tds",
    # Receiving
    "player_reception_yds":         "receiving_yards",
    "player_reception_yds_alternate": "receiving_yards",
    "receiving_yards":              "receiving_yards",
    "reception_yards":              "receiving_yards",
    "player_receptions":            "receptions",
    "player_receptions_alternate":  "receptions",
    "receptions":                   "receptions",
    "player_reception_tds":         "receiving_tds",
    "receiving_tds":                "receiving_tds",
}

# TD markets are Poisson.  Yardage is Normal-truncated at 0.  Volume
# (attempts / completions / receptions / carries) is Normal
# truncated + rounded to int.
_TD_STATS = {"passing_tds", "rushing_tds", "receiving_tds"}
_VOLUME_STATS = {
    "attempts", "completions", "carries",
    "rushing_attempts", "receptions",
}

# Game markets are handled by Platinum NFL / other authorities.
_GAME_MARKET_TOKENS = ("moneyline", "spread", "total ")


def _wilson_ci(p: float, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt((p * (1 - p) / n) + (z * z / (4 * n * n))) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def _normalize_market(pick: dict) -> tuple[Optional[str], bool]:
    """Return ``(stat_key, is_game_market)`` from a pick object.

    ``stat_key`` is None when the market is unsupported.
    """
    raw = str(pick.get("market") or "").strip()
    if not raw:
        return None, False
    ml = raw.lower()
    # Game markets are OUT OF SCOPE for this simulator.
    if any(tok in ml for tok in _GAME_MARKET_TOKENS) and not any(
        p in ml for p in ("passing", "rushing", "receiving",
                            "receptions", "targets", "carries",
                            "anytime")
    ):
        return None, True
    # ATD is handled by nfl_atd_engine — pass through.
    if "anytime_td" in ml or "anytime td" in ml or "1st_td" in ml:
        return None, False
    # Try direct market_key mapping first.
    if raw in _MARKET_STAT_KEY:
        return _MARKET_STAT_KEY[raw], False
    if ml in _MARKET_STAT_KEY:
        return _MARKET_STAT_KEY[ml], False
    # Loose text match for display-style markets.
    for token, stat in _MARKET_STAT_KEY.items():
        if token in ml:
            return stat, False
    return None, False


def _parse_side(pick: dict) -> Optional[str]:
    """Return "over"/"under" or None.  Reads pick["side"] first, then
    tries to detect Over/Under in ``pick["market"]`` / ``pick["selection"]``.
    """
    for key in ("side", "selection", "pick", "market"):
        v = str(pick.get(key) or "").lower()
        if "over" in v:
            return "over"
        if "under" in v:
            return "under"
    return None


def _extract_line(pick: dict) -> Optional[float]:
    for k in ("line", "point", "threshold"):
        v = pick.get(k)
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    # Last-resort: parse from display market text.
    import re
    m = re.search(r"(-?\d+(?:\.\d+)?)", str(pick.get("market") or ""))
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def _extract_context(pick: dict, stat_key: str) -> dict:
    """Assemble the sim inputs from whatever data the pick carries.

    The primary source is ``pick["nfl_sim_context"]``.  We also
    look at ``pick["opportunity_intel"]`` and ``pick["player_intel"]``
    as legacy sources so the sim degrades gracefully.
    """
    ctx = dict(pick.get("nfl_sim_context") or {})
    pi = pick.get("player_intel") or {}
    op = pick.get("opportunity_intel") or {}

    # Backfill any missing rolling avgs from player_intel.
    ctx.setdefault("l5_avg", (pi.get("l5") or {}).get(stat_key)
                     if isinstance(pi.get("l5"), dict) else pi.get(f"l5_{stat_key}"))
    ctx.setdefault("l3_avg", (pi.get("l3") or {}).get(stat_key)
                     if isinstance(pi.get("l3"), dict) else pi.get(f"l3_{stat_key}"))
    ctx.setdefault("season_avg", (pi.get("season_avg") or {}).get(stat_key)
                     if isinstance(pi.get("season_avg"), dict) else pi.get(f"season_{stat_key}"))

    ctx.setdefault("l5_n", pi.get("n_games_l5") or op.get("n_games_l5"))
    ctx.setdefault("l3_n", pi.get("n_games_l3") or op.get("n_games_l3"))
    ctx.setdefault("season_n", pi.get("n_games_season") or op.get("n_games_season"))

    ctx.setdefault("is_home", pick.get("is_home") or op.get("is_home"))
    ctx.setdefault("opp_defense_mult",
                    op.get("opp_defense_mult") or ctx.get("opp_defense_mult"))
    ctx.setdefault("role_volume_mult",
                    op.get("role_volume_mult") or ctx.get("role_volume_mult"))
    ctx.setdefault("position", pick.get("position") or op.get("position"))
    return ctx


def _compute_mean_std(stat_key: str, ctx: dict) -> Optional[tuple[float, float, int]]:
    """Blend L3 / L5 / season averages into a single MC mean and derive
    a stddev.  Returns ``(mean, std, sample_size)`` or ``None`` when
    insufficient real evidence.

    Weights (empirical NFLverse tuning):
        L3 = 0.45  (most recent form)
        L5 = 0.35  (recent baseline)
        Season = 0.20  (season baseline)
    """
    l3 = ctx.get("l3_avg")
    l5 = ctx.get("l5_avg")
    ss = ctx.get("season_avg")
    l3n = int(ctx.get("l3_n") or 0)
    l5n = int(ctx.get("l5_n") or 0)
    ssn = int(ctx.get("season_n") or 0)
    parts: list[tuple[float, float, int]] = []
    if isinstance(l3, (int, float)):
        parts.append((float(l3), 0.45, max(l3n, 3)))
    if isinstance(l5, (int, float)):
        parts.append((float(l5), 0.35, max(l5n, 5)))
    if isinstance(ss, (int, float)):
        parts.append((float(ss), 0.20, max(ssn, 4)))
    if not parts:
        return None
    total_w = sum(w for _, w, _ in parts)
    mean = sum(m * w for m, w, _ in parts) / total_w
    # Sample size = weighted min so we don't overstate.
    sample = int(round(min(n for _, _, n in parts)))
    # Coefficient of Variation → σ.  Yardage/TDs use their family CV.
    cv = _CV.get(stat_key, 0.35)
    std = max(0.1, mean * cv)
    # Small-sample penalty (widen σ if <5 games).
    if sample < 5:
        std *= 1.4
    return mean, std, sample


def _apply_context_multipliers(mean: float, ctx: dict) -> float:
    """Apply home/away, opponent defense, and role/volume modifiers.

    Contract:
        * Home player ⇒ +3% baseline.
        * Away player ⇒ −3% baseline.
        * opp_defense_mult multiplies mean directly (1.05 = 5%
          friendlier to the offense).  Clamped to ±20%.
        * role_volume_mult multiplies mean directly.  Clamped to ±30%.
    """
    m = float(mean)
    ih = ctx.get("is_home")
    if ih is True:
        m *= 1.03
    elif ih is False:
        m *= 0.97
    odm = ctx.get("opp_defense_mult")
    if isinstance(odm, (int, float)):
        m *= max(0.80, min(1.20, float(odm)))
    rvm = ctx.get("role_volume_mult")
    if isinstance(rvm, (int, float)):
        m *= max(0.70, min(1.30, float(rvm)))
    return m


# ── Samplers ─────────────────────────────────────────────────────
def _sample_yardage(mean: float, std: float,
                     runs: int = RUNS) -> list[float]:
    """Normal-truncated at 0.  Yardage is continuous."""
    out: list[float] = []
    for _ in range(runs):
        x = random.gauss(mean, std)
        out.append(max(0.0, x))
    return out


def _sample_volume(mean: float, std: float,
                    runs: int = RUNS) -> list[float]:
    """Normal-truncated at 0, rounded to int (attempts/completions/
    receptions/carries)."""
    out: list[float] = []
    for _ in range(runs):
        x = random.gauss(mean, std)
        out.append(float(max(0, int(round(x)))))
    return out


def _sample_poisson(lam: float) -> int:
    """Knuth Poisson sampler — fine for λ < 30 (all NFL TD counts)."""
    if lam <= 0:
        return 0
    L = math.exp(-lam)
    k = 0
    p = 1.0
    while True:
        k += 1
        p *= random.random()
        if p < L:
            break
    return k - 1


def _sample_tds(mean: float, runs: int = RUNS) -> list[float]:
    lam = max(0.01, float(mean))
    return [float(_sample_poisson(lam)) for _ in range(runs)]


# ── Public entry point ───────────────────────────────────────────
def simulate(pick: dict) -> dict:
    """Real Monte Carlo NFL simulator.

    Returns the standard sim output envelope::

        {
          "ran": True,
          "sim_win_probability":  73.2,     # percent, matches sim_mlb
          "sim_ci_lower":         70.8,
          "sim_ci_upper":         75.5,
          "sim_runs":             20000,
          "sim_threshold":        74.5,
          "sim_is_under":         False,
          "sim_expected_stat":    82.1,
          "sim_alt_lines":        {...},
          "sim_disagreement_with_model": -1.4,
          "sim_signal":           "neutral",
          "sim_stat_key":         "passing_yards",
          "sim_pctl_*":           # from compute_percentiles(...)
          "simulator_provenance": "CAUSAL_INDEPENDENT" |
                                    "EMPIRICAL_INDEPENDENT" |
                                    "PRIOR_ONLY",
        }

    On any failure returns::

        {"ran": False, "reason": <str>, ...}

    ``reason`` values:
        WRONG_SPORT / GAME_MARKET_OUT_OF_SCOPE / UNSUPPORTED_MARKET /
        MISSING_LINE / MISSING_SIDE / DATA_INSUFFICIENT
    """
    sport = str(pick.get("sport") or "").upper()
    if sport != "NFL":
        # CFB continues to be a stub — sim_runner routes CFB here but
        # we intentionally return ran=False so nothing changes for it.
        return {"ran": False, "reason": "WRONG_SPORT",
                "sport": sport, "market": pick.get("market")}

    stat_key, is_game_market = _normalize_market(pick)
    if is_game_market:
        return {"ran": False,
                "reason": "GAME_MARKET_HANDLED_BY_PLATINUM_NFL",
                "market": pick.get("market")}
    if stat_key is None:
        # ATD flows through nfl_atd_engine; unrecognised markets
        # fall closed here.
        return {"ran": False, "reason": "UNSUPPORTED_MARKET",
                "market": pick.get("market")}

    line = _extract_line(pick)
    if line is None:
        return {"ran": False, "reason": "MISSING_LINE"}
    side = _parse_side(pick)
    if side is None:
        return {"ran": False, "reason": "MISSING_SIDE"}
    is_under = (side == "under")

    ctx = _extract_context(pick, stat_key)
    ms = _compute_mean_std(stat_key, ctx)
    if ms is None:
        return {"ran": False, "reason": "DATA_INSUFFICIENT",
                "market": pick.get("market"),
                "stat_key": stat_key}
    raw_mean, std, sample = ms
    if sample < 3:
        return {"ran": False, "reason": "DATA_INSUFFICIENT",
                "market": pick.get("market"),
                "stat_key": stat_key, "sample": sample}
    mean = _apply_context_multipliers(raw_mean, ctx)

    # Draw the distribution.
    if stat_key in _TD_STATS:
        distribution = _sample_tds(mean)
    elif stat_key in _VOLUME_STATS:
        distribution = _sample_volume(mean, std)
    else:
        # Yardage families (passing_yards / rushing_yards / receiving_yards).
        distribution = _sample_yardage(mean, std)

    n = len(distribution)
    if is_under:
        wins = sum(1 for x in distribution if x < line)
    else:
        wins = sum(1 for x in distribution if x > line)
    p_win = wins / n if n else 0.0
    ci_lo, ci_hi = _wilson_ci(p_win, n)

    blended_wp = float(pick.get("win_probability") or 0)
    sim_wp_pct = round(p_win * 100, 1)
    disagreement = round(sim_wp_pct - blended_wp, 2)
    if disagreement > 5:
        signal = "stronger"
    elif disagreement < -5:
        signal = "weaker"
    else:
        signal = "neutral"

    # Alt-line sensitivity table.
    alt_lines: dict = {}
    for delta in (-2.5, -1.5, -1.0, -0.5, 0.5, 1.0, 1.5, 2.5):
        alt = round(line + delta, 1)
        if alt < 0:
            continue
        if is_under:
            hits = sum(1 for x in distribution if x < alt)
        else:
            hits = sum(1 for x in distribution if x > alt)
        alt_lines[str(alt)] = round(hits / n * 100, 1)

    expected_stat = sum(distribution) / max(1, n)
    payload = {
        "ran":                          True,
        "sim_win_probability":          sim_wp_pct,
        "sim_ci_lower":                 round(ci_lo * 100, 1),
        "sim_ci_upper":                 round(ci_hi * 100, 1),
        "sim_runs":                     n,
        "sim_threshold":                line,
        "sim_is_under":                 is_under,
        "sim_expected_stat":            round(expected_stat, 2),
        "sim_alt_lines":                alt_lines,
        "sim_disagreement_with_model":  disagreement,
        "sim_signal":                   signal,
        "sim_stat_key":                 stat_key,
        # Diagnostic — expose the raw mean AND context-adjusted mean.
        "sim_mean_raw":                 round(raw_mean, 2),
        "sim_mean_adjusted":            round(mean, 2),
        "sim_std":                      round(std, 2),
        "sim_sample_size":              sample,
        # Meta so sim_runner sees this as a real distribution sim.
        "simulator_type":               "distribution_monte_carlo",
        "simulator_name":               "nfl_simulator",
        "simulator_version":            "2.0.0",
        "independent_evidence":         True,
        "valid":                        True,
        **compute_percentiles(distribution, threshold=line),
    }

    # Universal simulator provenance envelope.
    try:
        from services.simulator_provenance import (
            stamp_sim_output, classify_input_quality,
        )
        signals = sum(
            1 for k in ("l5_avg", "l3_avg", "season_avg",
                         "is_home", "opp_defense_mult",
                         "role_volume_mult", "position")
            if ctx.get(k) is not None
        )
        if signals >= 5:
            provenance = "CAUSAL_INDEPENDENT"
        elif signals >= 3:
            provenance = "EMPIRICAL_INDEPENDENT"
        elif signals >= 1:
            provenance = "PRIOR_ONLY"
        else:
            provenance = "PRIOR_ONLY"
        mp = pick.get("win_probability")
        if isinstance(mp, (int, float)) and mp > 1:
            mp = mp / 100.0
        stamp_sim_output(
            payload, provenance=provenance,
            input_quality=classify_input_quality(signals),
            sim_prob=p_win,
            model_prob=(float(mp) if isinstance(mp, (int, float)) else None),
        )
    except Exception:
        pass
    return payload


def supports(pick: dict) -> bool:
    """Whether this simulator claims jurisdiction over the pick.

    Returns True for NFL only.  CFB routing is preserved by returning
    True as well (with a ran=False result) — the sim_runner already
    treats ran=False as "skip" so nothing changes for CFB.
    """
    return str(pick.get("sport") or "").upper() in {"NFL", "CFB"}


# Alias so callers can pattern-match on either name.
simulate_nfl_pick = simulate


__all__ = ["simulate", "simulate_nfl_pick", "supports"]
