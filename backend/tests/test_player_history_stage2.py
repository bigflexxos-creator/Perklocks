"""Phase 5.3 Stage 2 — multi-sport Player History adapter tests.

Locks:

  §6  NFL  — raw actual preserved, threshold evaluation, opponent +
             home/away context, identity resolution.
  §7  NBA  — raw stats preserved, PRA/PR/PA/RA derived correctly,
             missing component never becomes zero.
  §8  Soccer — club-at-event-time preserved, current-roster stays
             separate, goals/assists/shots/SOT, milestone semantics.
  §9  Tennis — surface preserved, per-surface split, opponent
             history, Tennis-specific schema (no team fields).
  §10 UFC  — fighter identity, method/result preserved, missing
             stats remain UNKNOWN.
  §11 Windows — L5/L10/L20, season, home/away, vs_opponent.
  §12 Sample size truth — no silent padding.
  §13 Canonical identity — display-name string equality alone is
             NOT sufficient; canonical_player_id resolves.
  §16 Shared class — one threshold engine, all sports.
  §17 MLB regression — Stage 1 still passes.
"""
from __future__ import annotations

import asyncio
import sys
import pytest

sys.path.insert(0, "/app/backend")


pytestmark = pytest.mark.unit


# ═══════════════════════════════════════════════════════════════════
# Fake DB
# ═══════════════════════════════════════════════════════════════════
class _AsyncCursor:
    def __init__(self, docs):
        self._docs = list(docs)
    def sort(self, key, direction=1):
        # Sort by first key (works for single-key sorts).
        if isinstance(key, str):
            self._docs.sort(key=lambda d: d.get(key) or "",
                             reverse=(direction == -1))
        return self
    def limit(self, n):
        self._docs = self._docs[:n]
        return self
    def __aiter__(self):
        self._i = 0
        return self
    async def __anext__(self):
        if self._i >= len(self._docs):
            raise StopAsyncIteration
        d = self._docs[self._i]
        self._i += 1
        return dict(d)


def _matches(doc, query):
    for k, v in query.items():
        if isinstance(v, dict):
            # Supports $lt only — sufficient for these tests.
            if "$lt" in v and not (doc.get(k) and doc.get(k) < v["$lt"]):
                return False
        elif doc.get(k) != v:
            return False
    return True


class _FakeCollection:
    def __init__(self):
        self.docs: list[dict] = []
    def find(self, query, projection=None):
        return _AsyncCursor(
            [d for d in self.docs if _matches(d, query)]
        )
    async def find_one(self, query, projection=None):
        for d in self.docs:
            if _matches(d, query):
                return dict(d)
        return None
    async def create_index(self, *a, **kw):
        return None


class _FakeDB:
    def __init__(self):
        self._colls: dict[str, _FakeCollection] = {}
    def __getitem__(self, name):
        return self._colls.setdefault(name, _FakeCollection())
    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self.__getitem__(name)


def _run(coro):
    return asyncio.run(coro)


def _seed(db, rows):
    for r in rows:
        db["player_game_actuals"].docs.append(dict(r))


