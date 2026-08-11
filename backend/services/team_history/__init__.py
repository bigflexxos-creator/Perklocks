"""Team History Foundation — Phase 5.3 Stage 3.

Multi-sport team-history contracts.  Every adapter normalises to the
same ``TeamHistoryEvidence`` shape while preserving sport-specific
raw fields in ``extras``.

Do NOT force team-history semantics onto Tennis / UFC — the
dispatcher returns ``NOT_APPLICABLE`` for those sports.
"""
from __future__ import annotations

from .models import (
    TeamHistoryEvidence,
    TeamHistoryWindow,
    TeamHistoryStatus,
    H2HResult,
    TeamHistoryQuality,
)
from .service import get_team_history
from .h2h import get_h2h_history

__all__ = [
    "TeamHistoryEvidence",
    "TeamHistoryWindow",
    "TeamHistoryStatus",
    "TeamHistoryQuality",
    "H2HResult",
    "get_team_history",
    "get_h2h_history",
]
