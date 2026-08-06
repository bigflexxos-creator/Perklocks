"""user_bet_ledger — Phase 3G Step 2 canonical wager ledger.

Introduces a **typed contract** and a **service module** that makes
``user_bets`` the canonical personal wager ledger for the app.

Step 2 scope (per user's Phase 3G Step 2 prompt)
────────────────────────────────────────────────
• Define typed contracts: :class:`UserBet`, :class:`UserBetLeg`,
  :class:`UserBetSettlementEvent`, :class:`UserBetCreateRequest`,
  :class:`UserBetResult`.
• Implement the ledger service (create / settle / void / cancel /
  list / lookup / legacy mapping / diagnostics).
• Provide a **pure** legacy mapping function for eligible ``p_*``
  rows in ``parlay_history`` that performs **zero** database writes.
• Provide an admin diagnostics helper that reports canonical counts,
  eligible-legacy counts, excluded ``plearn_*`` counts, status
  distribution, coverage of ``prediction_id`` / ``snapshot_id`` /
  ``clv_value`` / ``sportsbook`` — WITHOUT exposing individual user
  wager details.
• Provide an index-preflight helper that scans existing ``user_bets``
  for duplicates that would block the new unique indexes declared in
  the Phase 3C ``services/index_registry`` **without applying them**.

STRICT guardrails carried from Step 1 audit:
  • No permanent dual-write is enabled by this module.
  • No route is flipped by this module.
  • No index is created or dropped by this module.
  • ``plearn_*`` rows are hard-rejected from every path — the guard is
    enforced at multiple layers (ID prefix, presence of ``user_id``,
    absence of ``signature``).
  • ``parlay_history`` collection is never modified by this module.
  • The shared Phase 3B database lifecycle is used exclusively — this
    module MUST NOT construct its own ``AsyncIOMotorClient``.

Status vocabulary decisions from Step 2 prompt:
  • Canonical statuses: pending, won, lost, pushed, void,
    partially_settled, cancelled.
  • Legacy `live` → pending. Legacy `push` → pushed. Legacy `pushed`
    → pushed. Legacy `void` stays `void` (NEVER coerced to pushed).
  • Unknown statuses are preserved in ``original_status`` and mapped
    to ``canonical_status="unknown"`` — never silently interpreted.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Literal, Optional, Sequence

from motor.motor_asyncio import AsyncIOMotorDatabase

from services import database as _shared_db

logger = logging.getLogger("lockscore.user_bet_ledger")


# ═════════════════════════════════════════════════════════════════════
# Collection + version constants
# ═════════════════════════════════════════════════════════════════════
COLLECTION = "user_bets"

# migration_version bump whenever the canonical schema changes shape.
# Step 2 introduces the v1 canonical shape.
CANONICAL_MIGRATION_VERSION = 1


# ═════════════════════════════════════════════════════════════════════
# Canonical status vocabulary
# ═════════════════════════════════════════════════════════════════════
STATUS_PENDING             = "pending"
STATUS_WON                 = "won"
STATUS_LOST                = "lost"
STATUS_PUSHED              = "pushed"
STATUS_VOID                = "void"
STATUS_PARTIALLY_SETTLED   = "partially_settled"
STATUS_CANCELLED           = "cancelled"

CANONICAL_STATUSES = frozenset({
    STATUS_PENDING, STATUS_WON, STATUS_LOST, STATUS_PUSHED,
    STATUS_VOID, STATUS_PARTIALLY_SETTLED, STATUS_CANCELLED,
})
TERMINAL_STATUSES = frozenset({
    STATUS_WON, STATUS_LOST, STATUS_PUSHED, STATUS_VOID, STATUS_CANCELLED,
})

# Legacy → canonical mapping.
# Any status not on this map is preserved in ``original_status`` and
# canonical_status is set to "unknown".
LEGACY_STATUS_MAP: dict[str, str] = {
    "live":     STATUS_PENDING,
    "pending":  STATUS_PENDING,
    "won":      STATUS_WON,
    "lost":     STATUS_LOST,
    "push":     STATUS_PUSHED,
    "pushed":   STATUS_PUSHED,
    "void":     STATUS_VOID,          # NEVER map to pushed.
    "cancelled": STATUS_CANCELLED,
    "canceled":  STATUS_CANCELLED,    # US-spelling tolerance
    "partial":   STATUS_PARTIALLY_SETTLED,
    "partially_settled": STATUS_PARTIALLY_SETTLED,
}

STATUS_UNKNOWN = "unknown"


def map_legacy_status(legacy_status: Optional[str]) -> str:
    """Map a legacy status string to the canonical vocabulary.

    Rules:
      • None or empty → ``pending`` (default for un-settled).
      • Known legacy → mapped value.
      • Unknown → returns ``"unknown"`` (never silently interpreted).
    """
    if legacy_status is None or legacy_status == "":
        return STATUS_PENDING
    s = str(legacy_status).strip().lower()
    if s in LEGACY_STATUS_MAP:
        return LEGACY_STATUS_MAP[s]
    return STATUS_UNKNOWN


# ═════════════════════════════════════════════════════════════════════
# CLV vocabulary
# ═════════════════════════════════════════════════════════════════════
CLV_UNAVAILABLE = "unavailable"        # closing line not captured / not derivable
CLV_AVAILABLE   = "available"          # both opening + closing captured
CLV_PENDING     = "pending"            # closing line not yet snapshotted (game hasn't started)


# ═════════════════════════════════════════════════════════════════════
# Wager type
# ═════════════════════════════════════════════════════════════════════
WAGER_TYPE_STRAIGHT = "straight"
WAGER_TYPE_PARLAY   = "parlay"
CANONICAL_WAGER_TYPES = frozenset({WAGER_TYPE_STRAIGHT, WAGER_TYPE_PARLAY})


# ═════════════════════════════════════════════════════════════════════
# Errors
# ═════════════════════════════════════════════════════════════════════
class UserBetLedgerError(RuntimeError):
    pass


class LegacyRowNotEligible(UserBetLedgerError):
    """The provided legacy ``parlay_history`` row is not a user wager
    (``plearn_*`` rows, rows missing ``user_id``, learning-loop rows).
    """


class DuplicateIdempotencyError(UserBetLedgerError):
    """A wager already exists for the provided idempotency key /
    client_bet_id.  Callers should call
    :func:`get_or_create_by_idempotency` instead."""


# ═════════════════════════════════════════════════════════════════════
# Typed contracts (Step 2)
# ═════════════════════════════════════════════════════════════════════
@dataclass(slots=True)
class UserBetLeg:
    """Frozen leg snapshot inside a parlay wager.

    Lines and odds are captured at wager creation and MUST NOT be
    rewritten later with current market data.
    """
    leg_id:                Optional[str]      = None    # canonical unique id for this leg row
    prediction_id:         Optional[str]      = None    # → picks.id (canonical reference)
    snapshot_id:           Optional[str]      = None    # → prediction_snapshots._id
    market_contract_id:    Optional[str]      = None    # → identity_resolver.canonical_market_contract_id
    event_id:              Optional[str]      = None
    sport_key:             Optional[str]      = None
    participant_id:        Optional[str]      = None
    market:                Optional[str]      = None
    selection:             Optional[str]      = None
    side:                  Optional[str]      = None
    line:                  Optional[float]    = None    # frozen exact line at bet time
    original_odds:         Optional[int]      = None    # frozen American odds at bet time
    sportsbook:            Optional[str]      = None
    status:                str                = STATUS_PENDING
    original_status:       Optional[str]      = None
    actual_result:         Optional[str]      = None    # e.g. "over", "under", "won", etc.
    settled_at:            Optional[datetime] = None

    def to_document(self) -> dict[str, Any]:
        return {
            "leg_id":             self.leg_id,
            "prediction_id":      self.prediction_id,
            "snapshot_id":        self.snapshot_id,
            "market_contract_id": self.market_contract_id,
            "event_id":           self.event_id,
            "sport_key":          self.sport_key,
            "participant_id":     self.participant_id,
            "market":             self.market,
            "selection":          self.selection,
            "side":               self.side,
            "line":               self.line,
            "original_odds":      self.original_odds,
            "sportsbook":         self.sportsbook,
            "status":             self.status,
            "original_status":    self.original_status,
            "actual_result":      self.actual_result,
            "settled_at":         self.settled_at,
        }

    @classmethod
    def from_document(cls, doc: dict[str, Any]) -> "UserBetLeg":
        return cls(
            leg_id             = doc.get("leg_id"),
            prediction_id      = doc.get("prediction_id"),
            snapshot_id        = doc.get("snapshot_id"),
            market_contract_id = doc.get("market_contract_id"),
            event_id           = doc.get("event_id"),
            sport_key          = doc.get("sport_key"),
            participant_id     = doc.get("participant_id"),
            market             = doc.get("market"),
            selection          = doc.get("selection"),
            side               = doc.get("side"),
            line               = doc.get("line"),
            original_odds      = doc.get("original_odds"),
            sportsbook         = doc.get("sportsbook"),
            status             = doc.get("status") or STATUS_PENDING,
            original_status    = doc.get("original_status"),
            actual_result      = doc.get("actual_result"),
            settled_at         = doc.get("settled_at"),
        )


@dataclass(slots=True)
class UserBetSettlementEvent:
    """Immutable settlement/state-change entry appended to a bet.

    Used to build an audit trail on top of the current mutation-based
    row.  Persisted as a subdocument under ``settlement_events``.
    """
    event_id:      str
    event_kind:    Literal["settle", "void", "cancel", "leg_update"]
    at:            datetime
    prev_status:   Optional[str]
    new_status:    Optional[str]
    reason:        Optional[str] = None
    actor:         Optional[str] = None       # e.g. "settlement_engine", "admin:<user_id>"
    payload:       dict[str, Any] = field(default_factory=dict)

    def to_document(self) -> dict[str, Any]:
        return {
            "event_id":    self.event_id,
            "event_kind":  self.event_kind,
            "at":          self.at,
            "prev_status": self.prev_status,
            "new_status":  self.new_status,
            "reason":      self.reason,
            "actor":       self.actor,
            "payload":     dict(self.payload or {}),
        }


@dataclass(slots=True)
class UserBet:
    """Canonical user wager record (v1)."""
    # Primary identity
    user_bet_id:           str
    user_id:               str
    wager_type:            str                         # straight | parlay

    # Idempotency
    client_bet_id:         Optional[str]              = None
    idempotency_key:       Optional[str]              = None

    # Status
    status:                str                         = STATUS_PENDING
    original_status:       Optional[str]              = None      # verbatim from source

    # Money
    stake_amount:          Optional[float]            = None      # unit-of-account amount (nullable if unknown)
    stake_units:           Optional[float]            = None
    odds:                  Optional[int]              = None      # American odds (single-leg or combined)
    odds_format:           str                         = "american"
    combined_odds:         Optional[int]              = None
    potential_payout:      Optional[float]            = None
    actual_payout:         Optional[float]            = None
    profit_loss:           Optional[float]            = None

    # Book
    sportsbook:            Optional[str]              = None

    # Time
    placed_at:             Optional[datetime]         = None
    settled_at:            Optional[datetime]         = None
    created_at:            Optional[datetime]         = None
    updated_at:            Optional[datetime]         = None

    # Provenance
    source:                str                         = "user_track"
    migration_version:     int                         = CANONICAL_MIGRATION_VERSION
    migration_source:      Optional[str]              = None      # e.g. "parlay_history"
    migration_source_id:   Optional[str]              = None      # e.g. "p_abc12345"
    is_legacy:             bool                        = False

    # Discretionary metadata
    mode:                  Optional[str]              = None      # standard/advanced/high_risk/today
    tags:                  list[str]                   = field(default_factory=list)
    risk_tier:             Optional[str]              = None
    correlation_warning:   Optional[str]              = None
    notes:                 Optional[str]              = None

    # Reference IDs (single-leg / straight)
    prediction_id:         Optional[str]              = None
    snapshot_id:           Optional[str]              = None
    market_contract_id:    Optional[str]              = None
    board_version:         Optional[str]              = None
    event_id:              Optional[str]              = None
    sport_key:             Optional[str]              = None

    # Nullable future line-value fields (populated only when we have
    # frozen bet-time and closing-time market data).
    opening_line:          Optional[float]            = None
    opening_odds:          Optional[int]              = None
    closing_line:          Optional[float]            = None
    closing_odds:          Optional[int]              = None
    clv_value:             Optional[float]            = None
    clv_status:            str                         = CLV_UNAVAILABLE

    # Parlay legs (empty for straight bets)
    legs:                  list[UserBetLeg]           = field(default_factory=list)

    # Audit trail
    settlement_events:     list[UserBetSettlementEvent] = field(default_factory=list)

    def to_document(self) -> dict[str, Any]:
        return {
            "user_bet_id":         self.user_bet_id,
            "user_id":             self.user_id,
            "wager_type":          self.wager_type,
            "client_bet_id":       self.client_bet_id,
            "idempotency_key":     self.idempotency_key,
            "status":              self.status,
            "original_status":     self.original_status,
            "stake_amount":        self.stake_amount,
            "stake_units":         self.stake_units,
            "odds":                self.odds,
            "odds_format":         self.odds_format,
            "combined_odds":       self.combined_odds,
            "potential_payout":    self.potential_payout,
            "actual_payout":       self.actual_payout,
            "profit_loss":         self.profit_loss,
            "sportsbook":          self.sportsbook,
            "placed_at":           self.placed_at,
            "settled_at":          self.settled_at,
            "created_at":          self.created_at,
            "updated_at":          self.updated_at,
            "source":              self.source,
            "migration_version":   self.migration_version,
            "migration_source":    self.migration_source,
            "migration_source_id": self.migration_source_id,
            "is_legacy":           bool(self.is_legacy),
            "mode":                self.mode,
            "tags":                list(self.tags or []),
            "risk_tier":           self.risk_tier,
            "correlation_warning": self.correlation_warning,
            "notes":               self.notes,
            "prediction_id":       self.prediction_id,
            "snapshot_id":         self.snapshot_id,
            "market_contract_id":  self.market_contract_id,
            "board_version":       self.board_version,
            "event_id":            self.event_id,
            "sport_key":           self.sport_key,
            "opening_line":        self.opening_line,
            "opening_odds":        self.opening_odds,
            "closing_line":        self.closing_line,
            "closing_odds":        self.closing_odds,
            "clv_value":           self.clv_value,
            "clv_status":          self.clv_status,
            "legs":                [L.to_document() for L in self.legs],
            "settlement_events":   [e.to_document() for e in self.settlement_events],
        }

    @classmethod
    def from_document(cls, doc: dict[str, Any]) -> "UserBet":
        legs_docs = doc.get("legs") or []
        legs = [UserBetLeg.from_document(L) for L in legs_docs if isinstance(L, dict)]
        evt_docs = doc.get("settlement_events") or []
        events = [
            UserBetSettlementEvent(
                event_id=e.get("event_id") or "",
                event_kind=e.get("event_kind") or "settle",
                at=e.get("at") or _now_utc(),
                prev_status=e.get("prev_status"),
                new_status=e.get("new_status"),
                reason=e.get("reason"),
                actor=e.get("actor"),
                payload=e.get("payload") or {},
            )
            for e in evt_docs if isinstance(e, dict)
        ]
        return cls(
            user_bet_id         = doc.get("user_bet_id") or doc.get("id") or "",
            user_id             = doc.get("user_id") or "",
            wager_type          = doc.get("wager_type") or (
                WAGER_TYPE_PARLAY if legs else WAGER_TYPE_STRAIGHT
            ),
            client_bet_id       = doc.get("client_bet_id"),
            idempotency_key     = doc.get("idempotency_key"),
            status              = doc.get("status") or STATUS_PENDING,
            original_status     = doc.get("original_status"),
            stake_amount        = doc.get("stake_amount"),
            stake_units         = doc.get("stake_units"),
            odds                = doc.get("odds") if doc.get("odds") is not None else doc.get("odds_at_bet"),
            odds_format         = doc.get("odds_format") or "american",
            combined_odds       = doc.get("combined_odds"),
            potential_payout    = doc.get("potential_payout"),
            actual_payout       = doc.get("actual_payout"),
            profit_loss         = doc.get("profit_loss") if doc.get("profit_loss") is not None else doc.get("pnl_units"),
            sportsbook          = doc.get("sportsbook"),
            placed_at           = doc.get("placed_at") or doc.get("created_at"),
            settled_at          = doc.get("settled_at"),
            created_at          = doc.get("created_at"),
            updated_at          = doc.get("updated_at"),
            source              = doc.get("source") or "user_track",
            migration_version   = int(doc.get("migration_version") or CANONICAL_MIGRATION_VERSION),
            migration_source    = doc.get("migration_source"),
            migration_source_id = doc.get("migration_source_id"),
            is_legacy           = bool(doc.get("is_legacy") or False),
            mode                = doc.get("mode"),
            tags                = list(doc.get("tags") or []),
            risk_tier           = doc.get("risk_tier"),
            correlation_warning = doc.get("correlation_warning"),
            notes               = doc.get("notes"),
            prediction_id       = doc.get("prediction_id"),
            snapshot_id         = doc.get("snapshot_id"),
            market_contract_id  = doc.get("market_contract_id"),
            board_version       = doc.get("board_version"),
            event_id            = doc.get("event_id"),
            sport_key           = doc.get("sport_key"),
            opening_line        = doc.get("opening_line"),
            opening_odds        = doc.get("opening_odds"),
            closing_line        = doc.get("closing_line"),
            closing_odds        = doc.get("closing_odds"),
            clv_value           = doc.get("clv_value"),
            clv_status          = doc.get("clv_status") or CLV_UNAVAILABLE,
            legs                = legs,
            settlement_events   = events,
        )


@dataclass(slots=True)
class UserBetCreateRequest:
    """Input contract for :func:`create_bet` / :func:`create_parlay`."""
    user_id:               str
    wager_type:            str                         # straight | parlay
    stake_amount:          Optional[float]            = None
    stake_units:           Optional[float]            = None
    odds:                  Optional[int]              = None
    combined_odds:         Optional[int]              = None
    sportsbook:            Optional[str]              = None
    placed_at:             Optional[datetime]         = None
    client_bet_id:         Optional[str]              = None
    idempotency_key:       Optional[str]              = None
    source:                str                         = "user_track"
    mode:                  Optional[str]              = None
    tags:                  list[str]                   = field(default_factory=list)
    risk_tier:             Optional[str]              = None
    correlation_warning:   Optional[str]              = None
    notes:                 Optional[str]              = None
    prediction_id:         Optional[str]              = None
    snapshot_id:           Optional[str]              = None
    market_contract_id:    Optional[str]              = None
    board_version:         Optional[str]              = None
    event_id:              Optional[str]              = None
    sport_key:             Optional[str]              = None
    opening_line:          Optional[float]            = None
    opening_odds:          Optional[int]              = None
    legs:                  list[UserBetLeg]           = field(default_factory=list)


@dataclass(slots=True)
class UserBetResult:
    """Return contract for creation / settlement operations."""
    bet:                   UserBet
    created:               bool                        # True on first insert, False when returning an idempotent match
    idempotency_key_used:  Optional[str]              = None
    warnings:              list[str]                   = field(default_factory=list)


# ═════════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════════
def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _to_utc_dt(value: Any) -> Optional[datetime]:
    """Normalize an ISO 8601 string or datetime to UTC-aware datetime.
    Returns ``None`` if the value cannot be parsed.  Never invents a
    timestamp — only converts values that were already provided.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        try:
            s = value.strip()
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None
    return None