# ═══════════════════════════════════════════════════════════════════
# §6 — NFL adapter
# ═══════════════════════════════════════════════════════════════════
def test_nfl_passing_yards_exact_threshold_evaluation():
    from services.player_history import get_player_history

    db = _FakeDB()
    _seed(db, [
        {"sport": "nfl", "canonical_player_id": "cpid-mahomes",
          "event_time": "2026-01-05T20:00:00Z", "season": 2025,
          "week": 18, "team": "KC", "opponent": "BUF",
          "home_away": "home",
          "actuals": {"pass_yds": 267, "pass_tds": 2}},
        {"sport": "nfl", "canonical_player_id": "cpid-mahomes",
          "event_time": "2025-12-29T20:00:00Z", "season": 2025,
          "week": 17, "team": "KC", "opponent": "LAC",
          "home_away": "away",
          "actuals": {"pass_yds": 224, "pass_tds": 3}},
        {"sport": "nfl", "canonical_player_id": "cpid-mahomes",
          "event_time": "2025-12-22T20:00:00Z", "season": 2025,
          "week": 16, "team": "KC", "opponent": "BUF",
          "home_away": "away",
          "actuals": {"pass_yds": 299, "pass_tds": 4}},
    ])
    ev = _run(get_player_history(
        db, sport="NFL",
        canonical_player_id="cpid-mahomes",
        market="player_pass_yds",
        threshold=249.5, direction="over",
        opponent="BUF",
        event_time="2026-06-01T00:00:00Z",
    ))
    assert ev.source == "NORMALIZED"
    assert ev.games_available == 3
    assert ev.games_used == 3
    # L5: 267, 224, 299 → over 249.5 → 267 win, 224 loss, 299 win → 2/3
    l5 = ev.last_5["result"]
    assert l5["wins"] == 2 and l5["losses"] == 1
    assert l5["hit_rate"] == pytest.approx(2 / 3, rel=1e-3)
    assert l5["actual_values"] == [267.0, 224.0, 299.0]
    # vs BUF: 267 (win), 299 (win) → 2/2
    vs = ev.vs_opponent["result"]
    assert vs["wins"] == 2 and vs["losses"] == 0
    # Historical team preserved and NOT overwritten as current_team.
    assert ev.historical_team == "KC"
    assert ev.current_team is None    # not supplied to the query


def test_nfl_missing_data_never_becomes_zero():
    from services.player_history import get_player_history

    db = _FakeDB()
    _seed(db, [
        {"sport": "nfl", "canonical_player_id": "cpid-x",
          "event_time": "2026-01-05T20:00:00Z", "season": 2025,
          "actuals": {}},   # missing pass_yds
        {"sport": "nfl", "canonical_player_id": "cpid-x",
          "event_time": "2025-12-29T20:00:00Z", "season": 2025,
          "actuals": {"pass_yds": 300}},
    ])
    ev = _run(get_player_history(
        db, sport="NFL", canonical_player_id="cpid-x",
        market="player_pass_yds", threshold=250.5,
        event_time="2026-06-01T00:00:00Z",
    ))
    assert ev.games_available == 2
    assert ev.games_used == 1   # only the row with pass_yds counted
    assert ev.missing_games == 1
    l5 = ev.last_5["result"]
    # Only ONE decision — 300 win, first row excluded (never zero).
    assert l5["wins"] == 1 and l5["losses"] == 0
    assert l5["sample_size"] == 1


def test_nfl_atd_milestone_semantics():
    from services.player_history import get_player_history

    db = _FakeDB()
    _seed(db, [
        {"sport": "nfl", "canonical_player_id": "cpid-rb",
          "event_time": "2026-01-05T20:00:00Z", "season": 2025,
          "actuals": {"rush_tds": 1, "rec_tds": 0}},
        {"sport": "nfl", "canonical_player_id": "cpid-rb",
          "event_time": "2025-12-29T20:00:00Z", "season": 2025,
          "actuals": {"rush_tds": 0, "rec_tds": 1}},
        {"sport": "nfl", "canonical_player_id": "cpid-rb",
          "event_time": "2025-12-22T20:00:00Z", "season": 2025,
          "actuals": {"rush_tds": 0, "rec_tds": 0}},
    ])
    ev = _run(get_player_history(
        db, sport="NFL", canonical_player_id="cpid-rb",
        market="player_anytime_td", threshold=1.0,
        direction="over",
        event_time="2026-06-01T00:00:00Z",
    ))
    # ATD is milestone (>=1) — two hits (rush_td=1, rec_td=1), one miss.
    l5 = ev.last_5["result"]
    assert l5["wins"] == 2
    assert l5["losses"] == 1


