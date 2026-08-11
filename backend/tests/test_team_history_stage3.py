"""Phase 5.3 Stage 3 — Team History Foundation deterministic tests.

Covers §19 requirements:

  1  Canonical team identity is used (not text matching)
  2  Team/opponent perspective preserved
  3  Raw scoring actuals preserved
  4  Home / away distinct
  5  L5/L10/L20 ordering correct
  6  Current + previous season boundaries correct
  7  Multi-season preserves represented seasons
  8  H2H uses canonical identities
  9  H2H perspective correct from both directions
  10 Small H2H samples not inflated
  11 Q25/median/Q75/variance correct where enough data
  12 Missing scores stay UNKNOWN, not zero
  13 Legitimate zero stays zero
  14 as_of prevents future leakage
  15 Historical results cannot rewrite immutable pregame evidence
  16 Tennis/UFC return NOT_APPLICABLE
  17 Production-Truth cannot claim consumption from module existence
"""
from __future__ import annotations

import asyncio
import sys
import pytest

sys.path.insert(0, "/app/backend")


pytestmark = pytest.mark.unit


# ═══════════════════════════════════════════════════════════════════
# Fake async DB
# ═══════════════════════════════════════════════════════════════════
class _AsyncCursor:
    def __init__(self, docs):
        self._docs = list(docs)
    def sort(self, key, direction=1):
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
            if "$lt" in v and not (doc.get(k) and doc.get(k) < v["$lt"]):
                return False
        elif doc.get(k) != v:
            return False
    return True


class _FakeCollection:
    def __init__(self):
        self.docs: list[dict] = []
    def find(self, query, projection=None):
        return _AsyncCursor([d for d in self.docs if _matches(d, query)])
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
        db["team_game_actuals"].docs.append(dict(r))


# ═══════════════════════════════════════════════════════════════════
# MLB team history
# ═══════════════════════════════════════════════════════════════════
def test_mlb_team_history_raw_scores_preserved():
    from services.team_history import get_team_history

    db = _FakeDB()
    _seed(db, [
        {"sport": "mlb", "canonical_team_id": "NYY",
          "event_time": "2026-05-01T20:00:00Z", "season": 2026,
          "canonical_opponent_id": "BOS", "home_away": "home",
          "team_score": 7, "opponent_score": 4, "result": "WIN",
          "starting_pitcher_id": "sp-cole"},
        {"sport": "mlb", "canonical_team_id": "NYY",
          "event_time": "2026-04-30T20:00:00Z", "season": 2026,
          "canonical_opponent_id": "BOS", "home_away": "home",
          "team_score": 2, "opponent_score": 3, "result": "LOSS"},
        {"sport": "mlb", "canonical_team_id": "NYY",
          "event_time": "2026-04-29T20:00:00Z", "season": 2026,
          "canonical_opponent_id": "TB", "home_away": "away",
          "team_score": 0, "opponent_score": 6, "result": "LOSS"},
    ])
    ev = _run(get_team_history(
        db, sport="MLB", canonical_team_id="NYY",
        as_of="2026-06-01T00:00:00Z",
    ))
    assert ev.source == "NORMALIZED"
    assert ev.events_available == 3
    assert ev.events_used == 3
    l5 = ev.last_5
    assert l5["sample_size"] == 3
    assert l5["scored_values"] == [7.0, 2.0, 0.0]
    assert l5["conceded_values"] == [4.0, 3.0, 6.0]
    assert l5["wins"] == 1 and l5["losses"] == 2
    # Legitimate 0 preserved.
    assert 0.0 in l5["scored_values"]


