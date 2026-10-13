"""Apex Reachability — Constant Independent-Model-Probability Matrix.

User contract (2026-06 final closure):

    HOLD CONSTANT across every row:
      • independent_model_probability
      • calibrated_probability
      • evidence coverage
      • reliability
      • historical evidence
      • matchup/context
      • convergence
      • distribution stability
      • UEA / Bet Quality inputs

    CHANGE ONLY:
      • book_odds

    Run the SAME elite fixture at:
      +200, +150, +100, -110, -150, -200, -300, -400, -500, -600

Return for every row:
    book_odds, book_implied_probability, independent_model_probability,
    calibrated_probability, UEA/evidence score, legacy_wp_cap,
    effective_authority_ceiling, base_lock_score, final_lock_score,
    apex_eligible, limiting_factor.

PASS CONDITION:
    Changing only sportsbook price cannot impose a structural Apex
    ceiling — ``apex_eligible == True`` for every row at this
    independent model probability (0.90) with full evidence coverage.

INVERSE CONTROL (same book_odds, vary evidence):
    hold book_odds = -500
    weak evidence  → Apex blocked
    elite evidence → Apex eligible

All tests run against the REAL production code paths —
    * ``services.magic.lock_score_integrator.apply_magic_and_apex``
    * ``services.magic.apex_gate.evaluate_apex``
    * the orchestrator reliability-cap math (replicated verbatim from
      ``services/pick_refresh_orchestrator.py:2340-2420`` so the test
      runs without a live Mongo).
"""
from __future__ import annotations

import copy
import math
from typing import Any

import pytest

from services.magic.contract import (
    Availability, EvidenceItem, EvidenceType, MagicOutput, MagicTier,
)
from services.magic.lock_score_integrator import apply_magic_and_apex
from services.magic.apex_gate import (
    APEX_MIN_BASE_SCORE, APEX_MIN_POSITIVE_CATEGORIES,
)


# ─── CONSTANT INPUTS (identical for every row of the main matrix) ────
# These are the "elite fixture" the user approved — a plausible NFL
# player-prop set-up with full evidence coverage.  Kept small/clear so
# the test harness is auditable.

INDEPENDENT_MODEL_PROB   = 0.90    # frozen across rows
CALIBRATED_PROB          = 0.88    # frozen across rows
UEA_COVERAGE             = 0.85    # frozen across rows
UEA_CEILING              = 100.0   # frozen across rows (full evidence)
BASE_LOCK_SCORE          = 97.0    # pre-Magic base (meets APEX floor)
CONVERGENCE              = 0.88    # frozen across rows
DISTRIBUTION_STABILITY   = 0.90
MAGIC_SCORE              = 85.0

# Odds matrix (American format).
ODDS_MATRIX = [+200, +150, +100, -110, -150, -200, -300, -400, -500, -600]


def _american_to_implied(odds: float) -> float:
    """Standard American-odds → implied probability."""
    o = float(odds)
    if o > 0:
        return 100.0 / (o + 100.0)
    return (-o) / ((-o) + 100.0)


def _reliability_cap_math(base: float, wp: float,
                            uea_cov: float, uea_contras: list,
                            uea_ceiling: float) -> tuple[float, bool, str]:
    """Replicates the orchestrator §RANKING NFL reliability cap exactly.

    Returns ``(effective_ceiling, has_full_evidence, limiting_factor)``.
    """
    wp = max(0.0, min(1.0, float(wp)))
    reliability_cap = 60.0 + wp * 40.0
    has_full_evidence = (
        uea_cov >= 0.65 and not uea_contras and uea_ceiling >= 85.0
    )
    if has_full_evidence:
        # UEA-authority scope: ceiling is max(60+wp*40, uea_ceiling).
        eff = max(reliability_cap, uea_ceiling)
        return eff, True, "uea_authority_ceiling"
    # Legacy low-evidence scope: 60 + wp * 40.
    return reliability_cap, False, "legacy_reliability_cap"


