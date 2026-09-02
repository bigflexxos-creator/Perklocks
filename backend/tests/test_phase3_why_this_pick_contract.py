"""Phase 3 — Why-This-Pick structured payload invariants.

Root-class proofs the directive requires:

  W1. Rationale MUST be a dict, never a bare string, never None on
      a publishable pick.
  W2. Rationale MUST contain required keys AND at least one
      substantive payload field (evidence bullet, factor row, or a
      structured engine summary).
  W3. Vacuous fallback text ("summary only, empty evidence, no
      factors") is REJECTED by the publication-time assertion.
  W4. The publication service's PublishedPayload freezes the
      rationale AS-IS into `published_reasoning`; `hydrate()` reads
      it back byte-for-byte identical.
  W5. The Locks card and Pick Breakdown pull from the SAME frozen
      rationale — the UI never re-generates.
"""
from __future__ import annotations
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest

from services.why_this_pick_contract import (
    validate_rationale,
    assert_publishable_rationale,
    RationaleContractError,
    REQUIRED_KEYS,
)


def _base_rationale(**extra):
    r = {
        "summary": "Home ML · 68% model win prob, +5.4pp over book",
        "evidence": ["✅ Ace pitcher matchup",
                     "📊 Home team 8-2 last 10 vs LHP"],
        "concerns": [],
        "data_source": "model",
        "model_win_prob_pct": 68.0,
        "edge_percent": 5.4,
        "lock_score": 89.0,
    }
    r.update(extra)
    return r


# ── W1 · types ───────────────────────────────────────────────────
def test_rejects_none():
    v = validate_rationale(None)
    assert v["ok"] is False
    assert "rationale_is_none" in v["reasons"]


def test_rejects_bare_string():
    v = validate_rationale("Home ML · 68% chance")
    assert v["ok"] is False
    assert "rationale_is_bare_string" in v["reasons"]


def test_rejects_wrong_type():
    v = validate_rationale(["evidence", "list"])
    assert v["ok"] is False
    assert any(r.startswith("rationale_wrong_type") for r in v["reasons"])


# ── W2 · required keys ───────────────────────────────────────────
def test_rejects_missing_required_key():
    r = _base_rationale()
    del r["evidence"]
    v = validate_rationale(r)
    assert v["ok"] is False
    assert "evidence" in v["missing_keys"]


def test_accepts_full_structured_rationale():
    v = validate_rationale(_base_rationale())
    assert v["ok"] is True
    assert v["is_substantive"] is True


# ── W3 · vacuous fallback rejected ───────────────────────────────
def test_rejects_vacuous_summary_only_rationale():
    r = _base_rationale(evidence=[])
    # Also strip any substantive payload keys.
    v = validate_rationale(r)
    assert v["ok"] is False
    assert "vacuous_rationale_no_evidence_or_factors" in v["reasons"]


def test_accepts_factor_only_rationale_without_evidence_list():
    """A pick with structured `top_factors` but empty evidence list
    is still substantive (factor payload counts)."""
    r = _base_rationale(evidence=[], top_factors=[
        {"name": "starter_stuff_plus", "value": 112, "band": "elite"},
        {"name": "team_offense_rank",  "value": 5,   "band": "top10"},
    ])
    v = validate_rationale(r)
    assert v["ok"] is True
    assert v["is_substantive"] is True


def test_accepts_monte_carlo_summary_without_bullets():
    r = _base_rationale(evidence=[], monte_carlo_summary=(
        "Monte Carlo · 62% over · projects 1.4 · n=10000"))
    v = validate_rationale(r)
    assert v["ok"] is True


def test_publication_hard_assert_raises_on_vacuous():
    r = _base_rationale(evidence=[])
    with pytest.raises(RationaleContractError):
        assert_publishable_rationale(r)


def test_publication_hard_assert_passes_on_substantive():
    # Must not raise.
    assert_publishable_rationale(_base_rationale())


# ── W4 · publication payload freezes rationale as-is ────────────
def test_publication_payload_preserves_full_rationale_dict():
    from services.prediction_publication_service import PublishedPayload
    from datetime import datetime, timezone
    rat = _base_rationale(
        top_factors=[{"name": "wind", "value": 12.0, "band": "high"}],
        matchup_summary="Direct H2H · 65% over · projects 1.4 · n=6",
    )
    payload = PublishedPayload(
        prediction_id="pR", pick_id="pR", snapshot_version=1,
        board_version="board-2026-06-01",
        published_probability=0.68, published_edge=5.4,
        published_lock_score=89.0, published_grade="Lock",
        published_confidence="High", published_confidence_score=None,
        published_reasoning=rat,
        published_line=None, published_odds=-140,
        model_version="v1", fusion_version="v1", scoring_version="v1",
        calibration_version="v1", validator_version="v1",
        simulation_version="v1", feature_snapshot_version="v1",
        publication_source="canonical_pipeline",
    )
    d = payload.to_snapshot_dict(
        payload_hash="", idempotency_key="",
        published_at=datetime(2000, 1, 1, tzinfo=timezone.utc),
    )
    assert d["published_reasoning"] == rat
    # Nested lists / dicts preserved exactly.
    assert d["published_reasoning"]["top_factors"][0]["name"] == "wind"


# ── W5 · hydrate round-trips the rationale ──────────────────────
def test_hydrate_round_trips_rationale():
    from services.published_prediction_reader import hydrate
    rat = _base_rationale()
    pick = {
        "id": "pH",
        "published_lock_score": 89.0,
        "published_probability": 0.68,
        "published_edge":        5.4,
        "published_grade":       "Lock",
        "published_confidence":  "High",
        "published_odds":        -140,
        "published_line":        None,
        "published_reasoning":   rat,
    }
    h = hydrate(pick)
    assert h["reasoning"] == rat


# ── W2 (existing pipeline) — pick_enrichment produces a valid shape
def test_pick_enrichment_build_rationale_shape_is_valid():
    """The existing pick_enrichment._build_rationale must produce a
    dict that at minimum passes required-keys."""
    from pick_enrichment import _build_rationale
    fake = {
        "sport": "mlb",
        "player_name": "Test Player",
        "win_probability": 63.4,
        "edge_percent": 3.2,
        "lock_score": 87.0,
        "source": "model",
    }
    r = _build_rationale(fake, "mlb", "Test Player")
    assert isinstance(r, dict)
    for k in ("summary", "evidence", "concerns", "data_source",
             "model_win_prob_pct", "edge_percent", "lock_score"):
        assert k in r, f"pick_enrichment missing {k}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
