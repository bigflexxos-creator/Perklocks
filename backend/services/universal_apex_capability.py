"""Universal Apex capability declaration (Block 48).

The Apex gate (``services/magic/apex_gate.py``) is unchanged.  This
module ONLY declares which (sport, family) combinations are
STRUCTURALLY capable of reaching Lock Score = 100 Apex if the complete
evidence path converges.

Any arbitrary per-market ceiling (e.g. "soccer AGS can only ever
reach 92") is a BUG and must be caught by the preservation
regression tests.

Block 49: Apex remains fail-closed.  A famous name alone never
qualifies.  A low-frequency event alone is NOT disqualified.
"""
from __future__ import annotations

from services.sport_capability_matrix import is_universal_apex_capable


__all__ = ["is_universal_apex_capable"]
