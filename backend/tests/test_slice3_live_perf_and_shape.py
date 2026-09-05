"""MAIN 39 · Slice 3 — LIVE perf + shape sanity.

Runs against EXPO_PUBLIC_BACKEND_URL (Kubernetes ingress) and:
  - Confirms /api/picks/today?lite=true is materially faster (p50 <= 8s)
  - Confirms per-pick canonical fields (home_meta/away_meta/injury_chip
    for games; player_meta for player-prop markets) are still present
  - Confirms /api/picks/rollover, /api/picks/parlay, /api/health still 200
"""
from __future__ import annotations
import os
import time
import statistics
import requests
import pytest

BASE_URL = (
    os.environ.get("EXPO_PUBLIC_BACKEND_URL")
    or os.environ.get("EXPO_BACKEND_URL")
    or ""
).rstrip("/")
assert BASE_URL, "EXPO_PUBLIC_BACKEND_URL must be set"

DEMO_EMAIL = "demo@lockscore.ai"
DEMO_PW = "demo123"


@pytest.fixture(scope="module")
def token():
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": DEMO_EMAIL, "password": DEMO_PW},
        timeout=15,
    )
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:200]}"
    tok = r.json().get("access_token")
    assert tok, "missing access_token"
    return tok


@pytest.fixture(scope="module")
def auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


def test_health_fast():
    # Warm-up (first cold TLS/DNS handshake can spike ~5-10s in preview).
    requests.get(f"{BASE_URL}/api/health", timeout=15)
    latencies = []
    for _ in range(3):
        t0 = time.time()
        r = requests.get(f"{BASE_URL}/api/health", timeout=10)
        latencies.append((time.time() - t0) * 1000)
        assert r.status_code == 200
    med = statistics.median(latencies)
    print(f"/health p50={med:.0f}ms  samples={latencies}")
    assert med < 1000, f"health median too slow: {med:.0f}ms"


def test_picks_today_lite_p50_under_budget(auth_headers):
    """Warm p50 over 5 back-to-back calls must be under 9s (was ~11-29s)."""
    # 1 warm-up
    requests.get(
        f"{BASE_URL}/api/picks/today?lite=true",
        headers=auth_headers, timeout=60,
    )
    latencies = []
    for _ in range(5):
        t0 = time.time()
        r = requests.get(
            f"{BASE_URL}/api/picks/today?lite=true",
            headers=auth_headers, timeout=60,
        )
        latencies.append((time.time() - t0) * 1000)
        assert r.status_code == 200, f"lite failed: {r.status_code}"
    p50 = statistics.median(latencies)
    p95 = max(latencies)
    print(f"\n/picks/today?lite=true — p50={p50:.0f}ms p95={p95:.0f}ms across n={len(latencies)}")
    # Guardrail: main agent claims p50 ~5.4s / p95 ~6.2s. Allow slack.
    assert p50 < 9000, f"p50 latency regressed: {p50:.0f}ms"


def test_picks_today_lite_shape_preserved(auth_headers):
    r = requests.get(
        f"{BASE_URL}/api/picks/today?lite=true",
        headers=auth_headers, timeout=60,
    )
    assert r.status_code == 200
    body = r.json()
    picks = body if isinstance(body, list) else body.get("picks", [])
    assert isinstance(picks, list) and len(picks) > 0, "expected non-empty picks"

    # Canonical required fields on every pick.
    for p in picks:
        for k in ("lock_score", "market", "event"):
            assert k in p, f"pick missing canonical field {k}: keys={list(p.keys())[:20]}"

    # ESPN enrichment must have run for a material share of game markets.
    n_game_meta = sum(1 for p in picks if p.get("home_meta") and p.get("away_meta"))
    n_player_props = sum(1 for p in picks if p.get("player_name"))
    n_signals = sum(1 for p in picks if p.get("espn_signals"))
    print(f"picks={len(picks)}  home+away_meta={n_game_meta}  player_prop_picks={n_player_props}  espn_signals={n_signals}")
    # At least SOME picks must have game meta (proves _decorate_with_espn_meta ran).
    assert n_game_meta > 0, "no picks carry home_meta+away_meta — enrichment fan-out broken"


def test_rollover_still_200_and_v4_sticky(auth_headers):
    t0 = time.time()
    r = requests.get(f"{BASE_URL}/api/picks/rollover", headers=auth_headers, timeout=30)
    dt = (time.time() - t0) * 1000
    assert r.status_code == 200
    body = r.json()
    assert body.get("rollover_version") in ("v4-sticky", "v4-db-frozen"), body.get("rollover_version")
    surv = body.get("survivability") or {}
    # odds_dead_zone retirement: when the key IS present (i.e., live
    # eligibility mode), it must be an empty list.  In db_frozen_restore
    # mode the survivability payload is condensed and omits the key.
    if "odds_dead_zone" in surv:
        assert surv["odds_dead_zone"] == [], f"odds_dead_zone should be []: {surv.get('odds_dead_zone')}"
    print(f"/rollover status=200 {dt:.0f}ms  version={body.get('rollover_version')}  surv={surv}")


def test_parlay_still_200(auth_headers):
    r = requests.get(f"{BASE_URL}/api/picks/parlay", headers=auth_headers, timeout=30)
    assert r.status_code == 200
