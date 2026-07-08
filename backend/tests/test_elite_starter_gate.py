"""Tests for the starter-gate in elite_players.py.

Regression (2026-07-08): Ollie Watkins / Ivan Toney kept surfacing as
Elite Locks on World Cup picks despite not starting for England in 45+
days.  These tests lock in the gate that suppresses reputation boosts
when a player hasn't logged ≥ 2 STARTS (ESPN `starter: true`) in the
target league kind within the last 45 days.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from elite_players import (  # noqa: E402
    _classify_event_league_kind,
    _is_actively_starting_soccer,
    _slug_player,
    _STARTER_CACHE,
    _STARTER_LEAGUE_KIND,
    ELITE_PLAYERS,
)


def _prime_cache(entries: dict) -> None:
    """Populate the module cache directly for a test.

    `entries` maps player name → dict of league_kind counts, e.g.
        {"Harry Kane": {"national": 7, "club": 0}}
    """
    _STARTER_CACHE.clear()
    _STARTER_LEAGUE_KIND.clear()
    for name, kinds in entries.items():
        slug = _slug_player(name)
        _STARTER_LEAGUE_KIND[slug] = dict(kinds)
        _STARTER_CACHE[slug] = 1 if sum(kinds.values()) >= 2 else 0
    # Suppress cache-refresh in the tested code paths.
    import elite_players as ep
    ep._STARTER_CACHE_TS = float("inf")


# ── League-kind classifier ────────────────────────────────────────
class TestLeagueKindClassifier:
    def test_world_cup_events_are_national(self):
        assert _classify_event_league_kind(
            "England @ Norway", "FIFA World Cup · Props"
        ) == "national"

    def test_euro_events_are_national(self):
        assert _classify_event_league_kind(
            "Germany @ France", "UEFA Euro 2028"
        ) == "national"

    def test_nations_league_events_are_national(self):
        assert _classify_event_league_kind(
            "Portugal @ Spain", "UEFA Nations League"
        ) == "national"

    def test_premier_league_events_are_club(self):
        assert _classify_event_league_kind(
            "Arsenal @ Manchester City", "English Premier League"
        ) == "club"

    def test_champions_league_events_are_club(self):
        assert _classify_event_league_kind(
            "Bayern Munich @ Real Madrid", "UEFA Champions League"
        ) == "club"


# ── Starter gate — the Watkins / Toney regression tests ───────────
class TestStarterGateSoccer:
    def test_bench_forward_blocked_for_national_pick(self):
        """Watkins with 1 lone national start in 45 days must NOT
        qualify for a World Cup Elite Lock — needs ≥ 2."""
        _prime_cache({"Ollie Watkins": {"national": 1, "club": 1}})
        assert _is_actively_starting_soccer(
            "Ollie Watkins", league_kind="national"
        ) is False

    def test_zero_starts_elite_name_blocked(self):
        """Ivan Toney: on the elite list, has zero recent starts data —
        must fail closed, NOT default open."""
        _prime_cache({})  # empty — Toney is unknown
        assert "Ivan Toney" in ELITE_PLAYERS["Soccer"], \
            "Precondition: Toney must be on the elite roster"
        assert _is_actively_starting_soccer(
            "Ivan Toney", league_kind="national"
        ) is False, "Elite name with no data → fail-closed"

    def test_regular_starter_passes_national_gate(self):
        _prime_cache({"Harry Kane": {"national": 7}})
        assert _is_actively_starting_soccer(
            "Harry Kane", league_kind="national"
        ) is True

    def test_national_starts_dont_rescue_club_pick(self):
        """A Premier League pick on Kane requires club starts.  His 7
        England starts DON'T count for a Bayern club match."""
        _prime_cache({"Harry Kane": {"national": 7, "club": 0}})
        assert _is_actively_starting_soccer(
            "Harry Kane", league_kind="club"
        ) is False

    def test_club_starts_dont_rescue_national_pick(self):
        """Symmetric: Aston Villa league starts don't rescue an
        England World Cup pick for Watkins."""
        _prime_cache({"Ollie Watkins": {"club": 5, "national": 0}})
        assert _is_actively_starting_soccer(
            "Ollie Watkins", league_kind="national"
        ) is False
        # But he DOES qualify for a Villa club pick.
        assert _is_actively_starting_soccer(
            "Ollie Watkins", league_kind="club"
        ) is True

    def test_unknown_non_elite_name_fails_open(self):
        """A new-transfer forward we've never seen and isn't on the
        elite list gets the benefit of the doubt — reputation still
        applies (fail-open)."""
        _prime_cache({})
        assert _is_actively_starting_soccer(
            "Some Unknown Newcomer", league_kind="national"
        ) is True
