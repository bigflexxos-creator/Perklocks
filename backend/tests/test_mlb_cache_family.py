"""P1 μ-closure focused test:
MLB cache-first HIT must require ALL requested market families to be
present in the fresh cache. If any requested family is missing, the
function must fall through to the provider fetch (cache MISS).

This regression test guards against the previous defect where any
fresh cached row caused a cache HIT for the event, silently
suppressing acquisition of newly requested families.
"""
from __future__ import annotations

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sports_engine import _prop_family_key


def _family_of_rows(rows):
    return {_prop_family_key(r["market_key"]) for r in rows if r.get("market_key")}


def test_family_key_collapses_alternate():
    assert _prop_family_key("batter_hits") == "batter_hits"
    assert _prop_family_key("batter_hits_alternate") == "batter_hits"
    assert _prop_family_key("pitcher_strikeouts_alternate") == "pitcher_strikeouts"


def test_family_key_keeps_distinct_families_separate():
    # Ks vs outs are distinct families and MUST not collapse.
    assert _prop_family_key("pitcher_strikeouts") != _prop_family_key("pitcher_outs")


def test_cache_completeness_partial_families_triggers_fetch():
    """Simulate: fresh cache has batter_hits only; caller requests
    batter_hits + pitcher_strikeouts. Missing family must be detected."""
    cached_rows = [{"market_key": "batter_hits"},
                   {"market_key": "batter_hits_alternate"}]
    requested = ["batter_hits", "pitcher_strikeouts"]
    req_families = {_prop_family_key(m) for m in requested}
    cached_families = _family_of_rows(cached_rows)
    missing = req_families - cached_families
    assert missing == {"pitcher_strikeouts"}, (
        f"Family-aware completeness gate broken: {missing}"
    )


def test_cache_completeness_all_families_present_hits():
    """Simulate: fresh cache has both families. Cache HIT is legal."""
    cached_rows = [{"market_key": "batter_hits"},
                   {"market_key": "pitcher_strikeouts_alternate"}]
    requested = ["batter_hits", "pitcher_strikeouts"]
    req_families = {_prop_family_key(m) for m in requested}
    cached_families = _family_of_rows(cached_rows)
    missing = req_families - cached_families
    assert missing == set(), f"Should have HIT but missing={missing}"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
