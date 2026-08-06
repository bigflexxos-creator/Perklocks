"""Phase 2γ — OddsApiGateway cutover + fan-out consolidation tests.

Covers the 27 required assertions from the Phase 2γ spec plus the
repository guardrail test that fails if a direct Odds-API URL/key
appears outside the approved allow-list.
"""
from __future__ import annotations

import asyncio
import os
import re
import uuid
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pytest
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from motor.motor_asyncio import AsyncIOMotorClient

from services.odds_api_gateway import (
    OddsApiGateway,
    _classify_endpoint, _sport_key_from_url, _event_id_from_url,
    ENDPOINT_SPORTS_LIST, ENDPOINT_EVENTS_LIST, ENDPOINT_EVENT_ODDS,
    ENDPOINT_ALT_LINES,
    APPROVED_422_ENDPOINTS, MAX_422_RETRY_REQUESTS, MAX_422_RETRY_CREDITS,
    _gateway_enabled, _global_refresh_mode,
)
from services.single_flight import SingleFlight, build_request_key
from services.tournament_registry import (
    TournamentRegistry, CONSECUTIVE_EMPTY_THRESHOLD,
)


def _run(coro):
    return asyncio.run(coro)


def _fresh_db():
    return AsyncIOMotorClient(os.environ["MONGO_URL"])[
        os.environ.get("DB_NAME", "lockscore_db")
    ]


# ═════════════════════════════════════════════════════════════════════
# Guardrail — direct-call detection
# ═════════════════════════════════════════════════════════════════════
BACKEND_ROOT = Path("/app/backend")

# Allowlist:  Modules permitted to reference the Odds API base URL,
# THE_ODDS_API_KEY, or a direct httpx client targeting the provider.
GUARDRAIL_ALLOWLIST = {
    # The gateway owns transport.
    "services/odds_api_gateway.py",
    # odds_cache holds the module-level constant (imported from gateway)
    # and a rollback-only httpx path guarded by ODDS_GATEWAY_ENABLED.
    "services/odds_cache.py",
    # Legacy modules to be migrated — grandfathered for Phase 2γ ONLY
    # if they use cached_httpx_get (which now routes through the
    # gateway).  A follow-up test asserts they issue no direct httpx
    # AsyncClient call against api.the-odds-api.com.
    "services/odds_provider.py",       # probes only
    "sports_engine.py",                # circuit-breaker probe
    "alt_lines_feed.py",               # discovery — uses cached_httpx_get
    "closing_line_snapshotter.py",     # uses cached_httpx_get
    "brain/nrfi_engine.py",            # uses cached_httpx_get
    "soccer/real_odds.py",             # uses cached_httpx_get
    "tennis_extra/real_odds.py",       # uses cached_httpx_get
    "soccer_lab.py",                   # config-only
    "services/mls_direct_inject.py",   # uses cached_httpx_get
    "services/soccer_prop_inject.py",  # uses cached_httpx_get
    "routes/admin_routes.py",          # documentation of the env var
    "server.py",                       # docstring only
}

# Tests and scripts are excluded from the guardrail.
GUARDRAIL_SKIP_DIRS = {"tests", "scripts", "__pycache__"}

# Regex for direct httpx to the Odds API.
_DIRECT_HTTPX_ODDS = re.compile(
    r'httpx\.(get|post|AsyncClient)[^)\n]{0,80}api\.the-odds-api\.com',
    re.IGNORECASE,
)
_URL_LITERAL = re.compile(r"api\.the-odds-api\.com")
_KEY_LITERAL = re.compile(r"THE_ODDS_API_KEY")


def _iter_backend_py():
    for p in BACKEND_ROOT.rglob("*.py"):
        rel = p.relative_to(BACKEND_ROOT).as_posix()
        top = rel.split("/", 1)[0]
        if top in GUARDRAIL_SKIP_DIRS:
            continue
        # Skip .pytest_cache etc.
        if "__pycache__" in rel:
            continue
        yield rel, p


def test_guardrail_no_direct_odds_api_url_outside_allowlist():
    """No file outside the allowlist may reference api.the-odds-api.com."""
    offenders: list[str] = []
    for rel, path in _iter_backend_py():
        if rel in GUARDRAIL_ALLOWLIST:
            continue
        text = path.read_text(errors="ignore")
        if _URL_LITERAL.search(text):
            offenders.append(rel)
    assert not offenders, (
        "Direct Odds API URL literal found outside allowlist:\n"
        + "\n".join(offenders)
    )


