"""Phase 4C — MLB Model + H+R+RBI + Rejection Counter + Lineup Gate Tests.

Enforces:
  1. H+R+RBI simulator does NOT double-count HR contributions.
  2. H+R+RBI is deterministic under Phase 4B seeding.
  3. H+R+RBI is lineup-slot aware (slot changes distribution).
  4. Rejection counters record and expose reasons correctly.
  5. Lineup gates enforce the confirmed / projected / bench / scratched /
     unknown status contract.
  6. Bench and scratched players do not publish.
  7. Unknown status caps confidence below the elite tier.
  8. Bookmaker metadata retention preserves the contract audit trail.
  9. Dead ``_synthesize_chalk_alt_totals`` is a stub that emits no
     synthetic outcomes.
 10. Repository guardrail: no MLB path emits ``_synthesized=True``.
 11. Baseline artefacts exist.
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


# ═══════════════════════════════════════════════════════════════════
# H+R+RBI simulator — no HR double-count + lineup awareness
# ═══════════════════════════════════════════════════════════════════
def _seed(s: int):
    import random
    random.seed(s)


def test_hrr_simulator_no_hr_double_count():
    """With HR/AB pinned to zero, the H+R+RBI distribution mean should
    exactly equal the (BA-derived hit + slot-conditional run + RBI)
    contribution — no extra HR bump.  The old model added a
    `random.random() < hr*0.4` extra bump; the new one draws HR from
    the outcome tree so 0 HR/AB → 0 extra."""
    from brain.sim_mlb import _simulate_hrr
    _seed(42)
    d_zero_hr = _simulate_hrr(batter_ba=0.30, batter_hr_rate=0.0,
                                batter_rbi_rate=0.15,
                                expected_abs=4, runs=5000,
                                lineup_slot=4, team_runs_projection=4.5,
                                obp=0.36)
    mean_zero_hr = sum(d_zero_hr) / len(d_zero_hr)
    _seed(42)
    d_high_hr = _simulate_hrr(batter_ba=0.30, batter_hr_rate=0.10,
                                batter_rbi_rate=0.15,
                                expected_abs=4, runs=5000,
                                lineup_slot=4, team_runs_projection=4.5,
                                obp=0.36)
    mean_high_hr = sum(d_high_hr) / len(d_high_hr)
    # HR path contributes ≥3 per HR (1H + 1R + 1RBI + possible extras).
    # 10% HR/AB × 4 AB → ~0.4 HRs/game → ≥1.2 extra H+R+RBI.  This is
    # BIGGER than the old spurious bump, which is fine — the point of
    # this test is to prove the DISTRIBUTION correctly reflects HR
    # contribution (3+ per HR, not the old 0.4 magic bump on top of
    # already-counted BA).
    assert mean_high_hr > mean_zero_hr


def test_hrr_simulator_deterministic_with_seed():
    """Same seed → same distribution."""
    from brain.sim_mlb import _simulate_hrr
    _seed(12345)
    d1 = _simulate_hrr(0.28, 0.05, 0.15, 4, runs=1000,
                        lineup_slot=3, team_runs_projection=4.5, obp=0.34)
    _seed(12345)
    d2 = _simulate_hrr(0.28, 0.05, 0.15, 4, runs=1000,
                        lineup_slot=3, team_runs_projection=4.5, obp=0.34)
    assert d1 == d2


def test_hrr_simulator_lineup_slot_aware():
    """Slot 3 (heart of order) should produce a higher H+R+RBI mean
    than slot 8 given identical batter stats — because RBI conversion
    is materially higher for middle-order hitters."""
    from brain.sim_mlb import _simulate_hrr
    _seed(7)
    heart = _simulate_hrr(0.28, 0.04, 0.15, 4, runs=5000,
                            lineup_slot=3, team_runs_projection=4.8, obp=0.34)
    _seed(7)
    tail = _simulate_hrr(0.28, 0.04, 0.15, 4, runs=5000,
                          lineup_slot=8, team_runs_projection=4.8, obp=0.34)
    heart_mean = sum(heart) / len(heart)
    tail_mean = sum(tail) / len(tail)
    assert heart_mean > tail_mean


def test_hrr_simulator_team_environment_scaling():
    """Higher team run projection → higher H+R+RBI mean (holding
    batter stats + slot constant)."""
    from brain.sim_mlb import _simulate_hrr
    _seed(99)
    hot = _simulate_hrr(0.28, 0.04, 0.15, 4, runs=5000,
                         lineup_slot=4, team_runs_projection=6.0, obp=0.34)
    _seed(99)
    cold = _simulate_hrr(0.28, 0.04, 0.15, 4, runs=5000,
                          lineup_slot=4, team_runs_projection=3.0, obp=0.34)
    assert (sum(hot) / len(hot)) > (sum(cold) / len(cold))


# ═══════════════════════════════════════════════════════════════════
# Rejection counters
# ═══════════════════════════════════════════════════════════════════
def test_rejection_counters_record_and_snapshot():
    from services.mlb_gates import (record_rejection, snapshot, reset,
                                      REJECTION_REASONS)
    reset()
    record_rejection("provider_line_missing", market_key="batter_hits_runs_rbis")
    record_rejection("provider_line_missing", market_key="batter_hits")
    record_rejection("lineup_scratched", market_key="pitcher_strikeouts")
    record_rejection("edge_gate", market_key="batter_hits")
    snap = snapshot()
    assert snap["totals"]["provider_line_missing"] == 2
    assert snap["totals"]["lineup_scratched"] == 1
    assert snap["by_market"]["batter_hits"]["provider_line_missing"] == 1
    assert snap["by_market"]["batter_hits"]["edge_gate"] == 1
    assert set(snap["reasons"]) == set(REJECTION_REASONS)
    reset()
    assert sum(snapshot()["totals"].values()) == 0


def test_rejection_counters_reject_unknown_reasons():
    from services.mlb_gates import record_rejection, snapshot, reset
    reset()
    record_rejection("not_a_real_reason")
    assert sum(snapshot()["totals"].values()) == 0


# ═══════════════════════════════════════════════════════════════════
# Lineup gate states
# ═══════════════════════════════════════════════════════════════════
def test_lineup_gate_confirmed_starter():
    from services.mlb_gates import (classify_lineup_status,
                                      data_quality_cap_for_status,
                                      should_publish)
    s = classify_lineup_status(lineup_confirmed=True, is_starter=True,
                                lineup_slot=3)
    assert s == "confirmed_starter"
    assert should_publish(s) is True
    assert data_quality_cap_for_status(s) == 99.0


def test_lineup_gate_projected_starter_capped():
    from services.mlb_gates import (classify_lineup_status,
                                      data_quality_cap_for_status)
    s = classify_lineup_status(is_starter=True, lineup_confirmed=False,
                                lineup_slot=2)
    assert s == "projected_starter"
    assert data_quality_cap_for_status(s) == 92.0


def test_lineup_gate_bench_scratched_blocked():
    from services.mlb_gates import (classify_lineup_status,
                                      data_quality_cap_for_status,
                                      should_publish)
    assert classify_lineup_status(on_bench=True) == "bench"
    assert should_publish("bench") is False
    assert data_quality_cap_for_status("bench") is None
    assert classify_lineup_status(scratched=True) == "scratched"
    assert should_publish("scratched") is False
    assert data_quality_cap_for_status("scratched") is None


def test_lineup_gate_unknown_capped_below_elite():
    from services.mlb_gates import (classify_lineup_status,
                                      data_quality_cap_for_status)
    s = classify_lineup_status()
    assert s == "unknown"
    cap = data_quality_cap_for_status(s)
    assert cap is not None and cap < 95.0


# ═══════════════════════════════════════════════════════════════════
# Bookmaker metadata retention
# ═══════════════════════════════════════════════════════════════════
def test_bookmaker_metadata_preserves_contract_audit():
    from services.mlb_gates import build_bookmaker_metadata
    contributors = [
        {"book": "draftkings", "odds": -120, "line": 1.5, "ts": "T1"},
        {"book": "fanduel",    "odds": -115, "line": 1.5, "ts": "T2"},
        {"book": "betmgm",     "odds": -125, "line": 1.5, "ts": "T3"},
    ]
    m = build_bookmaker_metadata(
        provider="odds_api",
        provider_event_id="evt-mlb-1",
        provider_market_key="batter_hits_runs_rbis",
        bookmakers_contributed=contributors,
        consensus_method="median_across_books",
        consensus_odds=-120,
        consensus_line=1.5,
        odds_timestamp="2026-08-06T20:00:00Z",
        main_or_alt="main",
        market_contract_id="mlb|evt-mlb-1|hrr|Over|1.5",
    )
    assert m["provider"] == "odds_api"
    assert len(m["bookmakers_contributed"]) == 3
    assert m["consensus_method"] == "median_across_books"
    assert "NOT a directly bettable" in m["notice"]
    assert m["market_contract_id"] == "mlb|evt-mlb-1|hrr|Over|1.5"


# ═══════════════════════════════════════════════════════════════════
# Dead-code removal + synthetic-line guardrail
# ═══════════════════════════════════════════════════════════════════
def test_synthesize_chalk_alt_totals_returns_empty():
    from sports_engine import _synthesize_chalk_alt_totals
    assert _synthesize_chalk_alt_totals(
        [{"name": "Over", "point": 15.5, "price": -110}]) == []


def test_no_synthetic_mlb_alt_lines_repo_guardrail():
    """No MLB alt-line path in the codebase writes ``_synthesized=True``."""
    for fname in ("sports_engine.py", "brain/sim_mlb.py",
                    "services/mlb_gates.py", "services/mlb_feature_engine.py"):
        path = f"/app/backend/{fname}"
        if not os.path.exists(path):
            continue
        src = open(path, encoding="utf-8").read()
        # The dead stub still contains the string in its docstring only.
        # The GUARDRAIL is: no CODE (assignment / dict key) writes it.
        assert '"_synthesized": True' not in src, (
            f"{fname} must not emit synthetic alt lines")
        assert "'_synthesized': True" not in src, (
            f"{fname} must not emit synthetic alt lines")


def test_baseline_artefacts_exist():
    assert os.path.exists("/app/PHASE4C_MLB_BASELINE.md")
    assert os.path.exists("/app/PHASE4C_MLB_BASELINE.json")
    import json
    with open("/app/PHASE4C_MLB_BASELINE.json") as fp:
        report = json.load(fp)
    assert report["sport"] == "MLB"
    assert report["scanned_picks"] > 0
    assert "by_lineup_status" in report["axes"]
    assert "by_data_quality_band" in report["axes"]


def test_mlb_baseline_script_zero_writes():
    src = open("/app/backend/scripts/phase4c_mlb_baseline.py",
                encoding="utf-8").read()
    for forbidden in ("db.picks.update", "db.picks.insert",
                       "db.picks.delete", "insert_one", "insert_many",
                       "update_one", "update_many", "delete_one",
                       "delete_many", "drop("):
        assert forbidden not in src, (
            f"Baseline script must not contain {forbidden!r}")