# ═══════════════════════════════════════════════════════════════════
# §7 — NBA adapter
# ═══════════════════════════════════════════════════════════════════
def test_nba_derived_pra_missing_component_stays_none():
    from services.player_history.nba import _extract_nba_actual

    row_ok = {"actuals": {"points": 20, "rebounds": 8, "assists": 5}}
    row_missing_reb = {"actuals": {"points": 20, "assists": 5}}
    assert _extract_nba_actual("player_points_rebounds_assists", row_ok) == 33
    # Missing component → None, never 0.
    assert _extract_nba_actual(
        "player_points_rebounds_assists", row_missing_reb) is None
    assert _extract_nba_actual(
        "player_points_rebounds", row_missing_reb) is None
    # PA (points+assists) is derivable from the missing_reb row.
    assert _extract_nba_actual(
        "player_points_assists", row_missing_reb) == 25


def test_nba_windows_and_home_away_split():
    from services.player_history import get_player_history

    db = _FakeDB()
    rows = []
    for i, (pts, ha) in enumerate([
        (30, "home"), (18, "away"), (25, "home"),
        (22, "away"), (35, "home"),
    ]):
        rows.append({
            "sport": "nba", "canonical_player_id": "cpid-lbj",
            "event_time": f"2026-01-{10+i:02d}T20:00:00Z",
            "season": 2025, "home_away": ha,
            "actuals": {"points": pts, "rebounds": 8, "assists": 6},
        })
    _seed(db, rows)
    ev = _run(get_player_history(
        db, sport="NBA", canonical_player_id="cpid-lbj",
        market="player_points", threshold=24.5, direction="over",
        event_time="2026-06-01T00:00:00Z",
    ))
    # L5 over 24.5: 30/18/25/22/35 → 30w, 18l, 25w, 22l, 35w → 3/5
    l5 = ev.last_5["result"]
    assert l5["wins"] == 3 and l5["losses"] == 2
    # Home split: 30, 25, 35 (all wins).
    home = ev.home["result"]
    assert home["wins"] == 3 and home["losses"] == 0
    # Away split: 18, 22 (both losses).
    away = ev.away["result"]
    assert away["wins"] == 0 and away["losses"] == 2
    # Quantiles populated (>= 3 samples).
    assert l5["median"] == 25
    assert l5["q25"] is not None and l5["q75"] is not None
    assert l5["variance"] is not None


# ═══════════════════════════════════════════════════════════════════
# §8 — Soccer adapter
# ═══════════════════════════════════════════════════════════════════
def test_soccer_anytime_scorer_milestone_and_club_preserved():
    from services.player_history import get_player_history

    db = _FakeDB()
    _seed(db, [
        {"sport": "soccer", "canonical_player_id": "cpid-messi",
          "event_time": "2026-05-01T20:00:00Z", "season": 2025,
          "team": "Inter Miami", "competition": "MLS",
          "opponent": "LAFC", "home_away": "home",
          "actuals": {"goals": 2, "assists": 1, "shots": 6,
                        "shots_on_target": 4}},
        {"sport": "soccer", "canonical_player_id": "cpid-messi",
          "event_time": "2026-04-24T20:00:00Z", "season": 2025,
          "team": "Inter Miami", "competition": "MLS",
          "opponent": "NYC", "home_away": "away",
          "actuals": {"goals": 0, "assists": 2, "shots": 3,
                        "shots_on_target": 1}},
        {"sport": "soccer", "canonical_player_id": "cpid-messi",
          "event_time": "2026-04-17T20:00:00Z", "season": 2025,
          "team": "Inter Miami", "competition": "USOC",
          "opponent": "COL", "home_away": "home",
          "actuals": {"goals": 1, "assists": 0, "shots": 5,
                        "shots_on_target": 2}},
    ])
    ev = _run(get_player_history(
        db, sport="Soccer", canonical_player_id="cpid-messi",
        market="player_goal_scorer_anytime", threshold=1.0,
        direction="over",
        current_team="Inter Miami",     # supplied by candidate context
        event_time="2026-06-01T00:00:00Z",
    ))
    # Milestone: 2 goals=hit, 0 goals=miss, 1 goal=hit
    l5 = ev.last_5["result"]
    assert l5["wins"] == 2 and l5["losses"] == 1
    # Historical club preserved and DIFFERENT from current_team field.
    assert ev.historical_team == "Inter Miami"
    assert ev.current_team == "Inter Miami"     # supplied via arg
    assert ev.extras.get("historical_club") == "Inter Miami"
    # Competition breakdown attached.
    assert ev.by_competition is not None
    assert "MLS" in ev.by_competition
    assert "USOC" in ev.by_competition


