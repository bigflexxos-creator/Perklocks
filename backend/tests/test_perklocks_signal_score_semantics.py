"""PERKLOCKS MAIN 37 · P0.3 — signal_score semantics regression.

Spec Test 4: If signal_score and lock_score are intentionally
separate, assert Lock badge / tier / board eligibility use canonical
lock score, while signal_score remains separately labeled research
evidence.

Signal_score is the slate-wide percentile rank output of
``services.signal_engine.engine`` — a research metric completely
independent from the canonical PublishedPickContract Lock score.
The wire response MUST:
  * expose ``signal_score`` as a distinct field
  * label it explicitly (``signal_score_label``)
  * NEVER let it override / merge into the Lock badge canonical
    (``published_lock_score`` / ``published_grade``)
"""
from __future__ import annotations

from services.published_pick_contract import PublishedPickContract


def _mk_pick_with_conflicting_scores() -> dict:
    """A pick where signal_score contradicts published_lock_score.
    The Lock badge MUST render from published_lock_score, not
    signal_score.
    """
    return {
        "id":                    "pk-CANON-SIG-001",
        "canonical_pick_id":     "pk-CANON-SIG-001",
        "sport":                 "MLB",
        "market":                "Rafael Devers (SF) Over 0.5 Hits",
        "selection":             "Rafael Devers",
        "canonical_selection":   "Rafael Devers",
        "published_line":        0.5,
        "published_side":        "over",
        "published_odds":       -135,
        "published_lock_score":  98.0,
        "published_grade":       "Elite Lock",
        "publication_state":     "PUBLISHED",
        # SIGNAL score dramatically disagrees — this is the exact
        # UI-contradiction shape ("Over 1.5 scores 69 vs your pick
        # at 0" beside "93 Lock BEST").
        "signal_score":          23,
        "signal_score_raw":      31,
        "lock_score":            72.0,   # stale legacy
        "grade":                 "Pass", # stale legacy
    }


def test_lock_badge_reads_published_lock_score_not_signal_score():
    """Canonical Lock authority must come from
    ``published_lock_score``. ``signal_score`` and ``lock_score``
    legacy are ignored for the Lock badge.
    """
    pick = _mk_pick_with_conflicting_scores()
    contract = PublishedPickContract.from_pick(pick).as_dict()
    # Contract Lock authority = canonical published value.
    assert contract["published_lock_score"] == 98.0
    assert contract["published_grade"]       == "Elite Lock"
    # signal_score is NOT surfaced anywhere on the canonical contract.
    assert "signal_score" not in contract
    assert "signal_score_raw" not in contract


def test_signal_score_stays_distinct_research_evidence():
    """The row-level ``signal_score`` may travel on the wire as a
    separate research metric, but it MUST NOT be used to derive
    Lock authority.  A pick with an appalling signal_score can still
    be an Elite Lock.
    """
    pick = _mk_pick_with_conflicting_scores()
    # Signal metrics remain on the raw row for research/lab surfaces.
    assert pick["signal_score"] == 23
    # But when the canonical contract is asked "what is the Lock
    # score?" it returns the PUBLISHED value, not the signal value.
    contract = PublishedPickContract.from_pick(pick).as_dict()
    assert contract["published_lock_score"] == 98.0
    # And the tier resolves to the CANONICAL grade, ignoring the
    # low signal_score entirely.
    assert contract["published_grade"] == "Elite Lock"


def test_market_competition_row_labels_signal_score_explicitly():
    """The Market Competition wire response includes an explicit
    ``signal_score_label`` field so the frontend never merges the
    metric into the Lock badge and no consumer can misread the
    number as an authoritative Lock score.
    """
    import inspect
    from market_competition import routes as _mc_routes
    src = inspect.getsource(_mc_routes)
    # Both wire fields must ship on the current-pick row.
    assert '"signal_score":' in src
    assert '"signal_score_label":' in src, (
        "signal_score must ship with an explicit label so the UI "
        "never renders it as a canonical Lock score."
    )


def test_contract_provenance_marks_lock_source_correctly():
    """The provenance tag on the immutable contract must attribute
    published_lock_score to 'canonical' — not to signal_score or
    legacy lock_score.  This is what regression telemetry watches
    for silent consumer drift.
    """
    pick = _mk_pick_with_conflicting_scores()
    prov = PublishedPickContract.from_pick(pick).provenance()
    assert prov["published_lock_score"] == "canonical"
    assert prov["published_grade"] == "canonical"


def test_research_only_pick_has_no_canonical_lock():
    """A pure research candidate — ``signal_score`` populated but
    NO ``published_*`` fields — MUST resolve to
    ``publication_state == None``.  This is what stops the "phantom
    lock" bug where the frontend rendered a research candidate as a
    currently published Lock badge.
    """
    research = {
        "id":             "pk-RESEARCH-999",
        "sport":          "MLB",
        "market":         "Some Player Over 0.5 Hits",
        "selection":      "Some Player",
        "signal_score":   88,
        "signal_score_raw": 92,
        "lock_score":     93.0,        # stale evaluator output
        "grade":          "Strong Lock",
    }
    contract = PublishedPickContract.from_pick(research).as_dict()
    assert contract["publication_state"] is None
    assert contract["publication_revision"] is None
    # The contract may echo the legacy lock_score fallback but the
    # publication_state=None short-circuit is what stops phantom
    # Lock badges downstream.
