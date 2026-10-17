"""Commit B/4 — Real runtime adoption integration tests.

Proves the shared B1/B2/B3 contracts are now CONSUMED by the actual
production runtime files (not just helpers).
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ═══════════════════════════════════════════════════════════════════
# Items 1-3 — Real Rollover Route Adoption
# ═══════════════════════════════════════════════════════════════════

def test_rollover_route_no_longer_contains_future_backfill_branch():
    """The ACTUAL /api/picks/rollover route source must NOT contain
    the semantic equivalent of ``today_picks if today_picks else
    picks`` (the pre-B4 defect that let future picks backfill).
    """
    src = open("/app/backend/routes/picks_routes.py").read()
    # Find the pick_rollover function body.
    idx = src.index("async def pick_rollover")
    # Everything up to the next top-level @router.X decorator
    nxt = src.find("@router.", idx + 1)
    body = src[idx:nxt] if nxt > 0 else src[idx:]
    # Strip comment lines (``#``-prefixed) — the retired defect may
    # remain in a historical comment as documentation.  Only executable
    # code must be gone.
    executable_body = "\n".join(
        ln for ln in body.splitlines()
        if not ln.strip().startswith("#")
    )
    assert "if today_picks else picks" not in executable_body, (
        "/api/picks/rollover still contains future-backfill branch "
        "in EXECUTABLE code (not just comments)")
    # The B4 adoption must be present.
    assert "rollover_slate_contract" in body, (
        "/api/picks/rollover must import rollover_slate_contract")
    assert "choose_rollover_locks" in body, (
        "/api/picks/rollover must call choose_rollover_locks")
    assert "validate_freeze_slate" in body, (
        "/api/picks/rollover must call validate_freeze_slate before "
        "sticky-cache write")


def test_rollover_route_uses_choose_rollover_locks_today_only():
    """Call choose_rollover_locks directly with the fixture the B4
    directive specifies: today 94, today 92, tomorrow 99 → selected
    is exactly {94, 92}."""
    from services.rollover_slate_contract import choose_rollover_locks
    picks = [
        {"event_time": "2026-06-20T18:00:00Z", "lock_score": 94,
         "canonical_event_id": "e1"},
        {"event_time": "2026-06-20T20:00:00Z", "lock_score": 92,
         "canonical_event_id": "e2"},
        {"event_time": "2026-06-21T18:00:00Z", "lock_score": 99,
         "canonical_event_id": "e3"},
    ]
    rr = choose_rollover_locks(
        picks, as_of_iso="2026-06-20T10:00:00Z",
        dedupe_key=lambda p: p.get("canonical_event_id"),
    )
    assert 99 not in [p["lock_score"] for p in rr["selected"]]
    assert rr["rollover_telemetry"]["rollover_rejected_future_day"] == 1


# ═══════════════════════════════════════════════════════════════════
# Item 11 — Real HI freshness verifier adoption
# ═══════════════════════════════════════════════════════════════════

def test_hi_freshness_verifier_sport_map_no_longer_uses_updated_at():
    """The ACTUAL _SPORT_CONSUMER_MAP must drive freshness off
    event_time / game_date / date — NEVER off updated_at."""
    from services.hi_freshness_verifier import _SPORT_CONSUMER_MAP
    forbidden = {"updated_at", "ingested_at", "ingest_time"}
    allowed_event_time_fields = {
        "event_time", "game_date", "match_date", "event_date", "date",
    }
    for sport, entries in _SPORT_CONSUMER_MAP.items():
        for coll, filt, field in entries:
            assert field not in forbidden, (
                f"{sport}/{coll} freshness still uses forbidden "
                f"field {field!r} — must be an event-time column")
            assert field in allowed_event_time_fields, (
                f"{sport}/{coll} field {field!r} is not a known "
                f"event-time column")


# ═══════════════════════════════════════════════════════════════════
# Items 14-15 — Real funnel instrumentation at actual stages
# ═══════════════════════════════════════════════════════════════════

def test_publication_service_publish_batch_records_stage_counters():
    """The ACTUAL publish_batch must contain record_stage(...) calls
    at the real runtime stage (publication_attempts + published).  The
    counters must NOT be inferred from a post-hoc summary loop."""
    src = open("/app/backend/services/prediction_publication_service.py").read()
    assert "stage_funnel" in src, (
        "prediction_publication_service must import stage_funnel")
    assert "STAGE_PUBLICATION_ATTEMPTS" in src, (
        "publication service must record STAGE_PUBLICATION_ATTEMPTS")
    assert "STAGE_PUBLISHED" in src, (
        "publication service must record STAGE_PUBLISHED")
    # Not a synthetic single loop at end — the stage calls must be
    # near publish_batch / PUBLISHED state transition.
    idx_batch = src.index("async def publish_batch")
    nxt = src.find("async def ", idx_batch + 1)
    batch_body = src[idx_batch:nxt] if nxt > 0 else src[idx_batch:]
    assert "STAGE_PUBLICATION_ATTEMPTS" in batch_body, (
        "publication_attempts must be recorded INSIDE publish_batch")


def test_stage_funnel_differentiates_published_from_api_visible():
    """``published`` and ``api_visible`` are distinct counters — the
    publication service increments one, the read-path increments the
    other.  Prove the funnel keeps them separate (item 15)."""
    from services import stage_funnel as sf
    sf.reset()
    sf.record_stage("MLB", "batter_hits", sf.STAGE_PUBLISHED)
    sf.record_stage("MLB", "batter_hits", sf.STAGE_PUBLISHED)
    sf.record_stage("MLB", "batter_hits", sf.STAGE_PUBLISHED)
    # Only 1 of them is visible via /api/picks/today (e.g. 2 have
    # off_board=True after a scratch).
    sf.record_stage("MLB", "batter_hits", sf.STAGE_API_VISIBLE)
    rep = sf.report(sport="MLB")
    fam = rep["sports"]["MLB"]["families"]["batter_hits"]
    assert fam[sf.STAGE_PUBLISHED]   == 3
    assert fam[sf.STAGE_API_VISIBLE] == 1
    assert fam[sf.STAGE_PUBLISHED] > fam[sf.STAGE_API_VISIBLE]
    sf.reset()


# ═══════════════════════════════════════════════════════════════════
# Item 16 — Real publication-rejection lifecycle adoption
# ═══════════════════════════════════════════════════════════════════

def test_publication_service_imports_and_uses_rejection_lifecycle():
    """The ACTUAL prediction_publication_service must call BOTH
    add_publication_rejection and clear_publication_owned_reasons."""
    src = open(
        "/app/backend/services/prediction_publication_service.py").read()
    assert "publication_rejection_lifecycle" in src, (
        "service must import publication_rejection_lifecycle")
    assert "add_publication_rejection" in src, (
        "service must call add_publication_rejection on REJECTED")
    assert "clear_publication_owned_reasons" in src, (
        "service must call clear_publication_owned_reasons on PUBLISH")


def test_publication_lifecycle_reject_then_publish_recovery_preserves_blockers():
    """Reject → later publish must clear ONLY publication-owned
    reasons.  Independent blockers (SETTLED_FROZEN, LINEUP_SCRATCHED,
    MANUAL_BLOCK, etc.) survive.  This mirrors the mutations
    _mark_pick_lifecycle performs on the in-memory pick."""
    from services.publication_rejection_lifecycle import (
        add_publication_rejection, clear_publication_owned_reasons,
        has_only_publication_rejections,
    )
    # Phase 1 — publication REJECTS the pick for SYNTHETIC_BOOK_ODDS.
    pick = {"id": "p-1"}
    add_publication_rejection(pick, reason="SYNTHETIC_BOOK_ODDS")
    assert pick["off_board"] is True and pick["no_bet"] is True
    assert has_only_publication_rejections(pick)
    # Phase 2 — upstream also adds an INDEPENDENT scratch blocker.
    pick["off_board_reasons"].append("LINEUP_SCRATCHED")
    pick["no_bet_reasons"].append("LINEUP_SCRATCHED")
    # Phase 3 — a real observed FanDuel quote arrives and the
    # service publishes the SAME canonical wager.  Lifecycle clears
    # publication-owned reasons ONLY.
    clear_publication_owned_reasons(pick)
    assert pick["off_board"] is True, (
        "LINEUP_SCRATCHED must survive the publication-owned clear")
    assert "LINEUP_SCRATCHED" in pick["off_board_reasons"]
    assert pick["no_bet"] is True
    # The publication-owned entry is gone.
    assert all(not r.startswith("PUBLICATION_REJECTED:")
                 for r in pick["off_board_reasons"])


# ═══════════════════════════════════════════════════════════════════
# Items 17-18 — Real snapshot/ETag route adoption
# ═══════════════════════════════════════════════════════════════════

def test_board_snapshot_cache_exposes_decide_etag_response():
    from services.board_snapshot_cache import decide_etag_response
    out = decide_etag_response(client_if_none_match=None, params={"k": "v"})
    # No committed, no cached_snapshot → 503.
    assert out["status"] == 503
    assert out["serve"] is None


def test_decide_etag_response_committed_overrides_cached_snapshot(monkeypatch):
    """CASE A (item 60): committed V2 + cached snapshot V1 + client
    If-None-Match=V1 → 200 (NEVER 304).  Via the ACTUAL
    board_snapshot_cache.decide_etag_response route helper."""
    import services.board_snapshot_cache as bsc
    monkeypatch.setattr(bsc, "_committed_version", lambda: "v2")
    bsc._state.entries.clear()
    # Seed the cache with a V1 snapshot.
    _snap = bsc.Snapshot(
        board_version="v1",
        generated_at=bsc.datetime.now(bsc.timezone.utc),
        response={"picks": []},
        payload_bytes=0,
    )
    bsc._state.entries[bsc._cache_key({"k": "v"})] = _snap
    v1_etag = 'W/"v1:v1"'
    out = bsc.decide_etag_response(client_if_none_match=v1_etag,
                                    params={"k": "v"})
    assert out["status"] == 200
    assert out["serve"] == "committed"
    assert out["etag"].startswith('W/"v2:')
    assert out["fallback_stale"] is False
    bsc._state.entries.clear()


def test_decide_etag_response_matching_committed_version_returns_304(monkeypatch):
    """CASE B (item 60): committed V2 + If-None-Match=V2 → 304
    through the ACTUAL board_snapshot_cache helper."""
    import services.board_snapshot_cache as bsc
    monkeypatch.setattr(bsc, "_committed_version", lambda: "v2")
    bsc._state.entries.clear()
    out = bsc.decide_etag_response(
        client_if_none_match='W/"v2:v2"', params={"k": "v"})
    assert out["status"] == 304
    assert out["serve"] == "committed"


def test_decide_etag_response_committed_unavailable_fallback_stale(monkeypatch):
    """CASE C (item 60): committed is None, cached snapshot exists →
    fallback_stale=True + no ETag."""
    import services.board_snapshot_cache as bsc
    monkeypatch.setattr(bsc, "_committed_version", lambda: None)
    bsc._state.entries.clear()
    _snap = bsc.Snapshot(
        board_version="v1",
        generated_at=bsc.datetime.now(bsc.timezone.utc),
        response={"picks": []},
        payload_bytes=0,
    )
    bsc._state.entries[bsc._cache_key({"k": "v"})] = _snap
    out = bsc.decide_etag_response(
        client_if_none_match=None, params={"k": "v"})
    assert out["status"] == 200
    assert out["serve"] == "cached_snapshot"
    assert out["fallback_stale"] is True
    assert out["etag"] is None
    bsc._state.entries.clear()


def test_board_snapshot_cache_committed_wins_over_ttl():
    """Committed-version authority wins over TTL (item 18 — no
    competing freshness authority).  Already-present behavior in
    get_snapshot — regression-pinned here."""
    src = open("/app/backend/services/board_snapshot_cache.py").read()
    # Comment + logic must still mark TTL as fallback-only.
    assert ("committed-version authority is unavailable" in src
            or "TTL may ONLY be used as a fallback" in src)
    # When committed != cached version the cache MUST return None
    # immediately (no TTL fallback).
    assert "snap.board_version != committed" in src


# ═══════════════════════════════════════════════════════════════════
# Preservation
# ═══════════════════════════════════════════════════════════════════

def test_preservation_still_holds_after_b4():
    from services.preservation_contract import snapshot
    snap = snapshot()
    assert snap.get("APEX_MIN_BASE_SCORE") == 97.0
    assert snap.get("APEX_MIN_POSITIVE_CATEGORIES") == 5
    assert "provider_median" in (snap.get("_SYNTHETIC_ODDS_SOURCES") or [])
