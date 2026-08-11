"""Phase 5.3 — Player History Foundation + MLB tests.

Locks the invariants from Phase 5.3 §4-§12, §17-§21, §27.
Zero DB required for the threshold engine tests; MLB adapter uses
a fake in-memory DB.
"""
from __future__ import annotations

import asyncio
import sys

import pytest

sys.path.insert(0, "/app/backend")

pytestmark = pytest.mark.unit


# ═══════════════════════════════════════════════════════════════════
# §4 / §8 — Threshold Engine — Over / Under / Push
# ═══════════════════════════════════════════════════════════════════
def test_over_threshold_basic():
    from services.player_history.threshold_engine import evaluate_threshold
    r = evaluate_threshold([2, 1, 3, 2, 0], threshold=1.5, direction="over")
    assert r.wins == 3
    assert r.losses == 2
    assert r.pushes == 0
    assert r.decisions == 5
    assert r.hit_rate == pytest.approx(0.60)


def test_under_threshold_basic():
    from services.player_history.threshold_engine import evaluate_threshold
    r = evaluate_threshold([2, 1, 3, 2, 0], threshold=1.5, direction="under")
    assert r.wins == 2   # (1, 0)
    assert r.losses == 3
    assert r.pushes == 0
    assert r.decisions == 5


def test_whole_number_push_excluded_from_decisions():
    from services.player_history.threshold_engine import evaluate_threshold
    r = evaluate_threshold([2, 1, 3, 2, 0], threshold=2.0, direction="over")
    # actuals 2,2 → push; 3 → win; 1,0 → loss
    assert r.pushes == 2
    assert r.wins == 1
    assert r.losses == 2
    assert r.decisions == 3
    assert r.hit_rate == pytest.approx(1/3)


def test_half_line_never_pushes():
    from services.player_history.threshold_engine import evaluate_threshold
    r = evaluate_threshold([1.5, 2.5, 3.5], threshold=1.5, direction="over")
    # Line is 1.5 (fractional) — 1.5 > 1.5? No.  Not push either.
    assert r.pushes == 0


def test_missing_actuals_never_zero():
    """Phase 5.3 §3, §6, §27: None must be EXCLUDED, never counted
    as 0."""
    from services.player_history.threshold_engine import evaluate_threshold
    r = evaluate_threshold([2, None, 3, None, 0], threshold=1.5, direction="over")
    # None values excluded — sample_size=3, decisions=3.
    assert r.sample_size == 3
    assert r.decisions == 3
    assert r.wins == 2   # 2, 3
    assert r.losses == 1   # 0


def test_milestone_gte_semantics():
    """25+ yards, 1+ Hit, Anytime TD — inclusive lower bound."""
    from services.player_history.threshold_engine import evaluate_milestone
    r = evaluate_milestone([0, 1, 2, 0, 1], threshold=1.0, semantics="gte")
    assert r.wins == 3
    assert r.losses == 2
    assert r.pushes == 0   # milestones never push
    assert r.decisions == 5


def test_milestone_gte_never_pushes_on_whole_number():
    """1+ Hit with actual=1 IS a HIT, not a push (>= semantics)."""
    from services.player_history.threshold_engine import evaluate_milestone
    r = evaluate_milestone([1], threshold=1, semantics="gte")
    assert r.wins == 1
    assert r.pushes == 0


def test_alt_thresholds_reuse_same_actuals():
    """Phase 5.3 §5, §9: One raw actual supports arbitrary thresholds."""
    from services.player_history.threshold_engine import evaluate_milestone
    actual = [82]   # 82 rushing yards
    for thr, expected_hit in [(25, True), (50, True), (75, True), (100, False)]:
        r = evaluate_milestone(actual, threshold=thr, semantics="gte")
        assert (r.wins == 1) == expected_hit, f"thr={thr}"


def test_average_actual_computed_from_valid_only():
    from services.player_history.threshold_engine import evaluate_threshold
    r = evaluate_threshold([2, None, 4, None, 6], threshold=1.5, direction="over")
    assert r.average_actual == pytest.approx(4.0)


def test_direction_validation():
    from services.player_history.threshold_engine import evaluate_threshold
    with pytest.raises(ValueError):
        evaluate_threshold([1], threshold=0.5, direction="sideways")