def _norm_leg_key(leg: UserBetLeg | dict[str, Any]) -> str:
    """Build a stable identity key for a leg for idempotency purposes.

    Prefers the strongest available signal: market_contract_id →
    (prediction_id + line) → (event_id + market + selection + line).
    Falls back to prediction_id alone for very legacy rows.

    Uses NO display strings (player names, selection text) as the
    primary key.  Display strings are only tie-breakers within an
    already stable event_id + market_contract_id envelope.
    """
    if isinstance(leg, UserBetLeg):
        mc = leg.market_contract_id
        pid = leg.prediction_id
        ev = leg.event_id
        mk = leg.market
        sd = leg.side or leg.selection
        ln = leg.line
    else:
        mc = leg.get("market_contract_id")
        pid = leg.get("prediction_id")
        ev = leg.get("event_id")
        mk = leg.get("market")
        sd = leg.get("side") or leg.get("selection")
        ln = leg.get("line")
    if mc:
        return f"mc::{mc}"
    if pid and ln is not None:
        return f"pid::{pid}::ln::{ln}"
    if pid:
        return f"pid::{pid}"
    if ev and mk:
        return f"ev::{ev}::mk::{mk}::sd::{sd}::ln::{ln}"
    return "unknown"


def compute_idempotency_key(req: UserBetCreateRequest) -> str:
    """Deterministic idempotency key from stable normalized fields.

    Never uses player display names or selection text alone.  See
    Phase 3G Step 2 idempotency rules.
    """
    if req.client_bet_id:
        return f"cb::{req.user_id}::{req.client_bet_id}"

    parts: list[str] = [
        f"u::{req.user_id}",
        f"wt::{req.wager_type}",
    ]
    if req.legs:
        leg_keys = sorted(_norm_leg_key(L) for L in req.legs)
        parts.append("legs::" + "|".join(leg_keys))
    elif req.prediction_id is not None:
        # Straight bet — bind to prediction + line + odds.
        parts.append(f"pid::{req.prediction_id}")
        # Include the exact bet-time odds so re-tracking after a line
        # move produces a distinct wager (as required by test #14 & #16).
        parts.append(f"odds::{req.odds}")
    if req.sportsbook:
        parts.append(f"sb::{req.sportsbook.lower()}")
    if req.placed_at is not None:
        # Bucket to the minute to avoid microsecond drift creating dupes.
        p = _to_utc_dt(req.placed_at)
        if p is not None:
            parts.append(f"pa::{p.strftime('%Y-%m-%dT%H:%MZ')}")

    raw = "|".join(parts)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return f"idk::{digest}"


