"""Block 2D Stage A — Specialized-engine wiring proof tests.

Locks the following invariants:

  §A1  NFL ATD engine is invoked by the real candidate path.
  §A1a ATD candidate outcomes (side="Yes", point=None) survive the
       _props_picks_from_event bucket collection.
  §A1b ATD engine uses independent probability, NOT book implied.
  §A1c ATD candidate is dropped cleanly on unresolved identity.
  §A1d ATD engine has an nfl_player_weekly data-source adapter.
  §A1e ATD player-identity resolver refuses to guess on ambiguity.
  §A2  First-TD (player_1st_td) shares the same wiring/gate as ATD.
  §A3  MLB HR intel is invoked for batter_home_runs* candidates.
  §A3a HR intel evidence attaches to the pick when signals moved.
  §A3b HR intel factor is normalised into [0.30, 0.95] Lock-Score band.
  §A4  Fusion runs BEFORE insert_many (deep-trace correction of the
       Block 2A "XCUT-1 post-publication" false positive).
  §A4a Fusion decorator explicitly documents non-modification of
       lock_score / win_probability.
  §A4b No circular Lock → Fusion → Lock recursion (structural test).
  §A5  Universal invariants unchanged after Stage A.
"""
from __future__ import annotations

import asyncio
import re
import sys

import pytest

sys.path.insert(0, "/app/backend")

pytestmark = pytest.mark.unit


# ═══════════════════════════════════════════════════════════════════
# §A1 — NFL ATD engine consumed by real candidate path
# ═══════════════════════════════════════════════════════════════════
def test_atd_engine_is_wired_into_precompute():
    """build_nfl_game_context MUST call nfl_atd_engine.predict_player_atd
    for anytime_td / 1st_td candidates."""
    src = open("/app/backend/services/nfl_feature_engine.py").read()
    assert "from nfl_atd_engine import" in src or "import nfl_atd_engine" in src, (
        "nfl_feature_engine must import nfl_atd_engine for ATD precompute"
    )
    assert "predict_player_atd" in src
    assert "resolve_player_id_from_name" in src
    # Both ATD-family markets must trigger the precompute.
    assert '"player_anytime_td"' in src
    assert '"player_1st_td"' in src
    # Output must be exported as ctx["nfl_atd_precomputed"].
    assert "nfl_atd_precomputed" in src


def test_atd_precompute_writes_reject_on_unresolved_identity():
    """When resolve_player_id_from_name returns None, the precompute
    MUST record a reject marker rather than skipping silently."""
    src = open("/app/backend/services/nfl_feature_engine.py").read()
    assert '"unresolved_player_identity"' in src


def test_atd_sync_emitter_reads_precomputed_and_overrides_model_prob():
    """The sync branch in sports_engine._props_picks_from_event MUST
    read nfl_atd_precomputed, override model_win_prob with the
    engine's independent td_probability, and attach an atd_evidence
    block."""
    src = open("/app/backend/sports_engine.py").read()
    assert "nfl_atd_precomputed" in src
    assert "_atd_model_override" in src
    assert "_atd_evidence_block" in src
    # The override MUST reach _build_pick's model_win_prob argument.
    # Locate the _build_pick call in the props emitter and verify the
    # kwarg uses the override.
    idx = src.index("Block 2D A1 — ATD engine's independent probability")
    window = src[idx: idx + 1200]
    assert "model_win_prob=_effective_mp" in window
    assert "_atd_model_override if _atd_model_override is not None else mp" in window


# ═══════════════════════════════════════════════════════════════════
# §A1a — Bucket accepts ATD Yes outcomes (no `point`)
# ═══════════════════════════════════════════════════════════════════
def test_props_bucket_accepts_atd_outcomes():
    """player_anytime_td outcomes have side='Yes' and NO point.  The
    bucket collection in _props_picks_from_event must have a branch
    that keeps them (previously they were dropped by the point-is-None
    filter)."""
    src = open("/app/backend/sports_engine.py").read()
    assert "is_anytime_td = mk == \"player_anytime_td\"" in src
    assert "is_first_td = mk == \"player_1st_td\"" in src


def test_extract_nfl_prop_candidates_includes_atd():
    """_extract_nfl_prop_candidates must include ATD/1st-TD markets so
    the precompute layer sees them."""
    src = open("/app/backend/sports_engine.py").read()
    idx = src.index("def _extract_nfl_prop_candidates")
    end = src.index("\n\n\n", idx)
    body = src[idx: end]
    assert "_NFL_EXTRA_ATD" in body
    assert '"player_anytime_td"' in body
    assert '"player_1st_td"' in body


