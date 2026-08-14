"""iter-96: GoalScorer Engine v3 backend tests.

Covers:
  - Admin auth (demo@lockscore.ai / demo123)
  - GET  /api/admin/goalscorer/v3/status
  - POST /api/admin/goalscorer/v3/refresh
  - POST /api/admin/goalscorer/v3/predict
  - Picks feed stamped with `goal_scorer_v3` provenance
  - Regression: /api/health, /api/picks/today, /api/admin/odds-diagnostic
"""
import os
import pytest
import requests

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "https://canonical-parity.preview.emergentagent.com").rstrip("/")
DEMO_EMAIL = "demo@lockscore.ai"
DEMO_PASS = "demo123"


@pytest.fixture(scope="module")
def token():
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"email": DEMO_EMAIL, "password": DEMO_PASS}, timeout=30)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:400]}"
    tok = r.json().get("access_token")
    assert tok
    return tok


@pytest.fixture(scope="module")
def auth_headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ─── 1. /api/admin/goalscorer/v3/status ────────────────────────────
class TestV3Status:
    def test_status_returns_engine_version_and_leagues(self, auth_headers):
        r = requests.get(f"{BASE_URL}/api/admin/goalscorer/v3/status",
                         headers=auth_headers, timeout=120)
        assert r.status_code == 200, f"{r.status_code} {r.text[:400]}"
        data = r.json()
        assert data.get("engine_version") == "gs_v3.0.0", f"engine_version={data.get('engine_version')}"
        leagues = data.get("leagues") or {}
        for lg in ("EPL", "LaLiga", "SerieA", "Bundesliga", "Ligue1", "MLS"):
            assert lg in leagues, f"missing league {lg}"
            info = leagues[lg]
            assert "matches_used" in info
            assert "seasons_used" in info
            assert "teams_indexed" in info
        # Top-5 European leagues need >= 20 teams
        for lg in ("EPL", "LaLiga", "SerieA", "Bundesliga", "Ligue1"):
            ti = leagues[lg]["teams_indexed"]
            assert ti >= 20, f"{lg} teams_indexed={ti} (<20)"
            # Seasons include at least 2 of the target set
            seasons = set(leagues[lg]["seasons_used"] or [])
            target = {"2022-23", "2023-24", "2024-25"}
            assert len(seasons & target) >= 2, f"{lg} seasons_used={seasons}"
        # MLS needs >= 25 teams
        mls_ti = leagues["MLS"]["teams_indexed"]
        assert mls_ti >= 25, f"MLS teams_indexed={mls_ti} (<25)"

    def test_status_requires_admin(self):
        r = requests.get(f"{BASE_URL}/api/admin/goalscorer/v3/status", timeout=30)
        assert r.status_code in (401, 403)


# ─── 2. /api/admin/goalscorer/v3/refresh ───────────────────────────
class TestV3Refresh:
    def test_refresh_returns_ok(self, auth_headers):
        r = requests.post(f"{BASE_URL}/api/admin/goalscorer/v3/refresh",
                          headers=auth_headers, timeout=180)
        assert r.status_code == 200, f"{r.status_code} {r.text[:400]}"
        data = r.json()
        assert data.get("ok") is True
        refreshed = data.get("refreshed") or {}
        assert isinstance(refreshed, dict) and len(refreshed) >= 6


# ─── 3. /api/admin/goalscorer/v3/predict ───────────────────────────
class TestV3Predict:
    def _predict(self, headers, payload):
        return requests.post(f"{BASE_URL}/api/admin/goalscorer/v3/predict",
                             headers=headers, json=payload, timeout=120)

    def test_salah_vs_chelsea_home_starting_xi(self, auth_headers):
        r = self._predict(auth_headers, {
            "player": "Mohamed Salah",
            "opponent": "Chelsea",
            "league_hint": "EPL",
            "sport_key": "soccer_epl",
            "is_home": True,
            "lineup_status": "starting_xi",
        })
        assert r.status_code == 200, f"{r.status_code} {r.text[:400]}"
        data = r.json()
        pred = data.get("prediction") or {}
        # Shape assertions
        for k in ("p_anytime", "lam_player", "lam_team", "lam_opponent",
                  "expected_minutes", "confidence", "ensemble"):
            assert k in pred, f"missing prediction.{k}"
        assert 0.0 <= pred["p_anytime"] <= 1.0
        ens = pred["ensemble"] or {}
        for k in ("monte_carlo", "closed_form", "form_baseline"):
            assert k in ens, f"missing prediction.ensemble.{k}"
        assert pred["confidence"] in ("HIGH", "MEDIUM", "LOW")
        # Business assertions for Salah@home vs Chelsea
        assert 0.20 <= pred["p_anytime"] <= 0.55, f"p_anytime={pred['p_anytime']}"
        assert pred["confidence"] == "HIGH", f"confidence={pred['confidence']}"
        assert pred["lam_team"] > 1.5, f"lam_team={pred['lam_team']}"

    def test_messi_vs_la_galaxy_mls(self, auth_headers):
        r = self._predict(auth_headers, {
            "player": "Lionel Messi",
            "opponent": "LA Galaxy",
            "league_hint": "MLS",
            "sport_key": "soccer_usa_mls",
            "is_home": True,
            "lineup_status": "starting_xi",
        })
        assert r.status_code == 200, f"{r.status_code} {r.text[:400]}"
        data = r.json()
        pred = data.get("prediction") or {}
        assert 0.0 < pred.get("p_anytime", 0) <= 1.0
        assert pred.get("confidence") in ("HIGH", "MEDIUM"), f"conf={pred.get('confidence')}"

    def test_nonexistent_player_returns_404(self, auth_headers):
        r = self._predict(auth_headers, {
            "player": "Xxx Nonexistent",
            "opponent": "Chelsea",
        })
        assert r.status_code == 404, f"expected 404, got {r.status_code}: {r.text[:300]}"


