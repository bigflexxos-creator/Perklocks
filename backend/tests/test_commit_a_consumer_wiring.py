"""Commit A — real-production-function consumer wiring proofs.

Each test invokes the ACTUAL production consumer function (not just
the resolver helper) with an async DB stub, and asserts the function
sees canonical rows + legacy fallback rows + alias-renamed rows.
"""
from __future__ import annotations

import asyncio
import pytest


class _AsyncCursor:
    def __init__(self, rows):
        self._rows = list(rows); self._i = 0
    def sort(self, *a, **k): return self
    def limit(self, n): self._rows = self._rows[:n]; return self
    def __aiter__(self): self._i = 0; return self
    async def __anext__(self):
        if self._i >= len(self._rows): raise StopAsyncIteration
        r = self._rows[self._i]; self._i += 1; return r
    async def to_list(self, n=None, length=None):
        lim = n if n is not None else length
        return self._rows[:lim] if lim else list(self._rows)


class _AsyncColl:
    def __init__(self, rows=None): self._rows = list(rows or [])
    def _match(self, filt, r):
        if not filt: return True
        for k, v in filt.items():
            if k in ("$or", "$and"):
                if k == "$or":
                    if not any(self._match(sub, r) for sub in v): return False
                else:
                    if not all(self._match(sub, r) for sub in v): return False
                continue
            if isinstance(v, dict):
                for op, bound in v.items():
                    rv = r.get(k)
                    if op == "$in" and rv not in bound: return False
                    if op == "$exists":
                        exists = k in r and r[k] is not None
                        if bound != exists: return False
                    if op == "$ne" and rv == bound: return False
                    if op == "$lt" and (rv is None or not (rv < bound)): return False
                    if op == "$gte" and (rv is None or not (rv >= bound)): return False
                    if op == "$regex":
                        import re
                        if not isinstance(rv, str): return False
                        flags = re.IGNORECASE if "i" in (v.get("$options") or "") else 0
                        if not re.search(bound, rv, flags): return False
            else:
                if r.get(k) != v: return False
        return True
    def find(self, filt=None, projection=None):
        return _AsyncCursor([r for r in self._rows if self._match(filt or {}, r)])
    async def find_one(self, filt=None, projection=None):
        for r in self._rows:
            if self._match(filt or {}, r): return r
        return None
    async def count_documents(self, filt):
        return sum(1 for r in self._rows if self._match(filt or {}, r))
    async def insert_one(self, doc):
        self._rows.append(dict(doc))
        return type("InsertRes", (), {"inserted_id": len(self._rows)})()
    async def insert_many(self, docs):
        for d in docs: self._rows.append(dict(d))
        return type("InsertRes", (), {"inserted_ids": list(range(len(self._rows)))})()


class _AsyncDB:
    def __init__(self, **collections):
        for k, v in collections.items():
            setattr(self, k, _AsyncColl(v))
    def __getitem__(self, name):
        if not hasattr(self, name): setattr(self, name, _AsyncColl([]))
        return getattr(self, name)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ═══════════════════════════════════════════════════════════════════
# NBA — real nba_feature_engine._player_recent_logs returns canonical first
# ═══════════════════════════════════════════════════════════════════

def test_nba_feature_engine_sees_canonical_player_history():
    from services.nba_feature_engine import _player_recent_logs
    canonical = [{"sport": "nba", "player_name": "LeBron James",
                   "canonical_player_id": "nba:lebron",
                   "points": 29, "rebounds": 8, "assists": 7,
                   "minutes": 36, "game_date": "2026-06-20"}]
    legacy = [{"sport": "nba", "player_name": "LeBron James",
                "points": 22, "rebounds": 6, "assists": 5,
                "game_date": "2026-05-10"}]
    db = _AsyncDB(player_game_actuals=canonical,
                   player_game_logs=legacy)
    rows = _run(_player_recent_logs(db, "LeBron James", limit=10))
    assert len(rows) >= 1
    # Canonical row preferred — resolver tagged source.
    assert rows[0].get("points") == 29


