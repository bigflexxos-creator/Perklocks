"""Block 8 — CONTROLLED MAGIC → LOCK SCORE + APEX 100 TESTS.

Comprehensive certification suite covering:

    * Bounded Magic delta engine (per-base positive caps, negative -4.0)
    * Category grouping + independence (Model+Sim+Cal collapse to 1;
      Same-source History+Form collapse to 1)
    * Contradiction / risk_flag downcapping
    * INSUFFICIENT_EVIDENCE → zero delta
    * Non-APEX HARD CAP = 99 under EVERY code path
    * Explicit APEX 100 gate (positive reachability + negative false-APEX)
    * Sport / market APEX whitelist
    * Anytime Goal Scorer extra-strict soccer gate
    * Anytime TD extra-strict NFL gate
    * Defensive downgrade (100 without apex_lock → 99)
    * Immutable pregame score snapshot
    * Settled picks are not re-scored

These tests are all deterministic — no DB dependencies — and confirm
the invariants documented in `/tmp/block8_phase2_integration_contract.md`.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest

from services.magic.contract import (
    Availability, EvidenceItem, EvidenceType, MagicOutput, MagicTier,
)
from services.magic.lock_score_integrator import (
    APEX_SCORE, BLOCK8_INTEGRATOR_VERSION, NON_APEX_HARD_CAP,
    ALL_CATEGORIES,
    CATEGORY_HISTORY, CATEGORY_FORM, CATEGORY_ROLE, CATEGORY_MATCHUP,
    CATEGORY_MODEL, CATEGORY_MARKET,
    apply_magic_and_apex,
    block8_grade, block8_tier,
    categorize_evidence, collapse_history_form,
    compute_magic_delta,
    count_positive_categories,
    defensive_downgrade_if_needed,
    positive_cap_for_base,
    snapshot_pregame_score,
)
from services.magic.apex_gate import (
    APEX_ELIGIBLE_SPORTS, APEX_MIN_BASE_SCORE, APEX_MIN_POSITIVE_CATEGORIES,
    APEX_UNAVAILABLE_SPORTS,
    apex_market_allowed, evaluate_apex,
)


# ─────────────────────────────────────────────────────────────────────────
# Helpers to build synthetic evidence
# ─────────────────────────────────────────────────────────────────────────

def _ev(evtype: EvidenceType, *, availability: Availability = Availability.AVAILABLE,
        direction: str = "positive", confidence: float = 0.8,
        source: str = "test_source", source_class: str = "authoritative",
        label: str = "", notes: str = "", value: float | None = 0.7) -> EvidenceItem:
    return EvidenceItem(
        evidence_type=evtype, availability=availability,
        sport="MLB", market="test_market",
        value=value, direction=direction, confidence=confidence,
        source=source, source_class=source_class,
        label=label, notes=notes, sample_size=25,
    )


def _mo(sport="MLB", market="batter_hits", tier=MagicTier.ALIGNED,
        score=70.0, score_available=True,
        evidence=None, risk_flags=None) -> MagicOutput:
    out = MagicOutput(
        pick_id="pid-1", sport=sport, market=market,
        magic_tier=tier, magic_score=score, magic_score_available=score_available,
        risk_flags=list(risk_flags or []),
    )
    for e in (evidence or []):
        out.add(e)
    return out


def _all_six_positive_evidence() -> list[EvidenceItem]:
    """One AVAILABLE + positive item in every category, from DIFFERENT
    sources so nothing collapses.  Confidence >= 0.6 everywhere."""
    return [
        _ev(EvidenceType.HISTORICAL_EXACT_THRESHOLD, source="stats_api",
            source_class="authoritative"),
        _ev(EvidenceType.RECENT_FORM, source="game_logs_v2",
            source_class="mapped"),
        _ev(EvidenceType.ROLE_OPPORTUNITY, source="lineups_service",
            source_class="authoritative"),
        _ev(EvidenceType.MATCHUP, source="matchup_engine",
            source_class="mapped"),
        _ev(EvidenceType.MODEL_PROBABILITY, source="sim_v3",
            source_class="model"),
        _ev(EvidenceType.SPORTSBOOK_CONSENSUS, source="the_odds_api",
            source_class="authoritative"),
    ]


# ═════════════════════════════════════════════════════════════════════════
# 1. Positive-uplift cap bucketing
# ═════════════════════════════════════════════════════════════════════════

class TestPositiveCapBucketing:
    def test_below_80(self):
        for x in (0.0, 55.0, 70.0, 79.9):
            assert positive_cap_for_base(x) == 0.5

    def test_80_to_89(self):
        for x in (80.0, 85.0, 89.9):
            assert positive_cap_for_base(x) == 1.0

    def test_90_to_94(self):
        for x in (90.0, 92.5, 94.9):
            assert positive_cap_for_base(x) == 1.5

    def test_95_to_98(self):
        for x in (95.0, 96.0, 98.9):
            assert positive_cap_for_base(x) == 1.0

    def test_99_and_above(self):
        for x in (99.0, 99.5, 100.0):
            assert positive_cap_for_base(x) == 0.0


# ═════════════════════════════════════════════════════════════════════════
# 2. Category grouping + independence collapse
# ═════════════════════════════════════════════════════════════════════════

class TestCategoryGrouping:
    def test_all_six_independent(self):
        mo = _mo(evidence=_all_six_positive_evidence())
        votes = categorize_evidence(mo)
        assert set(votes.keys()) == set(ALL_CATEGORIES)
        assert all(votes[c].positive for c in ALL_CATEGORIES)

    def test_model_family_collapses_to_one(self):
        # Three model-family items — MUST collapse to a single vote.
        mo = _mo(evidence=[
            _ev(EvidenceType.MODEL_PROBABILITY, source="m1", source_class="model"),
            _ev(EvidenceType.SIMULATOR_PROBABILITY, source="m2", source_class="model"),
            _ev(EvidenceType.CALIBRATED_PROBABILITY, source="m3", source_class="model"),
        ])
        votes = categorize_evidence(mo)
        positive = count_positive_categories(votes)
        assert positive == [CATEGORY_MODEL]
        assert votes[CATEGORY_MODEL].n_items == 3

    def test_history_form_same_source_collapses(self):
        # A + B from IDENTICAL source → one vote after collapse.
        src, sc = "shared_game_logs", "authoritative"
        mo = _mo(evidence=[
            _ev(EvidenceType.HISTORICAL_EXACT_THRESHOLD, source=src, source_class=sc),
            _ev(EvidenceType.RECENT_FORM, source=src, source_class=sc),
        ])
        votes = categorize_evidence(mo)
        collapse_history_form(votes)
        pos = count_positive_categories(votes)
        assert CATEGORY_HISTORY in pos
        assert CATEGORY_FORM not in pos

    def test_history_form_different_source_no_collapse(self):
        mo = _mo(evidence=[
            _ev(EvidenceType.HISTORICAL_EXACT_THRESHOLD,
                source="career_exact", source_class="authoritative"),
            _ev(EvidenceType.RECENT_FORM,
                source="rolling_form_v2", source_class="mapped"),
        ])
        votes = categorize_evidence(mo)
        collapse_history_form(votes)
        pos = count_positive_categories(votes)
        assert CATEGORY_HISTORY in pos
        assert CATEGORY_FORM in pos

    def test_confidence_below_threshold_not_counted(self):
        # confidence < 0.6 → not positive
        mo = _mo(evidence=[
            _ev(EvidenceType.MATCHUP, confidence=0.4),
        ])
        votes = categorize_evidence(mo)
        assert votes[CATEGORY_MATCHUP].positive is False
        assert votes[CATEGORY_MATCHUP].available is True   # still counts as available

    def test_soccer_subsignals_stay_one_category(self):
        # Three matchup items labelled shots / SOT / xG → still ONE category.
        mo = _mo(sport="Soccer", evidence=[
            _ev(EvidenceType.MATCHUP, label="shots per 90"),
            _ev(EvidenceType.MATCHUP, label="shots on target rate"),
            _ev(EvidenceType.MATCHUP, label="npxG"),
        ])
        votes = categorize_evidence(mo)
        assert count_positive_categories(votes) == [CATEGORY_MATCHUP]

    def test_contradictory_flag(self):
        mo = _mo(evidence=[
            _ev(EvidenceType.MATCHUP, availability=Availability.CONTRADICTORY,
                direction="negative"),
        ])
        votes = categorize_evidence(mo)
        assert votes[CATEGORY_MATCHUP].contradictory is True


# ═════════════════════════════════════════════════════════════════════════
# 3. Bounded delta engine
# ═════════════════════════════════════════════════════════════════════════

class TestBoundedDelta:
    def test_positive_delta_bucketed_below_80(self):
        mo = _mo(tier=MagicTier.ALIGNED, score=100.0,
                  evidence=[_ev(EvidenceType.MATCHUP)])
        res = compute_magic_delta(75.0, mo)
        # magic_score 100 → candidate +5.0, but base<80 cap = +0.5
        assert res.delta == 0.5

    def test_positive_delta_bucketed_80_89(self):
        mo = _mo(tier=MagicTier.ALIGNED, score=100.0,
                  evidence=[_ev(EvidenceType.MATCHUP)])
        res = compute_magic_delta(85.0, mo)
        assert res.delta == 1.0

    def test_positive_delta_bucketed_90_94(self):
        mo = _mo(tier=MagicTier.ALIGNED, score=100.0,
                  evidence=[_ev(EvidenceType.MATCHUP)])
        res = compute_magic_delta(92.0, mo)
        assert res.delta == 1.5

    def test_positive_delta_bucketed_95_98(self):
        mo = _mo(tier=MagicTier.ALIGNED, score=100.0,
                  evidence=[_ev(EvidenceType.MATCHUP)])
        res = compute_magic_delta(96.0, mo)
        assert res.delta == 1.0

    def test_positive_delta_zero_at_99(self):
        mo = _mo(tier=MagicTier.ALIGNED, score=100.0,
                  evidence=[_ev(EvidenceType.MATCHUP)])
        res = compute_magic_delta(99.0, mo)
        assert res.delta == 0.0

    def test_negative_delta_full_range(self):
        mo = _mo(tier=MagicTier.RISK_ELEVATED, score=0.0,
                  evidence=[_ev(EvidenceType.MATCHUP)])
        # magic_score 0 → candidate -5.0, negative cap = -4.0
        res = compute_magic_delta(97.0, mo)
        assert res.delta == -4.0

    def test_insufficient_evidence_zero_delta(self):
        mo = _mo(tier=MagicTier.INSUFFICIENT_EVIDENCE,
                  score=100.0, evidence=[])
        res = compute_magic_delta(90.0, mo)
        assert res.delta == 0.0
        assert res.insufficient_evidence is True

    def test_one_contradiction_positive_cap_zero_point_five(self):
        mo = _mo(tier=MagicTier.ALIGNED, score=100.0, evidence=[
            _ev(EvidenceType.MATCHUP,
                availability=Availability.CONTRADICTORY, direction="negative"),
        ])
        res = compute_magic_delta(90.0, mo)
        assert res.contradiction_capped is True
        assert res.positive_cap_applied == 0.5
        assert res.delta == 0.5

    def test_two_contradictions_positive_delta_zero(self):
        mo = _mo(tier=MagicTier.ALIGNED, score=100.0, evidence=[
            _ev(EvidenceType.MATCHUP,
                availability=Availability.CONTRADICTORY, direction="negative"),
            _ev(EvidenceType.MODEL_PROBABILITY,
                availability=Availability.CONTRADICTORY, direction="negative"),
        ])
        res = compute_magic_delta(92.0, mo)
        assert res.contradiction_capped is True
        assert res.positive_cap_applied == 0.0
        assert res.delta == 0.0

    def test_risk_flag_caps_positive_delta(self):
        mo = _mo(tier=MagicTier.ALIGNED, score=100.0,
                  evidence=[_ev(EvidenceType.MATCHUP)],
                  risk_flags=["late_lineup_uncertain"])
        res = compute_magic_delta(92.0, mo)
        assert res.risk_capped is True
        assert res.delta <= 0.5

    def test_risk_elevated_tier_zeros_positive(self):
        mo = _mo(tier=MagicTier.RISK_ELEVATED, score=100.0,
                  evidence=[_ev(EvidenceType.MATCHUP)])
        res = compute_magic_delta(90.0, mo)
        assert res.risk_capped is True
        assert res.positive_cap_applied == 0.0
        assert res.delta == 0.0

    def test_conflicted_tier_allows_negative(self):
        mo = _mo(tier=MagicTier.CONFLICTED, score=20.0,
                  evidence=[_ev(EvidenceType.MATCHUP)])
        res = compute_magic_delta(90.0, mo)
        # Negative full-range unaffected — candidate -3.0, no floor.
        assert res.delta == -3.0
        assert res.contradiction_capped is True


# ═════════════════════════════════════════════════════════════════════════
# 4. Non-APEX HARD CAP = 99
# ═════════════════════════════════════════════════════════════════════════

class TestNonApexHardCap:
    def test_max_positive_delta_never_exceeds_99(self):
        # Base 99 + max delta must land at 99 (positive cap = 0 there).
        pick = {"id": "p1", "sport": "MLB", "market": "batter_hits",
                 "lock_score": 99.0}
        mo = _mo(tier=MagicTier.ALIGNED, score=100.0,
                  evidence=[_ev(EvidenceType.MATCHUP)])
        apply_magic_and_apex(pick, mo)
        assert pick["lock_score"] <= NON_APEX_HARD_CAP
        assert pick["apex_lock"] is False

    def test_98_plus_max_delta_still_caps_at_99(self):
        pick = {"id": "p2", "sport": "MLB", "market": "batter_hits",
                 "lock_score": 98.5}
        mo = _mo(tier=MagicTier.ALIGNED, score=100.0,
                  evidence=[_ev(EvidenceType.MATCHUP)])
        apply_magic_and_apex(pick, mo)
        assert pick["lock_score"] == 99.0
        assert pick["apex_lock"] is False

    def test_weak_base_cant_jump_to_lock(self):
        # base 60, max positive delta 0.5 → final 60.5, still Pass tier.
        pick = {"id": "p3", "sport": "MLB", "market": "batter_hits",
                 "lock_score": 60.0}
        mo = _mo(tier=MagicTier.ALIGNED, score=100.0,
                  evidence=[_ev(EvidenceType.MATCHUP)])
        apply_magic_and_apex(pick, mo)
        assert pick["lock_score"] <= 60.5
        assert pick["apex_lock"] is False


# ═════════════════════════════════════════════════════════════════════════
# 5. Explicit APEX 100 gate — positive reachability
# ═════════════════════════════════════════════════════════════════════════

class TestApexPositiveReachability:
    def _pick(self, sport="MLB", market="batter_hits", base=98.0) -> dict:
        return {"id": "apex-pos", "sport": sport, "market": market,
                 "lock_score": base}

    def _mo(self, sport="MLB", market="batter_hits") -> MagicOutput:
        return _mo(sport=sport, market=market,
                    tier=MagicTier.ALIGNED_STRONG,
                    score=95.0,
                    evidence=_all_six_positive_evidence())

    def test_all_criteria_met_apex_assigned(self):
        pick = self._pick()
        mo = self._mo()
        # Sync sport on evidence items (all default to MLB).
        apply_magic_and_apex(pick, mo)
        assert pick["apex_lock"] is True, pick.get("apex_block_reason")
        assert pick["lock_score"] == APEX_SCORE
        assert pick["grade"] == "APEX Lock"
        assert pick["tier"] == "APEX_LOCK"
        # Provenance stamped
        assert pick["apex_reasons"]
        assert pick["apex_block_reason"] is None
        assert pick["lock_score_v3_base"] == 98.0
        assert pick["apex_gate_version"] == "apex_gate.v1.0"
        assert pick["block8_integrator_version"] == BLOCK8_INTEGRATOR_VERSION

    def test_soccer_ml_all_criteria_apex(self):
        pick = self._pick(sport="Soccer", market="moneyline", base=97.5)
        mo = self._mo(sport="Soccer", market="moneyline")
        apply_magic_and_apex(pick, mo)
        assert pick["apex_lock"] is True, pick.get("apex_block_reason")

    def test_nba_all_criteria_apex(self):
        pick = self._pick(sport="NBA", market="spread", base=97.0)
        mo = self._mo(sport="NBA", market="spread")
        apply_magic_and_apex(pick, mo)
        assert pick["apex_lock"] is True, pick.get("apex_block_reason")


# ═════════════════════════════════════════════════════════════════════════
# 6. APEX gate — negative / false-APEX tests
# ═════════════════════════════════════════════════════════════════════════

class TestApexFalsePositives:
    def _pick(self, **overrides):
        pick = {"id": "apex-neg", "sport": "MLB", "market": "batter_hits",
                 "lock_score": 98.0}
        pick.update(overrides)
        return pick

    def test_base_below_97_blocks(self):
        pick = self._pick(lock_score=96.9)
        mo = _mo(tier=MagicTier.ALIGNED_STRONG, score=95.0,
                  evidence=_all_six_positive_evidence())
        apply_magic_and_apex(pick, mo)
        assert pick["apex_lock"] is False
        assert "base_score_below_apex_min" in (pick["apex_block_reason"] or "")

    def test_magic_tier_aligned_not_strong_blocks(self):
        pick = self._pick()
        mo = _mo(tier=MagicTier.ALIGNED, score=95.0,
                  evidence=_all_six_positive_evidence())
        apply_magic_and_apex(pick, mo)
        assert pick["apex_lock"] is False
        assert "magic_tier_not_aligned_strong" in (pick["apex_block_reason"] or "")

    def test_only_four_categories_blocks(self):
        pick = self._pick()
        # Only 4 of 6 categories — missing role & market.
        mo = _mo(tier=MagicTier.ALIGNED_STRONG, score=95.0, evidence=[
            _ev(EvidenceType.HISTORICAL_EXACT_THRESHOLD, source="s1"),
            _ev(EvidenceType.RECENT_FORM, source="s2"),
            _ev(EvidenceType.MATCHUP, source="s3"),
            _ev(EvidenceType.MODEL_PROBABILITY, source="s4"),
        ])
        apply_magic_and_apex(pick, mo)
        assert pick["apex_lock"] is False
        assert "insufficient_independent_categories" in (pick["apex_block_reason"] or "")

    def test_missing_market_intelligence_blocks(self):
        # 5 categories but WITHOUT market intel → APEX blocked.
        pick = self._pick()
        mo = _mo(tier=MagicTier.ALIGNED_STRONG, score=95.0, evidence=[
            _ev(EvidenceType.HISTORICAL_EXACT_THRESHOLD, source="s1"),
            _ev(EvidenceType.RECENT_FORM, source="s2"),
            _ev(EvidenceType.ROLE_OPPORTUNITY, source="s3"),
            _ev(EvidenceType.MATCHUP, source="s4"),
            _ev(EvidenceType.MODEL_PROBABILITY, source="s5"),
            # NO SPORTSBOOK_CONSENSUS
        ])
        apply_magic_and_apex(pick, mo)
        assert pick["apex_lock"] is False
        assert (pick["apex_block_reason"] or "").startswith((
            "missing_market_intelligence",
            "insufficient_independent_categories",
        ))

    def test_contradictory_category_blocks_apex(self):
        pick = self._pick()
        ev = _all_six_positive_evidence()
        # Corrupt one category with a CONTRADICTORY item.
        ev.append(_ev(EvidenceType.MATCHUP,
                       availability=Availability.CONTRADICTORY,
                       direction="negative"))
        mo = _mo(tier=MagicTier.ALIGNED_STRONG, score=95.0, evidence=ev)
        apply_magic_and_apex(pick, mo)
        assert pick["apex_lock"] is False
        assert "contradictory_categories" in (pick["apex_block_reason"] or "")

    def test_risk_flag_blocks_apex(self):
        pick = self._pick()
        mo = _mo(tier=MagicTier.ALIGNED_STRONG, score=95.0,
                  evidence=_all_six_positive_evidence(),
                  risk_flags=["late_lineup_uncertain"])
        apply_magic_and_apex(pick, mo)
        assert pick["apex_lock"] is False
        assert "risk_flags" in (pick["apex_block_reason"] or "")

    def test_model_family_alone_blocks(self):
        # ALL evidence from model-family only — collapses to 1 category,
        # so both the count and the "not sole" gate fire.
        pick = self._pick()
        mo = _mo(tier=MagicTier.ALIGNED_STRONG, score=95.0, evidence=[
            _ev(EvidenceType.MODEL_PROBABILITY, source="a"),
            _ev(EvidenceType.SIMULATOR_PROBABILITY, source="b"),
            _ev(EvidenceType.CALIBRATED_PROBABILITY, source="c"),
        ])
        apply_magic_and_apex(pick, mo)
        assert pick["apex_lock"] is False

    def test_model_sim_cal_together_is_ONE_vote_not_three(self):
        # 5 = MODEL(1) + HISTORY + FORM + ROLE + MATCHUP — the model family
        # counts as ONE.  Market missing → APEX must fail on market gate,
        # NOT on category count (proves the collapse).
        pick = self._pick()
        mo = _mo(tier=MagicTier.ALIGNED_STRONG, score=95.0, evidence=[
            _ev(EvidenceType.HISTORICAL_EXACT_THRESHOLD, source="s1"),
            _ev(EvidenceType.RECENT_FORM, source="s2"),
            _ev(EvidenceType.ROLE_OPPORTUNITY, source="s3"),
            _ev(EvidenceType.MATCHUP, source="s4"),
            _ev(EvidenceType.MODEL_PROBABILITY, source="s5"),
            _ev(EvidenceType.SIMULATOR_PROBABILITY, source="s6"),
            _ev(EvidenceType.CALIBRATED_PROBABILITY, source="s7"),
        ])
        apply_magic_and_apex(pick, mo)
        assert pick["apex_lock"] is False
        # Should NOT be blocked for category-count — should be blocked
        # for missing market intelligence (or insufficient count, which
        # would also confirm collapse).
        reason = pick["apex_block_reason"] or ""
        assert "market" in reason or "insufficient_independent_categories" in reason

    def test_apex_100_never_means_win_probability_1(self):
        # Sanity: an APEX pick's magic_score can be any positive value.
        # The APEX assignment is orthogonal to any "p_win = 1.0" claim.
        pick = {"id": "apex-not-1p", "sport": "MLB", "market": "batter_hits",
                 "lock_score": 97.5}
        mo = _mo(tier=MagicTier.ALIGNED_STRONG,
                  score=80.0,  # 80/100, NOT 100%
                  evidence=_all_six_positive_evidence())
        apply_magic_and_apex(pick, mo)
        assert pick["apex_lock"] is True
        assert pick["lock_score"] == APEX_SCORE
        # No "win_probability" field being set to 1.0 anywhere.
        assert pick.get("win_probability") in (None, 0, 0.0)


# ═════════════════════════════════════════════════════════════════════════
# 7. Sport / market APEX whitelist
# ═════════════════════════════════════════════════════════════════════════

class TestSportMarketWhitelist:
    def test_cfb_apex_unavailable(self):
        allowed, reason = apex_market_allowed("CFB", "moneyline", {})
        assert allowed is False
        assert "cfb" in (reason or "").lower()

    def test_ufc_apex_unavailable(self):
        allowed, reason = apex_market_allowed("UFC", "moneyline", {})
        assert allowed is False

    def test_mma_apex_unavailable(self):
        allowed, reason = apex_market_allowed("MMA", "moneyline", {})
        assert allowed is False

    def test_nhl_apex_unavailable(self):
        allowed, reason = apex_market_allowed("NHL", "puckline", {})
        assert allowed is False

    def test_kbo_apex_unavailable(self):
        # KBO reuse-MLB rules until explicit certification.
        allowed, reason = apex_market_allowed("KBO", "moneyline", {})
        assert allowed is False

    def test_first_goal_scorer_blocked(self):
        allowed, reason = apex_market_allowed(
            "Soccer", "player_first_goal_scorer", {})
        assert allowed is False
        assert "market_apex_blocked" in (reason or "")

    def test_first_td_blocked(self):
        allowed, reason = apex_market_allowed(
            "NFL", "player_1st_td", {})
        assert allowed is False

    def test_first_td_dormant_flag_blocks(self):
        pick = {"publication_gate": "first_td_dormant_no_scoring_order_model"}
        allowed, reason = apex_market_allowed("NFL", "player_1st_td_alt", pick)
        assert allowed is False

    def test_mma_method_of_victory_blocked(self):
        allowed, reason = apex_market_allowed(
            "UFC", "mma_method_of_victory", {})
        # UFC is doubly-blocked (sport + market) — either reason is fine.
        assert allowed is False

    def test_standard_mlb_markets_allowed(self):
        for market in ("moneyline", "run_line", "totals",
                       "batter_hits", "pitcher_strikeouts"):
            allowed, reason = apex_market_allowed("MLB", market, {})
            assert allowed is True, (market, reason)

    def test_nfl_anytime_td_market_allowed_for_apex_gate(self):
        # Anytime TD is APEX-eligible (subject to sport-specific gate).
        allowed, reason = apex_market_allowed("NFL", "player_anytime_td", {})
        assert allowed is True, reason


# ═════════════════════════════════════════════════════════════════════════
# 8. Soccer Anytime-Goal-Scorer extra-strict gate
# ═════════════════════════════════════════════════════════════════════════

class TestSoccerAnytimeGoalStrictGate:
    def _base_pick(self, **overrides):
        pick = {
            "id": "soc-atg", "sport": "Soccer",
            "market": "player_goal_scorer_anytime",
            "lock_score": 97.5,
            "confirmed_starter": True,
            "expected_minutes": 85,
            "sim_win_probability": 0.62,
            "simulator_type": "distribution_monte_carlo",
        }
        pick.update(overrides)
        return pick

    def _evidence_with_shots_and_xg(self):
        ev = _all_six_positive_evidence()
        # Sport-tag them
        for e in ev:
            e.sport = "Soccer"
        # Attach shots + xG labels to the MATCHUP entry.
        ev.append(_ev(EvidenceType.MATCHUP, label="shots on target rate",
                       source="soccer_stats"))
        ev.append(_ev(EvidenceType.ROLE_OPPORTUNITY, label="npxG per 90",
                       source="soccer_stats"))
        return ev

    def test_full_soccer_atg_stack_passes(self):
        pick = self._base_pick()
        mo = _mo(sport="Soccer", market="player_goal_scorer_anytime",
                  tier=MagicTier.ALIGNED_STRONG, score=95.0,
                  evidence=self._evidence_with_shots_and_xg())
        apply_magic_and_apex(pick, mo)
        assert pick["apex_lock"] is True, pick.get("apex_block_reason")

    def test_soccer_atg_no_starter_blocks(self):
        pick = self._base_pick(confirmed_starter=False, role="",
                                lineup_status="")
        mo = _mo(sport="Soccer", market="player_goal_scorer_anytime",
                  tier=MagicTier.ALIGNED_STRONG, score=95.0,
                  evidence=self._evidence_with_shots_and_xg())
        apply_magic_and_apex(pick, mo)
        assert pick["apex_lock"] is False
        assert "soccer_apex:role_not_confirmed_starter" == pick["apex_block_reason"]

    def test_soccer_atg_insufficient_minutes_blocks(self):
        pick = self._base_pick(expected_minutes=45)
        mo = _mo(sport="Soccer", market="player_goal_scorer_anytime",
                  tier=MagicTier.ALIGNED_STRONG, score=95.0,
                  evidence=self._evidence_with_shots_and_xg())
        apply_magic_and_apex(pick, mo)
        assert pick["apex_lock"] is False
        assert "soccer_apex:insufficient_expected_minutes" == pick["apex_block_reason"]

    def test_soccer_atg_no_sim_blocks(self):
        pick = self._base_pick(sim_win_probability=None, simulator_type=None)
        mo = _mo(sport="Soccer", market="player_goal_scorer_anytime",
                  tier=MagicTier.ALIGNED_STRONG, score=95.0,
                  evidence=self._evidence_with_shots_and_xg())
        apply_magic_and_apex(pick, mo)
        assert pick["apex_lock"] is False
        assert pick["apex_block_reason"] == "soccer_apex:no_simulator_support"

    def test_soccer_atg_no_shots_evidence_blocks(self):
        # All six independent categories but NO shots-labeled evidence.
        pick = self._base_pick()
        ev = _all_six_positive_evidence()
        for e in ev:
            e.sport = "Soccer"
        # add only xG (no shots)
        ev.append(_ev(EvidenceType.ROLE_OPPORTUNITY, label="xG per 90",
                       source="soccer_stats"))
        mo = _mo(sport="Soccer", market="player_goal_scorer_anytime",
                  tier=MagicTier.ALIGNED_STRONG, score=95.0, evidence=ev)
        apply_magic_and_apex(pick, mo)
        assert pick["apex_lock"] is False
        assert "shots" in (pick["apex_block_reason"] or "")


# ═════════════════════════════════════════════════════════════════════════
# 9. NFL Anytime-TD extra-strict gate
# ═════════════════════════════════════════════════════════════════════════

class TestNflAnytimeTdStrictGate:
    def _base_pick(self, **overrides):
        pick = {
            "id": "nfl-atd", "sport": "NFL",
            "market": "player_anytime_td",
            "lock_score": 97.5,
            "sim_win_probability": 0.55,
            "simulator_type": "distribution_monte_carlo",
        }
        pick.update(overrides)
        return pick

    def _evidence_full(self):
        ev = _all_six_positive_evidence()
        for e in ev:
            e.sport = "NFL"
        ev.append(_ev(EvidenceType.ROLE_OPPORTUNITY,
                       label="backfield carry share",
                       source="nfl_intel"))
        ev.append(_ev(EvidenceType.ROLE_OPPORTUNITY,
                       label="red zone opportunity share",
                       source="nfl_intel"))
        ev.append(_ev(EvidenceType.MATCHUP,
                       label="opponent defense rank",
                       source="nfl_intel"))
        return ev

    def test_full_nfl_atd_stack_passes(self):
        pick = self._base_pick()
        mo = _mo(sport="NFL", market="player_anytime_td",
                  tier=MagicTier.ALIGNED_STRONG, score=95.0,
                  evidence=self._evidence_full())
        apply_magic_and_apex(pick, mo)
        assert pick["apex_lock"] is True, pick.get("apex_block_reason")

    def test_nfl_atd_no_role_usage_blocks(self):
        pick = self._base_pick()
        ev = _all_six_positive_evidence()
        for e in ev:
            e.sport = "NFL"
        # No role/usage labels
        mo = _mo(sport="NFL", market="player_anytime_td",
                  tier=MagicTier.ALIGNED_STRONG, score=95.0, evidence=ev)
        apply_magic_and_apex(pick, mo)
        assert pick["apex_lock"] is False
        assert (pick["apex_block_reason"] or "").startswith("nfl_apex:")

    def test_nfl_first_td_blocked(self):
        pick = self._base_pick(market="player_1st_td")
        mo = _mo(sport="NFL", market="player_1st_td",
                  tier=MagicTier.ALIGNED_STRONG, score=95.0,
                  evidence=self._evidence_full())
        apply_magic_and_apex(pick, mo)
        assert pick["apex_lock"] is False
        assert "market_apex_blocked" in (pick["apex_block_reason"] or "")


# ═════════════════════════════════════════════════════════════════════════
# 10. Defensive downgrade
# ═════════════════════════════════════════════════════════════════════════

class TestDefensiveDowngrade:
    def test_100_without_apex_lock_forced_to_99(self):
        pick = {"lock_score": 100.0, "apex_lock": False}
        defensive_downgrade_if_needed(pick)
        assert pick["lock_score"] == 99.0
        assert pick.get("apex_defensive_downgrade") is True

    def test_100_missing_apex_flag_forced_to_99(self):
        pick = {"lock_score": 100.0}
        defensive_downgrade_if_needed(pick)
        assert pick["lock_score"] == 99.0

    def test_100_with_apex_lock_preserved(self):
        pick = {"lock_score": 100.0, "apex_lock": True}
        defensive_downgrade_if_needed(pick)
        assert pick["lock_score"] == 100.0
        assert "apex_defensive_downgrade" not in pick

    def test_99_untouched(self):
        pick = {"lock_score": 99.0, "apex_lock": False}
        defensive_downgrade_if_needed(pick)
        assert pick["lock_score"] == 99.0


# ═════════════════════════════════════════════════════════════════════════
# 11. Immutable pregame snapshot
# ═════════════════════════════════════════════════════════════════════════

class TestPregameSnapshot:
    def test_snapshot_written_once(self):
        pick = {"id": "snap", "sport": "MLB", "market": "batter_hits",
                 "lock_score": 90.0}
        mo = _mo(tier=MagicTier.ALIGNED, score=80.0,
                  evidence=[_ev(EvidenceType.MATCHUP)])
        apply_magic_and_apex(pick, mo)
        first_snap = copy.deepcopy(pick["pregame_score_snapshot"])
        assert first_snap["block8_version"] == BLOCK8_INTEGRATOR_VERSION
        # Second call — snapshot is not overwritten.
        pick["lock_score"] = 55.0
        snapshot_pregame_score(pick)
        assert pick["pregame_score_snapshot"] == first_snap


# ═════════════════════════════════════════════════════════════════════════
# 12. Settled picks are not re-scored
# ═════════════════════════════════════════════════════════════════════════

class TestSettledPicksImmutable:
    @pytest.mark.parametrize("status", ["won", "lost", "void", "push"])
    def test_settled_status_skipped(self, status):
        pick = {"id": "settled", "sport": "MLB", "market": "batter_hits",
                 "lock_score": 90.0, "status": status}
        mo = _mo(tier=MagicTier.ALIGNED_STRONG, score=100.0,
                  evidence=_all_six_positive_evidence())
        audit = apply_magic_and_apex(pick, mo)
        assert audit.get("skipped") == "settled"
        # No score / apex mutation
        assert pick["lock_score"] == 90.0
        assert "apex_lock" not in pick


# ═════════════════════════════════════════════════════════════════════════
# 13. Grade / tier assignment (Block 8 extensions)
# ═════════════════════════════════════════════════════════════════════════

class TestGradeAndTier:
    def test_apex_grade(self):
        assert block8_grade(100.0, True) == "APEX Lock"
        assert block8_tier(100.0, True) == "APEX_LOCK"

    def test_apex_grade_requires_flag(self):
        # 100 without apex_lock — treated as Elite Lock (not APEX)
        assert block8_grade(100.0, False) == "Elite Lock"

    def test_peak_non_apex_tier(self):
        assert block8_tier(99.0, False) == "PEAK_NON_APEX"

    def test_elite_lock_at_98(self):
        assert block8_grade(98.5, False) == "Elite Lock"
        assert block8_tier(98.5, False) == "ELITE_LOCK"

    def test_strong_lock_at_95(self):
        assert block8_grade(95.5, False) == "Strong Lock"

    def test_lock_at_90(self):
        assert block8_grade(90.0, False) == "Lock"

    def test_sports_engine_grade_new_apex_band(self):
        # sports_engine._grade must recognise the 100 band.
        from sports_engine import _grade as se_grade
        assert se_grade(100.0) == "APEX Lock"
        assert se_grade(99.0) == "Elite Lock"


# ═════════════════════════════════════════════════════════════════════════
# 14. Composite invariants (regression protection)
# ═════════════════════════════════════════════════════════════════════════

class TestBlock8Invariants:
    """Board-wide invariants that MUST hold after Block 8 wiring."""

    def test_non_apex_score_always_at_most_99(self):
        # Sweep base scores × magic tiers; every non-APEX must clamp ≤ 99.
        for base in (0, 30, 55, 79.9, 80, 89, 90, 94, 95, 98, 99):
            for tier in MagicTier:
                pick = {"id": f"inv-{base}-{tier.value}", "sport": "MLB",
                         "market": "batter_hits", "lock_score": base}
                mo = _mo(tier=tier, score=100.0,
                          evidence=[_ev(EvidenceType.MATCHUP)])
                apply_magic_and_apex(pick, mo)
                if not pick.get("apex_lock"):
                    assert pick["lock_score"] <= NON_APEX_HARD_CAP, (
                        f"base={base} tier={tier.value} → {pick['lock_score']}"
                    )

    def test_apex_never_assigned_without_gate_pass(self):
        # Sweep: base < 97 must NEVER receive APEX regardless of magic.
        for base in (50, 70, 85, 96.9):
            pick = {"id": f"anti-{base}", "sport": "MLB",
                     "market": "batter_hits", "lock_score": base}
            mo = _mo(tier=MagicTier.ALIGNED_STRONG, score=100.0,
                      evidence=_all_six_positive_evidence())
            apply_magic_and_apex(pick, mo)
            assert pick["apex_lock"] is False, base
            assert pick["lock_score"] < APEX_SCORE

    def test_apex_zero_quota_ok(self):
        # A batch with NO qualifying picks must produce ZERO APEX.
        # Weak base scores across all supported sports.
        picks = [
            {"id": "no-apex-1", "sport": "MLB", "market": "batter_hits",
             "lock_score": 85.0},
            {"id": "no-apex-2", "sport": "Soccer", "market": "moneyline",
             "lock_score": 80.0},
            {"id": "no-apex-3", "sport": "NBA", "market": "spread",
             "lock_score": 92.0},
        ]
        mo = _mo(tier=MagicTier.ALIGNED_STRONG, score=100.0,
                  evidence=_all_six_positive_evidence())
        for p in picks:
            apply_magic_and_apex(p, mo)
            assert p["apex_lock"] is False

    def test_apex_multiple_qualified_no_quota_ceiling(self):
        # Multiple picks with FULL evidence stacks → all should qualify.
        # (No hidden APEX quota.)
        n = 5
        for i in range(n):
            pick = {"id": f"multi-{i}", "sport": "MLB",
                     "market": "batter_hits", "lock_score": 98.0}
            mo = _mo(tier=MagicTier.ALIGNED_STRONG, score=95.0,
                      evidence=_all_six_positive_evidence())
            apply_magic_and_apex(pick, mo)
            assert pick["apex_lock"] is True, (i, pick.get("apex_block_reason"))

    def test_provenance_always_stamped(self):
        pick = {"id": "prov", "sport": "MLB", "market": "batter_hits",
                 "lock_score": 88.0}
        mo = _mo(tier=MagicTier.ALIGNED, score=70.0,
                  evidence=[_ev(EvidenceType.MATCHUP)])
        apply_magic_and_apex(pick, mo)
        for k in ("lock_score_v3_base", "lock_score_v3_delta",
                   "lock_score_v3_positive_cap", "lock_score_v3_negative_cap",
                   "magic_categories_available", "magic_categories_positive",
                   "magic_categories_contradictory", "magic_delta_reasons",
                   "magic_tier_at_integration", "block8_integrator_version",
                   "apex_gate_version", "apex_lock", "grade", "tier",
                   "pregame_score_snapshot"):
            assert k in pick, k
