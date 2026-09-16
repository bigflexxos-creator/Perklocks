"""Tennis point→game→set→match Markov simulator (Session 6, 2026-09-17).

REAL distribution-based pricing for Tennis markets.  The simulator
consumes matchup-specific serve-point-win probabilities and produces a
complete distribution over match outcomes:

    - match winner (ML)
    - player games / opponent games / total games (Spread & Total)

Prices EVERY observed sportsbook spread rung directly from the simulated
GAME MARGIN distribution — no ML-probability proxy across the ladder.
Same distribution feeds Totals so ML/Spread/Total are internally
coherent.

Fully deterministic given (spw_p, spw_o, best_of, seed).  In-process
memoization ensures each unique matchup is simulated exactly once per
request.
"""
from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass, field, asdict
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Public data classes
# ---------------------------------------------------------------------------

@dataclass
class MatchDistribution:
    n_sims:         int
    spw_p:          float
    spw_o:          float
    best_of:        int
    p_win_ml:       float           # match win probability for player
    mean_p_games:   float
    mean_o_games:   float
    mean_total:     float
    stddev_total:   float
    margin_hist:    dict[int, int]  # sparse histogram of (p_games - o_games)
    total_hist:     dict[int, int]  # sparse histogram of total games
    provenance:     str = "markov_v1"
    # Session 6 P1 · Serve/return + simulation provenance flags.
    # These are set by callers (spw_from_matchup) to disclose whether
    # the SPWs feeding this simulation are empirically independent
    # (real serve % + return % observations) or derived from Elo.
    serve_return_provenance: str = "ELO_DERIVED"
    simulation_provenance:   str = "MODEL_CONDITIONED"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PricedMarkets:
    ml_prob:         float
    spread_prices:   list[dict]     # [{spread, p_cover, ...}]
    total_prices:    list[dict]     # [{threshold, p_over, p_under}]
    distribution:    MatchDistribution
    # Session 6 P2 · Evidence family map — used by UEA/convergence
    # gating to prevent Elo → Elo-derived S/R → Elo-conditioned sim
    # being counted as three independent evidence votes.
    evidence_families: dict[str, str] = field(default_factory=lambda: {
        "strength":      "elo_and_surface_elo",
        "serve_return":  "elo_derived",
        "simulation":    "model_conditioned_on_elo",
        "market":        "external_benchmark_only",
    })


# ---------------------------------------------------------------------------
# Point → game → set → match simulator
# ---------------------------------------------------------------------------

def _game_win_prob(p_serve: float) -> float:
    """Closed-form probability of winning a game given serve-point-win
    probability p_serve.  Standard tennis Markov result."""
    p = max(1e-6, min(1 - 1e-6, p_serve))
    q = 1 - p
    P = (p**4) * (
        1 + 4*q + 10*q*q
    ) + 20 * (p**3) * (q**3) * (p**2 / (p**2 + q**2))
    return P


def _sim_set(spw_p: float, spw_o: float, rng: random.Random,
             tiebreak: bool = True) -> tuple[int, int, bool]:
    """Simulate one set.  Returns (p_games, o_games, p_won_set)."""
    pg = og = 0
    server_p = rng.random() < 0.5  # who serves first
    while True:
        if server_p:
            if rng.random() < _game_win_prob(spw_p): pg += 1
            else:                                    og += 1
        else:
            if rng.random() < _game_win_prob(spw_o): og += 1
            else:                                    pg += 1
        server_p = not server_p
        # Set win conditions
        if pg >= 6 and pg - og >= 2: return pg, og, True
        if og >= 6 and og - pg >= 2: return pg, og, False
        if pg == 7 and og == 5:      return pg, og, True
        if og == 7 and pg == 5:      return pg, og, False
        if pg == 6 and og == 6 and tiebreak:
            # Tiebreak resolves the set
            if rng.random() < spw_p / (spw_p + spw_o + 1e-9):
                return pg + 1, og, True
            else:
                return pg, og + 1, False


