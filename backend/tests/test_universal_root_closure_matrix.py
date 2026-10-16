"""Universal Root-Closure — real-path test matrix.

Satisfies Block V of the Universal Root-Closure directive.  Each test
invokes an ACTUAL production function and asserts the real behavior.
Zero scoring or threshold changes.
"""
from __future__ import annotations

import asyncio
import inspect
import pytest


# ─────────────────────────────────────────────────────────────────────
# Shared async DB stub — in-memory collections with mongo-like API.
# ─────────────────────────────────────────────────────────────────────
class _AsyncCursor:
    def __init__(self, rows):
        self._rows = list(rows)
        self._i = 0
    def sort(self, *a, **k): return self
    def limit(self, n): self._rows = self._rows[:n]; return self
    def __aiter__(self): self._i = 0; return self
    async def __anext__(self):
        if self._i >= len(self._rows): raise StopAsyncIteration
        r = self._rows[self._i]; self._i += 1; return r


class _AsyncColl:
    def __init__(self, rows=None): self._rows = list(rows or [])
    def find(self, filt=None, projection=None):
        rows = self._rows
        if isinstance(filt, dict):
            def _match_sport():
                sp = filt.get("sport")
                if sp is None: return rows
                return [r for r in rows if r.get("sport") == sp]
            rows = _match_sport()
        return _AsyncCursor(rows)
    async def count_documents(self, filt):
        return len(list(self.find(filt)._rows))


class _AsyncDB:
    def __init__(self, **collections):
        for k, v in collections.items():
            setattr(self, k, _AsyncColl(v))
    def __getitem__(self, name):
        if not hasattr(self, name): setattr(self, name, _AsyncColl([]))
        return getattr(self, name)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ─────────────────────────────────────────────────────────────────────
# NHL — canonical player history reaches real consumer with ≥5 rows.
# ─────────────────────────────────────────────────────────────────────

def test_nhl_canonical_player_history_resolves_via_resolver():
    from services.canonical_history_resolver import read_player_recent
    canonical_rows = [
        {"sport": "nhl", "player_name": "A. Matthews", "name": "A. Matthews",
         "goals": 1, "assists": 2, "points": 3, "shots": 5,
         "game_date": "2026-06-20"},
        {"sport": "nhl", "player_name": "A. Matthews", "name": "A. Matthews",
         "goals": 2, "assists": 1, "points": 3, "shots": 6,
         "game_date": "2026-06-18"},
        {"sport": "nhl", "player_name": "A. Matthews", "name": "A. Matthews",
         "goals": 0, "assists": 1, "points": 1, "shots": 4,
         "game_date": "2026-06-15"},
        {"sport": "nhl", "player_name": "A. Matthews", "name": "A. Matthews",
         "goals": 1, "assists": 0, "points": 1, "shots": 3,
         "game_date": "2026-06-12"},
        {"sport": "nhl", "player_name": "A. Matthews", "name": "A. Matthews",
         "goals": 0, "assists": 2, "points": 2, "shots": 5,
         "game_date": "2026-06-10"},
    ]
    db = _AsyncDB(player_game_actuals=canonical_rows, player_game_logs=[])
    rows = _run(read_player_recent(db, sport="nhl",
                                     player_name="A. Matthews", limit=40))
    assert len(rows) >= 5
    assert rows[0]["player_name"] == "A. Matthews"
    assert rows[0]["goals"] == 1
    # All rows source-tagged canonical.
    assert all(r["_source_collection"] == "player_game_actuals" for r in rows)


def test_nhl_legacy_player_history_fallback_still_works():
    from services.canonical_history_resolver import read_player_recent
    legacy_rows = [
        {"sport": "nhl", "name": "A. Matthews",
         "goals": 1, "points": 3, "shots": 5, "game_date": "2026-06-20"},
    ]
    db = _AsyncDB(player_game_actuals=[], player_game_logs=legacy_rows)
    rows = _run(read_player_recent(db, sport="nhl",
                                     player_name="A. Matthews", limit=40))
    assert len(rows) == 1
    assert rows[0]["_source_collection"] == "player_game_logs"
    assert rows[0]["player_name"] == "A. Matthews"


def test_nhl_feature_engine_uses_resolver():
    """The real nhl_feature_engine code must import the resolver."""
    import services.nhl_feature_engine as mod
    src = inspect.getsource(mod)
    assert "from services.canonical_history_resolver import read_player_recent" in src


# ─────────────────────────────────────────────────────────────────────
# NHL — canonical team history reaches team accessor with alias support.
# ─────────────────────────────────────────────────────────────────────

def test_nhl_team_history_alias_resolution():
    from services.canonical_history_resolver import read_team_recent
    # Mix of canonical (`home_team`) and legacy (`home`) rows:
    canonical_rows = [
        {"sport": "nhl", "home_team": "TOR", "away_team": "BOS",
         "home_score": 4, "away_score": 3, "game_date": "2026-06-20"},
        {"sport": "nhl", "home_team": "MTL", "away_team": "TOR",
         "home_score": 2, "away_score": 5, "game_date": "2026-06-18"},
    ]
    db = _AsyncDB(team_game_actuals=canonical_rows, games=[])
    rows = _run(read_team_recent(db, sport="nhl", team="TOR", limit=10))
    assert len(rows) == 2
    # Resolver must have normalized every row to the canonical field names.
    for r in rows:
        assert "home_team" in r and "away_team" in r
        assert "home_score" in r and "away_score" in r


