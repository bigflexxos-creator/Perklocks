"""
Backend verification for surgical NFL ATD leaderboard routing fix.

Verifies:
  1. GET /api/nfl/atd/leaderboard returns 200 with expected shape (canonical_publication).
  2. Filters honored (limit, min_probability, min_opportunity_rating).
  3. GET /api/nfl/atd/predict is reachable (no routing 404).
  4. Regression sanity: /api/picks/today?lite=true still 200 (with bearer).
  5. No fabricated data — all picks carry real provenance/book_odds.
"""

import os
import requests
import pytest

BASE_URL = os.environ["EXPO_PUBLIC_BACKEND_URL"].rstrip("/")
DEMO_EMAIL = "demo@lockscore.ai"
DEMO_PW = "demo123"


@pytest.fixture(scope="module")
def api():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module")
def bearer(api):
    """Attempt to get a bearer token for regression endpoint."""
    for path in ("/api/auth/login", "/api/users/login"):
        try:
            r = api.post(
                f"{BASE_URL}{path}",
                json={"email": DEMO_EMAIL, "password": DEMO_PW},
                timeout=30,
            )
            if r.status_code == 200:
                data = r.json()
                tok = (
                    data.get("access_token")
                    or data.get("token")
                    or (data.get("user") or {}).get("access_token")
                )
                if tok:
                    return tok
        except Exception:
            continue
    return None


# ── 1. ATD leaderboard: routing + shape ───────────────────────────────────
class TestAtdLeaderboardRouting:
    def test_endpoint_returns_200_not_404(self, api):
        r = api.get(f"{BASE_URL}/api/nfl/atd/leaderboard", timeout=45)
        assert r.status_code == 200, (
            f"Expected 200, got {r.status_code}. body[:400]={r.text[:400]}"
        )

    def test_response_has_required_top_level_keys(self, api):
        r = api.get(f"{BASE_URL}/api/nfl/atd/leaderboard", timeout=45)
        assert r.status_code == 200
        data = r.json()
        for k in ("mode", "picks", "total_candidates"):
            assert k in data, f"missing top-level key '{k}': {list(data.keys())}"
        assert isinstance(data["picks"], list)
        assert isinstance(data["total_candidates"], int)

    def test_mode_is_canonical_publication_when_picks_exist(self, api):
        r = api.get(f"{BASE_URL}/api/nfl/atd/leaderboard", timeout=45)
        data = r.json()
        # If canonical picks present, mode must be canonical_publication.
        # Per problem statement 3 canonical picks currently exist.
        if data.get("picks"):
            assert data["mode"] == "canonical_publication", (
                f"expected canonical_publication, got mode={data.get('mode')}"
            )

    def test_pick_shape_and_provenance(self, api):
        r = api.get(f"{BASE_URL}/api/nfl/atd/leaderboard", timeout=45)
        data = r.json()
        picks = data.get("picks") or []
        assert len(picks) >= 1, "no picks returned — cannot verify pick shape"
        required = [
            "player_name", "team", "td_probability", "book_odds",
            "market", "publication_state", "provenance",
        ]
        for p in picks:
            for f in required:
                assert f in p, f"pick missing field '{f}': keys={list(p.keys())}"
            assert isinstance(p["td_probability"], (int, float))
            assert p["provenance"] == "canonical_publication", (
                f"provenance must be canonical_publication, got {p['provenance']}"
            )
            assert p["publication_state"] == "PUBLISHED", (
                f"publication_state must be PUBLISHED, got {p['publication_state']}"
            )
            # No mock/fabricated data guard: book_odds must be non-empty
            assert p["book_odds"] not in (None, "", 0), (
                f"book_odds missing/fabricated for {p.get('player_name')}: {p['book_odds']}"
            )

    def test_picks_sorted_by_td_probability_desc(self, api):
        r = api.get(f"{BASE_URL}/api/nfl/atd/leaderboard", timeout=45)
        data = r.json()
        picks = data.get("picks") or []
        if len(picks) < 2:
            pytest.skip("need >=2 picks to verify sort order")
        probs = [float(p["td_probability"]) for p in picks]
        assert probs == sorted(probs, reverse=True), (
            f"picks not sorted by td_probability DESC: {probs}"
        )
        # Verify tiebreaker: for equal probs, confidence must be desc
        for i in range(len(picks) - 1):
            if probs[i] == probs[i + 1]:
                assert (
                    float(picks[i].get("confidence", 0))
                    >= float(picks[i + 1].get("confidence", 0))
                ), "tiebreaker (confidence DESC) violated"


# ── 2. ATD leaderboard: filters ───────────────────────────────────────────
class TestAtdLeaderboardFilters:
    def test_filters_and_limit_honored(self, api):
        r = api.get(
            f"{BASE_URL}/api/nfl/atd/leaderboard",
            params={
                "limit": 5,
                "min_probability": 0.1,
                "min_opportunity_rating": "low",
            },
            timeout=45,
        )
        assert r.status_code == 200, f"got {r.status_code}: {r.text[:300]}"
        data = r.json()
        assert "picks" in data
        assert len(data["picks"]) <= 5, (
            f"limit=5 violated: got {len(data['picks'])} picks"
        )
        for p in data["picks"]:
            assert float(p["td_probability"]) >= 0.1, (
                f"td_probability {p['td_probability']} < min 0.1"
            )


# ── 3. ATD predict reachability ───────────────────────────────────────────
class TestAtdPredictReachable:
    def test_predict_not_a_routing_404(self, api):
        r = api.get(
            f"{BASE_URL}/api/nfl/atd/predict",
            params={"player_id": "nonexistent-xyz"},
            timeout=45,
        )
        # Must NOT be routing 404 (i.e., FastAPI "Not Found" for missing route).
        # 400 / 404 with a domain-level message is acceptable.
        if r.status_code == 404:
            body = r.text.lower()
            assert "not found" not in body or "player" in body or "atd" in body, (
                f"looks like routing 404, not domain 404: {r.text[:300]}"
            )
        assert r.status_code in (200, 400, 404, 422, 500), (
            f"unexpected status {r.status_code}: {r.text[:200]}"
        )


# ── 4. Regression: /api/picks/today?lite=true ─────────────────────────────
class TestPicksTodayRegression:
    def test_picks_today_lite_200(self, api, bearer):
        if not bearer:
            pytest.skip("could not obtain bearer token for demo user")
        r = api.get(
            f"{BASE_URL}/api/picks/today",
            params={"lite": "true"},
            headers={"Authorization": f"Bearer {bearer}"},
            timeout=60,
        )
        assert r.status_code == 200, (
            f"picks/today?lite=true expected 200, got {r.status_code}: {r.text[:300]}"
        )
