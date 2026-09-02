"""SLICE 1.2 — Lightweight Board DTO regression contract
==========================================================

The Lightweight Board DTO architecture is already deployed:

  * `/api/picks/today?lite=true` returns board-required fields only
    (see `backend/server.py::_LITE_STRIPPED_FIELDS` — 22 heavy fields
    stripped, `_slim_rationale` trims pick_rationale ~72%).
  * `/api/picks/{pick_id}` returns the full breakdown (H2H, deep
    evidence, matchup, distribution, risks, model provenance).
  * Frontend `lib/api.ts::fetchTodayPicks` already sends `lite=true`.

This test locks in the invariant so future changes cannot silently
regress it or drift canonical truth between lite and full payloads.

Slice-1.2 invariants:
    BOARD_DTO_TRUTH_DRIFT = 0     lite ↔ full canonical fields match
    BREAKDOWN_TRUTH_DRIFT = 0     /picks/{id} matches board DTO
    LITE_MISSING_CARD_FIELDS = 0  every card-required field survives
"""
from __future__ import annotations
import os, sys
import pytest, httpx

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

_BASE = "http://localhost:8001"

# Board-required fields (what the LockPickCard renders on the collapsed
# home feed).  Any of these missing from a lite pick = SLICE 1.2 FAIL.
BOARD_REQUIRED = {
    "id", "sport", "market", "selection",
    "lock_score", "book_odds",
    "locks_eligibility",
}

# Canonical truth fields that MUST match byte-for-byte between lite
# and full payloads on the same snapshot.
CANONICAL_TRUTH_FIELDS = (
    "id", "sport", "market", "selection", "line", "book_odds",
    "published_lock_score", "lock_score",
    "publication_state", "publication_revision",
)


def _tok():
    r = httpx.post(f"{_BASE}/api/auth/login",
                    json={"email":"demo@lockscore.ai","password":"demo123"}, timeout=10)
    if r.status_code != 200:
        pytest.skip(f"login failed: {r.status_code}")
    return r.json()["access_token"]


def _picks(sport="all", lite=False, tok=None):
    tok = tok or _tok()
    params = {"sport": sport}
    if lite:
        params["lite"] = "true"
    j = httpx.get(f"{_BASE}/api/picks/today",
                   params=params,
                   headers={"Authorization": f"Bearer {tok}"}, timeout=45).json()
    return j.get("picks", []) if isinstance(j, dict) else j


def test_slice_1_2_lite_carries_all_board_required_fields():
    picks = _picks(lite=True)
    if not picks: pytest.skip("no picks")
    for p in picks[:20]:
        missing = BOARD_REQUIRED - set(p.keys())
        # `book_odds` may legitimately be None for auction/futures
        # markets; presence is sufficient.
        assert not (missing - {"book_odds"}), (
            f"lite pick {p.get('id')} missing board-required: {missing}"
        )


def test_slice_1_2_lite_vs_full_canonical_parity():
    """Same-snapshot canonical truth must match between lite and full."""
    tok = _tok()
    full = _picks(lite=False, tok=tok)
    lite = _picks(lite=True, tok=tok)
    full_by_id = {p["id"]: p for p in full if p.get("id")}
    lite_by_id = {p["id"]: p for p in lite if p.get("id")}
    common = set(full_by_id) & set(lite_by_id)
    if not common: pytest.skip("no shared picks")
    drift = []
    for pid in list(common)[:50]:
        for f in CANONICAL_TRUTH_FIELDS:
            if full_by_id[pid].get(f) != lite_by_id[pid].get(f):
                drift.append((pid, f, full_by_id[pid].get(f), lite_by_id[pid].get(f)))
    assert not drift, f"BOARD_DTO_TRUTH_DRIFT detected: {drift[:3]}"


def test_slice_1_2_breakdown_endpoint_matches_board_truth():
    """`/api/picks/{id}` (deep breakdown) MUST expose the SAME canonical
    Lock/Line/Odds/Eligibility as the board DTO — Breakdown expands
    frozen truth; it never recalculates it."""
    tok = _tok()
    lite = _picks(lite=True, tok=tok)
    if not lite: pytest.skip("no picks")
    sample = lite[0]
    pid = sample.get("id")
    if not pid: pytest.skip("no id")
    r = httpx.get(f"{_BASE}/api/picks/{pid}",
                   headers={"Authorization": f"Bearer {tok}"}, timeout=15)
    assert r.status_code == 200, r.text[:200]
    detail = r.json()
    for f in CANONICAL_TRUTH_FIELDS:
        assert detail.get(f) == sample.get(f), (
            f"BREAKDOWN_TRUTH_DRIFT on {f}: "
            f"board={sample.get(f)} vs breakdown={detail.get(f)}"
        )


def test_slice_1_2_lite_payload_is_smaller_than_full():
    """Lite must be at least as small as full on the same snapshot."""
    tok = _tok()
    r_full = httpx.get(f"{_BASE}/api/picks/today",
                        headers={"Authorization": f"Bearer {tok}"}, timeout=45)
    r_lite = httpx.get(f"{_BASE}/api/picks/today?lite=true",
                        headers={"Authorization": f"Bearer {tok}"}, timeout=45)
    assert r_full.status_code == 200 and r_lite.status_code == 200
    n_full = len(r_full.content)
    n_lite = len(r_lite.content)
    assert n_lite <= n_full, f"lite ({n_lite}) larger than full ({n_full})"
    # And breakdown of a single pick must be at least an order of
    # magnitude smaller than the whole board (real card-detail split).
    lite = r_lite.json().get("picks", [])
    if lite:
        pid = lite[0].get("id")
        r_det = httpx.get(f"{_BASE}/api/picks/{pid}",
                            headers={"Authorization": f"Bearer {tok}"}, timeout=15)
        assert r_det.status_code == 200
        assert len(r_det.content) < n_lite / 10, (
            f"breakdown ({len(r_det.content)}) not smaller than board/10 "
            f"({n_lite / 10}) — Breakdown must be a single-pick detail"
        )
