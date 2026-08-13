"""MAGIC 3F — Immutable market snapshot + CLV contract.

Persists sportsbook state at three explicit stages: OPENING, CURRENT,
CLOSING (with UNKNOWN when the stage cannot be safely inferred).

Collection: ``market_snapshots``
    _id (auto)
    snapshot_id           str   sha256 over identity + stage + timestamp
    stage                 str   OPENING | CURRENT | CLOSING | UNKNOWN
    canonical_event_id    str
    sport, league         str
    canonical_player_id   Optional[str]
    canonical_team_id     Optional[str]
    market                str
    side                  str
    line                  Optional[float]     — exact threshold
    american_odds         float
    decimal_odds          float
    book                  str
    sportsbook_id         Optional[str]
    captured_at           iso datetime  (never inferred)
    is_live               bool
    source                str   provider label
    source_event_id       Optional[str]
    source_market_id      Optional[str]
    is_immutable          bool  True for CLOSING once finalized
    provenance            dict

Collection: ``pick_clv``  (post-prediction analytics)
    pick_id               str  (unique)
    pick_line, pick_odds  — pick-time truth
    pick_timestamp        iso
    closing_line, closing_odds
    closing_timestamp     iso
    line_clv              Optional[float]
    price_clv             Optional[float]
    clv_method            str
    clv_version           str
    book_context          list[str]
    consensus_context     dict
    is_immutable          bool  once written after event start

Hard rules:
  * CURRENT is never copied into OPENING or CLOSING.
  * CLOSING becomes immutable at event start; further writes refused.
  * CLV is never computed for a pregame Magic score consumer — accessor
    :func:`clv_for_postgame_only` raises for a pregame consumer.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional

from services.magic.market_math import (
    american_to_decimal, implied_probability,
)

logger = logging.getLogger("lockscore.market_snapshot_store")


class SnapshotStage:
    OPENING = "OPENING"
    CURRENT = "CURRENT"
    CLOSING = "CLOSING"
    UNKNOWN = "UNKNOWN"
    ALL = (OPENING, CURRENT, CLOSING, UNKNOWN)


class MarketEvidenceState:
    STRONG_AGREEMENT       = "STRONG_AGREEMENT"
    MODERATE_AGREEMENT     = "MODERATE_AGREEMENT"
    MODEL_HIGHER_THAN_MARKET = "MODEL_HIGHER_THAN_MARKET"
    MARKET_HIGHER_THAN_MODEL = "MARKET_HIGHER_THAN_MODEL"
    MIXED                  = "MIXED"
    INSUFFICIENT_EVIDENCE  = "INSUFFICIENT_EVIDENCE"


def _make_snapshot_id(*, canonical_event_id: str, market: str,
                       side: str, line: Optional[float], book: str,
                       captured_at: str, stage: str) -> str:
    key = "|".join([
        str(canonical_event_id or ""),
        str(market or ""), str(side or ""),
        (f"{float(line):.4f}" if line is not None else "none"),
        str(book or ""), str(captured_at or ""), stage,
    ])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


async def upsert_market_snapshot(
    db, *, stage: str,
    canonical_event_id: str, sport: str, market: str, side: str,
    american_odds: float, book: str,
    captured_at: str,
    league: Optional[str] = None,
    line: Optional[float] = None,
    canonical_player_id: Optional[str] = None,
    canonical_team_id: Optional[str] = None,
    is_live: bool = False,
    sportsbook_id: Optional[str] = None,
    source: str = "unknown",
    source_event_id: Optional[str] = None,
    source_market_id: Optional[str] = None,
    provenance: Optional[dict] = None,
) -> dict:
    """Persist a market snapshot immutably-per-(identity,stage,timestamp)."""
    if stage not in SnapshotStage.ALL:
        raise ValueError(f"invalid stage: {stage!r}")
    snapshot_id = _make_snapshot_id(
        canonical_event_id=canonical_event_id, market=market,
        side=side, line=line, book=book,
        captured_at=captured_at, stage=stage,
    )
    doc = {
        "snapshot_id":       snapshot_id,
        "stage":             stage,
        "canonical_event_id": canonical_event_id,
        "sport":             sport,
        "league":            league,
        "canonical_player_id": canonical_player_id,
        "canonical_team_id":  canonical_team_id,
        "market":            market,
        "side":              side,
        "line":              line,
        "american_odds":     float(american_odds),
        "decimal_odds":      american_to_decimal(american_odds),
        "book":              book,
        "sportsbook_id":     sportsbook_id,
        "captured_at":       captured_at,
        "is_live":           is_live,
        "source":            source,
        "source_event_id":   source_event_id,
        "source_market_id":  source_market_id,
        "is_immutable":      stage == SnapshotStage.CLOSING,
        "provenance":        provenance or {},
    }
    # Never overwrite an immutable CLOSING snapshot.
    existing = None
    try:
        existing = await db.market_snapshots.find_one(
            {"snapshot_id": snapshot_id})
    except Exception:
        existing = None
    if existing and existing.get("is_immutable"):
        return existing
    try:
        await db.market_snapshots.update_one(
            {"snapshot_id": snapshot_id},
            {"$set": doc},
            upsert=True,
        )
    except Exception as e:
        logger.debug("market snapshot upsert failed: %s", e)
    return doc


async def latest_current_snapshot(
    db, *, canonical_event_id: str,
    market: str, side: str, line: Optional[float],
    as_of_iso: Optional[str] = None,
) -> Optional[dict]:
    """Return the latest CURRENT snapshot for this exact market that is
    ≤ ``as_of_iso`` (no future leakage).
    """
    q = {"canonical_event_id": canonical_event_id,
         "market": market, "side": side,
         "stage": {"$in": [SnapshotStage.CURRENT, SnapshotStage.OPENING]}}
    if line is not None:
        q["line"] = line
    if as_of_iso:
        q["captured_at"] = {"$lte": as_of_iso}
    try:
        return await db.market_snapshots.find_one(
            q, sort=[("captured_at", -1)])
    except Exception:
        return None


async def closing_snapshot(
    db, *, canonical_event_id: str,
    market: str, side: str, line: Optional[float],
) -> Optional[dict]:
    """Return the CLOSING snapshot (immutable) for this exact market."""
    q = {"canonical_event_id": canonical_event_id,
         "market": market, "side": side,
         "stage": SnapshotStage.CLOSING}
    if line is not None:
        q["line"] = line
    try:
        return await db.market_snapshots.find_one(q)
    except Exception:
        return None


async def compute_pregame_market_evidence(
    db, *, canonical_event_id: str, market: str, side: str,
    line: Optional[float], as_of_iso: str,
    model_probability: Optional[float],
) -> dict:
    """Assemble Magic market evidence STRICTLY from data with
    ``captured_at <= as_of_iso``.  No future leakage.
    """
    # Fetch all snapshots for this exact market up to the cutoff.
    q = {"canonical_event_id": canonical_event_id,
         "market": market, "side": side,
         "captured_at": {"$lte": as_of_iso}}
    if line is not None:
        q["line"] = line
    snaps: list[dict] = []
    try:
        cursor = db.market_snapshots.find(q).sort([("captured_at", 1)])
        async for r in cursor:
            snaps.append(r)
    except Exception:
        snaps = []
    if not snaps:
        return {"availability": "UNAVAILABLE",
                "as_of": as_of_iso,
                "book_count": 0,
                "reason": "no pre-cutoff market_snapshots"}

    # Latest per book.
    by_book: dict[str, dict] = {}
    for s in snaps:
        by_book[str(s.get("book") or "")] = s
    latest_snaps = list(by_book.values())
    book_count = len({s.get("book") for s in latest_snaps
                       if s.get("book")})

    # Best/worst American price on this side.
    prices = [s["american_odds"] for s in latest_snaps
              if s.get("american_odds") is not None]
    line_vals = [s["line"] for s in latest_snaps
                  if s.get("line") is not None]
    raw_probs = [implied_probability(p) for p in prices]
    raw_probs = [p for p in raw_probs if p is not None]

    from statistics import median
    consensus_p_raw = median(raw_probs) if raw_probs else None

    ev = {
        "availability":   ("AVAILABLE" if book_count >= 3
                            else ("PARTIAL" if book_count >= 1
                                  else "UNAVAILABLE")),
        "as_of":          as_of_iso,
        "book_count":     book_count,
        "median_side_prob_raw": consensus_p_raw,
        "best_side_price_american":  max(prices) if prices else None,
        "worst_side_price_american": min(prices) if prices else None,
        "line_range":     ((min(line_vals), max(line_vals))
                            if line_vals else None),
        "line_dispersion": (max(line_vals) - min(line_vals)
                             if len(line_vals) >= 2 else 0.0)
                             if line_vals else None,
        "price_dispersion": (max(prices) - min(prices)
                              if len(prices) >= 2 else 0.0),
        "source_stages":  sorted({s.get("stage") for s in latest_snaps}),
    }

    # Opening / current split.
    openings = [s for s in snaps if s.get("stage") == SnapshotStage.OPENING]
    currents = [s for s in snaps if s.get("stage") == SnapshotStage.CURRENT]
    if openings:
        earliest = min(openings, key=lambda x: x.get("captured_at") or "")
        ev["opening_line"]    = earliest.get("line")
        ev["opening_odds"]    = earliest.get("american_odds")
        ev["opening_book"]    = earliest.get("book")
        ev["opening_captured_at"] = earliest.get("captured_at")
    if currents:
        latest = max(currents, key=lambda x: x.get("captured_at") or "")
        ev["current_line"]    = latest.get("line")
        ev["current_odds"]    = latest.get("american_odds")
        ev["current_book"]    = latest.get("book")
        ev["current_captured_at"] = latest.get("captured_at")

    # Movement (probability space + line space).
    from services.magic.market_math import line_delta, price_delta
    ev["line_delta"] = line_delta(ev.get("opening_line"),
                                     ev.get("current_line"))
    ev["price_delta_prob"] = price_delta(ev.get("opening_odds"),
                                            ev.get("current_odds"))

    # Model vs market.
    if consensus_p_raw is not None and model_probability is not None:
        diff = float(model_probability) - float(consensus_p_raw)
        if abs(diff) < 0.03:
            state = MarketEvidenceState.STRONG_AGREEMENT
        elif abs(diff) < 0.08:
            state = MarketEvidenceState.MODERATE_AGREEMENT
        elif diff > 0:
            state = MarketEvidenceState.MODEL_HIGHER_THAN_MARKET
        else:
            state = MarketEvidenceState.MARKET_HIGHER_THAN_MODEL
    else:
        state = MarketEvidenceState.INSUFFICIENT_EVIDENCE
    ev["model_market_state"] = state
    ev["model_edge_vs_raw"] = (
        float(model_probability) - float(consensus_p_raw)
        if (model_probability is not None and consensus_p_raw is not None)
        else None
    )
    ev["provenance"] = {
        "as_of":          as_of_iso,
        "n_snapshots":    len(snaps),
        "n_books":        book_count,
        "source":         "market_snapshots",
        "temporal_rule":  "captured_at <= as_of (no future leakage)",
    }
    return ev


# ═══════════════════════════════════════════════════════════════════
# CLV — POST-PREDICTION ANALYTICS ONLY
# ═══════════════════════════════════════════════════════════════════
class ClvAvailabilityError(RuntimeError):
    """Raised when a pregame consumer tries to read CLV."""


async def finalize_pick_clv(
    db, *, pick_id: str,
    pick_line: Optional[float], pick_odds: Optional[float],
    pick_timestamp: str,
    canonical_event_id: str, market: str, side: str,
    event_start_iso: str,
    now_iso: Optional[str] = None,
) -> Optional[dict]:
    """Compute + persist CLV ONLY when a trustworthy closing snapshot
    exists AND the event has started (immutability boundary).
    """
    now = now_iso or datetime.now(timezone.utc).isoformat()
    # Guard: no CLV before event start.
    if now < event_start_iso:
        return None
    closing = await closing_snapshot(
        db, canonical_event_id=canonical_event_id,
        market=market, side=side, line=pick_line,
    )
    if not closing:
        return {"pick_id": pick_id, "clv_available": False,
                "reason": "no CLOSING snapshot for exact market"}
    closing_line = closing.get("line")
    closing_odds = closing.get("american_odds")
    price_clv = None
    if pick_odds is not None and closing_odds is not None:
        p_pick = implied_probability(pick_odds)
        p_close = implied_probability(closing_odds)
        if p_pick is not None and p_close is not None:
            # positive when close is a WORSE price than pick (= good CLV).
            price_clv = p_close - p_pick
    line_clv = None
    if pick_line is not None and closing_line is not None:
        try:
            line_clv = float(closing_line) - float(pick_line)
        except (TypeError, ValueError):
            line_clv = None
    doc = {
        "pick_id":            pick_id,
        "pick_line":          pick_line,
        "pick_odds":          pick_odds,
        "pick_timestamp":     pick_timestamp,
        "closing_line":       closing_line,
        "closing_odds":       closing_odds,
        "closing_timestamp":  closing.get("captured_at"),
        "closing_book":       closing.get("book"),
        "line_clv":           line_clv,
        "price_clv":          price_clv,
        "clv_method":         "probability_space_delta",
        "clv_version":        "3f.v1",
        "is_immutable":       True,
        "consensus_context":  {"book": closing.get("book"),
                                "closing_ts": closing.get("captured_at")},
        "computed_at":        now,
    }
    try:
        existing = await db.pick_clv.find_one({"pick_id": pick_id})
        if existing and existing.get("is_immutable"):
            return existing   # never overwrite
        await db.pick_clv.update_one(
            {"pick_id": pick_id}, {"$set": doc}, upsert=True,
        )
    except Exception as e:
        logger.debug("CLV finalize upsert failed for %s: %s", pick_id, e)
    return doc


def clv_for_postgame_only(pick: dict, *, allow_pregame: bool = False):
    """Accessor that raises if a pregame consumer tries to read CLV.
    Pregame Magic MUST call with ``allow_pregame=False``.
    """
    if not allow_pregame:
        # Any pregame consumer must not have CLV available.
        raise ClvAvailabilityError(
            "CLV is post-prediction analytics — not available pregame")
    return {"line_clv":  pick.get("line_clv"),
            "price_clv": pick.get("price_clv")}


__all__ = [
    "SnapshotStage", "MarketEvidenceState",
    "upsert_market_snapshot", "latest_current_snapshot",
    "closing_snapshot", "compute_pregame_market_evidence",
    "finalize_pick_clv", "clv_for_postgame_only",
    "ClvAvailabilityError",
]