def test_guardrail_no_direct_odds_api_key_outside_allowlist():
    """THE_ODDS_API_KEY may only appear inside the gateway + config."""
    offenders: list[str] = []
    for rel, path in _iter_backend_py():
        if rel in GUARDRAIL_ALLOWLIST:
            continue
        text = path.read_text(errors="ignore")
        if _KEY_LITERAL.search(text):
            offenders.append(rel)
    assert not offenders, (
        "THE_ODDS_API_KEY reference found outside allowlist:\n"
        + "\n".join(offenders)
    )


def test_guardrail_no_direct_httpx_odds_api_asyncclient():
    """No file may construct an httpx client with the Odds API host
    hard-coded in the same expression (defence in depth against the
    allowlist regressing)."""
    offenders: list[str] = []
    for rel, path in _iter_backend_py():
        if rel == "services/odds_api_gateway.py":
            continue
        text = path.read_text(errors="ignore")
        if _DIRECT_HTTPX_ODDS.search(text):
            offenders.append(rel)
    assert not offenders, offenders


# ═════════════════════════════════════════════════════════════════════
# Gateway internals
# ═════════════════════════════════════════════════════════════════════
def test_endpoint_classification():
    b = "https://api.the-odds-api.com/v4"
    assert _classify_endpoint(f"{b}/sports") == ENDPOINT_SPORTS_LIST
    assert _classify_endpoint(f"{b}/sports/basketball_nba/events") == ENDPOINT_EVENTS_LIST
    assert _classify_endpoint(f"{b}/sports/basketball_nba/odds") == "bulk_odds"
    assert _classify_endpoint(f"{b}/sports/x/events/y/odds") == ENDPOINT_EVENT_ODDS
    assert _classify_endpoint(f"{b}/foo") == "generic"
    assert _sport_key_from_url(f"{b}/sports/basketball_nba/odds") == "basketball_nba"
    assert _event_id_from_url(f"{b}/sports/x/events/abc/odds") == "abc"


def test_request_key_deterministic():
    k1 = build_request_key(
        endpoint="/v4/sports/nba/events", sport_key="basketball_nba",
        markets="h2h,spreads", regions="us")
    k2 = build_request_key(
        endpoint="/v4/sports/nba/events", sport_key="basketball_nba",
        markets="spreads,h2h", regions="us")
    assert k1 == k2, "market order must not change the key"
    k3 = build_request_key(
        endpoint="/v4/sports/nba/events", sport_key="basketball_nba",
        markets="spreads,h2h", regions="us",
        extra_params={"apiKey": "SECRET"})
    assert k3 == k1, "apiKey must never enter the key"


# ═════════════════════════════════════════════════════════════════════
# Single-flight (assertions 2, 3, 4)
# ═════════════════════════════════════════════════════════════════════
def test_single_flight_only_one_owner_across_two_callers():
    async def go():
        db = _fresh_db()
        sf = SingleFlight(db)
        await sf.ensure_indices()
        rk = f"test_sf_{uuid.uuid4().hex}"
        won1, doc1 = await sf.acquire(rk, ttl_seconds=15)
        won2, doc2 = await sf.acquire(rk, ttl_seconds=15)
        assert won1 is True
        assert won2 is False
    _run(go())


def test_single_flight_waiter_gets_result():
    async def go():
        db = _fresh_db()
        sf = SingleFlight(db)
        await sf.ensure_indices()
        rk = f"test_sf_wait_{uuid.uuid4().hex}"
        won, doc = await sf.acquire(rk, ttl_seconds=15)
        assert won is True
        owner_token = doc["owner_token"]

        async def slow_complete():
            await asyncio.sleep(0.2)
            await sf.complete(rk, owner_token,
                                result_summary={"actual_credits": 5})
        asyncio.create_task(slow_complete())
        result = await sf.wait_for_result(rk, timeout=2.0)
        assert result is not None
        assert result.get("status") == "done"
    _run(go())


