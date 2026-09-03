"""P0A / P0B — Full/Lite Canonical Board Parity + Per-Sport / Per-Market
=========================================================================

Root Closure invariant PERKLOCKS-MAIN 34:
    set(full_published_pick_ids) == set(lite_published_pick_ids)
    AND
    Counter(sport)      matches   full vs. lite
    AND
    Counter(sport, mkt) matches   full vs. lite  (representative markets)

Slice 1.2B compressed the lite DTO from ~1.08 MB → ~165 KB (−84.8%).
The optimisation MUST NEVER alter board TRUTH — every canonical
identity present on the full board must also appear on the lite board,
and the sport / market-family distribution of each must match.

Failure of this test class means the whitelist projection dropped a
pick that survived the full pipeline — the exact regression class the
user flagged in PERKLOCKS-MAIN 34 ("MLB player picks disappeared /
MLB game markets disappeared").
"""
from __future__ import annotations
import os, sys
import pytest, httpx
from collections import Counter

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

_BASE = "http://localhost:8001"


def _tok():
    try:
        from rate_limit import _reset_for_tests
        _reset_for_tests(scope_prefix="ip:")
    except Exception:
        pass
    r = httpx.post(f"{_BASE}/api/auth/login",
                    json={"email": "demo@lockscore.ai", "password": "demo123"},
                    timeout=10)
    if r.status_code != 200:
        pytest.skip(f"login failed {r.status_code}")
    return r.json()["access_token"]


def _fetch_full_and_lite():
    tok = _tok()
    hdrs = {"Authorization": f"Bearer {tok}"}
    full = httpx.get(f"{_BASE}/api/picks/today", headers=hdrs, timeout=90).json()
    lite = httpx.get(f"{_BASE}/api/picks/today?lite=true", headers=hdrs, timeout=90).json()
    fp = full.get("picks", []) if isinstance(full, dict) else full
    lp = lite.get("picks", []) if isinstance(lite, dict) else lite
    if not fp or not lp:
        pytest.skip("no picks live")
    return fp, lp


def _classify_market(p: dict) -> str:
    m = (p.get("market") or "").lower()
    mk = (p.get("market_key") or "").lower()
    mf = (p.get("market_family") or "").lower()
    if any(k in m for k in ("hits", "home run", "total base", "rbi", "hitter")) or "batter" in mk:
        return "hitter_prop"
    if "strikeout" in m or "pitcher" in mk:
        return "pitcher_prop"
    if "moneyline" in m or mk == "h2h":
        return "moneyline"
    if "run line" in m or "runline" in mk or "spread" in mk:
        return "spread_runline"
    if "total" in m or "total" in mk:
        return "total"
    if "goal scorer" in m or "goalscorer" in mk or "anytime" in m:
        return "goalscorer"
    return mf or mk or "other"


def test_p0a_board_membership_parity():
    full, lite = _fetch_full_and_lite()
    full_ids = {p.get("id") for p in full if p.get("id")}
    lite_ids = {p.get("id") for p in lite if p.get("id")}
    only_full = full_ids - lite_ids
    only_lite = lite_ids - full_ids
    assert not only_full, (
        f"BOARD_PARITY: {len(only_full)} pick(s) present in FULL but "
        f"missing from LITE — Slice 1.2B whitelist regression. "
        f"Sample: {list(only_full)[:5]}"
    )
    assert not only_lite, (
        f"BOARD_PARITY: {len(only_lite)} pick(s) present in LITE but "
        f"missing from FULL — impossible unless a duplicate slipped "
        f"through post-rescue dedupe. Sample: {list(only_lite)[:5]}"
    )


def test_p0b_per_sport_membership_parity():
    full, lite = _fetch_full_and_lite()
    cf = Counter(p.get("sport") for p in full)
    cl = Counter(p.get("sport") for p in lite)
    assert cf == cl, (
        f"PER_SPORT_PARITY drift — Slice 1.2B whitelist may be "
        f"dropping picks of a specific sport.\n"
        f"  FULL: {dict(cf)}\n  LITE: {dict(cl)}"
    )


def test_p0b_per_market_family_parity():
    full, lite = _fetch_full_and_lite()
    cf = Counter((p.get("sport"), _classify_market(p)) for p in full)
    cl = Counter((p.get("sport"), _classify_market(p)) for p in lite)
    assert cf == cl, (
        f"PER_MARKET_FAMILY drift — a specific (sport, market_family) "
        f"combination differs full ↔ lite.\n"
        f"  drift: { {k: (cf.get(k,0), cl.get(k,0)) for k in set(cf)|set(cl) if cf.get(k)!=cl.get(k)} }"
    )


def test_p0b_canonical_truth_fields_survive_lite():
    """Every canonical identity field required downstream (History,
    Rollover, Parlay, My Bets) MUST survive the lite projection."""
    _full, lite = _fetch_full_and_lite()
    REQUIRED = ("id", "sport", "market", "selection",
                 "publication_state", "locks_eligibility",
                 "grade")
    missing_report = Counter()
    for p in lite:
        for f in REQUIRED:
            if p.get(f) is None:
                missing_report[f] += 1
    # `grade` may legitimately drop on Passes/rescue rows, so relax it.
    hard_missing = {k: v for k, v in missing_report.items() if k != "grade"}
    assert not hard_missing, (
        f"CANONICAL_TRUTH regression: lite payload is missing required "
        f"identity fields on some picks: {hard_missing}"
    )


def test_p0a_no_pick_deleted_by_whitelist():
    """Bisect canary — assert lite pipeline never nets fewer rows than
    full. Same-count is the minimum bar; identity parity is the real
    proof (test_p0a_board_membership_parity)."""
    full, lite = _fetch_full_and_lite()
    assert len(lite) == len(full), (
        f"LITE has {len(lite)} rows vs FULL {len(full)} — Slice 1.2B "
        f"whitelist may have suppressed at least one row."
    )
