"""Backend regression tests for the slate-wide Signal Score percentile
rank fix (iter 76, 2026-07-17).

Original bug: "signal filter is there just no picks" — /picks/today
$gte on `signal_score` silently dropped all picks missing the field
(~65% of the slate). Fix: `refresh_slate_signal_rank` now backfills
every pick and re-maps scores to a slate-wide percentile 0-100 at the
top of the handler, and the query treats missing-field picks as
neutral (50) when the threshold is ≤50.

These tests hit the PUBLIC preview URL to mirror what the user sees.
"""
from __future__ import annotations

import os
import time
import pytest
import requests

BASE_URL = os.environ.get(
    "EXPO_BACKEND_URL",
    "https://bet-edge-ai-1.preview.emergentagent.com",
).rstrip("/")

DEMO_EMAIL = "demo@lockscore.ai"
DEMO_PASSWORD = "demo123"

TIMEOUT = 30  # /picks/today can take up to 3s per the spec


# ─── shared fixtures ────────────────────────────────────────────────

@pytest.fixture(scope="module")
def token() -> str:
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD},
        timeout=TIMEOUT,
    )
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:300]}"
    body = r.json()
    assert "access_token" in body
    return body["access_token"]


@pytest.fixture(scope="module")
def auth_headers(token) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _get_picks(auth_headers: dict, **params) -> list[dict]:
    """GET /picks/today and normalise the response to a plain list."""
    r = requests.get(
        f"{BASE_URL}/api/picks/today",
        headers=auth_headers,
        params=params,
        timeout=TIMEOUT,
    )
    assert r.status_code == 200, f"/picks/today {params} -> {r.status_code}: {r.text[:400]}"
    data = r.json()
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "picks" in data:
        return data["picks"]
    pytest.fail(f"unexpected response shape: {type(data).__name__}")


# ─── /picks/today?min_signal=* threshold coverage ───────────────────

class TestSignalThresholdBands:
    """Each slider position should return a monotonically-decreasing count
    that matches the buckets in the review request.
    """

    def test_min_signal_0_returns_full_slate(self, auth_headers):
        picks = _get_picks(auth_headers, min_signal=0, lite="true")
        assert len(picks) >= 200, (
            f"min_signal=0 should return the full slate (~230+); got {len(picks)}"
        )

    def test_min_signal_50_neutral_threshold(self, auth_headers):
        picks = _get_picks(auth_headers, min_signal=50, lite="true")
        # Spec: ~170. Allow a generous band around it (data drifts).
        assert 100 <= len(picks) <= 230, (
            f"min_signal=50 expected ~170, got {len(picks)}"
        )

    def test_min_signal_70_strong_plus(self, auth_headers):
        picks = _get_picks(auth_headers, min_signal=70, lite="true")
        assert 50 <= len(picks) <= 180, (
            f"min_signal=70 expected ~120, got {len(picks)}"
        )

    def test_min_signal_90_elite_nonzero(self, auth_headers):
        """The KEY regression: before the fix this was 0. Must be >0 now."""
        picks = _get_picks(auth_headers, min_signal=90, lite="true")
        assert len(picks) > 0, (
            "REGRESSION: min_signal=90 returned 0 picks — the bug is back."
        )
        assert 10 <= len(picks) <= 100, (
            f"min_signal=90 expected ~30-40, got {len(picks)}"
        )

    def test_min_signal_95_top_5_percent(self, auth_headers):
        picks = _get_picks(auth_headers, min_signal=95, lite="true")
        assert len(picks) > 0, "min_signal=95 should still surface top picks"
        assert 5 <= len(picks) <= 60, (
            f"min_signal=95 expected ~20, got {len(picks)}"
        )

    def test_thresholds_monotonically_decreasing(self, auth_headers):
        """Higher threshold => fewer or equal picks."""
        counts = {}
        for t in (0, 50, 70, 90, 95):
            counts[t] = len(_get_picks(auth_headers, min_signal=t, lite="true"))
        assert counts[0] >= counts[50] >= counts[70] >= counts[90] >= counts[95], (
            f"non-monotonic threshold counts: {counts}"
        )


# ─── field-coverage assertions ──────────────────────────────────────

class TestSignalScoreFieldCoverage:

    def test_every_pick_has_signal_score_and_raw(self, auth_headers):
        picks = _get_picks(auth_headers, min_signal=0)  # full (non-lite) response
        assert len(picks) > 0
        missing_score = [p.get("id") for p in picks if p.get("signal_score") is None]
        missing_raw = [p.get("id") for p in picks if p.get("signal_score_raw") is None]
        assert not missing_score, (
            f"{len(missing_score)}/{len(picks)} picks missing signal_score "
            f"(sample: {missing_score[:5]})"
        )
        assert not missing_raw, (
            f"{len(missing_raw)}/{len(picks)} picks missing signal_score_raw "
            f"(sample: {missing_raw[:5]})"
        )

    def test_signal_score_within_0_100(self, auth_headers):
        picks = _get_picks(auth_headers, min_signal=0)
        bad = [(p.get("id"), p.get("signal_score")) for p in picks
               if not (isinstance(p.get("signal_score"), (int, float))
                       and 0 <= p["signal_score"] <= 100)]
        assert not bad, f"signal_score out of range: {bad[:5]}"

    def test_high_threshold_scores_actually_high(self, auth_headers):
        picks = _get_picks(auth_headers, min_signal=90)
        assert len(picks) > 0
        below = [(p.get("id"), p.get("signal_score")) for p in picks
                 if p.get("signal_score", 0) < 90]
        assert not below, f"min_signal=90 returned picks with score<90: {below[:5]}"


