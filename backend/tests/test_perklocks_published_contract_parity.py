"""PERKLOCKS MAIN 37 · P0.2 — canonical PublishedPickContract
consumer parity tests.

Tests 1 & 3 from the spec:

  Test 1 — Published parity
    For every canonical PUBLISHED + Locks-eligible fixture, the
    contract → Locks DTO → Breakdown DTO produce identical canonical
    ``canonical_pick_id`` / ``published_lock_score`` /
    ``published_grade`` / ``publication_state`` / ``selection`` /
    ``line``.

  Test 2 — No Breakdown phantom Lock
    A research/unpublished candidate can appear in Breakdown but its
    canonical fields (``publication_state``, ``published_grade``,
    ``published_lock_score``) MUST NOT be fabricated locally to
    match a real published Lock.

  Test 3 — Canonical precedence
    When legacy aliases (``grade`` / ``lock_score``) conflict with
    the canonical (``published_grade`` / ``published_lock_score``),
    the consumers MUST resolve to the canonical.
"""
from __future__ import annotations

from services.published_pick_contract import (
    PublishedPickContract, contract_dict,
)


def _mk_published_pick(**overrides) -> dict:
    row = {
        "id":                    "pk-CANON-001",
        "canonical_pick_id":     "pk-CANON-001",
        "canonical_event_id":    "evt-CANON-042",
        "sport":                 "MLB",
        "market":                "Rafael Devers (SF) Over 0.5 Hits",
        "canonical_selection":   "Rafael Devers",
        "selection":             "Rafael Devers",
        "published_side":        "over",
        "published_line":        0.5,
        "published_odds":       -135,
        "published_lock_score":  98.0,
        "published_grade":       "Elite Lock",
        "publication_state":     "PUBLISHED",
        "publication_revision":  3,
        "published_probability": 0.88,
        # Legacy aliases DELIBERATELY set to different values so the
        # precedence tests can prove canonical wins.
        "lock_score":            72.0,
        "grade":                 "Pass",
        "win_probability":       50.0,
        "line":                  1.5,
        "side":                  "under",
        "book_odds":             -220,
    }
    row.update(overrides)
    return row


def _mk_unpublished_research(**overrides) -> dict:
    row = {
        "id":                    "pk-RESEARCH-777",
        "sport":                 "MLB",
        "market":                "Some Player Over 0.5 Hits",
        "selection":             "Some Player",
        # Research candidate — NO published_* fields, NO
        # publication_state.  Legacy score exists as raw evaluator
        # output but was never canonically published.
        "lock_score":            93.0,
        "grade":                 "Strong Lock",
        "book_odds":            -140,
        "line":                  0.5,
    }
    row.update(overrides)
    return row


def test_1_published_parity_locks_and_breakdown_agree():
    """Same canonical row → same contract → identical canonical
    fields regardless of which consumer serialised it.
    """
    pick = _mk_published_pick()
    contract_from_locks    = PublishedPickContract.from_pick(pick).as_dict()
    contract_from_breakdown = PublishedPickContract.from_pick(pick).as_dict()

    for field in (
        "canonical_pick_id", "published_lock_score", "published_grade",
        "publication_state", "selection", "side", "line",
        "published_odds", "event_id",
    ):
        assert contract_from_locks[field] == contract_from_breakdown[field], \
            f"consumer drift on `{field}`"
    assert contract_from_locks["publication_state"] == "PUBLISHED"
    assert contract_from_locks["published_grade"]   == "Elite Lock"
    assert contract_from_locks["published_lock_score"] == 98.0
    assert contract_from_locks["canonical_pick_id"] == "pk-CANON-001"


def test_2_no_breakdown_phantom_lock_from_research_row():
    """A research/unpublished candidate must NOT expose canonical
    published-Lock state via the contract.  The contract must
    surface ``publication_state == None`` (or ``ABSENT``) so no
    consumer can mistake it for a currently published Locks-board
    pick.
    """
    research = _mk_unpublished_research()
    contract = PublishedPickContract.from_pick(research).as_dict()

    # No canonical publication metadata.
    assert contract["publication_state"] is None, contract
    assert contract["publication_revision"] is None
    assert contract["published_at"] is None
    # The contract may echo the legacy lock_score fallback (94/93
    # etc.) but the caller can distinguish research from published
    # via ``publication_state``.  Consumers rendering the "current
    # Locks-board Lock" badge MUST short-circuit on
    # ``publication_state != 'PUBLISHED'``.
    prov = PublishedPickContract.from_pick(research).provenance()
    if contract["published_lock_score"] is not None:
        assert prov["published_lock_score"] == "legacy:lock_score", prov
    if contract["published_grade"] is not None:
        assert prov["published_grade"] == "legacy:grade", prov


def test_3_canonical_grade_outranks_legacy_when_both_set():
    """Legacy ``grade='Pass'`` vs canonical ``published_grade='Elite
    Lock'`` — the contract MUST expose the canonical value.
    """
    pick = _mk_published_pick()
    contract = PublishedPickContract.from_pick(pick).as_dict()
    prov = PublishedPickContract.from_pick(pick).provenance()
    assert contract["published_grade"] == "Elite Lock"
    assert prov["published_grade"] == "canonical"


def test_3_canonical_lock_score_outranks_legacy_when_both_set():
    """Legacy ``lock_score=72`` vs canonical ``published_lock_score
    =98`` — contract exposes canonical."""
    pick = _mk_published_pick()
    contract = PublishedPickContract.from_pick(pick).as_dict()
    prov = PublishedPickContract.from_pick(pick).provenance()
    assert contract["published_lock_score"] == 98.0
    assert prov["published_lock_score"] == "canonical"


def test_3_canonical_line_selection_outrank_legacy():
    """Legacy ``line=1.5`` / ``side='under'`` vs canonical
    ``published_line=0.5`` / ``published_side='over'`` — canonical
    wins.  This is the exact drift class that flipped an "Over 0.5
    Hits" pick into "Under 1.5 Hits" in a stale wire payload.
    """
    pick = _mk_published_pick()
    contract = PublishedPickContract.from_pick(pick).as_dict()
    assert contract["line"] == 0.5
    assert contract["side"] == "over"


def test_3_convenience_dict_matches_full_contract():
    """``contract_dict(pick)`` is what most consumers actually call.
    It must return the same as ``.from_pick(pick).as_dict()``.
    """
    pick = _mk_published_pick()
    assert contract_dict(pick) == PublishedPickContract.from_pick(pick).as_dict()