def _elite_mo(pick_id: str, book_odds: float) -> MagicOutput:
    """Build an ``ALIGNED_STRONG`` MagicOutput with 5/6 positive
    categories, no contradictions, no risk flags — identical shape
    for every row except the ``market_intel`` evidence carries the
    varying ``book_odds``."""
    mo = MagicOutput(
        pick_id=pick_id,
        sport="NFL",
        market="player_rushing_yards",
        selection="Over",
        line=65.5,
        canonical_player_id="pid::test::rb_elite",
        canonical_team_id="tid::test::bills",
        magic_score=MAGIC_SCORE,
        magic_tier=MagicTier.ALIGNED_STRONG,
        magic_score_available=True,
    )
    # ─── 5 of 6 independent categories positive ──────────────────
    # 1. HISTORY (history_exact)
    mo.add(EvidenceItem(
        evidence_type=EvidenceType.HISTORICAL_EXACT_THRESHOLD,
        availability=Availability.AVAILABLE,
        sport="NFL", direction="positive", confidence=0.85,
        value=12.0, label="hit 65.5+ in 12 of last 15 games",
        source="player_game_actuals", source_class="authoritative",
        sample_size=15,
    ))
    # 2. ROLE (role_opportunity) — context
    mo.add(EvidenceItem(
        evidence_type=EvidenceType.ROLE_OPPORTUNITY,
        availability=Availability.AVAILABLE,
        sport="NFL", direction="positive", confidence=0.80,
        value=18.0, label="18 carries projected (season high)",
        source="nfl_role_registry", source_class="authoritative",
    ))
    # 3. MATCHUP — context
    mo.add(EvidenceItem(
        evidence_type=EvidenceType.MATCHUP,
        availability=Availability.AVAILABLE,
        sport="NFL", direction="positive", confidence=0.82,
        value=0.72, label="opponent run defense DVOA 29th",
        source="nfl_matchup_layer", source_class="authoritative",
    ))
    # 4. MODEL (model_family — Model/Sim/Calibration collapse to one)
    mo.add(EvidenceItem(
        evidence_type=EvidenceType.MODEL_PROBABILITY,
        availability=Availability.AVAILABLE,
        sport="NFL", direction="positive", confidence=0.90,
        value=INDEPENDENT_MODEL_PROB,
        label=f"model_prob={INDEPENDENT_MODEL_PROB} (constant across odds)",
        source="brain.sim_nfl", source_class="authoritative",
    ))
    mo.add(EvidenceItem(
        evidence_type=EvidenceType.CALIBRATED_PROBABILITY,
        availability=Availability.AVAILABLE,
        sport="NFL", direction="positive", confidence=0.88,
        value=CALIBRATED_PROB,
        label=f"calibrated_prob={CALIBRATED_PROB} (constant across odds)",
        source="calibration", source_class="authoritative",
    ))
    # 5. MARKET — the ONLY category that varies (because book_odds changed)
    implied = _american_to_implied(book_odds)
    mo.add(EvidenceItem(
        evidence_type=EvidenceType.SPORTSBOOK_CONSENSUS,
        availability=Availability.AVAILABLE,
        sport="NFL", direction="positive", confidence=0.75,
        value=implied,
        label=f"market_odds={book_odds:+.0f} implied_prob={implied:.3f}",
        source="the_odds_api", source_class="authoritative",
    ))
    return mo


def _elite_pick(book_odds: float, pid: str = "test::elite::rb") -> dict:
    """Build the elite pick dict that goes through the real
    ``apply_magic_and_apex`` + orchestrator reliability-cap math.
    ``book_odds`` varies; EVERY other field is identical across the
    matrix (that is the test's defining invariant)."""
    implied = _american_to_implied(book_odds)
    return {
        "id":                pid,
        "pick_id":           pid,
        "sport":             "NFL",
        "market":            "player_rushing_yards",
        "selection":         "Over 65.5",
        "line":              65.5,
        "book_odds":         book_odds,
        "implied_probability": implied,
        # independent model probability — DOES NOT depend on book_odds
        "model_probability":   INDEPENDENT_MODEL_PROB,
        "sim_probability":     INDEPENDENT_MODEL_PROB,
        "calibrated_probability": CALIBRATED_PROB,
        "win_probability":     INDEPENDENT_MODEL_PROB,
        "edge_percent":        (INDEPENDENT_MODEL_PROB - implied) * 100.0,
        "sportsbook":          "DraftKings",
        "book":                "draftkings",
        "lock_score":          BASE_LOCK_SCORE,
        "evidence_authority":  {
            "coverage":     UEA_COVERAGE,
            "contradictions": [],
            "ceiling":      UEA_CEILING,
            "convergence":  CONVERGENCE,
            "distribution_stability": DISTRIBUTION_STABILITY,
        },
        "bet_quality":          {"score": 0.88, "reliability": 0.90},
        "factors": {
            "evidence_quality": 90.0,
            "model_consensus":  88.0,
            "matchup_edge":     85.0,
            "role_opportunity": 82.0,
            "historical_rate":  83.0,
        },
        "no_real_book_line":    False,
        "model_only":           False,
        "hide_from_main_board": False,
        "sim_win_probability":  INDEPENDENT_MODEL_PROB,
        "simulator_type":       "nfl_sim",
    }