# ─── 4. Picks feed stamped with v3 metadata ────────────────────────
class TestPicksV3Stamp:
    def test_soccer_picks_include_v3_stamp_list(self, auth_headers):
        """Validates picks feed (lite/list endpoint) exposes v3 stamps.

        Uses limit=500 because with today's slate (215 assists dominate),
        the requested limit=50 with sort=confidence excludes all AGS picks.
        """
        r = requests.get(f"{BASE_URL}/api/picks/today",
                         params={"sport": "Soccer", "sort": "confidence", "limit": 500},
                         headers=auth_headers, timeout=120)
        assert r.status_code == 200, f"{r.status_code} {r.text[:400]}"
        picks = r.json().get("picks") or []
        ags = [p for p in picks if (p.get("market_type") or "").lower() == "anytime_goal_scorer"]
        if not ags:
            pytest.skip("no anytime_goal_scorer picks in current soccer slate")

        v3_stamped = [p for p in ags if p.get("source") == "goal_scorer_v3"]
        assert v3_stamped, (
            f"no AGS picks have source='goal_scorer_v3'. "
            f"sample sources: {[p.get('source') for p in ags[:5]]}"
        )
        p = v3_stamped[0]
        # These match the review request exactly
        assert p.get("edge_percent") is None, f"edge_percent={p.get('edge_percent')} (expected None)"
        # NOTE: LIST endpoint rewrites odds_source via _odds_decorate → "odds_api"
        # rather than preserving stored "model_derived". Report as issue.
        assert p.get("odds_source") == "model_derived", (
            f"odds_source={p.get('odds_source')} (list endpoint rewriting via _odds_decorate)")
        rat = p.get("pick_rationale") or {}
        assert rat.get("engine") == "goal_scorer_v3", f"engine={rat.get('engine')}"
        sig = rat.get("v3_signals") or {}
        for k in ("lam_player", "lam_team", "lam_opponent", "expected_minutes",
                  "goal_share", "ensemble", "p_first", "p_2plus", "seasons_used"):
            assert k in sig, (
                f"missing v3_signals.{k} in list-endpoint response — "
                f"_slim_rationale is stripping it. present={list(sig.keys())}")

    def test_soccer_picks_v3_stamp_detail(self, auth_headers):
        """Cross-check: does detail endpoint /api/picks/{id} carry the v3 signals?"""
        r = requests.get(f"{BASE_URL}/api/picks/today",
                         params={"sport": "Soccer", "limit": 500},
                         headers=auth_headers, timeout=120)
        assert r.status_code == 200
        picks = r.json().get("picks") or []
        v3 = [p for p in picks if p.get("source") == "goal_scorer_v3"]
        if not v3:
            pytest.skip("no v3 picks in current soccer slate")
        pid = v3[0]["id"]
        r2 = requests.get(f"{BASE_URL}/api/picks/{pid}", headers=auth_headers, timeout=30)
        assert r2.status_code == 200
        pd = r2.json()
        rat = pd.get("pick_rationale") or {}
        assert rat.get("engine") == "goal_scorer_v3"
        sig = rat.get("v3_signals") or {}
        for k in ("lam_player", "lam_team", "lam_opponent", "expected_minutes",
                  "goal_share", "ensemble", "p_first", "p_2plus", "seasons_used"):
            assert k in sig, f"detail missing v3_signals.{k}"
        # Detail should have odds_source=model_derived (stored value)
        assert pd.get("odds_source") == "model_derived", (
            f"detail odds_source={pd.get('odds_source')}")
        # NOTE: Detail endpoint returns edge_percent=-25.18 (post-hoc calc)
        # even though writer stored None. Report as inconsistency.
        # Not enforced here — just documented.

    def test_non_goalscorer_soccer_still_uses_v2(self, auth_headers):
        r = requests.get(f"{BASE_URL}/api/picks/today",
                         params={"sport": "Soccer", "sort": "confidence", "limit": 100},
                         headers=auth_headers, timeout=90)
        assert r.status_code == 200
        picks = r.json().get("picks") or []
        others = [p for p in picks
                  if (p.get("market_type") or "").lower() in ("anytime_assist", "goal_involvement")]
        if not others:
            pytest.skip("no assist/goal_involvement picks in slate")
        # These should NOT be stamped v3.
        v3_leak = [p for p in others if p.get("source") == "goal_scorer_v3"]
        assert not v3_leak, f"v3 leaked into {[p.get('market_type') for p in v3_leak[:3]]}"


# ─── 5. Regression ─────────────────────────────────────────────────
class TestRegression:
    def test_health(self):
        r = requests.get(f"{BASE_URL}/api/health", timeout=30)
        assert r.status_code == 200

    def test_picks_today_smoke(self, auth_headers):
        r = requests.get(f"{BASE_URL}/api/picks/today",
                         params={"limit": 5}, headers=auth_headers, timeout=60)
        assert r.status_code == 200
        assert "picks" in r.json()

    def test_picks_soccer_sort_time(self, auth_headers):
        r = requests.get(f"{BASE_URL}/api/picks/today",
                         params={"sport": "Soccer", "sort": "time", "limit": 10},
                         headers=auth_headers, timeout=90)
        assert r.status_code == 200

    def test_admin_odds_diagnostic(self, auth_headers):
        r = requests.get(f"{BASE_URL}/api/admin/odds-diagnostic",
                         headers=auth_headers, timeout=60)
        assert r.status_code == 200
