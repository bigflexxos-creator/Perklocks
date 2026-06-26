"""Iter 59 — Verify SportDB-driven scorer + xG-totals features.

Scope (backend-only, per review_request):
  • /api/version returns data_version "2026.06.26-sportdb-scorer-xg"
  • /api/picks/today and /api/picks/today?lite=true respond <2s and 200
  • sportdb_player_scorer + sportdb_xg_totals modules import cleanly and
    expose the documented public surface
  • Existing admin endpoints still auth-gate correctly (401 unauth)
  • Regression: demo login + pick detail + /api/picks/all still 200
"""
from __future__ import annotations

import importlib
import os
import sys
import time

import pytest
import requests

BASE_URL = (os.environ.get("EXPO_PUBLIC_BACKEND_URL")
            or os.environ.get("EXPO_BACKEND_URL")
            or "https://bet-edge-ai-1.preview.emergentagent.com").rstrip("/")
DEMO_EMAIL = "demo@lockscore.ai"
DEMO_PASSWORD = "demo123"
EXPECTED_DATA_VERSION = "2026.06.26-sportdb-scorer-xg"

# Make /app/backend importable so we can smoke-test the modules in-process.
sys.path.insert(0, "/app/backend")


@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module")
def demo_token(session):
    r = session.post(f"{BASE_URL}/api/auth/login",
                     json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD},
                     timeout=10)
    assert r.status_code == 200, f"demo login failed: {r.status_code} {r.text[:200]}"
    return r.json().get("access_token")


@pytest.fixture(scope="module")
def auth_headers(demo_token):
    return {"Authorization": f"Bearer {demo_token}"}


def _picks_list(payload):
    """/api/picks/today response can be a raw list OR {picks: [...]}—normalise."""
    if isinstance(payload, dict):
        return payload.get("picks") or payload.get("items") or []
    return payload if isinstance(payload, list) else []


# ─────────────────── /api/version + module surface ───────────────────

class TestVersionAndImports:
    def test_data_version_matches_release(self, session):
        r = session.get(f"{BASE_URL}/api/version", timeout=10)
        assert r.status_code == 200
        body = r.json()
        assert body.get("data_version") == EXPECTED_DATA_VERSION, (
            f"data_version mismatch — got {body.get('data_version')!r}, "
            f"want {EXPECTED_DATA_VERSION!r}"
        )

    def test_player_scorer_imports_cleanly(self):
        mod = importlib.import_module("sportdb_player_scorer")
        importlib.reload(mod)  # exercise fresh module load
        assert hasattr(mod, "LEAGUE_MAP"), "LEAGUE_MAP missing"
        assert isinstance(mod.LEAGUE_MAP, dict) and len(mod.LEAGUE_MAP) >= 20
        # spot check the primary targets
        for key in ("soccer_china_superleague", "soccer_japan_j_league",
                    "soccer_usa_mls", "soccer_korea_kleague1"):
            assert key in mod.LEAGUE_MAP, f"{key} missing from LEAGUE_MAP"
        assert hasattr(mod, "compute_anytime_scorer_picks")
        assert callable(mod.compute_anytime_scorer_picks)
        assert hasattr(mod, "SYNTH_MIN_PROB") and 0 < mod.SYNTH_MIN_PROB < 1
        assert hasattr(mod, "SYNTH_MAX_PROB") and 0 < mod.SYNTH_MAX_PROB <= 1
        assert mod.SYNTH_MIN_PROB < mod.SYNTH_MAX_PROB

    def test_xg_totals_imports_cleanly(self):
        mod = importlib.import_module("sportdb_xg_totals")
        importlib.reload(mod)
        assert hasattr(mod, "enrich_totals_pick_with_xg") and callable(mod.enrich_totals_pick_with_xg)
        assert hasattr(mod, "_is_totals_pick") and callable(mod._is_totals_pick)
        assert hasattr(mod, "get_team_xg_profile") and callable(mod.get_team_xg_profile)

    def test_is_totals_pick_classifier(self):
        from sportdb_xg_totals import _is_totals_pick
        assert _is_totals_pick({"market": "Totals", "selection": "Over 2.5"}) is True
        assert _is_totals_pick({"market": "Match Total", "selection": "Under 2.5"}) is True
        assert _is_totals_pick({"market": "Anytime Goal Scorer", "selection": "Mbappe"}) is False
        assert _is_totals_pick({"market": "h2h", "selection": "Home"}) is False


# ─────────────────── /api/picks/today perf + shape ───────────────────

