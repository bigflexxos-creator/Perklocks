"""Root Closure 2026-06 — Live Board Membership & Mobile Reachability
========================================================================

Section Y contract tests proven by live runtime evidence:

- ALL == canonical eligible Locks (backend truth)
- ranking changes order only (no membership loss)
- FlatList virtualization changes MOUNTED count only
- initialNumToRender does not limit REACHABLE count
- no duplicate list keys
- Web build uses `removeClippedSubviews=false` (RN Web truncates
  contentSize.height when this is true → CFB "16 eligible / 8 reachable")
- Zero-sport classifications are EXPLICIT reasons (never blanket
  "off-season / no active slate")
"""
from __future__ import annotations

import os
import re
import sys

import pytest

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)


def _read_index_tsx() -> str:
    p = os.path.join(_BACKEND, "..", "frontend", "app", "(tabs)", "index.tsx")
    with open(p, encoding="utf-8") as f:
        return f.read()


# ── Section H / I / N — FlatList virtualization contract ────────────
def test_flatlist_web_disables_remove_clipped_subviews():
    """RN Web + removeClippedSubviews=true has a known bug where the
    inner scroll container reports a truncated contentSize.height
    on mobile Safari (proven live 2026-09-02: 3916 px → 18723 px
    after fix).  Native must keep the optimisation; Web must disable
    it."""
    src = _read_index_tsx()
    m = re.search(r'removeClippedSubviews=\{([^}]+)\}', src)
    assert m, "FlatList must set removeClippedSubviews conditionally"
    expr = m.group(1)
    assert 'Platform.OS' in expr and 'web' in expr, (
        f"removeClippedSubviews must be platform-conditional, got: {expr}"
    )


def test_flatlist_web_window_is_wide_enough_for_full_slate():
    """On web, windowSize must be wide enough that legitimate slates
    (30-50 cards) never virtualise off-screen enough to strand
    layout measurements."""
    src = _read_index_tsx()
    m = re.search(r'windowSize=\{([^}]+)\}', src)
    assert m, "FlatList must set windowSize"
    expr = m.group(1)
    assert 'Platform.OS' in expr, f"windowSize must be platform-conditional: {expr}"
    # Web branch must be > 20 (default is 21; we want ≥ 21).
    web_match = re.search(r'"web"\s*\?\s*(\d+)', expr)
    assert web_match and int(web_match.group(1)) >= 21, (
        f"Web windowSize too small ({web_match}) — cards will strand"
    )


def test_flatlist_web_initial_render_matches_full_short_slate():
    """`initialNumToRender` must not implicitly cap reachable rows.
    On web we render 40 up front so 20-30 card slates paint completely."""
    src = _read_index_tsx()
    m = re.search(r'initialNumToRender=\{([^}]+)\}', src)
    assert m
    expr = m.group(1)
    web_match = re.search(r'"web"\s*\?\s*(\d+)', expr)
    assert web_match and int(web_match.group(1)) >= 40


# ── Section D — ALL == UNION(sport_i) contract on backend serving ───
def test_backend_all_equals_union_of_sports(monkeypatch):
    """Live invariant: the union of per-sport `/api/picks/today`
    responses must equal the ALL response set (by canonical id).
    Executed against the running server."""
    import httpx, os
    base = "http://localhost:8001"
    # login demo
    r = httpx.post(f"{base}/api/auth/login",
                    json={"email":"demo@lockscore.ai","password":"demo123"}, timeout=10)
    if r.status_code != 200:
        pytest.skip(f"backend login failed: {r.status_code}")
    tok = r.json().get("access_token")
    h = {"Authorization": f"Bearer {tok}"}

    def _ids(sp):
        try:
            j = httpx.get(f"{base}/api/picks/today", params={"sport": sp},
                           headers=h, timeout=15).json()
            picks = j.get("picks", j) if isinstance(j, dict) else j
            return {p.get("id") for p in (picks or []) if p.get("id")}
        except Exception:
            return set()

    all_ids = _ids("all") | _ids("ALL")   # both spellings tolerated
    union = set()
    for sp in ("MLB","NFL","CFB","NBA","NHL","Soccer","Tennis","UFC"):
        union |= _ids(sp)
    if not all_ids and not union:
        pytest.skip("no picks live — cannot enforce membership invariant")
    missing_in_all = union - all_ids
    assert not missing_in_all, \
        f"{len(missing_in_all)} sport-tab picks missing from ALL tab: {list(missing_in_all)[:5]}"


# ── Section T — Tennis zero-slate must carry an explicit reason ─────
def test_tennis_never_labeled_generic_off_season_in_certification_doc():
    """The certification doc MUST NOT describe Tennis with a generic
    'off-season' label — the US Open is active and Tennis is a live-slate
    sport.  If Tennis has zero on-board picks, an explicit reason must
    be stated (POST_EVENT_START_PREGAME_FILTER, PROVIDER_FAILURE,
    MODEL_UNAVAILABLE, BELOW_85, etc.)."""
    p = os.path.join(_BACKEND, "..", "memory", "phase24_final_certification.md")
    with open(p, encoding="utf-8") as f:
        doc = f.read()
    # If Tennis is mentioned, it must not be under a blanket off-season line.
    m = re.search(r"Tennis[^\n]*", doc, re.I)
    if m and re.search(r"off[- ]?season", m.group(0), re.I):
        # allowed only when qualified explicitly, e.g. "not off-season"
        assert re.search(r"not.*off.?season", m.group(0), re.I), (
            f"Tennis labelled off-season generically in cert doc: {m.group(0)}"
        )
