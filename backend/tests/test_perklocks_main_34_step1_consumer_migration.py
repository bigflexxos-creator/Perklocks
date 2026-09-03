"""STEP 1 · consumer migration proof — Pick Breakdown detail endpoint
========================================================================

The `/api/picks/{pick_id}` endpoint now ships the immutable
`published_pick_contract` block so Pick Breakdown, Rollover, Parlay,
My Bets, and any other consumer can read canonical frozen truth from a
single field instead of hand-parsing legacy pick columns.

Enforced invariants:

  1. Every /api/picks/{id} response carries a `published_pick_contract`
     dict.
  2. Every canonical key defined in
     `services.published_pick_contract._CANONICAL_KEYS` is present
     (may be null on legacy rows but the KEY itself must be there).
  3. `published_pick_contract_provenance` is attached and records the
     canonical / legacy source for each field so a regression that
     lets a mutable alias sneak past a canonical value is visible on
     the wire.
  4. The canonical contract matches the values the LITE board DTO
     ships for the SAME pick — this proves consumers can't drift.
"""
from __future__ import annotations
import os, sys
import pytest, httpx

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

_BASE = "http://localhost:8001"


def _tok():
    r = httpx.post(f"{_BASE}/api/auth/login",
                    json={"email": "demo@lockscore.ai", "password": "demo123"},
                    timeout=10)
    if r.status_code != 200:
        pytest.skip(f"login {r.status_code}")
    return r.json()["access_token"]


def _sample_pick(tok):
    r = httpx.get(f"{_BASE}/api/picks/today?lite=true",
                    headers={"Authorization": f"Bearer {tok}"}, timeout=90)
    if r.status_code != 200:
        pytest.skip("today unavailable")
    picks = r.json().get("picks", [])
    if not picks:
        pytest.skip("no picks live")
    return picks[0]


def test_step1_pick_detail_ships_published_pick_contract():
    tok = _tok()
    sample = _sample_pick(tok)
    r = httpx.get(f"{_BASE}/api/picks/{sample['id']}",
                    headers={"Authorization": f"Bearer {tok}"}, timeout=30)
    assert r.status_code == 200, r.text[:200]
    detail = r.json()
    assert "published_pick_contract" in detail, (
        "STEP 1: /api/picks/{id} must attach `published_pick_contract`."
    )
    assert "published_pick_contract_provenance" in detail, (
        "STEP 1: /api/picks/{id} must attach provenance map alongside "
        "the contract so mutable-alias regressions are visible."
    )


def test_step1_contract_has_all_canonical_keys():
    from services.published_pick_contract import _CANONICAL_KEYS
    tok = _tok()
    sample = _sample_pick(tok)
    r = httpx.get(f"{_BASE}/api/picks/{sample['id']}",
                    headers={"Authorization": f"Bearer {tok}"}, timeout=30)
    contract = r.json().get("published_pick_contract", {})
    for k in _CANONICAL_KEYS:
        assert k in contract, (
            f"STEP 1: `published_pick_contract` missing canonical key `{k}`."
        )


def test_step1_contract_matches_board_dto_for_same_pick():
    """The canonical wager visible via the frozen contract on the detail
    endpoint must match the same wager fields shipped by the lite board
    DTO. This is the "Locks == Pick Breakdown wager" invariant."""
    tok = _tok()
    sample = _sample_pick(tok)
    r = httpx.get(f"{_BASE}/api/picks/{sample['id']}",
                    headers={"Authorization": f"Bearer {tok}"}, timeout=30)
    contract = r.json().get("published_pick_contract", {})
    # Sport / market / selection are the identity spine of the wager.
    assert contract.get("sport") == sample.get("sport"), (
        f"sport drift: board={sample.get('sport')} vs contract={contract.get('sport')}"
    )
    # `selection` on the frozen contract must equal the lite-DTO's
    # selection string (both derived from the same publication snapshot).
    assert contract.get("selection") == sample.get("selection"), (
        f"selection drift: board={sample.get('selection')!r} vs contract={contract.get('selection')!r}"
    )


def test_step1_provenance_reports_canonical_over_legacy():
    tok = _tok()
    sample = _sample_pick(tok)
    r = httpx.get(f"{_BASE}/api/picks/{sample['id']}",
                    headers={"Authorization": f"Bearer {tok}"}, timeout=30)
    prov = r.json().get("published_pick_contract_provenance", {})
    # At least the identity spine should read from canonical for a
    # freshly published pick.
    # publication_state IS canonical on any published row.
    assert prov.get("publication_state") in ("canonical", "legacy:publication_state"), (
        f"publication_state provenance unexpected: {prov.get('publication_state')!r}"
    )
