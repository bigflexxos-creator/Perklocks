"""Commit B/3 — Scheduler, Rollover & Snapshot/ETag Authority tests.

Covers items 43-62 of the Universal Root-Closure Build v3.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ═══════════════════════════════════════════════════════════════════
# Items 43-44 — Automatic sport activation
# ═══════════════════════════════════════════════════════════════════

def test_sport_activation_classifies_events_plus_invocation_as_active():
    from services.sport_activation import (
        classify_sport_activation, ActivationState,
    )
    assert classify_sport_activation(
        "NFL", supported_events_exist=True, scheduler_invoked=True,
    ) == ActivationState.ACTIVE


def test_sport_activation_no_events_different_from_starved():
    from services.sport_activation import (
        classify_sport_activation, ActivationState,
    )
    assert classify_sport_activation(
        "NBA", supported_events_exist=False, scheduler_invoked=False,
    ) == ActivationState.NO_EVENTS


def test_sport_activation_starvation_detectable():
    """Events exist AND scheduler never invoked → SPORT_REFRESH_STARVED
    (item 47)."""
    from services.sport_activation import (
        classify_sport_activation, ActivationState,
    )
    assert classify_sport_activation(
        "NHL", supported_events_exist=True, scheduler_invoked=False,
    ) == ActivationState.SPORT_REFRESH_STARVED


def test_sport_activation_suspended_overrides():
    from services.sport_activation import (
        classify_sport_activation, ActivationState,
    )
    assert classify_sport_activation(
        "NFL", supported_events_exist=True, scheduler_invoked=True,
        suspended=True,
    ) == ActivationState.SUSPENDED


def test_scheduler_entry_point_registration_is_observable():
    from services.sport_activation import (
        register_scheduler_entry, scheduler_report, reset,
    )
    reset()
    register_scheduler_entry(
        "NFL", entry_point="pick_refresh_orchestrator.refresh_sport",
        invoked=True, detail={"cycle": 42})
    rep = scheduler_report()
    assert rep["NFL"]["entry_point"] == (
        "pick_refresh_orchestrator.refresh_sport")
    assert rep["NFL"]["invoked"] is True
    reset()


# ═══════════════════════════════════════════════════════════════════
# Item 45 — Dynamic Soccer / Tennis provider discovery
# ═══════════════════════════════════════════════════════════════════

def test_discovered_soccer_provider_key_classifies_and_acquires():
    """A newly-active Soccer competition key must enter acquisition
    without a manual whitelist edit."""
    from services.sport_activation import classify_discovered_provider_key
    def _cls(k):
        return "Soccer" if k.startswith("soccer_") else (
            "Tennis" if k.startswith("tennis_") else None)
    out = classify_discovered_provider_key(
        "soccer_uefa_europa_league",
        sport_family_classifier=_cls,
        supported_sports={"Soccer", "Tennis", "MLB", "NFL",
                           "NBA", "NHL", "CFB"},
    )
    assert out["family"] == "Soccer"
    assert out["supported"] is True
    assert out["action"] == "acquire"


def test_discovered_unsupported_provider_key_fails_closed():
    from services.sport_activation import classify_discovered_provider_key
    def _cls(k):
        return "Pickleball" if k.startswith("pickleball") else None
    out = classify_discovered_provider_key(
        "pickleball_pro_tour",
        sport_family_classifier=_cls,
        supported_sports={"Soccer", "Tennis", "MLB"},
    )
    assert out["action"] == "fail_closed"
    assert out["supported"] is False


# ═══════════════════════════════════════════════════════════════════
# Item 46 — Bad-market isolation (422 does not kill valid markets)
# ═══════════════════════════════════════════════════════════════════

def test_invalid_provider_market_does_not_suppress_valid_markets():
    from services.sport_activation import valid_markets, is_bad_market
    requested = [
        "player_reception_alternate",      # invalid (misspelled)
        "player_receptions_alternate",     # valid
        "player_pass_yds",                  # valid
        "alternate_puck_lines",             # invalid
        "alternate_spreads",                # valid
    ]
    kept = valid_markets(requested)
    assert "player_reception_alternate" not in kept
    assert "alternate_puck_lines" not in kept
    assert "player_receptions_alternate" in kept
    assert "player_pass_yds" in kept
    assert "alternate_spreads" in kept
    assert is_bad_market("player_reception_alternate")
    assert not is_bad_market("player_receptions_alternate")


# ═══════════════════════════════════════════════════════════════════
# Items 48-57 — Rollover TODAY-only contract
# ═══════════════════════════════════════════════════════════════════

def test_rollover_filter_today_only_rejects_tomorrow_pick_within_24h():
    """11 PM today + tomorrow 8 PM within 24h → tomorrow MUST NOT
    qualify (item 50 / 71.D)."""
    from services.rollover_slate_contract import filter_today_only
    picks = [
        {"event_time": "2026-06-20T22:30:00Z", "lock_score": 90},  # today-late
        {"event_time": "2026-06-21T20:00:00Z", "lock_score": 99},  # tomorrow
    ]
    today = filter_today_only(picks, as_of_iso="2026-06-20T23:00:00Z",
                                drop_started=False)
    # Only the first survives — product tz ET means event_time
    # cutoff is the product-day boundary, not now+24h.
    for p in today:
        assert p["event_time"].startswith("2026-06-2")  # same date
    # Explicitly: NO tomorrow pick.
    assert not any(p["event_time"].startswith("2026-06-21")
                     for p in today)


def test_rollover_chooses_today_94_over_tomorrow_99():
    """Today 94 + Today 92 + Tomorrow 99 → Tomorrow excluded (71.A)."""
    from services.rollover_slate_contract import choose_rollover_locks
    picks = [
        {"event_time": "2026-06-20T18:00:00Z", "lock_score": 94,
         "canonical_event_id": "e1"},
        {"event_time": "2026-06-20T20:00:00Z", "lock_score": 92,
         "canonical_event_id": "e2"},
        {"event_time": "2026-06-21T18:00:00Z", "lock_score": 99,
         "canonical_event_id": "e3"},
    ]
    result = choose_rollover_locks(
        picks, as_of_iso="2026-06-20T10:00:00Z",
        dedupe_key=lambda p: p.get("canonical_event_id"),
    )
    sel_scores = sorted([p["lock_score"] for p in result["selected"]])
    assert 99 not in sel_scores, (
        f"tomorrow 99 MUST NOT appear in Rollover; got {sel_scores}")
    assert sel_scores == [92, 94]
    tel = result["rollover_telemetry"]
    assert tel["rollover_canonical_candidates"] == 3
    assert tel["rollover_today_candidates"]     == 2
    assert tel["rollover_rejected_future_day"]  == 1
    assert tel["rollover_selected"]              == 2


def test_rollover_returns_zero_when_no_today_qualifier_available():
    """0 qualifying today → return 0.  Never backfill from future."""
    from services.rollover_slate_contract import choose_rollover_locks
    picks = [
        # Future-day high-lock — must not be pulled.
        {"event_time": "2026-06-21T18:00:00Z", "lock_score": 99,
         "canonical_event_id": "e3"},
        # Today but below 85.
        {"event_time": "2026-06-20T18:00:00Z", "lock_score": 70,
         "canonical_event_id": "e4"},
    ]
    result = choose_rollover_locks(
        picks, as_of_iso="2026-06-20T10:00:00Z",
        dedupe_key=lambda p: p.get("canonical_event_id"),
    )
    assert result["selected"] == []
    assert result["rollover_telemetry"]["rollover_selected"] == 0


def test_rollover_returns_one_when_only_one_qualifies_today():
    from services.rollover_slate_contract import choose_rollover_locks
    picks = [
        {"event_time": "2026-06-20T18:00:00Z", "lock_score": 92,
         "canonical_event_id": "e1"},
        {"event_time": "2026-06-21T18:00:00Z", "lock_score": 99,
         "canonical_event_id": "e2"},
    ]
    result = choose_rollover_locks(
        picks, as_of_iso="2026-06-20T10:00:00Z",
        dedupe_key=lambda p: p.get("canonical_event_id"),
    )
    assert len(result["selected"]) == 1
    assert result["selected"][0]["lock_score"] == 92


def test_rollover_future_weekly_pick_rejected_despite_today_pick_date():
    """A future NFL Sunday pick carrying today's pick_date MUST be
    rejected if its event_time is tomorrow (item 51)."""
    from services.rollover_slate_contract import filter_today_only
    picks = [
        {"event_time": "2026-06-21T19:00:00Z",   # tomorrow
         "pick_date":  "2026-06-20",             # today (misleading)
         "lock_score": 98, "canonical_event_id": "nfl-future"},
    ]
    today = filter_today_only(picks, as_of_iso="2026-06-20T10:00:00Z")
    assert today == [], (
        "pick_date on a future-event-time pick MUST NOT qualify")


def test_rollover_freeze_guard_rejects_wrong_day_leg():
    """``validate_freeze_slate`` MUST reject a slate containing any
    leg whose event_time doesn't belong to slate_date (item 53)."""
    from services.rollover_slate_contract import (
        validate_freeze_slate, ROLLOVER_FUTURE_EVENT_REJECTED,
    )
    legs = [
        {"event_time": "2026-06-20T18:00:00Z"},   # today
        {"event_time": "2026-06-21T18:00:00Z"},   # tomorrow — bad
    ]
    ok, failures = validate_freeze_slate(
        legs, slate_date_iso="2026-06-20T10:00:00Z")
    assert ok is False
    assert ROLLOVER_FUTURE_EVENT_REJECTED in failures


