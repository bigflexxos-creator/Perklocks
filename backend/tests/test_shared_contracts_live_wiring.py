"""Integration tests — proof that NEW shared contracts are now wired
into the REAL runtime paths (not just declaration-only modules).

This file asserts PRESENCE and INVOCATION of the new modules inside
specific existing engine files, so that a future refactor cannot
silently un-wire the shared contracts.
"""
from __future__ import annotations

import inspect
import pytest


# ── 1. soccer_prop_inject calls harden_lineage ───────────────────────
def test_soccer_prop_inject_imports_quote_lineage_guard():
    import services.soccer_prop_inject as mod
    src = inspect.getsource(mod)
    assert "from services.quote_lineage_guard import harden_lineage" in src, (
        "soccer_prop_inject must import harden_lineage")
    assert "harden_lineage(" in src, (
        "soccer_prop_inject must INVOKE harden_lineage, not just import it")


def test_soccer_prop_inject_fetches_per_bookmaker_quotes():
    """The producer must now call _fetch_event_book_quotes (which
    retains bookmaker lineage) BEFORE emitting picks."""
    import services.soccer_prop_inject as mod
    assert hasattr(mod, "_fetch_event_book_quotes"), (
        "soccer_prop_inject must expose _fetch_event_book_quotes")
    src = inspect.getsource(mod)
    assert "_fetch_event_book_quotes(sport_key, event_id)" in src, (
        "The producer must call _fetch_event_book_quotes per event")


def test_soccer_prop_inject_attaches_bookmaker_field():
    """Emitted picks must carry an explicit `bookmaker` field — not
    just book_odds — so the canonical publication boundary sees
    lineage."""
    import services.soccer_prop_inject as mod
    src = inspect.getsource(mod)
    assert '"bookmaker": _bookmaker_val' in src, (
        'emitted pick must set "bookmaker": _bookmaker_val')


# ── 2. pick_refresh_orchestrator wires season_active discovery ────────
def test_orchestrator_imports_season_active():
    import services.pick_refresh_orchestrator as mod
    src = inspect.getsource(mod)
    assert "from services.season_active_discovery import season_active" in src, (
        "orchestrator must import season_active_discovery")
    assert "await season_active(db, _s)" in src, (
        "orchestrator must INVOKE season_active per sport")


# ── 3. pick_refresh_orchestrator wires soccer_ags_freshness_isolator ──
def test_orchestrator_wires_soccer_ags_freshness_isolator():
    import services.pick_refresh_orchestrator as mod
    src = inspect.getsource(mod)
    assert "from services.soccer_ags_freshness_isolator import" in src, (
        "orchestrator must import the AGS freshness isolator")
    assert "soccer_player_freshness(db)" in src
    assert "ags_needs_refresh(" in src


# ── 4. HI projectors already wired (regression) ──────────────────────
def test_orchestrator_wires_hi_projectors():
    import services.pick_refresh_orchestrator as mod
    src = inspect.getsource(mod)
    assert "from services.historical_intelligence_projectors import" in src
    assert "project_sport as _hi_project_sport" in src
    assert "_hi_project_sport(db, _s)" in src


# ── 5. HI freshness verifier already wired (regression) ──────────────
def test_orchestrator_wires_hi_freshness_verifier():
    import services.pick_refresh_orchestrator as mod
    src = inspect.getsource(mod)
    assert "from services.hi_freshness_verifier import" in src
    assert "verify_hi_freshness as _hi_verify" in src


# ── 6. ObservedQuote validator is importable and enforces lineage ────
def test_quote_lineage_guard_rejects_median_source():
    from services.quote_lineage_guard import harden_lineage
    quotes = [{
        "sport": "soccer", "event_id": "e1",
        "provider_event_id": "e1",
        "provider_market_key": "player_goal_scorer_anytime",
        "market_family": "goalscorer_anytime",
        "line": None, "side": "yes",
        "bookmaker": "median", "price": -140,
        "odds_source": "provider_median",
        "observed_at": "2026-06-20T18:00:00Z",
    }]
    hard = harden_lineage(quotes, market_family="goalscorer_anytime",
                           event_id="e1", sport="soccer")
    assert hard["no_real_book_line"] is True
    assert hard["bookmaker"] is None


# ── 7. quote_lineage_guard SELECTS a real bookmaker when present ─────
def test_quote_lineage_guard_selects_fanduel_over_other_books():
    from services.quote_lineage_guard import harden_lineage
    quotes = [
        {"sport": "soccer", "event_id": "e1", "provider_event_id": "e1",
         "provider_market_key": "player_goal_scorer_anytime",
         "market_family": "goalscorer_anytime", "line": None, "side": "yes",
         "bookmaker": "betmgm", "price": +150,
         "odds_source": "the_odds_api",
         "observed_at": "2026-06-20T18:00:00Z"},
        {"sport": "soccer", "event_id": "e1", "provider_event_id": "e1",
         "provider_market_key": "player_goal_scorer_anytime",
         "market_family": "goalscorer_anytime", "line": None, "side": "yes",
         "bookmaker": "fanduel", "price": +140,
         "odds_source": "the_odds_api",
         "observed_at": "2026-06-20T18:00:00Z"},
    ]
    hard = harden_lineage(quotes, market_family="goalscorer_anytime",
                           event_id="e1", sport="soccer")
    assert hard["no_real_book_line"] is False
    assert hard["bookmaker"] == "fanduel"
    assert hard["book_odds"] == 140
    assert hard["odds_source"] == "the_odds_api"


# ── 8. universal funnel telemetry diagnoses dominant SYNTHETIC_BOOK ──
def test_universal_funnel_blames_right_stage():
    from services.universal_funnel_telemetry import diagnose_funnel
    funnel = {
        "provider_events": 5, "provider_outcomes": 20,
        "canonicalized": 15, "model_candidates": 10,
        "at_or_above_85": 4, "published": 0,
        "rejections": {"SYNTHETIC_BOOK_ODDS": 4},
    }
    assert diagnose_funnel(funnel) == "PUBLICATION_PROVENANCE_FAILURE"


# ── 9. preservation invariants still hold after wiring ────────────────
def test_preservation_invariants_still_hold():
    from services.preservation_contract import snapshot
    snap = snapshot()
    assert snap.get("APEX_MIN_BASE_SCORE") == 97.0
    assert snap.get("APEX_MIN_POSITIVE_CATEGORIES") == 5
    assert "provider_median" in (snap.get("_SYNTHETIC_ODDS_SOURCES") or [])
    assert "provider_median" not in (snap.get("_REAL_ODDS_SOURCES") or [])


# ── 10. canonical_publication_boundary SYNTHETIC_BOOK_ODDS enum intact
def test_canonical_boundary_synthetic_rejection_still_enforced():
    from services.canonical_publication_boundary import RejectionReason
    assert RejectionReason.SYNTHETIC_BOOK_ODDS.value == "SYNTHETIC_BOOK_ODDS"


if __name__ == "__main__":
    import subprocess, sys
    sys.exit(subprocess.call(["python", "-m", "pytest", __file__, "-v"]))