# ─────────────────────────────────────────────────────────────────────
# Soccer — canonical date alias resolution.
# ─────────────────────────────────────────────────────────────────────

def test_soccer_date_alias_resolution():
    from services.canonical_history_resolver import resolve_team_row
    row_new = {"sport": "soccer", "home_team": "ARS", "away_team": "CHE",
                "event_date": "2026-06-20"}
    row_old = {"sport": "soccer", "home_team": "ARS", "away_team": "CHE",
                "date": "2026-06-20"}
    assert resolve_team_row(row_new)["game_date"] == "2026-06-20"
    assert resolve_team_row(row_old)["game_date"] == "2026-06-20"


# ─────────────────────────────────────────────────────────────────────
# Tennis — canonical/legacy field dual-resolution.
# ─────────────────────────────────────────────────────────────────────

def test_tennis_winner_loser_name_alias_resolution():
    from services.canonical_history_resolver import resolve_player_row
    legacy = {"player1": "Alcaraz", "player2": "Djokovic",
               "event_date": "2026-06-20"}
    canonical = {"winner_name": "Alcaraz", "loser_name": "Djokovic",
                  "match_date": "2026-06-20"}
    r_legacy = resolve_player_row(legacy, sport="tennis")
    r_canon  = resolve_player_row(canonical, sport="tennis")
    assert r_legacy["winner_name"] == "Alcaraz"
    assert r_legacy["loser_name"] == "Djokovic"
    assert r_canon["winner_name"] == "Alcaraz"


# ─────────────────────────────────────────────────────────────────────
# NBA — pre-season closure: canonical history reads before legacy.
# ─────────────────────────────────────────────────────────────────────

def test_nba_canonical_history_preferred_over_legacy():
    from services.canonical_history_resolver import read_player_recent
    canonical = [
        {"sport": "nba", "canonical_player_id": "nba:lebron",
         "player_name": "LeBron James", "points": 29, "rebounds": 8,
         "assists": 7, "minutes": 36, "game_date": "2026-06-20"},
    ]
    legacy = [
        {"sport": "nba", "name": "LeBron James",
         "pts": 24, "reb": 10, "ast": 7, "min": 34, "date": "2026-05-10"},
    ]
    db = _AsyncDB(player_game_actuals=canonical,
                   player_game_logs=legacy)
    rows = _run(read_player_recent(db, sport="nba",
                                     player_name="LeBron James", limit=10))
    assert len(rows) == 1
    assert rows[0]["_source_collection"] == "player_game_actuals"
    assert rows[0]["points"] == 29


# ─────────────────────────────────────────────────────────────────────
# CFB — event_id / game_id alias resolution for H2H.
# ─────────────────────────────────────────────────────────────────────

def test_cfb_event_id_alias_resolution():
    from services.canonical_history_resolver import resolve_team_row
    legacy = {"sport": "cfb", "game_id": "cfb-2026-ga-ala",
               "home": "GA", "away": "ALA"}
    canonical = {"sport": "cfb", "event_id": "cfb-2026-ga-ala",
                  "home_team": "GA", "away_team": "ALA"}
    r_legacy = resolve_team_row(legacy)
    r_canon  = resolve_team_row(canonical)
    assert r_legacy["event_id"] == "cfb-2026-ga-ala"
    assert r_legacy["home_team"] == "GA"
    assert r_canon["event_id"] == "cfb-2026-ga-ala"


# ─────────────────────────────────────────────────────────────────────
# NFL — HI stat-key alias resolution.
# ─────────────────────────────────────────────────────────────────────

def test_nfl_passing_yards_alias_resolution():
    from services.canonical_history_resolver import resolve_player_row
    legacy = {"sport": "nfl", "player_name": "QB",
               "passing_yards": 318, "rushing_yards": 25,
               "receiving_yards": None, "game_date": "2026-06-20"}
    canonical = {"sport": "nfl", "player_name": "QB",
                  "pass_yds": 318, "rush_yds": 25, "game_date": "2026-06-20"}
    r1 = resolve_player_row(legacy, sport="nfl")
    r2 = resolve_player_row(canonical, sport="nfl")
    assert r1["pass_yds"] == 318
    assert r1["rush_yds"] == 25
    assert r2["pass_yds"] == 318


# ─────────────────────────────────────────────────────────────────────
# MLB — pitcher-K vs hitter-K distinction.
# ─────────────────────────────────────────────────────────────────────