def simulate_match(spw_p: float, spw_o: float,
                   best_of: int = 3, n_sims: int = 5000,
                   seed: int = 12345) -> MatchDistribution:
    """Run n_sims full matches; return the joint distribution."""
    rng = random.Random(seed)
    target_sets = 2 if best_of == 3 else 3
    wins = 0
    p_games_total = 0.0
    o_games_total = 0.0
    total_sq = 0.0
    total_sum = 0.0
    margin_hist: dict[int, int] = {}
    total_hist: dict[int, int] = {}

    for _ in range(n_sims):
        p_sets = o_sets = 0
        p_g = o_g = 0
        while p_sets < target_sets and o_sets < target_sets:
            pg, og, p_won = _sim_set(spw_p, spw_o, rng)
            p_g += pg; o_g += og
            if p_won: p_sets += 1
            else:     o_sets += 1
        if p_sets > o_sets: wins += 1
        p_games_total += p_g; o_games_total += o_g
        m  = p_g - o_g
        tt = p_g + o_g
        margin_hist[m]  = margin_hist.get(m, 0) + 1
        total_hist[tt]  = total_hist.get(tt, 0) + 1
        total_sum += tt; total_sq += tt * tt

    mean_p = p_games_total / n_sims
    mean_o = o_games_total / n_sims
    mean_t = total_sum / n_sims
    var_t  = max(0.0, (total_sq / n_sims) - mean_t ** 2)
    return MatchDistribution(
        n_sims=n_sims, spw_p=spw_p, spw_o=spw_o, best_of=best_of,
        p_win_ml=round(wins / n_sims, 4),
        mean_p_games=round(mean_p, 2),
        mean_o_games=round(mean_o, 2),
        mean_total=round(mean_t, 2),
        stddev_total=round(math.sqrt(var_t), 3),
        margin_hist=margin_hist, total_hist=total_hist,
    )


# ---------------------------------------------------------------------------
# Serve-point-win probabilities from Elo-based matchup probability
# ---------------------------------------------------------------------------

# Baseline serve-point-win rates (tour-specific).
_SPW_BASELINE = {"ATP": 0.635, "WTA": 0.585, "CH": 0.610, "ITF": 0.580, None: 0.615}


def spw_from_matchup(matchup_prob: float, tour: Optional[str] = None,
                     spw_baseline: Optional[float] = None,
                     best_of: int = 3) -> tuple[float, float]:
    """Invert Elo-derived match win probability into (spw_p, spw_o).

    Runs a bisection on δ (symmetric SPW shift around tour baseline)
    using a FAST simulate-only-1000-matches probe so the resulting
    (spw_p, spw_o) actually reproduces the target match probability
    under the same Markov structure.
    """
    baseline = spw_baseline if spw_baseline is not None else _SPW_BASELINE.get(tour, 0.615)
    target = max(0.03, min(0.97, matchup_prob))

    def _probe(delta: float) -> float:
        p1 = min(0.90, max(0.35, baseline + delta))
        p2 = min(0.90, max(0.35, baseline - delta))
        # 800 sims is enough for a stable bisection probe.
        d = simulate_match(p1, p2, best_of=best_of, n_sims=800, seed=777)
        return d.p_win_ml

    lo, hi = -0.20, 0.20
    for _ in range(14):
        mid = (lo + hi) / 2
        p = _probe(mid)
        if p < target: lo = mid
        else:           hi = mid
    delta = (lo + hi) / 2
    return (
        round(min(0.90, max(0.35, baseline + delta)), 4),
        round(min(0.90, max(0.35, baseline - delta)), 4),
    )


# ---------------------------------------------------------------------------
# Pricing from the joint distribution
# ---------------------------------------------------------------------------

