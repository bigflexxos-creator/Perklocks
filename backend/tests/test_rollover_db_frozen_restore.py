"""P4 μ-closure focused test:
Rollover Top-3 must survive RAM cache clears + backend restarts by
restoring frozen membership from ``db.picks`` (on_rollover_at +
rollover_selection_rank stamped on first live surface).

Because the route pulls in heavyweight FastAPI + Mongo deps, we
guard the DB-frozen restore behavior at the unit level by testing:
  1. The by_rank dedup preserves the FIRST stamp per rank.
  2. Missing ranks correctly fall through to the recompute path.
"""
from __future__ import annotations


def _restore_from_frozen(frozen_docs, in_play_filter=lambda x: x):
    """Reproduces the DB-frozen restore logic in isolation."""
    by_rank: dict[int, dict] = {}
    for d in frozen_docs:
        r = int(d.get("rollover_selection_rank") or 0)
        if r in (1, 2, 3) and r not in by_rank:
            by_rank[r] = d
    ordered = [by_rank[r] for r in (1, 2, 3) if r in by_rank]
    ordered = in_play_filter(ordered)
    if len(ordered) == 3:
        return ordered
    return None


def test_frozen_restore_returns_top3_in_rank_order():
    frozen = [
        {"id": "p2", "rollover_selection_rank": 2, "lock_score": 92},
        {"id": "p3", "rollover_selection_rank": 3, "lock_score": 90},
        {"id": "p1", "rollover_selection_rank": 1, "lock_score": 95},
    ]
    got = _restore_from_frozen(frozen)
    assert got is not None
    assert [p["id"] for p in got] == ["p1", "p2", "p3"]


def test_frozen_restore_first_stamp_wins_on_double_stamp():
    # If some historical double-stamp exists for rank 1, we keep the
    # earliest doc encountered (deterministic).
    frozen = [
        {"id": "p1_first", "rollover_selection_rank": 1},
        {"id": "p1_dupe",  "rollover_selection_rank": 1},
        {"id": "p2",       "rollover_selection_rank": 2},
        {"id": "p3",       "rollover_selection_rank": 3},
    ]
    got = _restore_from_frozen(frozen)
    assert got is not None
    assert [p["id"] for p in got] == ["p1_first", "p2", "p3"]


def test_missing_rank_falls_through():
    # Only ranks 1+2 stamped → restore MUST return None so the caller
    # runs a full recompute for a complete top-3.
    frozen = [
        {"id": "p1", "rollover_selection_rank": 1},
        {"id": "p2", "rollover_selection_rank": 2},
    ]
    assert _restore_from_frozen(frozen) is None


def test_in_play_gate_removes_settled():
    frozen = [
        {"id": "p1", "rollover_selection_rank": 1},
        {"id": "p2", "rollover_selection_rank": 2},
        {"id": "p3", "rollover_selection_rank": 3},
    ]
    def gate(picks):
        return [p for p in picks if p["id"] != "p3"]  # e.g. game finished
    assert _restore_from_frozen(frozen, gate) is None


if __name__ == "__main__":
    import sys, pytest
    sys.exit(pytest.main([__file__, "-v"]))
