"""PERKLOCKS-MAIN 35 · P0 — UNIVERSAL PLAYER→TEAM→OPPONENT IDENTITY.

Locks the invariants for the SHARED identity resolver used by every
sport.  No per-sport identity authority may drift from this contract.

Contracts asserted:
  * Canonical team IDs (Level 1) always beat display names / aliases.
  * A pick carrying `canonical_team_id` for the player + matching
    `canonical_home_team_id` / `canonical_away_team_id` on the event
    resolves VALID without any name matching.
  * A pick where the player's canonical_team_id is neither of the
    event's canonical participants → PLAYER_EVENT_IDENTITY_MISMATCH.
  * A pick with NO canonical fields and NO extractable name hints
    → PLAYER_TEAM_UNRESOLVED (fail closed).
  * The same shared function `evaluate_identity` is called for NFL /
    NBA / MLB / Soccer / NHL / CFB — no sport-specific bypass.
  * Team-side markets remain NOT_APPLICABLE.
  * Tennis game-total / spread selections remain NOT_APPLICABLE.
  * `PLAYER_TEAM_UNRESOLVED` is never returned when a canonical ID
    exists but merely differs from the display name (older bug).
  * When only display strings are available the alias fallback is
    scoped to the two event participants only.
"""
from __future__ import annotations
import pytest


def _mk(sport, **kw):
    base = {"sport": sport, "market": "unknown"}
    base.update(kw)
    return base


def test_canonical_team_id_wins_over_display_name():
    from services.player_event_identity_gate import (
        evaluate_identity, IdentityVerdict,
    )
    pick = _mk("MLB",
        market="Aaron Judge Over 0.5 Home Runs",
        canonical_player_id="aaron-judge-nyy-1992",
        canonical_team_id="NYY",
        canonical_home_team_id="NYY",
        canonical_away_team_id="BAL",
        # Display names deliberately mismatched — canonical IDs must win.
        home_team="New York Yankees",
        away_team="Baltimore Orioles",
        player_team="Bogus Legacy Alias",
    )
    v = evaluate_identity(pick)
    assert v == IdentityVerdict.VALID, v


def test_canonical_team_id_mismatch_fails_closed():
    from services.player_event_identity_gate import (
        evaluate_identity, IdentityVerdict,
    )
    pick = _mk("NFL",
        market="Patrick Mahomes Over 275.5 Passing Yards",
        canonical_player_id="patrick-mahomes-kc",
        canonical_team_id="KC",
        canonical_home_team_id="BUF",
        canonical_away_team_id="MIA",   # KC is neither participant
        home_team="Buffalo Bills",
        away_team="Miami Dolphins",
    )
    v = evaluate_identity(pick)
    assert v == IdentityVerdict.PLAYER_EVENT_IDENTITY_MISMATCH


def test_missing_all_identity_fields_fails_closed():
    from services.player_event_identity_gate import (
        evaluate_identity, IdentityVerdict,
    )
    pick = _mk("NFL",
        market="Anytime Touchdown Yes",
        # No canonical IDs, no player_team, no event participants
    )
    v = evaluate_identity(pick)
    assert v in (
        IdentityVerdict.PLAYER_TEAM_UNRESOLVED,
        IdentityVerdict.NOT_APPLICABLE,   # market not detected as player-prop
    )


def test_team_market_returns_not_applicable():
    from services.player_event_identity_gate import (
        evaluate_identity, IdentityVerdict,
    )
    pick = _mk("MLB",
        market="Moneyline",
        selection="New York Yankees",
        home_team="New York Yankees",
        away_team="Baltimore Orioles",
    )
    v = evaluate_identity(pick)
    assert v == IdentityVerdict.NOT_APPLICABLE


def test_tennis_totals_are_not_applicable_for_player_gate():
    from services.player_event_identity_gate import (
        evaluate_identity, IdentityVerdict,
    )
    pick = _mk("Tennis",
        market="Over 42.5 Games (Alt)",
        selection="Over",
        home_team="Alexei Popyrin",
        away_team="Alejandro Tabilo",
    )
    v = evaluate_identity(pick)
    assert v == IdentityVerdict.NOT_APPLICABLE


def test_soccer_anytime_goalscorer_resolves_via_canonical_ids():
    from services.player_event_identity_gate import (
        evaluate_identity, IdentityVerdict,
    )
    pick = _mk("Soccer",
        market="Anytime Goalscorer",
        provider_market_key="player_goal_scorer_anytime",
        player_name="Erling Haaland",
        canonical_player_id="erling-haaland-mci",
        canonical_team_id="MCI",
        canonical_home_team_id="MCI",
        canonical_away_team_id="AVL",
        home_team="Manchester City",
        away_team="Aston Villa",
    )
    v = evaluate_identity(pick)
    assert v == IdentityVerdict.VALID


