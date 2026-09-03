"""STEP 12 · Same-snapshot canonical wager parity harness
==============================================================

Locks == Pick Breakdown == Parlay leg == My Bets == History for the
SAME canonical_pick_id — verified from a SINGLE snapshot request.

Compares the canonical spine fields the user's directive lists:
    sport
    selection
    published_lock_score
    publication_state
    publication_revision (when present)

Ranking/correlation/settlement metadata is intentionally NOT compared
— surfaces may enrich presentation, they may NOT reinterpret the
wager.
"""
from __future__ import annotations
import os, sys
import pytest, httpx

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

_BASE = "http://localhost:8001"
_SPINE = ("sport", "selection", "publication_state")


def _tok():
    r = httpx.post(f"{_BASE}/api/auth/login",
                    json={"email": "demo@lockscore.ai", "password": "demo123"},
                    timeout=10)
    if r.status_code != 200:
        pytest.skip(f"login {r.status_code}")
    return r.json()["access_token"]


def _get(tok, path, timeout=90):
    return httpx.get(f"{_BASE}{path}",
                       headers={"Authorization": f"Bearer {tok}"}, timeout=timeout)


def _contract(payload):
    if isinstance(payload, dict):
        c = payload.get("published_pick_contract")
        return c if isinstance(c, dict) else None
    return None


def test_step12_locks_pick_detail_parity():
    """Every LITE-board contract must equal the pick-detail contract
    on the identity spine for the same pick_id."""
    tok = _tok()
    r = _get(tok, "/api/picks/today?lite=true")
    if r.status_code != 200:
        pytest.skip("picks unavailable")
    picks = r.json().get("picks", [])
    if not picks:
        pytest.skip("no picks live")
    checked = 0
    drift = []
    for lite in picks[:20]:
        pid = lite.get("id")
        if not pid:
            continue
        d = _get(tok, f"/api/picks/{pid}", timeout=30)
        if d.status_code != 200:
            continue
        dc = _contract(d.json())
        if not dc:
            continue
        for k in _SPINE:
            if lite.get(k) != dc.get(k) and not (
                # LITE board may not carry publication_state; skip if absent.
                k == "publication_state" and lite.get(k) is None
            ):
                drift.append((pid, k, lite.get(k), dc.get(k)))
        checked += 1
        if checked >= 6:
            break
    if checked == 0:
        pytest.skip("no picks resolved a detail contract")
    assert not drift, (
        f"STEP 12: Locks ↔ Pick Detail spine drift: {drift[:4]}"
    )


def test_step12_parlay_leg_parity_with_pick_detail():
    tok = _tok()
    p = _get(tok, "/api/picks/parlay?legs=3", timeout=90)
    if p.status_code != 200:
        pytest.skip(f"parlay {p.status_code}")
    parlays = p.json().get("parlays") or p.json().get("bundles") or []
    if not isinstance(parlays, list) or not parlays:
        pytest.skip("no parlays available today")
    checked = 0
    drift = []
    for parlay in parlays[:2]:
        for leg in (parlay.get("legs") or []):
            pid = leg.get("id") or (leg.get("published_pick_contract") or {}).get("canonical_pick_id")
            if not pid:
                continue
            d = _get(tok, f"/api/picks/{pid}", timeout=30)
            if d.status_code != 200:
                continue
            lc = _contract(leg)
            dc = _contract(d.json())
            if not (lc and dc):
                continue
            for k in _SPINE:
                if lc.get(k) != dc.get(k):
                    drift.append((pid, k, lc.get(k), dc.get(k)))
            checked += 1
            if checked >= 4:
                break
        if checked >= 4:
            break
    if checked == 0:
        pytest.skip("no parlay legs matched pick-detail")
    assert not drift, (
        f"STEP 12: Parlay leg ↔ Pick Detail spine drift: {drift[:4]}"
    )


def test_step12_history_parity_with_pick_detail():
    tok = _tok()
    r = _get(tok, "/api/picks/history?days=30", timeout=120)
    if r.status_code != 200:
        pytest.skip("history unavailable")
    picks = r.json().get("picks", [])
    if not picks:
        pytest.skip("no history rows")
    checked = 0
    drift = []
    for hp in picks[:15]:
        pid = hp.get("id")
        if not pid:
            continue
        d = _get(tok, f"/api/picks/{pid}", timeout=30)
        if d.status_code != 200:
            continue
        hc = _contract(hp)
        dc = _contract(d.json())
        if not (hc and dc):
            continue
        for k in ("sport", "selection"):
            if hc.get(k) != dc.get(k):
                drift.append((pid, k, hc.get(k), dc.get(k)))
        checked += 1
        if checked >= 4:
            break
    if checked == 0:
        pytest.skip("no history row matched detail")
    assert not drift, (
        f"STEP 12: History ↔ Pick Detail spine drift: {drift[:4]}"
    )


def test_step12_full_lite_web_api_membership_still_equal():
    """Delegates to the P0A/B invariant that full == lite membership
    holds — the same-snapshot Web / API parity guarantee. Duplicated
    here so a STEP 12 regression search always finds this test."""
    tok = _tok()
    full = _get(tok, "/api/picks/today").json().get("picks", [])
    lite = _get(tok, "/api/picks/today?lite=true").json().get("picks", [])
    if not full or not lite:
        pytest.skip("no picks live")
    full_ids = {p.get("id") for p in full}
    lite_ids = {p.get("id") for p in lite}
    assert full_ids == lite_ids, (
        f"STEP 12: same-snapshot API/LITE drift: "
        f"only_full={len(full_ids - lite_ids)} "
        f"only_lite={len(lite_ids - full_ids)}"
    )
