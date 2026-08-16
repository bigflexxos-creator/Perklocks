"""Post-Phase-10 audit fix — canonical-key player-market identity closure.

Independent acceptance audit found that
``services.player_event_identity_gate._is_player_market()`` recognised
human-readable display labels (``Anytime Goal Scorer``, ``Passing Yards``)
but not provider / canonical market keys (``player_goal_scorer_anytime``,
``player_pass_yds``).  A player prop expressed with the canonical key
bypassed the identity gate and was silently classified as
NOT_APPLICABLE, allowing a wrong-team player prop to publish.

This suite proves the bypass is closed by:

1. Reproducing the exact audit fixture (Vinicius Jr @ Athletic Bilbao vs
   Barcelona with market_key=``player_goal_scorer_anytime``) and asserting
   PLAYER_EVENT_IDENTITY_MISMATCH → REJECTED across every gate.

2. Repeating the negative fixture for every currently-supported
   canonical player-market key across MLB / NFL / NBA / Soccer.

3. Positive controls proving that VALID canonical-key player props
   still publish (no over-rejection).

4. Confirming the ``market`` field itself carrying a canonical key is
   also recognised (some soccer feeds do this).

5. Confirming the ``market_key`` and ``provider_market_key`` fallbacks
   are read.
"""
from __future__ import annotations

import pytest

from services.player_event_identity_gate import (
    IdentityVerdict, evaluate_identity, is_identity_valid_for_publication,
)
from services.canonical_publication_boundary import (
    evaluate_publication, PublicationState, RejectionReason,
)
from services.main_board_eligibility import (
    is_canonical_eligible, is_main_board_eligible,
)


# ═══════════════════════════════════════════════════════════════════════
# Base fixture — publication-ready except for the identity mismatch.
# ═══════════════════════════════════════════════════════════════════════
def _wrong_team_pick(*, sport: str,
                     event: str, home_team: str, away_team: str,
                     market_key: str,
                     selection: str, player_name: str,
                     player_team: str,
                     use_market_field: bool = False,
                     use_provider_market_key: bool = False,
                     book_odds: int = +150,
                     **overrides) -> dict:
    """Build a pick shaped like a real producer output.

    ``use_market_field`` — put the canonical key into ``market`` too
    (some soccer feeds do this).
    ``use_provider_market_key`` — additionally stamp
    ``provider_market_key`` (Odds API-style).
    """
    pick = {
        "id": f"audit_neg_{market_key}",
        "sport": sport,
        "event": event,
        "home_team": home_team,
        "away_team": away_team,
        "event_id": f"evt_audit_{market_key}",
        "selection": selection,
        "player_name": player_name,
        "player_team": player_team,        # wrong team!
        "book_odds": book_odds,
        "odds_source": "the_odds_api",
        "implied_probability": 0.55,
        "model_probability": 0.60,
        "lock_score": 98.0,
        "published_lock_score": 98.0,
        "win_probability": 62.0,
        "edge_percent": 4.5,
        "identity_class": "AUTHORITATIVE",
        "market_key": market_key,
    }
    if use_market_field:
        pick["market"] = market_key
    else:
        pick["market"] = market_key.replace("_", " ").title()
    if use_provider_market_key:
        pick["provider_market_key"] = market_key
    pick.update(overrides)
    return pick


def _right_team_pick(*, sport: str,
                     event: str, home_team: str, away_team: str,
                     market_key: str,
                     selection: str, player_name: str,
                     player_team: str,   # THIS time correct
                     use_market_field: bool = False,
                     book_odds: int = -140,
                     **overrides) -> dict:
    pick = _wrong_team_pick(
        sport=sport, event=event, home_team=home_team,
        away_team=away_team, market_key=market_key,
        selection=selection, player_name=player_name,
        player_team=player_team,
        use_market_field=use_market_field,
        book_odds=book_odds, **overrides,
    )
    pick["id"] = f"audit_pos_{market_key}"
    return pick


# ═══════════════════════════════════════════════════════════════════════
# Audit reproduction fixture — must reject
# ═══════════════════════════════════════════════════════════════════════
AUDIT_REPRODUCTION = _wrong_team_pick(
    sport="soccer",
    event="Athletic Bilbao @ Barcelona",
    home_team="Barcelona", away_team="Athletic Bilbao",
    market_key="player_goal_scorer_anytime",
    selection="Vinicius Jr", player_name="Vinicius Jr",
    player_team="Real Madrid",       # not in this fixture!
    use_market_field=True,           # market == canonical key
    use_provider_market_key=True,
    book_odds=+180,
)


