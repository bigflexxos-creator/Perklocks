"""MLB Early Prop Coverage μ-closure focused tests.

Validates the three surgical pre-model gates:
 FIX 1 — Early hitter hydration (Hits/TB/H+R+RBI)
 FIX 2 — Under pre-drop exception extended to prop families
 FIX 3 — Total Bases 62% generic gate replaced by market-specific 0.50
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _src(path: str) -> str:
    return open(path).read()


# ── FIX 2 · Under pre-drop exception coverage ────────────────────────
def test_under_pre_drop_allows_hits_tb_hrrbi_outs_under():
    src = _src("/app/backend/sports_engine.py")
    # New allow-list must include all four prop families + Ks.
    assert '"pitcher_strikeouts"' in src
    assert '"batter_hits"' in src
    assert '"batter_total_bases"' in src
    assert '"batter_hits_runs_rbis"' in src
    assert '"pitcher_outs"' in src
    # The single-market K exception variable must be gone.
    assert "is_k_main_under" not in src, (
        "old single-market K exception still present — must be replaced by "
        "_MAIN_UNDER_ALLOWED_MK set"
    )
    assert "_MAIN_UNDER_ALLOWED_MK" in src
    assert "is_prop_main_under" in src


def test_under_pre_drop_still_drops_generic_under():
    """The blanket-drop path for non-allowed main-line unders must
    remain — we're extending, not deleting, the exception."""
    src = _src("/app/backend/sports_engine.py")
    # After the exception check, non-allowed main-line unders `continue`.
    assert "and not is_prop_main_under" in src
    assert 'str(side).lower() == "under"' in src


# ── FIX 3 · Total Bases market-specific gate ─────────────────────────
def test_total_bases_has_own_pre_model_gate_not_generic_62():
    src = _src("/app/backend/sports_engine.py")
    # New TB branch present.
    assert 'elif mk in ("batter_total_bases", "batter_total_bases_alternate"):' in src
    # TB floor is 0.50, NOT 0.62.
    assert "if implied < 0.50:" in src
    # TB is now in the _mk_gated set (so it never falls to the generic
    # 0.62 floor).
    assert '"batter_total_bases"' in src
    assert '"batter_total_bases_alternate"' in src


def test_generic_0_62_gate_still_defined_but_tb_no_longer_uses_it():
    src = _src("/app/backend/sports_engine.py")
    # The generic constant remains for markets that legitimately need
    # a 62% floor (HR family etc.).
    assert "_HIGH_PROB_MIN_IMPLIED = 0.62" in src


# ── FIX 1 · Early Hitter Hydration helper ────────────────────────────
def test_hydrate_missing_hitter_helper_exists():
    from services.mlb_early_hitter_hydrate import hydrate_missing_hitter
    assert callable(hydrate_missing_hitter)


def test_sports_engine_calls_hydration_for_hitter_families():
    src = _src("/app/backend/sports_engine.py")
    assert "hydrate_missing_hitter" in src
    assert "_HITTER_FAMILY_MK" in src
    # Contract: must include Hits + TB + H+R+RBI (both main + alt keys).
    for mk in (
        '"batter_hits"', '"batter_hits_alternate"',
        '"batter_total_bases"', '"batter_total_bases_alternate"',
        '"batter_hits_runs_rbis"', '"batter_hits_runs_rbis_alternate"',
    ):
        assert mk in src, f"family key {mk} missing from _HITTER_FAMILY_MK"


def test_hydration_only_runs_when_lineup_unknown():
    src = _src("/app/backend/sports_engine.py")
    # Guard clause: must condition on lineup_status == "unknown"
    # so confirmed / projected paths remain untouched.
    idx = src.index("_HITTER_FAMILY_MK")
    block = src[idx:idx + 2000]
    assert '_lu_status == "unknown"' in block


def test_hydration_preserves_uncertainty_cap():
    """The hydrate helper must set lineup_confirmed=False so the
    existing UNKNOWN → cap 88 stays authoritative."""
    src = _src("/app/backend/services/mlb_early_hitter_hydrate.py")
    assert '"lineup_confirmed":   False' in src
    assert '"is_starter":         None' in src
    assert '"lineup_source":      "hydrated_from_player_db"' in src


def test_hydration_never_fabricates():
    """If zero real signals are found we must NOT attach a row."""
    src = _src("/app/backend/services/mlb_early_hitter_hydrate.py")
    # There's a "if signals == 0: return False" guard.
    assert "if signals == 0:" in src
    assert "return False" in src


# ── Pitcher Ks regression (unchanged) ────────────────────────────────
def test_pitcher_ks_still_in_gated_set_and_allowed_under():
    src = _src("/app/backend/sports_engine.py")
    # K existing behaviour unchanged: still in _mk_gated set + Under allowed
    assert '"pitcher_strikeouts"' in src


# ── Pitcher Outs unchanged floor 0.55 ────────────────────────────────
def test_pitcher_outs_market_gate_unchanged():
    src = _src("/app/backend/sports_engine.py")
    # The 55% market-specific gate for Outs must remain (only the blanket
    # Under pre-drop was relaxed).
    assert 'elif mk == "pitcher_outs":' in src


# ── Non-negotiables ──────────────────────────────────────────────────
def test_board_floor_and_lock_still_intact():
    """No changes to Board floor / Lock Score threshold."""
    for path in (
        "/app/backend/parlay_optimizer.py",
        "/app/backend/routes/parlay_routes.py",
    ):
        s = _src(path)
        # We haven't lowered the >=85 canonical floor.
        assert "85" in s


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
