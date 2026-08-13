"""Tennis Feature Engine — real data replacement for `_factors_random`.

BLOCK 2A CLASSIFICATION (2026-08-13): **UNREACHABLE_MODERN_ENGINE**

This module is NOT wired into the authoritative Tennis production
runtime.  The canonical entry point is
``tennis_engine.apply_tennis_engine`` via
``services/pick_refresh_orchestrator.py``; that path does not currently
consume ``build_tennis_ml_factors``.  The only references in the
codebase are:

    tests/test_phase4e.py            (unit test)
    services/pipeline_diagnostic.py  (evidence probe, read-only)

Do NOT reintroduce this module into the runtime candidate path without
an explicit consolidation edit that documents which canonical evidence
slot it fills, why the existing helper set
(``tennis_identity`` + ``tennis_calibration`` + ``tennis_data_quality``
+ ``tennis_math_engine`` + ``tennis_elite_players`` + ``sim_tennis``)
cannot serve the same purpose, and a corresponding update to the
Block 2A tennis-consolidation report.

The Block 2A duplicate-runtime static guard treats this file as an
UNREACHABLE_MODERN_ENGINE.  Any future runtime caller MUST also update
the guard's approved-owners list.

USER MANDATE (2026-07-21): "Never substitute randomness for missing data."

Real-data sources for Tennis Moneyline factors:
  • Elo (surface + overall)  → tennis_players collection
  • H2H (career + surface)   → tennis_h2h enrichment
  • Recent form (matches_7d) → tennis_players.matches_7d
  • First-set RPW edge       → tennis_first_set enrichment
  • Sackmann stats           → tennis_sackmann_stats

Every factor returns Optional[float] in [0.0, 1.0]. Callers gate on
`has_enough_tennis_data(factors)`.
"""
from __future__ import annotations
import logging
from typing import Any, Optional

logger = logging.getLogger("lockscore.tennis_feature_engine")

MIN_FACTORS_TENNIS_ML = 3   # need 3 of 5 real factors to emit


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _scale(value: float, low: float, high: float,
           out_low: float = 0.40, out_high: float = 0.95) -> float:
    if high == low:
        return (out_low + out_high) / 2.0
    frac = (value - low) / (high - low)
    return _clamp(out_low + frac * (out_high - out_low), out_low, out_high)


# ═══════════════════════════════════════════════════════════════════════
# Factor functions — READ from `pick` doc since Tennis picks build their
# tennis_players / tennis_h2h / tennis_first_set attachments during
# enrichment. If a specific data key is missing, return None.
# ═══════════════════════════════════════════════════════════════════════
def factor_surface_elo_edge(pick: dict) -> Optional[float]:
    """Elo delta on the CURRENT surface (clay/hard/grass). Higher = advantage.

    Range: ±150 Elo → strong signal in tennis (65% win prob edge).
    """
    deep = pick.get("tennis_deep") or {}
    edge = deep.get("elo_edge")
    if not isinstance(edge, (int, float)):
        return None
    return round(_scale(float(edge), -150, 150), 3)


def factor_overall_elo(pick: dict) -> Optional[float]:
    """Overall (non-surface-specific) Elo rating for the picked player.
    Normalises to 0.4-0.95 based on 1400-2100 Elo range."""
    tp = pick.get("tennis_players") or {}
    pk_elo = tp.get("pick_elo_overall") or tp.get("pick_elo")
    if not isinstance(pk_elo, (int, float)):
        return None
    return round(_scale(float(pk_elo), 1400, 2100), 3)


def factor_h2h_dominance(pick: dict) -> Optional[float]:
    """Career + surface H2H share. Needs ≥3 matches for reliability."""
    h2h = pick.get("tennis_h2h") or {}
    n = h2h.get("matches", 0) or 0
    if n < 3:
        return None
    aw = h2h.get("a_wins", 0) or 0
    bw = h2h.get("b_wins", 0) or 0
    if aw + bw == 0:
        return None
    share = aw / (aw + bw)
    # 0.50 = even (0.62 score), 0.85 = dominant (0.90).
    return round(_scale(share, 0.15, 0.85, 0.30, 0.95), 3)


def factor_recent_form(pick: dict) -> Optional[float]:
    """Recent form via matches_7d + surface_fit. Fresh legs + surface
    specialists get bumped."""
    deep = pick.get("tennis_deep") or {}
    m7 = deep.get("matches_7d")
    sf = deep.get("surface_fit")
    if not (isinstance(m7, (int, float)) or isinstance(sf, (int, float))):
        return None
    # Blend: too many recent matches = fatigue (bad); high surface fit = good.
    score = 0.60  # neutral start
    if isinstance(m7, (int, float)):
        # 3+ matches in 7d = fatigue penalty; 0-1 matches = fresh.
        if m7 >= 4:   score -= 0.15
        elif m7 <= 1: score += 0.05
    if isinstance(sf, (int, float)):
        if sf >= 80:   score += 0.20
        elif sf >= 70: score += 0.10
        elif sf <= 40: score -= 0.15
    return round(_clamp(score, 0.30, 0.95), 3)


def factor_first_set_edge(pick: dict) -> Optional[float]:
    """First-set RPW edge — strongest set-1 predictor in tennis."""
    fs = pick.get("tennis_first_set") or {}
    edge = fs.get("edge_1st")
    if not isinstance(edge, (int, float)):
        return None
    # ±8pp edge is significant. 0 → 0.62 (neutral), +5 → 0.85, -5 → 0.40.
    return round(_scale(float(edge), -8, 8), 3)


def build_tennis_ml_factors(pick: dict) -> tuple[dict, list[str]]:
    """Build tennis moneyline factors from REAL pick-attached data."""
    factors: dict[str, Optional[float]] = {
        "Surface Elo Edge":     factor_surface_elo_edge(pick),
        "Overall Elo":          factor_overall_elo(pick),
        "H2H Dominance":        factor_h2h_dominance(pick),
        "Recent Form / Fit":    factor_recent_form(pick),
        "First-Set RPW Edge":   factor_first_set_edge(pick),
    }
    sources = [k for k, v in factors.items() if v is not None]
    return factors, sources


def has_enough_tennis_data(factors: dict) -> bool:
    return sum(1 for v in factors.values() if v is not None) >= MIN_FACTORS_TENNIS_ML


__all__ = [
    "build_tennis_ml_factors",
    "has_enough_tennis_data",
    "MIN_FACTORS_TENNIS_ML",
]
