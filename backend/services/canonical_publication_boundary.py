"""Canonical Publication Boundary — Session A (2026-06).

The ONE mandatory contract every active producer crosses before a
pick becomes user-visible.

Design
──────
The Lowest Common runtime Ancestor of every active producer is
``PredictionPublicationService.publish_batch()`` — every direct-inject
writer (``mls_direct_inject``, ``soccer_prop_inject``) AND every
canonical wrapper (``publish_upserted_picks``) AND the main pipeline
(``pick_refresh_orchestrator``) route their batches through it.

Rather than move the barrier up (which would create a duplicate path
risk), we enforce the canonical contract INSIDE ``publish_batch`` by
calling ``evaluate_publication`` from this module for every candidate.

Contract
────────
For a pick to be PUBLISHED (`publication_state="PUBLISHED"`):

  1. Real-line integrity.  A pick that carries `book_odds` MUST have
     a `book_odds` value that came from a real sportsbook.  Two
     equivalent shapes satisfy the check:

       a) `odds_source in _REAL_ODDS_SOURCES` AND `book_odds` is a
          non-null integer/float American price.
       b) `no_real_book_line == True` AND `book_odds is None` — the
          "MODEL_ONLY / NO_REAL_LINE" state is EXPLICIT and truthful
          about the missing sportsbook line.  In this shape the pick
          is still allowed to publish (the frontend just cannot show
          an edge / edge_percent must be None).

  2. NO synthetic sportsbook coercion.  A pick is REJECTED when it
     has `book_odds` set but `odds_source` is a producer-generated /
     model-derived label (`model_derived`, `synthetic`, `hfa_baseline`,
     `form`, `computed`) — that pattern is the exact "model prob →
     American odds → book_odds" pipe the P0 directive purges.

  3. Model provenance.  Either `model_probability` is present OR
     `model_evidence.model_probability` is populated OR the pick
     explicitly declares `no_model_probability_reason` (e.g. a market
     that has no model yet).  Missing model provenance → REJECTED.

  4. Identity classification.  `identity_class` must be populated
     with one of: `AUTHORITATIVE`, `MAPPED`, `PROVISIONAL`, `UNRESOLVED`.
     A pick with no identity_class at all → REJECTED (never inferred
     silently — must be classified before it can publish).  BUT a
     `PROVISIONAL` pick is ALLOWED to publish — identity quality is
     a SEPARATE dimension from publication validity.

  5. Edge coercion.  A pick with `edge_percent == 0` OR `edge_percent
     is None` is allowed to publish (both are legitimate states).  But
     if `no_real_book_line == True` AND `edge_percent not in (None, 0,
     0.0)` the pick is REJECTED — an edge without a real book line is
     a synthetic edge.  A REAL calculated zero edge (`edge_percent =
     0` and `book_odds` present with a valid real source) is preserved.

Result values
─────────────
* ``PUBLISHED``          — accepted, pick will be snapshot + dual-write.
* ``PUBLICATION_PENDING``— transient (returned when caller has NOT yet
                           attempted a publish).  Not emitted by
                           ``evaluate_publication`` directly.
* ``REJECTED``           — permanent policy failure — do NOT retry.
* ``FAILED``             — transient runtime error (DB/network).  The
                           reconciler is expected to retry with
                           back-off up to ``MAX_ATTEMPTS``.

Fail-CLOSED behaviour
─────────────────────
When a contract check raises unexpectedly, the pick is REJECTED with
reason ``BOUNDARY_INTERNAL_ERROR`` — never silently published.
"""
from __future__ import annotations

import enum
import logging
from typing import Any, Optional

# Phase 19 (Observability) — canonical boundary emits structured
# rejection telemetry.  Every REJECTED verdict logs the sport /
# market / rejection reasons so operators can grep for
# `SYNTHETIC_BOOK_ODDS` / `MODEL_LINE_NOT_REAL_OFFERING` / etc.
# without instrumenting the caller.
logger = logging.getLogger("lockscore.canonical_publication_boundary")


