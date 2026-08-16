"""PHASE 7 — Rollover 2.0 Production Closure regressions.

Proves the EXISTING Rollover selector satisfies the Phase 7 contract
after minimal wiring. NO NEW SELECTOR built.

Existing authority:
  Rollover selector    : routes/picks_routes.py::pick_rollover
  Ranking function     : rollover_history_tagger._composite_score
  Event-uniqueness rule: rollover_history_tagger._top_three_for_slate
  Freeze writer        : routes/picks_routes.py (live) + tagger (backfill)
  History tagger       : rollover_history_tagger (Phase 3 patched)
  Analytics consumer   : shares db.picks + settlement_events (Phase 4)

Contracts proven here:

  §7B  Frozen live membership never overwritten by settlement-time tagger.
  §7D  Selection is NOT "top 3 lock scores" — event-uniqueness enforced.
  §7E  Lock Score, model probability, edge and Rollover utility are
       stored as separate fields (never collapsed).
  §7J  Ladder de-duplication at the event level (one leg per event).
  §7T  If < 3 legitimate candidates exist, DO NOT force 3 picks.
  §7W  Snapshot metadata: selection_rank + selector_version stamped
       alongside on_rollover_at and rollover_frozen_source.
  §7X  Immutable history — live-frozen picks resist tagger reconstruction.
"""
from __future__ import annotations


# ─────────────────────────────────────────────────────────────────────
# §7B / §7X — Frozen live membership immutability.
# ─────────────────────────────────────────────────────────────────────
def test_rollover_tagger_will_not_clear_live_frozen_membership_query():
    """The tagger's clear query MUST include
    ``rollover_frozen_source: {"$ne": "picks_route_live"}``.
    (Locks in the Phase 3 fix — the exact regression Phase 7 §7B
    requires.)
    """
    import inspect, rollover_history_tagger as rht
    src = inspect.getsource(rht.stamp_rollover_history_tags)
    # The clear-tags update_many MUST filter out live-frozen rows.
    assert '"rollover_frozen_source": {"$ne": "picks_route_live"}' in src, (
        "rollover_history_tagger.stamp_rollover_history_tags must "
        "protect rollover_frozen_source='picks_route_live' rows "
        "from being cleared"
    )


# ─────────────────────────────────────────────────────────────────────
# §7D / §7J — Event uniqueness (not "top 3 lock scores").
# ─────────────────────────────────────────────────────────────────────
def test_top_three_enforces_one_leg_per_event():
    from rollover_history_tagger import _top_three_for_slate
    slate = [
        # Same event — only 1 should survive
        {"id": "1a", "event": "MIL vs CHC", "lock_score": 96,
         "win_probability": 72, "edge_percent": 4, "no_bet": False,
         "book_odds": -155, "sport": "MLB", "market": "MIL ML"},
        {"id": "1b", "event": "MIL vs CHC", "lock_score": 95,
         "win_probability": 70, "edge_percent": 3, "no_bet": False,
         "book_odds": -150, "sport": "MLB", "market": "Over 8.5"},
        # Distinct events
        {"id": "2",  "event": "SD vs LAD", "lock_score": 92,
         "win_probability": 66, "edge_percent": 5, "no_bet": False,
         "book_odds": -145, "sport": "MLB", "market": "SD ML"},
        {"id": "3",  "event": "BOS vs NYY", "lock_score": 91,
         "win_probability": 65, "edge_percent": 4, "no_bet": False,
         "book_odds": -108, "sport": "MLB", "market": "Under 9.5"},
    ]
    top = _top_three_for_slate(slate)
    events = [p["event"] for p in top]
    assert len(top) == 3
    assert len(set(events)) == 3, f"Event uniqueness violated: {events}"
    assert "1a" in {p["id"] for p in top}
    assert "1b" not in {p["id"] for p in top}


# ─────────────────────────────────────────────────────────────────────
# §7T — Never force three picks.
# ─────────────────────────────────────────────────────────────────────
def test_rollover_returns_fewer_than_three_when_pool_is_short():
    from rollover_history_tagger import _top_three_for_slate
    # Only 2 qualifying candidates on distinct events (odds outside
    # dead-zone -140..-110).
    slate = [
        {"id": "a", "event": "MIL vs CHC", "lock_score": 92,
         "win_probability": 70, "edge_percent": 5, "no_bet": False,
         "book_odds": -155, "sport": "MLB", "market": "MIL ML"},
        {"id": "b", "event": "SD vs LAD", "lock_score": 90,
         "win_probability": 66, "edge_percent": 4, "no_bet": False,
         "book_odds": -108, "sport": "MLB", "market": "Over 8.5"},
    ]
    top = _top_three_for_slate(slate)
    assert len(top) == 2, "Selector must NOT fabricate a third pick"


# ─────────────────────────────────────────────────────────────────────
# §7W — Snapshot metadata stamped by the live route.
# ─────────────────────────────────────────────────────────────────────
def test_picks_route_stamps_selection_rank_and_selector_version():
    """The Phase 7 §7W metadata (selection_rank + selector_version)
    MUST be stamped by /picks/rollover alongside on_rollover_at."""
    import inspect
    from routes import picks_routes as pr
    src = inspect.getsource(pr.pick_rollover)
    for required in (
        "rollover_selection_rank",
        "rollover_selector_version",
        "picks_route_live",
        "on_rollover_at",
    ):
        assert required in src, (
            f"/picks/rollover must stamp {required} on top-3 for "
            f"§7W snapshot reproducibility"
        )


# ─────────────────────────────────────────────────────────────────────
# §7E — Score-semantics separation (fields never collapsed).
# ─────────────────────────────────────────────────────────────────────
def test_composite_score_uses_multiple_fields_not_just_lock_score():
    from rollover_history_tagger import _composite_score
    p_high_lock_low_edge = {"lock_score": 99, "win_probability": 80,
                             "edge_percent": 0.5, "book_odds": -700}
    p_low_lock_high_edge = {"lock_score": 90, "win_probability": 62,
                             "edge_percent": 8.0, "book_odds": -140}
    s1 = _composite_score(p_high_lock_low_edge)
    s2 = _composite_score(p_low_lock_high_edge)
    # Composite must consider price/edge, not raw Lock Score alone —
    # so the high-lock chalky pick shouldn't dominate by more than
    # 15 pts over the strong-value 90 pick.  (Prevents "top 3 lock
    # scores" behavior forbidden by §7D.)
    assert s1 - s2 < 15.0, (
        f"composite too Lock-Score-heavy: chalk={s1}, value={s2}"
    )


# ─────────────────────────────────────────────────────────────────────
# §7Y — Analytics uses frozen membership, not reconstructed.
# ─────────────────────────────────────────────────────────────────────
def test_rollover_analytics_uses_on_rollover_at_field():
    """Sanity: Rollover truth authorities filter by the frozen
    ``on_rollover_at`` tag — never by postgame reconstruction."""
    import inspect
    from services import published_results_truth as prt
    src = inspect.getsource(prt)
    assert "on_rollover_at" in src, (
        "published_results_truth must filter Rollover history by "
        "the frozen on_rollover_at tag, never by postgame "
        "reconstruction"
    )
