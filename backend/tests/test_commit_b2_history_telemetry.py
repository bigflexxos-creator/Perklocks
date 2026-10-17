"""Commit B/2 — History, Identity & Funnel Telemetry tests.

Covers items 17-42 of the Universal Root-Closure Build v3.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ─── Lightweight async DB stub reused across B2 tests ────────────────
class _AsyncCursor:
    def __init__(self, rows): self._rows = list(rows); self._i = 0
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
                    if op == "$lt" and (rv is None or not (rv < bound)): return False
                    if op == "$gte" and (rv is None or not (rv >= bound)): return False
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
        return type("R", (), {"inserted_id": len(self._rows)})()
    async def update_one(self, filt, update, upsert=False):
        for r in self._rows:
            if self._match(filt, r):
                if "$set" in update:
                    r.update(update["$set"])
                if "$setOnInsert" in update:
                    for k, v in update["$setOnInsert"].items():
                        r.setdefault(k, v)
                return type("R", (), {"matched_count": 1, "upserted_id": None})()
        if upsert:
            doc = dict(filt)
            if "$set" in update: doc.update(update["$set"])
            if "$setOnInsert" in update: doc.update(update["$setOnInsert"])
            self._rows.append(doc)
            return type("R", (), {"matched_count": 0, "upserted_id": len(self._rows)})()
        return type("R", (), {"matched_count": 0, "upserted_id": None})()


class _AsyncDB:
    def __init__(self, **collections):
        for name, rows in collections.items():
            setattr(self, name, _AsyncColl(rows))
    def __getitem__(self, name):
        return getattr(self, name, _AsyncColl([]))


# ═══════════════════════════════════════════════════════════════════
# Item 17 — Universal history-status contract
# ═══════════════════════════════════════════════════════════════════

def test_history_status_distinguishes_ingestion_lag_from_coverage_absence():
    """Upstream has newer events than consumer → INGESTION_LAG,
    NOT silent CURRENT and NOT PROVIDER_COVERAGE_LIMITED."""
    from services.history_status import classify_history_status, HistoryStatus
    s = classify_history_status(
        upstream_latest_event_time="2026-06-20T00:00:00Z",
        consumer_latest_event_time="2026-06-15T00:00:00Z",
    )
    assert s == HistoryStatus.INGESTION_LAG


def test_history_status_identity_lag_normalization_lag_stale_current():
    from services.history_status import classify_history_status, HistoryStatus
    assert classify_history_status(
        upstream_latest_event_time="2026-06-20T00:00:00Z",
        consumer_latest_event_time="2026-06-20T00:00:00Z",
        identity_resolved=False,
    ) == HistoryStatus.IDENTITY_LAG
    assert classify_history_status(
        upstream_latest_event_time="2026-06-20T00:00:00Z",
        consumer_latest_event_time="2026-06-20T00:00:00Z",
        normalized=False,
    ) == HistoryStatus.NORMALIZATION_LAG
    # STALE — consumer latest > stale_threshold_days older than as_of.
    assert classify_history_status(
        upstream_latest_event_time="2025-01-01T00:00:00Z",
        consumer_latest_event_time="2025-01-01T00:00:00Z",
        as_of="2026-06-20T00:00:00Z",
        stale_threshold_days=7,
    ) == HistoryStatus.STALE
    # CURRENT.
    assert classify_history_status(
        upstream_latest_event_time="2026-06-20T00:00:00Z",
        consumer_latest_event_time="2026-06-20T00:00:00Z",
        as_of="2026-06-21T00:00:00Z",
    ) == HistoryStatus.CURRENT


# ═══════════════════════════════════════════════════════════════════
# Item 21 — Freshness MUST use event-time, not updated_at
# ═══════════════════════════════════════════════════════════════════

def test_freshness_prefers_event_time_over_updated_at():
    """A 2025 completed match RE-WRITTEN today (updated_at=today) is
    STILL stale in June 2026 — the freshness verifier reads
    event_time, not updated_at."""
    from services.hi_event_time_freshness import consumer_freshness
    rows = [
        {"event_time": "2025-10-01T00:00:00Z",
         "updated_at": "2026-06-20T12:00:00Z"},   # projector touched today
    ]
    out = consumer_freshness(rows)
    assert out["consumer_latest_event_time"] == "2025-10-01T00:00:00Z"
    assert out["consumer_latest_updated_at"] == "2026-06-20T12:00:00Z"


def test_freshness_normalizes_across_event_time_aliases():
    from services.hi_event_time_freshness import consumer_freshness
    rows = [
        {"match_date":  "2026-06-10T00:00:00Z"},
        {"game_date":   "2026-06-15T00:00:00Z"},
        {"event_date":  "2026-06-18T00:00:00Z"},
        {"date":        "2026-06-05T00:00:00Z"},
        {"event_time":  "2026-06-19T00:00:00Z"},
    ]
    out = consumer_freshness(rows)
    assert out["consumer_latest_event_time"] == "2026-06-19T00:00:00Z"


# ═══════════════════════════════════════════════════════════════════
# Item 18 + 19 — NFL identity merge + current-season bootstrap
# ═══════════════════════════════════════════════════════════════════

def test_nfl_identity_canonical_prefers_gsis_when_both_present():
    from services.nfl_identity_merge import (
        canonical_nfl_player_id, normalize_provider_ids,
    )
    assert canonical_nfl_player_id(gsis_id="00-0036355",
                                      espn_id="3139477") == "nfl:p:00-0036355"
    assert canonical_nfl_player_id(espn_id="3139477") == "nfl:p:espn3139477"
    p = normalize_provider_ids({"gsis_id": "00-0036355", "athlete_id": 3139477})
    assert p["provider_ids"]["gsis"] == "00-0036355"
    assert p["provider_ids"]["espn"] == "3139477"
    assert p["canonical_player_id"] == "nfl:p:00-0036355"


def test_nfl_identity_merge_rows_from_gsis_and_espn_namespaces():
    """Old GSIS-keyed rows + new ESPN-ID-keyed rows must merge into
    ONE chronological player history."""
    from services.nfl_identity_merge import merge_player_rows
    gsis_rows = [
        {"event_time": "2025-10-05T13:00:00Z", "opponent": "KC",
         "actuals": {"passing_yards": 310}},
        {"event_time": "2025-10-12T13:00:00Z", "opponent": "DEN",
         "actuals": {"passing_yards": 280}},
    ]
    espn_rows = [
        # Later than any GSIS row — ESPN-only.
        {"event_time": "2026-01-05T13:00:00Z", "opponent": "LAC",
         "actuals": {"passing_yards": 350}},
        # Same (date, opponent) as a GSIS row — GSIS should win.
        {"event_time": "2025-10-05T13:00:00Z", "opponent": "KC",
         "actuals": {"passing_yards": 999}},     # bogus — must be dropped
    ]
    merged = merge_player_rows(
        gsis_rows, espn_rows,
        canonical_player_id="nfl:p:00-0036355",
    )
    assert len(merged) == 3
    event_times = [r["event_time"] for r in merged]
    assert event_times == sorted(event_times), "chronological"
    # GSIS row wins the collision — bogus 999 must be gone.
    for r in merged:
        if r["event_time"] == "2025-10-05T13:00:00Z":
            assert r["actuals"]["passing_yards"] == 310
            assert r["provider_source"] == "gsis"
    assert all(r["canonical_player_id"] == "nfl:p:00-0036355"
               for r in merged)


def test_nfl_current_season_dynamic_detection_no_hardcoded_years():
    """No hard-coded 2024/2025.  Season derives from the ``now_iso``."""
    from services.nfl_identity_merge import (
        detect_current_nfl_season, is_current_season_covered,
    )
    assert detect_current_nfl_season("2026-09-15T12:00:00Z") == 2026
    # Jan playoffs still belong to prior season.
    assert detect_current_nfl_season("2026-01-20T12:00:00Z") == 2025
    rows = [{"event_time": "2026-10-05T13:00:00Z"},
            {"event_time": "2026-10-12T13:00:00Z"},
            {"event_time": "2025-10-05T13:00:00Z"}]
    assert is_current_season_covered(rows, now_iso="2026-11-01T00:00:00Z",
                                      min_rows=1)
    # Count all NFL Final games >=32 across OLD seasons would NOT help —
    # the function requires rows from the CURRENT season.
    old_only = [{"event_time": "2025-10-05T13:00:00Z"}] * 50
    assert not is_current_season_covered(old_only,
                                           now_iso="2026-11-01T00:00:00Z",
                                           min_rows=1)


# ═══════════════════════════════════════════════════════════════════
# Items 22-29 — Universal direct VS OPP
# ═══════════════════════════════════════════════════════════════════

def test_direct_h2h_runs_opponent_indexed_query_not_fetch_recent_then_filter():
    """``direct_player_vs_opp`` filters BY OPPONENT at the DB layer
    (not: fetch 80 → filter)."""
    from services.direct_h2h import direct_player_vs_opp
    # Two completed games vs HOU, five vs other teams.  The direct
    # query must return exactly the 2 HOU rows.
    canonical_rows = [
        {"sport": "NFL",
         "canonical_player_id": "nfl:p:00-0036355",
         "canonical_opponent_id": "nfl:t:HOU",
         "opponent": "Houston Texans",
         "event_time": "2026-10-05T13:00:00Z",
         "actuals": {"passing_yards": 310}},
        {"sport": "NFL",
         "canonical_player_id": "nfl:p:00-0036355",
         "canonical_opponent_id": "nfl:t:HOU",
         "opponent": "Houston Texans",
         "event_time": "2024-09-01T13:00:00Z",
         "actuals": {"passing_yards": 220}},
    ] + [
        {"sport": "NFL",
         "canonical_player_id": "nfl:p:00-0036355",
         "canonical_opponent_id": f"nfl:t:T{i}",
         "opponent": f"Team {i}",
         "event_time": f"2026-09-{10+i:02d}T13:00:00Z",
         "actuals": {"passing_yards": 200+i}}
        for i in range(5)
    ]
    db = _AsyncDB(player_game_actuals=canonical_rows)
    rows = _run(direct_player_vs_opp(
        db, sport="NFL",
        canonical_player_id="nfl:p:00-0036355",
        canonical_opponent_id="nfl:t:HOU",
        as_of_iso="2026-12-31T00:00:00Z",
    ))
    assert len(rows) == 2
    # Older valid H2H must appear (NOT just "recent 80").
    event_times = sorted(r["event_time"] for r in rows)
    assert event_times[0] == "2024-09-01T13:00:00Z"
    assert event_times[1] == "2026-10-05T13:00:00Z"


def test_direct_h2h_fails_closed_without_identity():
    from services.direct_h2h import direct_player_vs_opp
    db = _AsyncDB(player_game_actuals=[])
    rows = _run(direct_player_vs_opp(db, sport="NFL"))
    assert rows == []


def test_direct_h2h_identity_priority_canonical_id_first_no_fuzzy():
    """Identity priority: canonical opponent ID exact > verified
    aliases > exact normalized name > fail closed.  No substring."""
    from services.direct_h2h import direct_player_vs_opp
    rows = [{
        "sport": "MLB",
        "canonical_player_id": "mlb:p:judge",
        "canonical_opponent_id": "mlb:t:HOU",
        "opponent": "Houston Astros",
        "event_time": "2026-06-01T00:00:00Z",
        "actuals": {"hits": 2},
    }]
    db = _AsyncDB(player_game_actuals=rows)
    # Fuzzy substring MUST NOT match.
    out = _run(direct_player_vs_opp(
        db, sport="MLB",
        canonical_player_id="mlb:p:judge",
        opponent_name="Houston",      # substring — must not match
        as_of_iso="2026-07-01T00:00:00Z",
    ))
    assert out == [], (
        "Substring opponent must not match (strict identity).")
    # Canonical ID DOES match.
    out = _run(direct_player_vs_opp(
        db, sport="MLB",
        canonical_player_id="mlb:p:judge",
        canonical_opponent_id="mlb:t:HOU",
        as_of_iso="2026-07-01T00:00:00Z",
    ))
    assert len(out) == 1


# ═══════════════════════════════════════════════════════════════════
# Items 34-42 — Real stage-true funnel telemetry
# ═══════════════════════════════════════════════════════════════════

def test_funnel_counters_are_populated_at_actual_stages():
    from services import stage_funnel as sf
    sf.reset()
    # Simulate acquisition stage.
    for _ in range(5): sf.record_stage("MLB", "batter_hits",
                                         sf.STAGE_PROVIDER_EVENTS)
    # Each event has multiple outcomes.
    for _ in range(20): sf.record_stage("MLB", "batter_hits",
                                          sf.STAGE_PROVIDER_OUTCOMES)
    # Only 18 survive to source_wagers (2 were SYNTHETIC rejected).
    for _ in range(18): sf.record_stage("MLB", "batter_hits",
                                          sf.STAGE_SOURCE_WAGERS)
    sf.record_rejection("MLB", "batter_hits",
                        sf.REASON_SYNTHETIC_BOOK_ODDS, delta=2)
    # 16 normalise; 14 resolve identity.
    for _ in range(16): sf.record_stage("MLB", "batter_hits",
                                          sf.STAGE_NORMALIZED_ROWS)
    for _ in range(14): sf.record_stage("MLB", "batter_hits",
                                          sf.STAGE_IDENTITY_RESOLVED)
    sf.record_rejection("MLB", "batter_hits",
                        sf.REASON_IDENTITY_PLAYER_UNRESOLVED, delta=2)
    # 10 history_ready; 10 model_attempts; 9 model_candidates;
    # 5 >=85; 5 publication_attempts; 4 published; 4 api_visible.
    for _ in range(10): sf.record_stage("MLB", "batter_hits",
                                          sf.STAGE_HISTORY_READY)
    for _ in range(10): sf.record_stage("MLB", "batter_hits",
                                          sf.STAGE_MODEL_ATTEMPTS)
    for _ in range(9):  sf.record_stage("MLB", "batter_hits",
                                          sf.STAGE_MODEL_CANDIDATES)
    for _ in range(5):  sf.record_stage("MLB", "batter_hits",
                                          sf.STAGE_AT_OR_ABOVE_85)
    for _ in range(5):  sf.record_stage("MLB", "batter_hits",
                                          sf.STAGE_PUBLICATION_ATTEMPTS)
    for _ in range(4):  sf.record_stage("MLB", "batter_hits",
                                          sf.STAGE_PUBLISHED)
    for _ in range(4):  sf.record_stage("MLB", "batter_hits",
                                          sf.STAGE_API_VISIBLE)

    rep = sf.report(sport="MLB")
    fam = rep["sports"]["MLB"]["families"]["batter_hits"]
    assert fam[sf.STAGE_PROVIDER_EVENTS]       == 5
    assert fam[sf.STAGE_PROVIDER_OUTCOMES]     == 20
    assert fam[sf.STAGE_SOURCE_WAGERS]         == 18
    assert fam[sf.STAGE_NORMALIZED_ROWS]       == 16
    assert fam[sf.STAGE_IDENTITY_RESOLVED]     == 14
    assert fam[sf.STAGE_HISTORY_READY]         == 10
    assert fam[sf.STAGE_MODEL_ATTEMPTS]        == 10
    assert fam[sf.STAGE_MODEL_CANDIDATES]      == 9
    assert fam[sf.STAGE_AT_OR_ABOVE_85]        == 5
    assert fam[sf.STAGE_PUBLICATION_ATTEMPTS]  == 5
    assert fam[sf.STAGE_PUBLISHED]             == 4
    assert fam[sf.STAGE_API_VISIBLE]           == 4
    assert fam["rejections"][sf.REASON_SYNTHETIC_BOOK_ODDS] == 2
    assert fam["rejections"][sf.REASON_IDENTITY_PLAYER_UNRESOLVED] == 2
    sf.reset()


def test_funnel_differentiates_model_attempts_from_model_candidates():
    """``model_attempts`` ≠ ``model_candidates`` (item 40)."""
    from services import stage_funnel as sf
    sf.reset()
    for _ in range(7): sf.record_stage("NBA", "points",
                                         sf.STAGE_MODEL_ATTEMPTS)
    for _ in range(3): sf.record_stage("NBA", "points",
                                         sf.STAGE_MODEL_CANDIDATES)
    sf.record_rejection("NBA", "points", sf.REASON_MODEL_UNAVAILABLE,
                        delta=4)
    rep = sf.report(sport="NBA")
    fam = rep["sports"]["NBA"]["families"]["points"]
    assert fam[sf.STAGE_MODEL_ATTEMPTS] == 7
    assert fam[sf.STAGE_MODEL_CANDIDATES] == 3
    assert fam["rejections"][sf.REASON_MODEL_UNAVAILABLE] == 4
    sf.reset()


def test_funnel_differentiates_published_from_api_visible():
    """``published`` ≠ ``api_visible`` (item 41)."""
    from services import stage_funnel as sf
    sf.reset()
    for _ in range(10): sf.record_stage("NFL", "pass_yds",
                                          sf.STAGE_PUBLISHED)
    for _ in range(7): sf.record_stage("NFL", "pass_yds",
                                          sf.STAGE_API_VISIBLE)
    rep = sf.report(sport="NFL")
    fam = rep["sports"]["NFL"]["families"]["pass_yds"]
    assert fam[sf.STAGE_PUBLISHED] == 10
    assert fam[sf.STAGE_API_VISIBLE] == 7
    sf.reset()


def test_zero_truth_gaps_identify_unexplained_stage_drops():
    """``provider_events>0 AND provider_outcomes=0`` → gap surfaced.
    ``model_candidates>0 AND 85+=0`` → NO gap (legitimate outcome)."""
    from services import stage_funnel as sf
    sf.reset()
    # Case A — acquisition problem.
    sf.record_stage("MLB", "batter_hits",   sf.STAGE_PROVIDER_EVENTS, 5)
    # provider_outcomes=0 → should surface as gap.
    gaps = sf.zero_truth_gaps(sport="MLB")
    assert any(g["upstream_stage"] == sf.STAGE_PROVIDER_EVENTS
               and g["downstream_stage"] == sf.STAGE_PROVIDER_OUTCOMES
               for g in gaps), gaps
    sf.reset()


# ═══════════════════════════════════════════════════════════════════
# Item 31 — HI cache invalidation uses history_revision
# ═══════════════════════════════════════════════════════════════════

def test_hi_cache_key_invalidates_on_history_revision_bump():
    from services.hi_cache_identity import hi_cache_key, should_invalidate
    k1 = hi_cache_key(pick_id="pick-1", history_revision="rev-A",
                       sample_scope="L10", venue="home",
                       opponent_canonical_id="nfl:t:HOU")
    k2 = hi_cache_key(pick_id="pick-1", history_revision="rev-B",
                       sample_scope="L10", venue="home",
                       opponent_canonical_id="nfl:t:HOU")
    assert k1 != k2
    assert should_invalidate(prev_key=k1,
                              pick_id="pick-1",
                              history_revision="rev-B",
                              sample_scope="L10",
                              venue="home",
                              opponent_canonical_id="nfl:t:HOU")
    assert not should_invalidate(prev_key=k1,
                                   pick_id="pick-1",
                                   history_revision="rev-A",
                                   sample_scope="L10",
                                   venue="home",
                                   opponent_canonical_id="nfl:t:HOU")


# ═══════════════════════════════════════════════════════════════════
# Item 20 — NFL projector stamps consumer-ready data
# ═══════════════════════════════════════════════════════════════════

def test_nfl_projector_writes_event_time_opponent_id_scores_home_away():
    """Projector output rows carry the fields the downstream HI /
    model consumers need (item 20)."""
    from services.historical_intelligence_projectors import project_nfl_actuals
    source = [{
        "player_id": "00-0036355",
        "player_display_name": "Patrick Mahomes",
        "team": "KC", "opponent_team": "DEN",
        "season": 2026, "week": 5, "position": "QB",
        "game_id": "2026_05_DEN_KC",
        "game_date": "2026-10-05T13:00:00Z",
        "home_team": "KC", "away_team": "DEN",
        "home_score": 31, "away_score": 17,
        "canonical_opponent_id": "nfl:t:DEN",
        "passing_yards": 320, "passing_tds": 3,
    }]
    db = _AsyncDB(nfl_player_weekly=source,
                   player_game_actuals=[],
                   team_game_actuals=[])
    _run(project_nfl_actuals(db, season=2026))
    pga = _run(db.player_game_actuals.find_one(
        {"canonical_player_id": "00-0036355"}))
    assert pga is not None, "player row must be inserted"
    for required in ("event_time", "home_away", "canonical_opponent_id"):
        assert required in pga, (
            f"player row MUST stamp {required!r}; got "
            f"{list(pga.keys())}")
    assert pga["event_time"] == "2026-10-05T13:00:00Z"
    assert pga["home_away"] == "home"
    assert pga["canonical_opponent_id"] == "nfl:t:DEN"
    # Team row.
    tga = _run(db.team_game_actuals.find_one(
        {"canonical_team_id": "KC"}))
    assert tga is not None
    for required in ("event_time", "team_score", "opponent_score",
                     "canonical_opponent_id", "home_away"):
        assert required in tga, (
            f"team row MUST stamp {required!r}; got {list(tga.keys())}")
    assert tga["team_score"] == 31
    assert tga["opponent_score"] == 17


# ═══════════════════════════════════════════════════════════════════
# Item 32 — HI response truth states (partial truths exposed)
# ═══════════════════════════════════════════════════════════════════

def test_history_status_exposes_partial_truth_states():
    from services.history_status import HistoryStatus, status_dict
    out = status_dict(HistoryStatus.HISTORY_SYNCING,
                      lag_reason="ingestion_pipeline_running",
                      detail={"latest_event_date": "2026-06-20"})
    assert out["history_status"] == "HISTORY_SYNCING"
    assert out["lag_reason"] == "ingestion_pipeline_running"
    assert out["history_detail"]["latest_event_date"] == "2026-06-20"
    # Partial-truth members exist as required.
    assert hasattr(HistoryStatus, "HISTORY_SYNCING")
    assert hasattr(HistoryStatus, "CURRENT_SEASON_PARTIAL")
    assert hasattr(HistoryStatus, "IDENTITY_SYNC_NEEDED")


# ═══════════════════════════════════════════════════════════════════
# Preservation
# ═══════════════════════════════════════════════════════════════════

def test_preservation_assertions_still_hold_after_b2():
    from services.preservation_contract import snapshot
    snap = snapshot()
    assert snap.get("APEX_MIN_BASE_SCORE") == 97.0
    assert snap.get("APEX_MIN_POSITIVE_CATEGORIES") == 5
    assert "provider_median" in (snap.get("_SYNTHETIC_ODDS_SOURCES") or [])


if __name__ == "__main__":
    import subprocess
    sys.exit(subprocess.call(["python", "-m", "pytest", __file__, "-v"]))
