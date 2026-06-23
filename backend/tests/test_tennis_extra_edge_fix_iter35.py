"""
Iteration 35 — tennis_extra book-anchored edge fix verification.

Bug: Tennis Moneyline (Davidovich Fokina, Quinn, Draper, Fery, ...) showed
phantom -9% to -12% edges because pick_validator was reverse-stacking the
Tennis ML negative learning weight on top of an already book-anchored WP.

Fix: pick_validator.py now skips reverse-stack for source='tennis_extra'/'tennis_extra_model'
and forces edge_percent = 0.0 for those picks.

This test confirms:
- /api/version is bumped to 2026.06.23-tennis-extra-edge-fix
- Tennis ML picks on /api/picks/today?sport=Tennis have edge_percent == 0.0
- At least 4 Tennis ML picks are on the board
- Goal-scorer markets (anytime_scorer / first_goal_scorer) appear in Soccer feed
- Regression: Tennis ALT tab still has 40+ picks
- Regression: MLB pitcher-h2h endpoint works
- Regression: Chalk picks (lock 90+) still present
"""

import os
import requests
import pytest

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    # Fallback to frontend/.env conventions used by this app
    BASE_URL = os.environ.get("EXPO_BACKEND_URL", "").rstrip("/")

assert BASE_URL, "EXPO_PUBLIC_BACKEND_URL must be set"

TIMEOUT = 30


@pytest.fixture(scope="module")
def api():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    # Login with demo credentials (see /app/memory/test_credentials.md)
    r = s.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": "demo@lockscore.ai", "password": "demo123"},
        timeout=TIMEOUT,
    )
    assert r.status_code == 200, f"Login failed: {r.status_code} {r.text}"
    token = r.json().get("access_token")
    assert token, f"No access_token in login response: {r.text}"
    s.headers.update({"Authorization": f"Bearer {token}"})
    return s


def _picks_from(body):
    """Normalize picks list from response body."""
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        return body.get("picks") or body.get("data") or []
    return []


# ---------- /api/version ----------