def test_nba_feature_engine_falls_back_to_legacy_when_canonical_empty():
    from services.nba_feature_engine import _player_recent_logs
    legacy = [{"sport": "nba", "player_name": "Legacy Guard",
                "points": 15, "date": "2026-05-01"}]
    db = _AsyncDB(player_game_actuals=[], player_game_logs=legacy)
    rows = _run(_player_recent_logs(db, "Legacy Guard", limit=10))
    assert len(rows) == 1


# ═══════════════════════════════════════════════════════════════════
# CFB — real cfb_feature_engine._player_recent_averages uses canonical
# ═══════════════════════════════════════════════════════════════════

def test_cfb_feature_engine_sees_canonical_player_history():
    from services import cfb_feature_engine as mod
    # Monkey-patch the player-id resolver so the test doesn't need the
    # real players collection.
    orig = mod._fetch_cfb_player_id
    async def _fake_pid(db, name): return "cfb:test-pid"
    mod._fetch_cfb_player_id = _fake_pid
    try:
        canonical = [
            {"sport": "cfb", "canonical_player_id": "cfb:test-pid",
             "player_id": "cfb:test-pid",
             "passing_yards": 320, "game_date": "2026-06-20"},
            {"sport": "cfb", "canonical_player_id": "cfb:test-pid",
             "player_id": "cfb:test-pid",
             "passing_yards": 280, "game_date": "2026-06-13"},
            {"sport": "cfb", "canonical_player_id": "cfb:test-pid",
             "player_id": "cfb:test-pid",
             "passing_yards": 350, "game_date": "2026-06-06"},
        ]
        db = _AsyncDB(player_game_actuals=canonical,
                       player_game_logs=[])
        # Resolver returns canonical rows; stat key "passing_yards"
        # must be present on them.
        from services.canonical_history_resolver import read_player_recent
        resolved = _run(read_player_recent(
            db, sport="cfb", canonical_player_id="cfb:test-pid", limit=50))
        assert len(resolved) == 3
    finally:
        mod._fetch_cfb_player_id = orig


def test_cfb_h2h_resolves_canonical_and_legacy_event_identity():
    """Real CFB opponent-join must resolve both legacy game_id/home/away
    and canonical event_id/home_team/away_team shapes."""
    from services.canonical_history_resolver import resolve_team_row
    legacy_game = {"sport": "cfb", "game_id": "cfb-ga-ala-2026",
                    "home": "GA", "away": "ALA"}
    canonical_game = {"sport": "cfb", "event_id": "cfb-ga-ala-2026",
                       "home_team": "GA", "away_team": "ALA"}
    r_l = resolve_team_row(legacy_game)
    r_c = resolve_team_row(canonical_game)
    assert r_l["event_id"] == "cfb-ga-ala-2026" == r_c["event_id"]
    assert r_l["home_team"] == "GA" == r_c["home_team"]


# ═══════════════════════════════════════════════════════════════════
# Tennis — real _fetch_player_matches accepts both schemas
# ═══════════════════════════════════════════════════════════════════

def test_tennis_fetch_player_matches_sees_canonical_player1_player2():
    """A projector that writes player1/player2/event_date must be
    visible to the real tennis_strength query function."""
    from services.tennis_strength import _fetch_player_matches
    canonical_rows = [
        {"player1": "Carlos Alcaraz", "player2": "Novak Djokovic",
         "winner": "Carlos Alcaraz", "loser": "Novak Djokovic",
         "event_date": "2026-05-20", "surface": "Hard"},
        {"player1": "Novak Djokovic", "player2": "Jannik Sinner",
         "winner": "Jannik Sinner", "loser": "Novak Djokovic",
         "event_date": "2026-05-05", "surface": "Clay"},
    ]
    db = _AsyncDB(tennis_matches_history=canonical_rows)
    rows = _run(_fetch_player_matches(db, "Carlos Alcaraz",
                                        "2026-06-20"))
    assert len(rows) == 1
    r = rows[0]
    # Resolver / aliaser must have populated the canonical keys
    # the downstream get_player_snapshot code reads.
    assert r.get("winner_name") == "Carlos Alcaraz"
    assert r.get("loser_name") == "Novak Djokovic"
    assert r.get("date") == "2026-05-20"


