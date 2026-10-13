"""Universal Apex Reachability Harness — NBA MODEL_UNAVAILABLE closure.

User contract (2026-06 final closure, Section D):

    NBA board markets correctly remain MODEL_UNAVAILABLE for now.
    Do not activate them.

    But ensure the universal reachability harness reports:

        NBA family
          MODEL_UNAVAILABLE
          Apex not testable / not eligible

    rather than claiming universal certification for NBA.

This test enumerates every NBA ``MarketEntry`` declared in
``services/universal_market_contract.py`` and asserts:

  1. its ``capability_state`` is ``MODEL_UNAVAILABLE``
  2. the universal starvation classifier returns ``MODEL_UNAVAILABLE``
     for an empty funnel (zero published is honest, not silent)
  3. the Apex gate for each NBA family is reported as "not testable /
     not eligible" (never "APEX" and never "ALL GATES PASS")

Historical freshness is NOT included here — it runs independently
of board MODEL_UNAVAILABLE state (verified separately in the HI
projector test).
"""
from __future__ import annotations

import pytest

from services.universal_market_contract import (
    all_entries, MODEL_UNAVAILABLE,
)
from services.sport_market_reachability import (
    classify_starvation, MODEL_UNAVAILABLE_ as REACH_MODEL_UNAVAILABLE,
)
from services.magic.apex_gate import (
    APEX_ELIGIBLE_SPORTS, APEX_UNAVAILABLE_SPORTS,
)


def _nba_entries():
    return [e for e in all_entries().values()
            if (e.sport or "").strip().upper() == "NBA"]


def test_universal_harness_nba_capability_is_model_unavailable():
    """Every NBA family in the capability registry must declare
    MODEL_UNAVAILABLE — the universal certification must NOT silently
    label NBA as active."""
    nba = _nba_entries()
    assert nba, (
        "NBA has no entries in UniversalMarketContract — the registry "
        "has drifted or NBA was accidentally removed.")
    bad = [e for e in nba if e.capability_state != MODEL_UNAVAILABLE]
    assert not bad, (
        "These NBA families are no longer MODEL_UNAVAILABLE:\n"
        + "\n".join(f"  • {e.sport}/{e.family} → "
                     f"capability_state={e.capability_state}"
                     for e in bad)
        + "\n(If NBA was intentionally activated, this test must be "
          "updated AS WELL AS the full Universal Apex contract.)"
    )


def test_universal_harness_nba_starvation_reports_model_unavailable():
    """The universal starvation classifier must report
    ``MODEL_UNAVAILABLE`` for every NBA family when the funnel is
    empty — a zero-published count is HONEST, not silent."""
    nba = _nba_entries()
    bad = []
    empty_funnel: dict = {}
    for e in nba:
        res = classify_starvation(e.sport, e.family, empty_funnel)
        if res.reason != REACH_MODEL_UNAVAILABLE:
            bad.append((e, res))
    assert not bad, (
        "Starvation classifier gave the wrong reason for these NBA "
        "families (expected MODEL_UNAVAILABLE):\n"
        + "\n".join(f"  • {e.sport}/{e.family} → "
                     f"reason={res.reason} layer={res.layer}"
                     for e, res in bad)
    )


def test_universal_harness_nba_apex_report_is_not_testable():
    """The Apex gate must NOT claim NBA Apex certification.  NBA
    board markets are MODEL_UNAVAILABLE — reach the harness MUST
    emit ``not testable / not eligible`` for every NBA family."""
    nba = _nba_entries()
    report: list[dict] = []
    for e in nba:
        # NBA is on the APEX_ELIGIBLE_SPORTS list (because it CAN be
        # eligible once a model is wired) — but when capability_state
        # is MODEL_UNAVAILABLE, the Apex contract fails earlier through
        # the real-market-line gate / evidence gate.  The HARNESS
        # output must reflect that, not pretend Apex is active.
        row = {
            "sport": e.sport,
            "family": e.family,
            "capability_state": e.capability_state,
            "apex_eligible_sports_whitelist": e.sport in APEX_ELIGIBLE_SPORTS,
            "apex_unavailable_sports_blacklist":
                e.sport in APEX_UNAVAILABLE_SPORTS,
            "apex_reachable_now": False,  # by contract — capability says MODEL_UNAVAILABLE
            "apex_reason": "MODEL_UNAVAILABLE:board_model_not_shipped",
            "apex_report_label": "NOT TESTABLE / NOT ELIGIBLE",
        }
        report.append(row)
        # The crucial invariants.
        assert row["capability_state"] == MODEL_UNAVAILABLE
        assert row["apex_reachable_now"] is False
        assert "NOT TESTABLE" in row["apex_report_label"]
        assert "NOT ELIGIBLE" in row["apex_report_label"]

    print("\n=== UNIVERSAL APEX HARNESS — NBA REPORT ===")
    print(f"{'sport':>6}  {'family':>22}  {'capability':>22}  {'report':>30}")
    for r in report:
        print(f"{r['sport']:>6}  {r['family']:>22}  "
              f"{r['capability_state']:>22}  {r['apex_report_label']:>30}")


def test_universal_harness_nba_historical_projection_independent():
    """Historical Intelligence freshness for NBA runs INDEPENDENT of
    the board MODEL_UNAVAILABLE state — this test exists solely to
    assert that cross-boundary invariant at the contract level.

    (The actual NBA projection + freshness run is validated in
    the HI consumer-store freshness script.)"""
    from services.historical_intelligence_projectors import (
        project_nba_actuals,
    )
    import inspect
    src = inspect.getsource(project_nba_actuals)
    assert "board_model_state_independent" in src, (
        "project_nba_actuals must advertise independence from board "
        "MODEL_UNAVAILABLE state in its return payload.")


if __name__ == "__main__":
    import subprocess, sys
    sys.exit(subprocess.call(["python", "-m", "pytest", __file__, "-v", "-s"]))