def _american_profit_per_unit(odds: Optional[int], stake: Optional[float]) -> float:
    """Signed profit (not return) for one settled unit at American odds.

    Won: +profit.  Lost: -stake.  Push/void/cancelled: 0.
    """
    if odds is None or stake is None:
        return 0.0
    try:
        o = float(odds)
        s = float(stake)
    except (TypeError, ValueError):
        return 0.0
    if o >= 100:
        return s * (o / 100.0)
    if o <= -100:
        return s * (100.0 / (-o))
    return 0.0


# ═════════════════════════════════════════════════════════════════════
# Database handle helper (Phase 3B lifecycle)
# ═════════════════════════════════════════════════════════════════════
def _resolve_db(db: Optional[AsyncIOMotorDatabase]) -> AsyncIOMotorDatabase:
    if db is not None:
        return db
    return _shared_db.get_database()


# ═════════════════════════════════════════════════════════════════════
# Legacy eligibility (Step 2, hardened plearn_* exclusion)
# ═════════════════════════════════════════════════════════════════════
def is_learning_row(doc: dict[str, Any]) -> bool:
    """Return True if this ``parlay_history`` row is a learning-loop
    (``plearn_*``) row and therefore MUST NEVER be treated as a user
    wager.  Multi-signal detection (defence in depth).
    """
    if not isinstance(doc, dict):
        return True   # anything not a dict is not a user wager
    id_ = doc.get("id") or ""
    if isinstance(id_, str) and id_.startswith("plearn_"):
        return True
    # Learning rows use a `signature` field which user-saved rows do
    # NOT have.  Additionally, learning rows carry a `shown_at`
    # timestamp field that user-saved rows never set.
    if doc.get("signature") is not None and (doc.get("user_id") in (None, "", 0)):
        return True
    if doc.get("shown_at") is not None and (doc.get("user_id") in (None, "", 0)):
        return True
    # ranking_snapshot / correlation_snapshot are exclusively populated
    # by ``parlay_learning.record_parlay_shown``.
    if doc.get("ranking_snapshot") or doc.get("correlation_snapshot"):
        if doc.get("user_id") in (None, "", 0):
            return True
    return False