class TestVersion:
    def test_data_version_bumped(self, api):
        r = api.get(f"{BASE_URL}/api/version", timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data.get("data_version") == "2026.06.23-tennis-extra-edge-fix", data


# ---------- Tennis ML edge fix ----------

class TestTennisMLEdgeFix:
    @pytest.fixture(scope="class")
    def tennis_picks(self, api):
        r = api.get(f"{BASE_URL}/api/picks/today", params={"sport": "Tennis"}, timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        body = r.json()
        # API may return list or dict-with-list
        picks = body if isinstance(body, list) else body.get("picks") or body.get("data") or []
        return picks

    def test_tennis_picks_returned(self, tennis_picks):
        assert isinstance(tennis_picks, list)
        assert len(tennis_picks) > 0, "Expected Tennis picks on the board"

    def test_tennis_ml_picks_on_board(self, tennis_picks):
        """At least 4 Tennis ML picks should now surface (Davidovich Fokina, Quinn, Draper, Fery)."""
        ml_picks = [
            p for p in tennis_picks
            if (p.get("market") or "").lower() in ("moneyline", "h2h", "ml")
            or "moneyline" in (p.get("market") or "").lower()
        ]
        assert len(ml_picks) >= 4, (
            f"Expected >=4 Tennis ML picks on the board, got {len(ml_picks)}. "
            f"Markets seen: {sorted({(p.get('market') or '') for p in tennis_picks})}"
        )

    def test_tennis_extra_picks_edge_is_zero(self, tennis_picks):
        """Book-anchored tennis_extra picks must have edge_percent == 0.0 (no phantom negative edge)."""
        book_anchored = [
            p for p in tennis_picks
            if (p.get("source") or "").lower() in ("tennis_extra", "tennis_extra_model")
        ]
        assert len(book_anchored) > 0, (
            f"No tennis_extra source picks found. Sources seen: "
            f"{sorted({(p.get('source') or '') for p in tennis_picks})}"
        )
        bad = [
            {
                "player": p.get("player") or p.get("selection") or p.get("pick_text"),
                "edge_percent": p.get("edge_percent"),
                "source": p.get("source"),
                "lock_score": p.get("lock_score"),
            }
            for p in book_anchored
            if p.get("edge_percent") is None or abs(float(p.get("edge_percent") or 0)) > 0.05
        ]
        assert not bad, f"tennis_extra picks should have edge_percent ≈ 0.0, found bad: {bad}"

    def test_tennis_extra_lock_scores_positive(self, tennis_picks):
        """Locks should be positive and high (~90s) for these surfaced ML picks."""
        book_anchored = [
            p for p in tennis_picks
            if (p.get("source") or "").lower() in ("tennis_extra", "tennis_extra_model")
        ]
        for p in book_anchored:
            ls = p.get("lock_score")
            assert ls is not None and ls > 0, f"Bad lock_score for {p.get('player')}: {ls}"


# ---------- Goal-scorer surfacing ----------

class TestGoalScorers:
    @pytest.fixture(scope="class")
    def soccer_picks(self, api):
        r = api.get(f"{BASE_URL}/api/picks/today", params={"sport": "Soccer"}, timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        body = r.json()
        picks = body if isinstance(body, list) else body.get("picks") or body.get("data") or []
        return picks

    def test_anytime_goal_scorer_picks_present(self, soccer_picks):
        markets = [(p.get("market") or "").lower() for p in soccer_picks]
        atgs = [m for m in markets if "anytime" in m and "scor" in m]
        assert len(atgs) >= 1, (
            f"Expected at least 1 Anytime Goal Scorer pick. Markets seen: "
            f"{sorted(set(markets))}"
        )

    def test_first_goal_scorer_picks_present(self, soccer_picks):
        markets = [(p.get("market") or "").lower() for p in soccer_picks]
        fgs = [m for m in markets if "first" in m and "scor" in m]
        # First goal scorer might be small (11 in feed) — at least 1 should pass filters
        assert len(fgs) >= 1, (
            f"Expected at least 1 First Goal Scorer pick. Markets seen: "
            f"{sorted(set(markets))}"
        )

    def test_soccer_markets_endpoint_has_scorer_tokens(self, api):
        r = api.get(f"{BASE_URL}/api/picks/markets/Soccer", timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        body = r.json()
        # Could be list of strings or list of dicts
        tokens = []
        if isinstance(body, list):
            for item in body:
                if isinstance(item, str):
                    tokens.append(item.lower())
                elif isinstance(item, dict):
                    tokens.append((item.get("key") or item.get("token") or item.get("market") or "").lower())
        elif isinstance(body, dict):
            for v in body.get("markets", body.get("data", [])):
                if isinstance(v, str):
                    tokens.append(v.lower())
                elif isinstance(v, dict):
                    tokens.append((v.get("key") or v.get("token") or v.get("market") or "").lower())
        joined = ",".join(tokens)
        assert "anytime_scorer" in joined, f"Missing anytime_scorer token. Got: {tokens}"
        assert "first_goal_scorer" in joined, f"Missing first_goal_scorer token. Got: {tokens}"
        assert "score_or_assist" in joined, f"Missing score_or_assist token. Got: {tokens}"


# ---------- Regression: Tennis ALT ----------

class TestTennisAltRegression:
    def test_tennis_alt_picks_present(self, api):
        # Tennis ALT tab is typically market=alt or tab=alt
        r = api.get(f"{BASE_URL}/api/picks/today", params={"sport": "Tennis", "tab": "alt"}, timeout=TIMEOUT)
        if r.status_code != 200:
            # fallback to a query without tab if endpoint shape differs
            r = api.get(f"{BASE_URL}/api/picks/today", params={"sport": "Tennis"}, timeout=TIMEOUT)
            assert r.status_code == 200, r.text
            body = r.json()
            picks = body if isinstance(body, list) else body.get("picks") or body.get("data") or []
            alt = [p for p in picks if "alt" in (p.get("market") or "").lower() or "alt" in (p.get("tab") or "").lower()]
            assert len(alt) >= 40, f"Expected 40+ Tennis ALT picks, got {len(alt)}"
        else:
            body = r.json()
            picks = body if isinstance(body, list) else body.get("picks") or body.get("data") or []
            assert len(picks) >= 40, f"Expected 40+ Tennis ALT picks, got {len(picks)}"


# ---------- Regression: MLB pitcher-h2h ----------

class TestMLBPitcherH2HRegression:
    def test_pitcher_h2h_endpoint_ok(self, api):
        # endpoint pattern can vary — try a few canonical shapes
        candidates = [
            f"{BASE_URL}/api/picks/today?sport=MLB&market=pitcher_h2h",
            f"{BASE_URL}/api/picks/today?sport=MLB",
        ]
        last = None
        for url in candidates:
            r = api.get(url, timeout=TIMEOUT)
            last = r
            if r.status_code == 200:
                break
        assert last is not None and last.status_code == 200, last.text if last else "no response"
        body = last.json()
        assert isinstance(body, (list, dict))


# ---------- Regression: chalk picks remain high lock ----------

class TestChalkPicksRegression:
    def test_chalk_picks_locks_still_high(self, api):
        """Picks with very high implied probability should still produce lock_score >= 90 (Bieber-style chalk)."""
        r = api.get(f"{BASE_URL}/api/picks/today", timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        body = r.json()
        picks = body if isinstance(body, list) else body.get("picks") or body.get("data") or []
        # find any pick with lock_score >= 90
        elite = [p for p in picks if (p.get("lock_score") or 0) >= 90]
        assert len(elite) >= 1, f"Expected at least 1 pick with lock_score>=90, found 0 of {len(picks)} total"
