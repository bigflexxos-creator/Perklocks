"""MAGIC 3E — GOLD DATA INGESTION CLOSURE tests.

Covers:
  * xA authoritative wiring (bug fix from 3D)
  * Soccer recent-role adapter (temporal safety + honest availability)
  * Soccer teammate-context adapter (PARTIAL only)
  * NBA injury / usage / rest adapters
  * Lineup status vocabulary + freshness class
  * Identity safety (wrong-team refuse)
  * Temporal safety (future-log leak refuse)
  * Contradiction integration
  * Legacy orphan proof
  * Full-suite regression pins
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta

import pytest

from services.magic.gold_evidence import (
    Availability, build_soccer_creation, build_soccer_matchup,
    build_soccer_shot_quality, build_tennis_workload,
)
from services.magic.gold_evidence_ext import (
    LineupStatus, ExtGoldEvidenceType,
    build_soccer_recent_role, build_soccer_teammate_context,
    build_nba_injury_status, build_nba_recent_usage,
    build_nba_rest_context,
)


# ── Shared in-memory Mongo fake ──────────────────────────────────

class _Coll:
    def __init__(self, docs=None):
        self._docs = list(docs or [])
    async def find_one(self, q=None, projection=None, sort=None):
        q = q or {}
        matched = [d for d in self._docs if self._match(d, q)]
        if sort:
            key, direction = sort[0]
            matched.sort(key=lambda d: d.get(key) or "",
                         reverse=(direction == -1))
        return matched[0] if matched else None
    def find(self, q=None, projection=None):
        q = q or {}
        matched = [d for d in self._docs if self._match(d, q)]
        class _C:
            def __init__(self, a):
                self.a = a; self.i = 0
                self._sort = None; self._limit = None
            def sort(self, s):
                key, direction = s[0]
                self.a = sorted(self.a,
                                 key=lambda d: d.get(key) or "",
                                 reverse=(direction == -1))
                return self
            def limit(self, n):
                self.a = self.a[:n]; return self
            def __aiter__(self): return self
            async def __anext__(self):
                if self.i >= len(self.a):
                    raise StopAsyncIteration
                d = self.a[self.i]; self.i += 1; return d
        return _C(matched)
    async def count_documents(self, q):
        return sum(1 for d in self._docs if self._match(d, q))
    async def aggregate(self, pipeline):
        class _E:
            def __aiter__(self): return self
            async def __anext__(self): raise StopAsyncIteration
        return _E()
    async def update_one(self, filt, update, upsert=False):
        for d in self._docs:
            if self._match(d, filt):
                for k, v in (update.get("$set") or {}).items():
                    d[k] = v
                class _R:
                    matched_count = 1; modified_count = 1
                return _R()
        if upsert:
            new = dict((update.get("$set") or {}))
            new.update(filt)
            self._docs.append(new)
        class _R:
            matched_count = 0; modified_count = 0
        return _R()

    @staticmethod
    def _match(d, q):
        for k, v in q.items():
            if k == "$or":
                if not any(_Coll._match(d, sub) for sub in v):
                    return False
                continue
            dv = d.get(k)
            if isinstance(v, dict):
                if "$in" in v and dv not in v["$in"]: return False
                if "$gte" in v and not (dv is not None and dv >= v["$gte"]): return False
                if "$lt" in v and not (dv is not None and dv <  v["$lt"]):  return False
                if "$type" in v: pass
                if "$exists" in v:
                    ex = k in d and d[k] is not None
                    if ex != bool(v["$exists"]): return False
                if "$ne" in v and dv == v["$ne"]: return False
            else:
                if dv != v: return False
        return True


class _DB:
    def __init__(self, colls=None):
        self._c = colls or {}
    def __getattr__(self, n):
        return self._c.setdefault(n, _Coll())
    def __getitem__(self, n):
        return self._c.setdefault(n, _Coll())


# ═══════════════════════════════════════════════════════════════════
# 3E.b — xA authoritative wiring (the 3D bug fix)
# ═══════════════════════════════════════════════════════════════════

def test_soccer_creation_authoritative_xa_now_available():
    """When soccer_player_form.xa IS persisted, creator evidence must
    reach AVAILABLE with matchup_feature='xa_per_90'."""
    db = _DB({"soccer_player_form": _Coll([
        {"player_name": "Kevin De Bruyne",
         "name_canonical": "kevin de bruyne",
         "xa": 12.5, "key_passes": 90, "assists": 12,
         "minutes": 2700, "games": 30, "position": "MF",
         "team": "MCI", "league": "EPL", "season": 2025},
    ])})
    pick = {"sport": "Soccer",
             "player_name": "Kevin De Bruyne",
             "market": "Kevin De Bruyne Anytime Assist"}
    ev = asyncio.run(build_soccer_creation(db, pick))
    assert ev.availability == Availability.AVAILABLE
    assert ev.matchup_feature == "xa_per_90"
    assert ev.provenance["xa"] == 12.5
    assert ev.source == "soccer_player_form.xa"


def test_soccer_creation_falls_back_to_key_pass_proxy_when_xa_missing():
    """xA missing → falls back to key_passes PARTIAL proxy, NEVER
    labels the proxy as xA."""
    db = _DB({"soccer_player_form": _Coll([
        {"player_name": "Small League Winger",
         "name_canonical": "small league winger",
         "key_passes": 45, "assists": 5, "minutes": 2000,
         "games": 25, "position": "MF", "team": "SLC",
         "league": "MinorLeague"},
    ])})
    pick = {"sport": "Soccer",
             "player_name": "Small League Winger",
             "market": "Anytime Assist"}
    ev = asyncio.run(build_soccer_creation(db, pick))
    assert ev.availability == Availability.PARTIAL
    assert ev.matchup_feature == "key_passes_per_90"
    # Must NOT claim to be xA
    assert "xa" not in (ev.matchup_feature or "").lower() or \
           ev.provenance.get("note", "").lower().find("proxy") >= 0


def test_soccer_creation_unavailable_when_no_form_record():
    db = _DB()
    ev = asyncio.run(build_soccer_creation(
        db, {"sport": "Soccer", "player_name": "Unknown Player"}))
    assert ev.availability == Availability.UNAVAILABLE


def test_soccer_creation_not_confused_with_shot_quality():
    """Assist pathway must NOT reuse goal-only evidence type."""
    from services.magic.gold_evidence import GoldEvidenceType
    assert GoldEvidenceType.SOCCER_CREATION != GoldEvidenceType.SOCCER_SHOT_QUALITY


# ═══════════════════════════════════════════════════════════════════
# 3E.b — Soccer recent-role adapter
# ═══════════════════════════════════════════════════════════════════

def test_soccer_recent_role_available_with_5_recent_games():
    db = _DB({"soccer_player_game_logs": _Coll([
        {"player_name": "Erling Haaland",
         "name_canonical": "erling haaland",
         "match_date": f"2026-06-{d:02d} 15:00:00",
         "minutes": 88, "starts": 1, "position": "F",
         "team_name": "Manchester City",
         "opponent_team_name": "Arsenal"}
        for d in (1, 3, 5, 7, 9)
    ])})
    pick = {"sport": "Soccer", "player_name": "Erling Haaland",
             "event_time": "2026-06-15T18:00:00Z"}
    ev = asyncio.run(build_soccer_recent_role(db, pick))
    assert ev.availability == Availability.AVAILABLE
    assert ev.matchup_feature == "avg_minutes_last_5"
    assert ev.provenance["starts_last_5"] == 5
    assert ev.value == 88.0


def test_soccer_recent_role_partial_small_sample():
    db = _DB({"soccer_player_game_logs": _Coll([
        {"player_name": "Rookie Winger",
         "match_date": "2026-06-05 15:00:00",
         "minutes": 20, "starts": 0}
    ])})
    ev = asyncio.run(build_soccer_recent_role(db, {
        "sport": "Soccer", "player_name": "Rookie Winger",
        "event_time": "2026-06-15T18:00:00Z"}))
    assert ev.availability == Availability.PARTIAL
    assert ev.sample_size == 1


def test_soccer_recent_role_excludes_same_day_and_future_logs():
    db = _DB({"soccer_player_game_logs": _Coll([
        # same-day AND future — MUST be excluded
        {"player_name": "Test Player",
         "match_date": "2026-06-15 15:00:00", "minutes": 90, "starts": 1},
        {"player_name": "Test Player",
         "match_date": "2026-06-20 15:00:00", "minutes": 90, "starts": 1},
    ])})
    ev = asyncio.run(build_soccer_recent_role(db, {
        "sport": "Soccer", "player_name": "Test Player",
        "event_time": "2026-06-15T18:00:00Z"}))
    assert ev.availability == Availability.UNAVAILABLE
    assert ev.provenance["cutoff_day"] == "2026-06-15"


def test_soccer_recent_role_unavailable_when_no_logs():
    db = _DB({"soccer_player_game_logs": _Coll()})
    ev = asyncio.run(build_soccer_recent_role(db, {
        "sport": "Soccer", "player_name": "Unknown Player",
        "event_time": "2026-06-15T18:00:00Z"}))
    assert ev.availability == Availability.UNAVAILABLE


# ═══════════════════════════════════════════════════════════════════
# 3E.c — Soccer teammate-context adapter (PARTIAL only)
# ═══════════════════════════════════════════════════════════════════

def test_soccer_teammate_context_never_available():
    """Teammate context is PARTIAL evidence by policy — never AVAILABLE."""
    db = _DB({"soccer_injuries": _Coll([
        {"team_name": "Manchester City",
         "injuries": [
             {"athlete": "Rodri", "status": "Out",
              "description": "ACL", "date": "2026-06-01"},
             {"athlete": "John Stones", "status": "Questionable",
              "description": "hamstring", "date": "2026-06-10"},
         ],
         "updated_at": "2026-06-14T12:00:00Z"}
    ])})
    ev = asyncio.run(build_soccer_teammate_context(db, {
        "sport": "Soccer", "team": "Manchester City",
        "player_name": "Erling Haaland"}))
    assert ev.availability == Availability.PARTIAL
    assert ev.provenance["n_out"] == 1
    assert ev.provenance["n_questionable"] == 1
    assert "Rodri" in ev.provenance["teammates_out"]


def test_soccer_teammate_context_unavailable_when_no_feed():
    db = _DB()
    ev = asyncio.run(build_soccer_teammate_context(db, {
        "sport": "Soccer", "team": "Nobody FC"}))
    assert ev.availability == Availability.UNAVAILABLE


def test_soccer_teammate_context_unavailable_without_team():
    db = _DB({"soccer_injuries": _Coll([{"team_name": "X"}])})
    ev = asyncio.run(build_soccer_teammate_context(db, {"sport": "Soccer"}))
    assert ev.availability == Availability.UNAVAILABLE


# ═══════════════════════════════════════════════════════════════════
# 3E.d — NBA injury adapter
# ═══════════════════════════════════════════════════════════════════

def test_nba_injury_status_returns_out_when_player_listed_out():
    now_iso = datetime.now(timezone.utc).isoformat()
    db = _DB({"espn_injury_notes": _Coll([
        {"sport": "NBA", "team_id": "1", "team_name": "Atlanta Hawks",
         "updated_at": now_iso,
         "injuries": [{"athlete": "Trae Young", "status": "Out",
                        "severity": 3, "description": "ankle",
                        "date": "2026-06-14"}]},
    ])})
    ev = asyncio.run(build_nba_injury_status(db, {
        "sport": "NBA", "player_name": "Trae Young",
        "market": "Trae Young Over 27.5 Points"}))
    assert ev.availability == Availability.AVAILABLE
    assert ev.provenance["status"] == LineupStatus.OUT
    assert ev.value == -1.0
    assert ev.direction == "negative"


def test_nba_injury_status_healthy_when_not_on_any_list():
    now_iso = datetime.now(timezone.utc).isoformat()
    db = _DB({"espn_injury_notes": _Coll([
        {"sport": "NBA", "team_name": "Atlanta Hawks",
         "updated_at": now_iso,
         "injuries": [{"athlete": "Somebody Else", "status": "Out"}]},
    ])})
    ev = asyncio.run(build_nba_injury_status(db, {
        "sport": "NBA", "player_name": "Nikola Jokic"}))
    assert ev.availability == Availability.AVAILABLE
    assert ev.value == 1.0
    assert ev.direction == "positive"


def test_nba_injury_status_stale_when_feed_older_than_48h():
    old = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
    db = _DB({"espn_injury_notes": _Coll([
        {"sport": "NBA", "team_name": "Atlanta Hawks",
         "updated_at": old,
         "injuries": [{"athlete": "Trae Young", "status": "Out"}]},
    ])})
    ev = asyncio.run(build_nba_injury_status(db, {
        "sport": "NBA", "player_name": "Trae Young"}))
    assert ev.availability == Availability.STALE


def test_nba_injury_status_unavailable_when_no_feed():
    db = _DB({"espn_injury_notes": _Coll()})
    ev = asyncio.run(build_nba_injury_status(db, {
        "sport": "NBA", "player_name": "Anyone"}))
    assert ev.availability == Availability.UNAVAILABLE


# ═══════════════════════════════════════════════════════════════════
# 3E.d — NBA recent-usage adapter
# ═══════════════════════════════════════════════════════════════════

def test_nba_recent_usage_available_for_points_market():
    db = _DB({"player_game_actuals": _Coll([
        {"sport": "nba", "canonical_player_id": "2490149",
         "event_time": f"2026-06-{d:02d}T00:00:00Z",
         "actuals": {"points": pts, "rebounds": 5, "assists": 4,
                      "minutes": 34}}
        for d, pts in ((1, 22), (3, 28), (5, 30), (7, 24), (9, 26))
    ])})
    pick = {"sport": "NBA", "canonical_player_id": "2490149",
             "market": "Trae Young Over 24.5 Points",
             "line": 24.5,
             "event_time": "2026-06-15T00:00:00Z"}
    ev = asyncio.run(build_nba_recent_usage(db, pick))
    assert ev.availability == Availability.AVAILABLE
    assert ev.matchup_feature == "avg_points_last_5"
    assert ev.value == 26.0


def test_nba_recent_usage_market_family_isolation():
    """A rebounds pick must NOT consume points."""
    db = _DB({"player_game_actuals": _Coll([
        {"sport": "nba", "canonical_player_id": "2490149",
         "event_time": f"2026-06-{d:02d}T00:00:00Z",
         "actuals": {"points": 30, "rebounds": r}}
        for d, r in ((1, 5), (3, 6), (5, 4), (7, 7), (9, 8))
    ])})
    pick = {"sport": "NBA", "canonical_player_id": "2490149",
             "market": "Trae Young Over 5.5 Rebounds",
             "line": 5.5, "event_time": "2026-06-15T00:00:00Z"}
    ev = asyncio.run(build_nba_recent_usage(db, pick))
    assert ev.matchup_feature == "avg_rebounds_last_5"
    assert ev.value == 6.0


def test_nba_recent_usage_excludes_future_logs():
    """Future / same-cutoff logs must NOT leak in."""
    db = _DB({"player_game_actuals": _Coll([
        {"sport": "nba", "canonical_player_id": "cp1",
         "event_time": "2026-06-16T00:00:00Z",   # future
         "actuals": {"points": 40}},
        {"sport": "nba", "canonical_player_id": "cp1",
         "event_time": "2026-06-14T00:00:00Z",
         "actuals": {"points": 20}},
    ])})
    pick = {"sport": "NBA", "canonical_player_id": "cp1",
             "market": "Points", "line": 25.0,
             "event_time": "2026-06-15T00:00:00Z"}
    ev = asyncio.run(build_nba_recent_usage(db, pick))
    assert ev.value == 20.0  # only pre-cutoff row counted
    assert ev.provenance["n_games_last_5"] == 1


def test_nba_recent_usage_unavailable_without_canonical_id():
    ev = asyncio.run(build_nba_recent_usage(_DB(), {
        "sport": "NBA", "market": "Points",
        "player_name": "Foo Bar"}))
    assert ev.availability == Availability.UNAVAILABLE


# ═══════════════════════════════════════════════════════════════════
# 3E.d — NBA rest-context adapter
# ═══════════════════════════════════════════════════════════════════

def test_nba_rest_context_positive_when_rested():
    db = _DB({"player_game_logs": _Coll([
        {"sport": "nba", "player_id": "cp1", "date": "2026-06-10",
         "rest_days": 4, "is_b2b": 0, "is_home": 1,
         "opp_team_id": "13"},
    ])})
    ev = asyncio.run(build_nba_rest_context(db, {
        "sport": "NBA", "canonical_player_id": "cp1",
        "event_time": "2026-06-15T00:00:00Z"}))
    assert ev.availability == Availability.AVAILABLE
    assert ev.value == 4.0
    assert ev.direction == "positive"


def test_nba_rest_context_negative_when_b2b():
    db = _DB({"player_game_logs": _Coll([
        {"sport": "nba", "player_id": "cp1", "date": "2026-06-14",
         "rest_days": 0, "is_b2b": 1}
    ])})
    ev = asyncio.run(build_nba_rest_context(db, {
        "sport": "NBA", "canonical_player_id": "cp1",
        "event_time": "2026-06-15T00:00:00Z"}))
    assert ev.direction == "negative"


def test_nba_rest_context_unavailable_without_id():
    ev = asyncio.run(build_nba_rest_context(_DB(), {
        "sport": "NBA", "player_name": "X"}))
    assert ev.availability == Availability.UNAVAILABLE


# ═══════════════════════════════════════════════════════════════════
# LineupStatus vocabulary
# ═══════════════════════════════════════════════════════════════════

def test_lineup_status_vocabulary_locked():
    """Status names MUST match the contract exactly — downstream
    Magic contradiction consumers depend on these strings."""
    assert LineupStatus.CONFIRMED_STARTER == "CONFIRMED_STARTER"
    assert LineupStatus.CONFIRMED_BENCH   == "CONFIRMED_BENCH"
    assert LineupStatus.PREDICTED_STARTER == "PREDICTED_STARTER"
    assert LineupStatus.PREDICTED_BENCH   == "PREDICTED_BENCH"
    assert LineupStatus.QUESTIONABLE      == "QUESTIONABLE"
    assert LineupStatus.OUT               == "OUT"
    assert LineupStatus.SUSPENDED         == "SUSPENDED"
    assert LineupStatus.UNKNOWN           == "UNKNOWN"


def test_predicted_lineup_is_not_confirmed_lineup():
    """Predicted status MUST NOT be treated as confirmed."""
    assert LineupStatus.PREDICTED_STARTER != LineupStatus.CONFIRMED_STARTER
    assert LineupStatus.PREDICTED_BENCH   != LineupStatus.CONFIRMED_BENCH


# ═══════════════════════════════════════════════════════════════════
# Contradiction integration — reuse existing engine
# ═══════════════════════════════════════════════════════════════════

def test_contradiction_bench_vs_strong_scorer():
    from services.magic.contradictions import (
        detect_contradictions, RiskFlag,
    )
    from services.magic.contract import EvidenceItem, EvidenceType, Availability as A
    strong_hist = EvidenceItem(
        evidence_type=EvidenceType.HISTORICAL_EXACT_THRESHOLD,
        availability=A.AVAILABLE, value=0.72, sample_size=25,
    )
    flags = detect_contradictions(
        evidence=[strong_hist],
        identity_class="AUTHORITATIVE",
        starter_status="BENCH",
    )
    assert RiskFlag.HISTORICAL_STRONG_BUT_NOT_STARTER.value in flags


def test_contradiction_finishing_unsupported():
    from services.magic.contradictions import (
        detect_contradictions, RiskFlag,
    )
    flags = detect_contradictions(
        evidence=[], identity_class="AUTHORITATIVE",
        goals_over_xg_ratio=1.45,
    )
    assert RiskFlag.FINISHING_UNSUPPORTED_BY_SHOT_QUALITY.value in flags


# ═══════════════════════════════════════════════════════════════════
# LIVE reachability — production DB proof
# ═══════════════════════════════════════════════════════════════════

def test_soccer_creation_xa_live_reaches_available_for_big5():
    """Real proof: at least ONE big-5 player must reach AVAILABLE via
    the new xA path.  If Understat data is absent this becomes a
    documented data-scope issue rather than a wiring bug."""
    import os as _os
    from motor.motor_asyncio import AsyncIOMotorClient
    from dotenv import load_dotenv
    load_dotenv("/app/backend/.env")

    async def _run():
        db = AsyncIOMotorClient(_os.getenv("MONGO_URL"))["lockscore_db"]
        row = await db.soccer_player_form.find_one({
            "xa": {"$exists": True, "$gt": 3},
            "minutes": {"$gt": 500}})
        if not row:
            return   # data-scope: nothing to prove
        pick = {"sport": "Soccer",
                 "player_name": row.get("player_name"),
                 "market": f"{row.get('player_name')} Anytime Assist"}
        ev = await build_soccer_creation(db, pick)
        assert ev.availability == Availability.AVAILABLE
        assert ev.matchup_feature == "xa_per_90"
    asyncio.run(_run())


def test_legacy_orphan_repaired_no_immutable_touch():
    """MAGIC 3E Phase 24 — Confirm the legacy orphan repair only
    stamped `line_source` and left every settlement-truth field
    untouched.  Never break Phase 1/2 immutability."""
    import os as _os
    from motor.motor_asyncio import AsyncIOMotorClient
    from dotenv import load_dotenv
    load_dotenv("/app/backend/.env")

    async def _run():
        db = AsyncIOMotorClient(_os.getenv("MONGO_URL"))["lockscore_db"]
        r = await db.picks.find_one({"id": "pub_test_i_parity"})
        if r is None:
            return
        # Line_source must now be stamped (either by 3E orphan repair
        # OR by a subsequent 3a1 backfill run that stamped
        # `historical_selection_parse`).  Either way — no more orphan.
        assert r.get("line_source") in (
            "test_synthetic_placeholder",
            "historical_selection_parse",
        ), f"unexpected line_source: {r.get('line_source')!r}"
        # Immutable fields MUST all be unchanged (still None/orig).
        assert r.get("status") is None
        assert r.get("settled_at") is None
        assert r.get("units_profit") is None
        assert r.get("units_risked") is None
        assert r.get("market") is None
        assert r.get("selection") is None
        assert r.get("book_odds") == -155  # original value
        assert r.get("closing_odds") is None
    asyncio.run(_run())


# ═══════════════════════════════════════════════════════════════════
# Regression pins (do-not-touch guard)
# ═══════════════════════════════════════════════════════════════════

def test_magic_3e_does_not_change_locked_constants():
    from brain.sim_runner import SIM_RESIDUAL_MAX, MIN_RUNS_FOR_ANCHOR
    from brain.calibration import (
        MIN_SAMPLE_FOR_OVERRIDE, MAX_OPTIMISM_BUFFER,
    )
    assert SIM_RESIDUAL_MAX == 3.0
    assert MIN_RUNS_FOR_ANCHOR == 10_000
    assert MIN_SAMPLE_FOR_OVERRIDE == 20
    assert MAX_OPTIMISM_BUFFER == 5.0
