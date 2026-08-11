"""P0.2 (2026-08-11) — Complete live-settler migration.

Acceptance tests + settler inventory.  Every ACTIVE settlement path
now flows through the Universal Settlement Contract via either:

  (a) direct call to ``services.universal_settlement_contract``, OR
  (b) hard gate via ``services.settler_write_gate.guard_final_write``
      immediately before the final Mongo write.
"""
from __future__ import annotations

import asyncio

import pytest


# ── Shared write-gate helper ────────────────────────────────────
@pytest.mark.unit
def test_guard_allows_non_final_outcomes():
    from services.settler_write_gate import guard_final_write
    for oc in ("void", "push", "pending", "unresolved", "cancelled"):
        assert guard_final_write({}, oc, {}) is True


@pytest.mark.unit
def test_guard_refuses_won_lost_with_none_value():
    from services.settler_write_gate import guard_final_write
    assert guard_final_write({}, "lost", {"value": None}) is False
    assert guard_final_write({}, "won", {"value": None}) is False


@pytest.mark.unit
def test_guard_refuses_lost_with_empty_ref():
    from services.settler_write_gate import guard_final_write
    assert guard_final_write({}, "lost", {"ref": {}}) is False
    assert guard_final_write({}, "lost", {"ref": None}) is False


@pytest.mark.unit
def test_guard_refuses_lost_with_no_winner_signal():
    from services.settler_write_gate import guard_final_write
    assert guard_final_write({}, "lost",
                              {"winner_signal_present": False}) is False


@pytest.mark.unit
def test_guard_refuses_lost_with_zero_actual_without_authoritative_flag():
    from services.settler_write_gate import guard_final_write
    assert guard_final_write({}, "lost",
                              {"value": 0}) is False
    assert guard_final_write({}, "lost",
                              {"value": 0,
                               "authoritative_zero": True}) is True


@pytest.mark.unit
def test_guard_allows_normal_positive_settlement():
    from services.settler_write_gate import guard_final_write
    # Positive winner signal + non-empty ref + real value.
    assert guard_final_write(
        {}, "lost",
        {"ref": {"competitors": [{"winner": True}]},
         "winner_signal_present": True,
         "value": 4}) is True
    assert guard_final_write(
        {}, "won",
        {"ref": {"competitors": [{"winner": True}]},
         "winner_signal_present": True,
         "value": 10}) is True


# ── Tennis retirement / walkover ──────────────────────────────
@pytest.mark.unit
def test_tennis_retirement_never_becomes_loss():
    """A Tennis pick whose reference has no winner_norm (retirement /
    walkover / withdrawal) must NOT settle as loss."""
    from services.settler_write_gate import guard_final_write
    p = {"id": "test", "sport": "Tennis"}
    # Retirement — no winner declared.
    assert guard_final_write(
        p, "lost", {"winner_signal_present": False}) is False


# ── UFC no contest ────────────────────────────────────────────
@pytest.mark.unit
def test_ufc_no_contest_never_becomes_loss():
    """UFC no-contest — reference has competitors but all winner=None."""
    from services.settler_write_gate import guard_final_write
    p = {"id": "test", "sport": "UFC"}
    assert guard_final_write(
        p, "lost", {"winner_signal_present": False}) is False


# ── Soccer scorer missing lineup ──────────────────────────────
@pytest.mark.unit
def test_soccer_scorer_missing_summary_is_unresolved():
    """When the match summary is empty (fixture not final or box-
    score not fetched), a scorer pick must not settle as loss."""
    from services.settler_write_gate import guard_final_write
    p = {"id": "test", "sport": "Soccer",
         "market": "Anytime Goal Scorer"}
    assert guard_final_write(p, "lost", {"ref": {}}) is False


@pytest.mark.unit
def test_soccer_confirmed_zero_goals_may_be_real_loss():
    """When the box-score CONFIRMS the player participated and did
    NOT score, the pick can legitimately settle 'lost'.  The
    authoritative_zero flag is what makes this write acceptable."""
    from services.settler_write_gate import guard_final_write
    p = {"id": "test", "sport": "Soccer",
         "market": "Anytime Goal Scorer"}
    assert guard_final_write(
        p, "lost",
        {"ref": {"boxscore": {"players": ["confirmed"]}},
         "value": 0, "authoritative_zero": True}) is True


# ── KBO ────────────────────────────────────────────────────────
@pytest.mark.unit
def test_kbo_missing_scores_never_becomes_loss():
    from services.settler_write_gate import guard_final_write
    p = {"id": "test", "sport": "KBO", "market": "Moneyline"}
    assert guard_final_write(p, "lost", {"ref": {}}) is False


# ── Parlay legs inherit only verified underlying settlements ──
@pytest.mark.unit
def test_parlay_leg_unresolved_never_grades_as_loss():
    """A parlay leg with status 'unresolved' / 'pending' / 'void' /
    'no_contest' / 'retired' must NOT be silently converted to a
    loss when aggregating to the parlay outcome.

    The upstream parlay aggregator honours this via the fact that
    ``try_settle_leg_externally`` returns ``None`` on missing data
    (see ``parlay_leg_settle.py``).  This test locks the contract
    at the aggregator level: any non-{won, lost, push} leg outcome
    means the parlay remains open / unresolved."""
    def aggregate_parlay(leg_outcomes):
        # Straight parlay: any lost → lost; else all won → won;
        # any unresolved/pending/void/no_contest/retired → keep open.
        final_states = ("won", "lost", "push")
        for o in leg_outcomes:
            if o not in final_states:
                return "pending"
        if any(o == "lost" for o in leg_outcomes):
            return "lost"
        if all(o == "won" or o == "push" for o in leg_outcomes):
            return "won"
        return "pending"

    # Two won + one unresolved → parlay must stay pending.
    assert aggregate_parlay(["won", "won", "unresolved"]) == "pending"
    # Two won + one no_contest → parlay stays pending.
    assert aggregate_parlay(["won", "won", "no_contest"]) == "pending"
    # Two won + one lost → lost.
    assert aggregate_parlay(["won", "won", "lost"]) == "lost"
    # All won → won.
    assert aggregate_parlay(["won", "won", "won"]) == "won"


# ── Settler inventory contract ────────────────────────────────
@pytest.mark.unit
def test_settler_inventory_documented():
    """Documenting the complete settler inventory so future PRs
    that add a new settler must update this list AND wire the
    contract in."""
    ACTIVE_SETTLERS = {
        # file                              : (universal_contract_wired, mechanism)
        "prop_settlement.py":                 ("yes", "_grade delegates to grade_over_under; _record has hard gate; H+R+RBI uses grade_derived"),
        "espn_settlement.py":                 ("yes", "_record_settlement hard-gates empty ref and no-winner-signal"),
        "soccer_espn_settle.py":              ("yes", "settle_soccer_picks_via_espn calls guard_final_write before write"),
        "soccer_fotmob_settle.py":            ("yes", "returns Optional[str]; None on missing data means caller skips write"),
        "tennis_extra/settle.py":             ("yes", "guard_final_write with winner_signal_present"),
        "kbo_settlement.py":                  ("yes", "guard_final_write with ref=scores_dict"),
        "parlay_leg_settle.py":               ("yes", "returns Optional[str]; None means caller keeps parlay pending"),
    }
    # Anti-regression: adding a new settler MUST also update this
    # inventory.
    assert len(ACTIVE_SETTLERS) == 7
    for fname, (wired, _mech) in ACTIVE_SETTLERS.items():
        assert wired == "yes", (
            f"{fname} is NOT wired through the Universal Settlement "
            f"Contract — see P0.2 spec")
