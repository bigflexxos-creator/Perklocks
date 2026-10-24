"""Shared lineage adapter: extract ObservedQuote-shaped list from a
raw Odds-API per-event props payload for ANY sport.

Producers (MLB, NHL, NFL, CFB, Tennis) that already fetch a per-event
props payload via ``sports_engine._fetch_event_props_payload`` can
call ``extract_observed_quotes(payload, sport, event_id, markets=...)``
and attach the result to each emitted pick as ``observed_quotes``.

The universal pre-publication lineage pass
(``services/prepublication_lineage_pass.py``) then upgrades every
candidate's ``book_odds`` / ``bookmaker`` / ``odds_source`` from the
exact observed sportsbook quote — SYNTHETIC_BOOK_ODDS lineage is
closed at the producer layer.

This module is deliberately producer-agnostic: ANY code path that
has access to the per-event payload can wire lineage in <3 lines.
"""
from __future__ import annotations

import logging
import unicodedata
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

logger = logging.getLogger("lockscore.observed_quote_adapter")


def _norm_name(name: str) -> str:
    if not name:
        return ""
    nk = unicodedata.normalize("NFKD", name)
    return "".join(c for c in nk if not unicodedata.combining(c)).lower().strip()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canon_line(x) -> Optional[float]:
    """Canonicalize a sportsbook line for strict equality bucketing.

    * ``None`` passes through unchanged (binary markets: ML, BTTS).
    * All numeric values are rounded to 1-decimal precision — the
      half-point grid every US book publishes against (0.5, 1.5, 2.5,
      -3.5, ...).  Guards against float-noise mismatches like
      ``2.4999999`` vs ``2.5``.
    """
    if x is None:
        return None
    try:
        return round(float(x), 1)
    except (TypeError, ValueError):
        return None


def extract_observed_quotes(
    payload: dict,
    *,
    sport: str,
    event_id: str,
    allowed_markets: Optional[Iterable[str]] = None,
) -> dict[tuple[str, str, str, Optional[float]], list[dict]]:
    """Return {(market_key, normalized_name, side, line): [observed_quotes]}.

    Each observed quote is already in the ObservedQuote shape and
    passes `services.observed_quote.validate_observed_quote` provided
    the raw payload came from a trusted provider (`the_odds_api`).

    LINE IS PART OF THE BUCKET KEY (2026-10 NHL exact-identity fix).
    This prevents Over 1.5 / Over 2.5 (same market+player+side) from
    collapsing into one bucket.  Non-NHL callers that query without
    specifying ``line`` keep today's behavior via ``quotes_for``'s
    partial-lookup path, which iterates and aggregates across lines.

    Parameters
    ----------
    payload : dict
        Raw ``_fetch_event_props_payload`` response.  Expected to
        have a ``bookmakers`` list.
    sport, event_id : str
        Written into every emitted quote.
    allowed_markets : iterable of market keys, optional
        If provided, only quotes for these markets are returned.
        Otherwise ALL markets in the payload are extracted.
    """
    out: dict[tuple[str, str, str, Optional[float]], list[dict]] = {}
    if not isinstance(payload, dict):
        return out
    allowed_lower = (
        set(m.lower() for m in allowed_markets) if allowed_markets else None
    )
    observed_at = (
        payload.get("last_update")
        or payload.get("commence_time")
        or _now_iso()
    )
    for bm in (payload.get("bookmakers") or []):
        book_key = (bm.get("key") or bm.get("title") or "").lower()
        if not book_key:
            continue
        for m in (bm.get("markets") or []):
            mk = (m.get("key") or "").lower()
            if not mk:
                continue
            if allowed_lower is not None and mk not in allowed_lower:
                continue
            for o in (m.get("outcomes") or []):
                # Universal shape across sports:
                #   - name is EITHER Over/Under/Yes/No OR the player
                #     name directly (team markets).
                #   - description is the player name on binary families.
                raw_player = (
                    o.get("description")
                    or (o.get("name") if (o.get("name") or "").lower() not in
                        ("yes", "no", "over", "under") else None)
                )
                side_label = (o.get("name") or "").lower()
                if side_label not in ("yes", "no", "over", "under"):
                    # Team market or spread — treat selection name as side.
                    side_label = side_label or ""
                try:
                    price = int(o.get("price"))
                except (TypeError, ValueError):
                    continue
                try:
                    line = float(o.get("point")) if o.get("point") is not None else None
                except (TypeError, ValueError):
                    line = None
                player_norm = _norm_name(raw_player or "")
                line_canon = _canon_line(line)
                key = (mk, player_norm, side_label, line_canon)
                out.setdefault(key, []).append({
                    "sport":             sport,
                    "event_id":          str(event_id),
                    "provider_event_id": str(event_id),
                    "provider_market_key": mk,
                    "market_family":     mk,
                    "line":              line_canon,
                    "side":              side_label or "yes",
                    "bookmaker":         book_key,
                    "price":             price,
                    "odds_source":       "the_odds_api",
                    "observed_at":       observed_at,
                    "player_name":       raw_player,
                })
    return out


def quotes_for(
    extracted: dict[tuple[str, str, str, Optional[float]], list[dict]],
    *, market_key: str, player: Optional[str] = None,
    side: Optional[str] = None,
    line: Optional[float] = None,
    bookmaker: Optional[str] = None,
    require_line_match: bool = False,
) -> list[dict]:
    """Return the quote list for one (market, player, side[, line]) tuple.

    When ``line`` is provided AND ``require_line_match`` is True, ONLY
    quotes whose canonical line equals the requested line are returned
    (strict equality — tolerance 0).  Mandatory for NHL exact-identity
    attachment.  Default (``require_line_match=False``) preserves
    today's partial-lookup aggregation for non-NHL callers.

    When ``bookmaker`` is provided, only quotes from that exact book
    slug survive the filter.  Mandatory for NHL FanDuel-only attach.
    """
    mk = (market_key or "").lower()
    p  = _norm_name(player or "")
    sd = (side or "").lower()
    bk = (bookmaker or "").lower()
    line_canon = _canon_line(line) if line is not None else None

    out: list[dict] = []
    for (ek_mk, ek_p, ek_sd, ek_line), rows in extracted.items():
        if ek_mk != mk:
            continue
        if p and ek_p != p:
            continue
        if sd and ek_sd != sd:
            continue
        if require_line_match and ek_line != line_canon:
            continue
        if bk:
            # Filter the rows list itself by bookmaker — the key does
            # NOT carry book, so this is a per-row filter.
            rows = [r for r in rows
                    if str(r.get("bookmaker") or "").lower() == bk]
        out.extend(rows)
    return out


__all__ = ["extract_observed_quotes", "quotes_for", "_norm_name", "_canon_line"]