def is_eligible_legacy_user_parlay(doc: dict[str, Any]) -> bool:
    """Return True if this ``parlay_history`` row represents a real
    user wager and is eligible for migration to ``user_bets``.

    Strict eligibility:
      • Must be a dict.
      • Must NOT be a learning-loop row (:func:`is_learning_row`).
      • Must have a truthy ``user_id``.
      • ``id`` must start with ``p_`` (user-saved format).
      • Must have a recognizable wager structure (``leg_ids`` list with
        at least 2 entries OR a legs list with at least 2 entries).
    """
    if not isinstance(doc, dict):
        return False
    if is_learning_row(doc):
        return False
    uid = doc.get("user_id")
    if uid in (None, "", 0):
        return False
    if not isinstance(uid, str):
        return False
    id_ = doc.get("id") or ""
    if not (isinstance(id_, str) and id_.startswith("p_")):
        return False
    leg_ids = doc.get("leg_ids") or []
    legs = doc.get("legs") or []
    if len(leg_ids) < 2 and len(legs) < 2:
        return False
    return True


# ═════════════════════════════════════════════════════════════════════
# Pure legacy mapper — ZERO database writes
# ═════════════════════════════════════════════════════════════════════
def map_legacy_user_parlay(
    legacy_doc: dict[str, Any],
    *,
    override_created_at: Optional[datetime] = None,
) -> UserBet:
    """Pure mapping function: legacy ``parlay_history`` p_* doc → canonical
    :class:`UserBet`.

    Zero database writes.  Zero side effects.  Raises
    :class:`LegacyRowNotEligible` if the input is not an eligible user
    wager.  Missing fields become ``None`` (never invented).
    """
    if not is_eligible_legacy_user_parlay(legacy_doc):
        raise LegacyRowNotEligible(
            f"row id={legacy_doc.get('id')!r} is not an eligible user wager "
            f"(user_id={legacy_doc.get('user_id')!r}, "
            f"learning_row={is_learning_row(legacy_doc)})"
        )

    legacy_id = str(legacy_doc.get("id"))
    original_status = legacy_doc.get("status")
    canonical_status = map_legacy_status(original_status)

    stake = legacy_doc.get("stake")
    stake_f: Optional[float] = None
    if stake is not None:
        try:
            stake_f = float(stake)
        except (TypeError, ValueError):
            stake_f = None

    combined_odds_raw = legacy_doc.get("combined_odds")
    combined_odds: Optional[int] = None
    if combined_odds_raw is not None:
        try:
            combined_odds = int(combined_odds_raw)
        except (TypeError, ValueError):
            combined_odds = None

    payout_raw = legacy_doc.get("payout")
    payout: Optional[float] = None
    if payout_raw is not None:
        try:
            payout = float(payout_raw)
        except (TypeError, ValueError):
            payout = None

    profit_loss: Optional[float] = None
    if canonical_status == STATUS_WON:
        profit_loss = payout if payout is not None else _american_profit_per_unit(combined_odds, stake_f)
    elif canonical_status == STATUS_LOST:
        profit_loss = -stake_f if stake_f is not None else None
    elif canonical_status in (STATUS_PUSHED, STATUS_VOID, STATUS_CANCELLED):
        profit_loss = 0.0
    else:
        profit_loss = None  # unknown, pending, partially_settled: leave null

    placed_at = _to_utc_dt(legacy_doc.get("created_at"))
    settled_at = _to_utc_dt(legacy_doc.get("settled_at"))
    created_at = override_created_at or placed_at

    # Legs — preserve verbatim under the canonical shape.  Do NOT
    # invent identity fields; only carry over what the source row has.
    legs: list[UserBetLeg] = []
    for L in (legacy_doc.get("legs") or []):
        if not isinstance(L, dict):
            continue
        legs.append(UserBetLeg(
            leg_id             = None,   # no per-leg canonical id in the legacy record
            prediction_id      = L.get("pick_id"),
            snapshot_id        = None,
            market_contract_id = None,
            event_id           = None,
            sport_key          = L.get("sport"),
            participant_id     = None,
            market             = L.get("market"),
            selection          = L.get("selection"),
            side               = L.get("selection"),   # legacy has no explicit side
            line               = None,                  # no line field on legacy leg
            original_odds      = int(L.get("book_odds")) if L.get("book_odds") is not None else None,
            sportsbook         = None,
            status             = map_legacy_status(L.get("status")),
            original_status    = L.get("status"),
            actual_result      = None,
            settled_at         = None,
        ))
    # If the top-level ``leg_ids`` is longer than ``legs`` (e.g. snapshot
    # trimmed), keep the entries we have — we NEVER manufacture missing
    # legs.  The audit doc §11 flags this row for manual review.

    # Assemble the canonical wager.  user_bet_id is NEW (UUID); the
    # legacy id is preserved under ``migration_source_id`` per the
    # Step 2 idempotency plan.
    return UserBet(
        user_bet_id         = str(uuid.uuid4()),
        user_id             = str(legacy_doc.get("user_id")),
        wager_type          = WAGER_TYPE_PARLAY,
        client_bet_id       = None,
        idempotency_key     = None,      # backfill dedupes via migration_source_id
        status              = canonical_status,
        original_status     = original_status,
        stake_amount        = stake_f,
        stake_units         = stake_f,
        odds                = combined_odds,
        odds_format         = "american",
        combined_odds       = combined_odds,
        potential_payout    = None,      # not stored in legacy
        actual_payout       = payout,    # legacy stored profit-per-unit here
        profit_loss         = profit_loss,
        sportsbook          = None,      # not stored in legacy
        placed_at           = placed_at,
        settled_at          = settled_at,
        created_at          = created_at,
        updated_at          = _now_utc(),
        source              = "backfill_p",
        migration_version   = CANONICAL_MIGRATION_VERSION,
        migration_source    = "parlay_history",
        migration_source_id = legacy_id,
        is_legacy           = True,
        mode                = legacy_doc.get("mode"),
        tags                = [],
        risk_tier           = None,
        correlation_warning = None,
        notes               = None,
        prediction_id       = None,
        snapshot_id         = None,
        market_contract_id  = None,
        board_version       = None,
        event_id            = None,
        sport_key           = None,
        opening_line        = None,
        opening_odds        = None,
        closing_line        = None,
        closing_odds        = None,
        clv_value           = None,
        clv_status          = CLV_UNAVAILABLE,
        legs                = legs,
        settlement_events   = [],
    )