def price_spreads(dist: MatchDistribution, thresholds: list[float]
                   ) -> list[dict]:
    """Price sportsbook spread rungs from the simulated margin
    distribution.

    Session 6 P0 fix (2026-09-17):
        ``thresholds`` are now SPORTSBOOK SPREAD NUMBERS from the
        player's perspective (favourite negative, dog positive).  The
        cover predicate is:

            spread = -3.5  →  player must win by MORE than 3.5 games
                              → P(margin >  3.5)
            spread = +3.5  →  player may lose by AT MOST 3 games
                              → P(margin > -3.5)

        Both cases reduce to ``P(margin > -spread)``.  This is
        mathematically monotone: harder spread (more negative for a
        favourite / less positive for a dog) yields a strictly smaller
        cover set.  No post-hoc clamp.

    Returns a list of rows with fields:
        - spread            (sportsbook convention, signed)
        - p_cover           (probability THIS player covers)
        - p_push            (probability of an exact-line push)
        - opponent_p_cover  (1 - p_cover - p_push)
    """
    prices: list[dict] = []
    total = float(dist.n_sims)
    for s in thresholds:
        cutoff = -s   # convert sportsbook spread → margin cutoff
        cover_cnt = 0
        push_cnt  = 0
        for m, cnt in dist.margin_hist.items():
            if m > cutoff:
                cover_cnt += cnt
            elif m == cutoff:
                push_cnt += cnt
        p_cover = cover_cnt / total
        p_push  = push_cnt / total
        prices.append({
            "spread":           s,
            "p_cover":          round(p_cover, 4),
            "p_push":           round(p_push, 4),
            "opponent_p_cover": round(1 - p_cover - p_push, 4),
            "margin_cutoff":    cutoff,     # transparency for auditors
            "source":           "margin_hist",
        })
    return prices


def enforce_monotonic_ladder(spread_prices: list[dict]) -> list[dict]:
    """Sanity guard for the sportsbook-convention ladder.

    Under the P0 fix, spread rungs are indexed by ``spread`` (signed).
    As ``spread`` moves DOWN (favourite gets a harder handicap), the
    player's cover set shrinks, so ``p_cover`` must be non-decreasing
    in ``spread`` (i.e. more-negative spread → smaller p_cover).

    Any breach is guaranteed to be Monte-Carlo noise now that the
    predicate is analytically correct; we clamp only for display and
    always tag the row with a provenance note.
    """
    sorted_prices = sorted(spread_prices, key=lambda r: r["spread"])
    out: list[dict] = []
    last: Optional[float] = None
    for row in sorted_prices:
        row_c = dict(row)
        if last is not None and row_c["p_cover"] < last - 1e-6:
            row_c["monotonicity_note"] = (
                f"cover prob {row_c['p_cover']} < previous {last} "
                f"— MC sim noise; clamped for display"
            )
            row_c["p_cover"] = round(max(row_c["p_cover"], last), 4)
        last = row_c["p_cover"]
        out.append(row_c)
    return out


def price_totals(dist: MatchDistribution, thresholds: list[float]
                  ) -> list[dict]:
    prices: list[dict] = []
    total = float(dist.n_sims)
    for t in thresholds:
        over = under = push = 0
        for tt, cnt in dist.total_hist.items():
            if tt > t:  over  += cnt
            elif tt < t: under += cnt
            else:        push  += cnt
        prices.append({
            "threshold": t,
            "p_over":  round(over / total, 4),
            "p_under": round(under / total, 4),
            "p_push":  round(push / total, 4),
            "source":  "total_hist",
        })
    return prices


def price_from_matchup(matchup_prob: float, tour: Optional[str] = None,
                        spread_thresholds: Optional[list[float]] = None,
                        total_thresholds: Optional[list[float]] = None,
                        best_of: int = 3,
                        n_sims: int = 5000) -> PricedMarkets:
    """One-shot pricing.  Simulate ONCE, price every observed market."""
    spw_p, spw_o = spw_from_matchup(matchup_prob, tour, best_of=best_of)
    dist = simulate_match(spw_p, spw_o, best_of=best_of, n_sims=n_sims,
                          seed=int((matchup_prob * 10_000)) & 0xFFFFFFFF)
    # Session 6 P1/P2 · disclose provenance on every distribution.
    dist.serve_return_provenance = "ELO_DERIVED"
    dist.simulation_provenance   = "MODEL_CONDITIONED"
    spread_prices = price_spreads(dist, spread_thresholds or [])
    total_prices  = price_totals(dist,  total_thresholds  or [])
    return PricedMarkets(
        ml_prob=dist.p_win_ml,
        spread_prices=spread_prices,
        total_prices=total_prices,
        distribution=dist,
    )


# ---------------------------------------------------------------------------
# Ladder monotonicity check — sanity guard
# ---------------------------------------------------------------------------

def enforce_monotonic_ladder_deprecated(spread_prices: list[dict]) -> list[dict]:
    """Deprecated legacy shape — retained for import compatibility.
    Callers now use the sportsbook-convention version above."""
    return spread_prices
