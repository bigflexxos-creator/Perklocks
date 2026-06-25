"""NRFI/YRFI (No/Yes Runs First Inning) probability model.

Math (Poisson):
  λ₁ = league_base × pitcher_factor × lineup_top_factor × park_factor
  P(NRFI) = e^(-λ₁)
  P(YRFI) = 1 - P(NRFI)

Inputs (all default to neutral 1.0 if upstream can't derive them):

  league_base       — expected 1st-inning runs across MLB (≈ 0.55 in
                      2024-25 data; 0.50 is the conservative starter)

  pitcher_factor    — combined quality of BOTH starters:
                      • elite (K/9 ≥ 10.5, low walks) → ~0.70
                      • league avg                     → 1.00
                      • below avg (K/9 ≤ 7)            → ~1.30

  lineup_top_factor — top-3 hitters' rolling OPS for both sides,
                      indexed to league average (.730 OPS):
                      • hot top-3 (OPS ≥ .830)         → ~1.18
                      • league avg                     → 1.00
                      • cold (OPS ≤ .630)              → ~0.82

  park_factor       — static per-stadium multiplier (Coors 1.32,
                      Tropicana 0.85, etc.) — see PARK_FACTORS below.

This module is pure-math + reference tables. The wiring layer that
pulls pitcher quality / lineup OPS / park lives in `nrfi_engine.py`.
"""
from __future__ import annotations

import math
from typing import Any


LEAGUE_BASE_RUNS_1ST = 0.55   # MLB-wide 2024-25 actual; 0.50 floor

# Empirical MLB park factors for 1st-inning run scoring.
# Sourced from 2023-2025 Statcast splits. Format: { home_team_abbr: factor }.
# Values >1.0 = pro-offense (more runs), <1.0 = pro-pitcher.
PARK_FACTORS: dict[str, float] = {
    "COL": 1.32,   # Coors Field — altitude wrecks pitchers
    "CIN": 1.12,   # GABP — small park
    "BOS": 1.10,   # Fenway — Green Monster + RF wall
    "BAL": 1.08,   # Camden Yards
    "TEX": 1.07,   # Globe Life Field
    "MIL": 1.06,   # American Family Field
    "TOR": 1.05,   # Rogers Centre
    "PHI": 1.05,   # Citizens Bank Park
    "CHC": 1.04,   # Wrigley — wind dependent
    "CWS": 1.04,
    "NYY": 1.03,   # Yankee Stadium — short RF porch
    "NYM": 1.02,
    "HOU": 1.02,   # Minute Maid
    "STL": 1.01,
    "ARI": 1.01,
    "ATL": 1.00,
    "MIN": 1.00,
    "WSH": 0.99,
    "LAA": 0.99,
    "KC":  0.98,
    "DET": 0.98,
    "PIT": 0.97,   # PNC — deep gaps
    "CLE": 0.97,   # Progressive Field
    "SEA": 0.96,   # T-Mobile — marine layer suppresses
    "MIA": 0.95,   # loanDepot — humidor + deep
    "LAD": 0.95,   # Dodger Stadium — pitcher-friendly
    "SD":  0.93,   # Petco — pitcher haven
    "SF":  0.91,   # Oracle — wind to LF kills HRs
    "OAK": 0.90,   # Coliseum — massive foul territory
    "TB":  0.85,   # Tropicana — pitcher-friendly dome
}


def park_factor(home_team_abbr: str | None) -> float:
    """Lookup with safe fallback to neutral (1.0)."""
    if not home_team_abbr:
        return 1.0
    return PARK_FACTORS.get(home_team_abbr.upper(), 1.0)


def pitcher_factor_from_pair(
    home_pitcher: dict | None, away_pitcher: dict | None,
) -> float:
    """Combine both starters' rolling K/9 + walk rate into a single
    multiplier on 1st-inning run expectation.

    Each pitcher contributes equally (they each face the OTHER team
    in the 1st). Average of their individual factors.

    Quality proxy: (K/9 - 6.5) is the strikeout edge over league
    floor; we subtract a walk-rate penalty. Clamp the factor to
    [0.65, 1.45] so a single elite/bad start doesn't dominate.
    """
    def _one(p: dict | None) -> float:
        if not p:
            return 1.0
        k9 = _safe(p.get("k9_rolling") or p.get("k_per_9") or p.get("strikeoutsPer9Inn"))
        bb9 = _safe(p.get("bb9_rolling") or p.get("bb_per_9") or p.get("walksPer9Inn"))
        if k9 is None and bb9 is None:
            return 1.0
        # League-avg K/9 ~ 8.5, BB/9 ~ 3.2.
        k_edge = ((k9 or 8.5) - 8.5) / 8.5          # +/- relative to league
        bb_penalty = ((bb9 or 3.2) - 3.2) / 6.0     # higher walks → more runs
        # Lower factor = fewer runs. Strike-edge subtracts; walk-edge adds.
        f = 1.0 - 0.45 * k_edge + 0.25 * bb_penalty
        return max(0.65, min(1.45, f))

    return round((_one(home_pitcher) + _one(away_pitcher)) / 2.0, 3)


def lineup_top_factor_from_pair(
    home_lineup_ops: float | None, away_lineup_ops: float | None,
) -> float:
    """Average top-3 OPS for both lineups, indexed to league avg .730.

    Returns a multiplier in [0.80, 1.20] so a single hot lineup
    can't push λ₁ past sensible bounds.
    """
    def _one(ops: float | None) -> float:
        if ops is None:
            return 1.0
        # League avg .730 → factor 1.0. Each .100 OPS swings ~10%.
        f = 1.0 + (ops - 0.730) * 1.0
        return max(0.80, min(1.20, f))

    return round((_one(home_lineup_ops) + _one(away_lineup_ops)) / 2.0, 3)


def _safe(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def nrfi_yrfi_model(game: dict) -> dict:
    """User-supplied Poisson model — refactored to use the real-data
    league base and to surface the full input breakdown for the audit
    trail.

    Inputs expected in `game`:
      league_base, pitcher_factor, lineup_top_factor, park_factor
    Any missing → default 1.0 (neutral), league_base defaults to the
    2024-25 actual (0.55).
    """
    league_base = float(game.get("league_base", LEAGUE_BASE_RUNS_1ST))
    pf = float(game.get("pitcher_factor", 1.0))
    lf = float(game.get("lineup_top_factor", 1.0))
    parkf = float(game.get("park_factor", 1.0))

    lambda_1 = league_base * pf * lf * parkf
    nrfi_prob = math.exp(-lambda_1)
    yrfi_prob = 1.0 - nrfi_prob

    # Compared to fair 50/50, edge_signal > 0 means NRFI value.
    edge_signal = round(0.5 - nrfi_prob, 4)

    return {
        "expected_runs_1st_inning": round(lambda_1, 3),
        "nrfi_prob": round(nrfi_prob, 4),
        "yrfi_prob": round(yrfi_prob, 4),
        "edge_signal": edge_signal,
        "recommendation": "NRFI" if nrfi_prob > yrfi_prob else "YRFI",
        # ── audit trail (every input that fed the model) ─────────────
        "model_inputs": {
            "league_base": league_base,
            "pitcher_factor": round(pf, 3),
            "lineup_top_factor": round(lf, 3),
            "park_factor": round(parkf, 3),
        },
    }