# ═════════════════════════════════════════════════════════════════════
# Ledger API — create / settle / void / cancel / lookup / list
# ═════════════════════════════════════════════════════════════════════
async def _find_by_idempotency(
    coll,
    *,
    user_id: str,
    client_bet_id: Optional[str],
    idempotency_key: Optional[str],
) -> Optional[dict[str, Any]]:
    """Look up an existing wager by (user_id + client_bet_id) or
    (user_id + idempotency_key).  Client-scoped so two users can share
    the same client_bet_id value without collision."""
    if client_bet_id:
        doc = await coll.find_one(
            {"user_id": user_id, "client_bet_id": client_bet_id},
        )
        if doc:
            return doc
    if idempotency_key:
        doc = await coll.find_one(
            {"user_id": user_id, "idempotency_key": idempotency_key},
        )
        if doc:
            return doc
    return None


async def create_bet(
    req: UserBetCreateRequest,
    *,
    db: Optional[AsyncIOMotorDatabase] = None,
) -> UserBetResult:
    """Idempotently insert a wager.  Works for both straight and parlay.

    Idempotency:
      1. If ``client_bet_id`` is provided, `(user_id, client_bet_id)` is
         the primary key.  Second call with the same values returns the
         existing row.
      2. Else, ``idempotency_key`` is computed from stable normalized
         fields.
      3. Different exact odds / lines / sportsbooks are DISTINCT wagers.
      4. Never dedupes by display text alone.
    """
    if req.user_id in (None, ""):
        raise UserBetLedgerError("user_id is required")
    if req.wager_type not in CANONICAL_WAGER_TYPES:
        raise UserBetLedgerError(
            f"unknown wager_type {req.wager_type!r} — must be one of {sorted(CANONICAL_WAGER_TYPES)}"
        )
    if req.wager_type == WAGER_TYPE_PARLAY and len(req.legs) < 2:
        raise UserBetLedgerError("parlay wagers require at least 2 legs")
    if req.wager_type == WAGER_TYPE_STRAIGHT and (len(req.legs) > 0 or req.prediction_id is None):
        # Straight bet needs prediction_id and NO leg list.
        if req.prediction_id is None:
            raise UserBetLedgerError("straight wagers require prediction_id")
        if len(req.legs) > 0:
            raise UserBetLedgerError("straight wagers must not have legs")

    idem = req.idempotency_key or compute_idempotency_key(req)

    coll = _resolve_db(db)[COLLECTION]

    existing = await _find_by_idempotency(
        coll,
        user_id=req.user_id,
        client_bet_id=req.client_bet_id,
        idempotency_key=idem,
    )
    if existing:
        bet = UserBet.from_document(existing)
        return UserBetResult(bet=bet, created=False, idempotency_key_used=idem,
                             warnings=["idempotent match — returning existing wager"])

    now = _now_utc()
    placed = req.placed_at or now
    bet = UserBet(
        user_bet_id         = str(uuid.uuid4()),
        user_id             = str(req.user_id),
        wager_type          = req.wager_type,
        client_bet_id       = req.client_bet_id,
        idempotency_key     = idem,
        status              = STATUS_PENDING,
        original_status     = None,
        stake_amount        = req.stake_amount,
        stake_units         = req.stake_units if req.stake_units is not None else req.stake_amount,
        odds                = req.odds,
        odds_format         = "american",
        combined_odds       = req.combined_odds,
        potential_payout    = None,
        actual_payout       = None,
        profit_loss         = None,
        sportsbook          = req.sportsbook,
        placed_at           = placed,
        settled_at          = None,
        created_at          = now,
        updated_at          = now,
        source              = req.source or "user_track",
        migration_version   = CANONICAL_MIGRATION_VERSION,
        migration_source    = None,
        migration_source_id = None,
        is_legacy           = False,
        mode                = req.mode,
        tags                = list(req.tags or []),
        risk_tier           = req.risk_tier,
        correlation_warning = req.correlation_warning,
        notes               = req.notes,
        prediction_id       = req.prediction_id,
        snapshot_id         = req.snapshot_id,
        market_contract_id  = req.market_contract_id,
        board_version       = req.board_version,
        event_id            = req.event_id,
        sport_key           = req.sport_key,
        opening_line        = req.opening_line,
        opening_odds        = req.opening_odds,
        closing_line        = None,
        closing_odds        = None,
        clv_value           = None,
        clv_status          = CLV_UNAVAILABLE,
        legs                = list(req.legs or []),
        settlement_events   = [],
    )

    try:
        await coll.insert_one(bet.to_document())
    except Exception as e:
        # Race — someone else inserted the idempotent row between our
        # find and our insert.  Retry the find; if still nothing, re-raise.
        again = await _find_by_idempotency(
            coll,
            user_id=req.user_id,
            client_bet_id=req.client_bet_id,
            idempotency_key=idem,
        )
        if again:
            return UserBetResult(
                bet=UserBet.from_document(again),
                created=False, idempotency_key_used=idem,
                warnings=[f"race-lost — returning existing wager: {e}"],
            )
        raise
    return UserBetResult(bet=bet, created=True, idempotency_key_used=idem)


async def create_parlay(
    req: UserBetCreateRequest,
    *,
    db: Optional[AsyncIOMotorDatabase] = None,
) -> UserBetResult:
    """Explicit parlay creator — validates wager_type + legs before delegating."""
    if req.wager_type != WAGER_TYPE_PARLAY:
        raise UserBetLedgerError("create_parlay requires wager_type='parlay'")
    return await create_bet(req, db=db)


async def get_or_create_by_idempotency(
    req: UserBetCreateRequest,
    *,
    db: Optional[AsyncIOMotorDatabase] = None,
) -> UserBetResult:
    """Alias for :func:`create_bet` — highlights idempotent semantics
    for callers that want the intent to be explicit."""
    return await create_bet(req, db=db)


async def get_bet(
    user_bet_id: str,
    *,
    db: Optional[AsyncIOMotorDatabase] = None,
) -> Optional[UserBet]:
    coll = _resolve_db(db)[COLLECTION]
    doc = await coll.find_one({"user_bet_id": user_bet_id})
    if doc is None:
        # Backwards-compat: legacy user_bets rows use "id" not "user_bet_id".
        doc = await coll.find_one({"id": user_bet_id})
    return UserBet.from_document(doc) if doc else None


