"""Quote-lineage guard — shared adapter producers can use at the
publication boundary (Blocks 3, 32, 50).

Produces a canonical ``book_odds`` + ``bookmaker`` + ``odds_source``
tuple from an iterable of observed quotes, OR explicitly returns a
``no_real_book_line=True`` payload when no observation survives.

Zero formula changes.  Zero producer coupling.  Each producer migrates
at its own pace by calling ``harden_lineage(...)`` just before passing
a candidate pick into the canonical publication boundary.

2026-10 NHL exact-identity extension: ``harden_lineage`` accepts an
optional ``require_identity`` dict.  When supplied, the surviving
authoritative quote MUST equal the identity tuple exactly (bookmaker,
event_id, player, market, side, line).  Any mismatch triggers
fail-closed (``no_real_book_line=True``) — this is the NHL FanDuel
guarantee.
"""
from __future__ import annotations

import unicodedata
from typing import Any, Iterable, Optional

from services.observed_quote import (
    ObservedQuote, select_authoritative_book, consensus_median_price,
    validate_observed_quote,
)


def _norm_name(name: str) -> str:
    if not name:
        return ""
    nk = unicodedata.normalize("NFKD", name)
    return "".join(c for c in nk if not unicodedata.combining(c)).lower().strip()


def _canon_line(x) -> Optional[float]:
    if x is None:
        return None
    try:
        return round(float(x), 1)
    except (TypeError, ValueError):
        return None


def harden_lineage(quotes: Iterable[ObservedQuote | dict],
                    *, market_family: str,
                    event_id: str,
                    sport: str,
                    require_identity: Optional[dict] = None) -> dict:
    """Choose the authoritative observed quote and return a dict of
    publication fields that the canonical publication boundary will
    accept.

    When ``require_identity`` is supplied (NHL exact-identity path),
    the surviving quote is re-checked against the full wager-identity
    tuple before publication.  Shape:
        {"bookmaker": "fanduel", "event_id": ..., "player": ...,
         "market_key": ..., "side": "over", "line": 2.5}

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
    # Materialize quotes once — the iterable may be consumed below.
    materialized = list(quotes)

    survivors_as_dicts: list[dict] = []
    best_reason: Optional[str] = None
    for q in materialized:
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

    best = select_authoritative_book(materialized)
    assert best is not None, "survivors non-empty but selector returned None"

    # 2026-10 NHL exact-identity post-check.  Re-verify the SELECTED
    # quote object itself carries the required identity.  This is
    # defense-in-depth even after the attach pass has already filtered
    # — we never trust an earlier layer to have been clean.
    if require_identity:
        want_book    = str(require_identity.get("bookmaker") or "").lower()
        want_event   = str(require_identity.get("event_id") or "")
        want_player  = _norm_name(require_identity.get("player") or "")
        want_market  = str(require_identity.get("market_key") or "").lower()
        want_side    = str(require_identity.get("side") or "").lower()
        want_line    = _canon_line(require_identity.get("line"))

        got_book     = str(best.get("bookmaker") or "").lower()
        got_event    = str(best.get("event_id")
                             or best.get("provider_event_id") or "")
        got_player   = _norm_name(best.get("player_name") or "")
        got_market   = str(best.get("provider_market_key")
                             or best.get("market_family") or "").lower()
        got_side     = str(best.get("side") or "").lower()
        got_line     = _canon_line(best.get("line"))

        mismatch = None
        if want_book and got_book != want_book:
            mismatch = "identity_book_mismatch"
        elif want_event and got_event != want_event:
            mismatch = "identity_event_mismatch"
        elif want_player and got_player != want_player:
            mismatch = "identity_player_mismatch"
        elif want_market and got_market != want_market:
            mismatch = "identity_market_mismatch"
        elif want_side and got_side != want_side:
            mismatch = "identity_side_mismatch"
        elif got_line != want_line:
            mismatch = "identity_line_mismatch"

        if mismatch:
            return {
                "book_odds": None,
                "bookmaker": None,
                "odds_source": "MODEL_ONLY",
                "no_real_book_line": True,
                "consensus_price": None,
                "lineage_reason": mismatch,
                "observed_at": None,
                "event_id": event_id,
                "sport": sport,
                "market_family": market_family,
            }

    return {
        "book_odds":    int(best["price"]),
        "bookmaker":    best["bookmaker"],
        "odds_source":  best["odds_source"],
        "no_real_book_line": False,
        "consensus_price": consensus_median_price(materialized),
        "lineage_reason":  None,
        "observed_at":  best.get("observed_at"),
        "event_id":     event_id,
        "sport":        sport,
        "market_family": market_family,
        # Expose the verified identity for downstream assertions and
        # tests.  Callers can `assert hardened["selected_identity"]
        # == expected_tuple` without touching private shapes.
        "selected_identity": {
            "bookmaker":  str(best.get("bookmaker") or "").lower(),
            "event_id":   str(best.get("event_id")
                               or best.get("provider_event_id") or ""),
            "player":     _norm_name(best.get("player_name") or ""),
            "market_key": str(best.get("provider_market_key")
                               or best.get("market_family") or "").lower(),
            "side":       str(best.get("side") or "").lower(),
            "line":       _canon_line(best.get("line")),
        },
    }


__all__ = ["harden_lineage"]
