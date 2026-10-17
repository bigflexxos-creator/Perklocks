"""Canonical wager identity + quote identity (B1 — Commit B/1).

Deterministic structured identity for a wager:
  canonical_wager_id = hash((
      provider_event_id OR canonical_event_id,
      canonical_market_key,
      canonical_player_id OR canonical_team_id,
      line,
      side,
  ))

Deterministic structured identity for a specific observed price point:
  quote_id = hash((canonical_wager_id, bookmaker, observed_at))

These are the ONLY identity keys used by dedupe / refresh replacement /
publication idempotency.  Display strings are presentation only
(see item 15 — structured dedupe).

Collisions are deliberately impossible across (sport × event × market
× player/team × line × side) — SHA-1 digest truncated to 16 hex chars
gives ~2^64 keyspace, which is adequate for the universe we emit.
"""
from __future__ import annotations

import hashlib
from typing import Optional, Union


def _canon(s: Optional[Union[str, int, float]]) -> str:
    if s is None:
        return ""
    return str(s).strip().lower()


def canonical_wager_id(*,
                       provider_event_id: Optional[str] = None,
                       canonical_event_id: Optional[str] = None,
                       provider_market_key: Optional[str] = None,
                       canonical_market_key: Optional[str] = None,
                       canonical_player_id: Optional[str] = None,
                       canonical_team_id: Optional[str] = None,
                       line: Optional[Union[float, int, str]] = None,
                       side: Optional[str] = None,
                       ) -> str:
    """Return a stable canonical_wager_id.

    Precedence:
      * event    — provider_event_id > canonical_event_id
      * market   — canonical_market_key > provider_market_key
      * entity   — canonical_player_id > canonical_team_id
      * line     — normalized to one decimal when numeric
      * side     — case-folded

    An empty / missing bookmaker is intentional: bookmaker belongs
    to the QUOTE identity, never the WAGER identity.
    """
    event = _canon(provider_event_id) or _canon(canonical_event_id)
    market = _canon(canonical_market_key) or _canon(provider_market_key)
    entity = _canon(canonical_player_id) or _canon(canonical_team_id)
    if line is None or line == "":
        line_s = ""
    else:
        try:
            line_s = f"{float(line):.1f}"
        except (TypeError, ValueError):
            line_s = _canon(line)
    side_s = _canon(side)
    key = "|".join((event, market, entity, line_s, side_s))
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return f"cw_{h}"


def quote_id(*,
             canonical_wager_id: str,
             bookmaker: str,
             observed_at: str,
             ) -> str:
    """Return a stable quote_id for ONE per-book price point of a wager."""
    key = "|".join((canonical_wager_id,
                     _canon(bookmaker),
                     _canon(observed_at)))
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return f"q_{h}"


def structured_dedupe_key(*,
                          canonical_wager_id_val: Optional[str] = None,
                          provider_event_id: Optional[str] = None,
                          canonical_event_id: Optional[str] = None,
                          provider_market_key: Optional[str] = None,
                          canonical_market_key: Optional[str] = None,
                          canonical_player_id: Optional[str] = None,
                          canonical_team_id: Optional[str] = None,
                          line: Optional[Union[float, int, str]] = None,
                          side: Optional[str] = None,
                          ) -> tuple[str, tuple]:
    """Return a 2-tuple ``(priority, key)`` for dedupe.

    Priority ordering (per item 15):
      1. ``canonical_wager_id`` (if present)        → ("CW", (cw,))
      2. structured components                        → ("ST", (...))
      3. (caller falls back to legacy display parse)

    Never returns a display-string based key.
    """
    if canonical_wager_id_val:
        return ("CW", (canonical_wager_id_val,))
    event = _canon(provider_event_id) or _canon(canonical_event_id)
    market = _canon(canonical_market_key) or _canon(provider_market_key)
    entity = _canon(canonical_player_id) or _canon(canonical_team_id)
    try:
        line_s = f"{float(line):.1f}" if line not in (None, "") else ""
    except (TypeError, ValueError):
        line_s = _canon(line)
    side_s = _canon(side)
    return ("ST", (event, market, entity, line_s, side_s))


__all__ = [
    "canonical_wager_id",
    "quote_id",
    "structured_dedupe_key",
]
