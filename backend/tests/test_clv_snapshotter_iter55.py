"""Iteration 55 — Closing-Line Snapshotter (CLV tracking) tests.

Covers:
- Pure helpers from closing_line_snapshotter (event_id parsing, event_time
  selection, market matching).
- snapshot_status() against live Mongo.
- Admin endpoint GET /api/admin/clv/snapshot-status (admin vs non-admin).
- End-to-end fallback path: insert a pick with a sport NOT in
  _ODDS_SPORT_KEY → _snapshot_closes_once should still close it with
  closing_odds = book_odds and closing_odds_snapshotted = True.
"""
from __future__ import annotations

import os
import sys
import uuid
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

# Motor binds to the first event loop it touches. Create one shared loop
# for the whole test module so every async helper runs on the same loop.
_SHARED_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_SHARED_LOOP)


def _run(coro):
    return _SHARED_LOOP.run_until_complete(coro)

# Make /app/backend importable for direct helper tests
BACKEND_DIR = Path("/app/backend")
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

BASE_URL = os.environ.get(
    "EXPO_BACKEND_URL",
    "https://player-intel-engine.preview.emergentagent.com",
).rstrip("/")


# ───────────────────────── Module + helpers ─────────────────────────
class TestModuleImport:
    """The snapshotter module must import cleanly and expose helpers."""

    def test_imports_clean(self):
        from closing_line_snapshotter import (  # noqa: F401
            line_observer_loop,
            closing_snapshotter_loop,
            snapshot_status,
            _extract_event_id,
            _pick_event_time,
            _match_pick_to_odds,
            _parse_iso,
            _snapshot_closes_once,
        )

    def test_extract_event_id_from_soccer_external_id(self):
        from closing_line_snapshotter import _extract_event_id
        ext = (
            "Soccer-b55f9569de3f4d731bc5537ff5ff43e4-"
            "player_goal_scorer_anytime-Alexander-Yes-0.5"
        )
        assert _extract_event_id({"external_id": ext}) == \
            "b55f9569de3f4d731bc5537ff5ff43e4"

    def test_extract_event_id_prefers_explicit_event_id(self):
        from closing_line_snapshotter import _extract_event_id
        assert _extract_event_id(
            {"event_id": "abc123", "external_id": "Soccer-x-y"}
        ) == "abc123"

    def test_extract_event_id_returns_none_when_missing(self):
        from closing_line_snapshotter import _extract_event_id
        assert _extract_event_id({}) is None
        assert _extract_event_id({"external_id": "MLB-short"}) is None

    def test_pick_event_time_prefers_event_time(self):
        from closing_line_snapshotter import _pick_event_time
        et = "2026-06-25T20:00:00Z"
        ct = "2026-06-25T21:00:00Z"
        assert _pick_event_time({"event_time": et, "commence_time": ct}) == et

    def test_pick_event_time_falls_back_to_commence_time(self):
        from closing_line_snapshotter import _pick_event_time
        assert _pick_event_time({"commence_time": "x"}) == "x"


# ───────────────────────── _match_pick_to_odds ─────────────────────────
class TestMatchPickToOdds:
    """The market/selection matcher backs the closing snapshot."""

    @staticmethod
    def _h2h_bookmakers():
        # Three books with three different Yankees prices: median = -150
        return [
            {"key": "draftkings", "markets": [{
                "key": "h2h",
                "outcomes": [
                    {"name": "New York Yankees", "price": -140},
                    {"name": "Boston Red Sox",   "price": 120},
                ],
            }]},
            {"key": "fanduel", "markets": [{
                "key": "h2h",
                "outcomes": [
                    {"name": "New York Yankees", "price": -150},
                    {"name": "Boston Red Sox",   "price": 130},
                ],
            }]},
            {"key": "betmgm", "markets": [{
                "key": "h2h",
                "outcomes": [
                    {"name": "New York Yankees", "price": -160},
                    {"name": "Boston Red Sox",   "price": 140},
                ],
            }]},
        ]

    def test_h2h_moneyline_returns_median(self):
        from closing_line_snapshotter import _match_pick_to_odds
        pick = {"market": "Moneyline", "selection": "New York Yankees"}
        price = _match_pick_to_odds(pick, self._h2h_bookmakers())
        assert price == -150.0

    def test_prop_market_not_snapshotted(self):
        """Player-prop picks must return None (not in h2h/spreads/totals)."""
        from closing_line_snapshotter import _match_pick_to_odds
        pick = {
            "market": "Anytime Goal Scorer",
            "selection": "Alexander Isak",
        }
        assert _match_pick_to_odds(pick, self._h2h_bookmakers()) is None

    def test_no_bookmakers_returns_none(self):
        from closing_line_snapshotter import _match_pick_to_odds
        assert _match_pick_to_odds(
            {"market": "Moneyline", "selection": "X"}, []
        ) is None

    def test_selection_not_found_returns_none(self):
        from closing_line_snapshotter import _match_pick_to_odds
        pick = {"market": "Moneyline", "selection": "Chicago Cubs"}
        assert _match_pick_to_odds(pick, self._h2h_bookmakers()) is None


