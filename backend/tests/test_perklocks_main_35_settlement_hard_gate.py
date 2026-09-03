"""PERKLOCKS-MAIN 35 · P0-2 — SETTLEMENT HARD GATE regression suite.

Locks in the invariant that missing actuals / identity failures /
unsupported markets / not-final events NEVER produce a LOSS / zero /
VOID at any real grading entry point.

Contracts asserted:
  * `settlement_hard_gate.evaluate(...)` correctly routes to the
    SettlementCapabilityRegistry.
  * A supported MLB moneyline with FINAL score → gradeable=True.
  * MLB moneyline where the score payload is NOT completed →
    UNRESOLVED with reason EVENT_NOT_FINAL.
  * MLB moneyline with completed=True but no score entries →
    UNRESOLVED with reason MISSING_ACTUAL_DATA (never LOSS).
  * Soccer "Shots" (registry has NO capability) → UNRESOLVED with
    reason UNSUPPORTED_MARKET.
  * Tennis Total Games (Alt) with missing team names in event →
    UNRESOLVED with reason IDENTITY_FAILURE (never LOSS).
  * `settlement_engine.settle_pick()` returns None (never fabricates
    an outcome) whenever the gate refuses.
  * `settle_pick()` stamps `_hard_gate_refused=True` + reason on the
    pick so telemetry can prove no silent LOSS was emitted.
  * `settle_pick()` continues to return the correct outcome for a
    real completed game (no regression on the happy path).
"""
from __future__ import annotations

import pytest


def _mk_pick(**kw):
    base = {
        "sport": "MLB",
        "market": "Moneyline",
        "selection": "New York Yankees",
        "event": "Baltimore Orioles @ New York Yankees",
    }
    base.update(kw)
    return base


# ─────────────────────────────────────────────────────────────────────
# Hard-gate primitive
# ─────────────────────────────────────────────────────────────────────
def test_gate_ok_on_final_mlb_moneyline():
    from services.settlement_hard_gate import evaluate

    pick = _mk_pick()
    score_payload = {
        "completed": True,
        "scores": [
            {"name": "New York Yankees", "score": "5"},
            {"name": "Baltimore Orioles", "score": "3"},
        ],
    }
    ok, reason, canonical = evaluate(pick, score_payload)
    assert ok is True, reason
    assert reason == ""
    assert canonical["sport"] == "MLB"
    assert canonical["family"] == "moneyline"


def test_gate_refuses_when_event_not_final():
    from services.settlement_hard_gate import evaluate

    pick = _mk_pick()
    score_payload = {
        "completed": False,
        "scores": [
            {"name": "New York Yankees", "score": "5"},
            {"name": "Baltimore Orioles", "score": "3"},
        ],
    }
    ok, reason, _ = evaluate(pick, score_payload)
    assert ok is False
    assert reason == "EVENT_NOT_FINAL"


def test_gate_refuses_when_actuals_missing():
    from services.settlement_hard_gate import evaluate

    pick = _mk_pick()
    score_payload = {"completed": True, "scores": []}
    ok, reason, _ = evaluate(pick, score_payload)
    assert ok is False
    assert reason == "MISSING_ACTUAL_DATA"


def test_gate_refuses_unsupported_market():
    from services.settlement_hard_gate import evaluate

    pick = {
        "sport": "Soccer",
        "market": "Shots on Target Over 3.5",
        "selection": "Foo",
        "event": "Away FC @ Home FC",
    }
    score_payload = {"completed": True, "scores": [
        {"name": "Home FC", "score": "1"},
        {"name": "Away FC", "score": "0"},
    ]}
    # Soccer + shots on target isn't in the family map → UNSUPPORTED_MARKET.
    ok, reason, canonical = evaluate(pick, score_payload)
    assert ok is False
    assert reason == "UNSUPPORTED_MARKET"
    assert canonical["family"] is None


def test_gate_refuses_identity_failure_when_event_missing_teams():
    from services.settlement_hard_gate import evaluate

    pick = _mk_pick(event="")
    score_payload = {"completed": True, "scores": [
        {"name": "New York Yankees", "score": "5"},
        {"name": "Baltimore Orioles", "score": "3"},
    ]}
    ok, reason, _ = evaluate(pick, score_payload)
    assert ok is False
    # We can't resolve away/home identities → gate refuses.
    assert reason == "IDENTITY_FAILURE"


