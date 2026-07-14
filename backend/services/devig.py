"""Devig utilities — cross-sport no-vig fair odds + sharp/steam helpers.

Phase 0.1 of the data-gap roadmap. This is the single highest-ROI addition
to the model because it fixes the "chalk hemorrhage" pattern: books charge
a 4-5% vig on both sides of a market, so the implied probability sum
across sides is always > 100%. Grading picks off raw `book_odds` treats
that vig as real edge against the model and pushes the algorithm toward
chalk that closes at even worse prices → our observed 71.6% win / -4.7u
ROI band.

De-vigging normalises both sides so p(A) + p(B) = 1.0 (or p+p+p = 1 for
1X2), giving the "fair" market probability. Comparing our model against
FAIR odds is what real bettors use; comparing against BOOK odds
overstates edge on chalk and understates it on dogs.

Public API:
    no_vig_two_way(odds_a, odds_b)          -> (fair_a, fair_b, prob_a)
    no_vig_three_way(odds_h, odds_d, odds_a)-> (fair_h, fair_d, fair_a,
                                                 (p_h, p_d, p_a))
    fair_from_prob(prob)                     -> American odds
    american_to_prob(odds)                   -> decimal probability (0-1)
    devig_pick(pick)                         -> attaches:
        no_vig_book_odds   (American int)
        no_vig_implied_pct (0-100 float)
        book_hold_pct      (raw vig, e.g. 4.2)
        no_vig_source      (label — 'two_way' | 'three_way' | 'proportional')

All functions are pure — no DB, no HTTP, no logging side-effects. That
makes them safe to plug into pick_enrichment / probability_engine /
value_signal without worrying about IO ordering.
"""
from __future__ import annotations

from typing import Optional


def american_to_prob(odds: float | int | None) -> Optional[float]:
    """American odds → decimal probability in [0, 1]. Returns None on 0/None."""
    if odds is None or odds == 0:
        return None
    a = float(odds)
    if a > 0:
        return 100.0 / (a + 100.0)
    return abs(a) / (abs(a) + 100.0)


def fair_from_prob(prob: float | None) -> Optional[int]:
    """Fair prob → American odds (int, rounded). Returns None for out-of-range."""
    if prob is None or prob <= 0 or prob >= 1:
        return None
    if prob >= 0.5:
        return int(round(-100.0 * prob / (1.0 - prob)))
    return int(round(100.0 * (1.0 - prob) / prob))


def _prob_to_american_pct(prob: float) -> float:
    """Same as fair_from_prob but returns the implied % instead of American int."""
    return round(prob * 100.0, 2)


def no_vig_two_way(
    odds_a: float | int | None,
    odds_b: float | int | None,
) -> Optional[tuple[Optional[int], Optional[int], float, float, float]]:
    """De-vig a two-way market (h2h moneyline, totals, spread).

    Returns (fair_a_american, fair_b_american, fair_prob_a, fair_prob_b, hold_pct)
    or None when either side is unusable.

    hold_pct is the raw book vig, expressed as a percentage of extra implied
    probability above 100%. e.g. a market with p_a=0.55, p_b=0.50 has 5% hold.
    """
    p_a = american_to_prob(odds_a)
    p_b = american_to_prob(odds_b)
    if p_a is None or p_b is None:
        return None
    total = p_a + p_b
    if total <= 0.0:
        return None
    hold_pct = round((total - 1.0) * 100.0, 3)
    fair_a = p_a / total
    fair_b = p_b / total
    return (fair_from_prob(fair_a), fair_from_prob(fair_b),
            round(fair_a, 4), round(fair_b, 4), hold_pct)


def no_vig_three_way(
    odds_home: float | int | None,
    odds_draw: float | int | None,
    odds_away: float | int | None,
) -> Optional[tuple[Optional[int], Optional[int], Optional[int],
                    float, float, float, float]]:
    """De-vig a three-way market (soccer 1X2 moneyline).

    Returns (fair_h, fair_d, fair_a, prob_h, prob_d, prob_a, hold_pct)
    or None when insufficient inputs. Uses the multiplicative normalisation
    method (Shin's method would be marginally better but requires solving a
    quadratic; the proportional method is 99% as accurate for our uses).
    """
    p_h = american_to_prob(odds_home)
    p_d = american_to_prob(odds_draw)
    p_a = american_to_prob(odds_away)
    if p_h is None or p_d is None or p_a is None:
        return None
    total = p_h + p_d + p_a
    if total <= 0.0:
        return None
    hold_pct = round((total - 1.0) * 100.0, 3)
    fh = p_h / total
    fd = p_d / total
    fa = p_a / total
    return (fair_from_prob(fh), fair_from_prob(fd), fair_from_prob(fa),
            round(fh, 4), round(fd, 4), round(fa, 4), hold_pct)