def test_mlb_missing_score_stays_unknown_not_zero():
    from services.team_history import get_team_history

    db = _FakeDB()
    _seed(db, [
        {"sport": "mlb", "canonical_team_id": "NYY",
          "event_time": "2026-05-01T20:00:00Z", "season": 2026,
          "team_score": 5, "opponent_score": 3, "result": "WIN"},
        # missing opponent_score → excluded from numeric distribution
        {"sport": "mlb", "canonical_team_id": "NYY",
          "event_time": "2026-04-30T20:00:00Z", "season": 2026,
          "team_score": 4},
    ])
    ev = _run(get_team_history(
        db, sport="MLB", canonical_team_id="NYY",
        as_of="2026-06-01T00:00:00Z",
    ))
    l5 = ev.last_5
    # Only ONE row has BOTH scores → sample_size 1.
    assert l5["sample_size"] == 1
    assert l5["scored_values"] == [5.0]
    assert l5["conceded_values"] == [3.0]
    assert ev.missing_events == 0     # row is available, just missing opp score


# ═══════════════════════════════════════════════════════════════════
# NFL team history — home/away distinct
# ═══════════════════════════════════════════════════════════════════
def test_nfl_home_away_distinct():
    from services.team_history import get_team_history

    db = _FakeDB()
    rows = []
    for i, (ts, os_, ha) in enumerate([
        (28, 21, "home"), (14, 24, "away"), (35, 17, "home"),
        (20, 27, "away"), (31, 14, "home"),
    ]):
        rows.append({
            "sport": "nfl", "canonical_team_id": "KC",
            "event_time": f"2026-01-{10+i:02d}T20:00:00Z",
            "season": 2025, "week": 15 + i,
            "team_score": ts, "opponent_score": os_,
            "home_away": ha, "result": "WIN" if ts > os_ else "LOSS",
        })
    _seed(db, rows)
    ev = _run(get_team_history(
        db, sport="NFL", canonical_team_id="KC",
        as_of="2026-06-01T00:00:00Z",
    ))
    # Home: 28, 35, 31 (all wins). Away: 14, 20 (both losses).
    assert ev.home["wins"] == 3
    assert ev.home["losses"] == 0
    assert ev.away["wins"] == 0
    assert ev.away["losses"] == 2
    # Overall L5 preserves order (newest-first).
    assert ev.last_5["scored_values"] == [31.0, 20.0, 35.0, 14.0, 28.0]


# ═══════════════════════════════════════════════════════════════════
# NBA — quantiles when enough samples
# ═══════════════════════════════════════════════════════════════════
def test_nba_quantiles_populated_with_sufficient_sample():
    from services.team_history import get_team_history

    db = _FakeDB()
    scores = [110, 98, 122, 105, 115, 100, 118, 90, 108, 112]
    for i, ts in enumerate(scores):
        _seed(db, [{
            "sport": "nba", "canonical_team_id": "BOS",
            "event_time": f"2026-01-{10+i:02d}T20:00:00Z",
            "season": 2025,
            "team_score": ts, "opponent_score": 100,
            "result": "WIN" if ts > 100 else "LOSS",
            "home_away": "home",
        }])
    ev = _run(get_team_history(
        db, sport="NBA", canonical_team_id="BOS",
        as_of="2026-06-01T00:00:00Z",
    ))
    l10 = ev.last_10
    assert l10["sample_size"] == 10
    assert l10["scored_median"] is not None
    assert l10["scored_q25"] is not None
    assert l10["scored_q75"] is not None
    assert l10["scored_variance"] is not None


def test_nba_insufficient_sample_leaves_quantiles_none():
    from services.team_history import get_team_history

    db = _FakeDB()
    _seed(db, [
        {"sport": "nba", "canonical_team_id": "BOS",
          "event_time": "2026-01-10T20:00:00Z", "season": 2025,
          "team_score": 100, "opponent_score": 95, "result": "WIN"},
        {"sport": "nba", "canonical_team_id": "BOS",
          "event_time": "2026-01-08T20:00:00Z", "season": 2025,
          "team_score": 88, "opponent_score": 90, "result": "LOSS"},
    ])
    ev = _run(get_team_history(
        db, sport="NBA", canonical_team_id="BOS",
        as_of="2026-06-01T00:00:00Z",
    ))
    l5 = ev.last_5
    assert l5["sample_size"] == 2
    # Below QUANTILE_MIN_SAMPLE → q25/median/q75/variance all None.
    assert l5["scored_median"] is None
    assert l5["scored_q25"] is None
    assert l5["scored_q75"] is None


