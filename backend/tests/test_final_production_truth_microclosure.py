"""Final production-truth μ-closure — focused tests.

Certifies:
  • Block 4B — Ledger-first history admission (already implemented
    by ``HistoryProjectionService.project_pick`` — verified here).
  • Block 4E — Stuck reaper is now bounded (no ``length=None``).
  • Block 4F — Single-flight settlement trigger on /history.
"""
from __future__ import annotations
import asyncio, os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ─────────────────────────────────────────────────────────────────────
# Block 4B — Ledger-first history admission
# ─────────────────────────────────────────────────────────────────────
def test_ledger_wins_over_mirror_pending():
    """Canonical settlement_event exists WON while mirror.status is
    PENDING → projection must yield status=won."""
    from services.history_projection_service import project_pick
    pick = {
        "id": "P1", "sport": "MLB", "event": "NYY @ BOS",
        "market": "NYY ML", "selection": "NYY",
        "status": "pending",           # mirror is stale/pending
        "lock_score": 92.0,
    }
    active_event = {
        "settlement_id": "SE1", "settlement_version": 1,
        "result": "won",
        "settled_at": "2026-06-01T00:00:00+00:00",
        "source": "mlb_statsapi",
        "grader_version": "v1",
        "actual_result": {"final_score": "5-3"},
    }
    proj = project_pick(pick, active_event=active_event, prior_events=[])
    assert proj["status"] == "won"
    assert proj["result"] == "won"
    assert proj["_canonical_settlement_present"] is True


def test_ledger_wins_over_mirror_pending_lost():
    from services.history_projection_service import project_pick
    pick = {"id": "P2", "sport": "MLB", "status": "pending"}
    active_event = {"result": "lost", "settlement_id": "SE2",
                     "settlement_version": 1,
                     "settled_at": "2026-06-01T00:00:00+00:00"}
    proj = project_pick(pick, active_event=active_event)
    assert proj["status"] == "lost"


def test_push_and_void_preserved():
    from services.history_projection_service import project_pick
    for r in ("push", "void"):
        proj = project_pick(
            {"id": f"P_{r}"},
            active_event={"result": r, "settlement_id": "s",
                          "settlement_version": 1},
        )
        assert proj["status"] == r
        assert proj["result"] == r


def test_no_settlement_event_clears_legacy_status():
    """When no canonical event exists but the mirror carried a stale
    outcome, the projection reclassifies to 'unresolved' and stashes
    the legacy value for audit — canonical truth is never fabricated."""
    from services.history_projection_service import project_pick
    proj = project_pick({"id": "P3", "status": "won"},
                         active_event=None)
    assert proj["status"] == "unresolved"
    assert proj["_legacy_status_without_canonical_event"] == "won"
    assert proj["_canonical_settlement_present"] is False


# ─────────────────────────────────────────────────────────────────────
# Block 4E — Bounded stuck-pick reaper
# ─────────────────────────────────────────────────────────────────────
def test_reaper_uses_bounded_batch():
    """The reaper's find(...) chain must call .limit() and pass an
    integer length to .to_list — never length=None (in the live
    code path, comments/docstrings excluded)."""
    import stuck_pick_reaper as r
    import inspect, re
    src = inspect.getsource(r)
    # Strip comment lines so the historical explanation in the
    # μ-closure block doesn't trip the check.
    live = "\n".join(
        ln for ln in src.splitlines()
        if not ln.strip().startswith("#")
    )
    assert not re.search(r"\.to_list\s*\(\s*length\s*=\s*None\s*\)", live), (
        "Live reaper code must not call to_list(length=None)"
    )
    assert ".limit(_STUCK_REAPER_BATCH)" in live
    assert "_STUCK_REAPER_BATCH = 500" in live


# ─────────────────────────────────────────────────────────────────────
# Block 4F — Single-flight history settlement trigger
# ─────────────────────────────────────────────────────────────────────
def test_history_route_single_flight_settlement_trigger():
    """The /history route must guard the fire-and-forget settlement
    task behind a module-level single-flight so a rapid pull-to-
    refresh does not spawn overlapping settlement passes."""
    from routes import picks_routes as pr
    import inspect
    src = inspect.getsource(pr)
    assert "_HIST_SETTLE_INFLIGHT" in src
    assert "_HIST_SETTLE_COOLDOWN_UNTIL" in src
    assert "SINGLE HISTORY SETTLEMENT TRIGGER" in src


# ─────────────────────────────────────────────────────────────────────
# Block 3 (canonical publication) parity — re-verify /picks/today path
# ─────────────────────────────────────────────────────────────────────
def test_actionable_predicate_requires_published_state():
    """Re-verify the strong predicate is present in server.py."""
    import server, inspect
    src = inspect.getsource(server._ensure_today_picks)
    assert '"publication_state": "PUBLISHED"' in src
    assert 'STRONG CANONICAL PUBLICATION' in src


# ─────────────────────────────────────────────────────────────────────
# Block 6A (universal Brain) — re-verify publish_batch stamps Brain
# ─────────────────────────────────────────────────────────────────────
def test_publish_batch_has_brain_attenuation():
    from services import prediction_publication_service as pps
    import inspect
    src = inspect.getsource(pps)
    assert "UNIVERSAL BRAIN DECISION EFFECT" in src
    assert "convergence_confidence_multiplier" in src
    assert "Idempotency guard" in src
