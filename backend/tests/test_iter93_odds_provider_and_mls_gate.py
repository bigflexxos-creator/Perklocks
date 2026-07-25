"""Iter-93 backend tests.

Two independent changes to verify:
  A) Odds provider fallback layer (facade + circuit breaker + decorator)
  B) Soccer MLS goalscorer/assist data-quality gate

See review request in /app/test_reports/iteration_93 for full spec.
"""
import os
import sys
import time
import pytest
import requests

# Ensure backend imports resolve for in-process tests (Part A simulation).
sys.path.insert(0, "/app/backend")

# Load /app/backend/.env so odds_provider sees API_SPORTS_KEY_1..3 when the
# tests import it fresh in this pytest process (the backend service loads
# these itself at startup — this is only needed for the in-process test).
try:
    from dotenv import load_dotenv
    load_dotenv("/app/backend/.env")
except Exception:
    pass

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "https://player-intel-engine.preview.emergentagent.com").rstrip("/")


# ── shared auth fixture ─────────────────────────────────────────────
@pytest.fixture(scope="module")
def auth_headers():
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": "demo@lockscore.ai", "password": "demo123"},
        timeout=15,
    )
    assert r.status_code == 200, f"Login failed: {r.status_code} {r.text[:200]}"
    tok = r.json().get("access_token") or r.json().get("token")
    assert tok, f"No token in login response: {r.json()}"
    return {"Authorization": f"Bearer {tok}"}


# ────────────────────────────────────────────────────────────────────
# PART A — Odds provider fallback layer
# ────────────────────────────────────────────────────────────────────
class TestOddsHealthEndpoint:
    """/api/admin/odds-health envelope."""

    def test_odds_health_returns_expected_keys(self, auth_headers):
        r = requests.get(f"{BASE_URL}/api/admin/odds-health", headers=auth_headers, timeout=20)
        assert r.status_code == 200, f"Status {r.status_code}: {r.text[:200]}"
        data = r.json()
        for key in ("state", "active_source", "primary_provider",
                    "api_sports_keys_configured", "failures_in_window"):
            assert key in data, f"Missing key {key}: {data}"
        # Expect 3 API-Sports keys configured per .env.
        assert data["api_sports_keys_configured"] == 3, \
            f"expected 3 API-Sports keys, got {data['api_sports_keys_configured']}"
        assert data["primary_provider"] in ("odds_api", "api_sports", "espn")
        assert data["state"] in ("live", "degraded")
        assert data["active_source"] in ("odds_api", "api_sports", "espn")


# ────────────────────────────────────────────────────────────────────
# /api/picks/today envelope + decoration
# ────────────────────────────────────────────────────────────────────
class TestPicksEnvelopeAndDecoration:
    """Every pick must carry odds_source/odds_status/confidence_penalty,
    and the envelope must include odds_provider."""

    @pytest.fixture(scope="class")
    def mlb_response(self, auth_headers):
        t0 = time.time()
        r = requests.get(
            f"{BASE_URL}/api/picks/today?sport=MLB",
            headers=auth_headers,
            timeout=30,
        )
        elapsed = time.time() - t0
        assert r.status_code == 200, f"{r.status_code}: {r.text[:200]}"
        data = r.json()
        data["_elapsed"] = elapsed
        return data

    def test_envelope_has_odds_provider(self, mlb_response):
        assert "odds_provider" in mlb_response, "odds_provider missing from envelope"
        env = mlb_response["odds_provider"]
        assert env is not None, "odds_provider envelope was None"
        assert "state" in env and "active_source" in env, f"envelope missing keys: {env}"

    def test_every_pick_has_odds_tags(self, mlb_response):
        picks = mlb_response.get("picks", [])
        if not picks:
            pytest.skip("No MLB picks on the board today — cannot assert per-pick decoration")
        missing = []
        for p in picks:
            for k in ("odds_source", "odds_status", "confidence_penalty"):
                if k not in p or p.get(k) is None:
                    missing.append((p.get("id") or p.get("pick_id") or "?", k))
                    break
        assert not missing, f"Picks missing odds tags: {missing[:5]}"

    def test_live_path_tags(self, mlb_response):
        """When the envelope reports state=live/active_source=odds_api,
        every pick MUST also read that (penalty=0)."""
        env = mlb_response["odds_provider"]
        if env["state"] != "live" or env["active_source"] != "odds_api":
            pytest.skip(f"Envelope not on live/odds_api path: {env}")
        picks = mlb_response.get("picks", [])
        if not picks:
            pytest.skip("No MLB picks on the board")
        for p in picks:
            assert p["odds_source"] == "odds_api", f"pick {p.get('id')} odds_source={p['odds_source']}"
            assert p["odds_status"] == "live", f"pick {p.get('id')} odds_status={p['odds_status']}"
            assert p["confidence_penalty"] == 0, f"pick {p.get('id')} penalty={p['confidence_penalty']}"

    def test_mlb_perf(self, mlb_response):
        assert mlb_response["_elapsed"] < 10.0, f"MLB picks took {mlb_response['_elapsed']:.1f}s (>10s)"


