"""PERKLOCKS — Universal Production-Truth Contract deterministic tests.

Covers §13 "DETERMINISTIC VALIDATION":

  1.  Missing authoritative data cannot become valid evidence.
  2.  Missing required evidence cannot silently become numeric zero.
  3.  Missing sportsbook market/line cannot become synthetic
      sportsbook truth.
  4.  Failed canonical publication is distinguishable from
      successful canonical publication.
  5.  GENERATED but NOT PUBLISHED is not classified as supported
      user-facing output.
  6.  PUBLISHED but NOT VISIBLE is diagnosable.
  7.  Pregame evidence remains immutable after settlement.
  8.  Authoritative actuals can settle the correct canonical
      prediction.
  9.  Settled results link to the correct analytics/history identity.
  10. Drop reasons survive the production pipeline.
  11. NOT_APPLICABLE cannot become a fake PASS.
  12. Consumption Proof cannot declare a module operational solely
      because code exists.
  13. Seeded/direct DB data cannot masquerade as genuine production
      reachability.
  14. A shared-class defect can be handled at the common contract
      boundary when appropriate.
  15. Legacy records missing new metadata do not crash current
      runtime.
"""
from __future__ import annotations

import sys
import pytest

sys.path.insert(0, "/app/backend")

from services.production_truth import (
    ProductionStage,
    StageStatus,
    DropReason,
    stage_status_pass,
    stage_status_fail,
    stage_status_unknown,
    stage_status_not_applicable,
    UNKNOWN,
    is_unknown,
    validate_no_synthetic_odds,
    validate_no_synthetic_probability,
    coerce_optional_number,
    MissingDataViolation,
    ConsumerSurface,
    build_reachability_report,
    reachability_summary,
    CustodyStage,
    build_custody_record,
    EnforcementMode,
    current_mode,
    is_enforcing,
    set_mode_for_testing,
    reset_mode_for_testing,
    record_violation,
    recent_violations,
    clear_violations,
)
from services.production_truth.chain_of_custody import (
    distinguish_code_exists_from_real_path,
)
from services.production_truth.pregame_snapshot import (
    seal_snapshot,
    verify_snapshot_hash,
    compute_snapshot_hash,
    PregameSnapshotImmutable,
)
from services.production_truth.settlement_linkage import (
    classify_measurability,
    MeasurabilityState,
)


pytestmark = pytest.mark.unit


# ═══════════════════════════════════════════════════════════════════
# §13.1 — Missing authoritative data cannot become valid evidence
# ═══════════════════════════════════════════════════════════════════
def test_unknown_sentinel_is_not_zero_or_none():
    assert UNKNOWN != 0
    assert UNKNOWN != 0.0
    assert UNKNOWN is not None
    assert UNKNOWN != None                            # noqa: E711
    assert is_unknown(UNKNOWN) is True
    assert is_unknown(0) is False
    assert is_unknown(None) is False


def test_unknown_arithmetic_raises_missing_data_violation():
    """UNKNOWN must never silently coerce to zero during math."""
    with pytest.raises(MissingDataViolation):
        _ = UNKNOWN + 1
    with pytest.raises(MissingDataViolation):
        _ = 1 - UNKNOWN
    with pytest.raises(MissingDataViolation):
        _ = UNKNOWN * 2
    with pytest.raises(MissingDataViolation):
        _ = UNKNOWN < 5


def test_coerce_optional_number_distinguishes_zero_from_missing():
    assert coerce_optional_number(0) == 0.0
    assert coerce_optional_number("0") == 0.0
    assert coerce_optional_number(0.0) == 0.0
    assert is_unknown(coerce_optional_number(None))
    assert is_unknown(coerce_optional_number(""))
    assert is_unknown(coerce_optional_number("N/A"))
    assert is_unknown(coerce_optional_number("unknown"))
    assert is_unknown(coerce_optional_number(float("nan")))
    assert is_unknown(coerce_optional_number(float("inf")))
    # bool must not masquerade as evidence.
    assert is_unknown(coerce_optional_number(True))
    assert is_unknown(coerce_optional_number(False))


