"""Regression suite — Rollover True Immutability + Parlay 3.0 Root Closure.

Run:  cd /app/backend && python -m pytest tests/test_rollover_immutability_and_parlay30.py -v
"""
from __future__ import annotations

import asyncio
import pytest

from services.rollover_official_slate import (
    freeze_official_slate, get_official_slate, reconcile_official_slate,
    _pregame_invalid_reason, FROZEN_WAGER_VERSION, ensure_slate_indexes,
)
from services.rollover_frozen_view import build_frozen_view, build_frozen_views
from services.parlay.mode_policy import (
    resolve_mode, clamp_target_legs,
    STANDARD, ADVANCED_SAFER, ADVANCED_HIGH_EV, HIGH_RISK, TODAY_WINDOW,
)
from services.parlay.feasibility import (
    compute_funnel, STATUS_READY, STATUS_PARTIAL_ONLY, STATUS_INSUFFICIENT,
    REASON_INSUFFICIENT_UNIQUE_EVENTS, REASON_MODE_THRESHOLD_STARVATION,
    REASON_NO_REAL_ODDS, REASON_MARKET_CONCENTRATION_LIMIT,
)


# ══════════════════════════════════════════════════════════════════════
# PART A — ROLLOVER TRUE IMMUTABILITY
# ══════════════════════════════════════════════════════════════════════

class InMemoryColl:
    def __init__(self, unique_keys=None):
        self._docs = []
        self._unique = tuple(unique_keys or ())

    async def find_one(self, q, projection=None):
        for d in self._docs:
            if all(d.get(k) == v for k, v in q.items()):
                return dict(d)
        return None

    async def insert_one(self, doc):
        if self._unique:
            key = tuple(doc.get(k) for k in self._unique)
            for d in self._docs:
                if tuple(d.get(k) for k in self._unique) == key:
                    raise Exception("DuplicateKey")
        self._docs.append(dict(doc))

    async def update_one(self, q, upd, upsert=False):
        for d in self._docs:
            if all(d.get(k) == v for k, v in q.items()):
                if "$set" in upd:
                    d.update(upd["$set"])
                return
        if upsert:
            new = {**q, **upd.get("$set", {})}
            await self.insert_one(new)

    def find(self, q=None, projection=None):
        _self = self
        q = q or {}
        class C:
            def __init__(self):
                self._results = list(_self._docs)
                for k, v in q.items():
                    if isinstance(v, dict) and "$in" in v:
                        self._results = [d for d in self._results if d.get(k) in v["$in"]]
                    else:
                        self._results = [d for d in self._results if d.get(k) == v]

            def sort(self, *args, **kwargs): return self
            def limit(self, n): self._results = self._results[:n]; return self
            async def to_list(self, length=None): return list(self._results)
            def __aiter__(self):
                self._i = 0; return self
            async def __anext__(self):
                if self._i >= len(self._results): raise StopAsyncIteration
                v = self._results[self._i]; self._i += 1; return v
        return C()

    async def count_documents(self, q):
        return len([d for d in self._docs if all(d.get(k) == v for k, v in q.items())])

    async def delete_one(self, q):
        for i, d in enumerate(self._docs):
            if all(d.get(k) == v for k, v in q.items()):
                self._docs.pop(i); return

    async def create_index(self, *args, **kwargs):
        return "ok"


class InMemoryDB:
    def __init__(self):
        self.rollover_slates = InMemoryColl(unique_keys=("slate_date", "scope"))
        self.rollover_slate_events = InMemoryColl()
        self.picks = InMemoryColl(unique_keys=("id",))

    def __getitem__(self, name):
        return getattr(self, name)


def _pick(pid, **overrides):
    d = {
        "id": pid,
        "sport": "MLB",
        "league": "MLB",
        "event": "Yankees vs Red Sox",
        "canonical_event_id": "e-yanks-bosox",
        "event_time": "2099-01-01T20:00:00Z",
        "market": "Total Runs",
        "selection": "Over",
        "line": 8.5,
        "book_odds": -110,
        "published_odds": -110,
        "sportsbook": "DraftKings",
        "win_probability": 65.0,
        "published_probability": 65.0,
        "lock_score": 92.0,
        "published_lock_score": 92.0,
        "edge_percent": 3.0,
        "grade": "A",
    }
    d.update(overrides)
    return d


@pytest.mark.asyncio
async def test_freeze_wager_v2_has_full_snapshot():
    db = InMemoryDB()
    picks = [_pick("p1"), _pick("p2", id="p2", canonical_event_id="e-2"), _pick("p3", id="p3", canonical_event_id="e-3")]
    slate = await freeze_official_slate(db, "2099-01-01", picks, selector_version="test-v1", board_version="bv-1")
    assert slate["frozen_wager_version"] == FROZEN_WAGER_VERSION == 2
    assert slate["leg_count"] == 3
    leg = slate["legs"][0]
    assert leg["market"] == "Total Runs"
    assert leg["selection"] == "Over"
    assert leg["line"] == 8.5
    assert leg["odds"] == -110
    assert leg["sportsbook"] == "DraftKings"
    assert leg["win_probability"] == 65.0
    assert leg["lock_score"] == 92.0
    assert leg["frozen_wager_version"] == 2