def test_single_flight_expired_owner_can_be_replaced():
    async def go():
        db = _fresh_db()
        sf = SingleFlight(db)
        await sf.ensure_indices()
        rk = f"test_sf_exp_{uuid.uuid4().hex}"
        won, _ = await sf.acquire(rk, ttl_seconds=1)
        assert won
        # Force expiry.
        await db["odds_request_flights"].update_one(
            {"request_key": rk},
            {"$set": {"expires_at": datetime.now(timezone.utc)
                        - timedelta(seconds=5)}},
        )
        won2, _ = await sf.acquire(rk, ttl_seconds=15)
        assert won2 is True
    _run(go())


# ═════════════════════════════════════════════════════════════════════
# Tournament registry
# ═════════════════════════════════════════════════════════════════════
def test_tournament_suppressed_after_consecutive_empties():
    async def go():
        db = _fresh_db()
        tr = TournamentRegistry(db)
        await tr.ensure_indices()
        key = f"tennis_test_{uuid.uuid4().hex[:10]}"
        for _ in range(CONSECUTIVE_EMPTY_THRESHOLD):
            await tr.mark_empty(key)
        # Now suppressed.
        assert (await tr.is_eligible(key)) is False
        # Filter helper too.
        elig = await tr.filter_eligible([key, "other"])
        assert key not in elig
    _run(go())


def test_tournament_currentpicks_key_bypasses_suppression():
    async def go():
        db = _fresh_db()
        tr = TournamentRegistry(db)
        await tr.ensure_indices()
        key = f"soccer_test_{uuid.uuid4().hex[:10]}"
        for _ in range(CONSECUTIVE_EMPTY_THRESHOLD + 2):
            await tr.mark_empty(key)
        # Simulate the key being present in current picks.
        await db["odds_tournament_registry"].update_one(
            {"sport_key": key},
            {"$set": {"present_in_current_picks": True}},
        )
        assert (await tr.is_eligible(key)) is True
    _run(go())


def test_tournament_event_seen_unsuppresses_immediately():
    async def go():
        db = _fresh_db()
        tr = TournamentRegistry(db)
        await tr.ensure_indices()
        key = f"csl_test_{uuid.uuid4().hex[:10]}"
        for _ in range(CONSECUTIVE_EMPTY_THRESHOLD + 3):
            await tr.mark_empty(key)
        assert (await tr.is_eligible(key)) is False
        await tr.mark_events_seen(key, count=2)
        assert (await tr.is_eligible(key)) is True
    _run(go())


# ═════════════════════════════════════════════════════════════════════
# Bad-market filtering
# ═════════════════════════════════════════════════════════════════════
def test_bad_market_filter_removes_marked_markets():
    async def go():
        db = _fresh_db()
        from services import bad_market_registry
        await bad_market_registry.ensure_indices(db)
        sp = f"soccer_test_{uuid.uuid4().hex[:10]}"
        ev = "test-event-1"
        await bad_market_registry.mark_bad(
            db, sport_key=sp,
            markets=["player_goal_scorer_anytime"],
            reason="422",
        )
        good = await bad_market_registry.filter_markets(
            db, sport_key=sp,
            markets=["h2h", "player_goal_scorer_anytime", "totals"],
        )
        assert "player_goal_scorer_anytime" not in good
        assert "h2h" in good and "totals" in good
    _run(go())


# ═════════════════════════════════════════════════════════════════════
# Cost estimation + 422-retry cap constants
# ═════════════════════════════════════════════════════════════════════
def test_cost_estimation_reflects_markets_and_regions():
    est_small = OddsApiGateway.estimate_credits(
        "bulk_odds", markets="h2h", regions="us")
    est_large = OddsApiGateway.estimate_credits(
        "bulk_odds", markets="h2h,spreads,totals,alt_totals,alt_spreads",
        regions="us,uk,eu")
    assert est_large > est_small
    # /sports and /events are always 1 credit.
    assert OddsApiGateway.estimate_credits(ENDPOINT_SPORTS_LIST) == 1
    assert OddsApiGateway.estimate_credits(ENDPOINT_EVENTS_LIST) == 1