def test_rollover_sticky_cache_drops_wrong_day_entries():
    """Sticky cache MUST filter out wrong-day entries even if TTL is
    still fresh (item 55)."""
    from services.rollover_slate_contract import filter_sticky_cache_entries
    entries = [
        {"event_time": "2026-06-20T18:00:00Z", "canonical_event_id": "e1"},
        {"event_time": "2026-06-19T18:00:00Z", "canonical_event_id": "e2"},
        {"event_time": "2026-06-21T18:00:00Z", "canonical_event_id": "e3"},
    ]
    filtered = filter_sticky_cache_entries(
        entries, as_of_iso="2026-06-20T10:00:00Z")
    ids = {p["canonical_event_id"] for p in filtered}
    assert ids == {"e1"}, (
        f"sticky cache must drop wrong-day entries; got {ids}")


def test_rollover_canonical_event_dedupe_enforces_one_per_event():
    """Dedupe keeps ONE pick per canonical_event_id (item 71.I)."""
    from services.rollover_slate_contract import choose_rollover_locks
    picks = [
        {"event_time": "2026-06-20T18:00:00Z", "lock_score": 94,
         "canonical_event_id": "e1"},
        # Second pick on SAME canonical event — must dedupe out.
        {"event_time": "2026-06-20T18:00:00Z", "lock_score": 95,
         "canonical_event_id": "e1"},
        {"event_time": "2026-06-20T20:00:00Z", "lock_score": 93,
         "canonical_event_id": "e2"},
    ]
    result = choose_rollover_locks(
        picks, as_of_iso="2026-06-20T10:00:00Z",
        dedupe_key=lambda p: p.get("canonical_event_id"),
    )
    event_ids = {p["canonical_event_id"] for p in result["selected"]}
    assert event_ids == {"e1", "e2"}
    # The 95-scorer on e1 wins the dedupe (sorted by lock_score DESC).
    e1_pick = next(p for p in result["selected"] if p["canonical_event_id"] == "e1")
    assert e1_pick["lock_score"] == 95


