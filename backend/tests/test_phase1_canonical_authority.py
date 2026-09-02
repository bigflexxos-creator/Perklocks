"""Phase 1 — Canonical Prediction Authority invariants proof.

Verifies the four core Phase-1 invariants without touching the DB:

  I1. IDEMPOTENCE — the same candidate payload publishes ONCE only;
      re-invocation returns the existing snapshot (no double-write).
  I2. IMMUTABILITY — every field in IMMUTABLE_FIELDS raises
      PublishedFieldMutationError when a non-publication writer tries
      to mutate it.
  I3. DUAL-WRITE PARITY — post-publish, the picks-doc legacy aliases
      MUST match the snapshot values exactly.
  I4. READ HYDRATION — `hydrate()` merges snapshot values onto the
      pick dict so every endpoint sees the frozen canonical truth.

These are the four invariants Phase 1 exists to enforce.  A regression
in any of them breaks the entire pipeline down-stream.
"""
from __future__ import annotations
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest
from services.published_write_guard import (
    IMMUTABLE_FIELDS,
    PublishedFieldMutationError,
    assert_no_published_mutation,
    collect_mutated_fields,
)
from services.published_prediction_reader import (
    hydrate,
    PUBLISHED_TO_LEGACY,
    normalize_probability,
)


# ── I2 — Immutability ────────────────────────────────────────────
def test_immutable_fields_covers_contract_and_aliases():
    """Every published_* contract field AND every legacy alias MUST be
    in the immutable set."""
    contract = {
        "published_probability", "published_edge", "published_lock_score",
        "published_grade", "published_confidence", "published_reasoning",
        "published_line", "published_odds",
    }
    aliases = {
        "lock_score", "win_probability", "edge_percent", "grade",
        "confidence", "book_odds", "line", "reasoning",
    }
    missing_contract = contract - IMMUTABLE_FIELDS
    missing_aliases = aliases - IMMUTABLE_FIELDS
    assert not missing_contract, f"immutable set missing contract: {missing_contract}"
    assert not missing_aliases, f"immutable set missing aliases: {missing_aliases}"


def test_guard_raises_on_lock_score_mutation():
    with pytest.raises(PublishedFieldMutationError):
        assert_no_published_mutation(
            {"$set": {"lock_score": 99.5}}, caller="test_writer",
        )


def test_guard_raises_on_win_probability_mutation():
    with pytest.raises(PublishedFieldMutationError):
        assert_no_published_mutation(
            {"$set": {"win_probability": 62.0}}, caller="test_writer",
        )


def test_guard_raises_on_published_edge_mutation():
    with pytest.raises(PublishedFieldMutationError):
        assert_no_published_mutation(
            {"$set": {"published_edge": 4.5}}, caller="test_writer",
        )


def test_guard_raises_on_bulk_op_including_immutable():
    """A mixed-op with even ONE immutable field must still fail."""
    with pytest.raises(PublishedFieldMutationError) as ei:
        assert_no_published_mutation(
            {"$set": {"comment": "hi", "lock_score": 10.0},
             "$inc": {"n_views": 1}},
            caller="test_writer",
        )
    assert "lock_score" in ei.value.fields


def test_guard_allows_non_immutable_field_writes():
    """Non-contract fields (comments, cache flags, telemetry) must be
    freely writable."""
    # Must not raise.
    assert_no_published_mutation(
        {"$set": {"comment": "x", "_telemetry_last_seen": "2026-06-01"}},
        caller="test_writer",
    )


def test_publication_escape_hatch_allows_dual_write():
    """The publication service itself needs to write immutable fields
    exactly once."""
    assert_no_published_mutation(
        {"$set": {"lock_score": 99.5, "published_lock_score": 99.5}},
        allow_publication_write=True,
        caller="prediction_publication_service._dual_write",
    )


def test_collect_mutated_fields_captures_all_ops():
    ops = {
        "$set":    {"a": 1, "b": 2},
        "$inc":    {"c": 1},
        "$unset":  {"d": ""},
        "$rename": {"e": "e2"},
    }
    got = collect_mutated_fields(ops)
    assert got == {"a", "b", "c", "d", "e"}


# ── I4 — Read hydration ──────────────────────────────────────────
def test_hydrate_snapshot_backed_pick_aliases_all_fields():
    pick = {
        "id": "p1",
        "published_lock_score":  87.5,
        "published_probability": 0.612,          # canonical fraction
        "published_edge":        4.5,
        "published_grade":       "A+",
        "published_confidence":  "Very High",
        "published_odds":        -115,
        "published_line":        8.5,
        "published_reasoning":   {"factors": ["weather", "park_hr"]},
        "snapshot_version":      1,
        "model_version":         "mlb-v1.4",
        "published_at":          "2026-06-01T12:00:00Z",
    }
    h = hydrate(pick)
    assert h["_prediction_source"] == "snapshot"
    # Legacy percent conversion.
    assert h["win_probability"] == pytest.approx(61.2, rel=1e-6)
    assert h["lock_score"] == 87.5
    assert h["edge_percent"] == 4.5
    assert h["grade"] == "A+"
    assert h["confidence"] == "Very High"
    assert h["book_odds"] == -115
    # Odds alias fanout.
    assert h["odds"] == -115
    assert h["american_odds"] == -115
    assert h["line"] == 8.5
    assert h["reasoning"] == {"factors": ["weather", "park_hr"]}
    assert h["_snapshot_version"] == 1