# ────────────────────────────────────────────────────────────────────
# In-process circuit breaker (Part A behavioural test)
# ────────────────────────────────────────────────────────────────────
class TestOddsProviderCircuitBreakerInProcess:
    """Exercise the odds_provider module directly (no server round-trip)."""

    def test_report_failure_flips_to_degraded_and_decorate_backup(self):
        import asyncio
        from services import odds_provider

        # Ensure baseline
        odds_provider.report_success()
        assert odds_provider.get_state() == "live"
        assert odds_provider.get_active_source() == "odds_api"

        # Three 429s in the window → degraded
        odds_provider.report_failure(429, "test1")
        odds_provider.report_failure(429, "test2")
        odds_provider.report_failure(429, "test3")

        # status() is async — get snapshot without allowing the probe to
        # recover (it will only re-run if _last_probe_ts is old; force it
        # to now so probe is skipped in this fast test).
        odds_provider._last_probe_ts = time.time()
        snap = asyncio.get_event_loop().run_until_complete(odds_provider.status()) \
            if False else asyncio.new_event_loop().run_until_complete(odds_provider.status())

        assert snap["state"] == "degraded", f"expected degraded, got {snap}"
        assert snap["active_source"] == "api_sports", f"expected api_sports, got {snap['active_source']}"

        # decorate_pick under degraded state
        p = {"lock_score": 95, "edge_percent": 8.5, "id": "test_pick"}
        decorated = odds_provider.decorate_pick(p)
        assert decorated["odds_source"] == "api_sports", decorated
        assert decorated["odds_status"] == "backup", decorated
        assert decorated["confidence_penalty"] == -10, decorated
        assert decorated["edge_percent"] is None, decorated
        assert decorated["lock_score"] == 85.0, f"expected 85.0, got {decorated['lock_score']}"

    def test_report_success_clears_state_and_penalty_zero(self):
        import asyncio
        from services import odds_provider

        odds_provider.report_success()
        odds_provider._last_probe_ts = time.time()  # skip probe
        loop = asyncio.new_event_loop()
        snap = loop.run_until_complete(odds_provider.status())
        loop.close()

        assert snap["state"] == "live"
        assert snap["active_source"] == "odds_api"

        p = {"lock_score": 92, "edge_percent": 5.0, "id": "fresh"}
        decorated = odds_provider.decorate_pick(p)
        assert decorated["odds_source"] == "odds_api"
        assert decorated["odds_status"] == "live"
        assert decorated["confidence_penalty"] == 0
        assert decorated["edge_percent"] == 5.0
        assert decorated["lock_score"] == 92


