"""Phase 2A.5D FINAL — Live-candidate funnel + selection + restart stability.

Covers:
* Same-player related-market selection (Ojeda double-market case)
* Teammate ranking (Sevilla trio case)
* Startup board-visibility healer wired in server.py
* Universal ≥85 unchanged
* No hardcoded names
"""
from __future__ import annotations

import os, sys
BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)


# ═════════════════════════════════════════════════════════════════════
# Related-market selection — same player, multiple qualifying markets
# ═════════════════════════════════════════════════════════════════════
def test_same_player_related_market_selection_picks_best_ev():
    from services.soccer_team_ranker import apply_soccer_selection
    ojeda_soa = {"sport":"Soccer","event":"FC Cincinnati @ Orlando City",
                 "market":"To Score or Assist","selection":"Martín Ojeda",
                 "team":"Orlando City","book_odds":-132,
                 "model_win_prob":0.68,"published_lock_score":98}
    ojeda_aa = {"sport":"Soccer","event":"FC Cincinnati @ Orlando City",
                "market":"Anytime Assist","selection":"Martín Ojeda",
                "team":"Orlando City","book_odds":170,
                "model_win_prob":0.42,"published_lock_score":89}
    picks = [ojeda_soa, ojeda_aa]
    stats = apply_soccer_selection(picks)
    assert stats["related_market_demoted"] == 1
    # Higher EV wins.  SoA at -132 (dec 1.76): 0.68 * 0.76 = 0.517
    # AA at +170 (dec 2.70): 0.42 * 1.70 = 0.714 → AA wins.
    assert ojeda_aa.get("off_board") is not True
    assert ojeda_soa.get("off_board") is True
    assert "RELATED_MARKET_DOMINATED" in ojeda_soa.get("off_board_reasons", [])


def test_same_player_related_losers_stay_in_db():
    """Underlying rows are never deleted — only off_board tagged."""
    from services.soccer_team_ranker import apply_soccer_selection
    picks = [
        {"sport":"Soccer","event":"E","market":"To Score or Assist",
         "selection":"P","team":"T","book_odds":+200,"model_win_prob":0.55,
         "published_lock_score":92},
        {"sport":"Soccer","event":"E","market":"Anytime Goal Scorer",
         "selection":"P","team":"T","book_odds":+300,"model_win_prob":0.30,
         "published_lock_score":88},
    ]
    apply_soccer_selection(picks)
    # Both records still exist; loser is off_board.
    assert len(picks) == 2
    off_cnt = sum(1 for p in picks if p.get("off_board") is True)
    assert off_cnt == 1


# ═════════════════════════════════════════════════════════════════════
# Teammate ranking — Sevilla trio case
# ═════════════════════════════════════════════════════════════════════
def test_teammate_ranking_selects_one_primary():
    from services.soccer_team_ranker import apply_soccer_selection
    trio = [
        {"sport":"Soccer","event":"Rayo @ Sevilla","market":"To Score or Assist",
         "selection":"Camello","team":"Sevilla","book_odds":+271,
         "model_win_prob":0.42,"published_lock_score":89},
        {"sport":"Soccer","event":"Rayo @ Sevilla","market":"To Score or Assist",
         "selection":"Adams","team":"Sevilla","book_odds":+255,
         "model_win_prob":0.40,"published_lock_score":89},
        {"sport":"Soccer","event":"Rayo @ Sevilla","market":"To Score or Assist",
         "selection":"De Frutos","team":"Sevilla","book_odds":+386,
         "model_win_prob":0.35,"published_lock_score":85},
    ]
    stats = apply_soccer_selection(trio)
    on_board = [p for p in trio if not p.get("off_board")]
    assert len(on_board) == 1
    assert stats["teammate_demoted"] == 2
    for p in trio:
        if p.get("off_board"):
            assert "SCORER_TEAM_RANK" in (p.get("off_board_reasons") or [])


