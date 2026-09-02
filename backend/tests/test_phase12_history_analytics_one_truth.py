"""Phase 12 — HISTORY + ANALYTICS ONE RESULTS TRUTH invariants.

AUTHORITATIVE RESULT STORE
    settlement_events         canonical W/L/P/V ledger
    prediction_snapshots      frozen pregame truth
    HistoryProjectionService  deterministic read-only projector

Both History and Analytics MUST read from that canonical ledger (via
the projector).  Neither may reconstruct W/L/PUSH/VOID/UNRESOLVED
from mutable legacy pick fields where canonical settlement exists.

  R1. Projection uses the CANONICAL `result` from the active
      settlement_event when one exists.
  R2. LIVE / unresolved picks (no canonical settlement_event) do
      NOT project as WON/LOST/PUSH/VOID — legacy `status` is
      demoted to `unresolved` and preserved under
      `_legacy_status_without_canonical_event` for audit.
  R3. Frozen pregame overlay: line / odds / lock_score / grade /
      published_line / published_odds / rationale / evidence
      come from the snapshot and NEVER from post-game data.
  R4. PUSH ≠ VOID.  The projector never collapses one into the
      other.
  R5. Corrections (v2 supersedes v1) preserve the full lineage
      via `settlement_lineage[]` — old rows are never destroyed.
  R6. Same pick projected twice yields the same record (idempotent
      + deterministic — no state mutation across projections).
  R7. Wrong-identity guard: pick without canonical `id` receives
      `_history_projection_error = "missing_canonical_pick_id"`
      instead of being silently mis-attributed.
  R8. Analytics + History use the SAME projector — for identical
      filters/time windows, wins/losses/pushes/voids match.
  R9. Opening History does NOT mutate `picks` or `settlement_events`
      (the projector is READ-ONLY).
"""
from __future__ import annotations
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import copy
import pytest

from services.history_projection_service import (
    project_pick,
    HistoryProjectionService,
    CANONICAL_SETTLEMENT_FIELDS,
    FROZEN_PREGAME_FIELDS,
)


def _snapshot():
    return {
        "line": 8.5, "book_odds": -110,
        "sportsbook": "draftkings",
        "lock_score": 88.5,
        "published_lock_score": 88.5,
        "published_line": 8.5,
        "published_odds": -110,
        "published_grade": "Lock",
        "model_probability": 0.61,
        "sim_probability": 0.60,
        "board_version": "board-2026-06-01",
        "model_version": "mlb-v1.4",
    }


def _active_settlement(pid="p1", result="won", version=1):
    return {
        "prediction_id": pid, "is_active": True,
        "settlement_id": f"se_{pid}_v{version}",
        "settlement_version": version,
        "result": result, "source": "mlb_stats_api",
        "grader_version": "grader.v3",
        "settled_at": "2026-06-01T22:15:00Z",
        "actual_result": {"home_runs": 4, "away_runs": 5,
                            "total_runs": 9},
    }


# ── R1 · canonical result overrides everything ─────────────────
def test_projection_uses_canonical_result():
    pick = {"id": "p1", "status": "pending"}
    proj = project_pick(pick, active_event=_active_settlement(),
                          snapshot=_snapshot())
    assert proj["status"] == "won"
    assert proj["result"] == "won"
    assert proj["_canonical_settlement_present"] is True


def test_projection_carries_settlement_provenance():
    pick = {"id": "p1"}
    proj = project_pick(pick, active_event=_active_settlement(),
                          snapshot=_snapshot())
    for k in ("settlement_event_id", "settlement_version",
             "grader_version", "settlement_source", "settled_at",
             "actual_result"):
        assert k in proj


# ── R2 · LIVE with no canonical settlement demotes to unresolved
def test_live_pick_without_settlement_demotes_stale_status():
    pick = {"id": "p_live",
            "status": "won",   # stale legacy value
            "line": 8.5}
    proj = project_pick(pick, active_event=None, snapshot=_snapshot())
    assert proj["status"] == "unresolved"
    assert proj["result"] is None
    assert proj["_canonical_settlement_present"] is False
    # Legacy value preserved under audit-only key.
    assert proj["_legacy_status_without_canonical_event"] == "won"


def test_live_pick_pending_stays_pending():
    pick = {"id": "p_pending", "status": "pending"}
    proj = project_pick(pick, active_event=None)
    assert proj["status"] == "pending"
    assert proj.get("_legacy_status_without_canonical_event") is None


# ── R3 · frozen pregame overlay ────────────────────────────────
def test_pregame_snapshot_overrides_mutated_pick_fields():
    """A picks-doc that was tampered with (line moved post-game)
    still projects the FROZEN pregame line from the snapshot."""
    pick = {"id": "p1", "line": 99.0, "book_odds": -999,
            "lock_score": 12.0}
    proj = project_pick(pick, active_event=_active_settlement(),
                          snapshot=_snapshot())
    assert proj["line"] == 8.5
    assert proj["book_odds"] == -110
    assert proj["lock_score"] == 88.5