# ─── admin refresh endpoint ─────────────────────────────────────────

class TestSignalRankRefreshEndpoint:

    def test_refresh_returns_expected_shape(self, auth_headers):
        r = requests.post(
            f"{BASE_URL}/api/picks/signal-rank/refresh",
            headers=auth_headers,
            timeout=60,
        )
        assert r.status_code == 200, f"refresh failed: {r.status_code} {r.text[:300]}"
        body = r.json()
        assert body.get("ok") is True, f"ok flag missing: {body}"
        # cached=False path returns n_total/bands; cached=True path is
        # short-circuit. Either is valid — force=True bypasses cache so
        # we expect the full shape.
        assert "n_total" in body, f"n_total missing: {body}"
        assert body["n_total"] > 0, f"n_total should be > 0: {body}"
        assert "n_persisted" in body, f"n_persisted missing: {body}"
        assert "bands" in body, f"bands missing: {body}"
        bands = body["bands"]
        for key in ("90+", "75+", "50+", "25+"):
            assert key in bands, f"band {key} missing: {bands}"
        # Sanity: bands should be monotonically decreasing.
        assert bands["25+"] >= bands["50+"] >= bands["75+"] >= bands["90+"], bands


# ─── combined filters ───────────────────────────────────────────────

class TestCombinedFilters:

    def test_sport_mlb_plus_min_signal_70(self, auth_headers):
        picks = _get_picks(auth_headers, sport="MLB", min_signal=70, lite="true")
        # It's OK for this to be 0 if MLB slate is thin — but every
        # returned pick MUST honour both filters.
        for p in picks[:20]:
            assert (p.get("sport") or "").upper() == "MLB", (
                f"non-MLB pick in MLB filter: {p.get('id')} sport={p.get('sport')}"
            )
            assert p.get("signal_score", 0) >= 70, (
                f"pick below signal threshold: {p.get('id')} score={p.get('signal_score')}"
            )

    def test_min_lock_90_plus_min_signal_90_double_filter(self, auth_headers):
        picks = _get_picks(auth_headers, min_lock=90, min_signal=90, lite="true")
        # This is the strict "elite" bucket — may legitimately be small
        # or empty. What we care about is: no error, and any returned
        # picks honour BOTH thresholds.
        for p in picks[:20]:
            assert p.get("signal_score", 0) >= 90, (
                f"pick below signal threshold: {p.get('id')} score={p.get('signal_score')}"
            )
            lock = p.get("lock_score") or p.get("lock_score_v2") or 0
            assert lock >= 90, (
                f"pick below lock threshold: {p.get('id')} lock={lock}"
            )


# ─── detail endpoint ────────────────────────────────────────────────

class TestPickDetailSignalBlock:

    def test_detail_returns_signal_score_and_engine_block(self, auth_headers):
        picks = _get_picks(auth_headers, min_signal=70, lite="true")
        assert len(picks) > 0, "need at least one pick to inspect detail"
        pid = picks[0]["id"]
        r = requests.get(
            f"{BASE_URL}/api/picks/{pid}",
            headers=auth_headers,
            timeout=TIMEOUT,
        )
        assert r.status_code == 200, f"detail failed: {r.status_code} {r.text[:300]}"
        body = r.json()
        assert body.get("signal_score") is not None, (
            f"detail missing signal_score for {pid}: keys={list(body.keys())[:20]}"
        )
        assert 0 <= body["signal_score"] <= 100
        # signal_engine block is expected but might be absent on older
        # docs — soft assert with a clear message.
        se = body.get("signal_engine")
        assert isinstance(se, dict), (
            f"detail missing signal_engine block for {pid}: "
            f"type={type(se).__name__}"
        )


# ─── latency budget ─────────────────────────────────────────────────

class TestLatency:

    def test_picks_today_with_min_signal_under_3s(self, auth_headers):
        # Warm the TTL cache first (first hit may include the rank sweep).
        _get_picks(auth_headers, min_signal=70, lite="true")
        t0 = time.time()
        picks = _get_picks(auth_headers, min_signal=70, lite="true")
        elapsed = time.time() - t0
        assert len(picks) > 0
        assert elapsed < 3.0, (
            f"/picks/today?min_signal=70 took {elapsed:.2f}s (>3s budget)"
        )
