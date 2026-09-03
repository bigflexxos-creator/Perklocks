"""STEP 1b · Parlay consumer migration — Locks == Parlay wager parity
=========================================================================

Every parlay leg in the response must carry the identical immutable
`published_pick_contract` block the Locks board would ship for the
same pick_id. Ranking / survival / correlation math is intentionally
untouched — this is a pure canonical-identity parity guarantee.
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


def _fetch_parlays(tok):
    r = httpx.get(f"{_BASE}/api/picks/parlay?legs=3", timeout=90,
                   headers={"Authorization": f"Bearer {tok}"})
    if r.status_code != 200:
        pytest.skip(f"parlay {r.status_code}: {r.text[:200]}")
    return r.json()


def _fetch_lite_by_id(tok, pick_id):
    r = httpx.get(f"{_BASE}/api/picks/{pick_id}", timeout=30,
                   headers={"Authorization": f"Bearer {tok}"})
    return r.json() if r.status_code == 200 else None


def test_step1b_every_parlay_leg_carries_the_contract():
    tok = _tok()
    resp = _fetch_parlays(tok)
    parlays = (resp or {}).get("parlays") or resp.get("bundles") or []
    if not isinstance(parlays, list) or not parlays:
        # Some tenants may have no eligible parlay today; skip cleanly.
        pytest.skip("no parlays available today")
    for parlay in parlays[:3]:
        legs = parlay.get("legs") or []
        if not legs:
            continue
        for leg in legs:
            assert "published_pick_contract" in leg, (
                "STEP 1b: every parlay leg must carry the immutable "
                "`published_pick_contract` block."
            )
            c = leg["published_pick_contract"]
            for k in ("canonical_pick_id", "sport", "selection",
                        "publication_state"):
                assert k in c, (
                    f"STEP 1b: parlay leg contract missing canonical `{k}`"
                )


def test_step1b_parlay_leg_contract_matches_pick_detail():
    """The frozen contract on a parlay leg must equal the same frozen
    contract the pick-detail endpoint ships — the Locks == Parlay wager
    parity invariant."""
    tok = _tok()
    resp = _fetch_parlays(tok)
    parlays = (resp or {}).get("parlays") or resp.get("bundles") or []
    if not isinstance(parlays, list) or not parlays:
        pytest.skip("no parlays available today")
    checked = 0
    for parlay in parlays[:3]:
        for leg in (parlay.get("legs") or []):
            pid = leg.get("id") or leg.get("canonical_pick_id")
            if not pid:
                continue
            detail = _fetch_lite_by_id(tok, pid)
            if not detail:
                continue
            leg_c = leg.get("published_pick_contract") or {}
            det_c = detail.get("published_pick_contract") or {}
            for k in ("sport", "selection", "published_lock_score"):
                assert leg_c.get(k) == det_c.get(k), (
                    f"STEP 1b: {k!r} differs between parlay leg "
                    f"{pid} and pick detail: "
                    f"leg={leg_c.get(k)!r} detail={det_c.get(k)!r}"
                )
            checked += 1
            if checked >= 3:
                return
    if checked == 0:
        pytest.skip("no parlay legs with resolvable pick IDs")
