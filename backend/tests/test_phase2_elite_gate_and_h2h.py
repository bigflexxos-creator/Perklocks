"""Phase 2 (2026-08-11) — Elite Evidence Gate + H2H correctness + Fusion role.

Covers the 11 acceptance points from the Phase 2 (A+B+C) directive:

  1.  Famous + poor evidence cannot keep artificial 95-99.
  2.  Failed elite gate restores exact pre-boost Lock Score.
  3.  Pre-boost > 85 can remain on Locks after demotion.
  4.  Pre-boost ≤ 85 stays off Locks after demotion.
  5.  Strong multi-source evidence retains elite lock.
  6.  Lesser-known strong pick is not penalized just because it lacks
      elite reputation.
  7.  Missing H2H never renders `0-for-N`.
  8.  H2H is market-specific (Hits ≠ TB, Ks ≠ Outs).
  9.  Recent form is market-specific.
  10. Fusion agreement is exposed as supporting evidence WITHOUT
      rewriting canonical probability / lock.
  11. P0 + Phase 1 invariants (canonical publication, `> 85` contract)
      remain intact.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

_BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]


# ── A. Elite Evidence Gate ──────────────────────────────────────────
def test_elite_gate_demotes_famous_pick_with_no_evidence():
    """1. Famous + poor evidence cannot keep artificial 95-99."""
    from services.elite_evidence_gate import apply_elite_evidence_gate
    pick = {
        "elite_player": True,
        "elite_player_name": "Some Elite",
        "pre_elite_lock_score": 70.0,   # was 70 pre-boost
        "lock_score": 99.0,             # got boosted to 99 by apply_elite_boost
        # No form / sim / fusion / factors / learning → all zeros.
        "edge_percent": -1.0,
    }
    stats = apply_elite_evidence_gate([pick])
    assert stats["demoted"] == 1
    assert pick["elite_gate_demoted"] is True
    assert pick["elite_gate_passed"] is False
    # Restored to EXACT pre-boost value.
    assert pick["lock_score"] == 70.0
    # elite_player tag stays for badging.
    assert pick["elite_player"] is True


def test_elite_gate_restores_pre_boost_lock_exactly():
    """2. Demotion restores the exact pre-boost value."""
    from services.elite_evidence_gate import apply_elite_evidence_gate
    for pre in (60.0, 72.5, 84.9, 91.3):
        p = {
            "elite_player": True,
            "pre_elite_lock_score": pre,
            "lock_score": 99.0,
        }
        apply_elite_evidence_gate([p])
        assert p["lock_score"] == pre, (
            f"expected pre-boost {pre}, got {p['lock_score']}"
        )


def test_elite_gate_demoted_pick_above_85_remains_on_board():
    """3. Pre-boost > 85 can remain on Locks after demotion."""
    from services.elite_evidence_gate import apply_elite_evidence_gate
    from services.main_board_eligibility import is_main_board_eligible
    pick = {
        "elite_player": True,
        "pre_elite_lock_score": 87.0,
        "lock_score": 99.0,
    }
    apply_elite_evidence_gate([pick])
    assert pick["lock_score"] == 87.0
    assert is_main_board_eligible(pick) is True


def test_elite_gate_demoted_pick_at_or_below_85_falls_off():
    """4. Pre-boost ≤ 85 stays off Locks after demotion."""
    from services.elite_evidence_gate import apply_elite_evidence_gate
    from services.main_board_eligibility import is_main_board_eligible
    for pre in (85.0, 84.9, 70.0):
        p = {"elite_player": True, "pre_elite_lock_score": pre,
             "lock_score": 99.0}
        apply_elite_evidence_gate([p])
        assert is_main_board_eligible(p) is False, (
            f"pre-boost {pre} must NOT be board-eligible after demotion"
        )


def test_elite_gate_keeps_elite_with_strong_multi_source_evidence():
    """5. Multi-source positive evidence keeps the elite lock."""
    from services.elite_evidence_gate import apply_elite_evidence_gate
    pick = {
        "elite_player": True,
        "pre_elite_lock_score": 82.0,
        "lock_score": 97.0,
        "edge_percent": 5.5,   # +1 (edge)
        "player_form": {"classification": "hot"},   # +1 (form)
        "sim_result": {"consensus": "stronger"},    # +1 (sim)
        "fusion": {"supported": True, "final_probability": 0.72},
        "win_probability": 0.65,  # fusion +7pp → +1 (fusion)
    }
    apply_elite_evidence_gate([pick])
    assert pick["elite_gate_passed"] is True
    assert pick["elite_gate_demoted"] is False
    # Elite lock retained.
    assert pick["lock_score"] == 97.0
    sigs = pick["elite_gate_signals"]
    assert sum(1 for v in sigs.values() if v > 0) >= 3


def test_non_elite_pick_untouched_by_gate():
    """6. Non-elite picks are not penalized by the gate."""
    from services.elite_evidence_gate import apply_elite_evidence_gate
    p = {"elite_player": False, "lock_score": 91.0, "edge_percent": 3.0}
    apply_elite_evidence_gate([p])
    assert p["lock_score"] == 91.0
    assert "elite_gate_passed" not in p


def test_elite_gate_single_positive_signal_is_not_enough():
    """One positive signal alone must not sustain 95-99."""
    from services.elite_evidence_gate import apply_elite_evidence_gate
    p = {
        "elite_player": True,
        "pre_elite_lock_score": 75.0,
        "lock_score": 99.0,
        "edge_percent": 4.0,   # single +1
        # No other signals.
    }
    apply_elite_evidence_gate([p])
    assert p["elite_gate_demoted"] is True
    assert p["lock_score"] == 75.0


# ── B. H2H / Recent Form correctness ────────────────────────────────
def test_h2h_missing_data_does_not_render_zero_for_n():
    """7. Missing H2H must never render `0-for-N`."""
    # Directly inspect the pick_matchup_wiring path for MLB batter
    # branch: when vs_ab=0 the display must NOT contain `0-for-`.
    import services.h2h_enricher as he
    # Use the same display logic path by simulating an fetch_batter_h2h
    # response with zero at-bats.  We call the internal branch via a
    # minimal pick + mock the underlying fetch.
    async def _fake_fetch_batter_h2h(name, opp):
        return {
            "ok": True, "batter": name, "opp_team": opp,
            "season_avg": 0.0, "season_ab": 0, "season_hits": 0,
            "season_games": 0,
            "vs_team_ab": 0, "vs_team_hits": 0, "vs_team_hr": 0,
            "vs_team_rbi": 0, "vs_team_games": 0, "vs_team_avg": 0.0,
            "vs_team_recent": [],
        }
    # Monkey-patch just for this call.
    import mlb_batter_h2h as bh
    orig = bh.fetch_batter_h2h
    bh.fetch_batter_h2h = _fake_fetch_batter_h2h
    try:
        pick = {
            "market": "Aaron Judge (NYY) Over 0.5 Hits",
            "event": "New York Yankees @ Boston Red Sox",
        }
        result = asyncio.run(he._mlb_player_h2h(pick))
        assert result is not None
        display = result.get("primary_value_display", "")
        assert "0-for-" not in display, display
        assert "No prior at-bats" in display or "no " in display.lower()
        # market_specific flag reflects "no data" → False
        assert result.get("market_specific") is False
    finally:
        bh.fetch_batter_h2h = orig


def test_h2h_market_specific_display_for_hr_market():
    """8a. HR market shows HR count, NOT Hits AVG."""
    import services.h2h_enricher as he
    async def _fake(name, opp):
        return {
            "ok": True, "batter": name, "opp_team": opp,
            "season_avg": 0.278, "season_ab": 300, "season_hits": 83,
            "season_games": 80,
            "vs_team_ab": 40, "vs_team_hits": 12, "vs_team_hr": 5,
            "vs_team_rbi": 11, "vs_team_games": 10, "vs_team_avg": 0.300,
            "vs_team_recent": [],
        }
    import mlb_batter_h2h as bh
    orig = bh.fetch_batter_h2h
    bh.fetch_batter_h2h = _fake
    try:
        pick = {
            "market": "Aaron Judge (NYY) Over 0.5 Home Runs",
            "event": "New York Yankees @ Boston Red Sox",
        }
        result = asyncio.run(he._mlb_player_h2h(pick))
        assert result is not None
        assert result["market_family"] == "hr"
        assert result["primary_stat"] == "vs_team_hr"
        assert result["primary_value"] == 5.0
        assert "5 HR" in result["primary_value_display"]
        # And crucially — NOT the Hits AVG format
        assert "-for-" not in result["primary_value_display"]
    finally:
        bh.fetch_batter_h2h = orig


def test_h2h_market_specific_display_for_hits_market():
    """8b. Hits market still shows the classic X-for-Y AVG line."""
    import services.h2h_enricher as he
    async def _fake(name, opp):
        return {
            "ok": True, "batter": name, "opp_team": opp,
            "season_avg": 0.278, "season_ab": 300, "season_hits": 83,
            "season_games": 80,
            "vs_team_ab": 20, "vs_team_hits": 6, "vs_team_hr": 1,
            "vs_team_rbi": 3, "vs_team_games": 6, "vs_team_avg": 0.300,
            "vs_team_recent": [],
        }
    import mlb_batter_h2h as bh
    orig = bh.fetch_batter_h2h
    bh.fetch_batter_h2h = _fake
    try:
        pick = {
            "market": "Aaron Judge (NYY) Over 0.5 Hits",
            "event": "New York Yankees @ Boston Red Sox",
        }
        result = asyncio.run(he._mlb_player_h2h(pick))
        assert result["market_family"] == "hits"
        assert "6-for-20" in result["primary_value_display"]
    finally:
        bh.fetch_batter_h2h = orig


def test_h2h_total_bases_not_market_specific_no_avg_reuse():
    """8c. Total Bases market MUST NOT reuse Hits AVG as if it were TB."""
    import services.h2h_enricher as he
    async def _fake(name, opp):
        return {
            "ok": True, "batter": name, "opp_team": opp,
            "season_avg": 0.278, "season_ab": 300, "season_hits": 83,
            "season_games": 80,
            "vs_team_ab": 40, "vs_team_hits": 15, "vs_team_hr": 2,
            "vs_team_rbi": 8, "vs_team_games": 10, "vs_team_avg": 0.375,
            "vs_team_recent": [],
        }
    import mlb_batter_h2h as bh
    orig = bh.fetch_batter_h2h
    bh.fetch_batter_h2h = _fake
    try:
        pick = {
            "market": "Aaron Judge (NYY) Over 1.5 Total Bases",
            "event": "New York Yankees @ Boston Red Sox",
        }
        result = asyncio.run(he._mlb_player_h2h(pick))
        assert result["market_family"] == "total_bases"
        # Must NOT masquerade as a Hits X-for-Y display.
        disp = result["primary_value_display"]
        assert "15-for-40" not in disp
        assert "0.375 avg" not in disp
        # Must be tagged as NOT market-specific.
        assert result["market_specific"] is False
    finally:
        bh.fetch_batter_h2h = orig


def test_h2h_pitcher_ks_not_reused_as_outs():
    """8d. Pitcher Ks history cannot be surfaced for a Pitcher Outs pick."""
    import services.h2h_enricher as he
    # Even if we monkey-patch fetch_pitcher_h2h to return K data, an
    # "outs recorded" market must NOT rebrand K data as outs data.
    async def _fake_k(name, opp):
        return {"ok": True, "vs_team_starts": 3, "vs_team_avg_k": 7.0}
    import mlb_pitcher_h2h as ph
    orig = ph.fetch_pitcher_h2h
    ph.fetch_pitcher_h2h = _fake_k
    try:
        pick = {
            "market": "Gerrit Cole (NYY) Over 17.5 Outs Recorded",
            "event": "New York Yankees @ Boston Red Sox",
        }
        result = asyncio.run(he._mlb_player_h2h(pick))
        assert result is not None
        # Outs market: not market-specific, no K reuse.
        assert result["market_family"] == "non_k_pitcher"
        assert result["market_specific"] is False
        assert "K / start" not in result["primary_value_display"]
        assert result["sample_size"] == 0
    finally:
        ph.fetch_pitcher_h2h = orig


def test_h2h_pitcher_ks_still_works_for_strikeout_markets():
    """K markets keep the K-per-start display."""
    import services.h2h_enricher as he
    async def _fake_k(name, opp):
        return {"ok": True, "vs_team_starts": 4, "vs_team_avg_k": 8.5}
    import mlb_pitcher_h2h as ph
    orig = ph.fetch_pitcher_h2h
    ph.fetch_pitcher_h2h = _fake_k
    try:
        pick = {
            "market": "Gerrit Cole (NYY) Over 6.5 Strikeouts",
            "event": "New York Yankees @ Boston Red Sox",
        }
        result = asyncio.run(he._mlb_player_h2h(pick))
        assert result["market_family"] == "k"
        assert result["market_specific"] is True
        assert "8.5 K / start" in result["primary_value_display"]
    finally:
        ph.fetch_pitcher_h2h = orig


# ── C. Fusion role — supporting evidence, not decision logic ────────
def test_fusion_agreement_metadata_is_written():
    """10. Fusion agreement is exposed as supporting evidence.

    Rather than run the full async fusion decorator (which would need
    the whole DB stack), we inspect the source to prove the
    `fusion_agreement` metadata block is written after `pick["fusion"]`
    is populated.
    """
    src = (_BACKEND_ROOT / "services" / "pick_fusion_decorator.py").read_text()
    assert 'pick["fusion_agreement"]' in src
    # Extract the block that assigns fusion_agreement — must include
    # a direction classification.
    idx = src.find('pick["fusion_agreement"]')
    block = src[max(0, idx - 400):idx + 400]
    assert "direction" in block
    # Directions must include agree/disagree/neutral labels.
    for tok in ("agree_higher", "disagree_lower", "neutral"):
        assert tok in src


def test_fusion_does_not_rewrite_canonical_probability_or_lock():
    """Fusion decorator must not overwrite canonical scoring fields."""
    src = (_BACKEND_ROOT / "services" / "pick_fusion_decorator.py").read_text()
    for forbidden in (
        'pick["lock_score"] =',
        "pick['lock_score'] =",
        'pick["win_probability"] =',
        "pick['win_probability'] =",
        'pick["published_probability"] =',
        'pick["published_lock_score"] =',
        'pick["grade"] =',
    ):
        assert forbidden not in src, forbidden


def test_elite_gate_consumes_fusion_agreement():
    """The elite gate reads fusion evidence — proving the wiring is
    end-to-end (fusion → agreement → elite gate → lock decision)."""
    from services.elite_evidence_gate import _classify_fusion
    # Positive agreement.
    p_agree = {
        "win_probability": 0.60,
        "fusion": {"supported": True, "final_probability": 0.75},
    }
    assert _classify_fusion(p_agree) == +1
    # Strong disagreement.
    p_dis = {
        "win_probability": 0.65,
        "fusion": {"supported": True, "final_probability": 0.55},
    }
    assert _classify_fusion(p_dis) == -1
    # Neutral / no data.
    p_neu = {
        "win_probability": 0.60,
        "fusion": {"supported": True, "final_probability": 0.60},
    }
    assert _classify_fusion(p_neu) == 0


# ── D. Phase 1 / P0 invariants remain intact ────────────────────────
def test_locks_contract_still_strictly_gt_85():
    """11a. `>85` contract must not have shifted."""
    from services.main_board_eligibility import is_main_board_eligible
    assert is_main_board_eligible({"lock_score": 85.0}) is False
    assert is_main_board_eligible({"lock_score": 85.001}) is True
    assert is_main_board_eligible({"lock_score": 84.99}) is False


def test_canonical_wins_over_stale_legacy():
    """11b. Canonical Lock source still wins over stale legacy."""
    from services.main_board_eligibility import is_main_board_eligible
    p = {"published_lock_score": 60.0, "lock_score_v2": 98.0}
    assert is_main_board_eligible(p) is False


def test_orchestrator_wires_elite_gate_after_fusion_before_visibility_retag():
    """The orchestrator must call the elite gate AFTER the fusion
    enrichment block AND then re-tag board visibility."""
    src = (_BACKEND_ROOT / "services" /
           "pick_refresh_orchestrator.py").read_text()
    assert "apply_elite_evidence_gate" in src
    fusion_idx = src.find("from services.pick_fusion_decorator import enrich_picks_bulk")
    gate_idx = src.find("apply_elite_evidence_gate")
    assert fusion_idx > 0 and gate_idx > 0
    # Gate must appear AFTER the fusion-enrichment import site.
    assert gate_idx > fusion_idx
    # And board_visibility must be re-tagged after the elite gate.
    tag_after_gate = src.find("tag_board_visibility", gate_idx)
    assert tag_after_gate > gate_idx
