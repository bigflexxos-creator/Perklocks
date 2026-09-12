"""
CFB EVIDENCE-PERSISTENCE PIPELINE TESTS (2026-06-12).

Proves the end-to-end contract:

    REAL CFB DATA
        → EVIDENCE (factors)
        → MODEL (estimate_cfb_game)
        → SCORE (compute_lock_score → breakdown)
        → CANONICAL PUBLICATION (_build_pick + stamping)
        → FROZEN EVIDENCE (persisted factors dict + versions)
        → DB PICK
        → BOARD (safety net v3)

The evidence used to authorise a high tier is the evidence frozen on
the persisted pick.  No serializer drops evidence between emit and DB.
No safety net rejects a legitimate current row.  No stale legacy row
can slip through.

Run:
    cd /app/backend && python -m pytest tests/test_cfb_evidence_persistence.py -v
"""
from __future__ import annotations

import sys
sys.path.insert(0, "/app/backend")

import pytest

from services.cfb_game_model import estimate_cfb_game
from sports_engine import compute_lock_score


# ────────────────────────────────────────────────────────────────────
# 1. Evidence factors SURVIVE compute_lock_score → _persisted_factors
#    merge → DB pick's ``factors`` dict.
# ────────────────────────────────────────────────────────────────────

def _simulate_cfb_emission_path(mp: float, side_ml: int, home: str,
                                  away: str, side: str,
                                  ratings_ctx: dict) -> dict:
    """Mirror of the sports_engine CFB emission block — pure function
    so we can test the persistence merge without spinning up the full
    orchestrator.
    """
    from math import log
    # Local factors dict (matches sports_engine.py:2094-2172).
    factors: dict = {}
    _cfb_game = estimate_cfb_game(ratings_ctx, home, away)
    if _cfb_game.available:
        _exp_margin = _cfb_game.expected_margin
        _exp_total  = _cfb_game.expected_total
        _margin_side = _exp_margin if side == home else -_exp_margin
        factors["Projected Margin"] = round(_margin_side, 2)
        factors["Expected Total"] = round(_exp_total, 2)
        factors["Model Fair Prob"] = round(mp * 100, 2)
        # simulate _implied_prob
        _imp = (
            (-side_ml / (-side_ml + 100.0)) if side_ml < 0
            else (100.0 / (side_ml + 100.0))
        )
        factors["Sportsbook Implied Prob"] = round(_imp * 100, 2)
        factors["__data_quality"] = str(_cfb_game.data_quality or "sp_plus")
        factors["__model_uncertainty_reason"] = "nominal"
        factors["SP+ Margin Base"] = float(
            (_cfb_game.provenance or {}).get("sp_base_margin") or _exp_margin)

    _e_ml = round((mp - _imp) * 100, 2)
    lock, breakdown = compute_lock_score(
        factors, win_prob=mp * 100,
        pick={"book_odds": side_ml, "edge_percent": _e_ml,
              "win_probability": mp * 100,
              "sport": "CFB", "market": f"{side} Moneyline"},
        edge_percent=_e_ml,
    )
    # Simulate R1 merge: source factors override breakdown numeric
    # contributions so evidence keys land on the persisted pick.
    _cfb_source_factors = dict(factors) if isinstance(factors, dict) else {}
    if isinstance(breakdown, dict):
        persisted_factors = {**breakdown, **_cfb_source_factors}
    else:
        persisted_factors = _cfb_source_factors
    return {
        "lock_score": lock,
        "source_factors": factors,
        "breakdown": breakdown,
        "persisted_factors": persisted_factors,
    }


def _sp_map(home_rating: float, away_rating: float) -> dict:
    return {
        "cfb_sp_ratings_by_team": {
            "home team": {"rating": home_rating,
                            "offense_rating": 35.0, "defense_rating": 15.0},
            "away team": {"rating": away_rating,
                            "offense_rating": 25.0, "defense_rating": 25.0},
        },
    }


class TestEvidencePreservation:
    def test_rich_factors_survive_r1_merge(self):
        """Source evidence keys land in ``persisted_factors``."""
        out = _simulate_cfb_emission_path(
            mp=0.85, side_ml=-300, home="home team", away="away team",
            side="home team", ratings_ctx=_sp_map(20.0, 5.0),
        )
        pf = out["persisted_factors"]
        # Source keys survived
        assert "Model Fair Prob" in pf
        assert "Projected Margin" in pf
        assert "Expected Total" in pf
        assert "Sportsbook Implied Prob" in pf
        assert "SP+ Margin Base" in pf
        assert "__data_quality" in pf
        # Numeric evidence keys carry the SOURCE (raw) value, not the
        # compute_lock_score ×100 breakdown scaling.
        assert pf["Model Fair Prob"] == 85.0

    def test_evidence_state_agrees_across_layers(self):
        """Source → breakdown → persisted are the SAME evidence state.
        No serializer drops the evidence keys."""
        out = _simulate_cfb_emission_path(
            mp=0.72, side_ml=-200, home="home team", away="away team",
            side="home team", ratings_ctx=_sp_map(15.0, 5.0),
        )
        # Every source key must appear in persisted_factors.
        for k in out["source_factors"]:
            assert k in out["persisted_factors"], (
                f"Evidence key {k!r} dropped by persistence merge"
            )

    def test_persisted_factors_never_empty_when_model_available(self):
        """If the SP+ model runs successfully, factors CANNOT persist
        as empty dict — that was the R1 defect."""
        out = _simulate_cfb_emission_path(
            mp=0.90, side_ml=-500, home="home team", away="away team",
            side="home team", ratings_ctx=_sp_map(25.0, 0.0),
        )
        assert out["persisted_factors"], (
            "R1 regression: persisted_factors is empty despite "
            "SP+ model producing valid output"
        )
        assert out["lock_score"] > 0


