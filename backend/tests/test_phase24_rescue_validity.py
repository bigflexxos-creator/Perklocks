"""Root Closure Part-2 — Rescue validity + universal invariants
================================================================

Enforces the follow-up contract:
    * INVALID_BUT_RESCUED == 0
    * CONTRADICTORY_CURRENT_WAGERS == 0 (server-side 1X2)
    * DUPLICATE_CURRENT_WAGERS == 0
    * POST_START_CURRENT_LOCKS == 0
    * SYNTHETIC_ACTIONABLE_LOCKS == 0
    * Every served pick has real book_odds + market + sport
"""
from __future__ import annotations
import asyncio, os, sys, re
from datetime import datetime, timezone
import pytest, httpx
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)
load_dotenv(os.path.join(_BACKEND, ".env"))

_BASE = "http://localhost:8001"


def _tok():
    r = httpx.post(f"{_BASE}/api/auth/login",
                    json={"email": "demo@lockscore.ai", "password": "demo123"}, timeout=10)
    if r.status_code != 200:
        pytest.skip(f"login failed: {r.status_code}")
    return r.json()["access_token"]


def _picks(sport="all"):
    tok = _tok()
    j = httpx.get(f"{_BASE}/api/picks/today", params={"sport": sport},
                   headers={"Authorization": f"Bearer {tok}"}, timeout=45).json()
    return j.get("picks", []) if isinstance(j, dict) else j


# ── §1 INVALID_BUT_RESCUED == 0 ──────────────────────────────────────
def test_rescue_never_resurrects_invalid_predictions():
    """No served pick may carry an invalid-flag combination (synthetic,
    contradicted, no book_odds, no market, duplicate revision)."""
    from services.locks_eligibility import rescue_validity_reason
    picks = _picks("all")
    if not picks: pytest.skip("no picks")
    invalid = [p for p in picks if rescue_validity_reason(p) is not None]
    assert not invalid, (
        f"{len(invalid)} INVALID_BUT_RESCUED. sample: "
        f"{[(p.get('id'), rescue_validity_reason(p)) for p in invalid[:5]]}"
    )


# ── §2 Every eligible=True pick has canonical safety conditions ─────
def test_every_eligible_pick_carries_real_book_offering():
    picks = _picks("all")
    if not picks: pytest.skip("no picks")
    bad = [p for p in picks
            if (p.get("locks_eligibility") or {}).get("eligible") is True
            and (p.get("book_odds") in (None, "")
                  or p.get("model_only") is True
                  or p.get("no_real_book_line") is True)]
    assert not bad, f"{len(bad)} eligible picks lack real book offering"


# ── §6 CONTRADICTORY_CURRENT_WAGERS == 0 (Soccer 1X2) ───────────────
def test_no_all_three_way_1x2_on_live_board():
    picks = _picks("Soccer")
    if not picks: pytest.skip("no soccer picks")
    # Group ML picks (line is None) by event, count distinct selections
    by_ev: dict = {}
    for p in picks:
        if p.get("line") is not None: continue
        sel = (p.get("selection") or "").strip()
        home = (p.get("home_team_name") or "").strip()
        away = (p.get("away_team_name") or "").strip()
        if not (sel == "Draw" or sel == home or sel == away): continue
        ev = p.get("event") or "?"
        by_ev.setdefault(ev, set()).add(sel)
    offenders = {ev: sides for ev, sides in by_ev.items()
                  if len(sides) >= 3 and "Draw" in sides}
    assert not offenders, f"{len(offenders)} 1X2 all-three-sides on live board"


# ── §6 DUPLICATE_CURRENT_WAGERS == 0 (canonical identity dedupe) ────
def test_no_duplicate_canonical_wagers_on_live_board():
    from services.pick_identity_enricher import canonical_wager_identity
    picks = _picks("all")
    if not picks: pytest.skip("no picks")
    seen: dict = {}
    for p in picks:
        k = canonical_wager_identity(p)
        seen.setdefault(k, []).append(p.get("id"))
    dups = {k: ids for k, ids in seen.items() if len(ids) > 1}
    assert not dups, f"{len(dups)} duplicate canonical wagers: {list(dups.items())[:3]}"


# ── POST_START_CURRENT_LOCKS == 0 ───────────────────────────────────
def test_no_post_start_events_on_live_board():
    picks = _picks("all")
    if not picks: pytest.skip("no picks")
    now = datetime.now(timezone.utc)
    post = []
    for p in picks:
        et = p.get("event_time")
        if not et: continue
        try:
            dt = datetime.fromisoformat(str(et).replace("Z", "+00:00"))
            if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if dt < now:
            post.append(p.get("id"))
    assert not post, f"{len(post)} post-start picks on live board: {post[:5]}"


# ── §5 ALL == sum(sport_i) served counts ────────────────────────────
def test_all_count_equals_sum_of_sport_counts():
    def _n(sp): return len(_picks(sp))
    all_n = _n("all")
    by = {sp: _n(sp) for sp in ("MLB","NFL","CFB","NBA","NHL","Soccer","Tennis","UFC")}
    total = sum(by.values())
    if all_n == 0 and total == 0: pytest.skip("no picks live")
    assert all_n == total, f"ALL={all_n} but sum(sports)={total} · by={by}"