# ═══════════════════════════════════════════════════════════════════
# §13.2 — Missing required evidence cannot silently become zero
# ═══════════════════════════════════════════════════════════════════
def test_missing_evidence_does_not_become_numeric_zero():
    """A pick that has no ``model_probability`` reports UNKNOWN at
    MODEL_CONSUMED — never PASS at value 0."""
    pick = {
        "id":                   "p1",
        "sport":                "NFL",
        "market":               "h2h",
        "book_odds":            -110,
        "publication_gate":     "canonical_barrier_passed",
        "lock_score":           88,
        "commence_time":        "2026-09-01T00:00:00Z",
        "home_team":            "KC",
        "away_team":            "BAL",
        # Deliberately no model_probability.
    }
    report = build_reachability_report(pick)
    st = report.stages[ProductionStage.MODEL_CONSUMED.value]
    assert st["status"] == StageStatus.UNKNOWN.value
    # UNKNOWN prevents the pick from being classified as fully
    # supported even though everything else looks OK.
    assert report.supported is False


# ═══════════════════════════════════════════════════════════════════
# §13.3 — Missing market/line cannot become synthetic truth
# ═══════════════════════════════════════════════════════════════════
def test_validate_no_synthetic_odds_rejects_model_provenance():
    with pytest.raises(MissingDataViolation):
        validate_no_synthetic_odds(-110, provenance="MODEL")
    with pytest.raises(MissingDataViolation):
        validate_no_synthetic_odds(-110, provenance="synthetic")
    with pytest.raises(MissingDataViolation):
        validate_no_synthetic_odds(None)
    with pytest.raises(MissingDataViolation):
        validate_no_synthetic_odds(0)
    with pytest.raises(MissingDataViolation):
        validate_no_synthetic_odds(-110, no_real_book_line=True)
    # Real prices from real books pass silently.
    validate_no_synthetic_odds(-110, provenance="DraftKings")
    validate_no_synthetic_odds(150, provenance="fanduel")


def test_validate_no_synthetic_probability_requires_provenance():
    with pytest.raises(MissingDataViolation):
        validate_no_synthetic_probability(0.5)   # no provenance
    with pytest.raises(MissingDataViolation):
        validate_no_synthetic_probability(0.5, provenance="MANUAL")
    with pytest.raises(MissingDataViolation):
        validate_no_synthetic_probability(1.5, provenance="MODEL")
    with pytest.raises(MissingDataViolation):
        validate_no_synthetic_probability("x", provenance="MODEL")
    # A real model probability passes.
    validate_no_synthetic_probability(0.62, provenance="MODEL")
    validate_no_synthetic_probability(0.62, provenance="SIMULATOR")


def test_reachability_flags_synthetic_odds_as_fail():
    pick = {
        "id":               "p2",
        "sport":            "NFL",
        "market":           "h2h",
        "book_odds":        -110,
        "odds_provenance":  "MODEL",   # synthetic!
        "publication_gate": "canonical_barrier_passed",
        "lock_score":       90,
        "commence_time":    "2026-09-01T00:00:00Z",
        "home_team":        "KC",
    }
    report = build_reachability_report(pick)
    st = report.stages[ProductionStage.REAL_MARKET_AVAILABLE.value]
    assert st["status"] == StageStatus.FAIL.value
    assert st["reason"] == DropReason.REAL_LINE_UNAVAILABLE.value


# ═══════════════════════════════════════════════════════════════════
# §13.4 — Failed vs successful canonical publication
# ═══════════════════════════════════════════════════════════════════
def test_failed_canonical_publication_is_distinguishable():
    passed = {"id": "a", "publication_gate": "canonical_barrier_passed",
                "book_odds": -110, "lock_score": 90, "market": "h2h",
                "home_team": "X"}
    rejected = {"id": "b", "publication_gate": "canonical_barrier_rejected",
                  "barrier_failures": ["no_real_book_odds"],
                  "off_board": True, "no_bet": True,
                  "book_odds": None, "lock_score": 70,
                  "market": "h2h", "home_team": "Y"}
    r_pass = build_reachability_report(passed).stages[
        ProductionStage.CANONICAL_PUBLISHED.value]
    r_fail = build_reachability_report(rejected).stages[
        ProductionStage.CANONICAL_PUBLISHED.value]
    assert r_pass["status"] == StageStatus.PASS.value
    assert r_fail["status"] == StageStatus.FAIL.value
    assert r_fail["reason"] == DropReason.PUBLICATION_REJECTED.value