async def list_bets_for_user(
    user_id: str,
    *,
    db: Optional[AsyncIOMotorDatabase] = None,
    status: Optional[str] = None,
    wager_type: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> list[UserBet]:
    coll = _resolve_db(db)[COLLECTION]
    q: dict[str, Any] = {"user_id": user_id}
    if status:
        q["status"] = status
    if wager_type:
        q["wager_type"] = wager_type
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    cursor = coll.find(q).sort("placed_at", -1).skip(offset).limit(limit)
    docs = await cursor.to_list(limit)
    return [UserBet.from_document(d) for d in docs]


async def _append_settlement_event(
    coll,
    *,
    user_bet_id: str,
    event_kind: str,
    prev_status: Optional[str],
    new_status: Optional[str],
    reason: Optional[str],
    actor: Optional[str],
    payload: Optional[dict] = None,
) -> None:
    evt = UserBetSettlementEvent(
        event_id=str(uuid.uuid4()),
        event_kind=event_kind,           # type: ignore[arg-type]
        at=_now_utc(),
        prev_status=prev_status,
        new_status=new_status,
        reason=reason,
        actor=actor,
        payload=payload or {},
    )
    await coll.update_one(
        {"user_bet_id": user_bet_id},
        {"$push": {"settlement_events": evt.to_document()}},
    )


async def settle_bet(
    user_bet_id: str,
    *,
    status: str,
    profit_loss: Optional[float] = None,
    actual_payout: Optional[float] = None,
    settled_at: Optional[datetime] = None,
    actor: Optional[str] = None,
    reason: Optional[str] = None,
    db: Optional[AsyncIOMotorDatabase] = None,
) -> UserBetResult:
    """Settle a wager.  Idempotent if the target status is already
    terminal and matches the requested status.
    """
    if status not in CANONICAL_STATUSES:
        raise UserBetLedgerError(f"unknown canonical status {status!r}")
    coll = _resolve_db(db)[COLLECTION]
    doc = await coll.find_one({"user_bet_id": user_bet_id})
    if not doc:
        raise UserBetLedgerError(f"user_bet_id {user_bet_id!r} not found")
    prev = doc.get("status") or STATUS_PENDING
    if prev in TERMINAL_STATUSES and prev == status:
        return UserBetResult(bet=UserBet.from_document(doc), created=False,
                             warnings=["idempotent settle — already terminal"])
    settled_dt = settled_at or _now_utc()
    updates: dict[str, Any] = {
        "status":      status,
        "settled_at":  settled_dt,
        "updated_at":  _now_utc(),
    }
    if profit_loss is not None:
        updates["profit_loss"] = float(profit_loss)
    if actual_payout is not None:
        updates["actual_payout"] = float(actual_payout)
    await coll.update_one({"user_bet_id": user_bet_id}, {"$set": updates})
    await _append_settlement_event(
        coll,
        user_bet_id=user_bet_id,
        event_kind="settle",
        prev_status=prev,
        new_status=status,
        reason=reason,
        actor=actor,
        payload={"profit_loss": profit_loss, "actual_payout": actual_payout},
    )
    updated = await coll.find_one({"user_bet_id": user_bet_id})
    return UserBetResult(bet=UserBet.from_document(updated), created=False)


async def settle_leg(
    user_bet_id: str,
    leg_key: str,
    *,
    status: str,
    actual_result: Optional[str] = None,
    settled_at: Optional[datetime] = None,
    actor: Optional[str] = None,
    reason: Optional[str] = None,
    db: Optional[AsyncIOMotorDatabase] = None,
) -> UserBetResult:
    """Update one leg of a parlay.  ``leg_key`` may be either
    ``prediction_id`` or ``leg_id`` — both are matched.

    Does NOT roll-up the parent wager status; use :func:`settle_bet`
    with a computed status after the last leg settles.
    """
    if status not in CANONICAL_STATUSES:
        raise UserBetLedgerError(f"unknown canonical status {status!r}")
    coll = _resolve_db(db)[COLLECTION]
    doc = await coll.find_one({"user_bet_id": user_bet_id})
    if not doc:
        raise UserBetLedgerError(f"user_bet_id {user_bet_id!r} not found")
    legs = doc.get("legs") or []
    found_idx = -1
    for i, L in enumerate(legs):
        if not isinstance(L, dict):
            continue
        if L.get("leg_id") == leg_key or L.get("prediction_id") == leg_key:
            found_idx = i
            break
    if found_idx < 0:
        raise UserBetLedgerError(f"no leg matching {leg_key!r} on bet {user_bet_id!r}")
    prev_leg_status = legs[found_idx].get("status")
    legs[found_idx]["status"] = status
    legs[found_idx]["actual_result"] = actual_result
    legs[found_idx]["settled_at"] = settled_at or _now_utc()
    await coll.update_one(
        {"user_bet_id": user_bet_id},
        {"$set": {"legs": legs, "updated_at": _now_utc()}},
    )
    await _append_settlement_event(
        coll,
        user_bet_id=user_bet_id,
        event_kind="leg_update",
        prev_status=prev_leg_status,
        new_status=status,
        reason=reason,
        actor=actor,
        payload={"leg_key": leg_key, "actual_result": actual_result},
    )
    updated = await coll.find_one({"user_bet_id": user_bet_id})
    return UserBetResult(bet=UserBet.from_document(updated), created=False)


async def void_bet(
    user_bet_id: str,
    *,
    reason: Optional[str] = None,
    actor: Optional[str] = None,
    db: Optional[AsyncIOMotorDatabase] = None,
) -> UserBetResult:
    """Void a wager (invalidated / no-action).  Distinct from push."""
    return await settle_bet(
        user_bet_id, status=STATUS_VOID,
        profit_loss=0.0, actual_payout=0.0,
        actor=actor, reason=reason, db=db,
    )


async def cancel_bet(
    user_bet_id: str,
    *,
    reason: Optional[str] = None,
    actor: Optional[str] = None,
    db: Optional[AsyncIOMotorDatabase] = None,
) -> UserBetResult:
    """Cancel a wager pre-settlement.  Distinct from void."""
    coll = _resolve_db(db)[COLLECTION]
    doc = await coll.find_one({"user_bet_id": user_bet_id})
    if not doc:
        raise UserBetLedgerError(f"user_bet_id {user_bet_id!r} not found")
    prev = doc.get("status") or STATUS_PENDING
    if prev != STATUS_PENDING:
        raise UserBetLedgerError(
            f"cannot cancel bet {user_bet_id!r} — current status is {prev!r} (must be pending)"
        )
    now = _now_utc()
    await coll.update_one(
        {"user_bet_id": user_bet_id},
        {"$set": {"status": STATUS_CANCELLED, "settled_at": now, "updated_at": now,
                  "profit_loss": 0.0, "actual_payout": 0.0}},
    )
    await _append_settlement_event(
        coll,
        user_bet_id=user_bet_id,
        event_kind="cancel",
        prev_status=prev,
        new_status=STATUS_CANCELLED,
        reason=reason,
        actor=actor,
    )
    updated = await coll.find_one({"user_bet_id": user_bet_id})
    return UserBetResult(bet=UserBet.from_document(updated), created=False)


# ═════════════════════════════════════════════════════════════════════
# Index preflight — analysis-only, ZERO index creation
# ═════════════════════════════════════════════════════════════════════
@dataclass
class IndexPreflightReport:
    ok:                                bool
    checked_at:                        datetime
    total_user_bets:                   int
    duplicate_user_bet_id:             int
    duplicate_client_bet_id_per_user:  int
    duplicate_idempotency_key_per_user: int
    duplicate_migration_source_id:     int
    conflicts:                         list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok":                              self.ok,
            "checked_at":                      self.checked_at.isoformat(),
            "total_user_bets":                 self.total_user_bets,
            "duplicate_user_bet_id":           self.duplicate_user_bet_id,
            "duplicate_client_bet_id_per_user": self.duplicate_client_bet_id_per_user,
            "duplicate_idempotency_key_per_user": self.duplicate_idempotency_key_per_user,
            "duplicate_migration_source_id":   self.duplicate_migration_source_id,
            "conflicts":                       self.conflicts,
        }


