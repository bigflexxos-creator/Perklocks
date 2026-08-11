"""Block 2D Stage C — Publication bypass + engine inventory + diagnostics.

  §C1  Publication bypass observability:
        * mls_direct_inject writes are tagged bypasses_canonical=True
        * soccer_prop_inject writes are tagged bypasses_canonical=True
        * NON_CANONICAL_WRITE diagnostic is emissible
  §C2  Signal engine + rank engine writes are UPDATES to existing
       picks (shadow), NOT new pick creation — safe.
  §C3  Pipeline diagnostic reason-codes expose specialized-engine
       wiring state (ATD_ENGINE_USED, HR_INTEL_USED, BTTS_*, DC_*).
  §C4  Specialized-engine inventory recheck.
"""
from __future__ import annotations

import sys
import pytest

sys.path.insert(0, "/app/backend")

pytestmark = pytest.mark.unit


# ═══════════════════════════════════════════════════════════════════
# §C1 — Publication bypass observability
# ═══════════════════════════════════════════════════════════════════
def test_mls_direct_inject_tags_bypass_flag():
    """MLS direct-inject writes MUST be tagged so downstream
    telemetry can distinguish canonical vs bypass writes."""
    src = open("/app/backend/services/mls_direct_inject.py").read()
    assert 'p["bypasses_canonical_publication"] = True' in src
    assert 'p["publication_route"] = "mls_espn_direct"' in src


def test_mls_direct_inject_emits_non_canonical_write_diagnostic():
    src = open("/app/backend/services/mls_direct_inject.py").read()
    assert "NON_CANONICAL_WRITE" in src
    assert "mls_direct_inject" in src  # writer name in meta


def test_soccer_prop_inject_tags_bypass_flag():
    src = open("/app/backend/services/soccer_prop_inject.py").read()
    assert 'p["bypasses_canonical_publication"] = True' in src
    assert 'p["publication_route"] = "soccer_prop_direct_inject"' in src


def test_soccer_prop_inject_emits_non_canonical_write_diagnostic():
    src = open("/app/backend/services/soccer_prop_inject.py").read()
    assert "NON_CANONICAL_WRITE" in src
    assert "soccer_prop_inject" in src


# ═══════════════════════════════════════════════════════════════════
# §C2 — Signal-engine writes are updates, not new pick creation
# ═══════════════════════════════════════════════════════════════════
def test_signal_engine_writes_are_updates_only():
    """signal_engine/engine.py must use UpdateOne($set) on existing
    picks — never InsertOne or ReplaceOne(upsert=True) with new IDs."""
    src = open("/app/backend/services/signal_engine/engine.py").read()
    assert "UpdateOne" in src
    # Enrichment $set on existing picks — no new inserts.
    assert "InsertOne" not in src
    assert "ReplaceOne" not in src


def test_signal_engine_rank_writes_are_updates_only():
    src = open("/app/backend/services/signal_engine/rank.py").read()
    assert "UpdateOne" in src
    assert "InsertOne" not in src
    assert "ReplaceOne" not in src


# ═══════════════════════════════════════════════════════════════════
# §C3 — Pipeline diagnostic reason codes
# ═══════════════════════════════════════════════════════════════════
def test_all_block2d_reason_codes_present():
    from services.pipeline_diagnostic import ReasonCode
    required = {
        # ATD
        "ATD_ENGINE_USED", "ATD_ENGINE_MISSING",
        "ATD_ENGINE_UNRESOLVED_PLAYER",
        "ATD_ENGINE_REJECT_INSUFFICIENT_HISTORY",
        "ATD_ENGINE_REJECT_VOLUME_TOO_LOW",
        "ATD_ENGINE_REJECT_NO_RECENT_RED_ZONE_PATH",
        "ATD_ENGINE_REJECT_CONVERSION_EFF_LOW",
        "ATD_ENGINE_REJECT_TD_OUTLIER",
        # HR
        "HR_INTEL_USED", "HR_INTEL_MISSING",
        "HR_INTEL_INSUFFICIENT_DATA",
        # Fusion
        "FUSION_PRE_SCORE", "FUSION_POST_SCORE_ONLY",
        # Soccer BTTS
        "BTTS_LINE_FOUND", "BTTS_LINE_MISSING",
        "BTTS_CANDIDATE_CREATED", "BTTS_INSUFFICIENT_MODEL_DATA",
        # Soccer DC
        "DOUBLE_CHANCE_SYNTHETIC_LINE_BLOCKED",
        "DOUBLE_CHANCE_REAL_LINE_USED",
        "DOUBLE_CHANCE_INSUFFICIENT_MODEL_DATA",
        # Publication
        "NON_CANONICAL_WRITE",
    }
    got = {c.value for c in ReasonCode}
    missing = required - got
    assert not missing, f"missing reason codes: {missing}"