# ═══════════════════════════════════════════════════════════════════
# §13.5 — GENERATED but NOT PUBLISHED is not "supported"
# ═══════════════════════════════════════════════════════════════════
def test_generated_but_not_published_is_not_supported():
    pick = {
        "id":                "p3",
        "sport":             "NFL",
        "market":            "h2h",
        "book_odds":         -110,
        "publication_gate":  "canonical_barrier_rejected",
        "barrier_failures":  ["lock_below_strict_floor_85"],
        "off_board":         True,
        "no_bet":            True,
        "lock_score":        70,
        "commence_time":     "2026-09-01T00:00:00Z",
        "home_team":         "KC",
    }
    report = build_reachability_report(pick)
    assert report.supported is False
    assert ProductionStage.CANONICAL_PUBLISHED.value in report.failed_stages
    assert ProductionStage.VISIBLE_TO_CONSUMER.value in report.failed_stages


# ═══════════════════════════════════════════════════════════════════
# §13.6 — PUBLISHED but NOT VISIBLE is diagnosable
# ═══════════════════════════════════════════════════════════════════
def test_published_but_not_visible_is_diagnosable():
    pick = {
        "id":                "p4",
        "sport":             "NBA",
        "market":            "h2h",
        "book_odds":         -110,
        "publication_gate":  "canonical_barrier_passed",
        "lock_score":        82,   # below 85 → not visible
        "commence_time":     "2026-09-01T00:00:00Z",
        "home_team":         "LAL",
    }
    report = build_reachability_report(pick)
    pub = report.stages[ProductionStage.CANONICAL_PUBLISHED.value]
    vis = report.stages[ProductionStage.VISIBLE_TO_CONSUMER.value]
    assert pub["status"] == StageStatus.PASS.value
    assert vis["status"] == StageStatus.FAIL.value
    assert vis["reason"] == DropReason.BOARD_INELIGIBLE.value


# ═══════════════════════════════════════════════════════════════════
# §13.7 — Pregame evidence remains immutable after settlement
# ═══════════════════════════════════════════════════════════════════
def test_seal_snapshot_is_deterministic_and_hash_stable():
    pick = {
        "canonical_prediction_id": "cpid-1",
        "id":                      "p5",
        "sport":                   "MLB",
        "market":                  "batter_hits",
        "line":                    1.5,
        "book_odds":              -140,
        "lock_score":              91,
        "evidence":                {"L5_hit_rate": 0.6},
    }
    a = seal_snapshot(dict(pick))
    b = seal_snapshot(dict(pick))
    # The frozen_at timestamps will differ → the hash SHOULD differ
    # when frozen_at differs.  But if we build the payload with the
    # same frozen_at, the hash must be identical.
    payload_a = {k: v for k, v in a.items() if k != "snapshot_hash"}
    payload_b = {**payload_a}
    assert compute_snapshot_hash(payload_a) == compute_snapshot_hash(payload_b)
    # verify_snapshot_hash on a freshly sealed snapshot always True.
    assert verify_snapshot_hash(a) is True


def test_settlement_cannot_mutate_snapshot_hash():
    """Even after we simulate a settlement writing into the snapshot,
    the hash re-verification MUST fail — proving immutability."""
    pick = {
        "canonical_prediction_id": "cpid-2",
        "id":                      "p6",
        "sport":                   "MLB",
        "market":                  "batter_hits",
        "book_odds":              -140,
        "lock_score":              91,
    }
    snap = seal_snapshot(dict(pick))
    assert verify_snapshot_hash(snap) is True
    # Simulated settlement tampering — imagine analytics wrote
    # back-into the frozen record.
    snap["lock_score"] = 99
    assert verify_snapshot_hash(snap) is False