def _derive_market_family(pick: dict) -> str:
    """Best-effort market family from `pick.market` label.

    Used only when the producer didn't stamp `market_family`
    explicitly (legacy producers).  The lookup is CASE-INSENSITIVE
    and matches on stable market keywords so the fail-closed guard
    never no-ops on a family we do own.
    """
    m = (pick.get("market") or "").lower()
    if not m:
        return ""
    # Order matters — check specific tokens before generic ones.
    if "moneyline" in m or m.strip() in ("ml", "h2h"):
        return "moneyline"
    if "puck line" in m or "puckline" in m:
        return "puck_line"
    if "run line" in m or "runline" in m:
        return "run_line"
    if "spread" in m or "handicap" in m:
        return "spread"
    if "team total" in m:
        return "team_total"
    if "total" in m or "over" in m or "under" in m:
        return "total"
    return ""


# ── Constants ──────────────────────────────────────────────────────
MAX_PUBLICATION_ATTEMPTS = 5


class PublicationState(str, enum.Enum):
    PUBLICATION_PENDING = "PUBLICATION_PENDING"
    PUBLISHED           = "PUBLISHED"
    REJECTED            = "REJECTED"
    FAILED              = "FAILED"


class RejectionReason(str, enum.Enum):
    SYNTHETIC_BOOK_ODDS         = "SYNTHETIC_BOOK_ODDS"
    NO_REAL_LINE_WITH_ODDS      = "NO_REAL_LINE_WITH_ODDS"
    SYNTHETIC_EDGE              = "SYNTHETIC_EDGE"
    MISSING_MODEL_PROVENANCE    = "MISSING_MODEL_PROVENANCE"
    MISSING_IDENTITY_CLASS      = "MISSING_IDENTITY_CLASS"
    MISSING_PICK_ID             = "MISSING_PICK_ID"
    BOUNDARY_INTERNAL_ERROR     = "BOUNDARY_INTERNAL_ERROR"
    # Phase 9B/9F — player→event identity mismatch rejection.
    PLAYER_EVENT_IDENTITY_MISMATCH = "PLAYER_EVENT_IDENTITY_MISMATCH"
    # Phase 10A — cannot prove player belongs to event participants.
    PLAYER_TEAM_UNRESOLVED         = "PLAYER_TEAM_UNRESOLVED"
    # μ-closure Priority 2 (2026-06) — market cannot be authoritatively
    # settled by any currently-wired settler.  Fail closed so the pick
    # never becomes an actionable Board wager.
    SETTLEMENT_UNSUPPORTED         = "SETTLEMENT_UNSUPPORTED"
    # Phase 4 (Real Market Truth) — a pick that stamps ``model_line=True``
    # is a MODEL-derived line/threshold (e.g. Soccer Poisson-synthesized
    # alt totals, or any producer's "synthesized from market O/U"
    # branch).  Those outputs are RESEARCH-ONLY and MUST NOT become
    # actionable Locks candidates — they carry a book_odds computed
    # from the model, not an observed sportsbook offering.
    MODEL_LINE_NOT_REAL_OFFERING   = "MODEL_LINE_NOT_REAL_OFFERING"


# Verified real-sportsbook odds sources.  ANY producer that intends to
# publish a book_odds value MUST tag its source with one of these
# labels.  Model-derived labels (see _SYNTHETIC_ODDS_SOURCES) are
# purged from book_odds by this boundary.
_REAL_ODDS_SOURCES: frozenset[str] = frozenset({
    "the_odds_api", "the-odds-api", "theoddsapi",
    "odds_api", "odds-api",
    "sportsbook", "sportsbook_verified", "sportsbook_real",
    "prop-line", "propline", "prop_line",
    "draftkings", "fanduel", "betmgm", "caesars",
    "espn",       # ESPN scoreboard REAL moneylines only (parsed
                  # sportsbook price).  When ESPN has no odds the
                  # producer MUST set odds_source to MODEL_ONLY or
                  # HFA_BASELINE and no_real_book_line=True instead.
})

# Producer-generated labels that DO NOT satisfy real-line integrity.
# When a pick's `odds_source` is one of these AND `book_odds` is
# non-null, the boundary REJECTS with SYNTHETIC_BOOK_ODDS.
_SYNTHETIC_ODDS_SOURCES: frozenset[str] = frozenset({
    "model_derived", "model-derived",
    "synthetic", "synth", "fake",
    "hfa_baseline", "form", "computed",
    "espn_fallback",     # historical: ESPN synthetic-odds producer
                          # that computed book_odds from _prob_to_american.
                          # Post-Session-A those producers must set
                          # book_odds=None + no_real_book_line=True.
})

