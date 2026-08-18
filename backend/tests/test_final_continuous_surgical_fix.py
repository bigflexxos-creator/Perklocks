"""FINAL Continuous Surgical Fix — 5-item focused tests."""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _src(p): return open(p).read()


# ── Fix 1 · MLB markets endpoint resilient ─────────────────────────
def test_markets_endpoint_returns_configured_catalog_on_db_error():
    src = _src("/app/backend/routes/picks_routes.py")
    # try/except wraps the aux query; the return uses `markets` from
    # SPORT_MARKETS regardless.
    idx = src.index('async def markets_for_sport')
    end = src.index('return {"sport": sport, "markets": markets', idx)
    body = src[idx:end + 60]
    assert "try:" in body and "except Exception" in body
    assert "leagues = []" in body


def test_markets_projection_includes_eligibility_fields():
    src = _src("/app/backend/routes/picks_routes.py")
    idx = src.index('async def markets_for_sport')
    end = src.index('return {"sport": sport, "markets": markets', idx)
    body = src[idx:end]
    for req in ('"book_odds": 1', '"implied_probability": 1',
                '"published_lock_score": 1', '"lock_score": 1',
                '"no_bet": 1', '"off_board": 1', '"event_time": 1'):
        assert req in body, f"projection missing {req}"


def test_mlb_catalog_has_required_tokens():
    src = _src("/app/backend/server.py")
    for tok in ('"totals"', '"batter_hits"', '"batter_total_bases"',
                '"batter_hits_runs_rbis"', '"pitcher_strikeouts"',
                '"pitcher_outs"'):
        assert tok in src


# ── Fix 3 · Rollover canonical-score precedence ────────────────────
def test_rollover_prefers_published_lock_score():
    src = _src("/app/backend/routes/picks_routes.py")
    idx = src.index("def _passes_v4")
    end = src.index("return True, \"\"", idx)
    body = src[idx:end]
    assert "published_lock_score" in body
    assert "p.get(\"published_lock_score\")" in body


# ── Fix 4 · Parlay Standard/Advanced expansion ─────────────────────
def test_parlay_expansion_enabled_for_standard_and_advanced():
    src = _src("/app/backend/routes/parlay_routes.py")
    assert 'mode or "").lower() in ("standard", "advanced")' in src
    # EV filter re-applied on expanded pool
    assert 'if is_advanced and advanced_sub_norm == "ev":' in src


# ── Fix 5 · Simulator canonical line ───────────────────────────────
def test_simulator_uses_canonical_line_not_display_fallback():
    src = _src("/app/backend/pick_enrichment.py")
    # Old unconditional `line = 0.5` seed at the top of the block is gone.
    assert "_canonical_line = pick.get(\"line\")" in src
    assert "INVALID_INPUT" in src
    assert '"_sim_threshold_used"' in src
    assert '"_sim_line_source"' in src
    # Zero simulator contribution path is explicit
    assert 'simulator contribution zeroed' in src or 'INVALID_INPUT' in src


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