async def preflight_unique_indexes(
    *,
    db: Optional[AsyncIOMotorDatabase] = None,
) -> IndexPreflightReport:
    """Scan the live ``user_bets`` collection for duplicates that would
    prevent the new unique / partial-unique indexes from being applied
    safely.

    **This function creates no indexes and deletes no data.**  It
    returns a report the operator can review before authorising an
    index-application step in a later phase.
    """
    coll = _resolve_db(db)[COLLECTION]
    total = await coll.count_documents({})
    conflicts: list[dict[str, Any]] = []

    async def _agg_count(field: str, extra_match: Optional[dict] = None,
                         scope: Optional[list[str]] = None) -> int:
        match: dict[str, Any] = {field: {"$exists": True, "$ne": None}}
        if extra_match:
            match.update(extra_match)
        group_id: dict[str, Any] = {field: f"${field}"}
        if scope:
            for s in scope:
                group_id[s] = f"${s}"
        pipeline = [
            {"$match": match},
            {"$group": {"_id": group_id, "n": {"$sum": 1}}},
            {"$match": {"n": {"$gt": 1}}},
            {"$count": "total"},
        ]
        rows = await coll.aggregate(pipeline).to_list(1)
        return int(rows[0]["total"]) if rows else 0

    dup_ubid = await _agg_count("user_bet_id")
    # Some very old rows may have used ``id`` — include for safety.
    if dup_ubid == 0:
        dup_ubid = await _agg_count("id")
    dup_client_bet_id = await _agg_count(
        "client_bet_id",
        extra_match={"client_bet_id": {"$exists": True, "$ne": None}},
        scope=["user_id"],
    )
    dup_idempotency = await _agg_count(
        "idempotency_key",
        extra_match={"idempotency_key": {"$exists": True, "$ne": None}},
        scope=["user_id"],
    )
    dup_migration = await _agg_count(
        "migration_source_id",
        extra_match={"migration_source_id": {"$exists": True, "$ne": None},
                     "migration_source":    {"$exists": True, "$ne": None}},
        scope=["migration_source"],
    )

    for label, n in (
        ("user_bet_id unique", dup_ubid),
        ("client_bet_id per user_id partial-unique", dup_client_bet_id),
        ("idempotency_key per user_id partial-unique", dup_idempotency),
        ("migration_source + migration_source_id partial-unique", dup_migration),
    ):
        if n > 0:
            conflicts.append({
                "index": label,
                "duplicate_groups": int(n),
                "recommendation": "block — do not create until manual review",
            })

    return IndexPreflightReport(
        ok=(len(conflicts) == 0),
        checked_at=_now_utc(),
        total_user_bets=int(total),
        duplicate_user_bet_id=int(dup_ubid),
        duplicate_client_bet_id_per_user=int(dup_client_bet_id),
        duplicate_idempotency_key_per_user=int(dup_idempotency),
        duplicate_migration_source_id=int(dup_migration),
        conflicts=conflicts,
    )


# ═════════════════════════════════════════════════════════════════════
# Admin diagnostics — aggregate stats, NO per-user detail
# ═════════════════════════════════════════════════════════════════════
async def safe_ledger_diagnostics(
    *,
    db: Optional[AsyncIOMotorDatabase] = None,
) -> dict[str, Any]:
    """Aggregate operational stats safe for admin surfaces.

    Guarantees:
      • No individual user's wager details are returned.
      • No secrets, no per-user identifiers, no per-row IDs.
      • ``plearn_*`` and non-user-wager rows in ``parlay_history`` are
        counted and reported as EXCLUDED, not merged into the canonical
        totals.
    """
    d = _resolve_db(db)
    ub = d[COLLECTION]
    ph = d["parlay_history"]

    total_canonical = await ub.count_documents({})

    # ─── parlay_history segmentation (legacy compatibility view) ─────
    ph_total = await ph.count_documents({})
    ph_learning = await ph.count_documents({"id": {"$regex": "^plearn_"}})
    ph_missing_uid = await ph.count_documents(
        {"$or": [{"user_id": {"$exists": False}}, {"user_id": None}, {"user_id": ""}]}
    )
    # Eligible p_* rows — count using the eligibility definition above
    # in the language Mongo can express.  Full check requires field
    # presence + at least 2 legs.
    ph_p_eligible_q = {
        "id":       {"$regex": "^p_"},
        "user_id":  {"$exists": True, "$ne": None, "$nin": [""]},
    }
    ph_p_eligible = await ph.count_documents(ph_p_eligible_q)

    # Status distribution on canonical
    status_rows = await ub.aggregate([
        {"$group": {"_id": "$status", "n": {"$sum": 1}}},
        {"$sort": {"_id": 1}},
    ]).to_list(None)
    status_distribution = {(r["_id"] or "null"): int(r["n"]) for r in status_rows}

    # Coverage
    async def _coverage(field: str) -> int:
        return await ub.count_documents({field: {"$exists": True, "$nin": [None, ""]}})

    snapshot_cov = await _coverage("snapshot_id")
    prediction_cov = await _coverage("prediction_id")
    clv_cov = await _coverage("clv_value")
    sportsbook_cov = await _coverage("sportsbook")

    # Duplicate idempotency candidates within canonical (same
    # (user_id, idempotency_key) with n>1).
    dup_candidates_pipeline = [
        {"$match": {
            "idempotency_key": {"$exists": True, "$ne": None},
            "user_id":         {"$exists": True, "$ne": None},
        }},
        {"$group": {"_id": {"u": "$user_id", "k": "$idempotency_key"},
                    "n": {"$sum": 1}}},
        {"$match": {"n": {"$gt": 1}}},
        {"$count": "total"},
    ]
    dup_rows = await ub.aggregate(dup_candidates_pipeline).to_list(1)
    dup_candidates = int(dup_rows[0]["total"]) if dup_rows else 0

    return {
        "canonical": {
            "collection":              COLLECTION,
            "total_user_bets":         int(total_canonical),
            "status_distribution":     status_distribution,
            "coverage": {
                "snapshot_id":         int(snapshot_cov),
                "prediction_id":       int(prediction_cov),
                "clv_value":           int(clv_cov),
                "sportsbook":          int(sportsbook_cov),
            },
            "duplicate_idempotency_candidates": dup_candidates,
        },
        "legacy_parlay_history": {
            "total_rows":              int(ph_total),
            "excluded_plearn_rows":    int(ph_learning),
            "rows_missing_user_id":    int(ph_missing_uid),
            "eligible_p_star_rows":    int(ph_p_eligible),
        },
        "generated_at":                _now_utc().isoformat(),
        "migration_version":           CANONICAL_MIGRATION_VERSION,
        "canonical_status_vocab":      sorted(CANONICAL_STATUSES),
    }


# ═════════════════════════════════════════════════════════════════════
# Legacy-compatible serializer (Step 7 reader cutover)
# ═════════════════════════════════════════════════════════════════════
def serialize_parlay_history_row(bet: "UserBet") -> dict[str, Any]:
    """Convert a canonical :class:`UserBet` parlay into the exact
    legacy ``parlay_history`` response shape used by
    ``GET /api/parlay/history``.  Byte-parity with the pre-Step-7
    envelope."""
    legs_out = []
    legs_won = legs_lost = legs_pending = 0
    for L in (bet.legs or []):
        st = (L.status or STATUS_PENDING).lower()
        if st == STATUS_WON: legs_won += 1
        elif st == STATUS_LOST: legs_lost += 1
        else: legs_pending += 1
        legs_out.append({
            "pick_id":   L.prediction_id,
            "sport":     L.sport_key,
            "event":     None,
            "market":    L.market,
            "selection": L.selection,
            "book_odds": L.original_odds,
            "status":    L.status or STATUS_PENDING,
        })
    return {
        "id":              bet.migration_source_id or bet.user_bet_id,
        "user_id":         bet.user_id,
        "created_at":      (bet.created_at.isoformat() if bet.created_at else None),
        "mode":            bet.mode,
        "leg_ids":         [L.prediction_id for L in (bet.legs or [])],
        "legs":            legs_out,
        "combined_odds":   bet.combined_odds,
        "stake":           bet.stake_amount,
        # Map canonical → legacy status vocabulary for the response.
        "status": ("live" if bet.status == STATUS_PENDING
                   else ("push" if bet.status == STATUS_PUSHED
                         else bet.status)),
        "legs_won":        legs_won,
        "legs_lost":       legs_lost,
        "legs_pending":    legs_pending,
        "settled_at":      (bet.settled_at.isoformat() if bet.settled_at else None),
        "payout":          bet.actual_payout,
        "cashout_estimate": None,
        "user_bet_id":     bet.user_bet_id,
    }