# Sentinel labels that signal an explicit "no sportsbook line" state.
_MODEL_ONLY_SOURCES: frozenset[str] = frozenset({
    "model_only", "MODEL_ONLY",
    "no_real_line", "NO_REAL_LINE",
})

_VALID_IDENTITY_CLASSES: frozenset[str] = frozenset({
    "AUTHORITATIVE", "MAPPED", "PROVISIONAL", "UNRESOLVED",
})


class BoundaryVerdict:
    """Structured verdict returned by ``evaluate_publication``."""

    __slots__ = ("state", "reasons", "meta")

    def __init__(self, state: PublicationState,
                 reasons: Optional[list[str]] = None,
                 meta: Optional[dict[str, Any]] = None) -> None:
        self.state = state
        self.reasons = list(reasons or [])
        self.meta = dict(meta or {})

    @property
    def accepted(self) -> bool:
        return self.state == PublicationState.PUBLISHED

    def to_dict(self) -> dict[str, Any]:
        return {
            "state":   self.state.value,
            "reasons": list(self.reasons),
            "meta":    dict(self.meta),
        }


def _has_book_odds(pick: dict) -> bool:
    v = pick.get("book_odds")
    if v is None or v == "":
        return False
    try:
        int(round(float(v)))
        return True
    except (TypeError, ValueError):
        return False


def _real_line_state(pick: dict) -> str:
    """Return one of: 'REAL', 'MODEL_ONLY', 'SYNTHETIC', 'MISSING'.

    Fail-closed for KNOWN synthetic labels; accept unclassified
    labels as REAL so legacy picks (pre-Session-A) that carry a
    real sportsbook price but omit ``odds_source`` are not
    misclassified as synthetic.  Every actively-writing producer we
    control (espn_soccer_fixtures, mls_direct_inject,
    soccer_prop_inject, canonical_pipeline) explicitly tags its
    source post-Session-A, so this leniency only affects legacy
    paths and unit-test fixtures.
    """
    src = pick.get("odds_source") or ""
    src_l = str(src).lower()
    has_odds = _has_book_odds(pick)
    no_real = bool(pick.get("no_real_book_line") is True)

    if no_real and not has_odds:
        return "MODEL_ONLY"
    if has_odds and no_real:
        # A pick declaring no_real_book_line while carrying a
        # book_odds value is a contradiction — that pattern is
        # exactly the synthetic-odds pipe Session A purges.
        return "SYNTHETIC"
    if has_odds and src_l in _SYNTHETIC_ODDS_SOURCES:
        return "SYNTHETIC"
    if has_odds and src_l in _MODEL_ONLY_SOURCES:
        return "SYNTHETIC"
    if has_odds:
        # Either an explicit REAL label or an unknown/legacy label —
        # both accepted.  Fail-closure applies only to KNOWN
        # synthetic labels (see above).
        return "REAL"
    if not has_odds:
        return "MODEL_ONLY" if no_real else "MISSING"
    return "SYNTHETIC"


def _has_model_provenance(pick: dict) -> bool:
    if pick.get("no_model_probability_reason"):
        return True
    for key in ("model_probability", "model_win_prob"):
        v = pick.get(key)
        if v is None or v == "":
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if 0.0 <= f <= 1.0:
            return True
    ev = pick.get("model_evidence")
    if isinstance(ev, dict):
        mp = ev.get("model_probability")
        try:
            f = float(mp) if mp is not None else None
            if f is not None and 0.0 <= f <= 1.0:
                return True
        except (TypeError, ValueError):
            pass
    return False


def _has_edge_synth(pick: dict) -> bool:
    """True when a pick declares NO real line but reports a nonzero
    edge — synthetic-edge condition."""
    if pick.get("no_real_book_line") is not True:
        return False
    e = pick.get("edge_percent")
    if e is None:
        return False
    try:
        return abs(float(e)) > 1e-9
    except (TypeError, ValueError):
        return False