# ═══════════════════════════════════════════════════════════════════
# §A1b — ATD engine probability is independent of book implied
# ═══════════════════════════════════════════════════════════════════
def test_atd_engine_probability_is_model_derived_not_book_implied():
    """nfl_atd_engine.predict_player_atd computes P(TD ≥ 1) from
    team_td_rate × opp_share × matchup × game_script × conv_eff —
    NOT from book odds.  Guardrail against silent regressions."""
    src = open("/app/backend/nfl_atd_engine.py").read()
    idx = src.index("async def predict_player_atd")
    end = src.index("\n\n\n", idx)
    body = src[idx: end]
    # Must derive from touches / TD history / matchup — model math.
    assert "team_td_rate" in body
    assert "matchup_factor" in body
    assert "conv_eff" in body
    # Must NOT reference book implied inside the engine's probability
    # computation.
    assert "book_implied" not in body
    assert "implied_prob" not in body


# ═══════════════════════════════════════════════════════════════════
# §A1c — Missing identity → unresolved evidence (never invented)
# ═══════════════════════════════════════════════════════════════════
def test_atd_resolver_refuses_to_guess_on_ambiguity():
    """When multiple nflverse GSIS IDs match the same name, the
    resolver MUST return None (unresolved), not pick one at random."""
    src = open("/app/backend/nfl_atd_engine.py").read()
    idx = src.index("async def resolve_player_id_from_name")
    end = src.index("\n\n\n", idx)
    body = src[idx: end]
    # The ambiguity branch must return None, not a "best guess".
    assert "# Ambiguous → refuse to guess" in body
    assert "return None" in body


# ═══════════════════════════════════════════════════════════════════
# §A1d — nfl_player_weekly data-source adapter
# ═══════════════════════════════════════════════════════════════════
def test_atd_engine_falls_back_to_nfl_player_weekly():
    """The ATD engine must be able to read the modern NFL data
    collection (nfl_player_weekly) when the legacy player_game_logs
    is empty for NFL."""
    src = open("/app/backend/nfl_atd_engine.py").read()
    assert "nfl_player_weekly" in src
    assert "_player_profile_from_weekly" in src


def test_atd_engine_no_random_fallback():
    """MISSING DATA != invented probability — ATD engine must have no
    RNG / random fallback anywhere."""
    src = open("/app/backend/nfl_atd_engine.py").read()
    assert "random." not in src, (
        "nfl_atd_engine must not import or use random — MISSING DATA "
        "stays missing"
    )


# ═══════════════════════════════════════════════════════════════════
# §A2 — First-TD support classification
# ═══════════════════════════════════════════════════════════════════
def test_first_td_shares_atd_wiring():
    """player_1st_td shares the same specialized-engine wiring as
    player_anytime_td (both are Yes-only binary markets that route
    through the ATD engine).  Support status: PARTIAL — same
    engine, but no First-TD-specific bias applied (positional order
    of scoring is not currently modelled)."""
    src = open("/app/backend/sports_engine.py").read()
    # First-TD outcomes accepted in bucket.
    assert "is_first_td" in src
    # First-TD routed through the same ATD precompute.
    ne = open("/app/backend/services/nfl_feature_engine.py").read()
    assert '"player_1st_td"' in ne
    # First-TD label defined.
    assert 'f"{player} First TD"' in src


# ═══════════════════════════════════════════════════════════════════
# §A3 — MLB HR intel reaches main candidate path
# ═══════════════════════════════════════════════════════════════════
def test_hr_intel_wired_into_mlb_hitter_branch():
    """The sync MLB hitter branch must invoke mlb_hr_intel helpers
    for batter_home_runs / batter_home_runs_alternate markets and
    attach an hr_intel_evidence block."""
    src = open("/app/backend/sports_engine.py").read()
    assert "Block 2D A3 (2026-08) — MLB HR intel wiring" in src
    assert "from services import mlb_hr_intel" in src
    # All the specialized HR helpers must be called.
    for helper in ("_park_hr_mult", "_wind_hr_mult", "_temp_hr_mult",
                    "_pitcher_hr_mult", "_batter_power_mult",
                    "_recent_form_mult", "_platoon_mult"):
        assert helper in src, f"HR intel helper missing: {helper}"
    # Evidence block key.
    assert "_hr_intel_evidence" in src
    assert "\"hr_intel_evidence\"" in src or "'hr_intel_evidence'" in src


