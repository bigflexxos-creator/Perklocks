"""Platinum NFL player-market simulation (Block 2B.1A §5, §7, §8, §15).

Simulates NFL player markets **through the opportunity-before-outcome
chain** — never directly from a recent stat average.

Supported market families:
    * QB passing yards        (log-normal on attempts * ypa)
    * QB passing attempts     (Normal on attempts)
    * QB completions          (Binomial on attempts * comp_rate)
    * QB rushing yards        (log-normal on rush_att * rush_ypc)
    * RB rushing yards        (log-normal on carries * ypc)
    * RB carries              (Normal on carries)
    * RB receiving yards      (log-normal on targets * catch_rate * ypr)
    * RB receptions           (NegBin on targets * catch_rate)
    * WR/TE receiving yards   (log-normal on targets * catch_rate * ypt)
    * WR/TE receptions        (NegBin on targets * catch_rate)
    * WR/TE targets           (Normal on targets)
    * ATD (anytime TD)        (Bernoulli on P(TD ≥ 1))
                                Note: canonical ATD engine lives in
                                ``nfl_atd_engine`` — this simulator
                                provides a Challenger overlay only.

Correlation (§8) is preserved within a single call: QB attempts,
WR targets, and RB carries for a single team are all sampled from
the SAME game-script draw so a high-passing-volume script produces
correlated up-moves in QB yards + WR targets.  A separate call for
another player uses a different script draw — cross-call
correlation is the caller's responsibility (via shared seed +
context).
"""
from __future__ import annotations

import math
import random
from typing import Any, Optional

from services.platinum_nfl.football_core import (
    sample_lognormal, sample_poisson, sample_negative_binomial,
    quantile_summary, p_over, p_under,
    LEAGUE_QB_INT_RATE,
)
from services.platinum_nfl.opportunity import (
    QBOpportunity, RBOpportunity, WROpportunity,
    sample_qb_opportunity, sample_rb_opportunity, sample_wr_opportunity,
)


# ── Market classification ─────────────────────────────────────────
_QB_MARKETS = {
    "passing_yards": "qb_yards",
    "passing yards": "qb_yards",
    "player_pass_yds": "qb_yards",
    "passing_attempts": "qb_att",
    "passing attempts": "qb_att",
    "player_pass_attempts": "qb_att",
    "passing_completions": "qb_comp",
    "player_pass_completions": "qb_comp",
    "qb_rushing_yards": "qb_rush_yds",
    "passing_tds": "qb_tds",
    "player_pass_tds": "qb_tds",
}
_RB_MARKETS = {
    "rushing_yards":      "rb_yards",
    "rushing yards":      "rb_yards",
    "player_rush_yds":    "rb_yards",
    "carries":            "rb_carries",
    "rushing_attempts":   "rb_carries",
    "player_rush_attempts": "rb_carries",
    "receiving_yards_rb": "rb_rec_yards",
    "receptions_rb":      "rb_receptions",
}
_WR_MARKETS = {
    "receiving_yards":    "wr_yards",
    "receiving yards":    "wr_yards",
    "player_receiving_yds": "wr_yards",
    "receptions":         "wr_receptions",
    "player_receptions":  "wr_receptions",
    "targets":            "wr_targets",
    "receiving_tds":      "wr_tds",
}
_TD_MARKETS = {
    "anytime_td":         "atd",
    "player_anytime_td":  "atd",
}


def _resolve_market_family(pick: dict, position: str) -> Optional[str]:
    m = str(pick.get("market") or pick.get("market_key") or "").lower()
    for k, fam in _TD_MARKETS.items():
        if k in m:
            return fam
    if position == "QB":
        for k, fam in _QB_MARKETS.items():
            if k in m:
                return fam
    if position == "RB":
        for k, fam in _RB_MARKETS.items():
            if k in m:
                return fam
    if position in ("WR", "TE"):
        for k, fam in _WR_MARKETS.items():
            if k in m:
                return fam
    return None


