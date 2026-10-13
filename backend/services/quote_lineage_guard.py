"""Quote-lineage guard — shared adapter producers can use at the
publication boundary (Blocks 3, 32, 50).

Produces a canonical ``book_odds`` + ``bookmaker`` + ``odds_source``
tuple from an iterable of observed quotes, OR explicitly returns a
``no_real_book_line=True`` payload when no observation survives.

Zero formula changes.  Zero producer coupling.  Each producer migrates
at its own pace by calling ``harden_lineage(...)`` just before passing
a candidate pick into the canonical publication boundary.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from services.observed_quote import (
    ObservedQuote, select_authoritative_book, consensus_median_price,
    validate_observed_quote,
)


def harden_lineage(quotes: Iterable[ObservedQuote | dict],
                    *, market_family: str,
                    event_id: str,
                    sport: str) -> dict:
    """Choose the authoritative observed quote and return a dict of
    publication fields that the canonical publication boundary will
    accept.

    Return shape:
      {
        "book_odds":        int | None,
        "bookmaker":        str | None,
        "odds_source":      "the_odds_api" | ... | "MODEL_ONLY",
        "no_real_book_line": bool,
        "consensus_price":  int | None,    # analytics-only context
        "lineage_reason":   None | "SYNTHETIC_BOOK_ODDS" | ...,
        "observed_at":      str | None,
      }
    """
    survivors_as_dicts: list[dict] = []
    best_reason: Optional[str] = None
    for q in quotes:
        ok, reason = validate_observed_quote(q)
        if ok:
            survivors_as_dicts.append(
                q if isinstance(q, dict) else q.__dict__
            )
        elif best_reason is None:
            best_reason = reason

    if not survivors_as_dicts:
        return {
            "book_odds": None,
            "bookmaker": None,
            "odds_source": "MODEL_ONLY",
            "no_real_book_line": True,
            "consensus_price": None,
            "lineage_reason": best_reason or "no_observed_quote",
            "observed_at": None,
            "event_id": event_id,
            "sport": sport,
            "market_family": market_family,
        }

    best = select_authoritative_book(quotes)
    assert best is not None, "survivors non-empty but selector returned None"
    return {
        "book_odds":    int(best["price"]),
        "bookmaker":    best["bookmaker"],
        "odds_source":  best["odds_source"],
        "no_real_book_line": False,
        "consensus_price": consensus_median_price(quotes),
        "lineage_reason":  None,
        "observed_at":  best.get("observed_at"),
        "event_id":     event_id,
        "sport":        sport,
        "market_family": market_family,
    }


__all__ = ["harden_lineage"]
