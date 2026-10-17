"""Universal SourceWager contract (B1 — Commit B/1).

A *SourceWager* is the first-class representation of ONE exact
sportsbook offering, carrying its full acquisition-time provenance
through to canonical publication.  It is what the generic prop
emitter should produce *before* any median / consensus
reconstruction.  Median remains an *analytics* side-channel only.

The contract makes four invariants explicit:

  1. **Identity is structured, not display-string.**  Every wager
     carries ``canonical_wager_id`` + ``quote_id`` + ``provider_event_id``
     + ``provider_market_key`` + ``canonical_player_id`` /
     ``canonical_team_id``.  Dedupe / refresh / publication idempotency
     all key off these fields — never off a display label.

  2. **Line / Side are first-class.**  They are stamped at
     acquisition time from the provider outcome dict and MUST be
     carried verbatim through to publication; parsing them out of
     a display string is a legacy repair-pass concern.

  3. **Observed quotes are preserved.**  ``observed_quotes`` is a
     list of per-bookmaker price points.  Collapsing to
     ``prices: list[int]`` before publication is a lineage defect
     (SYNTHETIC_BOOK_ODDS).

  4. **Exact bookmaker provenance.**  ``bookmaker`` + ``book_odds``
     on the SourceWager are picked by
     ``select_authoritative_book(observed_quotes)`` from the
     ``PREFERRED_BOOK_ORDER`` ladder — never a median.

Design properties:
  * Additive.  Does not touch existing producers.
  * ``ObservedQuote`` (``services/observed_quote.py``) is the
    per-bookmaker atom; ``SourceWager`` wraps a *list* of them with
    the chosen authoritative book bubbled to the top-level fields.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Any, Optional

from services.observed_quote import (
    ObservedQuote,
    PREFERRED_BOOK_ORDER,
    TRUSTED_ODDS_SOURCES,
    BANNED_ODDS_SOURCES,
    validate_observed_quote,
    select_authoritative_book,
    consensus_median_price,
)
from services.canonical_wager_identity import (
    canonical_wager_id as _mk_canonical_wager_id,
    quote_id as _mk_quote_id,
)


# Line-source lineage labels.  These feed the publication boundary's
# fail-closed logic (see ``canonical_publication_boundary``).
#
#   OBSERVED_BOOK   — exact per-bookmaker observation from the
#                     The Odds API payload (REAL — publishable).
#   OBSERVED_BOOK_MEDIAN
#                   — median across observed quotes (ANALYTICS;
#                     NEVER publishable as sportsbook truth).
#   MODEL_ONLY      — no sportsbook offering; model independent
#                     probability only.
#   CONSENSUS_ONLY  — consensus / de-vigged market probability for
#                     context; not a direct sportsbook price.
LINE_SOURCE_OBSERVED_BOOK:          str = "OBSERVED_BOOK"
LINE_SOURCE_OBSERVED_BOOK_MEDIAN:   str = "OBSERVED_BOOK_MEDIAN"
LINE_SOURCE_MODEL_ONLY:             str = "MODEL_ONLY"
LINE_SOURCE_CONSENSUS_ONLY:         str = "CONSENSUS_ONLY"


# Classification tags for ``_build_pick`` call sites (item 68).  The
# publication boundary and the audit test consume this.
BUILD_PICK_SOURCE_DIRECT_REAL_QUOTE: str = "DIRECT_REAL_QUOTE"
BUILD_PICK_SOURCE_CONSENSUS_ONLY:    str = "CONSENSUS_ONLY"
BUILD_PICK_SOURCE_MODEL_ONLY:        str = "MODEL_ONLY"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class SourceWager:
    """ONE exact sportsbook offering carrying full lineage.

    The top-level ``bookmaker`` / ``book_odds`` / ``observed_at`` are
    the AUTHORITATIVE preferred-book choice selected from
    ``observed_quotes`` via ``select_authoritative_book``.
    ``observed_quotes`` retains the full per-book trail for audit.
    """
    # Provider identity (acquisition-time, from The Odds API payload).
    provider: str                              # e.g. "the_odds_api"
    provider_event_id: str
    provider_sport_key: str                    # e.g. "baseball_mlb"
    provider_market_key: str                   # e.g. "batter_hits"
    provider_outcome_name: Optional[str] = None
    provider_outcome_id: Optional[str] = None

    # Canonical identity (Perklocks internal).
    canonical_event_id: Optional[str] = None
    canonical_player_id: Optional[str] = None
    canonical_team_id: Optional[str] = None
    player_name: Optional[str] = None

    # Market shape — first class, stamped at acquisition.
    selection: Optional[str] = None            # display pick side e.g. "Judge Over 1.5 Hits"
    line: Optional[float] = None               # None allowed for binary families
    side: Optional[str] = None                 # "over"|"under"|"home"|"away"|"yes"|...

    # Authoritative sportsbook truth (chosen by preferred-book ladder).
    bookmaker: Optional[str] = None            # exact book slug
    book_odds: Optional[int] = None            # American odds integer
    observed_at: Optional[str] = None          # ISO-8601 UTC

    # Lineage lineage lineage.
    line_source: str = LINE_SOURCE_OBSERVED_BOOK
    is_alt: bool = False

    # Full per-book trail.  NEVER collapse to price-only arrays.
    observed_quotes: list[dict] = field(default_factory=list)

    # Analytics side-channel: median across observed_quotes.  NOT
    # publishable as book_odds.  Lives here for consensus analytics /
    # de-vig context.
    consensus_price: Optional[int] = None

    # Structured identity keys (populated by ``finalize()``).
    canonical_wager_id: Optional[str] = None
    quote_id: Optional[str] = None

    # Diagnostic extras (never consumed by publication).
    extras: dict[str, Any] = field(default_factory=dict)

    # ── Construction helpers ────────────────────────────────────────
    def finalize(self) -> "SourceWager":
        """Compute ``canonical_wager_id`` + ``quote_id`` once all
        identity fields are populated.  Idempotent.
        """
        if not self.canonical_wager_id:
            self.canonical_wager_id = _mk_canonical_wager_id(
                provider_event_id=self.provider_event_id,
                canonical_event_id=self.canonical_event_id,
                provider_market_key=self.provider_market_key,
                canonical_player_id=self.canonical_player_id,
                canonical_team_id=self.canonical_team_id,
                line=self.line,
                side=self.side,
            )
        if not self.quote_id and self.bookmaker and self.observed_at:
            self.quote_id = _mk_quote_id(
                canonical_wager_id=self.canonical_wager_id,
                bookmaker=self.bookmaker,
                observed_at=self.observed_at,
            )
        return self

    def to_dict(self) -> dict:
        return asdict(self)

    # ── Factories ────────────────────────────────────────────────────
    @classmethod
    def from_observed_quotes(cls,
                             quotes: list[ObservedQuote | dict],
                             *,
                             provider: str = "the_odds_api",
                             canonical_event_id: Optional[str] = None,
                             canonical_player_id: Optional[str] = None,
                             canonical_team_id: Optional[str] = None,
                             is_alt: bool = False,
                             selection: Optional[str] = None,
                             preferred_order: tuple[str, ...] = PREFERRED_BOOK_ORDER,
                             ) -> Optional["SourceWager"]:
        """Build ONE SourceWager from a list of per-book quotes.

        * Picks the authoritative book via ``select_authoritative_book``.
        * Carries all validated observed_quotes verbatim.
        * Returns ``None`` iff no quote survives validation — the
          caller MUST then fall back to a MODEL_ONLY wager (never
          fabricate a median as book_odds).
        """
        if not quotes:
            return None
        chosen = select_authoritative_book(
            quotes, preferred_order=preferred_order)
        if not chosen:
            return None
        trail: list[dict] = []
        for q in quotes:
            d = asdict(q) if isinstance(q, ObservedQuote) else dict(q)
            ok, _reason = validate_observed_quote(q)
            if ok:
                trail.append(d)
        median = consensus_median_price(quotes)
        sw = cls(
            provider=provider,
            provider_event_id=str(chosen.get("provider_event_id") or ""),
            provider_sport_key=str(chosen.get("sport") or ""),
            provider_market_key=str(chosen.get("provider_market_key") or ""),
            provider_outcome_name=chosen.get("player_name") or chosen.get("side"),
            canonical_event_id=canonical_event_id,
            canonical_player_id=canonical_player_id,
            canonical_team_id=canonical_team_id,
            player_name=chosen.get("player_name"),
            selection=selection,
            line=chosen.get("line"),
            side=chosen.get("side"),
            bookmaker=chosen.get("bookmaker"),
            book_odds=int(chosen["price"]) if chosen.get("price") is not None else None,
            observed_at=chosen.get("observed_at"),
            line_source=LINE_SOURCE_OBSERVED_BOOK,
            is_alt=is_alt,
            observed_quotes=trail,
            consensus_price=median,
        ).finalize()
        return sw


def classify_build_pick_call(*,
                              odds_source: Optional[str],
                              book_odds: Optional[int],
                              observed_quotes: Optional[list] = None,
                              ) -> str:
    """Classify a ``_build_pick`` call site per item 68.

    * DIRECT_REAL_QUOTE   — explicit real sportsbook provenance
                            (``odds_source`` ∈ TRUSTED and
                            ``book_odds`` is not None).
    * CONSENSUS_ONLY      — no explicit real provenance BUT a book
                            price is present (median/consensus
                            reconstruction — must fail-closed at
                            publication boundary).
    * MODEL_ONLY          — no book odds (model independent prob
                            only — ``no_real_book_line=True``).
    """
    src = (odds_source or "").lower().strip()
    if book_odds is None:
        return BUILD_PICK_SOURCE_MODEL_ONLY
    if src in TRUSTED_ODDS_SOURCES and src not in BANNED_ODDS_SOURCES:
        return BUILD_PICK_SOURCE_DIRECT_REAL_QUOTE
    if src in BANNED_ODDS_SOURCES:
        return BUILD_PICK_SOURCE_CONSENSUS_ONLY
    # No odds_source + book_odds present → median-reconstruction
    # path.  Publication boundary will fail-closed as SYNTHETIC.
    return BUILD_PICK_SOURCE_CONSENSUS_ONLY


__all__ = [
    "SourceWager",
    "LINE_SOURCE_OBSERVED_BOOK",
    "LINE_SOURCE_OBSERVED_BOOK_MEDIAN",
    "LINE_SOURCE_MODEL_ONLY",
    "LINE_SOURCE_CONSENSUS_ONLY",
    "BUILD_PICK_SOURCE_DIRECT_REAL_QUOTE",
    "BUILD_PICK_SOURCE_CONSENSUS_ONLY",
    "BUILD_PICK_SOURCE_MODEL_ONLY",
    "classify_build_pick_call",
]