# ═════════════════════════════════════════════════════════════════════
# Main dispatcher
# ═════════════════════════════════════════════════════════════════════
def simulate_player_market(
    pick: dict,
    *,
    opportunity,                        # QBOpportunity | RBOpportunity | WROpportunity
    team_plays: float,
    game_pass_rate: float,
    position: str,
    seed: random.Random,
    n_sims: int = 5000,
) -> dict:
    """Simulate a player-market pick through the opportunity chain.

    Returns the standard simulator-output dict.  On any failure
    returns the ``ran=False`` shape per §32 — never fakes a
    probability.
    """
    fam = _resolve_market_family(pick, position)
    if fam is None:
        return _failed("UNSUPPORTED_PLAYER_MARKET",
                       market_threshold=pick.get("line"))
    try:
        line = float(pick.get("line"))
    except (TypeError, ValueError):
        return _failed("MISSING_LINE", market_threshold=pick.get("line"))
    side = str(pick.get("side") or pick.get("pick_side") or "").strip().lower()

    samples: list[float] = _draw_samples(
        fam, opportunity=opportunity, team_plays=team_plays,
        game_pass_rate=game_pass_rate, seed=seed, n_sims=n_sims,
    )
    if not samples:
        return _failed("SIMULATOR_FAILED", market_threshold=line)

    if fam == "atd":
        # ATD samples are 0/1 Bernoullis - use mean as P(≥1 TD).
        p = sum(samples) / len(samples)
        return _summary(samples=samples, sim_probability=p,
                        market_threshold=line, market=fam, side=side)

    if side.startswith("over"):
        p = p_over(samples, line)
    elif side.startswith("under"):
        p = p_under(samples, line)
    else:
        # For markets without an Over/Under side (rare), we default
        # to Over.
        p = p_over(samples, line)
    return _summary(samples=samples, sim_probability=p,
                    market_threshold=line, market=fam, side=side)


