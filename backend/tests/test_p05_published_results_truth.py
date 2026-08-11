"""P0.5 — Regression tests locking Published-Results Truth invariants.

These tests permanently guard the P0.5 spec:

  * Outcome NEVER participates in dedupe (§3).
  * Current off_board CANNOT erase historical publication (§5).
  * Current mutable lock_score CANNOT redefine historical publication
    (§6).
  * History and Analytics consume the SAME canonical population (§7).
  * WON / LOST / PUSH / VOID / UNRESOLVED are first-class visible
    states (§8).
  * A "sweep" is only valid when losses == unresolved == pending
    == 0 (§9).
  * Missing CLV remains None — never fabricated as 0 (§13).
  * Original publication snapshot is NEVER rewritten (§17).
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, "/app/backend")

pytestmark = pytest.mark.unit


# ── Import under test ───────────────────────────────────────────
from services.published_results_truth import (
    canonical_query,
    classify_publication,
    project_publication_time_view,
    stable_publication_dedupe,
    summarise,
    verify_sweep,
    CANONICAL_STATES,
)


# ── §3  Outcome never participates in dedupe ────────────────────
def test_dedupe_never_prefers_won_over_lost():
    a = {"id": "abc", "status": "lost",
          "published_at": "2026-08-01T00:00:00Z",
          "_has_prediction_snapshot": True}
    b = {"id": "abc", "status": "won",
          "published_at": "2026-08-01T00:00:00Z",
          "_has_prediction_snapshot": False}
    out = stable_publication_dedupe([a, b])
    # Only one row survives (same identity).  Whichever it is,
    # it MUST NOT have been chosen because it won.  With equal
    # published_at, tie-break is prediction_snapshot presence.
    assert len(out) == 1
    assert out[0]["_has_prediction_snapshot"] is True
    assert out[0]["status"] == "lost"


def test_dedupe_tie_breaks_on_freshest_publication_not_outcome():
    older_win = {"id": "same", "status": "won",
                  "published_at": "2026-08-01T10:00:00Z"}
    newer_loss = {"id": "same", "status": "lost",
                   "published_at": "2026-08-02T10:00:00Z"}
    out = stable_publication_dedupe([older_win, newer_loss])
    assert len(out) == 1
    assert out[0]["status"] == "lost"  # freshest wins, outcome irrelevant


def test_dedupe_preserves_different_publications():
    a = {"id": "p1", "status": "lost",
          "published_at": "2026-08-01T00:00:00Z"}
    b = {"id": "p2", "status": "won",
          "published_at": "2026-08-01T00:00:00Z"}
    assert len(stable_publication_dedupe([a, b])) == 2


# ── §5  off_board cannot erase historical publication ───────────
def test_off_board_pick_still_classifies_as_published_when_it_has_board_stamp():
    pick = {
        "off_board": True,
        "status": "lost",
        "on_main_board_at": "2026-08-01T20:00:00Z",
    }
    assert classify_publication(pick) == "PROVEN_PUBLISHED"


def test_canonical_query_does_not_filter_by_off_board():
    q = canonical_query(days=30)
    # Serialise and search — off_board must not appear in the
    # provenance / status gate.
    import json
    text = json.dumps(q, default=str).lower()
    assert "off_board" not in text


# ── §6  Legacy lock_score >= 89 gate must not appear ────────────
def test_canonical_query_uses_no_lock_score_floor():
    q = canonical_query(days=30)
    import json
    text = json.dumps(q, default=str)
    assert "lock_score" not in text
    assert "raw_lock_score" not in text
    assert "89" not in text


# ── §8  All canonical states are first-class ────────────────────
def test_summarise_exposes_all_five_states_and_pending():
    rows = [
        {"status": "won"},
        {"status": "lost"},
        {"status": "push"},
        {"status": "void"},
        {"status": "unresolved"},
        {"status": None},  # pending
    ]
    s = summarise(rows)
    assert s["published_total"] == 6
    assert s["won"] == 1
    assert s["lost"] == 1
    assert s["push"] == 1
    assert s["void"] == 1
    assert s["unresolved"] == 1
    assert s["pending"] == 1


def test_hit_rate_denominator_excludes_push_and_void_and_unresolved():
    rows = [
        {"status": "won"},
        {"status": "won"},
        {"status": "lost"},
        {"status": "push"},
        {"status": "void"},
        {"status": "unresolved"},
    ]
    s = summarise(rows)
    # 2 wins / (2 wins + 1 loss) = 66.7 %
    assert s["hit_rate_pct"] == 66.7


# ── §9  Sweep validator ─────────────────────────────────────────
def test_sweep_is_invalid_when_a_loss_exists():
    rows = [{"status": "won"}, {"status": "won"}, {"status": "lost"}]
    sweep = verify_sweep(rows)
    assert sweep["is_valid_sweep"] is False
    assert any("loss" in r for r in sweep["reasons"])


def test_sweep_is_invalid_when_unresolved_exists():
    rows = [{"status": "won"}, {"status": "unresolved"}]
    sweep = verify_sweep(rows)
    assert sweep["is_valid_sweep"] is False
    assert any("unresolved" in r for r in sweep["reasons"])


def test_sweep_is_invalid_when_pending_exists():
    rows = [{"status": "won"}, {"status": None}]
    sweep = verify_sweep(rows)
    assert sweep["is_valid_sweep"] is False


def test_sweep_valid_when_all_verified_wins():
    rows = [{"status": "won"}, {"status": "won"}]
    sweep = verify_sweep(rows)
    assert sweep["is_valid_sweep"] is True


# ── §13  Missing CLV remains None ───────────────────────────────
def test_project_publication_time_view_missing_clv_stays_none():
    pick = {"id": "p1", "closing_odds": None, "clv_value": None}
    view = project_publication_time_view(pick)
    assert view["closing_odds"] is None
    assert view["clv_value"] is None
    assert view["clv_verified"] is False


def test_project_publication_time_view_verified_clv_flagged():
    pick = {"id": "p1", "closing_odds": -110, "clv_value": 0.03}
    view = project_publication_time_view(pick)
    assert view["closing_odds"] == -110
    assert view["clv_value"] == 0.03
    assert view["clv_verified"] is True


# ── §4/§17 publication-time frozen values ──────────────────────
def test_project_view_uses_published_line_not_current_line():
    pick = {
        "id": "p1",
        "published_line": 5.5,
        "line": 6.5,               # current mutable line
        "published_odds": -125,
        "book_odds": -130,          # current book odds
        "published_lock_score": 92,
        "lock_score": 88,          # current mutable lock score
    }
    view = project_publication_time_view(pick)
    assert view["published_line"] == 5.5
    assert view["published_odds"] == -125
    assert view["published_lock_score"] == 92


# ── §11  Classification ─────────────────────────────────────────
def test_classify_no_bet_is_proven_not_published():
    assert classify_publication({"no_bet": True}) == "PROVEN_NOT_PUBLISHED"


def test_classify_ambiguous_legacy():
    # No stamps, no snapshot flag, no explicit exclusion.
    assert classify_publication({"sport": "MLB"}) == "AMBIGUOUS_LEGACY"


def test_classify_snapshot_backed_is_proven_published():
    assert classify_publication({"_has_prediction_snapshot": True}) \
        == "PROVEN_PUBLISHED"


def test_classify_hide_from_main_board_is_not_published():
    assert classify_publication({"hide_from_main_board": True}) \
        == "PROVEN_NOT_PUBLISHED"


def test_classify_excluded_from_history_is_not_published():
    assert classify_publication({"excluded_from_history": True}) \
        == "PROVEN_NOT_PUBLISHED"


# ── §7  History and Analytics share one population ─────────────
def test_history_and_analytics_use_the_same_service():
    """Both /picks/history and /analytics/v2 must import the same
    PublishedResultsTruthService.  This is a structural test that
    guards against future drift."""
    history_src = open("/app/backend/routes/picks_routes.py").read()
    analytics_src = open("/app/backend/routes/analytics_routes.py").read()
    assert "PublishedResultsTruthService" in history_src
    assert "PublishedResultsTruthService" in analytics_src


def test_history_removed_win_biased_status_rank():
    """§3 — /picks/history must NOT reintroduce the _STATUS_RANK
    win-preferred dedupe.  This test locks the removal by scanning
    only executable code (comments describing the removal are OK)."""
    src = open("/app/backend/routes/picks_routes.py").read()
    # Strip comment lines (anything after # on a line) to avoid false
    # positives from removal-documentation.
    executable = []
    for line in src.splitlines():
        s = line.lstrip()
        if s.startswith("#"):
            continue
        # strip inline comments
        code = line.split("#", 1)[0]
        executable.append(code)
    exec_src = "\n".join(executable)
    # Guardrail: the exact tell-tale ranking dict must not appear
    # in executable code.
    assert '"won": 0, "lost": 1' not in exec_src
    assert "_STATUS_RANK" not in exec_src


# ── §17 Immutability marker ────────────────────────────────────
def test_canonical_states_enum_contract():
    assert CANONICAL_STATES == (
        "won", "lost", "push", "void", "unresolved")