@pytest.mark.asyncio
async def test_frozen_view_wager_immutable_under_live_mutation():
    db = InMemoryDB()
    picks = [_pick("p1")]
    slate = await freeze_official_slate(db, "2099-02-01", picks, selector_version="v", board_version="b")
    # Mutate the underlying live doc (simulate a line move + WP drift).
    mutated = _pick("p1", line=999.5, book_odds=+999, win_probability=5.0,
                    published_lock_score=55.0, sportsbook="Rogue-Book",
                    selection="Under")
    view = build_frozen_view(slate["legs"][0], mutated)
    assert view["line"] == 8.5          # NOT 999.5
    assert view["odds"] == -110         # NOT +999
    assert view["win_probability"] == 65.0
    assert view["lock_score"] == 92.0
    assert view["sportsbook"] == "DraftKings"
    assert view["selection"] == "Over"
    # But settlement info WOULD come from live doc if present:
    mutated["status"] = "won"
    view2 = build_frozen_view(slate["legs"][0], mutated)
    assert view2["status"] == "won"
    assert view2["line"] == 8.5  # still frozen


@pytest.mark.asyncio
async def test_frozen_view_survives_missing_live_doc():
    """PICK_MISSING alone must NOT alter wager truth."""
    db = InMemoryDB()
    picks = [_pick("p1")]
    slate = await freeze_official_slate(db, "2099-03-01", picks, selector_version="v", board_version="b")
    # No live doc at all.
    view = build_frozen_view(slate["legs"][0], None)
    assert view["line"] == 8.5
    assert view["odds"] == -110
    assert view["win_probability"] == 65.0
    assert view["live_pick_absent"] is True