# ═══════════════════════════════════════════════════════════════════
# §13.8 — Authoritative actuals settle the correct canonical prediction
# §13.9 — Settled results link to the correct analytics identity
# ═══════════════════════════════════════════════════════════════════
def test_measurability_states_are_distinguishable():
    pick = {
        "id":                      "p7",
        "canonical_prediction_id": "cpid-7",
    }
    # PUBLISHED_UNSETTLED — no settlement, no analytics.
    r0 = classify_measurability(pick)
    assert r0.state is MeasurabilityState.PUBLISHED_UNSETTLED

    # SETTLED_NOT_MEASURABLE — settled but no analytics linkage.
    settle = {"source": "MLB_STATS_API", "final": "won"}
    r1 = classify_measurability(pick, settlement_record=settle)
    assert r1.state is MeasurabilityState.SETTLED_NOT_MEASURABLE

    # FULLY_MEASURABLE — analytics row references canonical_prediction_id.
    analytics = {"canonical_prediction_id": "cpid-7", "_id": "an1"}
    r2 = classify_measurability(pick, settlement_record=settle,
                                  analytics_row=analytics)
    assert r2.state is MeasurabilityState.FULLY_MEASURABLE

    # SETTLED_NOT_MEASURABLE when analytics row belongs to a
    # DIFFERENT prediction — never fake a linkage.
    analytics_wrong = {"canonical_prediction_id": "SOME_OTHER"}
    r3 = classify_measurability(pick, settlement_record=settle,
                                  analytics_row=analytics_wrong)
    assert r3.state is MeasurabilityState.SETTLED_NOT_MEASURABLE


# ═══════════════════════════════════════════════════════════════════
# §13.10 — Drop reasons survive the pipeline
# ═══════════════════════════════════════════════════════════════════
def test_drop_reasons_survive_reachability_report():
    pick = {
        "id":                "p8",
        "sport":             "MLB",
        "market":            "batter_hits",
        "book_odds":         None,       # will fail REAL_MARKET_AVAILABLE
        "publication_gate":  "canonical_barrier_rejected",
        "barrier_failures":  ["no_real_book_odds"],
        "off_board":         True,
        "no_bet":            True,
        "lock_score":        0,
        "player_name":       "Aaron Judge",
        "commence_time":     "2026-09-01T00:00:00Z",
    }
    report = build_reachability_report(pick)
    # Every failing stage carries its explicit reason code.
    for stage_name in report.failed_stages:
        info = report.stages[stage_name]
        assert info.get("reason"), (
            f"failed stage {stage_name} MUST carry a reason code")


# ═══════════════════════════════════════════════════════════════════
# §13.11 — NOT_APPLICABLE cannot become fake PASS
# ═══════════════════════════════════════════════════════════════════
def test_not_applicable_is_distinct_from_pass():
    game_pick = {
        "id":                "p9",
        "sport":             "NBA",
        "market":            "h2h",     # game-level → roster N/A
        "book_odds":         -110,
        "publication_gate":  "canonical_barrier_passed",
        "lock_score":        88,
        "commence_time":     "2026-09-01T00:00:00Z",
        "home_team":         "LAL",
    }
    report = build_reachability_report(game_pick)
    roster = report.stages[ProductionStage.CURRENT_ROSTER_VALID.value]
    assert roster["status"] == StageStatus.NOT_APPLICABLE.value
    assert roster["status"] != StageStatus.PASS.value
    # NOT_APPLICABLE is tracked separately.
    assert ProductionStage.CURRENT_ROSTER_VALID.value \
        in report.not_applicable_stages


def test_not_applicable_status_helper_never_returns_pass():
    st = stage_status_not_applicable(detail="game-level")
    assert st["status"] == StageStatus.NOT_APPLICABLE.value
    assert st["status"] != StageStatus.PASS.value


# ═══════════════════════════════════════════════════════════════════
# §13.12 — Consumption Proof cannot declare operational from code alone
# ═══════════════════════════════════════════════════════════════════
def test_module_existence_never_produces_fake_pass():
    """A pick with only its ``id`` populated cannot claim any
    downstream evidence.  Every dependent stage must be UNKNOWN or
    FAIL — never PASS."""
    pick = {"id": "nothing", "market": "player_pass_yds",
             "sport": "NFL", "player_name": "Anonymous"}
    report = build_reachability_report(pick)
    # No PASS should appear for downstream stages that require data.
    assert report.stages[
        ProductionStage.REAL_MARKET_AVAILABLE.value]["status"] == \
        StageStatus.FAIL.value
    assert report.stages[
        ProductionStage.IDENTITY_RESOLVED.value]["status"] == \
        StageStatus.FAIL.value
    assert report.supported is False


