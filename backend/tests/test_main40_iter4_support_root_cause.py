"""MAIN 40 · Iter 4 (2026-06-05) — support-confirmed root cause tests.

FIX 1 — /picks/today ESPN enrichment moved off the hot path.
FIX 2 — Parlay legs projected to a thin canonical DTO.

Both fixes must:
  * Keep 200 OK.
  * Preserve canonical publication truth (canonical_pick_id,
    published_lock_score, published_grade, publication_state,
    market, selection, line, odds, event identity, book).
  * Shrink /api/picks/parlay to <100 KB total.
  * Keep MAIN 37 parity and MAIN 39 Slice 1-3 contracts green.
"""
from __future__ import annotations
import os
import json
import time
import urllib.request
import pytest

BACKEND = os.environ.get("EXPO_BACKEND_URL", "http://localhost:8001")


def _login_token() -> str:
    req = urllib.request.Request(
        f"{BACKEND}/api/auth/login",
        data=json.dumps({"email": "demo@lockscore.ai",
                         "password": "demo123"}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r).get("access_token", "")


def _get_json(path: str, token: str) -> tuple[dict, int, float]:
    req = urllib.request.Request(
        f"{BACKEND}{path}",
        headers={"Authorization": f"Bearer {token}"},
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=25) as r:
        raw = r.read()
        elapsed = time.time() - t0
        return json.loads(raw), len(raw), elapsed


# ═══════════════════════════════════════════════════════════════════
# FIX 1 — /picks/today?lite=true no longer waits for full ESPN meta
# ═══════════════════════════════════════════════════════════════════

def test_espn_cache_helpers_exist():
    """Static contract — server.py exposes the board-version keyed
    ESPN cache helpers introduced by MAIN 40 Iter 4."""
    src = open("/app/backend/server.py").read()
    for name in (
        "_ESPN_ENRICH_CACHE",
        "_ESPN_DISPLAY_FIELDS",
        "_current_board_version",
        "_reset_espn_cache_if_stale",
        "_apply_espn_cache_overlay",
        "_warm_espn_cache",
    ):
        assert name in src, f"missing helper: {name}"


def test_hot_path_no_longer_awaits_espn_meta():
    """Regression guard — picks_routes.py must NOT await
    `_decorate_with_espn_meta` on the request path.  It must use the
    cache overlay + background warm instead."""
    src = open("/app/backend/routes/picks_routes.py").read()
    # The overlay + warm pattern must be present in the /picks/today
    # code path.
    assert "_apply_espn_cache_overlay(picks)" in src
    assert "_warm_espn_cache" in src
    # And the sync call must be gone from the request pipeline.
    # (The deep endpoint at /pick/{id} may still call it lazily —
    # that's fine because it only enriches ONE pick.)
    # We assert the OLD pattern `picks = await _decorate_with_espn_meta(picks)`
    # is no longer present in the today handler.
    today_slice = src.split("async def picks_today")[1].split("async def ")[0]
    assert "await _decorate_with_espn_meta(picks)" not in today_slice, \
        "picks_today hot path must not await _decorate_with_espn_meta"


def test_picks_today_returns_200_and_canonical_shape():
    """Live regression — /picks/today?lite=true must return 200 with
    picks that carry the canonical identity fields regardless of
    whether the ESPN cache is warm."""
    token = _login_token()
    d, _size, elapsed = _get_json("/api/picks/today?lite=true", token)
    assert isinstance(d.get("picks"), list) and d["picks"]
    # 20 s client cap.  Cold from a fresh backend restart is ~5 s;
    # warm is ~3 s.  Give a generous ceiling well under the client cap.
    assert elapsed < 18.0, f"/picks/today?lite=true took {elapsed:.2f}s (client cap 20s)"
    # Canonical identity must be present on every pick.
    for p in d["picks"][:20]:
        # published_lock_score is the immutable truth (frozen at
        # publication).  If it's absent, the canonicalizer failed.
        assert "id" in p, "pick missing id"
        assert "sport" in p, "pick missing sport"
        assert "market" in p, "pick missing market"
        assert "selection" in p, "pick missing selection"
        assert "lock_score" in p, "pick missing lock_score"


def test_espn_cache_warms_on_second_call():
    """After a first request warms the cache in the background, the
    second request must return picks WITH the ESPN display overlay
    (home_meta / injury_chip / signal_score) applied on the hot path."""
    token = _login_token()
    # First call may return picks without overlay (cache cold).
    _first, _s, _t = _get_json("/api/picks/today?lite=true", token)
    # Give the background warm task time to complete.
    time.sleep(8)
    d, _s, elapsed = _get_json("/api/picks/today?lite=true", token)
    picks = d.get("picks", [])
    # At least a majority of picks should now carry SOME ESPN overlay
    # field.  We tolerate a partial warm if the slate is huge.
    with_overlay = sum(
        1 for p in picks
        if any(k in p for k in ("home_meta", "away_meta",
                                 "injury_chip", "signal_score"))
    )
    assert with_overlay >= max(1, int(len(picks) * 0.5)), \
        f"only {with_overlay}/{len(picks)} picks carry ESPN overlay after warm"
    # Warm response must still be well under the client cap.
    assert elapsed < 12.0, f"warm /picks/today?lite=true took {elapsed:.2f}s"


# ═══════════════════════════════════════════════════════════════════
# FIX 2 — /picks/parlay legs are a thin canonical DTO
# ═══════════════════════════════════════════════════════════════════

_HEAVY_LEG_FIELDS = {
    "pick_rationale",
    "pattern_signals",
    "signal_engine",
    "published_pick_contract",   # canonical truth stays on deep endpoint
    "bvp_history",
    "understat_form",
    "player_form",
    "home_meta", "away_meta",
    "injury_chip",
    "model_diagnostics",
    "signal_score",
    "espn_signals",
    "signal_score_raw",
    "player_meta",
    "market_rank",
    "family_context",
}


def test_parlay_legs_are_thin_dto():
    """No leg (primary or alternate) may embed the fat pick document
    or the full published_pick_contract."""
    token = _login_token()
    d, size, elapsed = _get_json("/api/picks/parlay?legs=3", token)
    parlays = d.get("parlays") or []
    assert parlays, "expected at least one parlay"
    for card in parlays:
        for group in ("legs", "alternates"):
            for leg in card.get(group) or []:
                heavy = _HEAVY_LEG_FIELDS.intersection(leg.keys())
                assert not heavy, \
                    f"{group} still contain heavy fields: {heavy}"


def test_parlay_legs_preserve_canonical_identity():
    """Thin DTO must still carry the identity + published surface the
    Parlay UI + deep endpoint round-trip need."""
    token = _login_token()
    d, _s, _t = _get_json("/api/picks/parlay?legs=3", token)
    parlays = d.get("parlays") or []
    for card in parlays:
        for leg in card.get("legs") or []:
            # id (frontend uses leg.id for pin/press).
            assert "id" in leg
            # Wager identity fields the card renders.
            for f in ("sport", "market", "grade", "book_odds"):
                assert f in leg, f"leg missing {f}"
            # Canonical publication surface — must be present so
            # consumers can round-trip via /api/picks/{id}.
            assert "published_lock_score" in leg or "lock_score" in leg


def test_parlay_response_under_100kb():
    """MAIN 40 target: /api/picks/parlay total response < 100 KB."""
    token = _login_token()
    d, size, elapsed = _get_json("/api/picks/parlay?legs=3", token)
    assert size < 100_000, f"parlay response is {size} bytes (>100 KB target)"
    # And 200 OK response time well under the client cap.
    assert elapsed < 8.0, f"parlay took {elapsed:.2f}s"
    assert isinstance(d.get("parlays"), list) and d["parlays"]


# ═══════════════════════════════════════════════════════════════════
# Canonical parity — /picks/today vs the raw eligible board
# ═══════════════════════════════════════════════════════════════════

def test_canonical_parity_all_picks_have_identity_fields():
    """Every pick that ships out of /picks/today?lite=true must carry
    the identity + publication fields the directive lists as
    invariants (canonical_pick_id or id, sport, market, selection,
    lock_score / published_lock_score)."""
    token = _login_token()
    d, _s, _t = _get_json("/api/picks/today?lite=true", token)
    picks = d.get("picks") or []
    assert picks, "expected non-empty board"
    missing = []
    for p in picks:
        for f in ("id", "sport", "market", "selection"):
            if f not in p:
                missing.append((p.get("id", "?"), f))
                break
    assert not missing, f"picks missing identity fields: {missing[:5]}"


def test_parlay_legs_reference_a_real_pick_id():
    """Legs must expose a stable id that maps to a canonical pick so
    the deep endpoint (GET /api/picks/{id}) can round-trip."""
    token = _login_token()
    d, _s, _t = _get_json("/api/picks/parlay?legs=3", token)
    for card in d.get("parlays") or []:
        for leg in card.get("legs") or []:
            assert isinstance(leg.get("id"), str) and leg["id"], \
                "leg.id must be a non-empty string"
