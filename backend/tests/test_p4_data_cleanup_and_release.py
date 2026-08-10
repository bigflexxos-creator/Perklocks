"""Phase 4 (2026-08-11) — Data cleanup + release readiness.

Locks in:
  * Obsolete blacklist rules removed from `quality_gate._block_reason`
    (H+R+RBI, odds -130, strikeouts -115).
  * Odds API gateway now honours the P1..P5 priority tier —
    low-priority requests are shed when headroom is tight before any
    P1/P2 current-board request.
  * Pre-flight priority decisions cannot silently bypass the gateway
    (they route to the same code path as budget-blocked results).
"""
from __future__ import annotations

import pytest


# ── 1. Obsolete blacklist rules ─────────────────────────────────
@pytest.mark.unit
def test_h_r_rbi_no_longer_auto_blocked():
    from quality_gate import _block_reason
    pick = {"sport": "MLB",
            "market": "Aaron Judge Hits + Runs + RBI Over 1.5",
            "lock_score": 95, "book_odds": -150}
    assert _block_reason(pick) is None, (
        "H+R+RBI must no longer be auto-blocked at the quality-gate — "
        "data-integrity / feature-engine gates apply downstream instead."
    )


@pytest.mark.unit
def test_odds_at_minus_130_no_longer_auto_blocked():
    from quality_gate import _block_reason
    pick = {"sport": "MLB",
            "market": "Aaron Judge Over 0.5 Hits",
            "lock_score": 92, "book_odds": -130}
    assert _block_reason(pick) is None, (
        "-130 odds must not be auto-blocked — dead-zone rule removed "
        "in Phase 4."
    )


@pytest.mark.unit
def test_strikeouts_at_minus_115_no_longer_auto_blocked():
    from quality_gate import _block_reason
    pick = {"sport": "MLB",
            "market": "Gerrit Cole Over 6.5 Strikeouts",
            "lock_score": 95, "book_odds": -115}
    assert _block_reason(pick) is None, (
        "Pitcher strikeouts at -115 must not be auto-blocked — "
        "dead-zone rule removed in Phase 4."
    )


# ── 2. Legitimate blacklist rules still enforced ────────────────
@pytest.mark.unit
def test_legitimate_blacklists_still_enforced():
    """Preserve rules that still have a real data-integrity or
    unsupported-market reason: hat tricks, MLB moneyline, lock
    dead-zone 80-84 on MLB/Soccer."""
    from quality_gate import _block_reason
    hat = _block_reason({"sport":"Soccer","market":"Bellingham Hat Trick",
                          "lock_score":92,"book_odds":+800})
    assert hat is not None and "hat" in hat.lower() or hat is not None
    ml = _block_reason({"sport":"MLB","market":"Yankees Moneyline",
                         "lock_score":92,"book_odds":-160})
    assert ml is not None
    dz = _block_reason({"sport":"MLB","market":"Judge Over 0.5 Hits",
                         "lock_score":82,"book_odds":-160})
    assert dz is not None and "dead_zone" in dz


# ── 3. Priority wired into gateway (contract) ───────────────────
@pytest.mark.unit
def test_gateway_fetch_signature_accepts_priority_kwarg():
    from services.odds_api_gateway import OddsApiGateway
    import inspect
    sig = inspect.signature(OddsApiGateway.fetch)
    assert "priority" in sig.parameters, (
        "OddsApiGateway.fetch must accept the P1..P5 priority tier."
    )


@pytest.mark.unit
def test_gateway_defaults_to_p3_neutral_priority_when_omitted():
    """Older callers that don't pass priority default to P3 (neutral
    middle tier).  P3 is allowed above 5 % headroom — so absent
    priority hints, ordinary fetches keep working."""
    from services import provider_budget_priority as pbp
    d = pbp.decide(pbp.P3_ALT_STRONG, 90, 100)  # 10% headroom
    assert d.allowed is True


@pytest.mark.integration
def test_gateway_low_priority_shed_under_budget_pressure(monkeypatch):
    """Simulate 3% headroom on ProviderBudget.  A P5 fetch attempt
    must be rejected with status='priority_shed' BEFORE any budget
    reservation happens.  P1 fetch attempt is allowed through the
    priority gate (subsequent budget layers may still gate but
    won't be `priority_shed`).
    """
    import asyncio, os
    from dotenv import load_dotenv
    load_dotenv("/app/backend/.env")
    from motor.motor_asyncio import AsyncIOMotorClient
    from services.odds_api_gateway import OddsApiGateway
    from services import provider_budget_priority as pbp

    async def go():
        db = AsyncIOMotorClient(os.environ["MONGO_URL"])[
            os.environ.get("DB_NAME","lockscore_db")]
        gw = OddsApiGateway(db)
        # Force the priority helper to see 3% headroom by monkey-
        # patching the budget's last-observed counters.
        gw.budget._last_used_daily = 970
        gw.budget._last_limit_daily = 1000

        # P5 — must be shed by the priority gate.
        r5 = await gw.fetch(
            url="https://api.example.com/sports",
            caller="p4_test", reason="unit_test",
            priority=pbp.P5_BACKGROUND,
        )
        assert r5.get("ok") is False
        assert r5.get("status") == "priority_shed"
        assert "blocked_priority" in (r5.get("reason") or "")

        # P4 also shed at 3% headroom.
        r4 = await gw.fetch(
            url="https://api.example.com/sports",
            caller="p4_test", reason="unit_test",
            priority=pbp.P4_UPCOMING_PRELOAD,
        )
        assert r4.get("ok") is False
        assert r4.get("status") == "priority_shed"

        # P1 passes the priority gate (may fail later for other
        # reasons — we only assert the priority gate itself did NOT
        # reject).
        r1 = await gw.fetch(
            url="https://api.example.com/sports",
            caller="p4_test", reason="unit_test",
            priority=pbp.P1_LOCKS_TODAY,
        )
        assert r1.get("status") != "priority_shed", (
            f"P1 must clear priority gate; got status={r1.get('status')!r} "
            f"reason={r1.get('reason')!r}"
        )
    asyncio.run(go())


# ── 4. Regression envelope — earlier gates intact ───────────────
@pytest.mark.unit
def test_locks_gate_still_strict_gt_85_after_phase4():
    from services.main_board_eligibility import is_main_board_eligible
    assert is_main_board_eligible({"lock_score": 85.0}) is False
    assert is_main_board_eligible({"lock_score": 85.001}) is True


@pytest.mark.unit
def test_endrick_regression_still_verifies_after_phase4():
    from services.player_team_fixture_validator import (
        validate_player_fixture_pick,
    )
    v = validate_player_fixture_pick(
        {"sport":"Soccer","market":"Endrick To Score or Assist",
         "event":"Haiti @ Brazil","league":"FIFA World Cup · Props"},
        {},
        national_team_lookup={"endrick": "Portugal"},
        fresh_national_team_names={"endrick"},
        nationality_lookup={"endrick": "Portugal"})
    assert v["verified"] is True
    assert v["player_team"] == "Brazil"
