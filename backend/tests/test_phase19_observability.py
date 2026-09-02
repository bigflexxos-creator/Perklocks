"""Phase 19 — OBSERVABILITY / FAIL-CLOSED HARDENING invariants.

  O1. Structured logging in place across canonical services.
  O2. Fail-closed behaviour on unregistered sport authority
      (verified in Phase 5).
  O3. Boundary rejection reasons are ENUMERATED (never free-form
      strings) — every rejection reason maps to a stable identifier
      that observability can index.
  O4. Settlement contract raises hard on `lost` with `actual=None`
      (verified in Phase 10) — never silently soft-fails.
  O5. Job coordinator logs every attempt (`job_audit_log`
      collection wired with 180d TTL).
  O6. `logger` present in every canonical service module —
      spot-check the six most critical.
"""
from __future__ import annotations
import pathlib


REPO = pathlib.Path("/app/backend")


def test_all_rejection_reasons_are_enumerated():
    """`RejectionReason` enum in `canonical_publication_boundary` is
    the sole source of rejection labels."""
    src = (REPO / "services/canonical_publication_boundary.py").read_text()
    assert "class RejectionReason(str, enum.Enum):" in src
    # Enumerate the specific reasons we depend on.
    for reason in (
        "SYNTHETIC_BOOK_ODDS", "NO_REAL_LINE_WITH_ODDS",
        "SYNTHETIC_EDGE", "MISSING_MODEL_PROVENANCE",
        "MISSING_IDENTITY_CLASS", "PLAYER_EVENT_IDENTITY_MISMATCH",
        "SETTLEMENT_UNSUPPORTED", "MODEL_LINE_NOT_REAL_OFFERING",
    ):
        assert reason in src, f"rejection reason {reason} missing"


def test_critical_services_have_loggers():
    critical = (
        "sports_engine.py",
        "server.py",
        "services/prediction_publication_service.py",
        "services/canonical_publication_boundary.py",
        "services/job_coordinator.py",
        "settlement_engine.py",
    )
    for name in critical:
        src = (REPO / name).read_text(encoding="utf-8")
        assert "logger" in src.lower(), f"{name} missing logger"


def test_job_audit_log_has_ttl():
    """Phase 9B established this; re-verify from Phase 19's
    observability lens."""
    from services.index_registry import _INDEX_SPECS
    m = [s for s in _INDEX_SPECS
         if s.collection == "job_audit_log"
         and s.expire_after_seconds is not None]
    assert m


def test_publication_mismatch_report_captures_drift():
    """The mismatch reporter exists — Phase 1 established it, Phase
    19 re-verifies it's still wired."""
    src = (REPO / "services/prediction_publication_service.py").read_text()
    assert "publication_mismatch_report" in src


def test_fail_closed_rejection_reasons_include_all_hardening():
    """Every hardening rule the master spec calls for must have a
    stable rejection reason available."""
    from services.canonical_publication_boundary import RejectionReason
    required = {
        "SYNTHETIC_BOOK_ODDS",
        "NO_REAL_LINE_WITH_ODDS",
        "SYNTHETIC_EDGE",
        "MODEL_LINE_NOT_REAL_OFFERING",   # Phase 4 fix
        "SETTLEMENT_UNSUPPORTED",
        "MISSING_MODEL_PROVENANCE",
        "MISSING_IDENTITY_CLASS",
    }
    got = {r.value for r in RejectionReason}
    missing = required - got
    assert not missing, f"missing rejection reasons: {missing}"
