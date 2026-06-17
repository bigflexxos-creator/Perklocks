"""Smoke tests — structural assertions only.

These tests verify the SHAPE of responses (keys present, types correct,
required fields non-empty) — they DO NOT assert specific values like
lock_score == 99 or "Kane in feed". That's intentional: live odds shift
every minute and value assertions would create flaky tests.

Goal: catch regressions that break the API contract or remove
critical fields, without re-failing every time a player gets benched.

Run with:
    cd /app/backend && python -m pytest tests/test_smoke.py -v
"""
from __future__ import annotations

import os
import sys
import pytest
import asyncio
import httpx

# Allow running as `pytest tests/...` from /app/backend
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

BASE = "http://localhost:8001"
DEMO_EMAIL = "demo@lockscore.ai"
DEMO_PW = "demo123"


@pytest.fixture(scope="module")
def token() -> str:
    """Login once per test module and reuse the JWT."""
    with httpx.Client(base_url=BASE, timeout=10.0) as cli:
        r = cli.post("/api/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PW})
        assert r.status_code == 200, f"Login failed: {r.text}"
        data = r.json()
        assert "access_token" in data, "Login response missing access_token"
        return data["access_token"]


@pytest.fixture(scope="module")
def auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _has(d: dict, *keys) -> bool:
    return all(k in d for k in keys)


# ──────────────────────────────────────────────────────────────────────
# Auth contract
# ──────────────────────────────────────────────────────────────────────

def test_auth_me_returns_user_shape(auth_header):
    with httpx.Client(base_url=BASE, timeout=10.0) as cli:
        r = cli.get("/api/auth/me", headers=auth_header)
        assert r.status_code == 200
        u = r.json()
        assert _has(u, "id", "email")


# ──────────────────────────────────────────────────────────────────────
# Picks/today — most critical user-facing endpoint
# ──────────────────────────────────────────────────────────────────────

def test_picks_today_returns_picks_array(auth_header):
    with httpx.Client(base_url=BASE, timeout=15.0) as cli:
        r = cli.get("/api/picks/today", headers=auth_header)
        assert r.status_code == 200
        data = r.json()
        # Either {"picks": [...]} or [...] — handle both
        picks = data if isinstance(data, list) else data.get("picks", [])
        assert isinstance(picks, list)


def test_picks_today_pick_has_required_fields(auth_header):
    """Every pick MUST have these fields for the UI to render correctly."""
    REQUIRED = ("id", "sport", "event", "market", "book_odds",
                "lock_score", "edge_percent", "win_probability")
    with httpx.Client(base_url=BASE, timeout=15.0) as cli:
        r = cli.get("/api/picks/today", headers=auth_header)
        picks = r.json() if isinstance(r.json(), list) else r.json().get("picks", [])
        if not picks:
            pytest.skip("No picks today — skipping field-shape check")
        for p in picks[:5]:
            for k in REQUIRED:
                assert k in p, f"Pick missing required field '{k}': {p}"


def test_picks_today_sort_param_accepted(auth_header):
    """All 4 sort modes must succeed without 500."""
    with httpx.Client(base_url=BASE, timeout=15.0) as cli:
        for sort in ("lock", "time", "edge", "implied"):
            r = cli.get(f"/api/picks/today?sort={sort}", headers=auth_header)
            assert r.status_code == 200, f"sort={sort} failed: {r.text[:200]}"


def test_picks_today_invalid_sport_does_not_crash(auth_header):
    """Bad sport param should return empty list, not 500."""
    with httpx.Client(base_url=BASE, timeout=15.0) as cli:
        r = cli.get("/api/picks/today?sport=Quidditch", headers=auth_header)
        assert r.status_code in (200, 400)


# ──────────────────────────────────────────────────────────────────────
# Parlay generator
# ──────────────────────────────────────────────────────────────────────

def test_parlay_returns_top3_shape(auth_header):
    with httpx.Client(base_url=BASE, timeout=20.0) as cli:
        r = cli.get("/api/picks/parlay?legs=3&mode=standard", headers=auth_header)
        assert r.status_code == 200
        data = r.json()
        assert "parlays" in data, "Parlay response missing 'parlays' array"
        assert isinstance(data["parlays"], list)
        # Should have up to 3 cards if there's enough data
        for card in data["parlays"]:
            REQUIRED_CARD = ("label", "grade", "strength_score", "legs",
                            "survival_pct", "avg_edge_pct", "leg_count",
                            "combined_american_odds", "reasons")
            for k in REQUIRED_CARD:
                assert k in card, f"Parlay card missing '{k}': keys={list(card.keys())}"
            assert card["label"] in ("SAFE", "BALANCED", "AGGRESSIVE")
            assert card["grade"] in ("A", "B", "C", "D", "F")


def test_parlay_high_risk_accepts_15_legs(auth_header):
    with httpx.Client(base_url=BASE, timeout=20.0) as cli:
        r = cli.get("/api/picks/parlay?legs=15&mode=high_risk&window_hours=168",
                   headers=auth_header)
        assert r.status_code == 200


def test_parlay_invalid_params_clamp(auth_header):
    """Negative / huge legs must NOT crash the optimizer."""
    with httpx.Client(base_url=BASE, timeout=20.0) as cli:
        for bad in ("-5", "0", "999"):
            r = cli.get(f"/api/picks/parlay?legs={bad}&mode=standard",
                       headers=auth_header)
            assert r.status_code == 200, f"legs={bad} crashed: {r.text[:200]}"


def test_parlay_refresh_cursor(auth_header):
    """Rank=1 and rank=2 should both return without error."""
    with httpx.Client(base_url=BASE, timeout=20.0) as cli:
        r1 = cli.get("/api/picks/parlay?legs=3&rank=1", headers=auth_header)
        r2 = cli.get("/api/picks/parlay?legs=3&rank=2", headers=auth_header)
        assert r1.status_code == 200 and r2.status_code == 200


# ──────────────────────────────────────────────────────────────────────
# Analytics
# ──────────────────────────────────────────────────────────────────────

def test_analytics_v2_shape(auth_header):
    with httpx.Client(base_url=BASE, timeout=20.0) as cli:
        r = cli.get("/api/analytics/v2", headers=auth_header)
        assert r.status_code == 200
        data = r.json()
        # We just check the response is a dict / list — schema may evolve
        assert isinstance(data, (dict, list))


def test_analytics_v2_has_no_wnba(auth_header):
    """Regression guard — WNBA was permanently removed. If it returns,
    something has gone seriously wrong in the analytics pipeline."""
    import json
    with httpx.Client(base_url=BASE, timeout=20.0) as cli:
        r = cli.get("/api/analytics/v2", headers=auth_header)
        assert r.status_code == 200
        flat = json.dumps(r.json()).upper()
        assert "WNBA" not in flat, "WNBA found in /api/analytics/v2 — should be permanently removed"


# ──────────────────────────────────────────────────────────────────────
# Stats summary
# ──────────────────────────────────────────────────────────────────────

def test_stats_summary_shape(auth_header):
    REQUIRED = ("date", "total_picks", "elite_count", "by_sport")
    with httpx.Client(base_url=BASE, timeout=10.0) as cli:
        r = cli.get("/api/stats/summary", headers=auth_header)
        assert r.status_code == 200
        d = r.json()
        for k in REQUIRED:
            assert k in d, f"stats/summary missing '{k}': {list(d.keys())}"


# ──────────────────────────────────────────────────────────────────────
# Reliability layer — request ID middleware
# ──────────────────────────────────────────────────────────────────────

def test_response_includes_request_id_header(auth_header):
    with httpx.Client(base_url=BASE, timeout=10.0) as cli:
        r = cli.get("/api/auth/me", headers=auth_header)
        assert r.status_code == 200
        # ReliabilityMiddleware adds X-Request-ID on every response
        assert "x-request-id" in {k.lower() for k in r.headers.keys()}, \
            "ReliabilityMiddleware not adding X-Request-ID header"
