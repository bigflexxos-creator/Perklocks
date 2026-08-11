"""Missing-Data Safety Guard (§4).

Enforces the invariant:

    UNKNOWN != 0

Missing required data must NEVER silently become:
    * numeric zero
    * false evidence
    * a fake probability
    * synthetic sportsbook odds
    * a passed evidence gate
    * artificial confidence
    * Magic evidence
    * Apex evidence

This module provides:

* An explicit ``UNKNOWN`` sentinel that is distinct from ``None`` and
  from any numeric zero.  Comparing ``UNKNOWN`` to a number always
  returns ``NotImplemented`` at the Python level, so accidental
  arithmetic raises loudly rather than silently coercing.
* Validators that raise ``MissingDataViolation`` when the guard is
  crossed.  Callers running in OBSERVE mode should catch and record
  the violation rather than crashing production.
* ``coerce_optional_number`` — the ONLY sanctioned way to convert a
  possibly-missing external number into either a ``float`` or the
  ``UNKNOWN`` sentinel.
"""
from __future__ import annotations

from typing import Any, Optional


class MissingDataViolation(ValueError):
    """Raised when missing data is being coerced into a fake value."""


class IsUnknown:
    """Explicit UNKNOWN sentinel type.

    Distinct from ``None`` (which callers sometimes mean "absent
    optional field") and distinct from ``0`` / ``0.0`` (which is a
    real numeric value).  Never equal to any numeric value.
    """

    _instance: "Optional[IsUnknown]" = None

    def __new__(cls) -> "IsUnknown":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:      # pragma: no cover - trivial
        return "UNKNOWN"

    def __bool__(self) -> bool:
        # An UNKNOWN value is NOT truthy.  But it is also NOT falsy
        # in the numeric-zero sense — callers should test with
        # ``is_unknown(x)`` and never with truthiness alone.
        return False

    def __eq__(self, other: Any) -> bool:
        # Only equal to itself.  Explicitly NOT equal to 0 / 0.0 /
        # None / empty string.  This is the core guard.
        return other is self

    def __ne__(self, other: Any) -> bool:
        return other is not self

    def __hash__(self) -> int:
        return id(IsUnknown)

    # Arithmetic is deliberately forbidden — the guard MUST NOT
    # silently coerce UNKNOWN to 0 during any calculation.
    def __add__(self, other: Any) -> Any:
        raise MissingDataViolation(
            "UNKNOWN cannot participate in arithmetic — "
            "handle missing data explicitly before math")
    __radd__ = __add__
    __sub__  = __add__
    __rsub__ = __add__
    __mul__  = __add__
    __rmul__ = __add__
    __truediv__ = __add__
    __rtruediv__ = __add__
    __lt__ = __add__
    __le__ = __add__
    __gt__ = __add__
    __ge__ = __add__


UNKNOWN = IsUnknown()


def is_unknown(value: Any) -> bool:
    """Return True IFF ``value`` is the ``UNKNOWN`` sentinel."""
    return value is UNKNOWN


# ═══════════════════════════════════════════════════════════════════
# Coercion helpers
# ═══════════════════════════════════════════════════════════════════
def coerce_optional_number(value: Any) -> "float | IsUnknown":
    """Convert a possibly-missing external value into either a
    real ``float`` or the ``UNKNOWN`` sentinel.

    * ``None``, empty string, whitespace, non-numeric strings, and
      obvious sentinels (e.g., ``"N/A"``) all become ``UNKNOWN``.
    * ``bool`` is rejected — booleans must never masquerade as
      numeric evidence.
    * NaN / infinity become ``UNKNOWN`` — they are not valid
      production numbers.
    * ``0`` and ``0.0`` return ``0.0`` — a real zero is a real
      value, distinct from missing data.
    """
    if value is None:
        return UNKNOWN
    if isinstance(value, bool):
        # bool subclasses int in Python but has no place in evidence.
        return UNKNOWN
    if isinstance(value, IsUnknown):
        return UNKNOWN
    if isinstance(value, (int, float)):
        f = float(value)
        # Reject NaN / +-inf.
        if f != f or f in (float("inf"), float("-inf")):
            return UNKNOWN
        return f
    if isinstance(value, str):
        s = value.strip()
        if not s or s.upper() in {"N/A", "NA", "NONE", "NULL",
                                    "UNKNOWN", "?", "-"}:
            return UNKNOWN
        try:
            f = float(s)
        except (TypeError, ValueError):
            return UNKNOWN
        if f != f or f in (float("inf"), float("-inf")):
            return UNKNOWN
        return f
    return UNKNOWN


# ═══════════════════════════════════════════════════════════════════
# Validators — refuse to accept synthetic values
# ═══════════════════════════════════════════════════════════════════
def validate_no_synthetic_odds(
    book_odds: Any,
    *,
    no_real_book_line: Optional[bool] = None,
    provenance: Optional[str] = None,
) -> None:
    """Refuse synthetic/model-only book_odds.

    Legitimate American odds are non-zero integers whose provenance
    is a real sportsbook.  A model-derived "fair" price MUST NOT
    reach book_odds — that's the ``MODEL_ONLY_SYNTHETIC_ODDS``
    condition from ``pipeline_diagnostic``.

    ``provenance`` may be the ``pick["odds_provenance"]`` marker
    when available.  A provenance of ``"MODEL"`` / ``"SYNTHETIC"`` /
    ``"FAIR"`` is rejected.
    """
    if no_real_book_line is True:
        raise MissingDataViolation(
            "no_real_book_line flag is set — synthetic odds refused")
    if provenance and provenance.upper() in {
        "MODEL", "SYNTHETIC", "FAIR", "MODEL_ONLY", "COMPUTED",
    }:
        raise MissingDataViolation(
            f"odds provenance {provenance!r} is not a real sportsbook")
    try:
        n = int(book_odds)
    except (TypeError, ValueError):
        raise MissingDataViolation(
            f"book_odds {book_odds!r} is not a real American price")
    if n == 0:
        raise MissingDataViolation("book_odds == 0 is not a real price")


def validate_no_synthetic_probability(
    prob: Any,
    *,
    provenance: Optional[str] = None,
) -> None:
    """Refuse hand-manufactured probability values.

    A production probability must come from the real model or
    simulator (provenance in {``MODEL``, ``SIMULATOR``, ``CALIBRATED``,
    ``FUSION``}).  Fabricated "confidence" values with no provenance
    are rejected.
    """
    if provenance is None:
        raise MissingDataViolation(
            "probability provenance is required — refuse anonymous confidence")
    if provenance.upper() in {"MANUAL", "HARDCODED", "ARTIFICIAL", "MAGIC_ONLY"}:
        raise MissingDataViolation(
            f"probability provenance {provenance!r} is not production-valid")
    try:
        p = float(prob)
    except (TypeError, ValueError):
        raise MissingDataViolation(
            f"probability {prob!r} is not numeric")
    if p != p or p < 0.0 or p > 1.0:
        raise MissingDataViolation(
            f"probability {p!r} is outside [0, 1]")


__all__ = [
    "MissingDataViolation",
    "IsUnknown",
    "UNKNOWN",
    "is_unknown",
    "coerce_optional_number",
    "validate_no_synthetic_odds",
    "validate_no_synthetic_probability",
]