# ═══════════════════════════════════════════════════════════════════
# §6, §7 — Derived-market missing component → None
# ═══════════════════════════════════════════════════════════════════
def test_hrr_missing_component_returns_none():
    from services.player_history.mlb import _extract_actual
    # Missing rbi → None (never 0).
    row = {"h": 2, "r": 1}   # missing rbi
    assert _extract_actual("batter_hits_runs_rbis", row) is None
    # All present → sum.
    row_full = {"h": 2, "r": 1, "rbi": 3}
    assert _extract_actual("batter_hits_runs_rbis", row_full) == 6.0


def test_hits_returns_zero_when_actually_zero():
    """Distinguish PLAYED-and-recorded-zero from missing.  If the
    row explicitly reports h=0, actual is 0.0 (not None)."""
    from services.player_history.mlb import _extract_actual
    assert _extract_actual("batter_hits", {"h": 0}) == 0.0
    assert _extract_actual("batter_hits", {}) is None


def test_total_bases_derived_from_1b_2b_3b_hr():
    from services.player_history.mlb import _extract_actual
    # actual TB present → use directly.
    assert _extract_actual("batter_total_bases", {"tb": 4}) == 4.0
    # Derived from components.
    row = {"h": 3, "2b": 1, "3b": 0, "hr": 1}
    # singles = 3 - 1 - 0 - 1 = 1; TB = 1 + 2*1 + 3*0 + 4*1 = 7
    assert _extract_actual("batter_total_bases", row) == 7.0


def test_pitcher_outs_derived_from_ip():
    from services.player_history.mlb import _extract_actual
    # IP=6.0 → 18 outs.
    assert _extract_actual("pitcher_outs", {"ip": 6.0}) == 18
    # outs field present overrides.
    assert _extract_actual("pitcher_outs", {"outs": 19, "ip": 6}) == 19.0
    # Missing both → None.
    assert _extract_actual("pitcher_outs", {}) is None


def test_all_supported_mlb_markets_extractable():
    from services.player_history.mlb import _extract_actual
    row = {"h": 1, "hr": 1, "rbi": 3, "r": 2, "tb": 4, "k": 8, "outs": 18}
    assert _extract_actual("batter_hits", row) == 1.0
    assert _extract_actual("batter_home_runs", row) == 1.0
    assert _extract_actual("batter_rbis", row) == 3.0
    assert _extract_actual("batter_runs_scored", row) == 2.0
    assert _extract_actual("batter_total_bases", row) == 4.0
    assert _extract_actual("batter_hits_runs_rbis", row) == 6.0
    assert _extract_actual("pitcher_strikeouts", row) == 8.0
    assert _extract_actual("pitcher_outs", row) == 18.0


