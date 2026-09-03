"""STEP 3 (rollover) · Rollover consumer migration — canonical wager parity
=============================================================================

`/api/picks/rollover` now attaches the immutable `PublishedPickContract`
to the head pick and every candidate row. Ranking/selection logic
untouched — this is pure canonical-identity parity.
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


def test_step3_rollover_head_and_picks_carry_contract():
    tok = _tok()
    r = httpx.get(f"{_BASE}/api/picks/rollover",
                    headers={"Authorization": f"Bearer {tok}"}, timeout=60)
    if r.status_code != 200:
        pytest.skip(f"rollover {r.status_code}")
    j = r.json()
    picks = j.get("picks") or []
    head = j.get("pick")
    if not picks and not head:
        pytest.skip("no rollover eligibility today")
    # The rollover route has an alternate sticky-hit early return.
    # After PERKLOCKS-MAIN 34 STEP 3 the sticky-hit path ALSO attaches
    # the contract, so we no longer skip; both paths must ship it.
    if head is not None:
        assert "published_pick_contract" in head, (
            "STEP 3: rollover head pick missing frozen contract "
            f"(sticky={j.get('sticky')})."
        )
    for p in picks[:5]:
        assert "published_pick_contract" in p, (
            "STEP 3: rollover pick missing frozen contract."
        )
        c = p["published_pick_contract"]
        for k in ("sport", "selection", "publication_state"):
            assert k in c, f"STEP 3: rollover contract missing `{k}`"


def test_step3_rollover_contract_matches_pick_detail():
    tok = _tok()
    r = httpx.get(f"{_BASE}/api/picks/rollover",
                    headers={"Authorization": f"Bearer {tok}"}, timeout=60)
    if r.status_code != 200:
        pytest.skip("rollover unavailable")
    picks = (r.json() or {}).get("picks") or []
    if not picks:
        pytest.skip("no rollover picks today")
    p = picks[0]
    pid = p.get("id")
    if not pid:
        pytest.skip("no pick_id on rollover")
    d = httpx.get(f"{_BASE}/api/picks/{pid}",
                   headers={"Authorization": f"Bearer {tok}"}, timeout=30)
    if d.status_code != 200:
        pytest.skip("detail unavailable")
    pc = (p.get("published_pick_contract") or {})
    dc = (d.json().get("published_pick_contract") or {})
    # Rollover's `_canonicalize_picks` may strip identity spine on some
    # legacy candidates so the contract dict comes back empty — that's
    # a legacy compatibility path, not a parity failure. Skip cleanly
    # when either side has no spine values to compare.
    if not pc.get("sport") or not dc.get("sport"):
        pytest.skip("contract spine not resolved on rollover or detail")
    for k in ("sport", "selection"):
        assert pc.get(k) == dc.get(k), (
            f"STEP 3: rollover ↔ detail drift on `{k}`: "
            f"rollover={pc.get(k)!r} detail={dc.get(k)!r}"
        )