async def list_parlays_history_shape(
    db: Optional[AsyncIOMotorDatabase],
    *,
    user_id: str,
    status_filter: Optional[str] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Canonical read for ``GET /api/parlay/history``.
    Returns legacy-shape rows scoped to the given user, excluding any
    ``plearn_*`` rows by construction (we only ever read
    ``user_bets``).  Preserves the pre-Step-7 sort (created_at desc)
    and status filter semantics (``won|live|lost|all``)."""
    coll = _resolve_db(db)[COLLECTION]
    q: dict[str, Any] = {"user_id": user_id, "wager_type": WAGER_TYPE_PARLAY}
    if status_filter and status_filter != "all":
        legacy_to_canonical = {"live": STATUS_PENDING, "won": STATUS_WON,
                                "lost": STATUS_LOST, "push": STATUS_PUSHED}
        q["status"] = legacy_to_canonical.get(status_filter, status_filter)
    limit = max(1, min(int(limit), 500))
    cursor = coll.find(q).sort("placed_at", -1).limit(limit)
    docs = await cursor.to_list(limit)
    return [serialize_parlay_history_row(UserBet.from_document(d)) for d in docs]


# ═════════════════════════════════════════════════════════════════════
# Step 7 — Canonical settlement resolver
# ═════════════════════════════════════════════════════════════════════
async def resolve_pending_parlays_canonical(
    db: Optional[AsyncIOMotorDatabase] = None,
) -> dict[str, int]:
    """Walk canonical ``user_bets`` parlays with ``status='pending'`` and
    roll up each ticket based on the current settled state of its legs
    (as reflected in the ``picks`` collection).

    Rules (identical to :func:`parlay_history.resolve_saved_parlays`
    but operating on the canonical ledger):
      • Any leg ``lost``/``void``     → parlay ``lost``
      • All legs ``won``              → parlay ``won`` (payout computed
                                        from ``combined_odds`` × stake)
      • One or more ``push`` + rest ``won`` → parlay ``won``
        (parlay treats push as neutral, standard book convention)
      • Otherwise                     → still pending
      • Rows missing ``combined_odds`` or ``legs`` are skipped.
      • ``is_legacy=True`` migrated rows already terminal are skipped
        by the ``status='pending'`` filter.

    Uses :func:`settle_bet` so every change appends a
    ``settlement_events`` audit entry.  Returns aggregate counts.
    """
    d = _resolve_db(db)
    ub = d[COLLECTION]
    picks = d["picks"]

    updated = won = lost = 0
    cursor = ub.find({
        "wager_type": WAGER_TYPE_PARLAY,
        "status":     STATUS_PENDING,
    })
    async for pdoc in cursor:
        legs = pdoc.get("legs") or []
        pred_ids = [L.get("prediction_id") for L in legs
                     if isinstance(L, dict) and L.get("prediction_id")]
        if len(pred_ids) < 2:
            continue
        pick_docs = await picks.find(
            {"id": {"$in": pred_ids}}, {"id": 1, "status": 1, "_id": 0},
        ).to_list(length=len(pred_ids))
        status_by_id = {p["id"]: p.get("status") for p in pick_docs}
        leg_statuses: list[str] = []
        for pid in pred_ids:
            s = status_by_id.get(pid)
            if s in ("won", "lost", "void", "push"):
                leg_statuses.append(s)
            else:
                leg_statuses.append("pending")
        n_pending = sum(1 for s in leg_statuses if s == "pending")
        n_lost    = sum(1 for s in leg_statuses if s in ("lost", "void"))
        n_won     = sum(1 for s in leg_statuses if s == "won")
        n_push    = sum(1 for s in leg_statuses if s == "push")

        target: Optional[str] = None
        if n_lost > 0:
            target = STATUS_LOST
        elif n_pending == 0 and n_won + n_push == len(pred_ids):
            # Parlay wins if all legs won-or-push (push = no-action).
            target = STATUS_WON if n_won > 0 else STATUS_PUSHED
        else:
            continue

        combined = pdoc.get("combined_odds")
        stake    = pdoc.get("stake_amount") if pdoc.get("stake_amount") is not None else pdoc.get("stake_units")
        try:
            stake_f = float(stake) if stake is not None else None
        except (TypeError, ValueError):
            stake_f = None
        if target == STATUS_WON and combined is not None and stake_f is not None:
            profit = _american_profit_per_unit(int(combined), stake_f)
            payout = round(profit, 3)
            pnl    = round(profit, 3)
        elif target == STATUS_LOST and stake_f is not None:
            payout = 0.0
            pnl    = -stake_f
        else:
            payout = 0.0
            pnl    = 0.0

        # Update canonical fields via settle_bet (adds settlement event).
        user_bet_id = pdoc.get("user_bet_id") or pdoc.get("id")
        if not user_bet_id:
            continue
        # Also mirror the roll-up into the legacy alias fields we stamp
        # on saves so /api/user/analytics/* stays byte-parity even for
        # rows that were saved via ``parlay_save``.
        try:
            await settle_bet(
                user_bet_id,
                status=target,
                profit_loss=pnl,
                actual_payout=payout,
                actor="parlay_resolver_canonical",
                reason="all_legs_settled",
                db=d,
            )
            # Update legacy per-leg statuses (only where they exist) so
            # the parlay's legs render with settled statuses in reads.
            await ub.update_one(
                {"user_bet_id": user_bet_id},
                {"$set": {
                    # Legacy alias fields (may or may not exist).
                    "pnl_units":  round(pnl, 3),
                    # settled_at stamped by settle_bet already.
                }},
            )
            # Per-leg canonical status stamping.
            for i, s in enumerate(leg_statuses):
                canon_leg_status = ({"won": STATUS_WON,
                                     "lost": STATUS_LOST,
                                     "void": STATUS_VOID,
                                     "push": STATUS_PUSHED,
                                     "pending": STATUS_PENDING}).get(s, STATUS_PENDING)
                await ub.update_one(
                    {"user_bet_id": user_bet_id},
                    {"$set": {f"legs.{i}.status":          canon_leg_status,
                              f"legs.{i}.original_status": s}},
                )
            updated += 1
            if target == STATUS_WON:  won += 1
            if target == STATUS_LOST: lost += 1
        except UserBetLedgerError as e:
            logger.warning("canonical parlay resolver: %s", e)
            continue

    if updated:
        logger.info(
            "Canonical parlay resolver: %d updated, %d won, %d lost",
            updated, won, lost,
        )
    return {"updated": updated, "won": won, "lost": lost}



__all__ = [
    # constants
    "COLLECTION",
    "CANONICAL_MIGRATION_VERSION",
    "STATUS_PENDING", "STATUS_WON", "STATUS_LOST", "STATUS_PUSHED",
    "STATUS_VOID", "STATUS_PARTIALLY_SETTLED", "STATUS_CANCELLED",
    "STATUS_UNKNOWN",
    "CANONICAL_STATUSES", "TERMINAL_STATUSES", "LEGACY_STATUS_MAP",
    "CLV_UNAVAILABLE", "CLV_AVAILABLE", "CLV_PENDING",
    "WAGER_TYPE_STRAIGHT", "WAGER_TYPE_PARLAY", "CANONICAL_WAGER_TYPES",
    # errors
    "UserBetLedgerError", "LegacyRowNotEligible", "DuplicateIdempotencyError",
    # contracts
    "UserBetLeg", "UserBetSettlementEvent",
    "UserBet", "UserBetCreateRequest", "UserBetResult",
    # eligibility helpers
    "is_learning_row", "is_eligible_legacy_user_parlay",
    # mapping + idempotency
    "map_legacy_user_parlay", "map_legacy_status",
    "compute_idempotency_key",
    # API
    "create_bet", "create_parlay", "get_or_create_by_idempotency",
    "get_bet", "list_bets_for_user",
    "settle_bet", "settle_leg", "void_bet", "cancel_bet",
    # ops
    "IndexPreflightReport", "preflight_unique_indexes",
    "safe_ledger_diagnostics",
    "serialize_parlay_history_row", "list_parlays_history_shape",
    "resolve_pending_parlays_canonical",
]