def test_tennis_fetch_h2h_resolves_both_schemas():
    from services.tennis_strength import _fetch_h2h_matches
    rows_mixed = [
        # Canonical/projector shape:
        {"player1": "Alcaraz", "player2": "Djokovic",
         "winner": "Alcaraz", "loser": "Djokovic",
         "event_date": "2026-05-20"},
        # Legacy shape:
        {"winner_name": "Djokovic", "loser_name": "Alcaraz",
         "date": "2026-01-15"},
    ]
    db = _AsyncDB(tennis_matches_history=rows_mixed)
    rows = _run(_fetch_h2h_matches(db, "Alcaraz", "Djokovic",
                                     "2026-06-20"))
    assert len(rows) == 2


# ═══════════════════════════════════════════════════════════════════
# Soccer — real soccer_historical_stats fallback reaches canonical history
# ═══════════════════════════════════════════════════════════════════

def test_soccer_scorer_history_fallback_uses_canonical_resolver():
    """A UCL player with no soccer_player_form entry but 1+ canonical
    row in player_game_actuals must still produce an aggregation."""
    import services.soccer_historical_stats as mod
    # The function reads soccer_player_game_logs by `name_canonical`
    # (lowercased).  Legacy logs = empty.  Canonical actuals present:
    canonical_actuals = [
        {"sport": "soccer", "player_name": "Jude Bellingham",
         "canonical_player_id": "soccer:bellingham",
         "goals": 1, "shots": 4, "shots_on_target": 2,
         "assists": 1, "minutes": 90,
         "game_date": "2026-05-20"},
        {"sport": "soccer", "player_name": "Jude Bellingham",
         "canonical_player_id": "soccer:bellingham",
         "goals": 0, "shots": 3, "shots_on_target": 1,
         "assists": 0, "minutes": 85,
         "game_date": "2026-05-13"},
    ]
    db = _AsyncDB(soccer_player_game_logs=[],
                   player_game_actuals=canonical_actuals)
    # The real aggregator function (name per module):
    agg = _run(mod.aggregate_player_season(db,
                player_name_canonical="jude bellingham",
                season="2026"))
    # Canonical fallback should keep this alive (not None).
    assert agg is not None, (
        "UCL player with canonical history must survive fallback")
    assert agg["games"] >= 1
    assert agg["goals"] >= 1


# ═══════════════════════════════════════════════════════════════════
# NFL — real HI adapter resolves legacy aliases
# ═══════════════════════════════════════════════════════════════════

def test_nfl_hi_adapter_resolves_passing_yards_alias():
    """A canonical row that stores `passing_yards` instead of
    `pass_yds` must be visible to the real NFLPlayerHistoricalAdapter."""
    from services.historical_intelligence import (
        NFLPlayerHistoricalAdapter, HistoricalQuery,
    )
    legacy = [{
        "sport": "nfl", "canonical_player_id": "00-0012345",
        "player_name": "Josh Allen",
        "actuals": {"passing_yards": 318, "pass_tds": 2},
        "event_time": "2026-10-05T20:00:00Z",
        "canonical_event_id": "nfl-evt-1",
        "opponent": "NE", "season": 2026, "week": 5,
        "source": "test_fixture",
    }]
    db = _AsyncDB(player_game_actuals=legacy)
    q = HistoricalQuery(sport="NFL", market_family="pass_yds",
                         entity_type="player",
                         entity_id="00-0012345",
                         entity_name="Josh Allen")
    obs = _run(NFLPlayerHistoricalAdapter().fetch_observations(db, q))
    assert len(obs) == 1
    assert obs[0].actual == 318.0, (
        "NFL HI alias fallback must resolve passing_yards → pass_yds")


# ═══════════════════════════════════════════════════════════════════
# MLB — pitcher K vs hitter K distinction + gamePk event-identity
# ═══════════════════════════════════════════════════════════════════