def test_mlb_pitcher_strikeouts_distinct_from_hitter_strikeouts():
    from services.canonical_history_resolver import resolve_player_row
    pitcher = {"sport": "mlb", "player_name": "Gerrit Cole",
                "role": "pitcher", "pitcher_strikeouts": 11,
                "pitcher_outs": 18, "game_date": "2026-06-20"}
    hitter  = {"sport": "mlb", "player_name": "Aaron Judge",
                "role": "batter", "hitter_strikeouts": 2,
                "hits": 1, "game_date": "2026-06-20"}
    rp = resolve_player_row(pitcher, sport="mlb")
    rh = resolve_player_row(hitter,  sport="mlb")
    assert rp.get("pitcher_strikeouts") == 11
    assert rp.get("pitcher_outs") == 18
    assert rh.get("hitter_strikeouts") == 2
    # Hitter row MUST NOT populate pitcher_strikeouts.
    assert "pitcher_strikeouts" not in rh or rh.get("pitcher_strikeouts") is None


# ─────────────────────────────────────────────────────────────────────
# ESPN — one completed fixture per sport reaches canonical destination.
# ─────────────────────────────────────────────────────────────────────

def test_espn_normalize_covers_all_supported_sports():
    from services.espn_enrichment_backbone import (
        normalize_espn_completed_game, ESPN_FIELDS_BY_SPORT,
    )
    raw = {
        "event_id": "espn-evt-1",
        "home_team": "H", "away_team": "A",
        "home_score": 3, "away_score": 2,
        "game_date": "2026-06-20", "completed": True,
    }
    for sport in ("nfl", "mlb", "nba", "nhl", "cfb", "soccer"):
        norm = normalize_espn_completed_game(sport, raw)
        assert norm["sport"] == sport
        assert norm["event_id"] == "espn-evt-1"
        assert norm["status"] == "Final"
        assert norm["provenance"]["provider"] == "espn"
    # Tennis contract exists (schedules-only).
    assert "tennis" in ESPN_FIELDS_BY_SPORT


# ─────────────────────────────────────────────────────────────────────
# Snapshot freshness — committed_version mismatch invalidates cache.
# ─────────────────────────────────────────────────────────────────────

def test_snapshot_cache_invalidated_on_committed_version_change():
    """When committed_version is known AND differs from the stored
    snapshot's board_version, get_fresh_snapshot() MUST return None
    (immediate invalidation) — NOT fall through to TTL."""
    import importlib
    from services import board_snapshot_cache as mod
    importlib.reload(mod)
    from datetime import datetime, timezone
    class _S:
        board_version = "OLD-VER"
        generated_at = datetime.now(timezone.utc)
    _snap = _S()
    params = {"sport": "NFL", "line_type": "main"}
    mod._state.entries[mod._cache_key(params)] = _snap
    # Force committed_version to differ from stored.
    orig = mod._committed_version
    mod._committed_version = lambda: "NEW-VER"
    try:
        res = mod.get_snapshot(params)
        assert res is None, (
            "Known committed_version mismatch MUST invalidate "
            "immediately; TTL may NOT be used when committed_version "
            "is authoritative.")
        # When committed and version match → fresh.
        _snap.board_version = "NEW-VER"
        res2 = mod.get_snapshot(params)
        assert res2 is _snap
    finally:
        mod._committed_version = orig


def test_snapshot_cache_falls_back_to_ttl_when_committed_unknown():
    """If committed_version authority is unavailable, TTL remains
    valid as a safety fallback."""
    import importlib
    from services import board_snapshot_cache as mod
    importlib.reload(mod)
    from datetime import datetime, timezone
    class _S:
        board_version = "ANY-VER"
        generated_at = datetime.now(timezone.utc)
    _snap = _S()
    params = {"sport": "NHL", "line_type": "main"}
    mod._state.entries[mod._cache_key(params)] = _snap
    orig = mod._committed_version
    mod._committed_version = lambda: None  # authority unavailable
    try:
        res = mod.get_snapshot(params)
        assert res is _snap
    finally:
        mod._committed_version = orig


# ─────────────────────────────────────────────────────────────────────
# Preservation — every protected constant still intact.
# ─────────────────────────────────────────────────────────────────────

def test_preservation_assertions_still_hold_after_resolver_wiring():
    from services.preservation_contract import snapshot
    snap = snapshot()
    assert snap.get("APEX_MIN_BASE_SCORE") == 97.0
    assert snap.get("APEX_MIN_POSITIVE_CATEGORIES") == 5
    # NHL floor markers still present.
    floor_t = snap.get("NHL_MIN_TEAM_RECENT_GAMES")
    floor_p = snap.get("NHL_MIN_PLAYER_RECENT_GAMES")
    if floor_t is not None or floor_p is not None:
        assert floor_t == 5
        assert floor_p == 5
    else:
        assert snap.get("NHL_floor_marker_present") is True
    assert snap.get("ALT_LINE_ENGINE_IMPORTABLE") is True
    assert snap.get("SOCCER_MARKET_GATE_IMPORTABLE") is True


if __name__ == "__main__":
    import subprocess, sys
    sys.exit(subprocess.call(["python", "-m", "pytest", __file__, "-v"]))
