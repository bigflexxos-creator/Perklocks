"""Magic 3A — Deterministic line-extraction fallback.

Compatibility-parse fallback per Phase 2 of the Magic-3A directive.
Extracts a numeric threshold from a market/selection string WHEN
AND ONLY WHEN the exact numeric value is present verbatim.

Rules
─────
* Never invent 0.5 or any generic threshold based solely on market
  type.
* Never infer from odds, model probability, historical averages.
* Return None when the string does not literally contain the number.
* Producer-side callers MUST tag the result with provenance
  ``line_source="selection_parse_fallback"`` (distinct from
  ``line_source="sportsbook_structured"``).
"""
from __future__ import annotations

import re
from typing import Optional


# Match Over/Under N.N, ±N.N Spread, +/- N.N handicap patterns.
_PATTERNS = [
    # "Over 1.5 Assists" / "Under 2.5 Points"
    re.compile(r"\b(?:over|under|o|u)\s+([+-]?\d+(?:\.\d+)?)\b", re.I),
    # "+1.5 Spread" / "-4.5"
    re.compile(r"(?:^|\s)([+-]\d+(?:\.\d+)?)(?:\s+(?:spread|line|handicap))?\b", re.I),
    # "Over/Under 2.5"
    re.compile(r"\bo/u\s+([+-]?\d+(?:\.\d+)?)\b", re.I),
    # "1.5+ Hits" (alt-line style)
    re.compile(r"\b(\d+(?:\.\d+)?)\+\s+\w+\b", re.I),
]


def extract_side(text: str) -> Optional[str]:
    if not text:
        return None
    t = text.strip().lower()
    if re.search(r"\bover\b|\bo\s+\d", t):  return "over"
    if re.search(r"\bunder\b|\bu\s+\d", t): return "under"
    if re.search(r"\+\d",  t):              return "positive_spread"
    if re.search(r"-\d",   t):              return "negative_spread"
    return None


def extract_line(market: str, selection: str = "") -> Optional[float]:
    """Extract a numeric betting threshold — or None if the string
    doesn't literally contain a number.

    Combines market + selection because the threshold sometimes
    lives in one and the side in the other (e.g., market='Total
    Games Over 21.5', selection='Over').  Never guesses.
    """
    combined = f"{market or ''} {selection or ''}".strip()
    if not combined:
        return None
    # Moneyline / h2h has no line — bail out early to avoid
    # accidentally matching a jersey number or year.
    lc = combined.lower()
    if "moneyline" in lc and not any(k in lc for k in
                                        ("over", "under", "spread",
                                          "+", "-")):
        return None
    for pat in _PATTERNS:
        m = pat.search(combined)
        if m:
            try:
                v = float(m.group(1))
                # Sanity: allow reasonable betting-threshold range only.
                if -50.0 <= v <= 2000.0:
                    return v
            except (TypeError, ValueError):
                continue
    return None


def extract_line_with_provenance(
    market: str, selection: str = "",
    *, structured_line: Optional[float] = None,
) -> dict:
    """Return {'line', 'side', 'line_source'} — structured wins over
    parse; parse wins over None; None is returned when neither
    source produces a value."""
    # Side is deterministically parseable from either the market OR
    # the selection string — combine both so patterns like
    # ("market='Mikal Bridges Over 1.5 Assists', selection='Mikal
    # Bridges'") still yield side='over'.  Prefer selection when it
    # ALREADY carries a proven side token (e.g., 'Over'/'Under'), but
    # fall through to the market string when selection is just a
    # team/player name.
    _side_from_selection = extract_side(selection)
    _side_from_market    = extract_side(market)
    _side = _side_from_selection or _side_from_market

    if structured_line is not None:
        try:
            v = float(structured_line)
            return {"line": v, "side": _side,
                     "line_source": "sportsbook_structured"}
        except (TypeError, ValueError):
            pass
    v = extract_line(market, selection)
    if v is None:
        return {"line": None, "side": _side,
                 "line_source": None}
    return {"line": v, "side": _side,
             "line_source": "selection_parse_fallback"}


__all__ = ["extract_line", "extract_side", "extract_line_with_provenance"]