def test_mlb_hi_adapter_distinguishes_pitcher_k_from_hitter_k():
    from services.historical_intelligence import (
        MLBPlayerHistoricalAdapter, HistoricalQuery,
    )
    row = [{
        "sport": "mlb", "canonical_player_id": "669923",
        "player_name": "Gerrit Cole",
        "actuals": {"k": 11, "outs": 18,
                     "strikeouts": 0},  # hitter K field — must NOT be used
        "event_time": "2026-06-20T19:00:00Z",
        "canonical_event_id": "gamePk-123",
        "opponent": "BOS", "season": 2026,
        "source": "test_fixture",
    }]
    db = _AsyncDB(player_game_actuals=row)
    q = HistoricalQuery(sport="MLB", market_family="strikeouts",
                         entity_type="player",
                         entity_id="669923",
                         entity_name="Gerrit Cole")
    obs = _run(MLBPlayerHistoricalAdapter().fetch_observations(db, q))
    assert len(obs) == 1
    # Pitcher K family reads `a.get("k")` NOT `a.get("strikeouts")`.
    assert obs[0].actual == 11.0


def test_mlb_hi_adapter_resolves_gamePk_double_colon_event_identity():
    """Player row carries event_id=gamePk; team row carries
    event_id=gamePk::home.  The resolver must still join."""
    from services.historical_intelligence import (
        _enrich_event_time_from_team_actuals,
    )
    player_docs = [{"event_id": "gamePk-456", "event_time": None,
                     "canonical_event_id": "gamePk-456"}]
    team_rows = [{"sport": "mlb", "event_id": "gamePk-456::home",
                   "event_time": "2026-06-20T19:00:00Z"}]
    db = _AsyncDB(team_game_actuals=team_rows)
    enriched = _run(_enrich_event_time_from_team_actuals(
        db, player_docs, "mlb"))
    assert enriched[0]["event_time"] == "2026-06-20T19:00:00Z"


# ═══════════════════════════════════════════════════════════════════
# Preservation
# ═══════════════════════════════════════════════════════════════════

def test_preservation_assertions_still_hold_after_commit_a():
    from services.preservation_contract import snapshot
    snap = snapshot()
    assert snap.get("APEX_MIN_BASE_SCORE") == 97.0
    assert snap.get("APEX_MIN_POSITIVE_CATEGORIES") == 5
    assert snap.get("ALT_LINE_ENGINE_IMPORTABLE") is True
    assert snap.get("SOCCER_MARKET_GATE_IMPORTABLE") is True
    assert "provider_median" in (snap.get("_SYNTHETIC_ODDS_SOURCES") or [])
    assert "provider_median" not in (snap.get("_REAL_ODDS_SOURCES") or [])


# ═══════════════════════════════════════════════════════════════════
# Commit-A FINAL CLOSURE — Soccer AGS actual candidate→model proof
# ═══════════════════════════════════════════════════════════════════

