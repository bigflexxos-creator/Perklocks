"""Pre-Magic Certification — state vocabulary.

Explicit multi-valued certification states.  ``true / false`` is
banned per §14 — every certification cell must report one of the
five explicit states below.

``CONSUMER_VISIBLE`` for MAGIC is ALWAYS ``NOT_WIRED`` in this
release — Magic 2.0 has not been proven to consume any evidence
yet (§15).  A change of that state requires a separate audited
release.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Optional


class CertificationState(str, enum.Enum):
    """Five-valued certification state per §14.  Never collapse to bool."""
    PASS         = "PASS"
    FAIL         = "FAIL"
    PARTIAL      = "PARTIAL"
    UNAVAILABLE  = "UNAVAILABLE"
    UNKNOWN      = "UNKNOWN"
    NOT_WIRED    = "NOT_WIRED"     # magic consumption state (§15)
    NOT_APPLICABLE = "NOT_APPLICABLE"


class EvidenceType(str, enum.Enum):
    """Distinct evidence surfaces certified independently.

    Each surface reports its own five-valued state — a PASS on
    PLAYER_HISTORY does NOT imply PASS on EXACT_THRESHOLD (§14).
    """
    PLAYER_HISTORY      = "PLAYER_HISTORY"
    TEAM_HISTORY        = "TEAM_HISTORY"
    H2H                 = "H2H"
    EXACT_THRESHOLD     = "EXACT_THRESHOLD"
    DISTRIBUTIONS       = "DISTRIBUTIONS"
    AS_OF_SAFETY        = "AS_OF_SAFETY"
    MISSING_NOT_ZERO    = "MISSING_NOT_ZERO"
    IDENTITY            = "IDENTITY"
    PICK_IDENTITY_TAGGING = "PICK_IDENTITY_TAGGING"
    MARKET_NORMALIZATION = "MARKET_NORMALIZATION"
    TENNIS_CONTEXT      = "TENNIS_CONTEXT"
    MARKET_READINESS    = "MARKET_READINESS"
    SOCCER_PRODUCER_INTEGRITY = "SOCCER_PRODUCER_INTEGRITY"
    MODEL_READINESS     = "MODEL_READINESS"
    LIVE_PICK_REACHABILITY = "LIVE_PICK_REACHABILITY"


@dataclass
class CertificationEntry:
    """A single (SPORT × MARKET × EVIDENCE_TYPE) certification row.

    Field semantics
    ---------------
    ``data_available``  Does the underlying raw source exist?
    ``identity_resolved`` Is canonical identity attachable?
    ``reachable``       Can the read-path fetch it for a REAL pick?
    ``as_of_safe``      Does the read-path refuse future leakage?
    ``sample_size``     Actual N used (never fabricated).
    ``provenance``      Source label — collection / adapter tag.
    ``consumer_visible`` Downstream consumer visibility state.  For
                         MAGIC this is ALWAYS ``NOT_WIRED``.
    ``certification_status`` Rollup of the above.
    ``drop_reason``     Canonical DropReason code when FAIL/UNAVAILABLE.
    ``detail``          Free-text machine-safe context (line#s, ids).
    """
    sport:               str
    market:              str
    evidence_type:       str
    data_available:      str = CertificationState.UNKNOWN.value
    identity_resolved:   str = CertificationState.UNKNOWN.value
    reachable:           str = CertificationState.UNKNOWN.value
    as_of_safe:          str = CertificationState.UNKNOWN.value
    sample_size:         Optional[int] = None
    provenance:          Optional[str] = None
    consumer_visible:    str = CertificationState.NOT_WIRED.value
    certification_status: str = CertificationState.UNKNOWN.value
    drop_reason:         Optional[str] = None
    detail:              Optional[str] = None
    checked_at:          str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CertificationMatrix:
    """Complete certification matrix — the final Pre-Magic artefact."""
    generated_at:  str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat())
    entries:       list[CertificationEntry] = field(default_factory=list)
    magic_consumption: str = CertificationState.NOT_WIRED.value
    lock_score_consumption: str = "UNCHANGED"    # §15 — not touched
    counts:        dict[str, int] = field(default_factory=dict)
    ready_for_magic: str = CertificationState.UNKNOWN.value
    findings:      list[dict] = field(default_factory=list)
    # Free-form recommendations (not machine actions) — a human must
    # decide whether to wire Magic.  §15 forbids automatic promotion.
    recommendation: Optional[str] = None

    def add(self, entry: CertificationEntry) -> None:
        self.entries.append(entry)

    def add_finding(self, level: str, code: str, detail: str,
                    context: Optional[dict] = None) -> None:
        self.findings.append({
            "level":   level,   # INFO | WARN | FAIL
            "code":    code,
            "detail":  detail,
            "context": context or {},
        })

    def rollup(self) -> None:
        """Compute ``counts`` and ``ready_for_magic``.

        ``ready_for_magic`` is PASS iff:
          * every entry with a non-UNAVAILABLE certification is PASS,
          * at least ONE ``LIVE_PICK_REACHABILITY`` entry is PASS
            (§1 — reachability must be proven on real picks),
          * magic consumption is still ``NOT_WIRED`` (§15 — always).
        """
        counts: dict[str, int] = {}
        any_fail = False
        any_unknown = False
        any_partial = False
        real_pass = 0
        live_pass = 0
        live_present = False
        for e in self.entries:
            s = e.certification_status
            counts[s] = counts.get(s, 0) + 1
            if s == CertificationState.FAIL.value:
                any_fail = True
            elif s == CertificationState.UNKNOWN.value:
                any_unknown = True
            elif s == CertificationState.PARTIAL.value:
                any_partial = True
            elif s == CertificationState.PASS.value:
                real_pass += 1
            if e.evidence_type == "LIVE_PICK_REACHABILITY":
                live_present = True
                if s == CertificationState.PASS.value:
                    live_pass += 1
        self.counts = counts
        # Magic consumption MUST remain NOT_WIRED (§15).  If it is
        # anything else the entire matrix is contradictory and we
        # refuse to declare READY.
        if self.magic_consumption != CertificationState.NOT_WIRED.value:
            self.ready_for_magic = CertificationState.FAIL.value
            self.recommendation = (
                "MAGIC CONSUMPTION STATE HAS CHANGED — refuse promotion. "
                "Investigate and revert.")
            return
        if any_fail:
            self.ready_for_magic = CertificationState.FAIL.value
            self.recommendation = (
                "NOT READY: at least one certification FAILed. "
                "Fix the FAIL rows before considering Magic 2.0.")
        elif any_partial or any_unknown:
            self.ready_for_magic = CertificationState.PARTIAL.value
            self.recommendation = (
                "PARTIAL: some evidence is unavailable in this "
                "environment (expected for empty DB or unsupported "
                "sports). Magic 2.0 must degrade gracefully for "
                "these before promotion.")
        elif live_present and live_pass == 0:
            # §1 — reachability must be proven on at least ONE real
            # live pick, otherwise we cannot certify that the read
            # path is actually reachable in production.
            self.ready_for_magic = CertificationState.PARTIAL.value
            self.recommendation = (
                "PARTIAL: history exists but NO live pick could be "
                "traced end-to-end through the read path. Investigate "
                "identity resolution / market normalization before "
                "considering Magic 2.0.")
        elif real_pass > 0:
            self.ready_for_magic = CertificationState.PASS.value
            self.recommendation = (
                "READY (subject to Magic wiring being explicitly "
                "opted in by the operator). No FAIL / PARTIAL rows, "
                "and end-to-end reachability proven for at least one "
                "live pick.")
        else:
            self.ready_for_magic = CertificationState.UNAVAILABLE.value
            self.recommendation = (
                "UNAVAILABLE: no live evidence certified in this "
                "environment. Re-run after canonical publication has "
                "populated real picks.")

    def to_dict(self) -> dict:
        return {
            "generated_at":            self.generated_at,
            "magic_consumption":       self.magic_consumption,
            "lock_score_consumption":  self.lock_score_consumption,
            "ready_for_magic":         self.ready_for_magic,
            "recommendation":          self.recommendation,
            "counts":                  self.counts,
            "findings":                self.findings,
            "entries":                 [e.to_dict() for e in self.entries],
        }


__all__ = [
    "CertificationState",
    "EvidenceType",
    "CertificationEntry",
    "CertificationMatrix",
]