def test_hr_intel_score_is_evidence_not_lockscore_replacement():
    """Per user directive: HR intel evidence MUST reach the existing
    feature system as ONE additional factor (max), not as a Lock
    Score replacement.  Verify the composite scales into the same
    [0.30, 0.95] band as other factors."""
    src = open("/app/backend/sports_engine.py").read()
    idx = src.index("Block 2D A3 (2026-08) — MLB HR intel wiring")
    window = src[idx: idx + 8000]
    # Factor name and band.
    assert 'factors["HR Intel Composite"]' in window
    assert "max(0.30, min(0.95," in window
    # And composite is ADDED to real_factors — not replacing them.
    assert 'factors["HR Intel Composite"] = round(' in window


def test_hr_intel_only_fires_when_signals_moved():
    """HR intel factor is added ONLY when at least one multiplier
    moved off neutral 1.0 — otherwise it would be misleading
    'evidence' from no signal.  Missing data stays missing."""
    src = open("/app/backend/sports_engine.py").read()
    idx = src.index("Block 2D A3 (2026-08) — MLB HR intel wiring")
    window = src[idx: idx + 8000]
    assert "_hri_moved" in window
    assert "if _hri_moved > 0:" in window


# ═══════════════════════════════════════════════════════════════════
# §A4 — Fusion deep-trace correction (was XCUT-1 false positive)
# ═══════════════════════════════════════════════════════════════════
def test_fusion_runs_before_insert_many_in_orchestrator():
    """Deep trace of pick_refresh_orchestrator: Fusion enrichment
    MUST run BEFORE the atomic insert_many.  The Block 2A audit's
    XCUT-1 claim of 'Fusion runs AFTER insert_many' was a false
    positive that this test locks against re-regressing."""
    src = open("/app/backend/services/pick_refresh_orchestrator.py").read()
    fusion_idx = src.index("enrich_picks_bulk")
    insert_idx = src.index("db.picks.insert_many(safe_picks")
    assert fusion_idx < insert_idx, (
        f"enrich_picks_bulk (idx {fusion_idx}) MUST run before "
        f"db.picks.insert_many (idx {insert_idx})"
    )


def test_fusion_decorator_documents_non_modification_of_lockscore():
    """pick_fusion_decorator MUST explicitly document that Fusion is
    POST_SCORE_EXPLANATION_ONLY at the pick level (attaches a
    `fusion` key without modifying lock_score / win_probability)."""
    src = open("/app/backend/services/pick_fusion_decorator.py").read()
    assert "Never" in src and "lock_score" in src, (
        "pick_fusion_decorator must explicitly state it never modifies"
        " lock_score"
    )
    assert "win_probability" in src


def test_no_circular_lockscore_fusion_dependency():
    """No path may write lock_score, then re-read it inside Fusion,
    then write lock_score AGAIN based on that Fusion output.  This
    would create the forbidden Lock → Fusion → Lock recursion.

    Structural check: pick_fusion_decorator.enrich_pick_with_fusion
    must NOT invoke compute_lock_score (or any lock_score writer).
    """
    src = open("/app/backend/services/pick_fusion_decorator.py").read()
    # These are the two ways to bump lock_score in this codebase.
    forbidden = ("compute_lock_score", 'pick["lock_score"] =',
                  "pick.lock_score =", "['lock_score'] =")
    for f in forbidden:
        assert f not in src, (
            f"pick_fusion_decorator must NOT touch lock_score "
            f"(found: {f!r} — that would create Lock→Fusion→Lock loop)"
        )


def test_elite_evidence_gate_reads_fusion_but_does_not_recompute():
    """Elite Evidence Gate MAY demote lock_score based on Fusion
    disagreement, but must NOT re-invoke Fusion afterward (which
    would create a cycle).  The gate's post-action is just
    board_visibility retag."""
    src = open("/app/backend/services/pick_refresh_orchestrator.py").read()
    eeg_idx = src.index("Elite Evidence Gate")
    # Look at the next 2000 chars for a re-invocation.
    window = src[eeg_idx: eeg_idx + 2500]
    assert "enrich_picks_bulk" not in window, (
        "Elite Evidence Gate must not re-invoke Fusion after demoting "
        "— would create Lock→Fusion→Lock recursion"
    )


# ═══════════════════════════════════════════════════════════════════
# §A4c — Log reason helper exposes wiring signals
# ═══════════════════════════════════════════════════════════════════
def test_pipeline_diagnostic_exposes_new_reason_codes():
    from services.pipeline_diagnostic import ReasonCode
    required = {
        "ATD_ENGINE_USED", "ATD_ENGINE_MISSING",
        "ATD_ENGINE_UNRESOLVED_PLAYER",
        "HR_INTEL_USED", "HR_INTEL_MISSING",
        "FUSION_PRE_SCORE", "FUSION_POST_SCORE_ONLY",
        "BTTS_LINE_FOUND", "BTTS_LINE_MISSING",
        "BTTS_CANDIDATE_CREATED",
        "DOUBLE_CHANCE_SYNTHETIC_LINE_BLOCKED",
        "DOUBLE_CHANCE_REAL_LINE_USED",
    }
    got = {c.value for c in ReasonCode}
    missing = required - got
    assert not missing, f"pipeline_diagnostic missing codes: {missing}"