def test_soccer_score_or_assist_missing_component_none():
    from services.player_history.soccer import _extract_soccer_actual
    row_full = {"actuals": {"goals": 1, "assists": 2}}
    row_missing_a = {"actuals": {"goals": 1}}
    assert _extract_soccer_actual("player_to_score_or_assist",
                                    row_full) == 3
    assert _extract_soccer_actual("player_to_score_or_assist",
                                    row_missing_a) is None


def test_soccer_historical_club_not_treated_as_current_roster():
    """A player who moved clubs — historical rows reflect the old
    club, but current_team must remain the CANDIDATE-supplied
    value.  The adapter MUST NOT overwrite current_team."""
    from services.player_history import get_player_history

    db = _FakeDB()
    _seed(db, [
        {"sport": "soccer", "canonical_player_id": "cpid-mbappe",
          "event_time": "2026-05-01T20:00:00Z", "season": 2025,
          "team": "PSG", "competition": "Ligue1",
          "actuals": {"goals": 1, "assists": 0}},
    ])
    ev = _run(get_player_history(
        db, sport="Soccer", canonical_player_id="cpid-mbappe",
        market="player_goals_scored", threshold=0.5,
        current_team="Real Madrid",         # transferred!
        event_time="2026-06-01T00:00:00Z",
    ))
    assert ev.historical_team == "PSG"
    assert ev.current_team == "Real Madrid"      # untouched
    # The two are STRICTLY separate.
    assert ev.historical_team != ev.current_team


# ═══════════════════════════════════════════════════════════════════
# §9 — Tennis adapter
# ═══════════════════════════════════════════════════════════════════
def test_tennis_surface_split_never_blends():
    from services.player_history import get_player_history

    db = _FakeDB()
    _seed(db, [
        {"sport": "tennis", "canonical_player_id": "cpid-djoker",
          "event_time": "2026-05-01T14:00:00Z", "season": 2025,
          "surface": "Clay", "tournament": "Madrid Open",
          "round": "QF", "opponent": "cpid-nadal",
          "actuals": {"aces": 5, "double_faults": 2}},
        {"sport": "tennis", "canonical_player_id": "cpid-djoker",
          "event_time": "2026-04-01T14:00:00Z", "season": 2025,
          "surface": "Hard", "tournament": "Miami Open",
          "round": "F", "opponent": "cpid-alcaraz",
          "actuals": {"aces": 12, "double_faults": 1}},
        {"sport": "tennis", "canonical_player_id": "cpid-djoker",
          "event_time": "2026-03-01T14:00:00Z", "season": 2025,
          "surface": "Hard", "tournament": "Dubai",
          "round": "F", "opponent": "cpid-medvedev",
          "actuals": {"aces": 9, "double_faults": 3}},
    ])
    ev = _run(get_player_history(
        db, sport="Tennis", canonical_player_id="cpid-djoker",
        market="player_aces", threshold=8.5, direction="over",
        event_time="2026-06-01T00:00:00Z",
    ))
    assert ev.by_surface is not None
    assert set(ev.by_surface.keys()) == {"clay", "hard"}
    # Hard: 12, 9 both > 8.5 → 2/2
    hard = ev.by_surface["hard"]["result"]
    assert hard["wins"] == 2 and hard["losses"] == 0
    # Clay: 5 < 8.5 → 0/1
    clay = ev.by_surface["clay"]["result"]
    assert clay["wins"] == 0 and clay["losses"] == 1
    # Tennis-specific schema — no team fields fabricated.
    # ``home_away`` fields must be untouched (Tennis has none).
    assert ev.home is None and ev.away is None
    # Extras carry latest surface + tournament.
    assert ev.extras.get("latest_surface") == "clay"
    assert ev.extras.get("latest_tournament") == "Madrid Open"