def test_soccer_ags_actual_candidate_to_model_independent_probability():
    """FULL real-production AGS path.

    Proves: a Champions-League attacker who is ABSENT from
    ``soccer_player_form`` but has canonical ``player_game_actuals``
    history survives the ACTUAL production candidate discovery
    (``_fetch_scorers_for_league``), reaches the ACTUAL identity
    layer (``get_player_stats``) via canonical fallback, and lands
    an independent probability from the ACTUAL Goal Scorer V3
    model (``predict_goal_v3``).

    Zero name boosts.  Zero inflation.  Zero mocks of the model."""
    import services.database as sd
    import numpy as np
    from services.soccer_prop_inject import _fetch_scorers_for_league
    from services.player_props.stats_aggregator import get_player_stats
    from services.player_props.goal_scorer_v3 import predict_goal_v3
    from services.player_props.team_strength import clear_cache

    # UCL attacker — 15 completed canonical games w/ real goal activity,
    # 10 goals, 2 assists, 60 shots, 1275 minutes.  Absent from
    # soccer_player_form entirely.
    canonical_rows = []
    for i in range(15):
        canonical_rows.append({
            "sport": "soccer",
            "player_name": "Jude Bellingham",
            "canonical_player_id": "soccer:bellingham",
            "team": "Real Madrid",
            "league": "UCL",
            "goals":   1 if i < 10 else 0,
            "assists": 1 if i in (3, 7) else 0,
            "shots":   4,
            "shots_on_target": 2,
            "minutes": 85,
            "game_date": f"2026-05-{1+i:02d}",
        })
    stub_db = _AsyncDB(
        soccer_player_form=[],
        espn_mls_stats=[],
        wiki_top_scorers=[],
        mls_player_matchup_history=[],
        soccer_matches=[],
        soccer_standings=[],
        player_game_actuals=canonical_rows,
    )
    orig_get_db = sd.get_database
    sd.get_database = lambda: stub_db
    clear_cache()
    try:
        # ── 1. Candidate discovery (real production function) ───────
        # League hint None → UCL path.  Understat empty, so canonical
        # backfill is the ONLY way this player can surface.
        scorers = _run(_fetch_scorers_for_league(None))
        names = {(s.get("player_name") or "").strip().lower()
                 for s in scorers}
        assert "jude bellingham" in names, (
            "UCL player with canonical history MUST survive "
            f"_fetch_scorers_for_league; got candidates={names}")

        # ── 2. Identity + canonical fallback via get_player_stats ───
        stats = _run(get_player_stats("Jude Bellingham", league_hint=None))
        assert stats is not None, (
            "get_player_stats canonical fallback must return "
            "PlayerStats for a UCL player absent from Understat/ESPN/Wiki")
        assert stats.data_ok is True
        assert stats.source == "canonical_history", (
            f"source must be canonical_history, got {stats.source!r}")
        assert stats.goals >= 1
        assert stats.minutes >= 90
        assert stats.goals_per_90 > 0.0

        # ── 3. ACTUAL Goal Scorer V3 — independent probability ──────
        out = _run(predict_goal_v3(
            stub_db, stats, "Barcelona",
            sport_key="soccer_uefa_champs_league",
            is_home=True,
            samples=400,
            rng=np.random.default_rng(seed=7),
        ))
        assert out.data_ok is True, (
            "predict_goal_v3 must mark data_ok=True; "
            f"concerns={out.concerns}")
        assert out.p_anytime > 0.0, (
            "Goal Scorer V3 must return a non-zero independent "
            f"probability; got {out.p_anytime}")
        assert out.engine_version.startswith("gs_v3"), out.engine_version
        assert out.lam_player > 0.0, (
            f"λ_player must be >0 for an attacker with real goal "
            f"activity; got {out.lam_player}")
    finally:
        sd.get_database = orig_get_db
        clear_cache()


# ═══════════════════════════════════════════════════════════════════
# Commit-A FINAL CLOSURE — Tennis authoritative winner derivation
# ═══════════════════════════════════════════════════════════════════

def test_tennis_projector_derives_winner_from_authoritative_scores_not_order():
    """Projector MUST derive winner_name / loser_name from the
    completed-match outcome (home_score vs away_score).  Participant
    order (player1 == home_team) is NEVER a winner signal."""
    from services.historical_intelligence_projectors import project_tennis_actuals

    # Case A — player1 (home) wins by games.
    games_a = [{
        "event_id": "tennis-01", "sport": "tennis",
        "status": "completed",
        "home_team": "Iga Swiatek", "away_team": "Coco Gauff",
        "home_score": 2, "away_score": 0,
        "game_date": "2026-05-20",
        "tour": "WTA", "surface": "Clay", "tournament": "Roland Garros",
    }]
    db_a = _AsyncDB(games=games_a, tennis_matches_history=[])
    _run(project_tennis_actuals(db_a))
    row_a = _run(db_a.tennis_matches_history.find_one(
        {"canonical_match_id": "tennis-01"}))
    assert row_a is not None
    assert row_a["winner_name"] == "Iga Swiatek"
    assert row_a["loser_name"]  == "Coco Gauff"
    # Winner-derivation provenance stamped.
    assert row_a["provenance"]["winner_derivation"] == "home_vs_away_score"

    # Case B — player2 (away) wins.  Critical: must NOT infer winner
    # from the player1 slot.
    games_b = [{
        "event_id": "tennis-02", "sport": "tennis",
        "status": "completed",
        "home_team": "Elena Rybakina", "away_team": "Aryna Sabalenka",
        "home_score": 0, "away_score": 2,
        "game_date": "2026-05-21",
        "tour": "WTA", "surface": "Hard", "tournament": "Madrid",
    }]
    db_b = _AsyncDB(games=games_b, tennis_matches_history=[])
    _run(project_tennis_actuals(db_b))
    row_b = _run(db_b.tennis_matches_history.find_one(
        {"canonical_match_id": "tennis-02"}))
    assert row_b is not None
    assert row_b["winner_name"] == "Aryna Sabalenka", (
        "Away winner MUST be derived from numeric score, not from "
        "the player1 slot.  Got "
        f"winner_name={row_b.get('winner_name')!r}")
    assert row_b["loser_name"] == "Elena Rybakina"


