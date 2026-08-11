"""Block 2A — Regression tests for the Pipeline Diagnostic Framework.

These tests lock the reason-code taxonomy, wiring status enum, and
trace container so that future refactors cannot silently shrink the
vocabulary or drop stages.
"""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "/app/backend")

pytestmark = pytest.mark.unit


from services.pipeline_diagnostic import (
    PIPELINE_STAGES,
    PipelineTrace,
    ReasonCode,
    StageResult,
    WiringStatus,
    build_wiring_matrix,
    get_wiring_evidence,
)


# ── Reason-code taxonomy ────────────────────────────────────────
def test_reason_code_taxonomy_is_stable():
    required = {
        "EVENT_NOT_DISCOVERED", "EVENT_TIME_FILTER",
        "MARKET_NOT_SUPPORTED", "NO_REAL_LINE", "STALE_LINE",
        "MODEL_ONLY_SYNTHETIC_ODDS",
        "STALE_CACHE_USED", "EMPTY_CACHE",
        "PROVIDER_ERROR", "PROVIDER_422",
        "BUDGET_BLOCKED", "BAD_MARKET_SUPPRESSED",
        "PLAYER_IDENTITY_UNRESOLVED",
        "ENGINE_MISSING", "ENGINE_OUTPUT_IGNORED",
        "CANDIDATE_NOT_GENERATED", "SCORE_BELOW_85",
        "PUBLICATION_BARRIER_REJECT", "NON_CANONICAL_WRITE",
        "FUSION_POST_PUBLICATION",
        "CORRELATED_CONFLICT", "DUPLICATE_PLAYER_MARKET",
        "PARLAY_MARKET_BLOCKED", "ROLLOVER_MARKET_BLOCKED",
        "FIRST_N_CAP_STARVATION",
    }
    got = {c.value for c in ReasonCode}
    missing = required - got
    assert not missing, f"missing reason codes: {missing}"


def test_wiring_status_enum():
    assert {s.value for s in WiringStatus} == {
        "FULLY_WIRED", "PARTIALLY_WIRED", "DEAD_END",
        "NO_REAL_LINE", "UNSUPPORTED", "DISABLED", "BROKEN"}


def test_pipeline_stages_complete_and_ordered():
    # The full pipeline stages from spec §1
    expected_head = ("source", "event_discovery", "real_line",
                      "identity", "feature_engine",
                      "specialized_engine", "model", "simulator",
                      "matchup_history", "candidate_generator",
                      "validation", "gt85_gate",
                      "canonical_publication",
                      "locks", "rollover", "parlay", "settlement")
    assert PIPELINE_STAGES == expected_head


# ── PipelineTrace container ────────────────────────────────────
def test_pipeline_trace_records_reasons_and_evidence():
    t = PipelineTrace(sport="MLB", market="batter_hits")
    t.enter("real_line", n=3)
    t.pass_("real_line", n=2)
    t.drop("real_line", ReasonCode.NO_REAL_LINE, n=1,
            evidence="event 823267 had no batter_hits market")
    d = t.to_dict()
    assert d["stages"]["real_line"]["entered"] == 3
    assert d["stages"]["real_line"]["passed"]  == 2
    assert d["stages"]["real_line"]["dropped"] == 1
    assert d["stages"]["real_line"]["reasons"]["NO_REAL_LINE"] == 1
    assert "823267" in d["stages"]["real_line"]["evidence"][0]


def test_pipeline_trace_accepts_string_reasons_too():
    t = PipelineTrace(sport="NFL")
    t.drop("candidate_generator", "CUSTOM_ADHOC_REASON", n=1)
    assert (t.stages["candidate_generator"].reasons["CUSTOM_ADHOC_REASON"]
            == 1)


# ── Wiring matrix builder ──────────────────────────────────────
def test_build_wiring_matrix_covers_every_enabled_sport():
    m = build_wiring_matrix()
    sports = set(m["sports"].keys())
    for s in ("MLB", "NFL", "NBA", "NHL", "CFB",
              "Soccer", "Tennis", "UFC"):
        assert s in sports, f"missing enabled sport: {s}"


def test_build_wiring_matrix_reports_disabled_sports_explicitly():
    m = build_wiring_matrix()
    for disabled in ("WNBA", "KBO"):
        entry = m["sports"].get(disabled)
        assert entry is not None
        assert entry["enabled"] is False
        assert entry["status_summary"].get("DISABLED", 0) >= 1


def test_matrix_flags_nfl_atd_p0_defect():
    m = build_wiring_matrix()
    defects = m["defects"]
    p0 = [d for d in defects if d["priority"] == "P0"]
    assert any(d["id"] == "NFL-ATD-1" for d in p0), \
        "NFL ATD wiring defect must remain P0 until 2D fixes it"


def test_matrix_flags_mlb_hr_p1_defect():
    m = build_wiring_matrix()
    defects = m["defects"]
    assert any(d["id"] == "MLB-HR-1" for d in defects), \
        "MLB HR intel wiring defect must remain visible until 2D"


def test_matrix_never_labels_ambiguous_market_as_fully_wired_by_default():
    # An unknown (sport, market) tuple must NOT default to FULLY_WIRED
    # — that would allow silent scope drift.
    assert get_wiring_evidence("NBA", "player_double_double") is None


# ── Cross-cutting defects present in the report ────────────────
def test_report_contains_cross_cutting_defect_ids():
    import json, pathlib
    p = pathlib.Path("/tmp/block2a_wiring_matrix.json")
    if not p.exists():
        pytest.skip("wiring matrix report not generated in this env")
    d = json.loads(p.read_text())
    cross = d.get("cross_cutting_defects") or []
    ids = {c["id"] for c in cross}
    assert {"XCUT-1", "XCUT-2", "XCUT-3", "XCUT-4", "XCUT-5"} <= ids