# ═══════════════════════════════════════════════════════════════════
# Soccer — draws + competition split + xG extras
# ═══════════════════════════════════════════════════════════════════
def test_soccer_draws_and_extras_preserved():
    from services.team_history import get_team_history

    db = _FakeDB()
    _seed(db, [
        {"sport": "soccer", "canonical_team_id": "MCI",
          "event_time": "2026-05-01T20:00:00Z", "season": 2025,
          "competition": "Premier League",
          "canonical_opponent_id": "LIV",
          "team_score": 2, "opponent_score": 2, "result": "DRAW",
          "home_away": "home",
          "xg_for": 1.7, "xg_against": 1.3},
        {"sport": "soccer", "canonical_team_id": "MCI",
          "event_time": "2026-04-30T20:00:00Z", "season": 2025,
          "competition": "Premier League",
          "canonical_opponent_id": "TOT",
          "team_score": 3, "opponent_score": 1, "result": "WIN",
          "home_away": "away",
          "xg_for": 2.4, "xg_against": 0.9},
        {"sport": "soccer", "canonical_team_id": "MCI",
          "event_time": "2026-04-24T20:00:00Z", "season": 2025,
          "competition": "Champions League",
          "canonical_opponent_id": "RMA",
          "team_score": 1, "opponent_score": 2, "result": "LOSS",
          "home_away": "home",
          "xg_for": 1.2, "xg_against": 1.5},
    ])
    ev = _run(get_team_history(
        db, sport="Soccer", canonical_team_id="MCI",
        as_of="2026-06-01T00:00:00Z",
    ))
    l5 = ev.last_5
    assert l5["wins"] == 1 and l5["losses"] == 1 and l5["draws"] == 1
    # xG extras attached, averaged from the three rows.
    xg_avg = ev.extras.get("xg_for_avg")
    assert xg_avg == round((1.7 + 2.4 + 1.2) / 3, 3)


# ═══════════════════════════════════════════════════════════════════
# NHL — OT loss handled distinctly
# ═══════════════════════════════════════════════════════════════════
def test_nhl_ot_loss_counted_as_loss_but_ot_flag_preserved():
    from services.team_history import get_team_history

    db = _FakeDB()
    _seed(db, [
        {"sport": "nhl", "canonical_team_id": "TOR",
          "event_time": "2026-05-01T20:00:00Z", "season": 2025,
          "team_score": 3, "opponent_score": 4, "result": "OTL",
          "overtime": True, "home_away": "home"},
        {"sport": "nhl", "canonical_team_id": "TOR",
          "event_time": "2026-04-29T20:00:00Z", "season": 2025,
          "team_score": 5, "opponent_score": 2, "result": "WIN",
          "home_away": "away"},
    ])
    ev = _run(get_team_history(
        db, sport="NHL", canonical_team_id="TOR",
        as_of="2026-06-01T00:00:00Z",
    ))
    l5 = ev.last_5
    assert l5["wins"] == 1
    assert l5["losses"] == 1
    assert l5["ot_losses"] == 1


# ═══════════════════════════════════════════════════════════════════
# CFB
# ═══════════════════════════════════════════════════════════════════
def test_cfb_team_history_basic():
    from services.team_history import get_team_history

    db = _FakeDB()
    _seed(db, [
        {"sport": "cfb", "canonical_team_id": "GA",
          "event_time": "2025-12-01T20:00:00Z", "season": 2025,
          "week": 14, "team_score": 42, "opponent_score": 10,
          "result": "WIN", "home_away": "home"},
        {"sport": "cfb", "canonical_team_id": "GA",
          "event_time": "2025-11-24T20:00:00Z", "season": 2025,
          "week": 13, "team_score": 28, "opponent_score": 35,
          "result": "LOSS", "home_away": "away"},
    ])
    ev = _run(get_team_history(
        db, sport="CFB", canonical_team_id="GA",
        as_of="2026-06-01T00:00:00Z",
    ))
    assert ev.last_5["sample_size"] == 2
    assert ev.extras.get("latest_week") == 14