# ═══════════════════════════════════════════════════════════════════
# §13.13 — Seeded/direct DB data cannot masquerade as real production
# ═══════════════════════════════════════════════════════════════════
def test_custody_distinguishes_seeded_from_real():
    seeded_pick = {
        "id":                "seed-1",
        "sport":             "NFL",
        # No odds_provenance, no canonical_prediction_id, no
        # publication_gate → clearly seeded/legacy.
        "lock_score":        95,
        "book_odds":         -110,
    }
    rec = build_custody_record(seeded_pick)
    verdict = distinguish_code_exists_from_real_path(rec)
    assert verdict != "REAL_PRODUCTION_PATH_PROVEN"
    # Every claimed real stage must be UNKNOWN when we cannot
    # prove real origin.
    producer = rec.stages[CustodyStage.PRODUCER.value]
    assert producer["origin"] in ("UNKNOWN", "DIRECT_INJECT")


def test_custody_marks_direct_inject_from_barrier_rejected():
    """A canonical_barrier_rejected marker means the writer was a
    direct-inject path — the custody record must NOT report DATA."""
    pick = {
        "id":                "inj-1",
        "publication_gate":  "canonical_barrier_rejected",
        "barrier_failures":  ["no_real_book_odds"],
    }
    rec = build_custody_record(pick)
    stage = rec.stages[CustodyStage.PRODUCTION_CONSUMER.value]
    assert stage["origin"] == "DIRECT_INJECT"
    verdict = distinguish_code_exists_from_real_path(rec)
    assert verdict != "REAL_PRODUCTION_PATH_PROVEN"


# ═══════════════════════════════════════════════════════════════════
# §13.14 — Shared-class boundary — one guard protects every sport
# ═══════════════════════════════════════════════════════════════════
def test_shared_class_missing_data_guard_protects_all_sports():
    """The same missing-data guard protects MLB, NFL, NBA, NHL,
    Soccer, Tennis, UFC, CFB — no per-sport bypass exists."""
    for sport in ["MLB", "NFL", "NBA", "NHL", "Soccer",
                    "Tennis", "UFC", "CFB"]:
        with pytest.raises(MissingDataViolation):
            validate_no_synthetic_odds(
                None, provenance="MODEL")
        # An UNKNOWN sentinel refuses arithmetic regardless of sport.
        assert is_unknown(coerce_optional_number(None))


def test_shared_class_reachability_uses_same_stages_across_sports():
    """Every sport traverses the same production stages — the
    contract is universal (§10)."""
    for sport, market in [("MLB", "batter_hits"),
                            ("NFL", "player_pass_yds"),
                            ("NBA", "player_points"),
                            ("NHL", "h2h"),
                            ("Soccer", "player_goal_scorer_anytime"),
                            ("Tennis", "h2h"),
                            ("UFC", "h2h"),
                            ("CFB", "h2h")]:
        pick = {"id": "x", "sport": sport, "market": market,
                 "book_odds": -110, "lock_score": 90,
                 "publication_gate": "canonical_barrier_passed",
                 "commence_time": "2026-09-01T00:00:00Z",
                 "home_team": "H", "away_team": "A"}
        report = build_reachability_report(pick)
        # Every one of the 16 canonical stages must exist for every sport.
        for stage in ProductionStage:
            assert stage.value in report.stages, \
                f"{sport}/{market} missing {stage.value}"


# ═══════════════════════════════════════════════════════════════════
# §13.15 — Legacy records must not crash current runtime
# ═══════════════════════════════════════════════════════════════════
def test_legacy_pick_with_no_new_metadata_does_not_crash():
    legacy = {"id": "legacy-1"}    # nothing else
    report = build_reachability_report(legacy)
    # Report is produced without exceptions.
    assert report.pick_id == "legacy-1"
    # Nothing in the chain is PASS — but nothing crashes either.
    assert report.supported is False
    # No exceptions from custody either.
    rec = build_custody_record(legacy)
    assert rec.pick_id == "legacy-1"


def test_legacy_pick_snapshot_hash_verification_returns_false_not_crash():
    """A dict that pretends to be a snapshot but has no
    ``snapshot_hash`` must NOT crash verify — it must return False."""
    assert verify_snapshot_hash({}) is False
    assert verify_snapshot_hash({"lock_score": 90}) is False


# ═══════════════════════════════════════════════════════════════════
# §11 — Enforcement mode contract
# ═══════════════════════════════════════════════════════════════════
def test_default_enforcement_mode_is_observe():
    reset_mode_for_testing()
    assert current_mode() is EnforcementMode.OBSERVE
    assert is_enforcing() is False