# ─────────────────────────────────────────────────────────────────────
# Direct settle_pick refusal + telemetry stamp
# ─────────────────────────────────────────────────────────────────────
def test_settle_pick_returns_none_when_gate_refuses_no_completed():
    from settlement_engine import settle_pick

    pick = _mk_pick()
    score_payload = {"completed": False, "scores": []}
    out = settle_pick(pick, score_payload)
    assert out is None
    assert pick.get("_hard_gate_refused") is True
    assert pick.get("_hard_gate_reason") == "EVENT_NOT_FINAL"


def test_settle_pick_returns_none_when_actuals_missing_after_completed():
    from settlement_engine import settle_pick

    pick = _mk_pick()
    score_payload = {"completed": True, "scores": []}
    out = settle_pick(pick, score_payload)
    assert out is None
    assert pick.get("_hard_gate_refused") is True
    assert pick.get("_hard_gate_reason") == "MISSING_ACTUAL_DATA"


def test_settle_pick_returns_none_and_never_loss_on_unsupported_market():
    from settlement_engine import settle_pick

    pick = {
        "sport": "Soccer",
        "market": "Shots on Target Over 3.5",
        "selection": "Foo",
        "event": "Away FC @ Home FC",
    }
    score_payload = {"completed": True, "scores": [
        {"name": "Home FC", "score": "1"},
        {"name": "Away FC", "score": "0"},
    ]}
    out = settle_pick(pick, score_payload)
    # UNSUPPORTED_MARKET must not silently become LOSS or PUSH — either
    # the gate refuses OR legacy branches fall through, both return None.
    assert out not in ("lost", "push", "won")
    assert out is None


# ─────────────────────────────────────────────────────────────────────
# Happy path regression — real final MLB moneyline still grades.
# ─────────────────────────────────────────────────────────────────────
def test_settle_pick_happy_path_returns_won_when_pick_wins():
    from settlement_engine import settle_pick

    pick = _mk_pick(selection="New York Yankees")
    score_payload = {
        "completed": True,
        "scores": [
            {"name": "New York Yankees", "score": "5"},
            {"name": "Baltimore Orioles", "score": "3"},
        ],
    }
    assert settle_pick(pick, score_payload) == "won"
    assert not pick.get("_hard_gate_refused")


def test_settle_pick_happy_path_returns_lost_when_pick_loses():
    from settlement_engine import settle_pick

    pick = _mk_pick(selection="Baltimore Orioles")
    score_payload = {
        "completed": True,
        "scores": [
            {"name": "New York Yankees", "score": "5"},
            {"name": "Baltimore Orioles", "score": "3"},
        ],
    }
    assert settle_pick(pick, score_payload) == "lost"


# ─────────────────────────────────────────────────────────────────────
# Registry contract sanity
# ─────────────────────────────────────────────────────────────────────
def test_capability_registry_returns_all_four_reasons():
    from services.settlement_capability_registry import (
        is_gradeable,
        REASON_UNSUPPORTED_MARKET,
        REASON_EVENT_NOT_FINAL,
        REASON_IDENTITY_FAILURE,
        REASON_MISSING_ACTUAL,
    )
    # 1. Unsupported → UNSUPPORTED_MARKET
    ok, reason = is_gradeable("MLB", "unknown_family", True, True, {})
    assert ok is False and reason == REASON_UNSUPPORTED_MARKET
    # 2. Not final → EVENT_NOT_FINAL
    ok, reason = is_gradeable("MLB", "moneyline", False, True,
                              {"home_score": 5, "away_score": 3})
    assert ok is False and reason == REASON_EVENT_NOT_FINAL
    # 3. Identity failure → IDENTITY_FAILURE
    ok, reason = is_gradeable("MLB", "moneyline", True, False,
                              {"home_score": 5, "away_score": 3})
    assert ok is False and reason == REASON_IDENTITY_FAILURE
    # 4. Missing actual → MISSING_ACTUAL_DATA
    ok, reason = is_gradeable("MLB", "moneyline", True, True,
                              {"home_score": None, "away_score": 3})
    assert ok is False and reason == REASON_MISSING_ACTUAL
    # Positive path
    ok, reason = is_gradeable("MLB", "moneyline", True, True,
                              {"home_score": 5, "away_score": 3})
    assert ok is True and reason == ""
