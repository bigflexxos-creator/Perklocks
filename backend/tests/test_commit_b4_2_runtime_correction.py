"""Commit B/4.2 — P0 runtime correction tests.

Verifies three correctness fixes:
  1. ``provider_events`` is NOT populated from projector source_rows.
  2. ``model_candidates`` / ``at_or_above_85`` instrumentation that
     cannot prove it is a fresh model output has been REMOVED (left
     open for B5).  Honest zero > false count.
  3. The REAL /api/picks/rollover route runs the product-day filter
     BEFORE select_rollover_top (so a tomorrow 99 never enters the
     ranker).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ═══════════════════════════════════════════════════════════════════
# Item 1 — provider_events no longer sourced from projector source_rows
# ═══════════════════════════════════════════════════════════════════

def test_orchestrator_does_not_populate_provider_events_from_source_rows():
    """The orchestrator must NOT call record_stage(STAGE_PROVIDER_EVENTS)
    from the HI projection source_rows loop.  Projector source rows
    are not provider events — honest zero > false count."""
    src = open(
        "/app/backend/services/pick_refresh_orchestrator.py").read()
    # STAGE_PROVIDER_EVENTS must NOT appear anywhere active.  Match
    # only non-comment lines to tolerate a historical mention in a
    # retired-defect comment.
    executable = "\n".join(
        ln for ln in src.splitlines()
        if not ln.strip().startswith("#")
    )
    assert "STAGE_PROVIDER_EVENTS" not in executable, (
        "orchestrator still records STAGE_PROVIDER_EVENTS in "
        "executable code — provider_events must be sourced ONLY "
        "from the real provider acquisition layer")


def test_orchestrator_does_not_populate_provider_events_bucket_from_source_rows():
    """The in-memory ``_funnels`` bucket must not fold HI projector
    source_rows into ``provider_events`` either."""
    src = open(
        "/app/backend/services/pick_refresh_orchestrator.py").read()
    # The retired defect line ``bucket["provider_events"] = max(...)
    # with int(_src)`` must NOT survive as executable code.
    executable = "\n".join(
        ln for ln in src.splitlines()
        if not ln.strip().startswith("#")
    )
    assert "bucket[\"provider_events\"] = max" not in executable, (
        "legacy ``bucket['provider_events'] = max(bucket.get(...), "
        "int(_src))`` must be gone — projector source_rows must NOT "
        "populate provider_events")


# ═══════════════════════════════════════════════════════════════════
# Item 2 — Misleading model_candidates / at_or_above_85 removed
# ═══════════════════════════════════════════════════════════════════

def test_orchestrator_does_not_record_model_candidates_from_classify_source():
    """``_classify_source`` carries published-legacy / replayed /
    cached rows — not proven fresh model outputs.  The stage call
    was pulled; counters now live only in the in-memory diagnostic
    bucket (not in the stage_funnel truth source)."""
    src = open(
        "/app/backend/services/pick_refresh_orchestrator.py").read()
    executable = "\n".join(
        ln for ln in src.splitlines()
        if not ln.strip().startswith("#")
    )
    for stage in ("STAGE_MODEL_CANDIDATES", "STAGE_AT_OR_ABOVE_85"):
        assert stage not in executable, (
            f"orchestrator still records {stage} in executable code "
            "— this counter cannot be truthfully derived from the "
            "classify loop; it belongs at the model/scorer function "
            "boundary (B5 scope)")


# ═══════════════════════════════════════════════════════════════════
# Item 3 — REAL route day-filter BEFORE select_rollover_top
# ═══════════════════════════════════════════════════════════════════

def test_pick_rollover_day_filter_runs_before_select_rollover_top():
    """The product-day event_time filter must execute in the route
    source BEFORE the ``select_rollover_top`` call so a future-day
    candidate never enters the ranker or the freeze pool."""
    src = open("/app/backend/routes/picks_routes.py").read()
    idx = src.index("async def pick_rollover")
    nxt = src.find("@router.", idx + 1)
    body = src[idx:nxt] if nxt > 0 else src[idx:]
    # 1. filter_today_only must appear in the pick_rollover body.
    assert "filter_today_only" in body, (
        "pick_rollover route must call filter_today_only pre-ranker")
    # 2. filter_today_only MUST come BEFORE select_rollover_top.
    i_filter = body.index("filter_today_only")
    i_select = body.index("select_rollover_top(")
    assert i_filter < i_select, (
        "filter_today_only must execute BEFORE select_rollover_top "
        f"— got filter@{i_filter} vs select@{i_select}")


def test_pick_rollover_no_future_fallback_branch_in_executable_code():
    """Executable code must not contain the retired
    ``pool = today_picks if today_picks else picks`` pattern."""
    src = open("/app/backend/routes/picks_routes.py").read()
    idx = src.index("async def pick_rollover")
    nxt = src.find("@router.", idx + 1)
    body = src[idx:nxt] if nxt > 0 else src[idx:]
    executable = "\n".join(
        ln for ln in body.splitlines()
        if not ln.strip().startswith("#")
    )
    assert "today_picks if today_picks else picks" not in executable, (
        "retired future-fallback branch still present in executable "
        "code of /api/picks/rollover")
    # Also the ``now + 24 hours`` ad-hoc day gate must be gone from
    # the executable path — replaced by product-day semantics.
    assert "cutoff = now + timedelta(hours=24)" not in executable, (
        "ad-hoc ``now + 24h`` day gate still active — replaced by "
        "product-day semantics via filter_today_only")


# ═══════════════════════════════════════════════════════════════════
# Item 4 — Real route selection fixture 94/92/tomorrow-99
# ═══════════════════════════════════════════════════════════════════

def test_real_route_fixture_94_92_excludes_tomorrow_99():
    """Simulate the route's pre-ranker + choose_rollover_locks chain
    end-to-end and prove tomorrow 99 is dropped before the ranker.
    This exercises the SAME functions (filter_today_only +
    select_rollover_top-equivalent via choose_rollover_locks) in the
    same order the route invokes them."""
    from services.rollover_slate_contract import (
        filter_today_only, choose_rollover_locks,
    )
    candidates = [
        {"event_time": "2026-06-20T18:00:00Z", "lock_score": 94,
         "canonical_event_id": "e1", "id": "today-94"},
        {"event_time": "2026-06-20T20:00:00Z", "lock_score": 92,
         "canonical_event_id": "e2", "id": "today-92"},
        {"event_time": "2026-06-21T18:00:00Z", "lock_score": 99,
         "canonical_event_id": "e3", "id": "tomorrow-99"},
    ]
    now_iso = "2026-06-20T10:00:00Z"
    # 1. Pre-ranker day filter (NEW in B4.2).
    pre_ranked = filter_today_only(candidates, as_of_iso=now_iso,
                                     drop_started=False)
    # Tomorrow 99 is removed BEFORE any ranking.
    pre_ids = {p["id"] for p in pre_ranked}
    assert pre_ids == {"today-94", "today-92"}, (
        f"tomorrow 99 must be removed before ranking; got {pre_ids}")
    # 2. Rank + dedupe (ranker never sees future-day).
    rr = choose_rollover_locks(
        pre_ranked, as_of_iso=now_iso,
        dedupe_key=lambda p: p.get("canonical_event_id"),
    )
    sel_ids = {p["id"] for p in rr["selected"]}
    assert sel_ids == {"today-94", "today-92"}
    assert "tomorrow-99" not in sel_ids


def test_real_route_11pm_today_excludes_tomorrow_8pm_within_24h():
    """11 PM today + tomorrow 8 PM (within 24h) → tomorrow still
    excluded by the pre-ranker day filter."""
    from services.rollover_slate_contract import filter_today_only
    candidates = [
        {"event_time": "2026-06-20T22:30:00Z", "lock_score": 90,
         "canonical_event_id": "e1", "id": "today-late"},
        {"event_time": "2026-06-21T20:00:00Z", "lock_score": 99,
         "canonical_event_id": "e2", "id": "tomorrow-8pm"},
    ]
    filtered = filter_today_only(
        candidates, as_of_iso="2026-06-20T23:00:00Z",
        drop_started=False,
    )
    ids = {p["id"] for p in filtered}
    assert "tomorrow-8pm" not in ids, (
        "tomorrow 8pm must be excluded despite being within 24h")


# ═══════════════════════════════════════════════════════════════════
# Item 5 — Freeze receives only today candidates (defense-in-depth)
# ═══════════════════════════════════════════════════════════════════

def test_freeze_validator_still_rejects_wrong_day_leg():
    """validate_freeze_slate remains defense-in-depth even though the
    route now pre-filters.  Prove it still catches a wrong-day leg
    if one somehow leaks through."""
    from services.rollover_slate_contract import (
        validate_freeze_slate, ROLLOVER_FUTURE_EVENT_REJECTED,
    )
    legs = [
        {"event_time": "2026-06-20T18:00:00Z"},
        {"event_time": "2026-06-21T18:00:00Z"},
    ]
    ok, failures = validate_freeze_slate(
        legs, slate_date_iso="2026-06-20T10:00:00Z")
    assert ok is False
    assert ROLLOVER_FUTURE_EVENT_REJECTED in failures


# ═══════════════════════════════════════════════════════════════════
# Item 7 — Preserve B4.1 good work
# ═══════════════════════════════════════════════════════════════════

def test_b4_1_api_visible_wiring_preserved():
    src = open("/app/backend/routes/picks_routes.py").read()
    assert "STAGE_API_VISIBLE" in src


def test_b4_1_decide_etag_response_wiring_preserved():
    src = open("/app/backend/routes/picks_routes.py").read()
    assert "decide_etag_response" in src


def test_b4_1_hi_freshness_event_time_map_preserved():
    from services.hi_freshness_verifier import _SPORT_CONSUMER_MAP
    for sport, entries in _SPORT_CONSUMER_MAP.items():
        for coll, filt, field in entries:
            assert field != "updated_at"


def test_b4_publication_lifecycle_wiring_preserved():
    src = open(
        "/app/backend/services/prediction_publication_service.py").read()
    assert "publication_rejection_lifecycle" in src
    assert "clear_publication_owned_reasons" in src
    assert "add_publication_rejection" in src


# ═══════════════════════════════════════════════════════════════════
# Preservation
# ═══════════════════════════════════════════════════════════════════

def test_preservation_still_holds_after_b4_2():
    from services.preservation_contract import snapshot
    snap = snapshot()
    assert snap.get("APEX_MIN_BASE_SCORE") == 97.0
    assert "provider_median" in (snap.get("_SYNTHETIC_ODDS_SOURCES") or [])
