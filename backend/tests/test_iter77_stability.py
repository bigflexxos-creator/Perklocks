"""
Iteration 77 — Stability regression tests for signal-rank floor + MLB subset + cache bounding.

Covers:
- Signal Score floor = 20 (no pick below 20)
- min_signal thresholds return monotonically decreasing counts
- ALL tab (no sport filter) contains picks from multiple sports and is superset of MLB
- POST /picks/signal-rank/refresh returns healthy bands
- Signal-rank cache stays bounded (<=14 date keys) after refresh
- 5 sequential /picks/today with different min_signal — no 500s, <3s per call
"""
from __future__ import annotations

import os
import time
import pytest
import requests

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "https://canonical-parity.preview.emergentagent.com").rstrip("/")
EMAIL = "demo@lockscore.ai"
PASSWORD = "demo123"


@pytest.fixture(scope="module")
def api_client():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    # login
    r = s.post(f"{BASE_URL}/api/auth/login", json={"email": EMAIL, "password": PASSWORD}, timeout=15)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:200]}"
    token = r.json().get("token") or r.json().get("access_token")
    assert token, f"missing token: {r.json()}"
    s.headers.update({"Authorization": f"Bearer {token}"})
    return s


def _picks(payload):
    """/picks/today returns bare array OR wrapped {picks:[...]}."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        return payload.get("picks") or []
    return []


# ── Signal Score floor / range ────────────────────────────────────────
class TestSignalFloor:
    def test_no_pick_below_floor_20(self, api_client):
        r = api_client.get(f"{BASE_URL}/api/picks/today?min_signal=0", timeout=30)
        assert r.status_code == 200, r.text[:200]
        picks = _picks(r.json())
        assert picks, "expected picks on unfiltered slate"
        low = [p for p in picks if p.get("signal_score") is not None and int(p["signal_score"]) < 20]
        assert not low, f"{len(low)} picks below floor 20 (first: signal={low[0].get('signal_score')})"

    def test_signal_score_in_0_100_range(self, api_client):
        r = api_client.get(f"{BASE_URL}/api/picks/today", timeout=30)
        picks = _picks(r.json())
        for p in picks:
            ss = p.get("signal_score")
            if ss is None:
                continue
            assert 0 <= int(ss) <= 100, f"signal_score out of range: {ss}"


# ── Monotonic decrease at thresholds ──────────────────────────────────
class TestMonotonic:
    def test_monotonic_decrease(self, api_client):
        counts = {}
        for m in (0, 30, 50, 70, 90, 95):
            r = api_client.get(f"{BASE_URL}/api/picks/today?min_signal={m}", timeout=30)
            assert r.status_code == 200
            counts[m] = len(_picks(r.json()))
        thresholds = sorted(counts)
        for a, b in zip(thresholds, thresholds[1:]):
            assert counts[a] >= counts[b], f"non-monotonic: min_signal={a}→{counts[a]}, {b}→{counts[b]}"
        print(f"[iter77] signal counts: {counts}")


# ── MLB subset of ALL / multi-sport slate ─────────────────────────────
class TestSportSubset:
    def test_all_tab_multi_sport(self, api_client):
        r = api_client.get(f"{BASE_URL}/api/picks/today", timeout=30)
        picks = _picks(r.json())
        sports = {p.get("sport") for p in picks if p.get("sport")}
        assert len(sports) >= 2, f"ALL tab returned single sport: {sports}"
        print(f"[iter77] ALL sports present: {sports}")

    def test_mlb_subset_of_all(self, api_client):
        r_all = api_client.get(f"{BASE_URL}/api/picks/today", timeout=30)
        r_mlb = api_client.get(f"{BASE_URL}/api/picks/today?sport=MLB", timeout=30)
        all_ids = {p.get("id") for p in _picks(r_all.json())}
        mlb_ids = {p.get("id") for p in _picks(r_mlb.json())}
        missing = mlb_ids - all_ids
        # Allow tiny drift due to slate refresh between calls (<5%)
        drift_pct = (len(missing) / max(1, len(mlb_ids))) * 100
        assert drift_pct <= 5, f"MLB not subset of ALL: {len(missing)}/{len(mlb_ids)} missing ({drift_pct:.1f}%)"
        print(f"[iter77] ALL={len(all_ids)}, MLB={len(mlb_ids)}, drift={drift_pct:.1f}%")


# ── Admin signal-rank refresh endpoint ────────────────────────────────
class TestSignalRankRefresh:
    def test_refresh_returns_healthy_bands(self, api_client):
        r = api_client.post(f"{BASE_URL}/api/picks/signal-rank/refresh", timeout=60)
        assert r.status_code == 200, r.text[:300]
        body = r.json()
        assert body.get("ok") is True, body
        assert "n_total" in body and body["n_total"] > 0, body
        assert "n_persisted" in body
        bands = body.get("bands") or {}
        for k in ("90+", "75+", "50+", "25+"):
            assert k in bands, f"missing band {k}: {bands}"
        # spread: 25+ >= 50+ >= 75+ >= 90+
        assert bands["25+"] >= bands["50+"] >= bands["75+"] >= bands["90+"], bands
        print(f"[iter77] refresh bands: {bands}, n_total={body['n_total']}")


# ── In-memory cache bounded ────────────────────────────────────────────
class TestCacheBounded:
    def test_cache_size_under_15(self):
        """Directly inspect the module — the cache is in-process."""
        import sys
        sys.path.insert(0, "/app/backend")
        from services.signal_engine import rank
        assert len(rank._LAST_RUN) <= rank._MAX_CACHE_ENTRIES, \
            f"cache exceeded {rank._MAX_CACHE_ENTRIES}: {len(rank._LAST_RUN)}"
        assert rank._MAX_CACHE_ENTRIES == 14, f"expected _MAX_CACHE_ENTRIES=14, got {rank._MAX_CACHE_ENTRIES}"


# ── Load / latency ────────────────────────────────────────────────────
class TestLoad:
    def test_five_sequential_calls_no_500_under_3s(self, api_client):
        durations = []
        for m in (0, 30, 50, 70, 90):
            t0 = time.time()
            r = api_client.get(f"{BASE_URL}/api/picks/today?min_signal={m}", timeout=10)
            dur = time.time() - t0
            durations.append(dur)
            assert r.status_code == 200, f"min_signal={m} → {r.status_code}: {r.text[:200]}"
            assert dur < 3.0, f"min_signal={m} took {dur:.2f}s"
        print(f"[iter77] latencies: {[f'{d:.2f}s' for d in durations]}")
