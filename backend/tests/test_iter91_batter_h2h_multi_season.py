"""iter-91 backend verification.

Three requests from the user:
  1. Multi-season gameLog — fetch_batter_h2h now merges 2023-2026 gameLogs
     so per-game rows span multiple seasons (not just current year).
  2. Deep-dive expand toggle — /api/picks/{id}/h2h returns recent list up
     to length 50 (bumped from 5).
  3. H2H signal wired into `why_this_pick` — when a batter has ≥30bp career
     vs-opp gap and ≥15 AB, the enricher emits an "H2H tailwind"/"headwind"
     bullet that appears in the pick's why_this_pick array.

Perf regression: cold < 10s, warm < 6s.
Iter-89/90 regressions: chip format stable; Ohtani vs Mets still ~.282.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
import sys

import pytest
import requests

sys.path.insert(0, "/app/backend")

BASE_URL = (
    os.environ.get("EXPO_PUBLIC_BACKEND_URL")
    or os.environ.get("EXPO_BACKEND_URL")
    or "https://canonical-parity.preview.emergentagent.com"
).rstrip("/")

CHIP_RE = re.compile(r"^\d+-for-\d+ vs .+ \(0\.\d{3} avg, \d+%\)$")
BAD_TAIL_RE = re.compile(r"H2H \d+-\d+|\bL\d+")
STAT_RE = re.compile(r"^\d+-\d+$")

DEMO_EMAIL = "demo@lockscore.ai"
DEMO_PASSWORD = "demo123"

_SESSION: requests.Session | None = None


def _session() -> requests.Session:
    global _SESSION
    if _SESSION is not None:
        return _SESSION
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD}, timeout=60)
    assert r.status_code == 200, f"login {r.status_code}: {r.text[:200]}"
    tok = r.json().get("token") or r.json().get("access_token")
    assert tok, "no token in login response"
    s.headers.update({"Authorization": f"Bearer {tok}",
                      "Content-Type": "application/json"})
    _SESSION = s
    return s


# ── 1) Direct function call: multi-season game log ────────────────────
def test_fetch_batter_h2h_multiseason_ohtani_mets():
    from mlb_batter_h2h import fetch_batter_h2h
    data = asyncio.get_event_loop().run_until_complete(
        fetch_batter_h2h("Shohei Ohtani", "New York Mets")
    )
    assert data.get("ok") is True, f"fetch_batter_h2h failed: {data}"
    recent = data.get("vs_team_recent") or []
    print(f"Ohtani vs Mets recent rows: {len(recent)}")
    # Must have >= 15 rows now (was 2 in iter-90)
    assert len(recent) >= 15, f"expected >=15 rows, got {len(recent)}"
    # Each row's stat matches /^\d+-\d+$/
    for r in recent:
        assert STAT_RE.match(str(r.get("stat", ""))), f"bad stat field: {r}"
    # Rows span at least 2 distinct years
    years = {str(r.get("date", ""))[:4] for r in recent if r.get("date")}
    print(f"Ohtani vs Mets years covered: {sorted(years)}")
    assert len(years) >= 2, f"expected >=2 distinct years, got {years}"
    # Iter-90 regression: career totals still ~.282
    assert data.get("vs_team_hits") == 20
    assert data.get("vs_team_ab") == 71
    assert abs(data.get("vs_team_avg", 0) - 0.282) < 0.005


# ── 2) API: /api/picks/today?sport=MLB perf ───────────────────────────
_CACHE = {"picks": None, "cold_ms": None, "warm_ms": None}


def _fetch_picks():
    """Fetch MLB picks with cold+warm timings, memoised."""
    if _CACHE["picks"] is not None:
        return _CACHE["picks"]
    s = _session()
    t0 = time.time()
    r = s.get(f"{BASE_URL}/api/picks/today", params={"sport": "MLB"}, timeout=30)
    cold_ms = (time.time() - t0) * 1000
    assert r.status_code == 200, r.text[:400]
    body = r.json()
    picks = body.get("picks") if isinstance(body, dict) else body
    # Warm
    t0 = time.time()
    r2 = s.get(f"{BASE_URL}/api/picks/today", params={"sport": "MLB"}, timeout=30)
    warm_ms = (time.time() - t0) * 1000
    assert r2.status_code == 200
    _CACHE["picks"] = picks
    _CACHE["cold_ms"] = cold_ms
    _CACHE["warm_ms"] = warm_ms
    print(f"cold={cold_ms:.0f}ms warm={warm_ms:.0f}ms picks={len(picks)}")
    return picks


def test_perf_cold_under_10s_warm_under_6s():
    _fetch_picks()
    assert _CACHE["cold_ms"] < 10_000, f"cold {_CACHE['cold_ms']:.0f}ms >= 10s"
    assert _CACHE["warm_ms"] < 6_000, f"warm {_CACHE['warm_ms']:.0f}ms >= 6s"


# ── 3) Deep-dive /api/picks/{id}/h2h has long recent list for a batter Hits pick
def _batter_hit_picks():
    picks = _fetch_picks()
    out = []
    for p in picks:
        market = (p.get("market") or "").lower()
        if "hits" in market and "hits allowed" not in market:
            out.append(p)
    return out


def test_deepdive_recent_long_across_multiple_years():
    hits_picks = _batter_hit_picks()
    if not hits_picks:
        pytest.skip("no batter Hits picks on today's slate")
    ok_count = 0
    multi_year = 0
    for p in hits_picks[:15]:
        pid = p.get("id") or p.get("pick_id") or p.get("_id")
        if not pid:
            continue
        r = requests.get(f"{BASE_URL}/api/picks/{pid}/h2h", timeout=20, headers=_session().headers)
        if r.status_code != 200:
            continue
        b = r.json()
        ph = b.get("player_h2h") or {}
        recent = ph.get("recent") or []
        if len(recent) >= 10:
            ok_count += 1
        years = {str(rr.get("date", ""))[:4] for rr in recent if rr.get("date")}
        if len(years) >= 2:
            multi_year += 1
    print(f"batters with >=10 recent rows: {ok_count}/{len(hits_picks[:15])}")
    print(f"batters with rows across >=2 years: {multi_year}/{len(hits_picks[:15])}")
    assert ok_count >= 1, "expected >=1 batter Hits pick with recent list length >= 10"
    assert multi_year >= 1, "expected >=1 batter with rows spanning >=2 years"


# ── 4) Wired H2H signal into why_this_pick ────────────────────────────
def test_h2h_signal_wired_into_why_this_pick():
    picks = _fetch_picks()
    hit_bullets = []
    ohtani_ok = False
    for p in picks:
        why = p.get("why_this_pick") or p.get("reasoning") or []
        if isinstance(why, str):
            why = [why]
        for b in why:
            if isinstance(b, str) and ("H2H tailwind" in b or "H2H headwind" in b):
                hit_bullets.append((p.get("selection") or p.get("market"), b))
        # regression: no chip contains 'H2H X-Y' or ' L\d+' tail
        chip = p.get("h2h_summary") or ""
        if isinstance(chip, str) and chip:
            # only fail on player-prop chips (batter/pitcher/etc)
            market = (p.get("market") or "").lower()
            if "hits" in market and "hits allowed" not in market:
                assert not BAD_TAIL_RE.search(chip), f"bad chip tail: {chip}"
        # regression: Ohtani vs Mets chip format
        if (p.get("selection") or "").strip().lower().startswith("shohei ohtani") and "hits" in (p.get("market") or "").lower():
            if chip:
                assert CHIP_RE.match(chip), f"Ohtani chip bad: {chip}"
                ohtani_ok = True
    print(f"picks with H2H tailwind/headwind bullet: {len(hit_bullets)}")
    for s, b in hit_bullets[:5]:
        print(f"  {s}: {b}")
    # If Ohtani vs Mets appears we validate his chip
    if ohtani_ok:
        print("Ohtani vs Mets chip format regression PASS")
    # Wired signal — at least 1 pick with tailwind/headwind bullet when
    # slate has enough batter samples.
    # We soft-assert because slate may not always have a ≥30bp gap batter.
    # If no candidates, skip.
    hits = _batter_hit_picks()
    if len(hits) < 5:
        pytest.skip("slate too thin to expect a ≥30bp H2H gap batter")
    assert len(hit_bullets) >= 1, (
        "expected at least one pick's why_this_pick to include "
        "'H2H tailwind' or 'H2H headwind' bullet — H2H signal not wired through."
    )