def devig_pick(pick: dict) -> dict:
    """Compute no-vig fair implied % for a pick using its own `alt_line_data`
    or `market_lines` payload (if the ingest layer stored the other side of
    the market alongside book_odds).

    Attaches (all optional — nothing overwritten if inputs missing):
        no_vig_book_odds   — American int, our side's fair odds
        no_vig_implied_pct — 0-100 float, our side's fair implied %
        book_hold_pct      — market vig
        no_vig_source      — 'two_way' | 'three_way'

    Returns the (mutated) pick for chaining.
    """
    book = pick.get("book_odds")
    if not book:
        return pick

    # 1) Try companion-side odds if the ingest layer captured them (e.g.
    #    totals: over_odds + under_odds; h2h: home_odds + away_odds; some
    #    markets stash it under alt_line_data.opposite_side or
    #    market_lines[<other_side>]).
    counterpart = (
        pick.get("counterpart_odds")
        or pick.get("opposite_odds")
        or ((pick.get("alt_line_data") or {}).get("opposite_odds"))
        or ((pick.get("market_lines") or {}).get("counterpart"))
    )

    # 2) Three-way (soccer moneyline). Look for `three_way_odds` payload:
    #    {"home": +150, "draw": +240, "away": +190}
    three = pick.get("three_way_odds") or None
    if isinstance(three, dict) and all(three.get(k) for k in ("home", "draw", "away")):
        result = no_vig_three_way(three["home"], three["draw"], three["away"])
        if result:
            fh, fd, fa, ph, pd, pa, hold = result
            # Determine which side the pick is on.
            sel = (pick.get("selection") or "").lower()
            event = (pick.get("event") or "").lower()
            side = None
            if "draw" in sel:
                side = ("draw", fd, pd)
            elif "@" in event:
                away_name, home_name = [p.strip() for p in event.split("@", 1)]
                if home_name.lower() in sel:
                    side = ("home", fh, ph)
                elif away_name.lower() in sel:
                    side = ("away", fa, pa)
            if side:
                pick["no_vig_book_odds"] = side[1]
                pick["no_vig_implied_pct"] = round(side[2] * 100.0, 2)
                pick["book_hold_pct"] = hold
                pick["no_vig_source"] = "three_way"
                return pick

    # 3) Two-way de-vig using counterpart odds
    if counterpart:
        result = no_vig_two_way(book, counterpart)
        if result:
            fa_am, fb_am, prob_a, prob_b, hold = result
            pick["no_vig_book_odds"] = fa_am
            pick["no_vig_implied_pct"] = round(prob_a * 100.0, 2)
            pick["book_hold_pct"] = hold
            pick["no_vig_source"] = "two_way"
            return pick

    # 4) No counterpart available → fallback to proportional single-sided
    #    de-vig using the sport's average market hold. This is a rough
    #    approximation but is still better than treating raw book_odds as
    #    fair. Standard holds by sport (industry averages, %):
    default_hold = {
        "MLB": 4.0,      # -110/-110 baseline
        "NFL": 4.5,
        "NBA": 4.5,
        "Soccer": 6.0,   # higher for 1X2
        "Tennis": 4.0,
        "UFC": 5.0,
    }
    sport_hold = default_hold.get(pick.get("sport"), 4.5)
    p_book = american_to_prob(book)
    if p_book is None:
        return pick
    # Assume the vig is split evenly across both sides — remove half.
    fair = p_book / (1.0 + sport_hold / 200.0)
    pick["no_vig_book_odds"] = fair_from_prob(fair)
    pick["no_vig_implied_pct"] = round(fair * 100.0, 2)
    pick["book_hold_pct"] = sport_hold
    pick["no_vig_source"] = "proportional_sport_default"
    return pick


def edge_vs_no_vig(model_prob_pct: float | None,
                   no_vig_pct: float | None) -> Optional[float]:
    """Compute edge using no-vig fair implied instead of raw book implied.
    Positive number = model believes higher probability than fair market."""
    if model_prob_pct is None or no_vig_pct is None:
        return None
    if model_prob_pct <= 0 or no_vig_pct <= 0:
        return None
    return round(model_prob_pct - no_vig_pct, 2)


def steam_detected(book_odds: float | int | None,
                   sharp_odds: float | int | None,
                   threshold_pct: float = 2.5) -> bool:
    """Return True when the raw book_odds' implied % is >= threshold_pct
    lower than the sharp-book (Pinnacle) implied %.

    In practice: if your book has our side at -150 (60% imp) but Pinnacle
    has it at -180 (64.3% imp), that's a 4.3-pp steam. Threshold defaults
    to 2.5pp which filters out normal market noise while catching real
    steam moves.
    """
    p_book = american_to_prob(book_odds)
    p_sharp = american_to_prob(sharp_odds)
    if p_book is None or p_sharp is None:
        return False
    return (p_sharp - p_book) * 100.0 >= threshold_pct


__all__ = [
    "american_to_prob",
    "fair_from_prob",
    "no_vig_two_way",
    "no_vig_three_way",
    "devig_pick",
    "edge_vs_no_vig",
    "steam_detected",
]