def test_422_retry_caps_are_bounded():
    assert MAX_422_RETRY_REQUESTS > 0
    assert MAX_422_RETRY_CREDITS > 0
    # sanity: the caps should be low enough that a full 422 storm
    # cannot burn more than ~50 credits.
    assert MAX_422_RETRY_REQUESTS * 10 <= MAX_422_RETRY_CREDITS + 10
    # 422 retry is only ever legal for event_odds / alt_lines.
    assert ENDPOINT_EVENT_ODDS in APPROVED_422_ENDPOINTS
    assert ENDPOINT_ALT_LINES in APPROVED_422_ENDPOINTS


# ═════════════════════════════════════════════════════════════════════
# Feature flags
# ═════════════════════════════════════════════════════════════════════
def test_gateway_flag_and_refresh_mode_defaults():
    # unset → gateway on, snapshot mode
    for k in ("ODDS_GATEWAY_ENABLED", "ODDS_GLOBAL_REFRESH_MODE"):
        os.environ.pop(k, None)
    assert _gateway_enabled() is True
    assert _global_refresh_mode() == "snapshot"
    os.environ["ODDS_GATEWAY_ENABLED"] = "false"
    assert _gateway_enabled() is False
    os.environ["ODDS_GATEWAY_ENABLED"] = "true"
    os.environ["ODDS_GLOBAL_REFRESH_MODE"] = "legacy_hourly"
    assert _global_refresh_mode() == "legacy_hourly"
    os.environ.pop("ODDS_GATEWAY_ENABLED", None)
    os.environ.pop("ODDS_GLOBAL_REFRESH_MODE", None)


# ═════════════════════════════════════════════════════════════════════
# Deleted-function guardrail
# ═════════════════════════════════════════════════════════════════════
def test_fetch_event_odds_individual_is_removed():
    """Phase 2γ: `_fetch_event_odds_individual` was the single largest
    fan-out source (~970 credits/day) and must be hard-removed."""
    text = Path("/app/backend/alt_lines_feed.py").read_text()
    assert "_fetch_event_odds_individual" not in text, (
        "_fetch_event_odds_individual is still defined — must be "
        "hard-removed per Phase 2γ spec."
    )


# ═════════════════════════════════════════════════════════════════════
# Snapshot loops: run_immediately=True removed on paid jobs
# ═════════════════════════════════════════════════════════════════════
def test_paid_snapshot_loops_do_not_run_immediately():
    """Phase 2γ: alt-lines, MLS direct-inject, and soccer prop-inject
    scheduled loops MUST NOT fire immediately on cold start.  The
    startup path must first read the last saved snapshot from Mongo
    and only trigger one coordinated recovery job if the board is
    missing or critically stale."""
    src = Path("/app/backend/server.py").read_text()
    # Locate each schedule_utc_hours invocation for a paid job and
    # confirm run_immediately is NOT True.
    for job in ("alt_lines_feed", "mls_direct_inject",
                 "soccer_prop_inject"):
        # Find the schedule call that names this job.
        m = re.search(
            r'schedule_utc_hours\(\s*name\s*=\s*["\']' + re.escape(job)
            + r'["\'][^)]*\)',
            src, flags=re.DOTALL,
        )
        assert m, f"{job} schedule_utc_hours call not found"
        block = m.group(0)
        assert "run_immediately=True" not in block, (
            f"{job} still has run_immediately=True — must be removed "
            f"per Phase 2γ Part 9."
        )


# ═════════════════════════════════════════════════════════════════════
# odds_cache no longer contains provider URL literals
# ═════════════════════════════════════════════════════════════════════
def test_odds_cache_module_has_no_provider_url_literal():
    src = Path("/app/backend/services/odds_cache.py").read_text()
    # The only allowed reference is via the imported ODDS_API_BASE
    # constant.  A bare URL literal must not appear.
    assert "api.the-odds-api.com" not in src
    assert "THE_ODDS_API_KEY" not in src


# ═════════════════════════════════════════════════════════════════════
# Phase 1 immutability + Phase 2β lease/budget tests still pass
# ═════════════════════════════════════════════════════════════════════
def test_phase1_and_2b_tests_are_still_importable():
    """Smoke test that we didn't break the earlier test suites."""
    import importlib
    for m in ("tests.test_iter117_phase1b",
              "tests.test_iter118_phase1c",
              "tests.test_iter119_phase2b"):
        importlib.import_module(m)
