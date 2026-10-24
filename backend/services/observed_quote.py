"""Universal Observed Quote Object (Blocks 2-7, 32).

Shared schema + validators for every observed sportsbook quote.
Producers that currently build price-only buckets MUST migrate to
emit ``ObservedQuote`` dicts so bookmaker lineage survives aggregation.

A SYNTHETIC_BOOK_ODDS rejection downstream of
``canonical_publication_boundary`` is a *lineage* failure (price lost
its bookmaker label at some point in the aggregation chain) — NOT a
model failure.  This module makes the lineage invariant explicit so
new producers inherit the correct shape and existing producers can be
migrated incrementally.

GUARDRAIL: this module is additive.  It does not modify existing
producers.  Each sport-specific producer can opt in by calling
``validate_observed_quote(q)`` or ``select_authoritative_book(quotes)``
in its own commit.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional


# Preferred bookmaker order when an exact threshold / line / side is
# available in multiple books (Block 5).  Order is deliberate; the
# first observed book in this list wins.
PREFERRED_BOOK_ORDER: tuple[str, ...] = (
    "fanduel", "draftkings", "betmgm", "caesars", "pointsbet",
)

# Trusted real-sportsbook odds-source labels.  Mirrors (without
# importing — avoids a circular dep) the authoritative list in
# ``canonical_publication_boundary._REAL_ODDS_SOURCES``.
TRUSTED_ODDS_SOURCES: frozenset[str] = frozenset({
    "the_odds_api", "theoddsapi", "odds_api", "odds-api",
    "sportsbook", "sportsbook_verified", "sportsbook_real",
    "prop-line", "propline", "prop_line",
    "draftkings", "fanduel", "betmgm", "caesars", "pointsbet",
    "espn", "real_book_line",
})

# Labels that MUST NEVER appear in a published book_odds row.
BANNED_ODDS_SOURCES: frozenset[str] = frozenset({
    "provider_median", "provider-median",
    "median", "consensus",
    "synthetic", "model_derived", "model-derived",
    "espn_fallback",
})


@dataclass(frozen=True)
class ObservedQuote:
    """Canonical observed sportsbook quote.

    Every field (except the two optional contextual ones) is required
    for the row to be considered a REAL observation.  Producers must
    NOT invent values; absence = exclude from the aggregate.
    """
    sport: str
    event_id: str
    provider_event_id: str
    provider_market_key: str
    market_family: str
    line: Optional[float]           # None allowed for binary families (ML, BTTS)
    side: str                        # "over" | "under" | "home" | "away" | "yes" | "no" | ...
    bookmaker: str                   # exact book slug (fanduel, draftkings, ...)
    price: int                       # American odds integer
    odds_source: str                 # must be in TRUSTED_ODDS_SOURCES
    observed_at: str                 # ISO-8601 UTC
    player_id: Optional[str] = None  # None for team/game markets
    player_name: Optional[str] = None
    extras: dict[str, Any] = field(default_factory=dict)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def validate_observed_quote(q: ObservedQuote | dict) -> tuple[bool, Optional[str]]:
    """Return (ok, reason).  ok=False with a canonical reason code
    describes exactly what lineage field is missing.

    This is the SHARED gate every producer should use before adding a
    row to its aggregate.  The reason codes mirror
    ``canonical_publication_boundary.RejectionReason`` where possible.
    """
    d = asdict(q) if isinstance(q, ObservedQuote) else dict(q)
    for required in ("sport", "event_id", "provider_event_id",
                      "provider_market_key", "market_family", "side",
                      "bookmaker", "odds_source", "observed_at"):
        if not d.get(required):
            return False, f"missing_lineage_field:{required}"
    if d.get("price") is None:
        return False, "missing_lineage_field:price"
    try:
        int(d["price"])
    except Exception:
        return False, "price_not_integer_american"

    src = str(d.get("odds_source") or "").lower()
    if src in BANNED_ODDS_SOURCES:
        return False, "SYNTHETIC_BOOK_ODDS"
    if src not in TRUSTED_ODDS_SOURCES:
        return False, "untrusted_odds_source"

    book = str(d.get("bookmaker") or "").lower()
    if book in BANNED_ODDS_SOURCES or book in {"", "median", "consensus"}:
        return False, "SYNTHETIC_BOOK_ODDS"

    return True, None


def select_authoritative_book(
    quotes: Iterable[ObservedQuote | dict],
    *,
    preferred_order: tuple[str, ...] = PREFERRED_BOOK_ORDER,
) -> Optional[dict]:
    """Pick ONE exact observed sportsbook quote to publish (Block 5).

    * Only validated quotes are considered.
    * Books are ranked by ``preferred_order``; unknown books fall into
      a deterministic alphabetical tail (so the output is stable).
    * Ties on bookmaker preference break on most-recent ``observed_at``.
    * Returns ``None`` when NO observed quote survives validation —
      the caller MUST then set ``book_odds=None`` and ``odds_source=
      MODEL_ONLY`` or ``NO_REAL_LINE`` (never fabricate a median).
    """
    survivors: list[dict] = []
    for q in quotes:
        ok, _reason = validate_observed_quote(q)
        if not ok:
            continue
        survivors.append(asdict(q) if isinstance(q, ObservedQuote) else dict(q))
    if not survivors:
        return None

    rank: dict[str, int] = {b.lower(): i for i, b in enumerate(preferred_order)}
    tail_base = len(preferred_order)

    def _key(d: dict) -> tuple[int, str, str]:
        b = str(d.get("bookmaker") or "").lower()
        if b in rank:
            r = rank[b]
        else:
            r = tail_base
        # recency DESC → negate ISO timestamp alphabetically so later
        # strings sort first within the same book bucket.
        return (r, b, "~" + str(d.get("observed_at") or ""))

    survivors.sort(key=_key, reverse=False)
    # Within same rank, more-recent timestamp wins — the "~" prefix in
    # _key sorts larger ISO strings LAST, so flip at the end:
    best_rank = _key(survivors[0])[0]
    same_rank = [s for s in survivors if _key(s)[0] == best_rank]
    same_rank.sort(key=lambda d: str(d.get("observed_at") or ""), reverse=True)
    return same_rank[0]


def _canon_line_strict(x) -> Optional[float]:
    """Canonical form used for strict wager-identity equality."""
    if x is None:
        return None
    try:
        return round(float(x), 1)
    except (TypeError, ValueError):
        return None


def _norm_name_strict(name: str) -> str:
    """Match adapter normalization so identity checks line up."""
    if not name:
        return ""
    import unicodedata
    nk = unicodedata.normalize("NFKD", name)
    return "".join(c for c in nk if not unicodedata.combining(c)).lower().strip()


def select_exact_fanduel_quote(
    quotes: Iterable[ObservedQuote | dict],
    *,
    pick_event_id: str,
    pick_player: Optional[str],
    pick_market_key: str,
    pick_side: str,
    pick_line: Optional[float],
) -> tuple[Optional[dict], Optional[str]]:
    """Return ``(quote, reason)`` where ``quote`` is the SINGLE
    FanDuel observed quote whose full wager identity matches the pick
    exactly: event + player + market + side + line + book=fanduel.

    When no such quote exists, returns ``(None, reason_code)`` where
    reason_code is one of:
        - "no_fanduel_quote"
        - "cross_event_rejected"
        - "cross_player_rejected"
        - "cross_market_rejected"
        - "cross_side_rejected"
        - "cross_line_rejected"
        - "no_valid_fanduel_quote"
        - "no_observed_quotes"

    This is the strict-identity gate used by the NHL attach path.
    """
    if not quotes:
        return None, "no_observed_quotes"

    want_event = str(pick_event_id or "")
    want_player_norm = _norm_name_strict(pick_player or "")
    want_market = (pick_market_key or "").lower()
    want_side = (pick_side or "").lower()
    want_line = _canon_line_strict(pick_line)

    reasons: list[str] = []
    survivors: list[dict] = []

    for q in quotes:
        d = asdict(q) if isinstance(q, ObservedQuote) else dict(q)
        # Validate lineage (trusted source, non-banned book, ...)
        ok, _vreason = validate_observed_quote(d)
        if not ok:
            continue
        # BOOK must be FanDuel — zero fallback.
        if str(d.get("bookmaker") or "").lower() != "fanduel":
            reasons.append("no_fanduel_quote")
            continue
        # EVENT must match exactly.
        q_event = str(d.get("event_id")
                       or d.get("provider_event_id") or "")
        if want_event and q_event != want_event:
            reasons.append("cross_event_rejected")
            continue
        # PLAYER must match (normalized) — only when the pick carries
        # a player.  Team/game markets have an empty player name on
        # both sides and still match.
        q_player = _norm_name_strict(d.get("player_name") or "")
        if want_player_norm and q_player != want_player_norm:
            reasons.append("cross_player_rejected")
            continue
        # MARKET must match exactly.
        q_market = str(d.get("provider_market_key")
                        or d.get("market_family") or "").lower()
        if q_market != want_market:
            reasons.append("cross_market_rejected")
            continue
        # SIDE must match exactly.
        if str(d.get("side") or "").lower() != want_side:
            reasons.append("cross_side_rejected")
            continue
        # LINE must match exactly (None==None for binary markets).
        if _canon_line_strict(d.get("line")) != want_line:
            reasons.append("cross_line_rejected")
            continue
        survivors.append(d)

    if not survivors:
        # Pick the strongest reason seen (first non-generic).
        for r in ("cross_event_rejected", "cross_player_rejected",
                   "cross_market_rejected", "cross_side_rejected",
                   "cross_line_rejected", "no_fanduel_quote"):
            if r in reasons:
                return None, r
        return None, "no_valid_fanduel_quote"

    # Ties on identity → most-recent observation wins.
    survivors.sort(key=lambda d: str(d.get("observed_at") or ""),
                    reverse=True)
    return survivors[0], None


def consensus_median_price(quotes: Iterable[ObservedQuote | dict]) -> Optional[int]:
    """Return a median American price for ANALYTICS / DEVIG only.

    Block 4: median/consensus is CONTEXT, never authoritative
    ``book_odds``.  Producers MUST tag the result with
    ``odds_source="provider_median"`` (which is banned from publication)
    or store it in a separate ``consensus_price`` field.
    """
    xs: list[int] = []
    for q in quotes:
        d = asdict(q) if isinstance(q, ObservedQuote) else dict(q)
        try:
            xs.append(int(d["price"]))
        except Exception:
            continue
    if not xs:
        return None
    xs.sort()
    n = len(xs)
    if n % 2 == 1:
        return xs[n // 2]
    # even count — pick the LOWER-magnitude (closer-to-even) of the two
    # middle values deterministically.
    a, b = xs[n // 2 - 1], xs[n // 2]
    return a if abs(a) <= abs(b) else b


__all__ = [
    "ObservedQuote",
    "PREFERRED_BOOK_ORDER", "TRUSTED_ODDS_SOURCES", "BANNED_ODDS_SOURCES",
    "validate_observed_quote", "select_authoritative_book",
    "select_exact_fanduel_quote",
    "consensus_median_price",
]
