"""iter-98 PERKLOCKS Phase 1C production foundation integrity review.

Covers items in the review request beyond the two regression suites:
  * BUG FIX 1: circuit breaker 422 never-trips + real 500 streak trips
    + reset re-arms.
  * BUG FIX 2: PickRefreshOrchestrator constructs with explicit motor db.
  * GET /api/admin/provider-foundation sanitized report + 401/403.
  * Funnel telemetry: DB collection has expected reasons + constants
    present.
  * Contract flags A/B/C.
  * Board-filling regression: NBA game emits ZERO picks + MODEL_UNAVAILABLE.
  * GET /api/picks/today returns 200 with auth.

IMPORTANT: this test always leaves the live breaker CLOSED (see the
save/restore fixture on TestCircuitBreaker422).
"""
from __future__ import annotations

import asyncio
import inspect
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE_URL = (os.environ.get("EXPO_PUBLIC_BACKEND_URL")
            or "http://localhost:8001").rstrip("/")


# ── Shared auth ──────────────────────────────────────────────────────

_TOKEN_CACHE: dict[str, str] = {}


@pytest.fixture(scope="session")
def admin_token() -> str:
    if "t" in _TOKEN_CACHE:
        return _TOKEN_CACHE["t"]
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": "demo@lockscore.ai", "password": "demo123"},
        timeout=15,
    )
    assert r.status_code == 200, r.text
    tok = r.json()["access_token"]
    _TOKEN_CACHE["t"] = tok
    return tok


# ── BUG FIX 1: circuit breaker 422 vs 500 ────────────────────────────

class TestCircuitBreaker422:
    def _snap(self, se):
        return dict(
            disabled=se._API_DISABLED,
            reason=se._API_DISABLED_REASON,
            s401=se._API_401_STREAK,
            sfail=se._API_FAIL_STREAK,
            ok=se._API_TOTAL_OK,
            fail=se._API_TOTAL_FAIL,
            err=se._API_LAST_ERR,
        )

    def _restore(self, se, snap):
        se._API_DISABLED = snap["disabled"]
        se._API_DISABLED_REASON = snap["reason"]
        se._API_401_STREAK = snap["s401"]
        se._API_FAIL_STREAK = snap["sfail"]
        se._API_TOTAL_OK = snap["ok"]
        se._API_TOTAL_FAIL = snap["fail"]
        se._API_LAST_ERR = snap["err"]

    def test_422_never_trips_breaker(self):
        import sports_engine as se
        snap = self._snap(se)
        try:
            se.reset_odds_api_circuit()
            for _ in range(12):
                se.record_odds_call_result(
                    status_code=422, body="422", ok=False)
            st = se.get_odds_api_status()
            assert st["disabled"] is False, (
                f"422 must not trip: {st}")
            assert st["consecutive_failures"] == 0, st
        finally:
            se.reset_odds_api_circuit()  # ensure live breaker CLOSED
            self._restore(se, snap)
            se.reset_odds_api_circuit()

    def test_real_500_streak_still_opens_breaker(self):
        import sports_engine as se
        snap = self._snap(se)
        try:
            se.reset_odds_api_circuit()
            for _ in range(max(8, se._API_FAIL_TRIP)):
                se.record_odds_call_result(
                    status_code=500, body="oops", ok=False)
            st = se.get_odds_api_status()
            assert st["disabled"] is True, (
                f"500 streak must open breaker: {st}")
            # reset re-arms
            st2 = se.reset_odds_api_circuit()
            assert st2["disabled"] is False, st2
            assert st2["consecutive_failures"] == 0, st2
        finally:
            se.reset_odds_api_circuit()  # ensure live breaker CLOSED
            self._restore(se, snap)
            se.reset_odds_api_circuit()


# ── BUG FIX 2: orchestrator explicit motor db construction ───────────

class TestOrchestratorMotorBoolCrash:
    def test_construct_with_explicit_motor_database(self):
        from services.database import get_database
        from services.pick_refresh_orchestrator import PickRefreshOrchestrator
        db = get_database()
        # BEFORE fix: `database or db` raised NotImplementedError
        orch = PickRefreshOrchestrator(database=db)
        assert orch._db is not None
        # basic sanity: attribute wiring untouched
        assert hasattr(orch, "refresh")


# ── GET /api/admin/provider-foundation ───────────────────────────────

