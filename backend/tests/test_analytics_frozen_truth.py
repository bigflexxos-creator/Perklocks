"""Analytics + Learning Frozen-Truth Closure test.

Validates the truth-source contract:
1. published_lock_score is preferred over post-publication mutable lock_score
2. CLV null contract — missing close ≠ 0
3. Research / backfill / synthetic rows are excluded from production stats
4. PROD_TRUTH_FILTER requires publication_state == PUBLISHED
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_prod_truth_filter_requires_published_state():
    from routes.me_performance_routes import _PROD_TRUTH_FILTER
    assert _PROD_TRUTH_FILTER.get("publication_state") == "PUBLISHED"


def test_prod_truth_filter_excludes_research_backfill_synthetic():
    from routes.me_performance_routes import _PROD_TRUTH_FILTER
    # Every provenance deny-flag returns $ne: True — a row with
    # backfill/synthetic/research/sample/historical set to True is
    # excluded from every production aggregation.
    for k in ("backfill", "synthetic", "research_only",
              "sample_data", "historical_only"):
        assert _PROD_TRUTH_FILTER.get(k) == {"$ne": True}, (
            f"provenance deny-flag {k} missing/mismatched"
        )


def test_frozen_lock_score_expr_prefers_published():
    from routes.me_performance_routes import _frozen_lock_score_expr
    expr = _frozen_lock_score_expr()
    # Semantic: coalesce(published_lock_score, lock_score) — published
    # ALWAYS beats mutable when both exist.
    assert expr == {"$ifNull": ["$published_lock_score", "$lock_score"]}


def test_clv_contract_null_when_unavailable_not_zero():
    """Contract: closing_odds unavailable → both closing_odds and
    clv_value MUST be null. Never 0.  Validated by inspecting the
    snapshotter update payloads at the source-code level."""
    import closing_line_snapshotter as clsnap
    src = open(clsnap.__file__).read()
    # The snapshotter must NOT write clv_value=0.0 as a fabricated
    # value when the close is unavailable.  It MUST write clv_value: None.
    assert '"clv_value":                 None' in src, (
        "CLV null contract broken — snapshotter still writes 0.0 for unavailable close"
    )
    # Explicit `closing_odds_source = "unavailable"` marker must exist.
    assert '"closing_odds_source":       "unavailable"' in src


def test_clv_avg_predicate_excludes_null():
    """Aggregation's `clv_n` counter must gate on `clv_value != None`
    so null CLVs don't contaminate the average."""
    import routes.me_performance_routes as mp
    src = open(mp.__file__).read()
    # `clv_n` uses `$ne: None` — matches null-safe count.
    assert '"clv_n": {"$sum": {"$cond": [{"$ne": ["$clv_value", None]}, 1, 0]}}' in src


def test_by_band_uses_frozen_lock_score():
    """Band aggregation must switch on frozen published_lock_score
    (via $ifNull) so post-publication mutations don't reshuffle bands."""
    import routes.me_performance_routes as mp
    src = open(mp.__file__).read()
    # The band pipeline references `_frozen_lock` produced by
    # `_frozen_lock_score_expr()` — proves the fix is wired.
    assert '"_frozen_lock":' in src
    assert '"$_frozen_lock"' in src


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