def test_audit_reproduction_identity_now_mismatch():
    """Exact fixture from the independent audit — was NOT_APPLICABLE
    before the fix; must now be PLAYER_EVENT_IDENTITY_MISMATCH."""
    assert evaluate_identity(AUDIT_REPRODUCTION) == \
        IdentityVerdict.PLAYER_EVENT_IDENTITY_MISMATCH


def test_audit_reproduction_publication_rejected():
    verdict = evaluate_publication(AUDIT_REPRODUCTION)
    assert verdict.state == PublicationState.REJECTED
    assert RejectionReason.PLAYER_EVENT_IDENTITY_MISMATCH.value \
        in verdict.reasons


def test_audit_reproduction_canonical_eligibility_false():
    assert is_canonical_eligible(AUDIT_REPRODUCTION) is False


def test_audit_reproduction_main_board_eligibility_false():
    assert is_main_board_eligible(AUDIT_REPRODUCTION) is False


# ═══════════════════════════════════════════════════════════════════════
# Cross-sport canonical-key NEGATIVE matrix
# ═══════════════════════════════════════════════════════════════════════
NEGATIVE_CANONICAL_KEY_FIXTURES = [
    # ── Soccer ──────────────────────────────────────────────────────
    ("soccer_player_goal_scorer_anytime", _wrong_team_pick(
        sport="soccer",
        event="Athletic Bilbao @ Barcelona",
        home_team="Barcelona", away_team="Athletic Bilbao",
        market_key="player_goal_scorer_anytime",
        selection="Vinicius Jr", player_name="Vinicius Jr",
        player_team="Real Madrid",
        use_market_field=True,
    )),
    ("soccer_player_to_score_or_assist", _wrong_team_pick(
        sport="soccer",
        event="Arsenal @ Chelsea",
        home_team="Chelsea", away_team="Arsenal",
        market_key="player_to_score_or_assist",
        selection="Erling Haaland", player_name="Erling Haaland",
        player_team="Manchester City",
        use_provider_market_key=True,
    )),
    # ── NFL ─────────────────────────────────────────────────────────
    ("nfl_player_pass_yds", _wrong_team_pick(
        sport="nfl",
        event="Chiefs @ Ravens",
        home_team="Ravens", away_team="Chiefs",
        market_key="player_pass_yds",
        selection="Josh Allen Over 249.5", player_name="Josh Allen",
        player_team="Bills",
    )),
    ("nfl_player_rush_yds", _wrong_team_pick(
        sport="nfl",
        event="Bills @ Dolphins",
        home_team="Dolphins", away_team="Bills",
        market_key="player_rush_yds",
        selection="Saquon Barkley Over 79.5", player_name="Saquon Barkley",
        player_team="Eagles",
        use_market_field=True,
    )),
    ("nfl_player_reception_yds_alt", _wrong_team_pick(
        sport="nfl",
        event="Chiefs @ Ravens",
        home_team="Ravens", away_team="Chiefs",
        market_key="player_reception_yds_alternate",
        selection="Justin Jefferson Over 89.5", player_name="Justin Jefferson",
        player_team="Vikings",
    )),
    ("nfl_player_anytime_td", _wrong_team_pick(
        sport="nfl",
        event="Cowboys @ Giants",
        home_team="Giants", away_team="Cowboys",
        market_key="player_anytime_td",
        selection="Christian McCaffrey", player_name="Christian McCaffrey",
        player_team="49ers",
        use_market_field=True,
    )),
    # ── MLB ─────────────────────────────────────────────────────────
    ("mlb_pitcher_strikeouts", _wrong_team_pick(
        sport="mlb",
        event="Cubs @ Mets",
        home_team="Mets", away_team="Cubs",
        market_key="pitcher_strikeouts",
        selection="Gerrit Cole Over 6.5", player_name="Gerrit Cole",
        player_team="Yankees",
    )),
    ("mlb_batter_hits", _wrong_team_pick(
        sport="mlb",
        event="Astros @ Rangers",
        home_team="Rangers", away_team="Astros",
        market_key="batter_hits",
        selection="Aaron Judge Over 0.5", player_name="Aaron Judge",
        player_team="Yankees",
        use_provider_market_key=True,
    )),
    ("mlb_batter_total_bases", _wrong_team_pick(
        sport="mlb",
        event="Astros @ Rangers",
        home_team="Rangers", away_team="Astros",
        market_key="batter_total_bases",
        selection="Aaron Judge Over 1.5", player_name="Aaron Judge",
        player_team="Yankees",
    )),
    ("mlb_batter_home_runs_alt", _wrong_team_pick(
        sport="mlb",
        event="Cubs @ Mets",
        home_team="Mets", away_team="Cubs",
        market_key="batter_home_runs_alternate",
        selection="Aaron Judge Over 0.5", player_name="Aaron Judge",
        player_team="Yankees",
    )),
    # ── NBA ─────────────────────────────────────────────────────────
    ("nba_player_points", _wrong_team_pick(
        sport="nba",
        event="Lakers @ Warriors",
        home_team="Warriors", away_team="Lakers",
        market_key="player_points",
        selection="Jayson Tatum Over 27.5", player_name="Jayson Tatum",
        player_team="Celtics",
    )),
    ("nba_player_rebounds", _wrong_team_pick(
        sport="nba",
        event="Heat @ Bucks",
        home_team="Bucks", away_team="Heat",
        market_key="player_rebounds",
        selection="Nikola Jokic Over 11.5", player_name="Nikola Jokic",
        player_team="Nuggets",
        use_market_field=True,
    )),
    ("nba_player_points_rebounds_assists", _wrong_team_pick(
        sport="nba",
        event="Lakers @ Warriors",
        home_team="Warriors", away_team="Lakers",
        market_key="player_points_rebounds_assists",
        selection="Nikola Jokic Over 49.5", player_name="Nikola Jokic",
        player_team="Nuggets",
        use_provider_market_key=True,
    )),
    ("nba_player_assists_alt", _wrong_team_pick(
        sport="nba",
        event="Heat @ Bucks",
        home_team="Bucks", away_team="Heat",
        market_key="player_assists_alternate",
        selection="Chris Paul Over 6.5", player_name="Chris Paul",
        player_team="Suns",
    )),
]