# ───────────────────────── snapshot_status() ─────────────────────────
class TestSnapshotStatus:
    """Direct call against live Mongo via deps.db."""

    def test_snapshot_status_keys_and_types(self):
        from deps import db
        from closing_line_snapshotter import snapshot_status

        result = _run(snapshot_status(db))

        expected = {
            "settled_picks", "closing_snapshotted", "live_snapshots",
            "fallback_snapshots", "line_observations", "coverage_pct",
        }
        assert expected.issubset(result.keys())
        for k in expected - {"coverage_pct"}:
            assert isinstance(result[k], int), f"{k} not int: {result[k]!r}"
        assert isinstance(result["coverage_pct"], (int, float))
        assert 0 <= result["coverage_pct"] <= 100


# ───────────────────────── Admin endpoint ─────────────────────────
def _login_demo():
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": "demo@lockscore.ai", "password": "demo123"},
        timeout=15,
    )
    if r.status_code != 200:
        return None
    return r.json().get("access_token")


def _register_and_promote_admin():
    """Register a fresh user, then promote in Mongo to role=admin."""
    from deps import db
    email = f"test_clv_admin_{uuid.uuid4().hex[:8]}@lockscore.com"
    password = "Test1234!"
    reg = requests.post(
        f"{BASE_URL}/api/auth/register",
        json={"email": email, "password": password, "name": "CLV Admin"},
        timeout=15,
    )
    if reg.status_code not in (200, 201):
        return None, email
    _run(db.users.update_one(
        {"email": email},
        {"$set": {"role": "admin", "status": "active"}},
    ))
    login = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": email, "password": password},
        timeout=15,
    )
    if login.status_code != 200:
        return None, email
    return login.json().get("access_token"), email


class TestAdminClvSnapshotStatusEndpoint:
    ENDPOINT = "/api/admin/clv/snapshot-status"

    def test_unauthenticated_is_401(self):
        r = requests.get(f"{BASE_URL}{self.ENDPOINT}", timeout=15)
        assert r.status_code == 401, f"got {r.status_code}: {r.text[:200]}"

    def test_non_admin_is_403(self):
        token = _login_demo()
        if not token:
            pytest.skip("demo user login failed")
        r = requests.get(
            f"{BASE_URL}{self.ENDPOINT}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        # require_admin_user raises 403 for non-admin authenticated users
        assert r.status_code == 403, f"got {r.status_code}: {r.text[:200]}"

    def test_admin_returns_200_with_status_payload(self):
        from deps import db
        token, email = _register_and_promote_admin()
        if not token:
            pytest.skip("admin register/promote failed")
        try:
            r = requests.get(
                f"{BASE_URL}{self.ENDPOINT}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
            )
            assert r.status_code == 200, f"{r.status_code}: {r.text[:300]}"
            body = r.json()
            for k in (
                "settled_picks", "closing_snapshotted", "live_snapshots",
                "fallback_snapshots", "line_observations", "coverage_pct",
            ):
                assert k in body, f"missing {k} in {body}"
        finally:
            _run(db.users.delete_one({"email": email}))


# ───────────── Startup log + _parse_iso regression check ─────────────
class TestStartupLogs:
    LOG = Path("/var/log/supervisor/backend.err.log")

    def test_startup_includes_clv_message(self):
        if not self.LOG.exists():
            pytest.skip("backend.err.log missing")
        text = self.LOG.read_text(errors="ignore")
        assert "Closing-line snapshotter started (CLV tracking enabled)" in text

    def test_no_parse_iso_errors_in_recent_log(self):
        """Earlier reloads logged `_parse_iso is not defined`. Confirm
        the most-recent server lifecycle is clean of that error."""
        if not self.LOG.exists():
            pytest.skip("backend.err.log missing")
        text = self.LOG.read_text(errors="ignore")
        # Find the last "Started server process" marker — everything after
        # it is the current process's logs.
        marker_idx = text.rfind("Started server process")
        recent = text[marker_idx:] if marker_idx != -1 else text
        assert "_parse_iso is not defined" not in recent, \
            "_parse_iso errors still present in current backend lifecycle"


# ───────────── End-to-end fallback path via _snapshot_closes_once ─────────────
class TestFallbackSnapshotPath:
    """Use a sport NOT in _ODDS_SPORT_KEY ('KBO') so the snapshotter takes
    the fallback branch (closing_odds = book_odds)."""

    PICK_ID = f"TEST_clv_kbo_{uuid.uuid4().hex[:8]}"

    def test_fallback_writes_closing_odds_from_book_odds(self):
        from deps import db
        from closing_line_snapshotter import _snapshot_closes_once

        now = datetime.now(timezone.utc)
        kickoff = now + timedelta(minutes=10)
        pick_doc = {
            "id":           self.PICK_ID,
            "status":       "pending",
            "sport":        "KBO",
            "market":       "Moneyline",
            "selection":    "Doosan Bears",
            "event_time":   kickoff.isoformat(),
            "external_id": f"KBO-{uuid.uuid4().hex}-ml",
            "book_odds":    -150,
            "odds_at_pick": -150,
        }
        try:
            _run(db.picks.insert_one(pick_doc))
            result = _run(_snapshot_closes_once(db))
            updated = _run(db.picks.find_one({"id": self.PICK_ID}))
            assert updated is not None
            assert updated.get("closing_odds_snapshotted") is True, updated
            assert updated.get("closing_odds_source") == "fallback_book_odds"
            assert updated.get("closing_odds") == -150
            assert isinstance(result, dict)
            assert "closed" in result and "events" in result
        finally:
            _run(db.picks.delete_one({"id": self.PICK_ID}))