class TestPicksTodayPerformance:
    def test_picks_today_under_2s_full(self, session, auth_headers):
        # Prime caches with a first hit (counts auth+DB warmup)
        session.get(f"{BASE_URL}/api/picks/today", headers=auth_headers, timeout=10)
        start = time.time()
        r = session.get(f"{BASE_URL}/api/picks/today", headers=auth_headers, timeout=10)
        elapsed = time.time() - start
        assert r.status_code == 200, f"status {r.status_code}: {r.text[:200]}"
        assert elapsed < 2.0, f"picks/today took {elapsed:.2f}s (>2s budget)"
        data = _picks_list(r.json())
        assert len(data) > 0, "empty slate"

    def test_picks_today_under_2s_lite(self, session, auth_headers):
        session.get(f"{BASE_URL}/api/picks/today?lite=true", headers=auth_headers, timeout=10)
        start = time.time()
        r = session.get(f"{BASE_URL}/api/picks/today?lite=true", headers=auth_headers, timeout=10)
        elapsed = time.time() - start
        assert r.status_code == 200
        assert elapsed < 2.0, f"picks/today?lite=true took {elapsed:.2f}s (>2s budget)"
        body = _picks_list(r.json())
        assert len(body) > 0

    def test_picks_today_no_mongo_objectid(self, session, auth_headers):
        """Regression: ensure _id is never leaked."""
        r = session.get(f"{BASE_URL}/api/picks/today?lite=true", headers=auth_headers, timeout=10)
        assert r.status_code == 200
        for p in _picks_list(r.json())[:25]:
            assert "_id" not in p, f"Mongo _id leaked in pick {p.get('id')}"


# ─────────────────── Admin endpoints auth-gating ───────────────────

class TestAdminAuthGating:
    def test_odds_diagnostic_requires_auth(self, session):
        r = session.get(f"{BASE_URL}/api/admin/odds-diagnostic", timeout=10)
        # 401 (no header) or 403 (no role) — both indicate auth gate works
        assert r.status_code in (401, 403), f"got {r.status_code}: {r.text[:200]}"

    def test_picks_heal_requires_auth(self, session):
        r = session.post(f"{BASE_URL}/api/admin/picks/heal", timeout=10)
        assert r.status_code in (401, 403, 405)  # 405 if GET-only — still gated

    def test_odds_circuit_reset_requires_auth(self, session):
        r = session.post(f"{BASE_URL}/api/admin/odds-circuit/reset", timeout=10)
        assert r.status_code in (401, 403, 405)

    def test_odds_diagnostic_with_demo_user_is_403(self, session, demo_token):
        if not demo_token:
            pytest.skip("no demo token")
        r = session.get(
            f"{BASE_URL}/api/admin/odds-diagnostic",
            headers={"Authorization": f"Bearer {demo_token}"},
            timeout=10,
        )
        # demo user is non-admin → must be 403
        assert r.status_code == 403, f"demo user got {r.status_code}, expected 403"


# ─────────────────── Regression: existing flows ───────────────────

class TestRegression:
    def test_demo_login_still_works(self, demo_token):
        assert demo_token, "demo login produced no access_token"

    def test_pick_detail_enriched(self, session, auth_headers):
        r = session.get(f"{BASE_URL}/api/picks/today?lite=true", headers=auth_headers, timeout=10)
        assert r.status_code == 200
        picks = _picks_list(r.json())
        assert picks, "no picks to detail-test"
        pick_id = picks[0].get("id") or picks[0].get("pick_id")
        assert pick_id, "no pick id on lite payload"
        detail = session.get(f"{BASE_URL}/api/picks/{pick_id}", headers=auth_headers, timeout=10)
        assert detail.status_code == 200, f"pick detail failed: {detail.status_code} {detail.text[:200]}"
        body = detail.json()
        assert any(k in body for k in ("lock_score", "lock_score_v2", "win_probability")), (
            "pick detail missing core lock/probability fields"
        )

    def test_picks_today_no_xg_crash_log(self, session, auth_headers):
        """xG enrichment must NEVER null out the slate."""
        for q in ("", "?lite=true", "?min_lock=70", "?market=moneyline"):
            r = session.get(f"{BASE_URL}/api/picks/today{q}", headers=auth_headers, timeout=10)
            assert r.status_code == 200, f"{q} → {r.status_code}"
            # accept list OR {"picks": [...]} payload shape
            picks = _picks_list(r.json())
            assert isinstance(picks, list)

    def test_picks_all_endpoint_still_ok(self, session, auth_headers):
        r = session.get(f"{BASE_URL}/api/picks/all", headers=auth_headers, timeout=10)
        assert r.status_code == 200
        assert isinstance(_picks_list(r.json()), list)


# ─────────────────── Synthetic scorer signature surface ───────────────

class TestSyntheticScorerInSlate:
    """If any synthetic scorer picks made it into today's slate, verify
    they carry the documented tags. Slate may have zero synth picks on a
    given day — that's OK; we skip then."""

    def test_synth_picks_carry_documented_tags(self, session, auth_headers):
        r = session.get(f"{BASE_URL}/api/picks/today", headers=auth_headers, timeout=10)
        assert r.status_code == 200
        picks = _picks_list(r.json())
        synth = [p for p in picks if p.get("is_synthetic_scorer") or p.get("source") == "sportdb_scorer_v1"]
        if not synth:
            pytest.skip("no synthetic scorer picks on today's slate")
        for p in synth[:5]:
            assert p.get("is_model_only") is True, "synth pick missing is_model_only"
            assert p.get("market", "").lower().startswith("anytime goal scorer"), \
                f"unexpected market for synth pick: {p.get('market')}"
            assert isinstance(p.get("lock_score"), (int, float))
            # Synthetic lock score capped at 88 per design
            assert p["lock_score"] <= 88.0, f"synth lock_score {p['lock_score']} exceeds documented cap of 88"
            assert "sportdb_signal" in p, "synth pick missing sportdb_signal"
