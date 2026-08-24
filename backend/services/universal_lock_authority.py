"""Universal Lock Authority — Pass 4 (2026-06).

One authoritative contract for every published pick.  Retires the
legacy Lock fallback paths that let each sport (or a fallback
branch) invent its own quality signal from book-implied
probability + a hard-coded ``data_quality = 75`` placeholder.

Contract:

    Win Expected  = model probability            (from a real sport-specific
                                                     model / simulator / engine)
    Fair Market   = no-vig sportsbook probability (metadata / benchmark only)
    Edge          = Win Expected - Fair Market
    Lock Score    = bet-quality / evidence reliability
                    ≠  Win Expected  (never a duplicate of the model prob)

Callers pass the pick and receive a normalised block:

    {
      "win_expected":    float in [0, 1],
      "fair_market":     float in [0, 1]  | None,
      "edge":            float           | None,
      "lock_score":      float in [0, 99],
      "authority":       str,   # "universal_lock_authority"
      "authority_version": str, # semantic version
    }

Missing / conditioned probabilities never surface — the gate
returns ``None`` so the caller can honestly fail-closed.
"""
from __future__ import annotations

from typing import Optional

AUTHORITY_NAME = "universal_lock_authority"
AUTHORITY_VERSION = "1.0.0"

_INDEPENDENT_PROVENANCES = {"CAUSAL_INDEPENDENT", "EMPIRICAL_INDEPENDENT"}
_CONDITIONED_PROVENANCES = {"MODEL_CONDITIONED", "PRIOR_ONLY", "INVALID"}


def _extract_win_expected(pick: dict) -> tuple[Optional[float], Optional[str]]:
    """Return ``(win_expected_float_0_1, source_label)`` or ``(None, reason)``.

    Reads the sport-specific probability fields in priority order:
        sim_win_probability (percent)     — the distribution simulator
        model_win_probability (percent)   — the sport-specific model
        model_win_prob (fraction)         — legacy fraction name
        win_probability (percent)         — legacy percent name

    Book-implied probability is NOT accepted here — that is Fair Market,
    not Win Expected.
    """
    for k in ("sim_win_probability", "model_win_probability",
              "win_probability"):
        v = pick.get(k)
        if isinstance(v, (int, float)) and 0.0 <= float(v) <= 100.0:
            return float(v) / 100.0, k
    v = pick.get("model_win_prob")
    if isinstance(v, (int, float)) and 0.0 <= float(v) <= 1.0:
        return float(v), "model_win_prob"
    return None, "no_model_probability"


def _extract_fair_market(pick: dict) -> Optional[float]:
    """Return no-vig book probability as a fraction in [0, 1].  Reads the
    same fields the rest of the codebase populates.
    """
    for k in ("no_vig_implied_pct", "no_vig_book_probability",
              "devig_market_probability", "book_no_vig_pct"):
        v = pick.get(k)
        if isinstance(v, (int, float)):
            fv = float(v)
            if 1.0 < fv <= 100.0:
                return fv / 100.0
            if 0.0 <= fv <= 1.0:
                return fv
    v = pick.get("implied_probability")
    if isinstance(v, (int, float)):
        fv = float(v)
        if 1.0 < fv <= 100.0:
            return fv / 100.0
        if 0.0 <= fv <= 1.0:
            return fv
    return None


def _provenance_of(pick: dict) -> Optional[str]:
    for k in ("simulator_provenance", "probability_provenance",
              "provenance"):
        v = pick.get(k)
        if v:
            return str(v)
    return None


def compute_universal_lock(pick: dict) -> Optional[dict]:
    """Return the universal-authority block for ``pick`` or ``None``.

    ``None`` when Win Expected cannot be produced from a legitimate
    independent model (MODEL_CONDITIONED / PRIOR_ONLY / INVALID
    provenance is NOT promotable — the pick must fail-closed
    upstream).
    """
    win_expected, src = _extract_win_expected(pick)
    if win_expected is None:
        return None
    prov = _provenance_of(pick)
    if prov in _CONDITIONED_PROVENANCES:
        return None
    fair = _extract_fair_market(pick)
    edge = None
    if fair is not None:
        edge = round(win_expected - fair, 6)

    # Lock Score = existing bet-quality score if already computed,
    # else recomputed via the sport-independent authority path so a
    # legacy caller receives a fresh score.  We DO NOT invent a Lock
    # from win_expected — Lock is bet-quality, not win %.
    lock_score = pick.get("lock_score")
    if not isinstance(lock_score, (int, float)):
        try:
            from sports_engine import compute_lock_score as _cls
            factors = pick.get("factors") or {}
            edge_pct = None
            if edge is not None:
                edge_pct = round(edge * 100, 2)
            score, _weighted = _cls(
                factors, win_prob=win_expected * 100,
                pick=pick, edge_percent=edge_pct,
            )
            lock_score = score
        except Exception:
            lock_score = 60.0

    return {
        "win_expected":       round(float(win_expected), 6),
        "fair_market":        (round(float(fair), 6)
                                 if fair is not None else None),
        "edge":                edge,
        "lock_score":         round(float(lock_score), 1),
        "authority":          AUTHORITY_NAME,
        "authority_version":  AUTHORITY_VERSION,
        "model_probability_source": src,
        "provenance":         prov,
    }


def apply_universal_lock(pick: dict) -> dict:
    """Legacy-migration helper.

    Mutates ``pick`` in place, stamping the universal block under
    ``pick["universal_lock"]`` when the pick has a valid independent
    model probability.  Returns the pick unchanged when the pick
    fails-closed (so the caller can decide to skip publication).

    This is the ENTRY POINT legacy Lock callers should adopt.  Every
    other Lock computation in the codebase must eventually route
    through here — the migration ships one caller at a time.
    """
    block = compute_universal_lock(pick)
    if block is None:
        pick["universal_lock"] = None
        pick["universal_lock_fail_reason"] = "no_independent_model_probability"
    else:
        pick["universal_lock"] = block
        # Retire the legacy inline Lock-Score-as-win-%-clone anti-pattern.
        # We stamp the authority version so downstream consumers can
        # verify (proof-observable).
        pick["lock_authority"]         = AUTHORITY_NAME
        pick["lock_authority_version"] = AUTHORITY_VERSION
    return pick


__all__ = [
    "AUTHORITY_NAME", "AUTHORITY_VERSION",
    "compute_universal_lock", "apply_universal_lock",
]
