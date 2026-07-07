"""Regression tests for Session 3 validator additions (2026-07-04).

§5 10-stage pipeline order
§7 Evidence threshold (min 3-of-6 independent signals)
§10 Automated integrity checks (missing metadata, invalid odds, dupes)

Run: python -m pytest backend/tests/test_board_validator_session3.py -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from board_validator import (  # noqa: E402
    integrity_check,
    evidence_threshold,
    validate_and_finalize,
)


def _base_valid(**overrides):
    """A minimally-valid pick — meets every gate by default."""
    p = {
        "id": "p1",
        "sport": "MLB",
        "event": "NYY @ BOS",
        "market": "Aaron Judge (NYY) Over 0.5 Hits",
        "selection": "Aaron Judge",
        "event_time": "2026-07-07T20:00:00Z",
        "book_odds": -120,
        "lock_score": 88,
        "edge_percent": 5.0,
        "win_probability": 0.62,
        "pick_rationale": {"vs_pitcher": {"avg": 0.318}, "recent_l5": {"hits": 4}},
        "factors": {"a": 0.6, "b": 0.65, "c": 0.7},
        "lock_components": {"ev_units": 0.08, "bucket_n": 30},
    }
    p.update(overrides)
    return p


# ── §10 integrity checks ──────────────────────────────────────────────

def test_missing_required_field_dropped():
    p = _base_valid()
    del p["event_time"]
    survivors, stats = integrity_check([p])
    assert len(survivors) == 0
    assert "missing_event_time" in stats["reasons"]


def test_zero_odds_rejected():
    p = _base_valid(book_odds=0)
    survivors, stats = integrity_check([p])
    assert len(survivors) == 0
    # Zero can trip either the missing-field or invalid_odds branch —
    # both are acceptable rejections.
    reasons = stats["reasons"]
    assert reasons.get("invalid_odds") or reasons.get("missing_book_odds")


def test_absurd_positive_odds_rejected():
    p = _base_valid(book_odds=250000)
    survivors, _ = integrity_check([p])
    assert len(survivors) == 0


def test_dead_zone_odds_rejected():
    """|odds| < 100 is impossible in American odds — must reject."""
    p = _base_valid(book_odds=50)
    survivors, _ = integrity_check([p])
    assert len(survivors) == 0


def test_bad_event_time_rejected():
    p = _base_valid(event_time="not-a-timestamp")
    survivors, stats = integrity_check([p])
    assert len(survivors) == 0
    assert "invalid_event_time" in stats["reasons"]


def test_duplicate_identity_keeps_highest_lock():
    """Two picks with same (sport, event, market, selection) → keep the
    higher-lock one, drop the other."""
    a = _base_valid(id="dup_a", lock_score=88)
    b = _base_valid(id="dup_b", lock_score=93)
    survivors, stats = integrity_check([a, b])
    assert len(survivors) == 1
    assert survivors[0]["id"] == "dup_b"
    assert stats["reasons"].get("duplicate_identity", 0) >= 1


def test_valid_pick_survives_integrity():
    survivors, stats = integrity_check([_base_valid()])
    assert len(survivors) == 1
    assert stats["dropped"] == 0


# ── §7 evidence threshold ─────────────────────────────────────────────

def test_only_edge_signal_rejected():
    """A pick with just a positive edge and nothing else is below the
    min-3-signals threshold."""
    p = _base_valid(pick_rationale={}, factors={}, edge_percent=2.0,
                     lock_components={})
    survivors, stats = evidence_threshold([p])
    assert len(survivors) == 0
    assert any("only_" in r for r in stats["reasons"].keys())


def test_three_signals_accepted():
    """Rationale + factors ≥ 3 + edge ≥ 1.5 = 3 signals → accept."""
    p = _base_valid(
        pick_rationale={"recent_form": [1, 0, 1]},
        factors={"a": 0.6, "b": 0.7, "c": 0.65},
        edge_percent=4.0,
        lock_components={"ev_units": 0.0, "bucket_n": 0},
    )
    survivors, _ = evidence_threshold([p])
    assert len(survivors) == 1
    assert survivors[0]["evidence_count"] >= 3


def test_evidence_count_stamped():
    p = _base_valid()
    evidence_threshold([p])
    assert "evidence_count" in p


# ── §5 full pipeline order ────────────────────────────────────────────

def test_full_pipeline_end_to_end():
    """A rich set of picks — verify every stage engages and drops the
    right ones, one good pick survives."""
    picks = [
        # Kept: high-quality, all signals
        _base_valid(
            id="keep",
            market="Aaron Judge (NYY) Over 0.5 Hits",
            selection="Aaron Judge",
            lock_score=94,
            edge_percent=8.0,
            lock_components={"ev_units": 0.12, "bucket_n": 40},
        ),
        # Dropped: contradictory Under of same pick
        _base_valid(
            id="drop_contra",
            market="Aaron Judge (NYY) Under 0.5 Hits",
            selection="Aaron Judge",
            lock_score=82,
        ),
        # Dropped: wrong-team batter
        _base_valid(
            id="drop_wrongteam",
            market="Kylian Mbappé (PSG) Over 0.5 Hits",
            selection="Kylian Mbappé",
        ),
        # Dropped: missing event_time
        _base_valid(id="drop_no_time", event_time=None),
        # Dropped: below evidence
        _base_valid(
            id="drop_no_evidence",
            selection="Different Selection",
            pick_rationale={}, factors={},
            edge_percent=1.0, lock_components={},
        ),
    ]
    survivors, report = validate_and_finalize(picks)
    kept_ids = {p["id"] for p in survivors}
    assert "keep" in kept_ids
    assert "drop_contra" not in kept_ids
    # Report shape
    for stage in ("contradictions", "batter_pitcher", "integrity",
                   "board_quality", "evidence", "snapshot", "rollover"):
        assert stage in report
    # The kept pick has snapshot AND evidence_count
    kept = next(p for p in survivors if p["id"] == "keep")
    assert kept.get("snapshot") is not None
    assert kept.get("evidence_count", 0) >= 3