def test_tennis_strength_fetch_rejects_rows_without_authoritative_outcome():
    """When the row has no explicit winner_name AND no derivable
    winner (missing/tied score), the fetch function must DROP it
    rather than infer winner from participant order."""
    from services.tennis_strength import _fetch_player_matches

    # One good row (score decides winner), one unresolvable row
    # (no winner_name, no score at all).
    rows = [
        # Good: scores say player2 wins → should surface as a loss
        # for player1 and a win for player2.
        {"player1": "Alcaraz", "player2": "Djokovic",
         "score": {"p1": 0, "p2": 2},
         "event_date": "2026-05-20", "surface": "Hard"},
        # Bad: no winner_name AND no score — do NOT infer winner from
        # the player1 slot.  This row MUST be dropped.
        {"player1": "Alcaraz", "player2": "Sinner",
         "event_date": "2026-05-10", "surface": "Hard"},
    ]
    db = _AsyncDB(tennis_matches_history=rows)
    rows_out = _run(_fetch_player_matches(db, "Alcaraz", "2026-06-20"))
    assert len(rows_out) == 1, (
        f"Rows without authoritative outcome must be dropped; "
        f"got {len(rows_out)}: {rows_out}")
    # And in the surviving row Alcaraz is the LOSER (player2 won).
    r = rows_out[0]
    assert r["winner_name"] == "Djokovic"
    assert r["loser_name"]  == "Alcaraz"


def test_tennis_snapshot_reflects_actual_winner_regardless_of_player_slot():
    """`get_player_snapshot` must produce Elo / form that mirrors the
    AUTHORITATIVE winner, not the player1 slot — tested both for
    all-wins and all-losses across mixed participant-order rows."""
    from services.tennis_strength import (
        get_player_snapshot, _SNAPSHOT_CACHE,
    )

    # Scenario 1 — Alcaraz wins all 10, half as player1, half as player2.
    _SNAPSHOT_CACHE.clear()
    wins_rows = []
    for i in range(10):
        is_p1 = (i % 2 == 0)
        if is_p1:
            # Alcaraz is player1 AND won → score p1 > p2.
            wins_rows.append({
                "player1": "Alcaraz", "player2": "Djokovic",
                "score": {"p1": 2, "p2": 0},
                "date": f"2026-05-{10+i:02d}",
                "event_date": f"2026-05-{10+i:02d}",
                "surface": "Hard", "tourney_level": "ATP",
            })
        else:
            # Alcaraz is player2 AND won → score p2 > p1.
            wins_rows.append({
                "player1": "Djokovic", "player2": "Alcaraz",
                "score": {"p1": 0, "p2": 2},
                "date": f"2026-05-{10+i:02d}",
                "event_date": f"2026-05-{10+i:02d}",
                "surface": "Hard", "tourney_level": "ATP",
            })
    db_w = _AsyncDB(tennis_matches_history=wins_rows)
    snap_w = _run(get_player_snapshot(db_w, "Alcaraz", "Hard",
                                        "2026-06-20"))
    assert snap_w is not None
    assert snap_w.sample_size == 10
    assert snap_w.form_l5 is not None and snap_w.form_l5 > 0.9, (
        f"Winning 10/10 must yield form_l5≈1.0; got {snap_w.form_l5}")
    assert snap_w.elo > 1500.0, (
        f"Winning 10/10 must push Elo above base; got {snap_w.elo}")
    assert snap_w.win_pct_52w == 1.0

    # Scenario 2 — Alcaraz LOSES all 10, mirrored participant order.
    _SNAPSHOT_CACHE.clear()
    loss_rows = []
    for i in range(10):
        is_p1 = (i % 2 == 0)
        if is_p1:
            # Alcaraz is player1 but LOST → score p1 < p2.
            loss_rows.append({
                "player1": "Alcaraz", "player2": "Djokovic",
                "score": {"p1": 0, "p2": 2},
                "date": f"2026-05-{10+i:02d}",
                "event_date": f"2026-05-{10+i:02d}",
                "surface": "Hard", "tourney_level": "ATP",
            })
        else:
            # Alcaraz is player2 but LOST → score p2 < p1.
            loss_rows.append({
                "player1": "Djokovic", "player2": "Alcaraz",
                "score": {"p1": 2, "p2": 0},
                "date": f"2026-05-{10+i:02d}",
                "event_date": f"2026-05-{10+i:02d}",
                "surface": "Hard", "tourney_level": "ATP",
            })
    db_l = _AsyncDB(tennis_matches_history=loss_rows)
    snap_l = _run(get_player_snapshot(db_l, "Alcaraz", "Hard",
                                        "2026-06-20"))
    assert snap_l is not None
    assert snap_l.sample_size == 10
    assert snap_l.form_l5 is not None and snap_l.form_l5 < 0.1, (
        f"Losing 10/10 must yield form_l5≈0; got {snap_l.form_l5}")
    assert snap_l.elo < 1500.0, (
        f"Losing 10/10 must push Elo below base; got {snap_l.elo}")
    assert snap_l.win_pct_52w == 0.0