def evaluate_publication(pick: dict) -> BoundaryVerdict:
    """Return the canonical publication verdict for ``pick``.

    This is the ONE contract every active producer's batch must
    pass.  A missing `id` or a contract failure produces a
    REJECTED verdict.  Runtime exceptions are trapped and produce a
    ``BOUNDARY_INTERNAL_ERROR`` REJECTED verdict — the boundary
    never silently accepts.
    """
    try:
        pid = pick.get("id") or pick.get("prediction_id")
        if not pid:
            return BoundaryVerdict(
                PublicationState.REJECTED,
                reasons=[RejectionReason.MISSING_PICK_ID.value],
            )

        reasons: list[str] = []

        # ── Rule 1/2 — real-line integrity + synthetic-odds purge ──
        line_state = _real_line_state(pick)
        if line_state == "SYNTHETIC":
            reasons.append(RejectionReason.SYNTHETIC_BOOK_ODDS.value)
        # NO_REAL_LINE combined with a book_odds value is a
        # contradiction — treat as synthetic.
        if pick.get("no_real_book_line") is True and _has_book_odds(pick):
            if RejectionReason.NO_REAL_LINE_WITH_ODDS.value not in reasons:
                reasons.append(RejectionReason.NO_REAL_LINE_WITH_ODDS.value)

        # ── Phase 4 (Real Market Truth) — model_line rejection ──
        # ``model_line=True`` marks a producer-synthesized threshold
        # (Soccer Poisson-alt totals, "synthesized from market O/U",
        # any model-derived alt line).  Even when it carries a
        # book_odds integer, that price came from the model — NOT
        # from an observed sportsbook offering.  Reject at the
        # canonical boundary so it can NEVER cross into Locks; the
        # row remains in db.picks (research/shadow provenance).
        if pick.get("model_line") is True:
            reasons.append(
                RejectionReason.MODEL_LINE_NOT_REAL_OFFERING.value)
        # Additional model-source guard: even without ``model_line``
        # a producer that tags ``model_source`` starting with the
        # synthesized-line prefixes must be rejected.  Keeps future
        # producers (e.g. NFL model-alt-props) from silently leaking.
        _ms = str(pick.get("model_source") or "").lower()
        _SYNTHESIZED_MODEL_SOURCE_PREFIXES = (
            "poisson_from_", "synthetic_", "model_only_",
            "synthesized_alt", "synthesized_from_",
        )
        if _ms and any(_ms.startswith(pref)
                       for pref in _SYNTHESIZED_MODEL_SOURCE_PREFIXES):
            if RejectionReason.MODEL_LINE_NOT_REAL_OFFERING.value \
                    not in reasons:
                reasons.append(
                    RejectionReason.MODEL_LINE_NOT_REAL_OFFERING.value)

        # ── Rule 5 — synthetic edge ──
        if _has_edge_synth(pick):
            reasons.append(RejectionReason.SYNTHETIC_EDGE.value)

        # ── Rule 5.5 — MODEL_UNAVAILABLE authority (Phase 5 wiring) ──
        # A pick whose (sport, market_family) is registered
        # ``MODEL_UNAVAILABLE`` in the sport model authority registry
        # MUST NEVER become an actionable Locks candidate.  This is
        # the runtime enforcement wire for the Phase-5 registry —
        # previously the registry existed but the boundary never
        # queried it, so UFC / NHL picks slipped through with
        # ``model_source=None``.  Fail-closed here.
        try:
            from services.sport_model_authority import (
                is_unavailable, is_authoritative, is_registered,
            )
            sport = pick.get("sport") or ""
            market_family = (pick.get("market_family")
                              or _derive_market_family(pick))
            if sport and market_family:
                if is_unavailable(sport, market_family):
                    reasons.append(
                        RejectionReason.MODEL_LINE_NOT_REAL_OFFERING.value
                    )
                    logger.warning(
                        "boundary_reject sport=%s market_family=%s "
                        "reason=MODEL_UNAVAILABLE pick=%s",
                        sport, market_family, pick.get("id"),
                    )
        except Exception:
            # Never crash publication on registry lookup failure —
            # the individual reason-specific guards above still fire.
            pass

        # ── Rule 3 — model provenance ──
        if not _has_model_provenance(pick):
            reasons.append(RejectionReason.MISSING_MODEL_PROVENANCE.value)

        # ── Rule 4 — identity classification present ──
        ic = pick.get("identity_class")
        if not (isinstance(ic, str) and ic in _VALID_IDENTITY_CLASSES):
            reasons.append(RejectionReason.MISSING_IDENTITY_CLASS.value)

        # ── Rule 6 (Phase 9B/9F + Phase 10A) — player→event identity gate ──
        # Fail-closed for provable mismatches AND for unresolvable
        # identity on player-markets (Phase 10A tightening).
        try:
            from services.player_event_identity_gate import (
                evaluate_identity, IdentityVerdict,
            )
            id_verdict = evaluate_identity(pick)
            if id_verdict == IdentityVerdict.PLAYER_EVENT_IDENTITY_MISMATCH:
                reasons.append(
                    RejectionReason.PLAYER_EVENT_IDENTITY_MISMATCH.value
                )
            elif id_verdict == IdentityVerdict.PLAYER_TEAM_UNRESOLVED:
                # Phase 10A: cannot prove membership → reject rather than
                # silently attach the player to the "most likely" event.
                reasons.append(
                    RejectionReason.PLAYER_TEAM_UNRESOLVED.value
                )
        except Exception:
            # Never let the gate crash the boundary — fail-open on gate
            # failure since identity check is defense-in-depth, not the
            # primary quality contract.
            pass

        # ── Rule 7 (μ-closure P2, 2026-06) — Settlement Capability ──
        # A pick whose (sport, market, league) is authoritatively
        # classified as SETTLEMENT_UNSUPPORTED must NEVER become an
        # actionable Board wager.  We fail closed at the publication
        # boundary — every producer's batch crosses this contract.
        # UNKNOWN classifications remain permitted (fail-open) so a
        # new market surface is not silently blocked before the
        # registry is updated.  Only the explicit UNSUPPORTED deny-
        # list rejects here.
        try:
            from services.settlement_capability import (
                classify as _settle_classify,
                UNSUPPORTED as _SETTLE_UNSUPPORTED,
            )
            # Producers may store the market under multiple keys;
            # canonical priority: market_key → market → prop_market → bet_type.
            _market = (
                pick.get("market_key")
                or pick.get("market")
                or pick.get("prop_market")
                or pick.get("bet_type")
            )
            _sport = pick.get("sport") or pick.get("sport_key")
            _league = pick.get("league")
            if _market:
                _status, _reason = _settle_classify(_sport, _market, _league)
                if _status == _SETTLE_UNSUPPORTED:
                    reasons.append(
                        RejectionReason.SETTLEMENT_UNSUPPORTED.value
                    )
        except Exception:
            # Never let the capability check crash the boundary.
            # Defense-in-depth — settler auto-void still catches
            # any that slip through.
            pass

        if reasons:
            return BoundaryVerdict(
                PublicationState.REJECTED,
                reasons=reasons,
                meta={
                    "line_state":         line_state,
                    "book_odds":          pick.get("book_odds"),
                    "odds_source":        pick.get("odds_source"),
                    "no_real_book_line":  pick.get("no_real_book_line"),
                    "identity_class":     pick.get("identity_class"),
                },
            )
        return BoundaryVerdict(
            PublicationState.PUBLISHED,
            reasons=[],
            meta={
                "line_state":     line_state,
                "identity_class": pick.get("identity_class"),
            },
        )
    except Exception as e:                          # pragma: no cover
        return BoundaryVerdict(
            PublicationState.REJECTED,
            reasons=[RejectionReason.BOUNDARY_INTERNAL_ERROR.value],
            meta={"exception": f"{e.__class__.__name__}: {e}"},
        )


__all__ = [
    "PublicationState",
    "RejectionReason",
    "BoundaryVerdict",
    "evaluate_publication",
    "MAX_PUBLICATION_ATTEMPTS",
    "_REAL_ODDS_SOURCES",
    "_SYNTHETIC_ODDS_SOURCES",
    "_MODEL_ONLY_SOURCES",
    "_VALID_IDENTITY_CLASSES",
]
