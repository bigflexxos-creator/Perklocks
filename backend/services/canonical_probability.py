"""Canonical Final Probability Authority (MAIN 40 · Item #P0-E).

Single accessor used by every downstream authority-consuming path
so publication and Lock Score / Pick Breakdown / Rollover / Parlay /
Analytics agree on ONE probability per pick.

Priority (from ``pick_model_evidence.extract_model_evidence``):

    1. ``model_probability``       — canonical, set by publication
    2. ``published_probability``   — post-publication canonical
    3. ``win_probability``         — raw engine output (legacy)

Every value is normalised to fractional [0, 1].  ``None`` means the
pick lacks an authoritative probability — callers MUST refuse to
publish rather than fabricate one.

NEVER read ``sim_probability`` / ``implied_probability`` /
``fusion_probability`` as the FINAL authority.  Those are inputs.
"""
from __future__ import annotations

from typing import Optional


_FIELDS = ("model_probability", "published_probability", "win_probability")


def _to_fraction(x) -> Optional[float]:
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    if -0.001 <= v <= 1.001:
        return max(0.0, min(1.0, v))
    if 0.0 <= v <= 100.5:
        return max(0.0, min(1.0, v / 100.0))
    return None


def canonical_final_probability(pick: dict) -> Optional[float]:
    """Return the canonical final probability for ``pick`` as a
    fraction in [0, 1], or ``None`` when authoritative absent.

    A pick that has been through the publication decorator has
    ``model_probability`` set — that is the authority.  Legacy raw
    picks (pre-publication) fall through to ``win_probability``.
    """
    if not isinstance(pick, dict):
        return None
    for f in _FIELDS:
        v = _to_fraction(pick.get(f))
        if v is not None:
            return v
    return None


def canonical_final_probability_source(pick: dict) -> Optional[str]:
    """Return the field name that produced the canonical
    probability, or ``None`` when absent.  Used for provenance /
    audit — never for gating."""
    if not isinstance(pick, dict):
        return None
    for f in _FIELDS:
        if _to_fraction(pick.get(f)) is not None:
            return f
    return None


__all__ = [
    "canonical_final_probability",
    "canonical_final_probability_source",
]