# ─────────────────────────────────────────────────────────────────────
# A.  CONSTANT-MODEL-PROBABILITY APEX REACHABILITY MATRIX
# ─────────────────────────────────────────────────────────────────────

def test_apex_reachability_constant_independent_model_probability():
    """Vary ONLY ``book_odds`` across +200 → -600.  Independent model
    probability MUST remain identical in every row and APEX MUST stay
    reachable (price alone cannot impose a structural Apex ceiling)."""

    rows: list[dict] = []
    for odds in ODDS_MATRIX:
        pick = _elite_pick(odds)
        mo   = _elite_mo(pick["id"], odds)
        base_before = float(pick["lock_score"])

        # Real production compute path.
        apply_magic_and_apex(pick, mo)
        final_lock = float(pick.get("lock_score") or 0.0)
        apex_eligible = bool(pick.get("apex_lock"))

        # Orchestrator reliability-cap math (NFL scope).
        eff_ceiling, has_full_evidence, limiter = _reliability_cap_math(
            base=base_before, wp=INDEPENDENT_MODEL_PROB,
            uea_cov=UEA_COVERAGE, uea_contras=[],
            uea_ceiling=UEA_CEILING,
        )
        legacy_cap = 60.0 + INDEPENDENT_MODEL_PROB * 40.0

        rows.append({
            "book_odds":                    f"{odds:+d}",
            "book_implied_probability":     round(_american_to_implied(odds), 4),
            "independent_model_probability": INDEPENDENT_MODEL_PROB,
            "calibrated_probability":       CALIBRATED_PROB,
            "uea_evidence_score":           UEA_COVERAGE,
            "legacy_wp_cap":                round(legacy_cap, 2),
            "effective_authority_ceiling":  round(eff_ceiling, 2),
            "base_lock_score":              base_before,
            "final_lock_score":             final_lock,
            "apex_eligible":                apex_eligible,
            "limiting_factor":              limiter if not apex_eligible
                                              else "none_full_evidence_apex_reached",
            "has_full_evidence":            has_full_evidence,
            "apex_block_reason":            pick.get("apex_reason"),
        })

    # ─── Invariants ─────────────────────────────────────────────────
    # 1. independent_model_probability must be byte-identical in every row.
    probs = {r["independent_model_probability"] for r in rows}
    assert probs == {INDEPENDENT_MODEL_PROB}, (
        f"independent_model_probability drifted across odds: {probs}")

    # 2. UEA coverage, ceiling, and legacy_wp_cap must be identical
    #    across every row (none of them derive from book_odds).
    assert {r["uea_evidence_score"] for r in rows} == {UEA_COVERAGE}
    assert {r["effective_authority_ceiling"] for r in rows} == {round(UEA_CEILING, 2)}
    assert {r["legacy_wp_cap"] for r in rows} == {round(60.0 + INDEPENDENT_MODEL_PROB * 40.0, 2)}

    # 3. APEX eligible in EVERY row — price alone cannot structurally
    #    cap Apex when model-prob + evidence are elite and constant.
    non_apex = [r for r in rows if not r["apex_eligible"]]
    assert not non_apex, (
        "APEX blocked by sportsbook price alone — this is the failure "
        "mode the user flagged.  Rows failing APEX:\n"
        + "\n".join(f"  {r['book_odds']:>5} → block={r['apex_block_reason']}"
                     for r in non_apex)
    )

    # Attach matrix to the test so pytest -s prints it for the report.
    print("\n=== APEX REACHABILITY MATRIX (constant model_prob=0.90) ===")
    _hdr = ("book_odds", "implied", "model_p", "calib_p", "uea", "legacy_cap",
             "eff_ceil", "base", "final", "apex")
    print(f"{_hdr[0]:>8}  {_hdr[1]:>7}  {_hdr[2]:>7}  {_hdr[3]:>7}  "
          f"{_hdr[4]:>5}  {_hdr[5]:>10}  {_hdr[6]:>8}  "
          f"{_hdr[7]:>6}  {_hdr[8]:>6}  {_hdr[9]:>6}")
    for r in rows:
        print(f"{r['book_odds']:>8}  "
              f"{r['book_implied_probability']:>7.3f}  "
              f"{r['independent_model_probability']:>7.3f}  "
              f"{r['calibrated_probability']:>7.3f}  "
              f"{r['uea_evidence_score']:>5.2f}  "
              f"{r['legacy_wp_cap']:>10.2f}  "
              f"{r['effective_authority_ceiling']:>8.2f}  "
              f"{r['base_lock_score']:>6.1f}  "
              f"{r['final_lock_score']:>6.1f}  "
              f"{'YES' if r['apex_eligible'] else 'NO':>6}")