# ═══════════════════════════════════════════════════════════════════
# §20 — No future-data leakage + §11, §17-§18 identity/current-roster
# ═══════════════════════════════════════════════════════════════════
class _FakeCursor:
    def __init__(self, rows): self.rows = list(rows)
    def sort(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def __aiter__(self):
        self._i = 0; return self
    async def __anext__(self):
        if self._i >= len(self.rows):
            raise StopAsyncIteration
        r = self.rows[self._i]; self._i += 1
        return r


class _FakeColl:
    def __init__(self, rows, filter_field="event_time"):
        self.rows = rows
        self.filter_field = filter_field
    def find(self, query, projection=None):
        # Apply event_time / date < cutoff filter.
        cutoff = None
        for f in ("event_time", "date"):
            cond = query.get(f)
            if isinstance(cond, dict) and "$lt" in cond:
                cutoff = cond["$lt"]
                break
        pid = query.get("player_id")
        canon = query.get("canonical_player_id")
        matched = []
        for r in self.rows:
            if pid and r.get("player_id") != pid:
                continue
            if canon and r.get("canonical_player_id") != canon:
                continue
            if cutoff is not None:
                stamp = r.get("event_time") or r.get("date") or ""
                if stamp >= cutoff:
                    continue
            matched.append(r)
        return _FakeCursor(matched)
    async def create_index(self, *a, **k): return "ok"


class _FakeDB:
    def __init__(self, actuals_rows=None, legacy_rows=None):
        self.player_game_actuals = _FakeColl(actuals_rows or [])
        self.player_game_logs = _FakeColl(legacy_rows or [], filter_field="date")


def test_future_games_excluded_by_cutoff():
    from services.player_history import get_player_history
    rows = [
        {"sport": "mlb", "player_id": "p1", "event_time": "2026-08-10T20:00:00Z",
          "actuals": {"h": 2}, "season": 2026, "team": "NYY", "opponent": "BOS",
          "home_away": "home"},
        # Future game — must be excluded.
        {"sport": "mlb", "player_id": "p1", "event_time": "2026-08-12T20:00:00Z",
          "actuals": {"h": 3}, "season": 2026, "team": "NYY"},
    ]
    db = _FakeDB(actuals_rows=rows)
    async def _run():
        return await get_player_history(
            db, sport="MLB", player_id="p1",
            market="batter_hits", threshold=0.5, direction="over",
            event_time="2026-08-11T00:00:00Z")
    ev = asyncio.run(_run())
    assert ev.games_available == 1
    assert ev.last_5["result"]["decisions"] == 1


def test_current_team_never_overwritten_by_historical_team():
    """Phase 5.3 §19."""
    from services.player_history import get_player_history
    rows = [
        {"sport": "mlb", "player_id": "p1", "event_time": "2026-06-01T00:00:00Z",
          "actuals": {"h": 1}, "team": "OLD_TEAM", "opponent": "X",
          "home_away": "home"},
    ]
    db = _FakeDB(actuals_rows=rows)
    async def _run():
        return await get_player_history(
            db, sport="MLB", player_id="p1", current_team="NEW_TEAM",
            market="batter_hits", threshold=0.5,
            event_time="2026-08-11T00:00:00Z")
    ev = asyncio.run(_run())
    assert ev.current_team == "NEW_TEAM"
    assert ev.historical_team == "OLD_TEAM"


def test_identity_confidence_low_when_no_ids():
    from services.player_history import get_player_history
    db = _FakeDB(actuals_rows=[
        {"sport": "mlb", "player_id": "p1", "event_time": "2026-06-01T00:00:00Z",
          "actuals": {"h": 1}},
    ])
    async def _run():
        return await get_player_history(
            db, sport="MLB",
            market="batter_hits", threshold=0.5,
            event_time="2026-08-11T00:00:00Z")
    ev = asyncio.run(_run())
    # No player_id AND no canonical_player_id → cannot look up → UNAVAILABLE.
    assert ev.data_quality in ("UNAVAILABLE", "INSUFFICIENT")


# ═══════════════════════════════════════════════════════════════════
# §3 / §16-§17 — Provenance + Quality reported
# ═══════════════════════════════════════════════════════════════════
def test_provenance_and_quality_reported():
    from services.player_history import get_player_history
    rows = [
        {"sport": "mlb", "player_id": "p1",
          "event_time": f"2026-06-{d:02d}T00:00:00Z",
          "actuals": {"h": h}, "season": 2026, "team": "T", "opponent": "X",
          "home_away": "home"}
        for d, h in enumerate([1, 2, 0, 1, 3, 2, 1, 0, 2, 1, 1, 3, 0, 1, 2, 2], start=1)
    ]
    db = _FakeDB(actuals_rows=rows)
    async def _run():
        return await get_player_history(
            db, sport="MLB", player_id="p1",
            market="batter_hits", threshold=0.5, direction="over",
            event_time="2026-08-11T00:00:00Z")
    ev = asyncio.run(_run())
    assert ev.source in ("MLB_STATSAPI", "MLB_STATSAPI_LEGACY")
    assert ev.data_quality in ("HIGH", "MEDIUM", "LOW")
    # 16 valid games → HIGH.
    assert ev.data_quality == "HIGH"
    assert ev.games_used == 16
    assert ev.history_as_of == "2026-08-11T00:00:00Z"


def test_windows_capped_correctly():
    from services.player_history import get_player_history
    rows = [
        {"sport": "mlb", "player_id": "p1",
          "event_time": f"2026-06-{d:02d}T00:00:00Z",
          "actuals": {"h": (d % 4)}, "season": 2026, "team": "T",
          "home_away": "home"}
        for d in range(1, 26)
    ]
    db = _FakeDB(actuals_rows=rows)
    async def _run():
        return await get_player_history(
            db, sport="MLB", player_id="p1",
            market="batter_hits", threshold=0.5,
            event_time="2026-08-11T00:00:00Z")
    ev = asyncio.run(_run())
    assert ev.last_5["games_used"] <= 5
    assert ev.last_10["games_used"] <= 10
    assert ev.last_20["games_used"] <= 20


# ═══════════════════════════════════════════════════════════════════
# §22 / §12 — Threshold change requires NO upstream call
# ═══════════════════════════════════════════════════════════════════
def test_threshold_change_reuses_cached_actuals():
    """Two calls with different thresholds must produce different
    hit_rates from the SAME cached rows — no upstream refetch."""
    from services.player_history import get_player_history
    rows = [
        {"sport": "mlb", "player_id": "p1",
          "event_time": f"2026-06-{d:02d}T00:00:00Z",
          "actuals": {"tb": tb}, "season": 2026, "team": "T",
          "home_away": "home"}
        for d, tb in enumerate([0, 1, 2, 3, 4, 1, 2, 1, 0, 3], start=1)
    ]
    db = _FakeDB(actuals_rows=rows)
    async def _run():
        e1 = await get_player_history(
            db, sport="MLB", player_id="p1",
            market="batter_total_bases", threshold=0.5, direction="over",
            event_time="2026-08-11T00:00:00Z")
        e2 = await get_player_history(
            db, sport="MLB", player_id="p1",
            market="batter_total_bases", threshold=2.5, direction="over",
            event_time="2026-08-11T00:00:00Z")
        return e1, e2
    e1, e2 = asyncio.run(_run())
    r1 = e1.last_10["result"]
    r2 = e2.last_10["result"]
    # 0.5 threshold: wins = count where tb > 0.5 → 8 of 10 (0 appears twice)
    assert r1["wins"] == 8
    # 2.5 threshold: wins = count where tb > 2.5 → tb in (3,4,3) = 3
    assert r2["wins"] == 3
    # Both from the SAME underlying rows.
    assert r1["sample_size"] == r2["sample_size"] == 10


# ═══════════════════════════════════════════════════════════════════
# §25 — db.picks is NOT athlete-history source
# ═══════════════════════════════════════════════════════════════════
def test_service_never_reads_db_picks_for_athlete_stats():
    """Phase 5.3 §25: db.picks is Perklocks performance truth, NOT
    athlete-stat truth.  Only check LIVE code (strip docstrings /
    comments) so the disclaimer comment itself doesn't false-fail."""
    import re
    def _live(path):
        src = open(path).read()
        # Remove triple-quoted docstrings.
        src = re.sub(r'"""[\s\S]*?"""', '', src)
        # Remove # comments.
        src = re.sub(r'#.*', '', src)
        return src
    assert "db.picks" not in _live("/app/backend/services/player_history/mlb.py")
    assert "db.picks" not in _live("/app/backend/services/player_history/service.py")


# ═══════════════════════════════════════════════════════════════════
# §21 — Publication-time freeze fields present
# ═══════════════════════════════════════════════════════════════════
def test_history_as_of_returned():
    from services.player_history import get_player_history
    db = _FakeDB(actuals_rows=[])
    async def _run():
        return await get_player_history(
            db, sport="MLB", player_id="p1",
            market="batter_hits", threshold=0.5,
            event_time="2026-08-11T00:00:00Z")
    ev = asyncio.run(_run())
    assert ev.history_as_of == "2026-08-11T00:00:00Z"


# ═══════════════════════════════════════════════════════════════════
# §4 §23 — Storage contract + indexes
# ═══════════════════════════════════════════════════════════════════
def test_index_helper_present_and_idempotent():
    from services.player_history.mlb import ensure_player_game_actuals_indexes
    db = _FakeDB(actuals_rows=[])
    async def _run():
        return await ensure_player_game_actuals_indexes(db)
    names = asyncio.run(_run())
    # Fake collection returns "ok" for every create_index call.
    assert isinstance(names, list)
    assert len(names) == 5   # 5 canonical indexes


# ═══════════════════════════════════════════════════════════════════
# §29 — Regression protection: prior blocks unchanged
# ═══════════════════════════════════════════════════════════════════
def test_universal_settlement_unchanged():
    from services import universal_settlement_contract as usc
    graded = usc.grade_over_under(actual=None, line=1.5, side="over")
    assert graded.get("result") == usc.RESULT_UNRESOLVED


def test_canonical_barrier_still_present():
    from services.canonical_publication_barrier import STRICT_LOCK_FLOOR
    assert STRICT_LOCK_FLOOR == 85


def test_block2c_isolate_still_wired():
    src = open("/app/backend/sports_engine.py").read()
    assert "_isolate_and_merge_event_props" in src


def test_first_td_still_dormant():
    src = open("/app/backend/sports_engine.py").read()
    assert "First-TD DORMANT" in src


def test_nhl_tab_still_in_shared_navigation():
    src = open("/app/frontend/src/theme.ts").read()
    idx = src.index("export const SPORTS")
    end = src.index("]", idx)
    assert '"NHL"' in src[idx: end + 1]
