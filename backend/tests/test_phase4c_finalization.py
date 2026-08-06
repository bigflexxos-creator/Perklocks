"""Phase 4C finalization — Wire-up guardrails.

Enforces that Phase 4C services are actually used by the live emission
path + admin diagnostics + settlement replay.
"""
from __future__ import annotations
import os


def test_mlb_rejection_counter_wired_into_emission():
    """`services.mlb_gates.record_rejection` MUST be called from the
    MLB rejection points in sports_engine.py."""
    src = open("/app/backend/sports_engine.py", encoding="utf-8").read()
    # Multiple call sites required — implied gate, feature gate, K math gate.
    assert src.count("services.mlb_gates import record_rejection") >= 3, (
        "record_rejection must be wired into ≥3 MLB rejection points")
    # Reasons from the enum must be referenced.
    for reason in ("missing_feature_data", "implied_probability_gate",
                     "edge_gate", "ev_gate", "correlation_conflict"):
        assert f'"{reason}"' in src, (
            f"Reason {reason!r} must be referenced in the MLB emission path")


def test_hrr_sim_reads_lineup_context_from_pick_stats():
    """`brain/sim_runner._player_stats_from_pick` MUST populate
    lineup_slot, team_runs_projection, obp from pick / player_intel /
    mlb_bvp so `sim_mlb._simulate_hrr` receives real inputs."""
    src = open("/app/backend/brain/sim_runner.py", encoding="utf-8").read()
    assert 'stats["lineup_slot"] = ls' in src
    assert 'stats["team_runs_projection"] = trp' in src
    assert 'stats["obp"] = obp' in src


def test_admin_diagnostics_route_mounted_in_server():
    src = open("/app/backend/server.py", encoding="utf-8").read()
    assert "mlb_admin_diagnostics" in src
    assert "Phase 4C MLB diagnostics" in src


def test_admin_diagnostics_route_uses_admin_gate():
    src = open("/app/backend/routes/mlb_admin_diagnostics.py",
                encoding="utf-8").read()
    assert "require_admin_user" in src
    assert "/api/admin/mlb" in src


def test_settlement_replay_artefacts_exist_and_zero_writes():
    """The 90-day replay must have produced artefacts and must not
    contain any write calls."""
    assert os.path.exists("/app/PHASE4C_SETTLEMENT_REPLAY.md")
    assert os.path.exists("/app/PHASE4C_SETTLEMENT_REPLAY.json")
    src = open("/app/backend/scripts/phase4c_mlb_settlement_replay.py",
                encoding="utf-8").read()
    for forbidden in ("insert_one", "insert_many", "update_one",
                       "update_many", "delete_one", "delete_many", "drop("):
        assert forbidden not in src, (
            f"Settlement replay must not contain {forbidden!r}")


def test_settlement_replay_report_shape():
    import json
    with open("/app/PHASE4C_SETTLEMENT_REPLAY.json") as fp:
        report = json.load(fp)
    assert report["sport"] == "MLB"
    assert "total_settled" in report
    assert "by_status" in report and set(report["by_status"]) == {"won","lost","push","void"}
    assert "by_market" in report
    assert isinstance(report["ambiguous"], list)


def test_rejection_reason_map_covers_k_gate_reasons():
    """Each `reason` returned by evaluate_k_pick MUST have a mapping
    to a structured rejection reason."""
    src = open("/app/backend/sports_engine.py", encoding="utf-8").read()
    for k_reason in ("book_odds_chalk_trap", "edge_too_low",
                       "model_prob_too_low", "under_self_contradict"):
        assert f'"{k_reason}"' in src, (
            f"K-gate reason {k_reason!r} must be mapped to a structured reason")


def test_mlb_baseline_and_settlement_scripts_are_readonly():
    for path in ("/app/backend/scripts/phase4c_mlb_baseline.py",
                 "/app/backend/scripts/phase4c_mlb_settlement_replay.py"):
        src = open(path, encoding="utf-8").read()
        for forbidden in ("db.picks.update", "db.picks.insert",
                           "db.picks.delete", "insert_one", "insert_many",
                           "update_one", "update_many", "delete_one",
                           "delete_many", "drop("):
            assert forbidden not in src, (
                f"{path} must not contain {forbidden!r}")