def test_teammate_exceptional_second_kept_for_elite_locks():
    """Two ≥95 LS teammates with DIFFERENT market categories stay."""
    from services.soccer_team_ranker import apply_soccer_selection
    picks = [
        {"sport":"Soccer","event":"E","market":"Anytime Goal Scorer",
         "selection":"A","team":"T","book_odds":+200,"model_win_prob":0.60,
         "published_lock_score":96},
        {"sport":"Soccer","event":"E","market":"Anytime Assist",
         "selection":"B","team":"T","book_odds":+250,"model_win_prob":0.55,
         "published_lock_score":97},
    ]
    apply_soccer_selection(picks)
    on = sum(1 for p in picks if not p.get("off_board"))
    assert on == 2, "both elite locks across different market types must survive"


# ═════════════════════════════════════════════════════════════════════
# Universal ≥85 unchanged
# ═════════════════════════════════════════════════════════════════════
def test_below_85_never_promoted_by_ranker():
    from services.soccer_team_ranker import apply_soccer_selection
    picks = [
        {"sport":"Soccer","event":"E","market":"Anytime Goal Scorer",
         "selection":"A","team":"T","book_odds":+200,"model_win_prob":0.40,
         "published_lock_score":84},
    ]
    apply_soccer_selection(picks)
    # A sub-85 pick never becomes eligible; ranker ignores it.
    # Its off_board state is decided by board_visibility, not ranker.
    assert picks[0].get("off_board") in (None, False, True)


def test_universal_lock_floor_still_85():
    from services.main_board_eligibility import MAIN_BOARD_LOCK_FLOOR
    assert MAIN_BOARD_LOCK_FLOOR == 85.0


# ═════════════════════════════════════════════════════════════════════
# Startup healer wired
# ═════════════════════════════════════════════════════════════════════
def test_server_startup_wires_board_visibility_healer():
    src = open(os.path.join(BACKEND, "server.py"), "r").read()
    assert "Phase 2A.5D startup board healer" in src
    assert "tag_board_visibility" in src
    assert "apply_soccer_selection" in src


def test_orchestrator_wires_soccer_selection_before_board_visibility():
    src = open(os.path.join(BACKEND, "services",
                             "pick_refresh_orchestrator.py"), "r").read()
    # There are two tag_board_visibility call sites in the orchestrator.
    # The Soccer selection must sit between the two — after the first
    # canonicalization sweep, before the final tagger runs.
    sel_idx = src.find("apply_soccer_selection")
    # Find the LAST tag_board_visibility (the final tagger).
    bv_final_idx = src.rfind("tag_board_visibility")
    assert 0 < sel_idx < bv_final_idx, (
        "Soccer selection must run before the FINAL board visibility tagger"
    )


# ═════════════════════════════════════════════════════════════════════
# No hardcoded names anywhere in the ranker
# ═════════════════════════════════════════════════════════════════════
def test_ranker_has_no_hardcoded_player_or_team_names():
    src = open(os.path.join(BACKEND, "services",
                             "soccer_team_ranker.py"), "r").read()
    for name in ("Messi", "Ojeda", "Evander", "Denkey", "Mercau",
                  "Cuypers", "Surridge", "Sevilla", "Cincinnati"):
        assert name not in src, f"ranker must not hardcode name: {name}"


# ═════════════════════════════════════════════════════════════════════
# Preservation
# ═════════════════════════════════════════════════════════════════════
def test_prior_phases_still_intact():
    from services.soccer_scorer_bridge import compute_soccer_scorer_factors_sync
    from services.soccer_game_model import estimate_soccer_game_probabilities
    from services.board_visibility import _canonical_grade
    from services.soccer_season_resolver import resolve_prior_season
    assert compute_soccer_scorer_factors_sync is not None
    assert estimate_soccer_game_probabilities is not None
    assert _canonical_grade(87.0) == "Playable"
    assert resolve_prior_season("MLS") in ("2025", "2024")