# ═══════════════════════════════════════════════════════════════════
# Items 58-62 — Snapshot / ETag one-authority chain
# ═══════════════════════════════════════════════════════════════════

def test_etag_v1_to_v2_does_not_304():
    """CASE A (item 60): snapshot=V1, committed=V2, If-None-Match=V1
    MUST NOT 304."""
    from services.snapshot_etag_authority import (
        new_identity, advance, snapshot_decision,
    )
    v1 = new_identity(board_version="v1", canonical_revision="r1",
                        generation_id="g1")
    time.sleep(0.001)
    v2 = advance(v1)
    assert v2.board_version != v1.board_version
    assert v2.canonical_revision != v1.canonical_revision
    d = snapshot_decision(
        client_if_none_match=v1.etag(),
        committed=v2,
        cached_snapshot=v1,
    )
    assert d["status"] == 200, d
    assert d["serve"] == "committed"
    assert d["etag"] == v2.etag()
    assert d["fallback_stale"] is False


def test_etag_matching_version_304_allowed():
    """CASE B (item 60): committed=V2, snapshot=V2, If-None-Match=V2
    → 304 allowed."""
    from services.snapshot_etag_authority import (
        new_identity, snapshot_decision,
    )
    v2 = new_identity(board_version="v2", canonical_revision="r2",
                        generation_id="g2")
    d = snapshot_decision(
        client_if_none_match=v2.etag(),
        committed=v2, cached_snapshot=v2,
    )
    assert d["status"] == 304
    assert d["serve"] == "committed"
    assert d["etag"] == v2.etag()


