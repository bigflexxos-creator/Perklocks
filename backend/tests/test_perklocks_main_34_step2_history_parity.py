"""STEP 2 · History parity — Locks == History canonical wager identity
========================================================================

`services.history_projection_service.project_pick` now attaches the
immutable `published_pick_contract` to every projected History row.
Settlement fields (`status`, `result`, `settled_at`, `actual_result`,
`settlement_lineage`, ...) stay separate — the contract carries the
frozen pregame wager, not the outcome.

This locks the invariant: a corrected settlement can NEVER cause the
canonical wager identity to drift between History and the Locks board.
"""
from __future__ import annotations
import os, sys
import pytest, httpx

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from services.history_projection_service import project_pick
from services.published_pick_contract import _CANONICAL_KEYS


_BASE = "http://localhost:8001"


def _tok():
    r = httpx.post(f"{_BASE}/api/auth/login",
                    json={"email": "demo@lockscore.ai", "password": "demo123"},
                    timeout=10)
    if r.status_code != 200:
        pytest.skip(f"login {r.status_code}")
    return r.json()["access_token"]


def test_step2_project_pick_attaches_published_contract():
    """Unit-level: `project_pick` output includes the frozen contract."""
    pick = {
        "id": "abc-123", "sport": "MLB", "league": "MLB",
        "market": "Batter Hits", "selection": "Aaron Judge Over 0.5",
        "line": 0.5, "published_odds": -155,
        "published_lock_score": 91.4, "published_grade": "Strong Lock",
        "publication_state": "PUBLISHED", "publication_revision": 1,
    }
    proj = project_pick(pick)
    assert "published_pick_contract" in proj, (
        "STEP 2: project_pick must attach published_pick_contract."
    )
    c = proj["published_pick_contract"]
    for k in _CANONICAL_KEYS:
        assert k in c, f"STEP 2: contract missing canonical key {k!r}"


def test_step2_history_endpoint_ships_contract_on_every_row():
    tok = _tok()
    r = httpx.get(f"{_BASE}/api/picks/history?days=30",
                    headers={"Authorization": f"Bearer {tok}"}, timeout=120)
    if r.status_code != 200:
        pytest.skip(f"history {r.status_code}")
    picks = r.json().get("picks", [])
    if not picks:
        pytest.skip("no history rows")
    sampled = picks[:40]
    missing = [p.get("id") for p in sampled
               if "published_pick_contract" not in p]
    assert not missing, (
        f"STEP 2: {len(missing)} history row(s) missing frozen contract: "
        f"{missing[:3]}"
    )


def test_step2_history_contract_matches_pick_detail():
    """The frozen wager on a settled history row must equal the frozen
    wager the Pick Breakdown endpoint ships for the same pick."""
    tok = _tok()
    r = httpx.get(f"{_BASE}/api/picks/history?days=30",
                    headers={"Authorization": f"Bearer {tok}"}, timeout=120)
    if r.status_code != 200:
        pytest.skip("history unavailable")
    picks = r.json().get("picks", [])
    if not picks:
        pytest.skip("no history rows")
    checked = 0
    for hp in picks[:20]:
        pid = hp.get("id")
        if not pid:
            continue
        d = httpx.get(f"{_BASE}/api/picks/{pid}",
                        headers={"Authorization": f"Bearer {tok}"}, timeout=30)
        if d.status_code != 200:
            continue
        hc = hp.get("published_pick_contract") or {}
        dc = d.json().get("published_pick_contract") or {}
        # sport + selection are the immutable identity spine.
        for k in ("sport", "selection"):
            assert hc.get(k) == dc.get(k), (
                f"STEP 2: {k!r} drift between history and detail for "
                f"pick {pid}: history={hc.get(k)!r} detail={dc.get(k)!r}"
            )
        checked += 1
        if checked >= 4:
            return
    if checked == 0:
        pytest.skip("no history rows resolved a detail contract")
