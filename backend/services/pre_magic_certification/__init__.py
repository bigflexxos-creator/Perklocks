"""Pre-Magic Certification (P1 — 2026-06 roadmap block).

READ-ONLY certification harness that proves the evidence foundation
required by Magic 2.0 is:

  1. real
  2. canonical
  3. correctly identified
  4. as-of safe
  5. threshold-aware
  6. provenance-aware
  7. reachable from REAL current pick identities
  8. honest when unavailable
  9. safe from missing→0 leakage
  10. ready for later Magic consumption

**HARD RULES enforced by this module:**

  DATA EXISTS
        !=
  DATA REACHABLE FOR CURRENT PICK
        !=
  MAGIC CONSUMES DATA
        !=
  PRODUCTION EFFECT PROVEN

* NO consumer wiring.  The module reads MongoDB, evaluates the
  existing history/observer/publication stacks, and emits a
  ``CertificationMatrix``.  It NEVER writes to any collection, NEVER
  mutates a pick, and NEVER upgrades Magic's consumption state.
* MAGIC_CONSUMPTION remains UNKNOWN / NOT_WIRED regardless of the
  outcome of any check.
* Sports currently classified as SOURCE_INSUFFICIENT / UNAVAILABLE
  (NHL Team, CFB Team, UFC) remain UNAVAILABLE — this module refuses
  to convert absent history into synthetic zero evidence.
"""
from __future__ import annotations

from .states import (
    CertificationState,
    EvidenceType,
    CertificationEntry,
    CertificationMatrix,
)
from .certifier import (
    build_certification_matrix,
    write_certification_report,
)

__all__ = [
    "CertificationState",
    "EvidenceType",
    "CertificationEntry",
    "CertificationMatrix",
    "build_certification_matrix",
    "write_certification_report",
]
