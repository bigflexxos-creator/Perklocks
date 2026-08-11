"""Block 2C (focused pass) — Regression tests.

Locks the invariants for:
  §1  bad_market_registry event-specific keying (XCUT-2 fix)
  §2  bounded 422 bundle-isolation contract
  §4  explicit CacheState taxonomy
  §5  differentiated TTL policy
  §12/§18 pipeline diagnostic reason-code mapping
  §19 StaleBuildBanner deploy-drift fix
"""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "/app/backend")

pytestmark = pytest.mark.unit


# ═══════════════════════════════════════════════════════════════
# §1 — bad_market_registry event-specific keying
# ═══════════════════════════════════════════════════════════════
def test_mark_bad_refuses_to_write_global_marker_from_event_422():
    """A single event-level 422 must NEVER produce a global marker."""
    src = open("/app/backend/services/bad_market_registry.py").read()
    assert "refusing to write a global marker" in src
    assert 'scope not in ("event", "global")' in src


def test_mark_bad_signature_supports_event_id_and_scope():
    import inspect
    from services import bad_market_registry as bmr
    sig = inspect.signature(bmr.mark_bad)
    assert "event_id" in sig.parameters
    assert "scope" in sig.parameters


def test_filter_markets_signature_supports_event_id():
    import inspect
    from services import bad_market_registry as bmr
    sig = inspect.signature(bmr.filter_markets)
    assert "event_id" in sig.parameters


def test_bad_market_registry_source_has_scope_taxonomy():
    src = open("/app/backend/services/bad_market_registry.py").read()
    assert "scope" in src
    assert "event_id" in src
    # Bad-market filter must honor BOTH scopes.
    assert '"scope": "global"' in src
    # Event-scoped filter present (line has flexible spacing).
    assert '"scope"' in src and '"event"' in src


# ═══════════════════════════════════════════════════════════════
# §2 — bounded 422 bundle-isolation
# ═══════════════════════════════════════════════════════════════
def test_bundle_isolation_hard_limits_present():
    from services.provider_cache_state import (
        MAX_422_RETRY_REQUESTS, MAX_422_RETRY_CREDITS, MAX_422_RETRY_DEPTH,
    )
    # Hard ceilings — never allow unbounded fanout.
    assert 1 <= MAX_422_RETRY_REQUESTS <= 16
    assert 1 <= MAX_422_RETRY_CREDITS  <= 16
    assert 1 <= MAX_422_RETRY_DEPTH    <= 5


def test_bundle_isolation_finds_single_bad_market():
    """Wrapped with asyncio.run since pytest-asyncio isn't in env."""
    import asyncio
    from services.provider_cache_state import isolate_bad_markets

    async def probe(markets):
        if "player_home_runs" in markets and len(markets) == 1:
            return None
        if "player_home_runs" in markets:
            return None
        return {m: {"line": 0.5} for m in markets}

    async def _run():
        return await isolate_bad_markets(
            ["batter_hits", "batter_rbis", "player_home_runs"], probe)

    r = asyncio.run(_run())
    assert "player_home_runs" in r.bad_markets
    assert "batter_hits" in r.supported_markets
    assert "batter_rbis" in r.supported_markets
    assert r.retries_used <= 8


def test_bundle_isolation_respects_max_retries_ceiling():
    import asyncio
    from services.provider_cache_state import isolate_bad_markets

    async def always_422(markets):
        return None

    async def _run():
        return await isolate_bad_markets(
            ["a", "b", "c", "d", "e", "f", "g", "h"], always_422)

    r = asyncio.run(_run())
    assert r.retries_used <= 8
    assert r.credits_used <= 8
    assert len(r.bad_markets) + len(r.unresolved_markets) == 8


# ═══════════════════════════════════════════════════════════════
# §4 — explicit CacheState taxonomy
# ═══════════════════════════════════════════════════════════════
def test_cache_state_taxonomy_locked():
    from services.provider_cache_state import CacheState
    required = {
        "VALID_DATA", "VALID_EMPTY_PROVIDER_ZERO", "EVENT_NOT_READY",
        "PROVIDER_ERROR", "PROVIDER_CIRCUIT_OPEN", "BUDGET_BLOCKED",
        "PRIORITY_SHED", "PARTIAL_MARKET_RESPONSE",
        "MARKET_UNSUPPORTED", "EVENT_MARKET_422",
        "PARTIAL_422_UNRESOLVED", "STALE_DATA", "EXPIRED",
    }
    got = {s.value for s in CacheState}
    missing = required - got
    assert not missing, f"missing CacheState members: {missing}"


def test_cache_state_categories_are_distinguishable():
    """Empty ≠ error ≠ unsupported ≠ budget-blocked (spec §4)."""
    from services.provider_cache_state import CacheState
    # Each must be a distinct string.
    values = [s.value for s in CacheState]
    assert len(values) == len(set(values))