# ═══════════════════════════════════════════════════════════════════
# §10 — UFC adapter
# ═══════════════════════════════════════════════════════════════════
def test_ufc_significant_strikes_and_missing_stats():
    from services.player_history import get_player_history

    db = _FakeDB()
    _seed(db, [
        {"sport": "ufc", "canonical_player_id": "cpid-mma-1",
          "event_time": "2026-05-01T22:00:00Z", "season": 2025,
          "event": "UFC 300", "opponent": "cpid-mma-2",
          "result": "WIN", "method": "KO", "round": 2,
          "actuals": {"significant_strikes": 74, "takedowns": 1,
                        "knockdowns": 1}},
        {"sport": "ufc", "canonical_player_id": "cpid-mma-1",
          "event_time": "2026-03-01T22:00:00Z", "season": 2025,
          "event": "UFC 299", "opponent": "cpid-mma-3",
          "result": "LOSS", "method": "SUB", "round": 3,
          "actuals": {"significant_strikes": 41}},  # takedowns missing
    ])
    ev = _run(get_player_history(
        db, sport="UFC", canonical_player_id="cpid-mma-1",
        market="fighter_significant_strikes",
        threshold=60.5, direction="over",
        event_time="2026-06-01T00:00:00Z",
    ))
    l5 = ev.last_5["result"]
    assert l5["wins"] == 1 and l5["losses"] == 1     # 74 win, 41 loss
    # Method / result distributions captured in extras.
    assert ev.extras.get("career_method_mix") == {"KO": 1, "SUB": 1}
    assert ev.extras.get("career_result_mix") == {"WIN": 1, "LOSS": 1}

    # UNKNOWN stat query — no fabrication.
    ev2 = _run(get_player_history(
        db, sport="UFC", canonical_player_id="cpid-mma-1",
        market="fighter_takedowns", threshold=0.5,
        event_time="2026-06-01T00:00:00Z",
    ))
    # Only ONE row has takedowns; missing rows excluded.
    l5b = ev2.last_5["result"]
    assert l5b["sample_size"] == 1
    assert l5b["wins"] == 1        # takedowns=1 > 0.5


# ═══════════════════════════════════════════════════════════════════
# §11 — Windows shape + §12 — sample-size truth
# ═══════════════════════════════════════════════════════════════════
def test_windows_expose_requested_and_available_counts():
    from services.player_history import get_player_history

    db = _FakeDB()
    rows = []
    for i in range(4):     # only 4 rows — L10/L20 must reflect truth
        rows.append({
            "sport": "nba", "canonical_player_id": "cpid-few",
            "event_time": f"2026-01-{10+i:02d}T20:00:00Z",
            "season": 2025,
            "actuals": {"points": 20 + i, "rebounds": 5, "assists": 3},
        })
    _seed(db, rows)
    ev = _run(get_player_history(
        db, sport="NBA", canonical_player_id="cpid-few",
        market="player_points", threshold=21.5,
        event_time="2026-06-01T00:00:00Z",
    ))
    # L5 requested 5, available 4 → sample_size == 4.
    assert ev.last_5["games_used"] == 4
    assert ev.last_5["games_requested"] == 5
    # L10 requested 10, available 4 → sample_size == 4.
    assert ev.last_10["games_used"] == 4
    assert ev.last_10["games_requested"] == 10
    # sample-size truth: quantiles populated (>=3 samples)
    l5 = ev.last_5["result"]
    assert l5["sample_size"] == 4
    assert l5["median"] is not None