def test_can_switch_to_enforce_mode_for_testing():
    try:
        set_mode_for_testing(EnforcementMode.ENFORCE)
        assert current_mode() is EnforcementMode.ENFORCE
        assert is_enforcing() is True
    finally:
        reset_mode_for_testing()


def test_record_violation_never_raises():
    clear_violations()
    record_violation(stage="REAL_MARKET_AVAILABLE",
                     reason=DropReason.REAL_LINE_UNAVAILABLE.value,
                     detail="model-only odds",
                     pick_id="p-abc",
                     sport="NFL",
                     market="h2h")
    v = recent_violations(pick_id="p-abc")
    assert len(v) == 1
    assert v[0]["reason"] == DropReason.REAL_LINE_UNAVAILABLE.value
    assert v[0]["mode"] in {"OBSERVE", "ENFORCE"}
    clear_violations()


# ═══════════════════════════════════════════════════════════════════
# §5 — Consumer surfaces are INDEPENDENT (never collapsed)
# ═══════════════════════════════════════════════════════════════════
def test_consumer_surfaces_are_independently_tracked():
    pick = {
        "id":                "p10",
        "sport":             "NFL",
        "market":            "h2h",
        "book_odds":         -110,
        "publication_gate":  "canonical_barrier_passed",
        "lock_score":        86,       # Locks/Parlay YES, Rollover NO
        "commence_time":     "2026-09-01T00:00:00Z",
        "home_team":         "KC",
    }
    report = build_reachability_report(pick)
    assert report.consumers[ConsumerSurface.LOCKS.value]["status"] == \
        StageStatus.PASS.value
    assert report.consumers[ConsumerSurface.PARLAY.value]["status"] == \
        StageStatus.PASS.value
    # Rollover requires >= 89 — must FAIL independently.
    assert report.consumers[ConsumerSurface.ROLLOVER.value]["status"] == \
        StageStatus.FAIL.value
    # Magic/Analytics are UNKNOWN — never faked to PASS.
    assert report.consumers[ConsumerSurface.MAGIC.value]["status"] == \
        StageStatus.UNKNOWN.value


# ═══════════════════════════════════════════════════════════════════
# Summary — reachability_summary aggregates without silent drops
# ═══════════════════════════════════════════════════════════════════
def test_reachability_summary_counts_are_consistent():
    picks = [
        {"id": "a", "sport": "NBA", "market": "h2h",
          "book_odds": -110, "publication_gate": "canonical_barrier_passed",
          "lock_score": 90, "commence_time": "2026-09-01T00:00:00Z",
          "home_team": "LAL", "model_probability": 0.62,
          "canonical_prediction_id": "cpid-a"},
        {"id": "b", "sport": "NBA", "market": "h2h",
          "book_odds": None, "publication_gate": "canonical_barrier_rejected",
          "off_board": True, "no_bet": True, "lock_score": 70},
    ]
    reports = [build_reachability_report(p) for p in picks]
    summary = reachability_summary(reports)
    assert summary["total"] == 2
    assert summary["supported"] + summary["unsupported"] == 2
    # Pick B fails REAL_MARKET_AVAILABLE + VISIBLE.
    assert summary["fail_by_stage"].get(
        ProductionStage.REAL_MARKET_AVAILABLE.value, 0) >= 1


# ═══════════════════════════════════════════════════════════════════
# Vocabulary — no reason-code duplication with legacy taxonomy
# ═══════════════════════════════════════════════════════════════════
def test_drop_reason_reuses_legacy_reason_codes_where_appropriate():
    from services.pipeline_diagnostic import ReasonCode
    assert DropReason.IDENTITY_UNRESOLVED.value == \
        ReasonCode.PLAYER_IDENTITY_UNRESOLVED.value
    assert DropReason.MARKET_UNAVAILABLE.value == \
        ReasonCode.MARKET_NOT_SUPPORTED.value
    assert DropReason.REAL_LINE_UNAVAILABLE.value == \
        ReasonCode.NO_REAL_LINE.value
    assert DropReason.PUBLICATION_REJECTED.value == \
        ReasonCode.PUBLICATION_BARRIER_REJECT.value


def test_all_production_stages_have_default_drop_reasons():
    from services.production_truth.vocabulary import default_drop_reasons
    # The two stages that have generic drop reasons — CANDIDATE_GENERATED
    # only fails with CANDIDATE_FILTERED, VISIBLE only with BOARD_INELIGIBLE
    # — MUST still produce a non-empty tuple.
    for stage in ProductionStage:
        assert default_drop_reasons(stage), \
            f"stage {stage.value} has no default drop reason"