# ═══════════════════════════════════════════════════════════════════
# H2H — canonical identity + perspective + small samples
# ═══════════════════════════════════════════════════════════════════
def test_h2h_uses_canonical_identity_not_text():
    from services.team_history import get_h2h_history

    db = _FakeDB()
    # Two "New York Yankees" entries with different canonical ids.
    _seed(db, [
        {"sport": "mlb", "canonical_team_id": "NYY",
          "canonical_opponent_id": "BOS",
          "team_name": "New York Yankees",
          "event_time": "2026-05-01T20:00:00Z", "season": 2026,
          "team_score": 7, "opponent_score": 4, "result": "WIN"},
        {"sport": "mlb", "canonical_team_id": "OTHER_YANKEES",
          "canonical_opponent_id": "BOS",
          "team_name": "New York Yankees",     # same display name
          "event_time": "2026-04-30T20:00:00Z", "season": 2026,
          "team_score": 3, "opponent_score": 8, "result": "LOSS"},
    ])
    h2h = _run(get_h2h_history(
        db, sport="MLB",
        canonical_team_id="NYY",
        canonical_opponent_id="BOS",
        as_of="2026-06-01T00:00:00Z",
    ))
    # Only the row with canonical_team_id=NYY counts — never blended
    # with the identically-named team.
    assert h2h.sample_size == 1
    assert h2h.wins == 1
    assert h2h.losses == 0


def test_h2h_perspective_correct_from_both_directions():
    """Team A vs Team B — the team's ``team_score`` field must always
    reflect its own perspective, never the opponent's."""
    from services.team_history import get_h2h_history

    db = _FakeDB()
    # From A's perspective — A scored 7, B scored 4.
    _seed(db, [
        {"sport": "mlb", "canonical_team_id": "A",
          "canonical_opponent_id": "B",
          "event_time": "2026-05-01T20:00:00Z", "season": 2026,
          "team_score": 7, "opponent_score": 4, "result": "WIN"},
    ])
    # From B's perspective — same game but flipped scores.
    _seed(db, [
        {"sport": "mlb", "canonical_team_id": "B",
          "canonical_opponent_id": "A",
          "event_time": "2026-05-01T20:00:00Z", "season": 2026,
          "team_score": 4, "opponent_score": 7, "result": "LOSS"},
    ])
    h2h_a = _run(get_h2h_history(
        db, sport="MLB", canonical_team_id="A",
        canonical_opponent_id="B",
        as_of="2026-06-01T00:00:00Z",
    ))
    h2h_b = _run(get_h2h_history(
        db, sport="MLB", canonical_team_id="B",
        canonical_opponent_id="A",
        as_of="2026-06-01T00:00:00Z",
    ))
    assert h2h_a.wins == 1 and h2h_a.losses == 0
    assert h2h_b.wins == 0 and h2h_b.losses == 1
    # Perspective: A's scored = 7, B's scored = 4.
    assert h2h_a.scored_avg == 7.0
    assert h2h_b.scored_avg == 4.0


def test_h2h_small_sample_not_inflated():
    from services.team_history import get_h2h_history

    db = _FakeDB()
    _seed(db, [
        {"sport": "mlb", "canonical_team_id": "A",
          "canonical_opponent_id": "B",
          "event_time": "2026-05-01T20:00:00Z", "season": 2026,
          "team_score": 5, "opponent_score": 4, "result": "WIN"},
        {"sport": "mlb", "canonical_team_id": "A",
          "canonical_opponent_id": "B",
          "event_time": "2026-04-01T20:00:00Z", "season": 2026,
          "team_score": 3, "opponent_score": 5, "result": "LOSS"},
    ])
    h2h = _run(get_h2h_history(
        db, sport="MLB", canonical_team_id="A",
        canonical_opponent_id="B",
        as_of="2026-06-01T00:00:00Z",
    ))
    assert h2h.sample_size == 2
    # Two-sample H2H — quantiles remain None (below MIN_SAMPLE=3).
    assert h2h.scored_median is None


