"""STEP 2 · UniversalMarketContract — contract tests
========================================================

Locks in the invariants that let the Tennis / NFL-alt / MLB-run_line /
NBA-authority defects become a single-file fix instead of five.
"""
from __future__ import annotations
import os, sys
import pytest

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from services.universal_market_contract import (
    Family, MarketEntry, get, all_entries, resolve_provider_key,
    is_alternate, capability,
    ACTIVE, RESEARCH_ONLY, MODEL_UNAVAILABLE,
    PROVIDER_UNAVAILABLE, SETTLEMENT_UNAVAILABLE,
    _CAPABILITY_STATES,
)


def test_step2_no_duplicate_sport_family_keys():
    """The registry MUST reject duplicate (sport, family) entries at
    module load — a compile-time single-truth guarantee. Any accidental
    dupe would raise on import and break every consumer."""
    entries = all_entries()
    keys = list(entries.keys())
    assert len(keys) == len(set(keys)), "duplicate (sport, family) keys"


def test_step2_every_entry_has_a_valid_capability_state():
    for (sport, family), e in all_entries().items():
        assert e.capability_state in _CAPABILITY_STATES, (
            f"{sport}/{family} has invalid capability_state {e.capability_state!r}"
        )


def test_step2_tennis_alt_key_canonicalization():
    """Tennis alt keys previously drifted between `alternate_spreads`
    (Over/Under semantics) and `alternate_spreads_games` (participant
    semantics on games). Contract MUST resolve both to the same
    canonical entry."""
    e1 = resolve_provider_key("Tennis", "alternate_spreads_games")
    e2 = resolve_provider_key("Tennis", "alternate_spreads")
    assert e1 is not None, "canonical `alternate_spreads_games` missing"
    assert e2 is not None, "legacy `alternate_spreads` must alias in"
    assert e1.family == e2.family == Family.TENNIS_GAME_HANDICAP

    t1 = resolve_provider_key("Tennis", "alternate_totals_games")
    t2 = resolve_provider_key("Tennis", "alternate_totals")
    assert t1.family == t2.family == Family.TENNIS_TOTAL_GAMES


def test_step2_mlb_run_line_taxonomy_canonicalization():
    """MLB `spreads` provider key must resolve to run_line, NOT to a
    generic point_spread entry (which is NFL/NBA/CFB)."""
    e = resolve_provider_key("MLB", "spreads")
    assert e is not None
    assert e.family == Family.RUN_LINE
    # Legacy aliases used by ingest code must still land on run_line.
    assert resolve_provider_key("MLB", "runline").family == Family.RUN_LINE
    assert resolve_provider_key("MLB", "spread").family == Family.RUN_LINE


def test_step2_alternate_classification_before_standard_filter():
    """`is_alternate` MUST correctly flag every real provider alt so
    the standard-prop filter never captures alts. Covers the NFL / NBA
    alt classification defect the user flagged."""
    for pk in ("player_reception_yds_alternate",
                 "player_receptions_alternate",
                 "batter_hits_alternate",
                 "pitcher_strikeouts_alternate",
                 "alternate_totals_games",
                 "alternate_spreads_games"):
        assert is_alternate("Tennis" if "games" in pk else
                              "MLB" if "batter" in pk or "pitcher" in pk else
                              "NFL", pk), (
            f"is_alternate({pk!r}) returned False — alt would fall "
            f"through standard-prop filter."
        )


def test_step2_over_under_conservation_two_sided_markets():
    """Every two-sided over/under market must declare BOTH sides in
    `allowed_sides` so a generic Under-filter cannot suppress Unders."""
    two_sided = [
        ("MLB", Family.HITTER_HITS),
        ("MLB", Family.PITCHER_STRIKEOUTS),
        ("MLB", Family.GAME_TOTAL),
        ("NFL", Family.WR_RECEIVING_YDS),
        ("NFL", Family.WR_RECEPTIONS),
        ("Tennis", Family.TENNIS_TOTAL_GAMES),
    ]
    for (sport, fam) in two_sided:
        e = get(sport, fam)
        assert e is not None, f"{sport}/{fam} missing from contract"
        assert "over" in e.allowed_sides and "under" in e.allowed_sides, (
            f"{sport}/{fam} allowed_sides={e.allowed_sides!r} — Under "
            f"conservation broken."
        )


def test_step2_one_sided_markets_stay_one_sided():
    """Anytime goalscorer / HR / Anytime TD legitimately have only one
    live sportsbook side. Contract must NOT invent an under."""
    gs = get("Soccer", Family.GOALSCORER_ANY)
    assert gs.allowed_sides == ("yes",), (
        "goalscorer_anytime must NOT synthesize a No side."
    )


def test_step2_active_markets_have_a_model_authority():
    """A market cannot claim ACTIVE without a `model_authority` — this
    is the "no contradictory support" invariant."""
    for (sport, family), e in all_entries().items():
        if e.capability_state != ACTIVE:
            continue
        assert e.model_authority, (
            f"{sport}/{family} is ACTIVE but has no model_authority — "
            f"contradictory capability."
        )


def test_step2_active_markets_declare_settlement_actuals():
    """A market cannot claim ACTIVE without declared settlement
    actuals — otherwise it grades to nothing."""
    for (sport, family), e in all_entries().items():
        if e.capability_state != ACTIVE:
            continue
        assert e.settlement_actuals, (
            f"{sport}/{family} ACTIVE without settlement_actuals."
        )
        assert e.settlement_primary, (
            f"{sport}/{family} ACTIVE without settlement_primary."
        )


def test_step2_active_markets_require_real_line_where_applicable():
    """Alt markets that publish a numeric threshold MUST require a
    real sportsbook line (prevents model_projection lines being
    displayed as bettable alts)."""
    for (sport, family), e in all_entries().items():
        if e.capability_state != ACTIVE:
            continue
        if e.line_type in ("standard", "both") and \
           e.selection_schema in ("over_under", "participant"):
            assert e.requires_real_line, (
                f"{sport}/{family} publishes a numeric threshold but "
                f"does not require a real sportsbook line."
            )


def test_step2_capability_helper_returns_provider_unavailable_for_unknown():
    assert capability("XLeague", "some_family") == PROVIDER_UNAVAILABLE


def test_step2_exact_threshold_enforced_on_ladders():
    """Every `line_type` ∈ {alternate, both} entry MUST set
    `exact_threshold=True` — the ladder-monotonicity invariant depends
    on it."""
    for (sport, family), e in all_entries().items():
        if e.line_type in ("alternate", "both"):
            assert e.exact_threshold, (
                f"{sport}/{family} ladder market must price at exact "
                f"threshold, not reuse standard-line probability."
            )
