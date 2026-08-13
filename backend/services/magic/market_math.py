"""MAGIC 3F — Market intelligence primitives.

Pure mathematical utilities with NO DB dependency:

  * :func:`american_to_decimal`, :func:`decimal_to_american`
  * :func:`implied_probability` (raw, per-side)
  * :func:`two_way_devig`, :func:`multi_way_devig` (proportional)
  * :func:`consensus_devig`  — median + book-count aware
  * :func:`price_delta`, :func:`line_delta`

All identity, staging, and DB reads live in
:mod:`services.magic.market_snapshot_store`.
"""
from __future__ import annotations

from statistics import median
from typing import Iterable, Optional


# ═══════════════════════════════════════════════════════════════════
# Odds conversion (American ↔ Decimal ↔ Probability)
# ═══════════════════════════════════════════════════════════════════
def american_to_decimal(american: Optional[float]) -> Optional[float]:
    """Return decimal price for American odds.  Handles ±100 correctly.

    -110 → 1.909090..., +200 → 3.0, 100 → 2.0, -100 → 2.0.
    None / 0 / non-numeric → None (never fabricate).
    """
    if american is None:
        return None
    try:
        a = float(american)
    except (TypeError, ValueError):
        return None
    if a == 0:
        return None
    if abs(a) < 100:
        return None
    if a >= 100:
        return 1.0 + a / 100.0
    return 1.0 + 100.0 / abs(a)


def decimal_to_american(decimal: Optional[float]) -> Optional[int]:
    if decimal is None:
        return None
    try:
        d = float(decimal)
    except (TypeError, ValueError):
        return None
    if d <= 1.0:
        return None
    if d >= 2.0:
        return int(round((d - 1.0) * 100.0))
    return int(round(-100.0 / (d - 1.0)))


def implied_probability(american: Optional[float]) -> Optional[float]:
    """RAW implied probability from American odds (with vig).
    NEVER treat this as a de-vig or model probability."""
    dec = american_to_decimal(american)
    if dec is None or dec <= 1.0:
        return None
    return 1.0 / dec


# ═══════════════════════════════════════════════════════════════════
# De-vig (proportional / Shin fallback disabled by default)
# ═══════════════════════════════════════════════════════════════════
def two_way_devig(
    american_side_a: Optional[float],
    american_side_b: Optional[float],
) -> Optional[tuple[float, float]]:
    """Proportional de-vig for a 2-way market.  Returns
    ``(p_a_devig, p_b_devig)`` summing to 1.0.  Any side missing → None.
    """
    p_a = implied_probability(american_side_a)
    p_b = implied_probability(american_side_b)
    if p_a is None or p_b is None:
        return None
    total = p_a + p_b
    if total <= 0:
        return None
    return (p_a / total, p_b / total)


def multi_way_devig(americans: list[Optional[float]]) -> Optional[list[float]]:
    """Proportional de-vig for N-way markets."""
    ps = [implied_probability(a) for a in americans]
    if any(p is None for p in ps):
        return None
    total = sum(ps)  # type: ignore[arg-type]
    if total <= 0:
        return None
    return [p / total for p in ps]  # type: ignore[union-attr]


# ═══════════════════════════════════════════════════════════════════
# Consensus (median across books)
# ═══════════════════════════════════════════════════════════════════
def consensus_devig(
    two_way_snapshots: Iterable[dict],
) -> Optional[dict]:
    """Aggregate a set of book snapshots for the SAME exact market
    into a de-vig consensus.

    Each snapshot dict must carry:
        american_side, opposing_american (same book, same event, same market,
        same line, same side, same timestamp window).

    Returns:
        {
          book_count, median_side_prob (raw),
          median_side_prob_devig, best_side_price_american,
          worst_side_price_american, side_price_dispersion,
          book_ids
        }
        or None when < 1 usable snapshot.
    """
    devig_ps: list[float] = []
    raw_ps: list[float] = []
    prices: list[float] = []
    books: list[str] = []
    for s in two_way_snapshots:
        a = s.get("american_side")
        b = s.get("opposing_american")
        if a is None:
            continue
        raw = implied_probability(a)
        if raw is not None:
            raw_ps.append(raw)
            try:
                prices.append(float(a))
            except (TypeError, ValueError):
                pass
            books.append(str(s.get("book") or ""))
            dv = two_way_devig(a, b)
            if dv is not None:
                devig_ps.append(dv[0])
    if not raw_ps:
        return None
    out: dict = {
        "book_count":               len(raw_ps),
        "median_side_prob_raw":     median(raw_ps),
        "best_side_price_american":  max(prices) if prices else None,
        "worst_side_price_american": min(prices) if prices else None,
        "side_price_dispersion":    (max(prices) - min(prices))
                                     if len(prices) >= 2 else 0.0,
        "book_ids":                 sorted({b for b in books if b}),
    }
    out["median_side_prob_devig"] = (median(devig_ps) if devig_ps
                                       else None)
    out["devig_book_count"] = len(devig_ps)
    return out


# ═══════════════════════════════════════════════════════════════════
# Movement primitives
# ═══════════════════════════════════════════════════════════════════
def line_delta(open_line: Optional[float],
                current_line: Optional[float]) -> Optional[float]:
    if open_line is None or current_line is None:
        return None
    try:
        return float(current_line) - float(open_line)
    except (TypeError, ValueError):
        return None


def price_delta(open_american: Optional[float],
                 current_american: Optional[float]) -> Optional[float]:
    """Return ``current_probability - open_probability`` (probability
    space so magnitudes are directly comparable across favorites/dogs)."""
    p_open = implied_probability(open_american)
    p_curr = implied_probability(current_american)
    if p_open is None or p_curr is None:
        return None
    return p_curr - p_open


__all__ = [
    "american_to_decimal", "decimal_to_american", "implied_probability",
    "two_way_devig", "multi_way_devig", "consensus_devig",
    "line_delta", "price_delta",
]