@pytest.mark.parametrize("name,pick", NEGATIVE_CANONICAL_KEY_FIXTURES,
                         ids=[f[0] for f in NEGATIVE_CANONICAL_KEY_FIXTURES])
def test_canonical_key_identity_mismatch_detected(name: str, pick: dict):
    """Every canonical-key wrong-team fixture must be flagged
    PLAYER_EVENT_IDENTITY_MISMATCH (not NOT_APPLICABLE)."""
    v = evaluate_identity(pick)
    assert v == IdentityVerdict.PLAYER_EVENT_IDENTITY_MISMATCH, (
        f"[{name}] canonical-key player market bypassed identity gate: "
        f"got {v.value} instead of PLAYER_EVENT_IDENTITY_MISMATCH"
    )


@pytest.mark.parametrize("name,pick", NEGATIVE_CANONICAL_KEY_FIXTURES,
                         ids=[f[0] for f in NEGATIVE_CANONICAL_KEY_FIXTURES])
def test_canonical_key_publication_rejected(name: str, pick: dict):
    v = evaluate_publication(pick)
    assert v.state == PublicationState.REJECTED, (
        f"[{name}] publication must REJECT canonical-key mismatch; "
        f"got state={v.state.value} reasons={v.reasons}"
    )
    assert RejectionReason.PLAYER_EVENT_IDENTITY_MISMATCH.value \
        in v.reasons, (
        f"[{name}] publication rejection missing identity reason: "
        f"{v.reasons}"
    )


@pytest.mark.parametrize("name,pick", NEGATIVE_CANONICAL_KEY_FIXTURES,
                         ids=[f[0] for f in NEGATIVE_CANONICAL_KEY_FIXTURES])
def test_canonical_key_canonical_eligibility_false(name: str, pick: dict):
    assert is_canonical_eligible(pick) is False, (
        f"[{name}] canonical eligibility must reject canonical-key mismatch"
    )


@pytest.mark.parametrize("name,pick", NEGATIVE_CANONICAL_KEY_FIXTURES,
                         ids=[f[0] for f in NEGATIVE_CANONICAL_KEY_FIXTURES])
def test_canonical_key_main_board_eligibility_false(name: str, pick: dict):
    """Defense-in-depth: even if some upstream stage misses it, the
    main-board eligibility gate rejects identity mismatches."""
    assert is_main_board_eligible(pick) is False, (
        f"[{name}] main-board eligibility must reject canonical-key mismatch"
    )