class TestProviderFoundationEndpoint:
    def test_401_when_no_auth(self):
        r = requests.get(
            f"{BASE_URL}/api/admin/provider-foundation", timeout=15)
        assert r.status_code in (401, 403), r.status_code

    def test_admin_returns_sanitized_report(self, admin_token):
        r = requests.get(
            f"{BASE_URL}/api/admin/provider-foundation",
            headers={"Authorization": f"Bearer {admin_token}"},
            timeout=30,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        # required sections
        for k in ("odds_api_key", "database", "circuit_breaker",
                  "budget_governor", "provider_truth",
                  "funnel_telemetry"):
            assert k in body, f"missing key {k}: {list(body.keys())}"
        # fingerprint present, no raw key
        fp = body["odds_api_key"].get("fingerprint_sha256_8")
        assert isinstance(fp, str) and len(fp) == 8, body["odds_api_key"]
        raw_key = os.environ.get("THE_ODDS_API_KEY") or ""
        payload_str = str(body)
        assert raw_key == "" or raw_key not in payload_str, (
            "raw key leaked into provider-foundation payload")
        # No 32-char hex key anywhere in the response (defensive)
        # allow SHA-256 (64) but block bare 32-char hex which is
        # the odds api key length
        m = re.findall(r"\b[0-9a-fA-F]{32}\b", payload_str)
        assert not m, f"32-char hex-like tokens present: {m[:3]}"
        # db reachable
        assert body["database"].get("read_write_ok") is True, body["database"]
        # provider truth section
        assert "last_quota_headers" in body["provider_truth"], (
            body["provider_truth"])
        # funnel telemetry by_reason present
        assert "by_reason" in body["funnel_telemetry"], (
            body["funnel_telemetry"])


# ── Funnel telemetry: constants + DB persistence ─────────────────────

class TestFunnelTelemetryFoundation:
    REQUIRED_CONSTANTS = (
        "PROVIDER_AUTH_FAILURE",
        "BUDGET_GOVERNOR_BLOCKED",
        "CIRCUIT_BREAKER_OPEN",
        "PROVIDER_RATE_LIMITED",
        "PROVIDER_REQUEST_FAILED",
        "REFRESH_RUNTIME_FAILURE",
        "MODEL_UNAVAILABLE",
        "SYNTHETIC_SCORER_RESEARCH_ONLY",
    )

    def test_constants_exist(self):
        from services import funnel_telemetry as ft
        missing = [c for c in self.REQUIRED_CONSTANTS
                   if not hasattr(ft, c)]
        assert not missing, f"missing telemetry constants: {missing}"
        # sanity: each constant equals its own name
        from services import funnel_telemetry as ft
        for c in self.REQUIRED_CONSTANTS:
            assert getattr(ft, c) == c, f"{c} != string constant"

    def test_db_collection_has_infra_and_model_reasons(self):
        from motor.motor_asyncio import AsyncIOMotorClient

        async def _run():
            cli = AsyncIOMotorClient(os.environ["MONGO_URL"])
            db = cli[os.environ.get("DB_NAME") or "perkslocks_production"]
            reasons_found: dict[str, int] = {}
            for reason in ("MODEL_UNAVAILABLE",
                           "EVIDENCE_THRESHOLD",
                           "SYNTHETIC_SCORER_RESEARCH_ONLY"):
                n = await db.funnel_telemetry.count_documents(
                    {"reason": reason})
                reasons_found[reason] = n
            cli.close()
            return reasons_found

        found = asyncio.run(_run())
        # Not every deploy has all three, but at least
        # MODEL_UNAVAILABLE should be present (dominant infra reason).
        assert found["MODEL_UNAVAILABLE"] > 0, (
            f"no MODEL_UNAVAILABLE funnel records in DB: {found}")


# ── Contract flag A: MLS ESPN leaderboard is research-only ───────────

class TestContractFlagA_MLS:
    def test_mls_leaderboard_not_extended_into_pick_stream(self):
        import sports_engine as se
        src = inspect.getsource(se._fetch_player_props_for_sport)
        assert "SYNTHETIC_ODDS_RESEARCH_ONLY" in src
        # No extend of mls_espn_leaderboard picks into publisher stream
        assert "all_picks.extend(espn_mls_picks)" not in src

    def test_db_has_no_publishable_mls_leaderboard_pick(self):
        from motor.motor_asyncio import AsyncIOMotorClient

        async def _run():
            cli = AsyncIOMotorClient(os.environ["MONGO_URL"])
            db = cli[os.environ.get("DB_NAME") or "perkslocks_production"]
            q = {
                "source": "mls_espn_leaderboard",
                "pick_date": {"$gte": "2026-08-14"},
                "$or": [
                    {"synthetic_odds": True},
                    {"is_synthetic": True},
                    {"research_only": True},
                ],
            }
            # A publishable pick would NOT carry research_only=True;
            # look for any pick from that source with pick_date >=
            # 2026-08-14 that is BOTH synthetic AND not research_only.
            bad_q = {
                "source": "mls_espn_leaderboard",
                "pick_date": {"$gte": "2026-08-14"},
                "$or": [
                    {"synthetic_odds": True},
                    {"is_synthetic": True},
                ],
                "research_only": {"$ne": True},
            }
            n_bad = await db.picks.count_documents(bad_q)
            n_flagged = await db.picks.count_documents(q)
            cli.close()
            return n_bad, n_flagged

        n_bad, n_flagged = asyncio.run(_run())
        assert n_bad == 0, (
            f"{n_bad} publishable synthetic MLS leaderboard picks in "
            f"db.picks — contract violated")


# ── Contract flag B: UFC ESPN pick contract ──────────────────────────

class TestContractFlagB_UFC:
    def test_build_ufc_pick_contract(self):
        import ufc_espn_ingest as ui
        src = inspect.getsource(ui._build_ufc_pick)
        assert '"book_odds":        None' in src
        assert '"no_real_book_line": True' in src
        assert '"model_only":       True' in src

    def test_main_board_ineligible_even_at_99(self):
        from services.main_board_eligibility import is_main_board_eligible
        pick = {
            "sport": "UFC",
            "book_odds": None,
            "implied_probability": None,
            "no_real_book_line": True,
            "model_only": True,
            "lock_score": 99,
            "published_lock_score": 99,
        }
        assert is_main_board_eligible(pick) is False


# ── Contract flag C: CSL elite injection retired ─────────────────────

class TestContractFlagC_CSL:
    def test_ensure_csl_elite_picks_writes_only_to_research(self):
        from services.pick_refresh_orchestrator import _ensure_csl_elite_picks
        src = inspect.getsource(_ensure_csl_elite_picks)
        assert "db.picks.insert_many" not in src
        assert "publish_upserted_picks" not in src
        assert "model_research_evidence" in src


# ── Board-filling regression: NBA -> MODEL_UNAVAILABLE ───────────────

class TestBoardFillingRegression:
    def _nba_game(self):
        from datetime import timedelta, timezone
        commence = (datetime.now(timezone.utc)
                    + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")

        def _book(k):
            return {
                "key": k,
                "markets": [
                    {"key": "h2h", "outcomes": [
                        {"name": "Boston Celtics", "price": -140},
                        {"name": "Miami Heat", "price": 120},
                    ]},
                    {"key": "totals", "outcomes": [
                        {"name": "Over", "point": 218.5, "price": -110},
                        {"name": "Under", "point": 218.5, "price": -110},
                    ]},
                    {"key": "spreads", "outcomes": [
                        {"name": "Boston Celtics", "point": -3.5,
                         "price": -110},
                        {"name": "Miami Heat", "point": 3.5, "price": -110},
                    ]},
                ],
            }
        return {
            "id": "evt-nba-review-001",
            "sport_key": "basketball_nba",
            "home_team": "Boston Celtics",
            "away_team": "Miami Heat",
            "commence_time": commence,
            "bookmakers": [_book("draftkings"), _book("fanduel")],
        }

    def test_nba_emits_zero_picks_and_records_model_unavailable(self):
        import sports_engine as se
        from services import funnel_telemetry as ft
        ft.drain()
        picks = se._picks_from_game(
            "NBA", "NBA", self._nba_game(), "2026-06-15")
        assert picks == [], (
            f"NBA must not emit picks (book-follow); got: "
            f"{[p.get('market') for p in picks]}")
        recs = ft.peek(sport="NBA")
        assert any(r["reason"] == "MODEL_UNAVAILABLE" for r in recs), (
            f"expected MODEL_UNAVAILABLE for NBA; got: {recs}")


# ── General API health ───────────────────────────────────────────────

class TestApiHealth:
    def test_picks_today_returns_200_with_auth(self, admin_token):
        r = requests.get(
            f"{BASE_URL}/api/picks/today?limit=50",
            headers={"Authorization": f"Bearer {admin_token}"},
            timeout=30,
        )
        assert r.status_code == 200, r.text[:400]
        data = r.json()
        assert isinstance(data, (list, dict)), type(data)