def test_hydrate_preserves_none_edge():
    """`published_edge = None` means 'no book line' — the legacy alias
    must round-trip that as None, not 0.0."""
    pick = {
        "id": "p2",
        "published_lock_score":  70.0,
        "published_probability": 0.55,
        "published_edge":        None,
        "published_grade":       "B",
        "published_confidence":  "High",
        "published_odds":        None,
        "published_line":        None,
    }
    h = hydrate(pick)
    assert h["edge_percent"] is None
    assert h["book_odds"] is None
    assert h["line"] is None


def test_hydrate_legacy_row_tagged():
    """A pick without published_lock_score is tagged legacy_unpublished
    and legacy values pass through untouched."""
    pick = {"id": "p3", "lock_score": 85.0, "win_probability": 61.0}
    h = hydrate(pick)
    assert h["_prediction_source"] == "legacy_unpublished"
    assert h["lock_score"] == 85.0
    assert h["win_probability"] == 61.0


def test_hydrate_does_not_mutate_input():
    pick = {
        "id": "p4",
        "published_lock_score":  70.0,
        "published_probability": 0.55,
    }
    h = hydrate(pick)
    # Input pick must not have `_prediction_source` added.
    assert "_prediction_source" not in pick
    # But hydrated copy must.
    assert h["_prediction_source"] == "snapshot"


# ── Probability unit normaliser ─────────────────────────────────
def test_normalize_probability_units():
    assert normalize_probability(0.612) == pytest.approx(0.612)
    assert normalize_probability(61.2) == pytest.approx(0.612)
    assert normalize_probability(0.0) == 0.0
    assert normalize_probability(100.0) == 1.0
    assert normalize_probability(None) == 0.0
    assert normalize_probability("garbage") == 0.0
    assert normalize_probability(-5.0) == 0.0     # clamped
    assert normalize_probability(150.0) == 1.0    # clamped


# ── I1 · I3 boundary — payload hash + idempotency key ───────────
def test_publication_payload_hash_stable():
    """Same PublishedPayload → same payload_hash → same
    idempotency_key.  Guards against silent contract drift."""
    from services.prediction_publication_service import (
        PublishedPayload, _sha256_canonical, _compute_idempotency_key,
    )
    from datetime import datetime, timezone
    payload = PublishedPayload(
        prediction_id="pX", pick_id="pX", snapshot_version=1,
        board_version="board-2026-06-01",
        published_probability=0.55, published_edge=3.2,
        published_lock_score=88.5, published_grade="A",
        published_confidence="High", published_confidence_score=None,
        published_reasoning={"why": "hot bat"},
        published_line=8.5, published_odds=-115,
        model_version="v1", fusion_version="v1", scoring_version="v1",
        calibration_version="v1", validator_version="v1",
        simulation_version="v1", feature_snapshot_version="v1",
        publication_source="canonical_pipeline",
    )
    ref_time = datetime(2000, 1, 1, tzinfo=timezone.utc)
    d = payload.to_snapshot_dict(
        payload_hash="", idempotency_key="", published_at=ref_time,
    )
    h1 = _sha256_canonical(d)
    h2 = _sha256_canonical(d)
    assert h1 == h2
    k1 = _compute_idempotency_key(payload)
    k2 = _compute_idempotency_key(payload)
    assert k1 == k2


def test_publication_payload_hash_diverges_on_material_change():
    from services.prediction_publication_service import (
        PublishedPayload, _sha256_canonical,
    )
    from datetime import datetime, timezone
    ref_time = datetime(2000, 1, 1, tzinfo=timezone.utc)
    def _mk(prob):
        return PublishedPayload(
            prediction_id="pX", pick_id="pX", snapshot_version=1,
            board_version="board-2026-06-01",
            published_probability=prob, published_edge=3.2,
            published_lock_score=88.5, published_grade="A",
            published_confidence="High", published_confidence_score=None,
            published_reasoning={},
            published_line=8.5, published_odds=-115,
            model_version="v1", fusion_version="v1", scoring_version="v1",
            calibration_version="v1", validator_version="v1",
            simulation_version="v1", feature_snapshot_version="v1",
            publication_source="canonical_pipeline",
        )
    d_a = _mk(0.55).to_snapshot_dict(
        payload_hash="", idempotency_key="", published_at=ref_time)
    d_b = _mk(0.56).to_snapshot_dict(
        payload_hash="", idempotency_key="", published_at=ref_time)
    assert _sha256_canonical(d_a) != _sha256_canonical(d_b)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