def test_insufficient_sample_leaves_quantiles_none():
    from services.player_history.threshold_engine import evaluate_threshold
    # 2 samples → quantiles remain None.
    r = evaluate_threshold([10.0, 20.0], threshold=15.0, direction="over")
    assert r.sample_size == 2
    assert r.median is None and r.q25 is None and r.q75 is None


# ═══════════════════════════════════════════════════════════════════
# §13 — Canonical identity resolution
# ═══════════════════════════════════════════════════════════════════
def test_identity_resolves_by_canonical_id_not_display_name():
    from services.player_history import get_player_history

    db = _FakeDB()
    # Two players with the SAME display name but different canonical ids.
    _seed(db, [
        {"sport": "nfl", "canonical_player_id": "cpid-A",
          "event_time": "2026-01-05T20:00:00Z", "season": 2025,
          "player_name": "John Smith",
          "actuals": {"pass_yds": 100}},
        {"sport": "nfl", "canonical_player_id": "cpid-B",
          "event_time": "2026-01-05T20:00:00Z", "season": 2025,
          "player_name": "John Smith",
          "actuals": {"pass_yds": 400}},
    ])
    ev = _run(get_player_history(
        db, sport="NFL", canonical_player_id="cpid-A",
        market="player_pass_yds", threshold=250.5,
        player_name="John Smith",
        event_time="2026-06-01T00:00:00Z",
    ))
    # Must pull only cpid-A row (100), NEVER blended with cpid-B (400).
    l5 = ev.last_5["result"]
    assert l5["sample_size"] == 1
    assert l5["actual_values"] == [100.0]
    assert ev.identity_confidence == "HIGH"


def test_missing_identity_returns_unavailable():
    from services.player_history import get_player_history
    db = _FakeDB()
    ev = _run(get_player_history(
        db, sport="NBA",
        # NO canonical_player_id / player_id at all
        market="player_points", threshold=20.5,
        event_time="2026-06-01T00:00:00Z",
    ))
    assert ev.data_quality == "UNAVAILABLE"
    assert ev.games_used == 0


# ═══════════════════════════════════════════════════════════════════
# §16 — Shared class fix: one threshold engine, all sports
# ═══════════════════════════════════════════════════════════════════
def test_shared_threshold_engine_same_semantics_across_sports():
    """All five sports go through the SAME evaluate_threshold /
    evaluate_milestone helpers.  Missing component → None → excluded
    — never zero — in every adapter."""
    from services.player_history.nfl import _extract_nfl_actual
    from services.player_history.nba import _extract_nba_actual
    from services.player_history.soccer import _extract_soccer_actual
    from services.player_history.tennis import _extract_tennis_actual
    from services.player_history.ufc import _extract_ufc_actual
    empty_row = {"actuals": {}}
    # Every derived-combo extractor returns None on an empty row.
    assert _extract_nba_actual("player_points_rebounds_assists",
                                 empty_row) is None
    assert _extract_soccer_actual("player_to_score_or_assist",
                                    empty_row) is None
    assert _extract_tennis_actual("total_games", empty_row) is None
    assert _extract_nfl_actual("player_anytime_td", empty_row) is None
    # UFC never has derived compound markets — but every extractor
    # returns None cleanly on a missing stat.
    assert _extract_ufc_actual("fighter_significant_strikes",
                                 empty_row) is None