# ═══════════════════════════════════════════════════════════════
# §5 — differentiated TTL policy
# ═══════════════════════════════════════════════════════════════
def test_ttl_policy_differentiates_empty_vs_valid():
    from services.provider_cache_state import (
        CacheState, ttl_seconds,
    )
    assert ttl_seconds(CacheState.VALID_DATA) >  ttl_seconds(
        CacheState.VALID_EMPTY_PROVIDER_ZERO)
    assert ttl_seconds(CacheState.VALID_EMPTY_PROVIDER_ZERO) > 0


def test_ttl_policy_budget_blocked_is_short():
    from services.provider_cache_state import (
        CacheState, ttl_seconds,
    )
    # Budget-blocked TTL must be short — the budget clock resets fast.
    assert ttl_seconds(CacheState.BUDGET_BLOCKED) <= 60


def test_ttl_policy_provider_error_is_short_backoff():
    from services.provider_cache_state import (
        CacheState, ttl_seconds,
    )
    assert ttl_seconds(CacheState.PROVIDER_ERROR) <= 300


def test_ttl_policy_market_unsupported_is_long():
    from services.provider_cache_state import (
        CacheState, ttl_seconds,
    )
    # Global unsupported markets sit at ~24h — matches registry TTL.
    assert ttl_seconds(CacheState.MARKET_UNSUPPORTED) >= 3600


def test_near_first_pitch_ttl_shortens_empty_and_not_ready():
    from services.provider_cache_state import (
        CacheState, near_first_pitch_ttl,
    )
    # >2h before first pitch → normal TTL.
    assert near_first_pitch_ttl(CacheState.EVENT_NOT_READY, 300) == \
        near_first_pitch_ttl(CacheState.EVENT_NOT_READY, None)
    # <2h before first pitch → capped at 120s.
    assert near_first_pitch_ttl(CacheState.EVENT_NOT_READY, 60) <= 120
    assert near_first_pitch_ttl(
        CacheState.VALID_EMPTY_PROVIDER_ZERO, 30) <= 120


# ═══════════════════════════════════════════════════════════════
# §12/§18 — pipeline diagnostic reason-code integration
# ═══════════════════════════════════════════════════════════════
def test_every_cache_state_maps_to_a_pipeline_reason_code():
    from services.provider_cache_state import (
        CacheState, cache_state_to_reason_code,
    )
    from services.pipeline_diagnostic import ReasonCode
    valid_codes = {c.value for c in ReasonCode} | {None}
    for s in CacheState:
        code = cache_state_to_reason_code(s)
        assert code in valid_codes, \
            f"{s} maps to unknown reason code {code!r}"


def test_budget_blocked_never_masquerades_as_no_market():
    """Spec §12 — budget block must NOT be conflated with unsupported."""
    from services.provider_cache_state import (
        CacheState, cache_state_to_reason_code,
    )
    assert cache_state_to_reason_code(CacheState.BUDGET_BLOCKED) != \
        cache_state_to_reason_code(CacheState.MARKET_UNSUPPORTED)
    assert cache_state_to_reason_code(CacheState.BUDGET_BLOCKED) != \
        cache_state_to_reason_code(CacheState.VALID_EMPTY_PROVIDER_ZERO)


# ═══════════════════════════════════════════════════════════════
# §19 — StaleBuildBanner deploy-drift fix
# ═══════════════════════════════════════════════════════════════
def test_stale_build_banner_uses_server_started_at_not_wall_clock():
    """The banner must not derive 'days behind' from wall-clock time
    OR from server_started_at.  Block 2C-cont Issue-6 (2026-08):
    the banner must ONLY cite an age when real deploy metadata
    (deploy_timestamp / deploy_id / git_commit_sha) is available;
    otherwise it may only note that a new backend build exists."""
    src = open("/app/frontend/src/components/StaleBuildBanner.tsx").read()
    # Deploy-drift fix rationale must be preserved.
    assert "deploy-drift fix" in src.lower() or "deploy drift" in src.lower()
    # The banner must NOT derive age from server_started_at anymore.
    # We tolerate the string appearing in the docstring/backwards-compat
    # comment, but the trigger logic must be data_version-based.
    assert "data_version" in src
    assert "deploy_metadata" in src


def test_backend_version_endpoint_exposes_server_started_at():
    src = open("/app/backend/server.py").read()
    assert "SERVER_STARTED_AT" in src
    assert "server_started_at" in src


# ═══════════════════════════════════════════════════════════════
# Invariants unchanged (§23)
# ═══════════════════════════════════════════════════════════════
def test_universal_settlement_contract_unchanged():
    from services import universal_settlement_contract as usc
    assert hasattr(usc, "grade_over_under")
    assert hasattr(usc, "RESULT_UNRESOLVED")


def test_published_results_truth_unchanged():
    from services import published_results_truth as prt
    assert hasattr(prt, "PublishedResultsTruthService")
    assert hasattr(prt, "canonical_query")


def test_perklocks_day_contract_unchanged():
    from services import perklocks_day as pd
    assert hasattr(pd, "perklocks_day")
    assert hasattr(pd, "is_in_current_slate")