def test_pregame_snapshot_preserves_none_when_absent():
    """A field not present in the snapshot stays None — never
    fabricated from post-game data."""
    pick = {"id": "p1"}
    partial = {"line": 7.5}   # only line — no odds/lock_score
    proj = project_pick(pick, active_event=_active_settlement(),
                          snapshot=partial)
    assert proj["line"] == 7.5
    assert "book_odds" not in proj or proj.get("book_odds") is None


# ── R4 · PUSH ≠ VOID ───────────────────────────────────────────
def test_push_and_void_remain_distinct():
    pick = {"id": "p1"}
    push = project_pick(pick, active_event=_active_settlement(result="push"),
                          snapshot=_snapshot())
    void = project_pick(pick, active_event=_active_settlement(result="void"),
                          snapshot=_snapshot())
    assert push["status"] == "push" and push["result"] == "push"
    assert void["status"] == "void" and void["result"] == "void"
    assert push["status"] != void["status"]


def test_cancelled_maps_to_void_status_but_result_preserved():
    """Cancelled events reduce status to void (matches sportsbook
    semantics) but `result` preserves the original label — analytics
    can still see 'cancelled' distinct from 'void'."""
    pick = {"id": "p1"}
    proj = project_pick(pick, active_event=_active_settlement(result="cancelled"),
                          snapshot=_snapshot())
    assert proj["status"] == "void"
    assert proj["result"] == "cancelled"


# ── R5 · correction lineage preserved ──────────────────────────
def test_correction_lineage_preserved():
    prior = [_active_settlement(result="lost", version=1) |
             {"is_active": False}]
    active_v2 = _active_settlement(result="won", version=2) | \
        {"supersedes_settlement_id": "se_p1_v1",
         "old_result": "lost", "new_result": "won",
         "correction_reason": "provider_correction",
         "corrected_at": "2026-06-02T09:00:00Z"}
    proj = project_pick({"id": "p1"}, active_event=active_v2,
                          prior_events=prior, snapshot=_snapshot())
    assert proj["result"] == "won"
    assert proj["supersedes_settlement_id"] == "se_p1_v1"
    assert proj["old_result"] == "lost"
    assert proj["correction_reason"] == "provider_correction"
    # Lineage: both v1 and v2 present, sorted by version.
    lineage = proj["settlement_lineage"]
    assert len(lineage) == 2
    assert lineage[0]["settlement_version"] == 1
    assert lineage[1]["settlement_version"] == 2


# ── R6 · idempotence + no input mutation ──────────────────────
def test_projection_does_not_mutate_pick():
    original = {"id": "p1", "status": "pending"}
    snap = _snapshot()
    frozen_pick = copy.deepcopy(original)
    frozen_snap = copy.deepcopy(snap)
    project_pick(original, active_event=_active_settlement(),
                   snapshot=snap)
    assert original == frozen_pick   # input untouched
    assert snap == frozen_snap


def test_projection_is_deterministic():
    pick = {"id": "p1"}
    s = _snapshot()
    e = _active_settlement()
    p1 = project_pick(pick, active_event=e, snapshot=s)
    p2 = project_pick(pick, active_event=e, snapshot=s)
    assert p1 == p2


# ── R7 · wrong-identity guard ─────────────────────────────────
def test_missing_pick_id_flagged():
    proj = project_pick({"no": "id_here"}, active_event=None)
    assert proj.get("_history_projection_error") == \
        "missing_canonical_pick_id"


# ── R8 · same projector = same numbers ────────────────────────
def test_history_and_analytics_share_projector_class():
    """Analytics and History both instantiate HistoryProjectionService
    (or call `project_pick` directly). This test asserts the
    projector class exists and is READ-ONLY (no write methods)."""
    write_methods = [m for m in dir(HistoryProjectionService)
                     if any(prefix in m.lower()
                             for prefix in ("update", "delete",
                                             "write", "insert",
                                             "upsert", "save",
                                             "grade", "mutate"))]
    assert not write_methods, \
        f"HistoryProjectionService must be read-only: {write_methods}"


# ── R9 · canonical field surface is enumerated ────────────────
def test_canonical_settlement_fields_declared():
    for f in ("settlement_event_id", "settlement_version",
             "supersedes_settlement_id", "correction_reason",
             "grader_version", "settlement_source", "settled_at",
             "actual_result"):
        assert f in CANONICAL_SETTLEMENT_FIELDS


def test_frozen_pregame_fields_include_lock_score_and_odds():
    for f in ("line", "book_odds", "lock_score",
             "published_lock_score", "published_line",
             "published_odds", "published_grade"):
        assert f in FROZEN_PREGAME_FIELDS


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