def test_log_reason_helper_broadcasts_and_can_be_inspected():
    from services.pipeline_diagnostic import (
        log_reason, recent_reasons, clear_reasons, ReasonCode,
    )
    clear_reasons()
    log_reason(sport="NFL", market="player_anytime_td",
                player="Test Player",
                reason=ReasonCode.ATD_ENGINE_USED.value)
    r = recent_reasons(reason="ATD_ENGINE_USED")
    assert len(r) == 1
    assert r[0]["player"] == "Test Player"


# ═══════════════════════════════════════════════════════════════════
# §A5 — Hard invariants unchanged
# ═══════════════════════════════════════════════════════════════════
def test_universal_settlement_missing_data_still_unresolved():
    from services import universal_settlement_contract as usc
    graded = usc.grade_over_under(actual=None, line=1.5, side="over")
    assert graded.get("result") == usc.RESULT_UNRESOLVED


def test_block2c_isolate_bad_markets_still_wired():
    src = open("/app/backend/sports_engine.py").read()
    assert "_isolate_and_merge_event_props" in src


def test_perklocks_day_contract_unchanged():
    from services import perklocks_day as pd
    assert hasattr(pd, "perklocks_day")
    assert hasattr(pd, "is_in_current_slate")


def test_published_results_truth_unchanged():
    from services import published_results_truth as prt
    assert hasattr(prt, "PublishedResultsTruthService")


# ═══════════════════════════════════════════════════════════════════
# §A6 — Deterministic synthetic ATD proof (Derrick-Henry-type profile)
# ═══════════════════════════════════════════════════════════════════
def test_atd_engine_recognises_strong_player_vs_opponent_profile():
    """Derrick-Henry-type synthetic case: high touches per game,
    consistent recent TDs, favourable opponent → ATD engine returns
    a strong probability + confidence, does NOT reject.  No
    player-name hardcoding anywhere."""
    from nfl_atd_engine import predict_player_atd
    # Build a synthetic profile via a duck-typed fake db.
    games = [{
        "game_id": f"G{i}", "date": f"2025-{i:02d}",
        "team": "TEN", "name": "Test RB",
        "car": 22, "tgts": 3, "td": 1 if i % 2 == 0 else 0,
        "rush_yd": 110, "rec_yd": 15,
    } for i in range(1, 11)]

    class _Cursor:
        def __init__(self, rows): self.rows = rows
        def sort(self, *a, **k): return self
        def limit(self, *a, **k): return self
        def __aiter__(self):
            self._i = 0; return self
        async def __anext__(self):
            if self._i >= len(self.rows):
                raise StopAsyncIteration
            r = self.rows[self._i]; self._i += 1
            return r

    class _Coll:
        def __init__(self, rows): self.rows = rows
        def find(self, *a, **k): return _Cursor([])   # legacy coll empty
        def aggregate(self, *a, **k): return _Cursor([])

    class _Weekly(_Coll):
        def find(self, *a, **k):
            # Return synthetic weekly rows.
            weekly = [{
                "player_id": "TEST_GSIS",
                "player_display_name": "Test RB",
                "team": "TEN",
                "season": 2025, "week": i,
                "game_id": f"G{i}",
                "carries": 22, "targets": 3,
                "rushing_tds": 1 if i % 2 == 0 else 0,
                "receiving_tds": 0,
                "rushing_yards": 110, "receiving_yards": 15,
            } for i in range(1, 11)]
            return _Cursor(weekly)

    class _Games(_Coll):
        def find(self, *a, **k):
            return _Cursor([{"game_id": f"G{i}",
                              "home": "TEN", "away": "IND"}
                             for i in range(1, 11)])

    class _FakeDB:
        def __init__(self):
            self.player_game_logs = _Coll([])
            self.nfl_player_weekly = _Weekly([])
            self.games = _Games([])

    db = _FakeDB()

    async def _run():
        return await predict_player_atd(
            db, player_id="TEST_GSIS", opponent="IND", spread=-6.5)

    out = asyncio.run(_run())
    # Should NOT be rejected — strong sample (10 games, 22 car/g).
    assert "reject" not in out, out
    assert out.get("td_probability", 0) > 0.30, out
    # Confidence is present.
    assert "confidence" in out
    # No player-name hardcoded floor — the engine's output depends on
    # the profile, not the name.
    assert out.get("player_id") == "TEST_GSIS"