# ─────────────────────────────────────────────────────────────────────
# B.  INVERSE CONTROL — hold price constant, vary evidence
# ─────────────────────────────────────────────────────────────────────

def _weak_mo(pick_id: str, book_odds: float) -> MagicOutput:
    """Weak-evidence MagicOutput: only MODEL + MARKET categories
    positive (= 2 categories, far below Apex floor of 5), tier
    NEUTRAL."""
    mo = MagicOutput(
        pick_id=pick_id, sport="NFL",
        market="player_rushing_yards", selection="Over", line=65.5,
        canonical_player_id="pid::test::rb_weak",
        canonical_team_id="tid::test::bills",
        magic_score=55.0,
        magic_tier=MagicTier.NEUTRAL,
        magic_score_available=True,
    )
    mo.add(EvidenceItem(
        evidence_type=EvidenceType.MODEL_PROBABILITY,
        availability=Availability.AVAILABLE, sport="NFL",
        direction="positive", confidence=0.80,
        value=INDEPENDENT_MODEL_PROB, source="brain", source_class="authoritative",
    ))
    implied = _american_to_implied(book_odds)
    mo.add(EvidenceItem(
        evidence_type=EvidenceType.SPORTSBOOK_CONSENSUS,
        availability=Availability.AVAILABLE, sport="NFL",
        direction="positive", confidence=0.70,
        value=implied, source="the_odds_api", source_class="authoritative",
    ))
    return mo


def _weak_pick(book_odds: float) -> dict:
    pick = _elite_pick(book_odds, pid="test::weak::rb")
    pick["evidence_authority"] = {
        "coverage": 0.25, "contradictions": [], "ceiling": 60.0,
    }
    pick["lock_score"] = 72.0   # below APEX floor of 97
    return pick


def test_apex_inverse_control_price_constant_evidence_varies():
    """Hold ``book_odds = -500`` constant.  Weak evidence → Apex
    BLOCKED.  Elite evidence → Apex ELIGIBLE.  Proves price alone
    does not create Apex."""
    PRICE = -500

    # Weak-evidence run
    weak_pick = _weak_pick(PRICE)
    weak_mo   = _weak_mo(weak_pick["id"], PRICE)
    apply_magic_and_apex(weak_pick, weak_mo)
    weak_apex = bool(weak_pick.get("apex_lock"))

    # Elite-evidence run (same price)
    elite_pick = _elite_pick(PRICE, pid="test::elite_control::rb")
    elite_mo   = _elite_mo(elite_pick["id"], PRICE)
    apply_magic_and_apex(elite_pick, elite_mo)
    elite_apex = bool(elite_pick.get("apex_lock"))

    assert not weak_apex, (
        f"Weak evidence @ -500 should be APEX-blocked but got "
        f"apex_lock={weak_apex} (reason={weak_pick.get('apex_reason')})"
    )
    assert elite_apex, (
        f"Elite evidence @ -500 should be APEX-eligible but got "
        f"apex_lock={elite_apex} (reason={elite_pick.get('apex_reason')})"
    )

    print("\n=== INVERSE CONTROL (price=-500 constant) ===")
    print(f"  weak evidence  → apex_eligible={weak_apex} "
          f"reason={weak_pick.get('apex_reason')}")
    print(f"  elite evidence → apex_eligible={elite_apex} "
          f"reason={elite_pick.get('apex_reason')}")


if __name__ == "__main__":
    # Allow `python -m tests.test_apex_reachability_...` for quick CLI run.
    test_apex_reachability_constant_independent_model_probability()
    test_apex_inverse_control_price_constant_evidence_varies()
    print("\nALL APEX REACHABILITY TESTS PASSED")