# ═══════════════════════════════════════════════════════════════════
# as_of safety — no future leakage
# ═══════════════════════════════════════════════════════════════════
def test_as_of_excludes_future_rows():
    from services.team_history import get_team_history

    db = _FakeDB()
    _seed(db, [
        {"sport": "nba", "canonical_team_id": "LAL",
          "event_time": "2026-03-01T20:00:00Z", "season": 2025,
          "team_score": 110, "opponent_score": 100, "result": "WIN"},
        # After the cutoff — MUST be excluded.
        {"sport": "nba", "canonical_team_id": "LAL",
          "event_time": "2026-05-01T20:00:00Z", "season": 2025,
          "team_score": 90, "opponent_score": 120, "result": "LOSS"},
    ])
    ev = _run(get_team_history(
        db, sport="NBA", canonical_team_id="LAL",
        as_of="2026-04-01T00:00:00Z",
    ))
    assert ev.last_5["sample_size"] == 1
    assert ev.last_5["scored_values"] == [110.0]


# ═══════════════════════════════════════════════════════════════════
# Season boundaries + multi-season
# ═══════════════════════════════════════════════════════════════════
def test_season_and_previous_season_boundaries():
    from services.team_history import get_team_history

    db = _FakeDB()
    _seed(db, [
        {"sport": "nba", "canonical_team_id": "BOS",
          "event_time": "2026-01-15T20:00:00Z", "season": 2025,
          "team_score": 120, "opponent_score": 100, "result": "WIN"},
        {"sport": "nba", "canonical_team_id": "BOS",
          "event_time": "2025-11-15T20:00:00Z", "season": 2025,
          "team_score": 115, "opponent_score": 110, "result": "WIN"},
        {"sport": "nba", "canonical_team_id": "BOS",
          "event_time": "2025-04-01T20:00:00Z", "season": 2024,
          "team_score": 90, "opponent_score": 95, "result": "LOSS"},
    ])
    ev = _run(get_team_history(
        db, sport="NBA", canonical_team_id="BOS",
        as_of="2026-06-01T00:00:00Z",
    ))
    assert ev.season["sample_size"] == 2      # 2025 season rows
    assert ev.previous_season["sample_size"] == 1  # 2024
    # Multi-season aggregates the three seasons cur/cur-1/cur-2.
    assert ev.multi_season["sample_size"] == 3


# ═══════════════════════════════════════════════════════════════════
# Tennis / UFC — NOT_APPLICABLE
# ═══════════════════════════════════════════════════════════════════
def test_tennis_returns_not_applicable():
    from services.team_history import get_team_history
    db = _FakeDB()
    ev = _run(get_team_history(
        db, sport="Tennis", canonical_team_id=None, team_name=None,
        as_of="2026-06-01T00:00:00Z",
    ))
    assert ev.status == "NOT_APPLICABLE"
    assert ev.source == "SPORT_NOT_APPLICABLE"


def test_ufc_returns_not_applicable():
    from services.team_history import get_team_history
    db = _FakeDB()
    ev = _run(get_team_history(
        db, sport="UFC", canonical_team_id="doesn't-matter",
        as_of="2026-06-01T00:00:00Z",
    ))
    assert ev.status == "NOT_APPLICABLE"


# ═══════════════════════════════════════════════════════════════════
# Identity gating
# ═══════════════════════════════════════════════════════════════════
def test_missing_identity_returns_identity_unresolved():
    from services.team_history import get_team_history
    db = _FakeDB()
    ev = _run(get_team_history(
        db, sport="MLB",
        canonical_team_id=None, team_name=None,
        as_of="2026-06-01T00:00:00Z",
    ))
    assert ev.status == "TEAM_IDENTITY_UNRESOLVED"