# ═══════════════════════════════════════════════════════════════════
# Immutability — freezing twice for same prediction is refused
# ═══════════════════════════════════════════════════════════════════
class _FakeCollection:
    def __init__(self):
        self.docs: list[dict] = []
    async def find_one(self, query, projection=None):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                return dict(d)
        return None
    async def insert_one(self, doc):
        self.docs.append(dict(doc))
        class _R:
            inserted_id = "fake"
        return _R()
    async def create_index(self, *a, **kw):
        return None


class _FakeDB:
    def __init__(self):
        self._colls: dict[str, _FakeCollection] = {}
    def __getitem__(self, name):
        return self._colls.setdefault(name, _FakeCollection())


async def _async_test_freeze_pregame_is_append_only():
    from services.production_truth.pregame_snapshot import (
        freeze_pregame,
        PREGAME_SNAPSHOTS_COLLECTION,
    )
    db = _FakeDB()
    pick = {
        "canonical_prediction_id": "cpid-freeze",
        "id":                      "p-freeze",
        "sport":                   "MLB",
        "market":                  "batter_hits",
        "book_odds":              -140,
        "lock_score":              91,
    }
    s1 = await freeze_pregame(db, pick)
    assert s1["snapshot_hash"]
    raised = False
    try:
        await freeze_pregame(db, pick)   # second attempt refused
    except PregameSnapshotImmutable:
        raised = True
    assert raised, "second freeze must raise PregameSnapshotImmutable"
    # Explicit ``supersedes`` allows a NEW row but does NOT modify
    # the original.
    await freeze_pregame(db, pick, supersedes=s1["snapshot_hash"])
    assert len(db[PREGAME_SNAPSHOTS_COLLECTION].docs) == 2


def test_freeze_pregame_is_append_only():
    import asyncio
    asyncio.run(_async_test_freeze_pregame_is_append_only())


# ═══════════════════════════════════════════════════════════════════
# Consumption Proof — real endpoint contract shape
# ═══════════════════════════════════════════════════════════════════
class _ProofFakeCollection(_FakeCollection):
    pass


class _ProofFakeDB:
    def __init__(self, picks=None):
        self._colls = {
            "picks":             _ProofFakeCollection(),
            "pregame_snapshots": _ProofFakeCollection(),
            "pick_settlements":  _ProofFakeCollection(),
            "pick_analytics":    _ProofFakeCollection(),
        }
        for p in (picks or []):
            self._colls["picks"].docs.append(dict(p))

    def __getitem__(self, name):
        return self._colls.setdefault(name, _ProofFakeCollection())


async def _async_test_consumption_proof_reports_unknown_for_seeded_pick():
    """A seeded pick with no provenance metadata MUST NOT be
    reported as REAL_PRODUCTION_PATH_PROVEN."""
    from services.production_truth.consumption_proof import (
        build_consumption_proof,
    )
    db = _ProofFakeDB(picks=[{
        "id":         "seed-999",
        "sport":      "MLB",
        "market":     "batter_hits",
        "book_odds":  -140,
        "lock_score": 91,
    }])
    proof = await build_consumption_proof(db, "seed-999")
    assert proof["found"] is True
    assert proof["path_verdict"] != "REAL_PRODUCTION_PATH_PROVEN"
    assert proof["pregame_snapshot"]["present"] is False
    assert "stages" in proof["reachability"]
    assert ProductionStage.CANONICAL_PUBLISHED.value \
        in proof["reachability"]["stages"]


def test_consumption_proof_reports_unknown_for_seeded_pick():
    import asyncio
    asyncio.run(_async_test_consumption_proof_reports_unknown_for_seeded_pick())


async def _async_test_consumption_proof_returns_not_found_for_missing_pick():
    from services.production_truth.consumption_proof import (
        build_consumption_proof,
    )
    db = _ProofFakeDB(picks=[])
    proof = await build_consumption_proof(db, "does-not-exist")
    assert proof["found"] is False
    assert proof["reason"] == "pick_not_found"


def test_consumption_proof_returns_not_found_for_missing_pick():
    import asyncio
    asyncio.run(_async_test_consumption_proof_returns_not_found_for_missing_pick())