def test_etag_committed_unavailable_fallback_is_truthfully_marked():
    """CASE C (item 60): committed authority unavailable → fallback
    snapshot allowed BUT must be truthfully marked ``fallback_stale``
    and must NOT advertise an ETag (so a client can't 304 against it)."""
    from services.snapshot_etag_authority import (
        new_identity, snapshot_decision,
    )
    v1 = new_identity(board_version="v1")
    d = snapshot_decision(
        client_if_none_match=v1.etag(),
        committed=None,
        cached_snapshot=v1,
    )
    assert d["status"] == 200
    assert d["serve"] == "cached_snapshot"
    assert d["fallback_stale"] is True
    assert d["etag"] is None, (
        "fallback stale response MUST NOT advertise an ETag "
        "otherwise clients could 304 against a stale snapshot")


def test_response_metadata_is_internally_consistent():
    """board_version / canonical_revision / generation_id / ETag /
    X-Board-Version all belong to ONE identity (item 61)."""
    from services.snapshot_etag_authority import (
        new_identity, response_metadata,
    )
    v = new_identity(board_version="v9", canonical_revision="r9",
                       generation_id="g9")
    meta = response_metadata(v)
    assert meta["board_version"] == "v9"
    assert meta["canonical_revision"] == "r9"
    assert meta["generation_id"] == "g9"
    assert meta["X-Board-Version"] == "v9"
    assert meta["ETag"].startswith('W/"v9:')
    assert meta["ETag"].endswith(':r9"')


def test_known_version_mismatch_invalidates_without_ttl_override():
    """Committed=V2 while cached_snapshot=V1 → committed wins
    immediately (item 59).  TTL is NOT consulted."""
    from services.snapshot_etag_authority import (
        new_identity, snapshot_decision,
    )
    v1 = new_identity(board_version="v1", canonical_revision="r1")
    v2 = new_identity(board_version="v2", canonical_revision="r2")
    d = snapshot_decision(
        client_if_none_match=None,
        committed=v2,
        cached_snapshot=v1,
    )
    assert d["status"] == 200
    assert d["serve"] == "committed"
    assert d["etag"] == v2.etag()
    assert d["reason"] == (
        "committed_authority_overrides_known_stale_snapshot")


# ═══════════════════════════════════════════════════════════════════
# Item 62 — Truth-manifest consistency
# ═══════════════════════════════════════════════════════════════════

def test_truth_manifest_fingerprint_still_functional_after_b1():
    """Preservation: B1 removed the external_id fallback; the manifest
    still produces a usable fingerprint for a canonical pick."""
    from services.truth_manifest import fingerprint_fields
    fp = fingerprint_fields({
        "canonical_event_id": "mlb:e:12345",
        "provider_event_id":  "p-99",
        "sport": "MLB",
        "market": "Hits",
        "selection": "Judge",
        "line": 1.5,
        "book_odds": -115,
        "bookmaker": "fanduel",
        "win_probability": 60.0,
        "lock_score": 90,
        "grade": "A",
        "generation_id": "g-abc",
    })
    assert fp["canonical_event_id"] == "mlb:e:12345"
    assert fp["model_probability"] == 0.6
    assert fp["lock_score"] == 90


# ═══════════════════════════════════════════════════════════════════
# Preservation
# ═══════════════════════════════════════════════════════════════════

def test_preservation_still_holds_after_b3():
    from services.preservation_contract import snapshot
    snap = snapshot()
    assert snap.get("APEX_MIN_BASE_SCORE") == 97.0
    assert snap.get("APEX_MIN_POSITIVE_CATEGORIES") == 5
    assert "provider_median" in (snap.get("_SYNTHETIC_ODDS_SOURCES") or [])


if __name__ == "__main__":
    import subprocess
    sys.exit(subprocess.call(["python", "-m", "pytest", __file__, "-v"]))
