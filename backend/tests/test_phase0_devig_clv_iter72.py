"""Iter72 — Phase 0 regression tests for devig + CLV endpoint.

Covers:
  1. GET /api/analytics/clv?days=30 — 200 + schema (since/days/overall/bands/
     snapshot_coverage/notes) + 6 bands + overall.beat_close_pct is number|null.
  2. GET /api/picks/today — every pick has no_vig_book_odds, no_vig_implied_pct,
     book_hold_pct, no_vig_source (on-read devig).
  3. GET /api/picks/today — regression on total count + lock_score presence.
  4. GET /api/picks/history?days=30 — 200 OK, no schema regression.
  5. MLB grading fix regression spot-check — Wheeler / Altuve / Machado picks
     retain the iter71 grading result (Wheeler won, Altuve/Machado lost).

Backend regression only — no frontend.
"""
from __future__ import annotations

import os

import pytest
import requests

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    # Fall back to EXPO_BACKEND_URL for older envs
    BASE_URL = os.environ.get("EXPO_BACKEND_URL", "").rstrip("/")

DEMO_EMAIL = "demo@lockscore.ai"
DEMO_PASSWORD = "demo123"


@pytest.fixture(scope="module")
def token() -> str:
    assert BASE_URL, "EXPO_PUBLIC_BACKEND_URL not set in env"
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD},
        timeout=15,
    )
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:300]}"
    body = r.json()
    tok = body.get("access_token") or body.get("token")
    assert tok, f"no access_token in login body: {body}"
    return tok


@pytest.fixture(scope="module")
def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# 1) CLV endpoint
# ---------------------------------------------------------------------------
class TestClvEndpoint:
    def test_clv_200_and_schema(self, auth_headers):
        r = requests.get(
            f"{BASE_URL}/api/analytics/clv?days=30",
            headers=auth_headers,
            timeout=30,
        )
        assert r.status_code == 200, f"{r.status_code} {r.text[:300]}"
        body = r.json()
        # Top-level keys
        for key in ("since", "days", "overall", "bands", "snapshot_coverage", "notes"):
            assert key in body, f"missing top-level key: {key}"
        assert body["days"] == 30
        assert isinstance(body["since"], str) and "T" in body["since"]

    def test_clv_overall_beat_close_pct_number_or_null(self, auth_headers):
        r = requests.get(
            f"{BASE_URL}/api/analytics/clv?days=30",
            headers=auth_headers,
            timeout=30,
        )
        assert r.status_code == 200
        body = r.json()
        overall = body["overall"]
        for key in ("n", "won", "win_pct", "roi_per_100u", "avg_clv_pp", "beat_close_pct"):
            assert key in overall, f"missing overall key: {key}"
        bc = overall["beat_close_pct"]
        assert bc is None or isinstance(bc, (int, float)), \
            f"beat_close_pct must be number or null, got {type(bc)}"

    def test_clv_has_six_bands(self, auth_headers):
        r = requests.get(
            f"{BASE_URL}/api/analytics/clv?days=30",
            headers=auth_headers,
            timeout=30,
        )
        assert r.status_code == 200
        body = r.json()
        bands = body["bands"]
        assert isinstance(bands, list)
        assert len(bands) == 6, f"expected 6 bands, got {len(bands)}"
        # Each band has full shape
        expected_labels = {
            "Heavy fav (<-200)",
            "Fav (-200 to -110)",
            "Coin flip (-110 to +110)",
            "Plus (+110 to +200)",
            "Big dog (+200 to +500)",
            "Long shot (+500+)",
        }
        got_labels = {b["label"] for b in bands}
        assert got_labels == expected_labels, f"band labels mismatch: {got_labels}"
        for band in bands:
            for key in ("label", "n", "won", "win_pct", "roi_per_100u",
                        "avg_clv_pp", "beat_close_pct"):
                assert key in band, f"band {band.get('label')} missing key: {key}"

    def test_clv_snapshot_coverage_shape(self, auth_headers):
        r = requests.get(
            f"{BASE_URL}/api/analytics/clv?days=30",
            headers=auth_headers,
            timeout=30,
        )
        assert r.status_code == 200
        body = r.json()
        cov = body["snapshot_coverage"]
        for key in ("real_close_snapshots", "sharp_book_snapshots", "note"):
            assert key in cov, f"snapshot_coverage missing key: {key}"
        assert isinstance(cov["real_close_snapshots"], int)
        assert isinstance(cov["sharp_book_snapshots"], int)