# ═══════════════════════════════════════════════════════════════════════
# Positive controls — VALID canonical-key player props still publish
# ═══════════════════════════════════════════════════════════════════════
POSITIVE_CANONICAL_KEY_FIXTURES = [
    ("soccer_valid_player_goal_scorer_anytime", _right_team_pick(
        sport="soccer",
        event="Athletic Bilbao @ Barcelona",
        home_team="Barcelona", away_team="Athletic Bilbao",
        market_key="player_goal_scorer_anytime",
        selection="Robert Lewandowski", player_name="Robert Lewandowski",
        player_team="Barcelona",
        use_market_field=True,
    )),
    ("nfl_valid_player_pass_yds", _right_team_pick(
        sport="nfl",
        event="Chiefs @ Ravens",
        home_team="Ravens", away_team="Chiefs",
        market_key="player_pass_yds",
        selection="Patrick Mahomes Over 249.5", player_name="Patrick Mahomes",
        player_team="Chiefs",
    )),
    ("mlb_valid_pitcher_strikeouts", _right_team_pick(
        sport="mlb",
        event="Yankees @ Red Sox",
        home_team="Red Sox", away_team="Yankees",
        market_key="pitcher_strikeouts",
        selection="Gerrit Cole Over 6.5", player_name="Gerrit Cole",
        player_team="Yankees",
        use_provider_market_key=True,
    )),
    ("nba_valid_player_points", _right_team_pick(
        sport="nba",
        event="Celtics @ Nuggets",
        home_team="Nuggets", away_team="Celtics",
        market_key="player_points",
        selection="Jayson Tatum Over 28.5", player_name="Jayson Tatum",
        player_team="Celtics",
    )),
]


@pytest.mark.parametrize("name,pick", POSITIVE_CANONICAL_KEY_FIXTURES,
                         ids=[f[0] for f in POSITIVE_CANONICAL_KEY_FIXTURES])
def test_canonical_key_positive_control_valid(name: str, pick: dict):
    v = evaluate_identity(pick)
    assert v == IdentityVerdict.VALID, (
        f"[{name}] valid canonical-key player prop must remain VALID; got {v}"
    )


@pytest.mark.parametrize("name,pick", POSITIVE_CANONICAL_KEY_FIXTURES,
                         ids=[f[0] for f in POSITIVE_CANONICAL_KEY_FIXTURES])
def test_canonical_key_positive_control_publication_not_id_blocked(
        name: str, pick: dict):
    v = evaluate_publication(pick)
    assert RejectionReason.PLAYER_EVENT_IDENTITY_MISMATCH.value \
        not in v.reasons
    assert RejectionReason.PLAYER_TEAM_UNRESOLVED.value \
        not in v.reasons


# ═══════════════════════════════════════════════════════════════════════
# Regression sanity — Phase 9 display-label suite still holds
# ═══════════════════════════════════════════════════════════════════════
def test_display_label_gate_still_works():
    """Sanity: the pre-existing display-label detection ("Anytime Goal
    Scorer", "Passing Yards", …) continues to work — no regression from
    adding the canonical-key branch."""
    pick_bad_display = {
        "id": "display_neg",
        "sport": "soccer",
        "event": "Athletic Bilbao @ Barcelona",
        "home_team": "Barcelona", "away_team": "Athletic Bilbao",
        "event_id": "evt_display_neg",
        "market": "Anytime Goal Scorer",     # display label form
        "selection": "Vinicius Jr", "player_name": "Vinicius Jr",
        "player_team": "Real Madrid",
        "book_odds": +180,
        "odds_source": "the_odds_api",
        "implied_probability": 0.55,
        "model_probability": 0.60,
        "identity_class": "AUTHORITATIVE",
    }
    assert evaluate_identity(pick_bad_display) == \
        IdentityVerdict.PLAYER_EVENT_IDENTITY_MISMATCH


# ═══════════════════════════════════════════════════════════════════════
# Documentation cleanup verification — no stale "fail-open" comments
# ═══════════════════════════════════════════════════════════════════════
def test_docstring_no_longer_says_fail_open():
    """Optional cleanup — the module docstring must no longer contain
    the stale 'Fail-open' phrase after Phase 10A tightening."""
    import services.player_event_identity_gate as gate
    doc = (gate.__doc__ or "") + (gate.is_identity_valid_for_publication.__doc__ or "")
    assert "fail-open" not in doc.lower() or "FAIL-CLOSED" in doc, \
        "Module docstring still claims fail-open — must reflect Phase 10A fail-closed"
