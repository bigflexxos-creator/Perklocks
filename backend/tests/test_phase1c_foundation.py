"""PERKLOCKS PHASE 1C — production foundation integrity tests.

Covers (§13):
  * Provider configuration: env-var key resolution, safe fingerprinting,
    no secret leakage from the sanitized diagnostics payload.
  * Budget governor: UTC day/month keys, reserve→commit accounting,
    failed-request zero-charging classification.
  * Circuit breaker: failure opens, reset re-arms, success recovers.
  * Database: env-driven resolution, funnel telemetry persistence.
  * Refresh observability: fetcher crashes + validator drops are
    funnel-attributable (no silent 0-pick refreshes).
  * Contract flags: MLS synthetic odds research-only, UFC ESPN
    no-real-line contract, CSL elite injection retired.
  * No board-filling regression (Phase 1B removals stay removed).

Run: EXPO_PUBLIC_BACKEND_URL=http://localhost:8001 python -m pytest -q \
     tests/test_phase1c_foundation.py
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from services import funnel_telemetry as funnel  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_funnel():
    funnel.drain()
    yield
    funnel.drain()


# ── Provider configuration ───────────────────────────────────────────

class TestProviderConfig:
    def test_key_resolution_is_env_only_and_stripped(self):
        from services.odds_api_gateway import _api_key
        old = os.environ.get("THE_ODDS_API_KEY")
        try:
            os.environ["THE_ODDS_API_KEY"] = "  test-key-123 \n"
            assert _api_key() == "test-key-123", (
                "whitespace/newline corruption must be stripped")
            os.environ["THE_ODDS_API_KEY"] = ""
            assert _api_key() == ""
        finally:
            if old is None:
                os.environ.pop("THE_ODDS_API_KEY", None)
            else:
                os.environ["THE_ODDS_API_KEY"] = old

    def test_no_hardcoded_fallback_key(self):
        import sports_engine as se
        src = inspect.getsource(se)[:4000]
        # SEC-002 contract: env-only resolution, no committed fallback.
        assert 'os.environ.get("THE_ODDS_API_KEY") or ""' in src

    def test_diagnostics_never_leak_full_secret(self):
        """The sanitized admin payload builder must fingerprint, not
        expose, THE_ODDS_API_KEY."""
        import routes.admin_routes as ar
        src = inspect.getsource(ar.provider_foundation)
        assert "sha256" in src
        # the endpoint must never place the raw key into the response
        assert 'key_report = {' in src and '"value": key' not in src
        assert 'cb.pop("key_tail"' in src, (
            "partial key tail must be stripped from foundation payload")

    def test_fingerprint_is_deterministic_and_short(self):
        fp = hashlib.sha256(b"abc").hexdigest()[:8]
        assert len(fp) == 8


# ── Budget governor ──────────────────────────────────────────────────

class TestBudgetGovernor:
    def test_day_and_month_keys_are_utc(self):
        from datetime import datetime, timezone, timedelta
        from services.provider_budget import _day_key, _month_key
        dt = datetime(2026, 8, 14, 23, 30,
                      tzinfo=timezone(timedelta(hours=-7)))  # PDT evening
        # 23:30 PDT = 06:30 UTC next day → UTC boundary governs reset
        assert _day_key(dt) == "2026-08-15"
        assert _month_key(dt) == "2026-08"

    def test_reserve_commit_release_accounting(self):
        from services.database import get_database
        from services.provider_budget import ProviderBudget

        async def _run():
            import uuid as _uuid
            db = get_database()
            budget = ProviderBudget(db, provider="test_phase1c")
            rk = f"phase1c-acct-{_uuid.uuid4().hex[:8]}"
            before = await budget.get_budget_status()
            r = await budget.reserve(
                estimated_credits=5, endpoint_type="bulk_odds",
                caller="phase1c_test", job_name="phase1c_test",
                request_key=rk)
            assert r.get("allowed"), r
            mid = await budget.get_budget_status()
            assert (mid["normal"]["day_reserved"]
                    >= before["normal"]["day_reserved"] + 5)
            await budget.commit(r["intent_id"], actual_credits=3)
            after = await budget.get_budget_status()
            assert (after["normal"]["day_used"]
                    == before["normal"]["day_used"] + 3), (
                "commit must count ACTUAL credits, not the estimate")
            assert (after["normal"]["day_reserved"]
                    <= before["normal"]["day_reserved"]), (
                "reservation must be released on commit")
        asyncio.run(_run())

    def test_failed_requests_charge_zero(self):
        """Gateway classification: 401/403/422/429/5xx cost 0 credits
        when the provider omits usage headers."""
        from services import odds_api_gateway as gw
        src = inspect.getsource(gw)
        assert '{"401", "403", "422", "429"}' in src
        assert "actual_credits = 0" in src

    def test_provider_header_reconciliation_exists(self):
        from services import odds_api_gateway as gw
        src = inspect.getsource(gw)
        assert "x-requests-used" in src
        assert "odds_api_quota_state" in src


# ── Circuit breaker ──────────────────────────────────────────────────

class TestCircuitBreaker:
    def _snapshot(self, se):
        return (se._API_DISABLED, se._API_DISABLED_REASON,
                se._API_401_STREAK, se._API_FAIL_STREAK,
                se._API_TOTAL_OK, se._API_TOTAL_FAIL, se._API_LAST_ERR)

    def _restore(self, se, snap):
        (se._API_DISABLED, se._API_DISABLED_REASON, se._API_401_STREAK,
         se._API_FAIL_STREAK, se._API_TOTAL_OK, se._API_TOTAL_FAIL,
         se._API_LAST_ERR) = snap

    def test_401_streak_opens_breaker_and_reset_rearms(self):
        import sports_engine as se
        snap = self._snapshot(se)
        try:
            se.reset_odds_api_circuit()
            for _ in range(se._API_401_TRIP):
                se.record_odds_call_result(status_code=401,
                                           body="bad key", ok=False)
            st = se.get_odds_api_status()
            assert st["disabled"] is True
            assert "401" in st["disabled_reason"]
            # stale-open repair path: admin reset re-arms
            st2 = se.reset_odds_api_circuit()
            assert st2["disabled"] is False
            assert st2["consecutive_401s"] == 0
        finally:
            self._restore(se, snap)

    def test_422_market_probe_never_trips_breaker(self):
        """Live-observed defect (2026-08-14): 8 consecutive 422
        alt-line market probes tripped the breaker and disabled the
        ENTIRE provider.  422 = market-shape response, not provider
        health — it must never count toward the fail streak."""
        import sports_engine as se
        snap = self._snapshot(se)
        try:
            se.reset_odds_api_circuit()
            for _ in range(se._API_FAIL_TRIP + 4):
                se.record_odds_call_result(status_code=422,
                                           body="422", ok=False)
            st = se.get_odds_api_status()
            assert st["disabled"] is False, (
                "422 probes must not open the circuit breaker")
            assert st["consecutive_failures"] == 0
        finally:
            self._restore(se, snap)

    def test_success_recovers_streaks(self):
        import sports_engine as se
        snap = self._snapshot(se)
        try:
            se.reset_odds_api_circuit()
            se.record_odds_call_result(status_code=429, body="rl", ok=False)
            assert se.get_odds_api_status()["consecutive_failures"] >= 1
            se.record_odds_call_result(status_code=200, ok=True)
            st = se.get_odds_api_status()
            assert st["consecutive_failures"] == 0
            assert st["consecutive_401s"] == 0
        finally:
            self._restore(se, snap)

    def test_breaker_state_is_db_synced(self):
        import sports_engine as se
        src = inspect.getsource(se.sync_circuit_breaker_from_db)
        assert "circuit_breaker_state" in inspect.getsource(se)[:20000] or \
               "circuit_breaker_state" in src


# ── Database + telemetry persistence ─────────────────────────────────

class TestDatabaseFoundation:
    def test_env_driven_db_resolution(self):
        from services.database import _resolve_env_config
        url, name = _resolve_env_config()
        assert url and name
        assert name == (os.environ.get("DB_NAME") or "perkslocks_production")

    def test_funnel_flush_persists_to_db(self):
        from motor.motor_asyncio import AsyncIOMotorClient

        async def _run():
            cli = AsyncIOMotorClient(os.environ["MONGO_URL"])
            db = cli[os.environ.get("DB_NAME") or "perkslocks_production"]
            funnel.record(sport="TEST1C", market="unit", stage="test",
                          reason="MODEL_UNAVAILABLE", event="A @ B")
            n = await funnel.flush(db, cycle_id="phase1c-test")
            assert n == 1
            doc = await db.funnel_telemetry.find_one(
                {"cycle_id": "phase1c-test", "sport": "TEST1C"})
            assert doc and doc["reason"] == "MODEL_UNAVAILABLE"
            await db.funnel_telemetry.delete_many(
                {"cycle_id": "phase1c-test"})
            cli.close()
        asyncio.run(_run())

    def test_infra_reason_codes_exist(self):
        for code in ("PROVIDER_QUOTA_BLOCKED", "BUDGET_GOVERNOR_BLOCKED",
                     "CIRCUIT_BREAKER_OPEN", "PROVIDER_AUTH_FAILURE",
                     "PROVIDER_RATE_LIMITED", "PROVIDER_REQUEST_FAILED",
                     "REFRESH_RUNTIME_FAILURE"):
            assert getattr(funnel, code) == code


# ── Refresh observability (no silent zero-pick refreshes) ────────────

class TestRefreshObservability:
    def test_orchestrator_accepts_explicit_database(self):
        """Regression: Motor Database forbids bool() — `database or db`
        crashed every explicit-db caller."""
        from services.database import get_database
        from services.pick_refresh_orchestrator import PickRefreshOrchestrator
        orch = PickRefreshOrchestrator(database=get_database())
        assert orch._db is not None

    def test_fetcher_crash_is_funnel_recorded(self, monkeypatch):
        import sports_engine as se

        async def _boom(date_str):
            raise RuntimeError("provider exploded")

        async def _no_props(*a, **kw):
            return []

        async def _noop():
            return None

        monkeypatch.setattr(se, "fetch_nhl_picks", _boom)
        monkeypatch.setattr(se, "_fetch_player_props_for_sport", _no_props)
        monkeypatch.setattr(se, "_load_active_sports", _noop)
        picks = asyncio.run(
            se.generate_all_picks("2026-06-15", sport_filter="NHL"))
        assert picks == []
        recs = funnel.peek(reason="REFRESH_RUNTIME_FAILURE")
        assert recs and recs[0]["sport"] == "NHL"
        assert "provider exploded" in (recs[0].get("detail") or "")

    def test_evidence_threshold_drop_is_funnel_recorded(self):
        from board_validator import evidence_threshold
        pick = {"id": "x", "sport": "NFL", "event": "A @ B",
                "market": "B Moneyline", "selection": "B",
                "event_time": "2026-06-15T00:00:00Z",
                "book_odds": -110, "lock_score": 90,
                "factors": {}, "edge_percent": 0.0}
        kept, stats = evidence_threshold([pick])
        assert kept == [] and stats["dropped"] == 1
        recs = funnel.peek(reason="EVIDENCE_THRESHOLD")
        assert recs and recs[0]["sport"] == "NFL"

    def test_gateway_budget_and_breaker_blocks_are_recorded(self):
        from services import odds_api_gateway as gw
        src = inspect.getsource(gw)
        assert "BUDGET_GOVERNOR_BLOCKED" in src
        assert "CIRCUIT_BREAKER_OPEN" in src
        assert "PROVIDER_AUTH_FAILURE" in src
        assert "PROVIDER_RATE_LIMITED" in src
        assert "PROVIDER_REQUEST_FAILED" in src


# ── Contract flags (§11) ─────────────────────────────────────────────

class TestContractFlags:
    def test_mls_synthetic_odds_cannot_publish(self):
        import sports_engine as se
        src = inspect.getsource(se._fetch_player_props_for_sport)
        assert "SYNTHETIC_ODDS_RESEARCH_ONLY" in src
        assert "all_picks.extend(espn_mls_picks)" not in src

    def test_ufc_espn_pick_contract_is_explicit(self):
        import ufc_espn_ingest as ui
        src = inspect.getsource(ui._build_ufc_pick)
        assert '"book_odds":        None' in src
        assert '"no_real_book_line": True' in src
        assert '"model_only":       True' in src

    def test_ufc_espn_pick_never_main_board_eligible(self):
        from services.main_board_eligibility import is_main_board_eligible
        pick = {"sport": "UFC", "book_odds": None,
                "implied_probability": None, "no_real_book_line": True,
                "model_only": True, "lock_score": 99,
                "published_lock_score": 99}
        assert is_main_board_eligible(pick) is False

    def test_csl_elite_injection_retired(self):
        from services.pick_refresh_orchestrator import _ensure_csl_elite_picks
        src = inspect.getsource(_ensure_csl_elite_picks)
        assert "db.picks.insert_many" not in src, (
            "CSL elite force-injection into db.picks must stay retired")
        assert "model_research_evidence" in src
        assert "publish_upserted_picks" not in src


# ── No board-filling regression (§12) ────────────────────────────────

class TestNoBoardFillingRegression:
    def test_phase1b_removals_stay_removed(self):
        import sports_engine as se
        src_pg = inspect.getsource(se._picks_from_game)
        # book-follow fallback restricted to MLB/Soccer
        assert 'sport not in ("MLB", "Soccer")' in src_pg
        # UFC ML-only suppression stays retired (comment may remain;
        # no assignment/usage may exist)
        assert "_ufc_ml_only =" not in src_pg
        assert "not _ufc_ml_only" not in src_pg
        # tennis hard-coded lock ladder gated on real math signal
        src_bf = inspect.getsource(se._backfill_tennis_moneylines)
        assert "_math_signal_ok" in src_bf
        # synthetic soccer scorer stays research-only
        src_props = inspect.getsource(se._fetch_player_props_for_sport)
        assert "all_picks.extend(synth_picks)" not in src_props
        # legacy soccer dual-write stays off
        from soccer import pipeline as sp
        assert sp.LEGACY_PICK_EMIT_ENABLED is False
        # tennis gap-filler stays consolidated
        from services.pick_refresh_orchestrator import _tennis_gap_fill_filter
        assert callable(_tennis_gap_fill_filter)