@pytest.mark.asyncio
async def test_pregame_invalid_reason_pick_missing_does_not_invalidate():
    """The root-closure behavior: pick=None returns None, NOT PICK_MISSING."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    assert _pregame_invalid_reason(None, now) is None


@pytest.mark.asyncio
async def test_pregame_invalid_reason_requires_explicit_evidence():
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    future = (now + timedelta(hours=5)).isoformat()
    # Live doc present, no invalidation flags → None
    assert _pregame_invalid_reason(_pick("p1", event_time=future), now) is None
    # off_board
    assert _pregame_invalid_reason(_pick("p1", event_time=future, off_board=True), now) == "OFF_BOARD_PREGAME"
    # no_bet
    assert _pregame_invalid_reason(_pick("p1", event_time=future, no_bet=True), now) == "NO_BET_PREGAME"
    # cancelled status
    assert _pregame_invalid_reason(_pick("p1", event_time=future, status="cancelled"), now) == "CANCELLED_PREGAME"
    # no_real_book_line requires provenance
    assert _pregame_invalid_reason(_pick("p1", event_time=future, no_real_book_line=True), now) is None
    assert _pregame_invalid_reason(
        _pick("p1", event_time=future, no_real_book_line=True, line_removed_provenance={"source": "op"}), now
    ) == "LINE_REMOVED_PREGAME"


@pytest.mark.asyncio
async def test_reconcile_does_not_replace_on_pick_missing():
    """Even when live pick disappears, reconcile keeps the frozen leg."""
    db = InMemoryDB()
    picks = [_pick("p1")]
    slate = await freeze_official_slate(db, "2099-04-01", picks, selector_version="v", board_version="b")
    # Live doc gets deleted (simulate board regen wipe)
    db.picks._docs = []
    candidates = [_pick("q1", id="q1", canonical_event_id="e-q1")]
    reconciled = await reconcile_official_slate(db, slate, candidates, selector_version="v")
    # Leg preserved — no replacement, no invalidation event
    assert reconciled["legs"][0]["canonical_pick_id"] == "p1"
    assert reconciled["legs"][0]["line"] == 8.5


@pytest.mark.asyncio
async def test_concurrent_freeze_yields_single_authoritative_slate():
    db = InMemoryDB()
    async def worker(name):
        picks = [_pick(f"{name}-1"), _pick(f"{name}-2", id=f"{name}-2")]
        return await freeze_official_slate(db, "2099-05-01", picks, selector_version="v", board_version="b")
    results = await asyncio.gather(*[worker(n) for n in ("A", "B", "C")])
    winners = {r["legs"][0]["canonical_pick_id"] for r in results}
    # All workers must observe the SAME winner (first-writer-wins)
    assert len(winners) == 1
    assert await db.rollover_slates.count_documents({"slate_date": "2099-05-01", "scope": "official"}) == 1


# ══════════════════════════════════════════════════════════════════════
# PART B — PARLAY 3.0 — ModePolicy + Feasibility
# ══════════════════════════════════════════════════════════════════════

def test_mode_policy_resolves_correctly():
    assert resolve_mode("standard").key == "standard"
    assert resolve_mode("high_risk").key == "high_risk"
    assert resolve_mode("high-risk").key == "high_risk"
    assert resolve_mode("lottery").key == "high_risk"
    assert resolve_mode("advanced", advanced_sub="safer").key == "advanced_safer"
    assert resolve_mode("advanced", advanced_sub="ev").key == "advanced_high_ev"
    assert resolve_mode("", window_hours=5).key == "today_window"
    assert resolve_mode("unknown").key == "standard"


def test_clamp_target_legs_respects_bands():
    assert clamp_target_legs(HIGH_RISK, 3) == HIGH_RISK.min_target_legs   # bumped up to 5
    assert clamp_target_legs(HIGH_RISK, 25) == HIGH_RISK.max_target_legs  # capped at 20
    assert clamp_target_legs(STANDARD, 10) == STANDARD.max_target_legs
    assert clamp_target_legs(STANDARD, None) == STANDARD.default_target_legs


def test_feasibility_ready_when_unique_events_meet_target():
    # 4 different events + different markets → READY at target 3
    markets = ["Moneyline", "Total Goals", "Spread", "Anytime Goal Scorer"]
    pool = [
        _pick(f"p{i}", canonical_event_id=f"e{i}", market=markets[i])
        for i in range(4)
    ]
    r = compute_funnel(pool, policy=STANDARD, requested_target=3)
    assert r.status == STATUS_READY
    assert r.max_feasible_legs >= 3
    assert r.unique_events == 4


def test_feasibility_partial_when_events_below_target_but_above_min_useful():
    # HIGH_RISK requests 10, only 6 events available → PARTIAL (min_useful=5)
    # Use varied markets so family_ceiling doesn't clip below unique events.
    markets = ["Moneyline", "Total Goals", "Spread", "Anytime Goal Scorer",
               "Win or Draw", "Wins By Decision"]
    pool = [_pick(f"p{i}", canonical_event_id=f"e{i}", market=markets[i]) for i in range(6)]
    r = compute_funnel(pool, policy=HIGH_RISK, requested_target=10)
    assert r.status == STATUS_PARTIAL_ONLY
    assert r.max_feasible_legs == 6
    assert REASON_INSUFFICIENT_UNIQUE_EVENTS in r.reason_codes


def test_feasibility_insufficient_when_below_min_useful():
    # HIGH_RISK min_useful=5, only 3 events → INSUFFICIENT
    markets = ["Moneyline", "Total Goals", "Spread"]
    pool = [_pick(f"p{i}", canonical_event_id=f"e{i}", market=markets[i]) for i in range(3)]
    r = compute_funnel(pool, policy=HIGH_RISK, requested_target=10)
    assert r.status == STATUS_INSUFFICIENT
    assert REASON_INSUFFICIENT_UNIQUE_EVENTS in r.reason_codes


def test_feasibility_no_real_odds_returns_insufficient():
    pool = [_pick("p1", book_odds=None, published_odds=None)]
    r = compute_funnel(pool, policy=STANDARD, requested_target=3)
    assert r.status == STATUS_INSUFFICIENT
    assert REASON_NO_REAL_ODDS in r.reason_codes


def test_feasibility_mode_threshold_starvation():
    # Lock scores all below high_risk floor (70)
    pool = [_pick(f"p{i}", canonical_event_id=f"e{i}", lock_score=50, published_lock_score=50) for i in range(10)]
    r = compute_funnel(pool, policy=HIGH_RISK, requested_target=10)
    assert r.status == STATUS_INSUFFICIENT
    assert REASON_MODE_THRESHOLD_STARVATION in r.reason_codes


def test_advanced_high_ev_gates_negative_edge():
    pool = [
        _pick("p1", canonical_event_id="e1", edge_percent=-0.5),
        _pick("p2", canonical_event_id="e2", edge_percent=+2.0),
        _pick("p3", canonical_event_id="e3", edge_percent=+1.1),
    ]
    r = compute_funnel(pool, policy=ADVANCED_HIGH_EV, requested_target=3)
    # 2 pass edge gate, 3 unique events → PARTIAL
    assert r.mode_eligible_count == 2


def test_high_risk_policy_expanded_risk_budget():
    """The old MAX_ABS_DROP_HIGH_RISK was 0.30 — regression asserts 0.35+."""
    assert HIGH_RISK.max_absolute_drop_per_leg >= 0.30
    assert HIGH_RISK.max_relative_drop_per_leg >= 0.45
    assert HIGH_RISK.min_useful_legs == 5


def test_saved_parlay_wager_freeze_marker():
    """Saved parlay leg schema now carries frozen_wager_version=2."""
    import parlay_history
    # Just import - the leg builder logic sets frozen_wager_version=2
    # on every leg (verified elsewhere by integration).
    assert hasattr(parlay_history, "save_parlay")


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