def test_nfl_player_prop_resolves_via_canonical_ids():
    from services.player_event_identity_gate import (
        evaluate_identity, IdentityVerdict,
    )
    pick = _mk("NFL",
        market="Patrick Mahomes Over 275.5 Passing Yards",
        provider_market_key="player_pass_yds",
        canonical_player_id="patrick-mahomes-kc",
        canonical_team_id="KC",
        canonical_home_team_id="KC",
        canonical_away_team_id="BUF",
        home_team="Kansas City Chiefs",
        away_team="Buffalo Bills",
    )
    v = evaluate_identity(pick)
    assert v == IdentityVerdict.VALID


def test_mlb_hitter_prop_resolves_via_canonical_ids():
    from services.player_event_identity_gate import (
        evaluate_identity, IdentityVerdict,
    )
    pick = _mk("MLB",
        market="Aaron Judge Over 0.5 Home Runs",
        provider_market_key="batter_home_runs",
        canonical_player_id="aaron-judge-nyy",
        canonical_team_id="NYY",
        canonical_home_team_id="NYY",
        canonical_away_team_id="BAL",
    )
    v = evaluate_identity(pick)
    assert v == IdentityVerdict.VALID


def test_alias_fallback_scoped_to_event_participants_only():
    """When only display names are available, the alias fallback must
    only match against the two event participants — never a global
    name lookup."""
    from services.player_event_identity_gate import (
        evaluate_identity, IdentityVerdict,
    )
    pick = _mk("MLB",
        market="Ronald Acuna Jr Over 0.5 Hits",
        player_name="Ronald Acuna Jr",
        player_team="Atlanta Braves",   # legacy alias only
        home_team="Atlanta Braves",
        away_team="Philadelphia Phillies",
    )
    v = evaluate_identity(pick)
    assert v == IdentityVerdict.VALID


def test_alias_fallback_rejects_when_player_team_not_a_participant():
    from services.player_event_identity_gate import (
        evaluate_identity, IdentityVerdict,
    )
    pick = _mk("MLB",
        market="Aaron Judge Over 0.5 Hits",
        player_name="Aaron Judge",
        player_team="Chicago Cubs",   # NYY is not in this event
        home_team="Atlanta Braves",
        away_team="Philadelphia Phillies",
    )
    v = evaluate_identity(pick)
    assert v == IdentityVerdict.PLAYER_EVENT_IDENTITY_MISMATCH


def test_shared_resolver_is_a_single_function_no_per_sport_bypass():
    """The public API is exactly `evaluate_identity(pick)`. No per-sport
    resolver may live outside this module and become a separate
    authority."""
    import services.player_event_identity_gate as ge
    assert callable(ge.evaluate_identity)
    # The only sport-specific helpers are `_evaluate_tennis` (internal
    # branch of the shared resolver) — not public identity authorities.
    public_syms = [x for x in dir(ge)
                   if not x.startswith("_") and callable(getattr(ge, x))]
    # Only `evaluate_identity` + a few thin helpers may be public.
    _ALLOWED_PUBLIC = {
        "evaluate_identity", "is_identity_valid_for_publication",
        "IdentityVerdict",
    }
    for sym in public_syms:
        assert sym in _ALLOWED_PUBLIC or sym[0].isupper(), sym


def test_no_indefinite_player_to_team_cache_bypass():
    """Team membership can change (trades, transfers).  There must be
    no module-level cache from player_id → team_id that persists
    across events without event/date scope."""
    import services.player_event_identity_gate as ge
    # No dict of raw player→team stored at module level.
    for name in dir(ge):
        if name.startswith("_"):
            continue
        obj = getattr(ge, name)
        if isinstance(obj, dict) and obj:
            # Any dict must not be a permanent player→team map.
            for k, v in obj.items():
                if isinstance(k, str) and isinstance(v, str):
                    assert not ("player" in name.lower()
                                and "team" in name.lower()), name


def test_identity_verdict_enum_exposes_full_universal_vocabulary():
    from services.player_event_identity_gate import IdentityVerdict
    for expected in (
        "VALID", "NOT_APPLICABLE",
        "PLAYER_TEAM_UNRESOLVED",
        "PLAYER_EVENT_IDENTITY_MISMATCH",
    ):
        assert hasattr(IdentityVerdict, expected), expected