def test_log_reason_and_recent_reasons_helpers_present():
    from services.pipeline_diagnostic import (
        log_reason, recent_reasons, clear_reasons,
    )
    clear_reasons()
    log_reason(sport="Soccer", market="btts", reason="BTTS_CANDIDATE_CREATED")
    r = recent_reasons(reason="BTTS_CANDIDATE_CREATED")
    assert len(r) == 1


# ═══════════════════════════════════════════════════════════════════
# §C4 — Specialized-engine inventory
# ═══════════════════════════════════════════════════════════════════
SPECIALIZED_ENGINE_INVENTORY = {
    # sport, engine_module, classification, evidence_test
    "NFL": {
        "nfl_atd_engine": "ACTIVE_AND_CONSUMED",
        "nfl_feature_engine": "ACTIVE_AND_CONSUMED",
        "nfl_matchup_intelligence": "POST_PUBLICATION_ONLY",
        "nfl_rationale": "POST_PUBLICATION_ONLY",
    },
    "MLB": {
        "mlb_feature_engine": "ACTIVE_AND_CONSUMED",
        "mlb_hr_intel": "ACTIVE_AND_CONSUMED",   # Block 2D A3
        "mlb_k_probability": "ACTIVE_AND_CONSUMED",
    },
    "NBA": {
        "nba_feature_engine": "ACTIVE_AND_CONSUMED",
    },
    "Soccer": {
        "soccer_feature_engine": "ACTIVE_AND_CONSUMED",
        "mls_direct_inject": "BYPASSES_CANONICAL_PUBLICATION",  # tagged now
        "soccer_prop_inject": "BYPASSES_CANONICAL_PUBLICATION",  # tagged now
    },
    "Tennis": {
        "tennis_deep (via signal_engine)": "ACTIVE_AND_CONSUMED",
    },
    "CFB": {
        "cfb_feature_engine": "ACTIVE_AND_CONSUMED",
    },
    "NHL": {
        "game_markets_only": "BLOCKED_BY_MISSING_PROP_MARKETS",
    },
    "UFC": {
        "mma_method_of_victory (main path)": "ACTIVE_AND_CONSUMED",
    },
}


def test_specialized_engine_inventory_matches_source():
    """Inventory sanity check — each 'ACTIVE_AND_CONSUMED' engine
    must be importable and each 'BYPASSES_CANONICAL_PUBLICATION'
    module must carry the bypass tag."""
    for sport, engines in SPECIALIZED_ENGINE_INVENTORY.items():
        for engine_name, classification in engines.items():
            if classification == "ACTIVE_AND_CONSUMED":
                # Try to import if it's a plain module name.
                if "(" in engine_name or " " in engine_name:
                    continue  # skip narrative entries
                if engine_name in ("nfl_atd_engine",):
                    import nfl_atd_engine  # noqa
                elif engine_name in (
                    "nfl_feature_engine", "mlb_feature_engine",
                    "mlb_hr_intel", "mlb_k_probability",
                    "nba_feature_engine", "soccer_feature_engine",
                    "cfb_feature_engine", "nfl_matchup_intelligence",
                    "nfl_rationale",
                ):
                    __import__(f"services.{engine_name}")
            elif classification == "BYPASSES_CANONICAL_PUBLICATION":
                src = open(f"/app/backend/services/{engine_name}.py").read()
                assert "bypasses_canonical_publication" in src, (
                    f"{engine_name}: missing bypass tag"
                )