def _draw_samples(fam: str, *, opportunity,
                  team_plays: float, game_pass_rate: float,
                  seed: random.Random, n_sims: int) -> list[float]:
    """Draw n_sims per-game samples for the given market family.
    Same opportunity object → same distribution.  All uncertainty
    is baked into the opportunity + efficiency sampling.
    """
    out: list[float] = []
    if fam.startswith("qb_"):
        if not isinstance(opportunity, QBOpportunity):
            return []
        for _ in range(n_sims):
            g = sample_qb_opportunity(
                opportunity, seed,
                game_pass_rate=game_pass_rate, team_plays=team_plays,
            )
            att = g["attempts"]
            ypa = g["ypa"]
            comp = g["comp_rate"]
            if fam == "qb_yards":
                y_mean = att * ypa
                y_std  = math.sqrt(att) * ypa * 0.55
                out.append(sample_lognormal(y_mean, y_std, seed))
            elif fam == "qb_att":
                out.append(float(att))
            elif fam == "qb_comp":
                # Completions = Binomial(att, comp_rate)
                comps = 0
                for _i in range(att):
                    if seed.random() < comp:
                        comps += 1
                out.append(float(comps))
            elif fam == "qb_rush_yds":
                rush_att = g["rush_att"]
                y_mean = rush_att * opportunity.rush_ypc_mean
                y_std  = math.sqrt(max(1, rush_att)) * opportunity.rush_ypc_std
                out.append(sample_lognormal(y_mean, y_std, seed))
            elif fam == "qb_tds":
                # TDs = NegBin with mean = attempts * TD_rate_per_att
                lam = att * 0.052     # league avg ~5.2% TDs per attempt
                out.append(float(sample_negative_binomial(lam, 0.35, seed)))
            elif fam == "atd":
                # QB rushing-TD prob per game (small).
                lam = 0.20            # ~1 rushing TD per 5 games league avg
                p_no_td = math.exp(-lam)
                out.append(1.0 if seed.random() < (1.0 - p_no_td) else 0.0)
    elif fam.startswith("rb_"):
        if not isinstance(opportunity, RBOpportunity):
            return []
        for _ in range(n_sims):
            g = sample_rb_opportunity(
                opportunity, seed,
                team_plays=team_plays, game_pass_rate=game_pass_rate,
            )
            carries = g["carries"]
            targets = g["targets"]
            if fam == "rb_yards":
                y_mean = carries * g["ypc"]
                y_std  = math.sqrt(max(1, carries)) * opportunity.ypc_std
                out.append(sample_lognormal(y_mean, y_std, seed))
            elif fam == "rb_carries":
                out.append(float(carries))
            elif fam == "rb_rec_yards":
                receptions = 0
                for _i in range(targets):
                    if seed.random() < g["catch_rate"]:
                        receptions += 1
                y_mean = receptions * g["ypr"]
                y_std  = math.sqrt(max(1, receptions)) * 3.5
                out.append(sample_lognormal(y_mean, y_std, seed))
            elif fam == "rb_receptions":
                out.append(float(sample_negative_binomial(
                    targets * g["catch_rate"], 0.35, seed)))
            elif fam == "atd":
                # RB TD prob per game via goal-line usage.
                gl = g.get("goal_line", 0.55)
                lam = 0.55 * gl + 0.15 * (carries / 20.0)
                p_no_td = math.exp(-lam)
                out.append(1.0 if seed.random() < (1.0 - p_no_td) else 0.0)
    elif fam.startswith("wr_") or fam == "atd":
        if not isinstance(opportunity, WROpportunity):
            return []
        for _ in range(n_sims):
            g = sample_wr_opportunity(
                opportunity, seed,
                team_plays=team_plays, game_pass_rate=game_pass_rate,
            )
            targets    = g["targets"]
            catch_rate = g["catch_rate"]
            ypt        = g["ypt"]
            if fam == "wr_yards":
                receptions = 0
                for _i in range(targets):
                    if seed.random() < catch_rate:
                        receptions += 1
                # Yards per catch (log-normal around ypr = ypt / catch_rate).
                ypr = ypt / max(0.35, catch_rate)
                y_mean = receptions * ypr
                y_std  = math.sqrt(max(1, receptions)) * 3.2
                out.append(sample_lognormal(y_mean, y_std, seed))
            elif fam == "wr_receptions":
                out.append(float(sample_negative_binomial(
                    targets * catch_rate, 0.28, seed)))
            elif fam == "wr_targets":
                out.append(float(targets))
            elif fam == "wr_tds":
                lam = 0.6 * g.get("red_zone_share", 0.15) + \
                      0.05 * (targets / 6.0)
                p_no_td = math.exp(-lam)
                out.append(1.0 if seed.random() < (1.0 - p_no_td) else 0.0)
            elif fam == "atd":
                lam = 0.6 * g.get("red_zone_share", 0.15) + \
                      0.05 * (targets / 6.0)
                p_no_td = math.exp(-lam)
                out.append(1.0 if seed.random() < (1.0 - p_no_td) else 0.0)
    return out


def _summary(*, samples: list[float], sim_probability: float,
             market_threshold: float, market: str, side: str) -> dict:
    q = quantile_summary(samples)
    return {
        "ran":                True,
        "market":             market,
        "side":               side,
        "market_threshold":   market_threshold,
        "sim_probability":    float(sim_probability),
        "distribution_mean":  q.mean,
        "distribution_median": q.median,
        "q10": q.q10, "q25": q.q25, "q75": q.q75, "q90": q.q90,
        "variance": q.variance, "std": q.std,
        "simulation_count":   q.n,
    }


def _failed(reason: str, **extra) -> dict:
    return {
        "ran":              False,
        "reason":           reason,
        "sim_probability":  None,
        **extra,
    }


__all__ = ["simulate_player_market"]