def test_text_only_query_downgrades_identity_confidence():
    from services.team_history import get_team_history

    db = _FakeDB()
    _seed(db, [
        {"sport": "mlb", "team_name": "New York Yankees",
          "event_time": "2026-05-01T20:00:00Z", "season": 2026,
          "team_score": 5, "opponent_score": 3, "result": "WIN"},
    ])
    ev = _run(get_team_history(
        db, sport="MLB", team_name="New York Yankees",
        as_of="2026-06-01T00:00:00Z",
    ))
    assert ev.identity_confidence == "LOW"


# ═══════════════════════════════════════════════════════════════════
# §17 — Production-Truth cannot claim consumption from module existence
# ═══════════════════════════════════════════════════════════════════
def test_production_truth_history_surface_still_unknown():
    """The Team History module now exists but no production consumer
    calls it yet — the reachability HISTORY surface MUST remain
    UNKNOWN for a random pick (§16)."""
    from services.production_truth import (
        build_reachability_report,
        ConsumerSurface,
    )
    pick = {
        "id":                "p-team-1",
        "sport":             "MLB",
        "market":            "h2h",
        "book_odds":        -110,
        "publication_gate":  "canonical_barrier_passed",
        "lock_score":        90,
        "commence_time":     "2026-09-01T00:00:00Z",
        "home_team":         "NYY",
    }
    report = build_reachability_report(pick)
    assert report.consumers[ConsumerSurface.HISTORY.value]["status"] == \
        "UNKNOWN"


# ═══════════════════════════════════════════════════════════════════
# §15 — Team-history growth cannot mutate immutable pregame snapshot
# ═══════════════════════════════════════════════════════════════════
def test_pregame_snapshot_immutable_even_when_history_grows_later():
    """Freeze a pregame snapshot; simulate NEW team-history rows
    arriving later; re-verify the snapshot hash MUST still match
    (no retroactive rewrite)."""
    from services.production_truth.pregame_snapshot import (
        seal_snapshot,
        verify_snapshot_hash,
    )
    pick = {
        "canonical_prediction_id": "cpid-team-snap",
        "sport": "MLB",
        "market": "h2h",
        "book_odds": -140,
        "lock_score": 91,
        "evidence": {"team_history_L10_wins": 6},   # captured at
                                                    # publication time
    }
    snap = seal_snapshot(pick)
    assert verify_snapshot_hash(snap) is True
    # Simulate a background job that "would have" mutated evidence
    # after new games are ingested — the caller writing back into
    # the snapshot payload must invalidate the hash.
    snap["evidence"] = {"team_history_L10_wins": 9}
    assert verify_snapshot_hash(snap) is False


# ═══════════════════════════════════════════════════════════════════
# Player-History Stage 1 + Stage 2 regression protection
# ═══════════════════════════════════════════════════════════════════
def test_player_history_stage1_still_works():
    """Ensure Stage-3 additions didn't disturb Stage-1 MLB adapter."""
    from services.player_history import get_player_history
    db = _FakeDB()
    db["player_game_actuals"].docs.append({
        "sport": "mlb", "canonical_player_id": "cpid-judge",
        "event_time": "2026-05-01T20:00:00Z", "season": 2025,
        "team": "NYY", "opponent": "BOS", "home_away": "home",
        "actuals": {"h": 3, "hr": 1, "rbi": 4},
    })
    ev = _run(get_player_history(
        db, sport="MLB", canonical_player_id="cpid-judge",
        market="batter_hits", threshold=1.5, direction="over",
        event_time="2026-06-01T00:00:00Z",
    ))
    assert ev.last_5["result"]["wins"] == 1


def test_player_history_stage2_still_works():
    from services.player_history import get_player_history
    db = _FakeDB()
    db["player_game_actuals"].docs.append({
        "sport": "nba", "canonical_player_id": "cpid-lbj",
        "event_time": "2026-05-01T20:00:00Z", "season": 2025,
        "actuals": {"points": 30, "rebounds": 8, "assists": 5},
    })
    ev = _run(get_player_history(
        db, sport="NBA", canonical_player_id="cpid-lbj",
        market="player_points_rebounds_assists", threshold=42.5,
        event_time="2026-06-01T00:00:00Z",
    ))
    assert ev.last_5["result"]["wins"] == 1     # 43 > 42.5
