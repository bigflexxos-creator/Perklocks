"""PERKLOCKS — Universal Production-Truth Contract (foundation gate).

This package is the *foundational gate* that applies across the 12
existing roadmap blocks. It does NOT introduce a new roadmap block.

It centralises the shared vocabulary and observability primitives
that all downstream production paths must reuse so that no feature
is ever classified as WIRED / SUPPORTED / PRODUCTION READY / ELITE /
MAGIC / APEX merely because code exists.

Modules
-------
* ``vocabulary``           — canonical stage codes, status states,
                              drop-reason contract, NOT_APPLICABLE
* ``missing_data_guard``   — UNKNOWN sentinel + missing-data safety
* ``reachability``         — universal reachability standard and
                              per-consumer eligibility surfaces
* ``chain_of_custody``     — producer → publication → user → snapshot
                              → settlement → analytics trace record
* ``consumption_proof``    — read-only real-production-path proofs
* ``pregame_snapshot``     — hash-sealed append-only pregame freeze
* ``settlement_linkage``   — PUBLISHED / SETTLED / MEASURABLE states
* ``enforcement``          — OBSERVE / ENFORCE mode with legacy
                              compatibility

Guarantees
----------
* Read-only import surface — importing this package never mutates
  any database record or performs a network call.
* Every function tolerates legacy records that predate the contract
  (they are reported as UNKNOWN, never crashed).
* No function in this package changes Lock Score, model probability,
  simulator probability, book_odds, canonical publication, or any
  scoring/ranking behaviour.
"""
from __future__ import annotations

from .vocabulary import (
    ProductionStage,
    StageStatus,
    DropReason,
    stage_status_pass,
    stage_status_fail,
    stage_status_unknown,
    stage_status_not_applicable,
    is_terminal,
)
from .missing_data_guard import (
    UNKNOWN,
    IsUnknown,
    is_unknown,
    validate_no_synthetic_odds,
    validate_no_synthetic_probability,
    coerce_optional_number,
    MissingDataViolation,
)
from .reachability import (
    ConsumerSurface,
    ReachabilityReport,
    build_reachability_report,
    reachability_summary,
)
from .chain_of_custody import (
    CustodyStage,
    CustodyRecord,
    build_custody_record,
)
from .enforcement import (
    EnforcementMode,
    current_mode,
    is_enforcing,
    set_mode_for_testing,
    reset_mode_for_testing,
    record_violation,
    recent_violations,
    clear_violations,
)

__all__ = [
    # vocabulary
    "ProductionStage",
    "StageStatus",
    "DropReason",
    "stage_status_pass",
    "stage_status_fail",
    "stage_status_unknown",
    "stage_status_not_applicable",
    "is_terminal",
    # missing-data guard
    "UNKNOWN",
    "IsUnknown",
    "is_unknown",
    "validate_no_synthetic_odds",
    "validate_no_synthetic_probability",
    "coerce_optional_number",
    "MissingDataViolation",
    # reachability
    "ConsumerSurface",
    "ReachabilityReport",
    "build_reachability_report",
    "reachability_summary",
    # chain of custody
    "CustodyStage",
    "CustodyRecord",
    "build_custody_record",
    # enforcement
    "EnforcementMode",
    "current_mode",
    "is_enforcing",
    "set_mode_for_testing",
    "reset_mode_for_testing",
    "record_violation",
    "recent_violations",
    "clear_violations",
]
