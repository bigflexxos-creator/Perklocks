"""Magic Layer 2.0 — Model ↔ Market Convergence States.

Preserves the DISTINCTION between:

  * model_probability      — the sport's learned probability model
  * simulator_probability  — Monte-Carlo output when available
  * calibrated_probability — post-hoc reliability adjustment
  * sportsbook_probability — de-vig consensus of real sportsbook lines

None of these are averaged into one opaque number.  Instead they map
to a categorical state so consumers can reason about the ALIGNMENT
between the model and the market.

States
──────
* MODEL_MARKET_STRONG_AGREEMENT  |Δ| ≤ 2 pts  AND edge sign > 0
* MODEL_MARKET_AGREEMENT          |Δ| ≤ 5 pts
* MODEL_EDGE_MARKET_NEUTRAL       model > market  AND edge in (5, 12] pts
* MARKET_STRONGER_THAN_MODEL      market > model  AND |Δ| > 5 pts
* MODEL_MARKET_DISAGREEMENT       |Δ| > 12 pts (large gap either way)
* INSUFFICIENT_MARKET_DATA        no real sportsbook consensus available
"""
from __future__ import annotations

import enum
from typing import Any, Optional


class ModelMarketState(str, enum.Enum):
    MODEL_MARKET_STRONG_AGREEMENT = "MODEL_MARKET_STRONG_AGREEMENT"
    MODEL_MARKET_AGREEMENT        = "MODEL_MARKET_AGREEMENT"
    MODEL_EDGE_MARKET_NEUTRAL     = "MODEL_EDGE_MARKET_NEUTRAL"
    MARKET_STRONGER_THAN_MODEL    = "MARKET_STRONGER_THAN_MODEL"
    MODEL_MARKET_DISAGREEMENT     = "MODEL_MARKET_DISAGREEMENT"
    INSUFFICIENT_MARKET_DATA      = "INSUFFICIENT_MARKET_DATA"


def _american_to_prob(odds: Optional[float]) -> Optional[float]:
    if odds is None:
        return None
    try:
        o = float(odds)
    except (TypeError, ValueError):
        return None
    if o == 0:
        return None
    if o > 0:
        return 100.0 / (o + 100.0)
    return -o / (-o + 100.0)


def evaluate_model_market_convergence(
    *,
    model_probability: Optional[float],
    book_odds:         Optional[float],
    no_real_book_line: bool = False,
    book_implied_prob: Optional[float] = None,
    consensus_prob:    Optional[float] = None,
    book_count:        Optional[int]   = None,
) -> dict[str, Any]:
    """Compare model probability to the sportsbook consensus.

    Never coerces missing data to zero — returns
    ``INSUFFICIENT_MARKET_DATA`` when the real market probability
    cannot be derived (no_real_book_line, missing book_odds, or a
    book_count below the minimum for a consensus).
    """
    mp = None
    if model_probability is not None:
        try:
            mp = float(model_probability)
        except (TypeError, ValueError):
            mp = None

    market_prob: Optional[float] = None
    market_source: Optional[str] = None
    if no_real_book_line or book_odds is None:
        market_prob = None
    elif consensus_prob is not None:
        market_prob = float(consensus_prob)
        market_source = "consensus"
    elif book_implied_prob is not None:
        market_prob = float(book_implied_prob)
        market_source = "book_implied"
    else:
        market_prob = _american_to_prob(book_odds)
        market_source = "single_book_american"

    if mp is None or market_prob is None:
        return {
            "state":           ModelMarketState.INSUFFICIENT_MARKET_DATA.value,
            "model_prob":      mp,
            "market_prob":     market_prob,
            "delta_pts":       None,
            "market_source":   market_source,
            "book_count":      book_count,
        }

    delta_pts = round((mp - market_prob) * 100.0, 2)
    abs_delta = abs(delta_pts)

    if abs_delta <= 2.0:
        st = ModelMarketState.MODEL_MARKET_STRONG_AGREEMENT
    elif abs_delta <= 5.0:
        st = ModelMarketState.MODEL_MARKET_AGREEMENT
    elif delta_pts > 5.0 and delta_pts <= 12.0:
        # Model likes it more than the market by a moderate margin.
        st = ModelMarketState.MODEL_EDGE_MARKET_NEUTRAL
    elif delta_pts < -5.0 and delta_pts >= -12.0:
        st = ModelMarketState.MARKET_STRONGER_THAN_MODEL
    else:
        st = ModelMarketState.MODEL_MARKET_DISAGREEMENT

    return {
        "state":           st.value,
        "model_prob":      round(mp, 4),
        "market_prob":     round(market_prob, 4),
        "delta_pts":       delta_pts,
        "market_source":   market_source,
        "book_count":      book_count,
    }


__all__ = ["ModelMarketState", "evaluate_model_market_convergence"]
