"""Block 1E focused test — one strong publication predicate.

Certifies that ``services.canonical_board_source.canonical_publication_filter``
now emits the SAME strong predicate used by:
  • ``server._ensure_today_picks`` health gate
  • ``picks_routes.canonical_today`` counter

So actionable consumers (Locks / /picks/all / Rollover / Parlay /
market counts) share one canonical publication semantic.
"""
from __future__ import annotations
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_canonical_publication_filter_is_strong():
    from services.canonical_board_source import canonical_publication_filter
    # Force the guard ON.
    os.environ["LOCKSCORE_REQUIRE_CANONICAL_PUBLICATION"] = "true"
    q = canonical_publication_filter()
    assert "$or" in q, (
        "canonical_publication_filter must emit an $or across "
        "publication_state=PUBLISHED and the legacy source-only bridge"
    )
    branches = q["$or"]
    # One branch requires PUBLISHED state.
    assert any(b.get("publication_state") == "PUBLISHED" for b in branches)
    # One branch is the legacy bridge (no state + source present).
    legacy = [b for b in branches
              if isinstance(b.get("publication_state"), dict)
              and b["publication_state"].get("$exists") is False]
    assert legacy, "Missing legacy bridge branch"
    assert legacy[0].get("publication_source") == {
        "$exists": True, "$ne": None,
    }


def test_predicate_parity_with_server_ensure():
    """Both canonical_board_source and server._ensure_today_picks
    must reference the SAME strong predicate structure."""
    import server, inspect
    from services.canonical_board_source import canonical_publication_filter
    os.environ["LOCKSCORE_REQUIRE_CANONICAL_PUBLICATION"] = "true"
    q = canonical_publication_filter()
    src = inspect.getsource(server._ensure_today_picks)
    # Server uses the same $or structure inline.
    assert '"publication_state": "PUBLISHED"' in src
    assert '"publication_state": {"$exists": False}' in src
    # And the module-level helper agrees.
    strs = [str(b) for b in q["$or"]]
    assert any("'publication_state': 'PUBLISHED'" in s for s in strs)


def test_guard_disable_returns_noop():
    from services.canonical_board_source import canonical_publication_filter
    os.environ["LOCKSCORE_REQUIRE_CANONICAL_PUBLICATION"] = "false"
    assert canonical_publication_filter() == {}
    # Restore
    os.environ["LOCKSCORE_REQUIRE_CANONICAL_PUBLICATION"] = "true"