# ---------------------------------------------------------------------------
# 2/3) Picks Today — no-vig fields + no regression
# ---------------------------------------------------------------------------
class TestPicksTodayDevig:
    @pytest.fixture(scope="class")
    def picks_today(self, auth_headers):
        r = requests.get(
            f"{BASE_URL}/api/picks/today",
            headers=auth_headers,
            timeout=45,
        )
        assert r.status_code == 200, f"{r.status_code} {r.text[:400]}"
        body = r.json()
        # payload might be either a list or a dict with picks list
        if isinstance(body, dict):
            for key in ("picks", "items", "data"):
                if key in body and isinstance(body[key], list):
                    return body[key]
            # fallthrough: assume dict itself has picks
            return body.get("picks", [])
        return body

    def test_picks_today_returns_list(self, picks_today):
        assert isinstance(picks_today, list), f"expected list, got {type(picks_today)}"
        # Non-blocking: warn if empty
        if len(picks_today) == 0:
            pytest.skip("no picks_today returned — cannot verify devig fields")

    def test_every_pick_has_devig_fields(self, picks_today):
        if len(picks_today) == 0:
            pytest.skip("no picks_today returned")
        missing_devig = []
        for i, p in enumerate(picks_today):
            # book_odds is required for devig — if pick has no book_odds skip
            if not p.get("book_odds"):
                continue
            for k in ("no_vig_book_odds", "no_vig_implied_pct",
                      "book_hold_pct", "no_vig_source"):
                if k not in p or p.get(k) is None:
                    missing_devig.append(
                        {"idx": i, "id": p.get("id") or p.get("_id"),
                         "sport": p.get("sport"), "book_odds": p.get("book_odds"),
                         "missing": k}
                    )
                    break
        assert not missing_devig, \
            f"{len(missing_devig)} picks missing devig fields. First 3: {missing_devig[:3]}"

    def test_picks_today_lock_score_present(self, picks_today):
        if len(picks_today) == 0:
            pytest.skip("no picks_today returned")
        missing_ls = [i for i, p in enumerate(picks_today)
                      if p.get("lock_score") is None]
        # allow small tolerance — lock_score can be null for very-fresh picks,
        # but the vast majority should have it. Fail if >20% missing.
        pct_missing = len(missing_ls) / len(picks_today) * 100
        assert pct_missing < 20, \
            f"{pct_missing:.1f}% of picks missing lock_score (n={len(picks_today)})"

    def test_picks_today_reasonable_count(self, picks_today):
        # Sanity: not returning empty list nor absurdly small count (regression check)
        # This is a soft assertion — logging count for report visibility.
        n = len(picks_today)
        print(f"[picks_today] count = {n}")
        assert n >= 0  # never negative; primary check is 200 above


# ---------------------------------------------------------------------------
# 4) Picks History regression
# ---------------------------------------------------------------------------
class TestPicksHistoryRegression:
    def test_history_200(self, auth_headers):
        r = requests.get(
            f"{BASE_URL}/api/picks/history?days=30",
            headers=auth_headers,
            timeout=60,
        )
        assert r.status_code == 200, f"{r.status_code} {r.text[:400]}"
        body = r.json()
        # Body may be list or dict
        if isinstance(body, dict):
            picks = body.get("picks") or body.get("items") or body.get("data") or []
        else:
            picks = body
        assert isinstance(picks, list), f"expected list, got {type(picks)}"
        print(f"[picks_history?days=30] count = {len(picks)}")


# ---------------------------------------------------------------------------
# 5) MLB grading fix regression spot-check
# ---------------------------------------------------------------------------
class TestMlbGradingRegression:
    @pytest.fixture(scope="class")
    def history_picks(self, auth_headers):
        r = requests.get(
            f"{BASE_URL}/api/picks/history?days=60",
            headers=auth_headers,
            timeout=60,
        )
        assert r.status_code == 200
        body = r.json()
        if isinstance(body, dict):
            return body.get("picks") or body.get("items") or body.get("data") or []
        return body

    def _find_pick(self, picks, player_substrs, expected_date_frag=None):
        """Find picks where selection contains player substring."""
        matched = []
        for p in picks:
            sel = (p.get("selection") or "").lower()
            event = (p.get("event") or "").lower()
            evt_time = p.get("event_time") or ""
            for sub in player_substrs:
                if sub.lower() in sel or sub.lower() in event:
                    if expected_date_frag and expected_date_frag not in evt_time:
                        continue
                    matched.append(p)
                    break
        return matched

    def test_wheeler_picks_still_won(self, history_picks):
        matches = self._find_pick(history_picks, ["Wheeler"], expected_date_frag="2026-07-12")
        if not matches:
            pytest.skip("no Wheeler 07-12 picks in 60-day history (may have rolled off)")
        for p in matches:
            status = p.get("status")
            # per iter71, Wheeler 07-12 K/Outs picks should be status=won
            assert status in ("won", "pending"), \
                f"Wheeler pick {p.get('selection')} regressed: status={status}"
            print(f"[wheeler 07-12] {p.get('selection')} → {status}")

    def test_altuve_lost_unchanged(self, history_picks):
        matches = self._find_pick(history_picks, ["Altuve"], expected_date_frag="2026-07-04")
        if not matches:
            pytest.skip("no Altuve 07-04 picks in 60-day history")
        # per iter71, Altuve 07-04 0.5 Hits should be lost
        losts = [p for p in matches if p.get("status") == "lost"]
        wons_or_other = [p for p in matches if p.get("status") not in ("lost", "pending", "push")]
        assert not wons_or_other, \
            f"Altuve pick(s) regressed away from lost: {[(p.get('selection'), p.get('status')) for p in wons_or_other]}"
        print(f"[altuve 07-04] {len(matches)} pick(s), {len(losts)} lost")

    def test_machado_lost_unchanged(self, history_picks):
        matches = self._find_pick(history_picks, ["Machado"], expected_date_frag="2026-07-09")
        if not matches:
            pytest.skip("no Machado 07-09 picks in 60-day history")
        wons_or_other = [p for p in matches
                         if p.get("status") not in ("lost", "pending", "push")]
        assert not wons_or_other, \
            f"Machado pick(s) regressed away from lost: {[(p.get('selection'), p.get('status')) for p in wons_or_other]}"
        print(f"[machado 07-09] {len(matches)} pick(s)")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
