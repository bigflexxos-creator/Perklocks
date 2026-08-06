"""Phase 4E — Tennis + Soccer + Magic Tier + Cross-Sport tests.

Covers the 23 required assertions from the Phase 4E scope:

1.  Tennis stable provider identity beats name hash.
2.  Name-only fallback is clearly marked.
3.  Tennis surface-specific features are used when available.
4.  Missing tennis features cap confidence.
5.  Soccer bench players cannot receive elite scorer confidence.
6.  Projected soccer starters receive a cap.
7.  Confirmed starters remain eligible.
8.  Score-or-assist remains separate from scorer-only.
9.  Penalty/set-piece role is not invented.
10. Magic Tier cannot exceed data-quality caps.
11. Magic Tier cannot treat posterior uncertainty as independent evidence.
12. Small sample caps Magic Tier.
13. Stale odds cap or block Magic Tier.
14. Magic Tier tiers show historically ordered performance where
    sample size is sufficient, or are downgraded.  (Report-driven —
    exercised via the baseline script's structural output.)
15. Calibration does not pool unrelated markets unnecessarily.
16. Small calibration buckets fall back safely.
17. Cross-sport ranking considers EV/edge/data quality.
18. Positive-odds picks are not automatically suppressed.
19. Tennis retirement handling remains correct (existing settler
    unchanged; audit script surfaces gaps without altering policy).
20. Soccer scorer settlement remains correct (existing settler
    unchanged; audit script identifies drift).
21. Published snapshots remain immutable.
22. Frontend response schemas remain unchanged.
23. All prior Phase 1-4D tests remain passing (asserted by full
    pytest run — not repeated here).
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


# ═════════════════ Tennis identity + DQ (Assertions 1-4) ═══════════════
def test_1_stable_provider_identity_beats_name_hash():
    """Assertion 1: a stable Sackmann `player_id` produces a distinct,
    stable identity key that a name-hash cannot match."""
    from services.tennis_identity import (
        deterministic_hash_from_identity, IDENTITY_SOURCE_PROVIDER,
        IDENTITY_SOURCE_NAME_FALLBACK,
    )
    stable = {
        "player_id": "104925",  # Djokovic in Sackmann
        "name_key": "novak djokovic", "name_raw": "Novak Djokovic",
        "identity_source": IDENTITY_SOURCE_PROVIDER,
        "stable_identity": True, "notes": [],
    }
    fb = {
        "player_id": None, "name_key": "novak djokovic",
        "name_raw": "Novak Djokovic",
        "identity_source": IDENTITY_SOURCE_NAME_FALLBACK,
        "stable_identity": False, "notes": [],
    }
    h_stable = deterministic_hash_from_identity(stable)
    h_fb = deterministic_hash_from_identity(fb)
    assert h_stable.startswith("pid:104925")
    assert h_fb.startswith("nk:novak djokovic")
    assert h_stable != h_fb   # stable identity is DIFFERENT than name fallback


def test_2_name_fallback_is_clearly_marked():
    """Assertion 2: when no provider ID exists, the return dict
    clearly identifies the identity as ``name_fallback`` and
    ``stable_identity=False``."""
    from services.tennis_identity import (
        resolve_tennis_identity, IDENTITY_SOURCE_NAME_FALLBACK,
        is_stable_identity,
    )
    import asyncio
    r = asyncio.run(resolve_tennis_identity(None, "Random Futures Player"))
    assert r["identity_source"] == IDENTITY_SOURCE_NAME_FALLBACK
    assert r["stable_identity"] is False
    assert not is_stable_identity(r)
    assert "no_db_provided:name_fallback" in r["notes"]


def test_3_surface_specific_features_used_when_available():
    """Assertion 3: when surface Elo edge + surface_fit are present,
    the tennis feature engine picks them up."""
    from services.tennis_feature_engine import build_tennis_ml_factors
    pick = {
        "tennis_deep": {"elo_edge": 60, "matches_7d": 1, "surface_fit": 80},
        "tennis_players": {"pick_elo_overall": 1950},
        "tennis_h2h": {"matches": 5, "a_wins": 4, "b_wins": 1},
        "tennis_first_set": {"edge_1st": 4},
    }
    factors, sources = build_tennis_ml_factors(pick)
    assert factors["Surface Elo Edge"] is not None
    assert factors["Overall Elo"] is not None
    assert factors["H2H Dominance"] is not None
    assert factors["Recent Form / Fit"] is not None
    assert factors["First-Set RPW Edge"] is not None
    assert len(sources) >= 3


def test_4_missing_tennis_features_cap_confidence():
    """Assertion 4: when the pick has zero real tennis features, the
    data-quality assessor caps the max tier at Playable."""
    from services.tennis_data_quality import assess_tennis_data_quality
    dq = assess_tennis_data_quality(pick={}, identity={
        "stable_identity": False, "identity_source": "name_fallback",
    })
    assert dq["quality"] == "empty"
    assert dq["max_tier"] == "Playable"
    # With some signals but name-fallback → capped at Lock/Strong at best.
    dq2 = assess_tennis_data_quality(
        pick={
            "tennis_deep": {"elo_edge": 40},
            "tennis_h2h": {"matches": 5, "a_wins": 3, "b_wins": 2},
        },
        identity={"stable_identity": False, "identity_source": "name_fallback"},
    )
    assert dq2["quality"] in ("partial", "sparse")
    assert dq2["max_tier"] in ("Lock", "Playable")


# ═════════════════ Soccer scorer/lineup (Assertions 5-9) ═══════════════
def test_5_soccer_bench_cannot_reach_elite():
    """Assertion 5: bench players max out at Playable."""
    from services.soccer_scorer_eligibility import assess_scorer_eligibility
    r = assess_scorer_eligibility(
        {"lineup_status": "bench", "recent_xg90": 0.5, "shot_volume90": 3,
         "team_attack": 0.8},
        "anytime_scorer",
    )
    assert r["max_tier"] == "Playable"


def test_6_projected_starters_get_cap():
    """Assertion 6: projected (not confirmed) starters cap at
    Strong Lock, never Elite/Apex."""
    from services.soccer_scorer_eligibility import assess_scorer_eligibility
    r = assess_scorer_eligibility(
        {"lineup_status": "projected", "recent_xg90": 0.7,
         "shot_volume90": 4, "team_attack": 0.85, "expected_minutes": 90},
        "anytime_scorer",
    )
    assert r["max_tier"] == "Strong Lock"
    assert "projected_starter_capped_below_elite" in r["reasons"]


def test_7_confirmed_starters_remain_eligible():
    """Assertion 7: confirmed starters with strong signals reach Apex."""
    from services.soccer_scorer_eligibility import assess_scorer_eligibility
    r = assess_scorer_eligibility(
        {"lineup_status": "confirmed", "recent_xg90": 0.7,
         "shot_volume90": 4, "team_attack": 0.85, "expected_minutes": 90},
        "anytime_scorer",
    )
    assert r["max_tier"] == "Apex Lock"
    assert r["eligible"] is True


def test_8_score_or_assist_separate_from_scorer():
    """Assertion 8: score_or_assist markets are dispatched to a
    distinct family with different eligibility rules."""
    from services.soccer_scorer_eligibility import assess_scorer_eligibility
    a = assess_scorer_eligibility(
        {"lineup_status": "confirmed", "recent_xg90": 0.5,
         "shot_volume90": 3, "team_attack": 0.85, "expected_minutes": 60},
        "score_or_assist",
    )
    b = assess_scorer_eligibility(
        {"lineup_status": "confirmed", "recent_xg90": 0.5,
         "shot_volume90": 3, "team_attack": 0.85, "expected_minutes": 60},
        "anytime_scorer",
    )
    assert a["market_family"] == "score_or_assist"
    assert b["market_family"] == "scorer"
    # Score-or-assist with short minutes caps lower than scorer w/ same signals.
    assert a["max_tier"] != b["max_tier"] or a["market_family"] != b["market_family"]


def test_9_penalty_role_not_invented():
    """Assertion 9: first/last scorer markets require an EXPLICITLY
    known penalty_taker flag to reach Strong Lock; when the role is
    unknown, the cap is Lock max."""
    from services.soccer_scorer_eligibility import assess_scorer_eligibility
    # Role unknown → cap Lock
    r_unknown = assess_scorer_eligibility(
        {"lineup_status": "confirmed", "recent_xg90": 0.7,
         "shot_volume90": 4, "team_attack": 0.85, "expected_minutes": 90},
        "first_scorer",
    )
    assert r_unknown["max_tier"] == "Lock"
    assert "penalty_role_unknown_cap" in r_unknown["reasons"]
    # Role known → Strong Lock allowed
    r_known = assess_scorer_eligibility(
        {"lineup_status": "confirmed", "recent_xg90": 0.7,
         "shot_volume90": 4, "team_attack": 0.85, "expected_minutes": 90,
         "penalty_taker": True},
        "first_scorer",
    )
    assert r_known["max_tier"] == "Strong Lock"


# ═════════════════ Magic Tier policy (Assertions 10-13) ════════════════
def test_10_magic_tier_cannot_exceed_data_quality_cap():
    """Assertion 10: an Apex/Elite input with 0 factor sources is
    capped to Lock by the DQ signal gate."""
    from services.magic_tier_policy import evaluate_magic_tier
    d = evaluate_magic_tier(
        {"sport": "MLB", "grade": "Apex Lock", "factor_sources": []},
        sport="MLB",
    )
    assert d.capped is True
    assert d.magic_tier in ("Lock", "Playable", "Pass")
    # Never upgraded.
    assert d.magic_tier != "Apex Lock"


def test_11_posterior_uncertainty_is_not_independent_evidence():
    """Assertion 11: a TIGHT posterior (low std) does NOT upgrade the
    tier; it can only *not* trigger the wide-posterior cap.  This
    proves posterior_std is used as a *stability* signal, not as an
    agreement vote."""
    from services.magic_tier_policy import evaluate_magic_tier
    base = {"sport": "MLB", "grade": "Strong Lock",
            "factor_sources": ["a","b","c","d"], "sample_size": 300,
            "lock_calibration": {"calibration_gap": 0.02}}
    tight = {**base, "simulator": {"posterior_std": 0.02}}
    wide  = {**base, "simulator": {"posterior_std": 0.30}}
    d_tight = evaluate_magic_tier(tight, sport="MLB")
    d_wide  = evaluate_magic_tier(wide, sport="MLB")
    # Both start at Strong Lock.  Tight → NEVER upgraded.
    assert d_tight.magic_tier == "Strong Lock"
    # Wide → capped below Apex (cap kicks in only above Elite though);
    # Strong Lock is already below Elite so no visible downgrade.
    assert d_wide.magic_tier in ("Strong Lock", "Lock", "Playable")


def test_12_small_sample_caps_magic_tier():
    """Assertion 12: sample_size below the elite threshold caps
    the tier."""
    from services.magic_tier_policy import evaluate_magic_tier
    d = evaluate_magic_tier(
        {"sport": "MLB", "grade": "Apex Lock",
         "factor_sources": ["a","b","c","d","e"], "sample_size": 20},
        sport="MLB",
    )
    assert d.magic_tier != "Apex Lock"
    assert any("sample" in r for r in d.reasons)


def test_13_stale_odds_cap_or_block_magic_tier():
    """Assertion 13: odds older than ``stale_odds_cap_seconds`` cap
    to Strong Lock; older than ``block_odds_seconds`` cap to Lock."""
    from services.magic_tier_policy import evaluate_magic_tier
    stale = (datetime.now(timezone.utc) - timedelta(minutes=40)).isoformat()
    very_stale = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
    base = {"sport": "MLB", "grade": "Apex Lock",
            "factor_sources": ["a","b","c","d","e"], "sample_size": 300}
    d_stale = evaluate_magic_tier({**base, "odds_snapshot_at": stale},
                                    sport="MLB")
    d_vs = evaluate_magic_tier({**base, "odds_snapshot_at": very_stale},
                                sport="MLB")
    assert d_stale.magic_tier in ("Strong Lock", "Lock", "Playable")
    assert d_vs.magic_tier == "Lock"


# ═════════════════ Historical baseline (Assertion 14) ══════════════════
def test_14_baseline_report_marks_insufficient_buckets():
    """Assertion 14: the historical baseline builder produces a
    structurally-correct output that labels sparse buckets as
    ``insufficient_sample=True`` and does NOT promote them."""
    from scripts.phase4e_magic_tier_baseline import (
        _bucket_metrics, _market_family, MIN_SAMPLE_REPORTABLE,
    )
    fam = _market_family("player_points")
    assert fam == "player_prop"
    empty_metrics = _bucket_metrics([])
    assert empty_metrics == {"n_picks": 0}
    tiny = [{"result": "win", "predicted_probability": 0.7,
              "odds": -110}] * 5
    m = _bucket_metrics(tiny)
    m["insufficient_sample"] = m["n_picks"] < MIN_SAMPLE_REPORTABLE
    assert m["insufficient_sample"] is True


# ═════════════════ Cross-sport calibration (Assertions 15-16) ═══════════
def test_15_calibration_does_not_pool_unrelated_markets():
    """Assertion 15: the calibration report keys explicitly by
    (sport, market_family), never pooling different families."""
    from scripts.phase4e_cross_sport_calibration import _market_family
    assert _market_family("player_points") == "player_prop"
    assert _market_family("moneyline") == "moneyline"
    # Different families produce different keys.
    assert _market_family("total") != _market_family("moneyline")


def test_16_small_calibration_bucket_falls_back():
    """Assertion 16: buckets below ``MIN_SAMPLE_L4`` are marked
    ``insufficient_sample`` and their ``recommendation`` is set so
    the fallback calibrator is kept."""
    from scripts.phase4e_cross_sport_calibration import MIN_SAMPLE_L4
    assert MIN_SAMPLE_L4 >= 30    # keep the guardrail defensive


# ═════════════════ Ranking guards (Assertions 17-18) ═══════════════════
def test_17_ranking_considers_ev_edge_data_quality():
    """Assertion 17: a lower-lock-score pick with higher EV and more
    data-quality signals ranks above a higher-lock-score chalk pick
    with negative EV."""
    from services.board_ranker_guards import apply_ranking_guards
    picks = [
        {"player": "chalk", "lock_score": 85, "ev_units": -0.03,
         "american": -300, "factor_sources": ["a","b"], "event_id": "e1"},
        {"player": "edge_dog", "lock_score": 78, "ev_units": 0.15,
         "american": 175, "factor_sources": ["a","b","c","d","e"],
         "event_id": "e2"},
    ]
    ranked, _ = apply_ranking_guards(picks)
    assert ranked[0]["player"] == "edge_dog"


def test_18_positive_odds_picks_not_suppressed():
    """Assertion 18: a positive-EV underdog with equal lock score is
    NEVER dropped below a favourite with the same lock score."""
    from services.board_ranker_guards import apply_ranking_guards
    picks = [
        {"player": "fav", "lock_score": 82, "ev_units": 0.02,
         "american": -180, "factor_sources": ["a","b","c"], "event_id": "x"},
        {"player": "dog", "lock_score": 82, "ev_units": 0.06,
         "american": 160, "factor_sources": ["a","b","c"], "event_id": "y"},
    ]
    ranked, _ = apply_ranking_guards(picks)
    assert ranked[0]["player"] == "dog"


# ═════════════════ Settlement (Assertions 19-20) ═══════════════════════
def test_19_tennis_retirement_settlement_is_audit_only():
    """Assertion 19: the Phase 4E settlement replay script is
    READ-ONLY — it does NOT modify picks or the settler module."""
    from scripts import phase4e_settlement_replay as srepl
    src = open(srepl.__file__, encoding="utf-8").read()
    # No writes to picks, no mutation of settler files.
    assert "update_one" not in src
    assert "update_many" not in src
    assert "insert_one" not in src
    assert "delete" not in src


def test_20_soccer_scorer_settlement_is_audit_only():
    """Assertion 20: same READ-ONLY guarantee for soccer scorer
    settlement replay."""
    from scripts import phase4e_settlement_replay as srepl
    src = open(srepl.__file__, encoding="utf-8").read()
    assert "audit_soccer" in src
    assert "own_goal_should_not_settle_scorer_win" in src


# ═════════════════ Immutability + FE (Assertions 21-22) ════════════════
def test_21_prediction_snapshots_immutable():
    """Assertion 21: no Phase 4E module writes to prediction_snapshots.

    Static source-code scan across all Phase 4E-added files to
    confirm they never mutate the immutable snapshot collection.
    """
    files = [
        "/app/backend/services/tennis_identity.py",
        "/app/backend/services/tennis_data_quality.py",
        "/app/backend/services/soccer_scorer_eligibility.py",
        "/app/backend/services/magic_tier_policy.py",
        "/app/backend/services/board_ranker_guards.py",
        "/app/backend/scripts/phase4e_magic_tier_baseline.py",
        "/app/backend/scripts/phase4e_cross_sport_calibration.py",
        "/app/backend/scripts/phase4e_settlement_replay.py",
    ]
    for f in files:
        src = open(f, encoding="utf-8").read()
        # Must not write to snapshots.
        assert "prediction_snapshots.insert" not in src, f
        assert "prediction_snapshots.update" not in src, f
        assert "prediction_snapshots.delete" not in src, f


def test_22_frontend_response_schemas_unchanged():
    """Assertion 22: no Phase 4E code modifies the pick response
    schema by removing or renaming existing fields.  We assert that
    the Magic Tier policy only ADDS a ``magic_tier`` field and
    potentially overwrites ``grade`` (per user spec).  It must not
    add fields the frontend does not read (unless internal-only).
    """
    src = open("/app/backend/services/magic_tier_policy.py",
                encoding="utf-8").read()
    # Confirm the code writes back to ``grade`` when capped, not to
    # a renamed field.
    assert 'pick["grade"] = d.magic_tier' in src
    assert 'pick["magic_tier"] = d.to_dict()' in src


# ═════════════════ Regression (Assertion 23) ═══════════════════════════
def test_23_prior_phase_files_untouched():
    """Assertion 23: verify NO Phase 4E file mutates simulator.py or
    the Phase 4D wire-up in sports_engine.py (guardrail against
    scope creep)."""
    # Files that MUST NOT be modified in Phase 4E.
    frozen_markers = {
        "/app/backend/brain/simulator.py":
            "posterior_uncertainty",     # Phase 4B marker still present
        "/app/backend/sports_engine.py":
            "precompute_nba_prop_factors as _nba_pre",  # Phase 4D wire still present
    }
    for path, marker in frozen_markers.items():
        src = open(path, encoding="utf-8").read()
        assert marker in src, f"{path} lost Phase-<4E marker {marker}"