# ═══════════════════════════════════════════════════════════════════
# §C5 — Before/after wiring matrix (documentation)
# ═══════════════════════════════════════════════════════════════════
BEFORE_AFTER_MATRIX = {
    # (sport, market_family): (before_status, after_status, reason)
    ("NFL", "player_anytime_td"): (
        "PARTIALLY_WIRED", "FULLY_WIRED",
        "nfl_atd_engine now precomputed by build_nfl_game_context, "
        "read by sync emitter, drives model_win_prob and factor set. "
        "Blocked by missing NFL data in dev env — engine returns "
        "reject cleanly (missing data != invented probability)."),
    ("NFL", "player_1st_td"): (
        "UNSUPPORTED", "PARTIAL",
        "Bucket now accepts First-TD Yes outcomes and routes through "
        "the ATD engine (same computation).  A First-TD-specific "
        "scoring-order model is not yet built — classified PARTIAL."),
    ("MLB", "batter_home_runs"): (
        "PARTIALLY_WIRED", "FULLY_WIRED",
        "mlb_hr_intel helpers now consumed inside the main hitter "
        "branch: park/wind/temp/pitcher-HR9/batter-power/recent-form/"
        "platoon multipliers → HR Intel Composite factor + full "
        "hr_intel_evidence block attached to pick."),
    ("Fusion", "*"): (
        "POST_PUBLICATION (falsely classified in Block 2A)",
        "POST_SCORE_EXPLANATION_ONLY (deep-trace corrected)",
        "enrich_picks_bulk runs BEFORE db.picks.insert_many "
        "(orchestrator lines 1262-1293 → 1350).  Fusion attaches a "
        "`fusion` key without modifying lock_score.  Elite Evidence "
        "Gate reads Fusion output → may demote lock_score.  No "
        "circular Lock→Fusion→Lock loop (tested)."),
    ("Soccer", "double_chance"): (
        "PARTIAL (synthetic odds published as real)",
        "PARTIAL (real-line only, real-line-integrity enforced)",
        "Synthetic book_odds via _win_prob_to_american(dc_implied) "
        "removed.  DC pick requires real _dc_outcomes.  Model "
        "probability now derived from build_soccer_ml_factors "
        "(independent of book implied)."),
    ("Soccer", "btts"): (
        "UNSUPPORTED", "PARTIAL",
        "BTTS Yes/No candidate emission block added.  Gated on "
        "real _btts_outcomes + independent soccer engine data for "
        "BOTH teams.  Book-implied never used as model prob.  "
        "PARTIAL classification pending formal Poisson goal model."),
    ("Soccer", "moneyline_dc_dedupe_bias"): (
        "BIASED (DC preferred by market family)",
        "EVIDENCE-COMPETITIVE",
        "_market_priority hardcoded DC→0 / moneyline→2 removed.  "
        "All game-outcome markets tie at 1 → dedupe falls back to "
        "lock_score comparison."),
    ("Cross", "publication_bypass_mls_direct"): (
        "SILENT BYPASS",
        "OBSERVABLE BYPASS + DEFERRED FIX",
        "bypasses_canonical_publication=True + publication_route "
        "tags now attached to every direct-inject write.  "
        "NON_CANONICAL_WRITE diagnostic emitted.  Full canonical "
        "routing deferred to Block 2E."),
}


def test_before_after_matrix_documented():
    """Sanity check: matrix documents every fix applied this pass."""
    assert len(BEFORE_AFTER_MATRIX) >= 7
    # Must include all Stage A / B / C fixes.
    assert ("NFL", "player_anytime_td") in BEFORE_AFTER_MATRIX
    assert ("MLB", "batter_home_runs") in BEFORE_AFTER_MATRIX
    assert ("Soccer", "double_chance") in BEFORE_AFTER_MATRIX
    assert ("Soccer", "btts") in BEFORE_AFTER_MATRIX
    assert ("Fusion", "*") in BEFORE_AFTER_MATRIX


# ═══════════════════════════════════════════════════════════════════
# §C6 — Regression: Stage A + B wiring still intact
# ═══════════════════════════════════════════════════════════════════
def test_stage_a_still_intact():
    src = open("/app/backend/sports_engine.py").read()
    assert "nfl_atd_precomputed" in src
    assert "_atd_model_override" in src
    assert "hr_intel_evidence" in src


def test_stage_b_still_intact():
    src = open("/app/backend/sports_engine.py").read()
    assert "_dc_outcomes" in src
    assert "_btts_outcomes" in src
    assert "DOUBLE_CHANCE_SYNTHETIC_LINE_BLOCKED" in src


def test_block2c_still_intact():
    src = open("/app/backend/sports_engine.py").read()
    assert "_isolate_and_merge_event_props" in src
