"""
Regression: NFL Star Player Watchlist (visibility-only filter)
==============================================================

Verifies that ``picks_today`` with ``stars_only=true`` narrows the NFL
response to canonical picks whose player identity matches the curated
NFL star roster, WITHOUT mutating any score, WP, evidence, publication
state, or canonical identity fields.

Contract (from HARD GUARDRAILS §3):
  - Never modifies lock_score / win_probability / edge_percent /
    book_odds / grade.
  - Never creates or deletes canonical picks.
  - Never changes settlement anchors (line, threshold).
  - Filter composes cleanly with market filters (STARS + PASS_YDS,
    STARS + REC_YDS, etc.).
"""
import os
import pytest
import requests

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "http://localhost:8001").rstrip("/")
DEMO_EMAIL = "demo@lockscore.ai"
DEMO_PASSWORD = "demo123"


@pytest.fixture(scope="module")
def bearer():
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD},
        timeout=30,
    )
    if r.status_code != 200:
        pytest.skip(f"demo login failed: {r.status_code}")
    return r.json().get("access_token") or r.json().get("token")


@pytest.fixture(scope="module")
def nfl_all(bearer):
    r = requests.get(
        f"{BASE_URL}/api/picks/today?sport=NFL&lite=true",
        headers={"Authorization": f"Bearer {bearer}"},
        timeout=60,
    )
    assert r.status_code == 200
    return r.json().get("picks", [])


@pytest.fixture(scope="module")
def nfl_stars(bearer):
    r = requests.get(
        f"{BASE_URL}/api/picks/today?sport=NFL&stars_only=true&lite=true",
        headers={"Authorization": f"Bearer {bearer}"},
        timeout=60,
    )
    assert r.status_code == 200
    return r.json().get("picks", [])


def test_stars_filter_reduces_slate(nfl_all, nfl_stars):
    """stars_only must produce a strict subset of the full NFL slate."""
    assert len(nfl_stars) < len(nfl_all), (
        f"stars_only should be a subset — got {len(nfl_stars)} vs full "
        f"{len(nfl_all)}"
    )
    # And it should still have SOME picks (the slate has stars).
    assert len(nfl_stars) > 0, "expected at least one star pick"


def test_star_filter_no_non_star_leakage(nfl_stars):
    """No non-star player names should appear in the stars-only response.

    We spot-check a handful of names that are demonstrably NOT on the
    curated NFL star list (backup/depth players who occasionally
    surface in the alt-ladder generator).
    """
    non_star_names = [
        "George Holani", "Woody Marks", "Dontayvion Wicks",
        "Kayshon Boutte", "Malik Davis", "Tyler Warren",
    ]
    leaks = []
    for p in nfl_stars:
        m = (p.get("market") or "")
        for name in non_star_names:
            if name in m:
                leaks.append((name, m))
                break
    assert not leaks, f"non-star names leaked into stars_only: {leaks[:5]}"


def test_star_filter_never_mutates_scores(nfl_all, nfl_stars):
    """Every pick present in BOTH the plain and stars_only response
    must carry identical scoring fields.  Visibility-only invariant."""
    by_id_plain = {p.get("id"): p for p in nfl_all if p.get("id")}
    checked = 0
    mismatches = []
    for s in nfl_stars:
        pid = s.get("id")
        if not pid or pid not in by_id_plain:
            continue
        p = by_id_plain[pid]
        checked += 1
        for field in ("lock_score", "win_probability", "book_odds",
                      "edge_percent", "grade", "line"):
            if s.get(field) != p.get(field):
                mismatches.append((pid, field, s.get(field), p.get(field)))
    assert checked > 0, "no overlap between star and plain responses"
    assert not mismatches, (
        f"star filter mutated scoring fields on {len(mismatches)} picks: "
        f"{mismatches[:5]}"
    )


def test_star_filter_composes_with_market_pass_yds(bearer):
    """STARS + PASS_YDS should return only passing-yards picks for
    canonical star QBs.  No cross-family leakage into other markets."""
    r = requests.get(
        f"{BASE_URL}/api/picks/today"
        f"?sport=NFL&stars_only=true&market=passing_yards&lite=true",
        headers={"Authorization": f"Bearer {bearer}"},
        timeout=60,
    )
    assert r.status_code == 200
    picks = r.json().get("picks", [])
    assert len(picks) > 0, "STARS + PASS_YDS returned no picks"
    for p in picks:
        m = (p.get("market") or "").lower()
        assert ("passing yards" in m or "pass yds" in m), (
            f"non-passing-yards leak in STARS+PASS_YDS: {p.get('market')}"
        )


def test_star_filter_composes_with_market_rec_yds(bearer):
    r = requests.get(
        f"{BASE_URL}/api/picks/today"
        f"?sport=NFL&stars_only=true&market=receiving_yards&lite=true",
        headers={"Authorization": f"Bearer {bearer}"},
        timeout=60,
    )
    assert r.status_code == 200
    picks = r.json().get("picks", [])
    assert len(picks) > 0, "STARS + REC_YDS returned no picks"
    for p in picks:
        m = (p.get("market") or "").lower()
        assert ("receiving yards" in m or "reception yds" in m), (
            f"non-receiving-yards leak: {p.get('market')}"
        )


def test_star_filter_iter138_labels_preserved(nfl_stars):
    """Iter 138 non-regression: no raw ' · ALT LOCK' should appear
    in the stars_only response."""
    raw = [p.get("market") for p in nfl_stars
           if "ALT LOCK" in (p.get("market") or "")]
    assert not raw, f"iter138 regression: raw ALT LOCK in stars: {raw[:3]}"