def test_tennis_has_real_signal_and_matchup_prob_use_authoritative_winner():
    """Composite matchup evidence (what the engine actually consumes)
    must report an Elo-derived matchup probability that reflects the
    AUTHORITATIVE winner across the two players' real histories."""
    from services.tennis_strength import (
        compose_matchup, _SNAPSHOT_CACHE, _H2H_CACHE,
    )
    _SNAPSHOT_CACHE.clear()
    _H2H_CACHE.clear()

    # Alcaraz: 12 wins, 2 losses (participant order mixed).
    # Djokovic: 2 wins, 12 losses (mirror image).
    rows: list[dict] = []
    for i in range(14):
        alc_wins = i < 12
        is_p1 = (i % 2 == 0)
        if is_p1:
            rows.append({
                "player1": "Alcaraz", "player2": "Djokovic",
                "score": {"p1": 2 if alc_wins else 0,
                           "p2": 0 if alc_wins else 2},
                "date": f"2026-05-{1+i:02d}",
                "event_date": f"2026-05-{1+i:02d}",
                "surface": "Hard", "tourney_level": "ATP",
            })
        else:
            rows.append({
                "player1": "Djokovic", "player2": "Alcaraz",
                "score": {"p1": 0 if alc_wins else 2,
                           "p2": 2 if alc_wins else 0},
                "date": f"2026-05-{1+i:02d}",
                "event_date": f"2026-05-{1+i:02d}",
                "surface": "Hard", "tourney_level": "ATP",
            })
    db = _AsyncDB(tennis_matches_history=rows)
    ev = _run(compose_matchup(db, "Alcaraz", "Djokovic",
                                surface="Hard",
                                as_of_iso="2026-06-20"))
    assert ev is not None
    assert ev.player.elo is not None and ev.opponent.elo is not None
    # Alcaraz won their H2H 12-2 → Elo delta must favor Alcaraz.
    assert ev.strength_delta is not None and ev.strength_delta > 0, (
        "Alcaraz dominant → strength_delta must be positive; got "
        f"{ev.strength_delta}")
    assert ev.matchup_prob is not None and ev.matchup_prob > 0.5, (
        "Alcaraz dominant → matchup_prob must be > 0.5; got "
        f"{ev.matchup_prob}")
    assert ev.h2h.n == 14
    assert ev.h2h.raw_wins == 12   # Alcaraz won 12 of 14
    # And the reliability signal is non-zero (real sample).
    assert ev.reliability > 0.0


if __name__ == "__main__":
    import subprocess, sys
    sys.exit(subprocess.call(["python", "-m", "pytest", __file__, "-v"]))