# ═══════════════════════════════════════════════════════════════════
# §17 — MLB regression (Stage 1 remains intact)
# ═══════════════════════════════════════════════════════════════════
def test_mlb_regression_still_works():
    from services.player_history import get_player_history

    db = _FakeDB()
    _seed(db, [
        {"sport": "mlb", "canonical_player_id": "cpid-judge",
          "event_time": "2026-05-01T20:00:00Z", "season": 2025,
          "team": "NYY", "opponent": "BOS", "home_away": "home",
          "actuals": {"h": 2, "hr": 1, "rbi": 3}},
        {"sport": "mlb", "canonical_player_id": "cpid-judge",
          "event_time": "2026-04-24T20:00:00Z", "season": 2025,
          "team": "NYY", "opponent": "BOS", "home_away": "away",
          "actuals": {"h": 0, "hr": 0, "rbi": 0}},
    ])
    ev = _run(get_player_history(
        db, sport="MLB", canonical_player_id="cpid-judge",
        market="batter_hits", threshold=0.5, direction="over",
        opponent="BOS",
        event_time="2026-06-01T00:00:00Z",
    ))
    l5 = ev.last_5["result"]
    assert l5["wins"] == 1 and l5["losses"] == 1   # 2 hits win, 0 hits loss
    assert ev.historical_team == "NYY"
    assert ev.source in ("NORMALIZED", "MLB_STATSAPI")


# ═══════════════════════════════════════════════════════════════════
# §14 — Production-truth History surface stays UNKNOWN when consumer
# is not yet wired (deliberately preserved — no fake PASS)
# ═══════════════════════════════════════════════════════════════════
def test_production_truth_history_consumer_still_unknown_when_no_snapshot():
    """History adapter EXISTS, but no live production consumer wires
    it into the reachability observation for a random published
    pick — the HISTORY consumer surface remains UNKNOWN.  §14: do
    not report PASS merely because an adapter exists."""
    from services.production_truth import (
        build_reachability_report,
        ConsumerSurface,
    )
    pick = {
        "id":                "p-hist-1",
        "sport":             "NBA",
        "market":            "player_points",
        "book_odds":        -110,
        "publication_gate":  "canonical_barrier_passed",
        "lock_score":        88,
        "commence_time":     "2026-09-01T00:00:00Z",
    }
    report = build_reachability_report(pick)
    # No pregame_snapshot passed → HISTORY surface is UNKNOWN.
    assert report.consumers[ConsumerSurface.HISTORY.value]["status"] == \
        "UNKNOWN"


# ═══════════════════════════════════════════════════════════════════
# Not-applicable sports return UNAVAILABLE (never fake PASS)
# ═══════════════════════════════════════════════════════════════════
def test_unsupported_sport_returns_sport_not_supported():
    from services.player_history import get_player_history
    db = _FakeDB()
    ev = _run(get_player_history(
        db, sport="CFB", canonical_player_id="cpid-x",
        market="player_pass_yds", threshold=200,
        event_time="2026-06-01T00:00:00Z",
    ))
    assert ev.source == "SPORT_NOT_SUPPORTED"
    assert ev.data_quality == "UNAVAILABLE"


# ═══════════════════════════════════════════════════════════════════
# History-as-of cutoff — future rows must NEVER leak
# ═══════════════════════════════════════════════════════════════════
def test_history_as_of_cutoff_excludes_future_rows():
    from services.player_history import get_player_history

    db = _FakeDB()
    _seed(db, [
        {"sport": "nba", "canonical_player_id": "cpid-y",
          "event_time": "2026-03-01T20:00:00Z", "season": 2025,
          "actuals": {"points": 30}},
        # THIS row is AFTER the cutoff → must be excluded.
        {"sport": "nba", "canonical_player_id": "cpid-y",
          "event_time": "2026-05-01T20:00:00Z", "season": 2025,
          "actuals": {"points": 5}},
    ])
    ev = _run(get_player_history(
        db, sport="NBA", canonical_player_id="cpid-y",
        market="player_points", threshold=15.5,
        event_time="2026-04-01T00:00:00Z",     # cutoff BETWEEN the two
    ))
    l5 = ev.last_5["result"]
    # Only the earlier row (30) is visible.
    assert l5["sample_size"] == 1
    assert l5["actual_values"] == [30.0]
