"""Block 50 — Universal Prop Regression Tests (10 universal invariants).

These tests exercise the SHARED observed-quote lineage path (which
multiple sports migrate onto) WITHOUT touching any production
producer's existing behavior.  Every assertion is pure Python; no DB
or network required.
"""
from __future__ import annotations

import pytest

from services.observed_quote import (
    ObservedQuote, validate_observed_quote, select_authoritative_book,
    consensus_median_price, PREFERRED_BOOK_ORDER,
)
from services.quote_lineage_guard import harden_lineage


def _q(book: str, price: int, *, line=1.5, side="over",
        source="the_odds_api", observed_at="2026-06-20T18:00:00Z",
        player="pid1") -> ObservedQuote:
    return ObservedQuote(
        sport="mlb", event_id="evt1", provider_event_id="prov_evt1",
        provider_market_key="batter_hits", market_family="hitter_hits",
        line=line, side=side, bookmaker=book, price=price,
        odds_source=source, observed_at=observed_at,
        player_id=player, player_name="Player One",
    )


# 1. observed quote retains bookmaker lineage
def test_01_observed_quote_retains_bookmaker_lineage():
    ok, _ = validate_observed_quote(_q("fanduel", -140))
    assert ok


# 2. consensus retains original observations (list identity preserved)
def test_02_consensus_preserves_original_observations():
    quotes = [_q("fanduel", -140), _q("draftkings", -135), _q("betmgm", -150)]
    m = consensus_median_price(quotes)
    assert m == -140
    # Original quotes are unchanged — the function returns a scalar.
    assert all(isinstance(q, ObservedQuote) for q in quotes)


# 3. exact quote selected for publication
def test_03_exact_quote_selected_for_publication():
    quotes = [_q("betmgm", -150), _q("fanduel", -140), _q("draftkings", -135)]
    best = select_authoritative_book(quotes)
    # fanduel is top of PREFERRED_BOOK_ORDER
    assert best is not None
    assert best["bookmaker"] == "fanduel"
    assert best["price"] == -140


# 4. exact line preserved
def test_04_exact_line_preserved():
    quotes = [_q("fanduel", -140, line=1.5)]
    best = select_authoritative_book(quotes)
    assert best["line"] == 1.5


# 5. exact side preserved
def test_05_exact_side_preserved():
    quotes = [_q("fanduel", -140, side="over")]
    best = select_authoritative_book(quotes)
    assert best["side"] == "over"


# 6. actual bookmaker preserved
def test_06_actual_bookmaker_preserved():
    quotes = [_q("fanduel", -140)]
    hard = harden_lineage(quotes, market_family="hitter_hits",
                           event_id="evt1", sport="mlb")
    assert hard["bookmaker"] == "fanduel"
    assert hard["no_real_book_line"] is False


# 7. trusted odds_source preserved
def test_07_trusted_odds_source_preserved():
    quotes = [_q("fanduel", -140, source="the_odds_api")]
    hard = harden_lineage(quotes, market_family="hitter_hits",
                           event_id="evt1", sport="mlb")
    assert hard["odds_source"] == "the_odds_api"


# 8. median cannot masquerade as observed book
def test_08_median_cannot_masquerade_as_observed_book():
    bogus = _q("median", -140, source="provider_median")
    ok, reason = validate_observed_quote(bogus)
    assert ok is False
    assert reason in ("SYNTHETIC_BOOK_ODDS", "untrusted_odds_source")


# 9. provider_median stays rejected via guard
def test_09_provider_median_rejected_by_guard():
    bogus = _q("fanduel", -140, source="provider_median")
    hard = harden_lineage([bogus], market_family="hitter_hits",
                           event_id="evt1", sport="mlb")
    assert hard["no_real_book_line"] is True
    assert hard["bookmaker"] is None
    assert hard["lineage_reason"] in ("SYNTHETIC_BOOK_ODDS",
                                       "untrusted_odds_source")


# 10. one broken market does not zero siblings (per-family isolation)
def test_10_broken_sibling_does_not_zero_other_family():
    broken = [_q("provider_median", -140, source="provider_median")]
    working = [_q("fanduel", -140, source="the_odds_api")]

    hard_broken = harden_lineage(broken, market_family="hitter_hits",
                                   event_id="evt1", sport="mlb")
    hard_working = harden_lineage(working, market_family="hitter_total_bases",
                                    event_id="evt1", sport="mlb")

    assert hard_broken["no_real_book_line"] is True
    assert hard_working["no_real_book_line"] is False
    assert hard_working["bookmaker"] == "fanduel"


# Bonus: preferred-book order is deterministic.
def test_preferred_book_order_is_stable():
    assert PREFERRED_BOOK_ORDER == (
        "fanduel", "draftkings", "betmgm", "caesars", "pointsbet",
    )


if __name__ == "__main__":
    import subprocess, sys
    sys.exit(subprocess.call(["python", "-m", "pytest", __file__, "-v"]))