# ────────────────────────────────────────────────────────────────────
# PART B — Soccer MLS data-quality gate + iter-92 tiebreaker regression
# ────────────────────────────────────────────────────────────────────
class TestMLSDataQualityGate:

    @pytest.fixture(scope="class")
    def soccer_mls_response(self, auth_headers):
        t0 = time.time()
        r = requests.get(
            f"{BASE_URL}/api/picks/today?sport=Soccer&leagues=MLS",
            headers=auth_headers,
            timeout=45,
        )
        elapsed = time.time() - t0
        assert r.status_code == 200, f"{r.status_code}: {r.text[:200]}"
        data = r.json()
        data["_elapsed"] = elapsed
        return data

    def test_soccer_perf(self, soccer_mls_response):
        assert soccer_mls_response["_elapsed"] < 12.0, \
            f"MLS soccer picks took {soccer_mls_response['_elapsed']:.1f}s (>12s)"

    def test_data_quality_gate_no_zero_sample_scorer_picks(self, soccer_mls_response):
        """Iter-93 gate: no scorer/assist pick may have samples.games==0 AND
        samples.minutes<180 AND all-zero attack signals."""
        picks = soccer_mls_response.get("picks", [])
        target_mt = {"anytime_goal_scorer", "anytime_assist", "anytime_goal_involvement", "to_score_or_assist"}
        offenders = []
        for p in picks:
            if p.get("market_type") not in target_mt:
                continue
            s = p.get("samples") or {}
            games = s.get("games") or 0
            minutes = s.get("minutes") or 0
            gp90 = s.get("goals_per_90") or 0
            ap90 = s.get("assists_per_90") or 0
            npxg = s.get("npxg_per_90") or 0
            if games == 0 and minutes < 180 and gp90 == 0 and ap90 == 0 and npxg == 0:
                offenders.append({
                    "player": p.get("selection"),
                    "market_type": p.get("market_type"),
                    "samples": s,
                })
        assert not offenders, f"{len(offenders)} scorer/assist picks slipped past the data-quality gate: {offenders[:3]}"

    def test_scorer_and_assist_have_real_stat_rationale(self, soccer_mls_response):
        """Every anytime_goal_scorer / anytime_assist / anytime_goal_involvement pick
        must reference a real stat in its `pick_rationale.evidence` bullets."""
        picks = soccer_mls_response.get("picks", [])
        target_mt = {"anytime_goal_scorer", "anytime_assist", "anytime_goal_involvement", "to_score_or_assist"}
        keywords = ("goal", "assist", "xg", "minute", "game", "shot", "npxg", "sca", "/90", "creator", "finisher", "form")
        offenders = []
        checked = 0
        for p in picks:
            if p.get("market_type") not in target_mt:
                continue
            checked += 1
            pr = p.get("pick_rationale") or {}
            ev_list = pr.get("evidence") if isinstance(pr.get("evidence"), list) else []
            rationale = (str(pr.get("summary") or "") + " " + " ".join(ev_list)).lower()
            if not any(k in rationale for k in keywords):
                offenders.append({
                    "player": p.get("selection"),
                    "market_type": p.get("market_type"),
                    "rationale_preview": rationale[:250],
                })
        if checked == 0:
            pytest.skip("No scorer/assist picks on today's MLS board")
        assert not offenders, f"{len(offenders)}/{checked} scorer/assist picks lack real-stat rationale: {offenders[:3]}"

    def test_mls_tiebreaker_regression_iter92(self, soccer_mls_response):
        """iter-92 tiebreaker: top-10 MLS picks (in returned order) include >=3
        Anytime Goal Scorer (or goal-involvement) entries.

        Bug-report-oriented: we assert on the RETURNED order because that's
        what the frontend renders as 'top picks'.
        """
        picks = soccer_mls_response.get("picks", [])
        if len(picks) < 10:
            pytest.skip(f"Only {len(picks)} MLS picks — cannot assert top-10 mix")
        top10 = picks[:10]
        scorer_like_count = sum(
            1 for p in top10
            if p.get("market_type") in ("anytime_goal_scorer", "anytime_goal_involvement", "to_score_or_assist")
        )
        assert scorer_like_count >= 3, \
            f"iter-92 regression: expected >=3 goal-scorer/involvement in top-10, got {scorer_like_count}. " \
            f"Top-10 market_types: {[p.get('market_type') for p in top10]}"


# ────────────────────────────────────────────────────────────────────
# MLB H2H bullet regression (iter-91)
# ────────────────────────────────────────────────────────────────────
class TestMLBH2HRegression:

    def test_h2h_bullet_present_on_at_least_one_batter(self, auth_headers):
        r = requests.get(f"{BASE_URL}/api/picks/today?sport=MLB",
                         headers=auth_headers, timeout=30)
        assert r.status_code == 200
        picks = r.json().get("picks", [])
        # Consider a pick a "batter" if archetype/market_type suggests hitter props.
        batters = []
        for p in picks:
            mt = str(p.get("market_type") or "").lower()
            arch = str(p.get("archetype") or "").lower()
            if any(x in mt for x in ("hit", "home_run", "total_bases", "batter", "hrrbi", "rbi")) \
               or "hitter" in arch:
                batters.append(p)
        if not batters:
            pytest.skip(f"No MLB batter picks on today's cold slate ({len(picks)} total picks) — cannot regress iter-91 H2H bullet")
        found = False
        for p in batters:
            pr = p.get("pick_rationale") or {}
            ev = pr.get("evidence") if isinstance(pr.get("evidence"), list) else []
            blob = (str(pr.get("summary") or "") + " " + " ".join(ev)).lower()
            if "h2h tailwind" in blob or "h2h headwind" in blob or "h2h" in blob:
                found = True
                break
        assert found, f"iter-91 regression: no MLB batter pick ({len(batters)}) has an 'H2H' bullet"
