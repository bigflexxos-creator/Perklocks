"""Enforcement Mode — OBSERVE / ENFORCE (§11).

The Production-Truth Contract launches in OBSERVE:

    * detect violations
    * record violations
    * classify violations
    * preserve drop reasons
    * expose diagnostic status
    * measure affected real production candidates

OBSERVE is NOT a permanent free pass — the system is built so
individual consumers can flip to ENFORCE (per-stage or globally)
before Block 12's deployment certification.

Legacy records that pre-date the contract MUST NOT crash even
when the contract is enforced — they surface as UNKNOWN and are
counted as a legacy compatibility violation, never a hard fault.
"""
from __future__ import annotations

import enum
import os
from datetime import datetime, timezone
from typing import Any, Optional


class EnforcementMode(str, enum.Enum):
    OBSERVE = "OBSERVE"
    ENFORCE = "ENFORCE"


# Default mode is OBSERVE (§11) unless overridden by env or tests.
_DEFAULT_MODE = EnforcementMode.OBSERVE

# Test override — set via ``set_mode_for_testing`` and cleared via
# ``reset_mode_for_testing``.  This ensures tests never rely on the
# process env being present in the CI shell.
_TEST_OVERRIDE: Optional[EnforcementMode] = None


def current_mode() -> EnforcementMode:
    """Return the currently active enforcement mode.

    Precedence:
        1. Test override (set via ``set_mode_for_testing``).
        2. Environment variable ``PRODUCTION_TRUTH_MODE``.
        3. Default (OBSERVE).
    """
    if _TEST_OVERRIDE is not None:
        return _TEST_OVERRIDE
    env = os.getenv("PRODUCTION_TRUTH_MODE")
    if env:
        env_u = env.upper().strip()
        if env_u in EnforcementMode.__members__:
            return EnforcementMode[env_u]
    return _DEFAULT_MODE


def is_enforcing() -> bool:
    return current_mode() is EnforcementMode.ENFORCE


def set_mode_for_testing(mode: "EnforcementMode | str") -> None:
    """Test helper — pin the enforcement mode for the current process."""
    global _TEST_OVERRIDE
    if isinstance(mode, str):
        mode = EnforcementMode(mode.upper())
    _TEST_OVERRIDE = mode


def reset_mode_for_testing() -> None:
    global _TEST_OVERRIDE
    _TEST_OVERRIDE = None


# ═══════════════════════════════════════════════════════════════════
# In-memory violation ring buffer (§11 — record violations)
# ═══════════════════════════════════════════════════════════════════
_VIOLATIONS: list[dict] = []
_VIOLATIONS_CAP = 1024


def record_violation(
    *,
    stage: Optional[str] = None,
    reason: Optional[str] = None,
    detail: Optional[str] = None,
    pick_id: Optional[str] = None,
    sport: Optional[str] = None,
    market: Optional[str] = None,
    extra: Optional[dict] = None,
) -> dict:
    """Record a contract violation.

    In OBSERVE mode this is fire-and-forget observability.
    In ENFORCE mode the caller is still expected to reject the
    offending record — this function itself never raises so
    observability writes always succeed.
    """
    rec: dict[str, Any] = {
        "ts":       datetime.now(timezone.utc).isoformat(),
        "mode":     current_mode().value,
        "stage":    stage,
        "reason":   reason,
        "detail":   detail,
        "pick_id":  pick_id,
        "sport":    sport,
        "market":   market,
    }
    if extra:
        rec["extra"] = extra
    _VIOLATIONS.append(rec)
    if len(_VIOLATIONS) > _VIOLATIONS_CAP:
        del _VIOLATIONS[: len(_VIOLATIONS) - _VIOLATIONS_CAP]
    return rec


def recent_violations(
    *,
    stage: Optional[str] = None,
    reason: Optional[str] = None,
    pick_id: Optional[str] = None,
    limit: int = 200,
) -> list[dict]:
    out = list(_VIOLATIONS)
    if stage:
        out = [r for r in out if r.get("stage") == stage]
    if reason:
        out = [r for r in out if r.get("reason") == reason]
    if pick_id:
        out = [r for r in out if r.get("pick_id") == pick_id]
    return out[-limit:]


def clear_violations() -> None:
    _VIOLATIONS.clear()


__all__ = [
    "EnforcementMode",
    "current_mode",
    "is_enforcing",
    "set_mode_for_testing",
    "reset_mode_for_testing",
    "record_violation",
    "recent_violations",
    "clear_violations",
]