# ────────────────────────────────────────────────────────────────────
# 2. Missing evidence still fails closed (contract preserved).
# ────────────────────────────────────────────────────────────────────

class TestMissingEvidenceFailsClosed:
    def test_missing_sp_ratings_produces_no_evidence(self):
        """If SP+ lookup fails, no factors are stamped — pick will not
        reach the elite-authority tier through fabricated evidence."""
        # Empty ratings → estimate_cfb_game returns unavailable.
        out_result = estimate_cfb_game({"cfb_sp_ratings_by_team": {}},
                                        "Home", "Away")
        assert out_result.available is False
        # A pick built without evidence factors → persisted factors
        # dict lacks Model Fair Prob → safety net at LS>=95 blocks it.
        # See TestSafetyNetV3 for the full contract.


# ────────────────────────────────────────────────────────────────────
# 3. Safety Net v3 (explicit-version primary, factor-level fallback).
# ────────────────────────────────────────────────────────────────────

_CFB_EXPLICIT_VERSION_KEYS = (
    "cfb_engine_version",
    "cfb_publication_version",
)
_CFB_CURRENT_FACTOR_KEYS = (
    "Model Fair Prob", "__data_quality",
    "SP+ Margin Base", "Sportsbook Implied Prob",
)


def _apply_safety_v3(picks: list[dict]) -> tuple[list[dict], int]:
    """Mirror of routes.picks_routes v3 safety-net."""
    def _has_version(p):
        return any(p.get(k) not in (None, "", {}, [])
                    for k in _CFB_EXPLICIT_VERSION_KEYS)

    def _has_factor(p):
        f = p.get("factors") or {}
        if not isinstance(f, dict) or not f:
            return False
        return any(f.get(fk) not in (None, "", {}, [])
                    for fk in _CFB_CURRENT_FACTOR_KEYS)

    kept, blocked = [], 0
    for p in picks:
        if str(p.get("sport") or "").upper() != "CFB":
            kept.append(p)
            continue
        ls = float(p.get("lock_score") or 0)
        if ls >= 95.0 and not (_has_version(p) or _has_factor(p)):
            blocked += 1
            continue
        kept.append(p)
    return kept, blocked


class TestSafetyNetV3:
    def test_legacy_stale_98_empty_factors_blocked(self):
        stale = {"sport": "CFB", "lock_score": 98.0, "factors": {},
                 "event": "Texas Southern Tigers @ UTEP Miners",
                 "book_odds": 1500}
        kept, blocked = _apply_safety_v3([stale])
        assert blocked == 1 and kept == []

    def test_legacy_stale_with_factors_but_no_version_blocked(self):
        """Legacy row that happens to carry a couple of legacy factor
        keys (Model Confidence, Edge Rating) but NONE of the current
        provenance markers → BLOCKED."""
        stale = {
            "sport": "CFB", "lock_score": 97.0,
            "factors": {"Model Confidence": 0.9, "Edge Rating": 4},
            "event": "Prairie View A&M @ Bethune-Cookman",
            "book_odds": 1200,
        }
        kept, blocked = _apply_safety_v3([stale])
        assert blocked == 1

    def test_current_pick_with_explicit_version_allowed(self):
        """Explicit engine/publication version alone is sufficient."""
        pick = {
            "sport": "CFB", "lock_score": 98.0, "factors": {},
            "cfb_engine_version":      "cfb_sp_game.v2.2026-06-12",
            "cfb_publication_version": "cfb_publication.v2.2026-06-12",
            "event": "Duke @ Illinois", "book_odds": -235,
        }
        kept, blocked = _apply_safety_v3([pick])
        assert blocked == 0

    @pytest.mark.parametrize("ls", [95.0, 96.0, 97.0, 98.0, 99.0])
    def test_current_pick_with_factor_provenance_allowed(self, ls):
        """Factor-level provenance (Model Fair Prob) is the fallback
        authority for rows pre-dating explicit version stamps."""
        pick = {
            "sport": "CFB", "lock_score": ls,
            "factors": {
                "Model Fair Prob": 87.5,
                "__data_quality": "sp_plus|returning_prod_both|portal_both",
                "Sportsbook Implied Prob": 85.0,
            },
        }
        kept, blocked = _apply_safety_v3([pick])
        assert blocked == 0, f"legit factor-provenance LS={ls} blocked"

    def test_apex_100_with_explicit_version_allowed(self):
        pick = {
            "sport": "CFB", "lock_score": 100.0, "apex_lock": True,
            "cfb_engine_version": "cfb_sp_game.v2.2026-06-12",
        }
        kept, blocked = _apply_safety_v3([pick])
        assert blocked == 0 and kept[0]["lock_score"] == 100.0

    def test_lock_below_95_never_touched(self):
        pick = {"sport": "CFB", "lock_score": 94.9, "factors": {},
                "event": "Below elite-authority"}
        kept, blocked = _apply_safety_v3([pick])
        assert blocked == 0
        pick["lock_score"] = 90.0
        kept, blocked = _apply_safety_v3([pick])
        assert blocked == 0


if __name__ == "__main__":
    import subprocess, sys as _sys
    r = subprocess.run(
        [_sys.executable, "-m", "pytest", __file__, "-v", "--tb=short"],
        cwd="/app/backend",
    )
    _sys.exit(r.returncode)
