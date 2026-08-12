"""MAGIC 3A.1 — Producer-side line/side/line_source attacher.

The ONE canonical entry-point every active producer's candidates
cross before publication.  It attaches three first-class fields to
each candidate:

    line          : Optional[float]   — numeric betting threshold.
    side          : Optional[str]     — over/under/positive_spread/…
    line_source   : Optional[str]     — provenance of the numeric line.

Source priority (per user directive)
────────────────────────────────────
    A. `sportsbook_structured`      (preferred, from a real book feed)
    B. `selection_parse_fallback`   (deterministic parse of the
                                     market/selection string via
                                     ``services.magic.line_extractor``)
    C. Unrecoverable                → line=None, line_source=None.

Idempotency
───────────
Existing `sportsbook_structured` values are NEVER overwritten.  A
candidate that already carries `line_source == "sportsbook_structured"`
passes through untouched.  Parse-fallback values ARE recomputed on
every call (the market/selection string is the source of truth and
the extractor is deterministic).

Side inference
──────────────
Side is deterministically parsed from the immutable market/selection
string.  It is NEVER inferred from model direction, bet outcome, or
implied probability.  When a side cannot be proven, it stays None.

Producers do NOT need to call this directly — `publish_batch` invokes
it inside the canonical publication boundary so every producer gets
line preservation for free.  It IS safe (and recommended) for
producers that emit real-book lines to also set
``line_source="sportsbook_structured"`` themselves.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from services.magic.line_extractor import (
    extract_line_with_provenance,
)


# ── Structured source tokens that MUST NOT be overwritten ────────────
_STRUCTURED_SOURCES: frozenset[str] = frozenset({
    "sportsbook_structured",
    "the_odds_api_structured",
    "book_line_structured",
})


def _to_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def attach_line_fields(candidate: dict) -> dict:
    """Attach line/side/line_source to a single candidate dict.

    Never mutates settlement or model fields.  Only writes into the
    three canonical keys.  Returns the same dict for chainability.
    """
    if not isinstance(candidate, dict):
        return candidate

    existing_line = _to_float(candidate.get("line"))
    existing_src = candidate.get("line_source")
    existing_side = candidate.get("side")

    # Rule 1: preserve trustworthy structured lines untouched.
    if existing_src in _STRUCTURED_SOURCES and existing_line is not None:
        # Side is deterministically parseable from the immutable
        # market/selection string.  If it's missing, add it.
        if not existing_side:
            r = extract_line_with_provenance(
                candidate.get("market") or "",
                candidate.get("selection") or "",
                structured_line=existing_line,
            )
            candidate["side"] = r["side"]
        return candidate

    market = candidate.get("market") or ""
    selection = candidate.get("selection") or ""

    # Rule 2: producers that already provide a structured numeric line
    # (but forgot to tag its source) — infer sportsbook_structured
    # ONLY when the candidate carries a real sportsbook price and a
    # numeric line simultaneously (both signals of a book feed).
    if (
        existing_line is not None
        and existing_src is None
        and candidate.get("book_odds") is not None
        and str(candidate.get("odds_source") or "").lower() in {
            "the_odds_api", "the-odds-api", "theoddsapi",
            "odds_api", "odds-api",
            "sportsbook", "sportsbook_verified", "sportsbook_real",
            "prop-line", "propline", "prop_line",
            "draftkings", "fanduel", "betmgm", "caesars", "espn",
        }
    ):
        r = extract_line_with_provenance(
            market, selection, structured_line=existing_line,
        )
        candidate["line"] = r["line"]
        candidate["line_source"] = "sportsbook_structured"
        if not existing_side:
            candidate["side"] = r["side"]
        return candidate

    # Rule 3: deterministic text parse fallback.
    r = extract_line_with_provenance(market, selection)
    if r["line"] is not None:
        candidate["line"] = r["line"]
        candidate["line_source"] = "selection_parse_fallback"
        if not existing_side:
            candidate["side"] = r["side"]
    else:
        # Unrecoverable — leave line as-is (may be a legacy numeric
        # value producers set without a source tag); only set
        # line_source when we can prove one.
        if existing_line is None:
            candidate["line"] = None
            candidate["line_source"] = None
        if not existing_side:
            candidate["side"] = r["side"]
    return candidate


def attach_line_fields_batch(candidates: Iterable[dict]) -> list[dict]:
    """Attach line/side/line_source to every candidate in a batch.

    Idempotent — safe to call multiple times.
    """
    out: list[dict] = []
    for c in candidates:
        attach_line_fields(c)
        out.append(c)
    return out


def dedupe_key_with_line(candidate: dict) -> tuple:
    """Return a dedupe key that INCLUDES the line — so two picks
    differing only by threshold are never collapsed.

    Producers/pipelines that dedupe candidates by
    (event, player, market_family) MUST use this key instead when
    the family carries a threshold (over/under, spread, alt-line).
    """
    return (
        candidate.get("event") or "",
        (candidate.get("player_name")
         or candidate.get("player")
         or candidate.get("selection")
         or "").strip().lower(),
        (candidate.get("market") or "").strip().lower(),
        candidate.get("side") or "",
        candidate.get("line"),          # None ≠ 0.5 ≠ 1.5 — preserved
        candidate.get("line_source") or "",
    )


__all__ = [
    "attach_line_fields",
    "attach_line_fields_batch",
    "dedupe_key_with_line",
]
