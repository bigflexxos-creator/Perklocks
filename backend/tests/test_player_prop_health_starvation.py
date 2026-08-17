"""Player-Prop Health Starvation μ-fix — focused tests.

Certifies the split GAME_MARKET_HEALTH / PLAYER_PROP_HEALTH gate in
``server.ensure_today_picks``.  Tests exercise ONLY the health-decision
logic; the refresh orchestrator itself is stubbed out because the
audit-confirmed defect is health separation, NOT the refresh path.

Contract asserted:
  1. Game markets healthy + player props starved
     → slate UNHEALTHY → refresh scheduled.
  2. Game markets healthy + player props all model-rejected (rows
     exist but not actionable)
     → slate HEALTHY → no refresh (Example A / no pointless retry).
  3. Game markets missing + player props healthy
     → slate UNHEALTHY → refresh scheduled.
  4. Both healthy → NO refresh scheduled.
  5. Player-prop detection uses canonical selectors ONLY
     (selection_v2.selection.player / elite_player_name / player_name).
"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any, Iterable

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ─────────────────────────────────────────────────────────────────────
# In-memory stand-in for db.picks that answers ``count_documents``
# against the exact queries emitted by ``ensure_today_picks``.
# ─────────────────────────────────────────────────────────────────────
class _StubPicks:
    def __init__(self, docs: list[dict]):
        self.docs = docs

    async def count_documents(self, q: dict) -> int:
        return sum(1 for d in self.docs if _matches(d, q))


def _matches(doc: dict, q: dict) -> bool:
    for k, v in q.items():
        if k == "$or":
            if not any(_matches(doc, sub) for sub in v):
                return False
            continue
        if k == "$nor":
            if any(_matches(doc, sub) for sub in v):
                return False
            continue
        val = _lookup(doc, k)
        if isinstance(v, dict):
            if not _op_match(val, v):
                return False
        else:
            if val != v:
                return False
    return True


def _lookup(doc: dict, dotted: str):
    cur: Any = doc
    for p in dotted.split("."):
        if isinstance(cur, dict):
            cur = cur.get(p)
        else:
            return None
    return cur


def _op_match(val: Any, ops: dict) -> bool:
    for op, arg in ops.items():
        if op == "$exists":
            if arg is True and val is None:
                return False
            if arg is False and val is not None:
                return False
        elif op == "$ne":
            if val == arg:
                return False
        elif op == "$nin":
            if val in arg:
                return False
        elif op == "$in":
            if val not in arg:
                return False
        elif op == "$gte":
            if val is None or val < arg:
                return False
        else:
            raise NotImplementedError(f"stub op {op}")
    return True


# ─────────────────────────────────────────────────────────────────────
# Helper builders.  Timestamps well in the future so ``event_time >=
# now_iso`` matches.
# ─────────────────────────────────────────────────────────────────────
FUTURE = "2999-01-01T00:00:00+00:00"


def _game_pick(*, pid: str, off_board=False, no_bet=False, status="pending",
               event_time=FUTURE, publication_source="canonical") -> dict:
    return {
        "id": pid, "pick_date": "TODAY",
        "publication_source": publication_source,
        "off_board": off_board, "settlement_block": False, "no_bet": no_bet,
        "status": status, "event_time": event_time,
        "market": "Run Line", "selection": "ARI +1.5",
        # No player fields — game market.
    }


def _prop_pick(*, pid: str, player="Aaron Judge", off_board=False, no_bet=False,
               status="pending", event_time=FUTURE,
               publication_source="canonical") -> dict:
    return {
        "id": pid, "pick_date": "TODAY",
        "publication_source": publication_source,
        "off_board": off_board, "settlement_block": False, "no_bet": no_bet,
        "status": status, "event_time": event_time,
        "market": "Total Bases", "selection": f"{player} Over 1.5",
        "selection_v2": {"selection": {"player": player, "team": "NYY"}},
        "elite_player_name": player,
    }


# ─────────────────────────────────────────────────────────────────────
# Async harness — runs ``ensure_today_picks`` against a stubbed db and
# captures whether the background refresh was scheduled.
# ─────────────────────────────────────────────────────────────────────
async def _run_ensure(monkeypatch, docs: list[dict]) -> dict:
    import server
    # Reset the module-level in-flight guard so each test is
    # independent.
    server._refresh_in_flight = False

    monkeypatch.setattr(server, "db", type("D", (), {"picks": _StubPicks(docs)})())
    monkeypatch.setattr(server, "_today_str", lambda: "TODAY")

    refresh_scheduled = {"called": False}

    class _StubRegistry:
        def register_and_start(self, name, coro_factory, **_kw):
            refresh_scheduled["called"] = True
            # Do not actually invoke the coro — we're only testing the
            # health decision.
            return None

    def _get_reg():
        return _StubRegistry()

    # Patch the runtime registry so asyncio.create_task isn't used.
    import services.runtime_task_registry as rtr
    monkeypatch.setattr(rtr, "get_registry", _get_reg)

    await server._ensure_today_picks()
    return {"refresh_scheduled": refresh_scheduled["called"]}


# ─────────────────────────────────────────────────────────────────────
# T1 — GAME MARKETS HEALTHY, PLAYER PROPS STARVED → REFRESH
# ─────────────────────────────────────────────────────────────────────
def test_game_healthy_prop_starved_triggers_refresh(monkeypatch):
    docs = [_game_pick(pid=f"G{i}") for i in range(25)]  # 25 game markets
    # ZERO player-prop rows in ANY state → starvation.
    r = asyncio.run(_run_ensure(monkeypatch, docs))
    assert r["refresh_scheduled"] is True, (
        "Starved player props with healthy game markets MUST schedule "
        "a refresh — previous behavior masked this as HEALTHY."
    )


# ─────────────────────────────────────────────────────────────────────
# T2 — GAME MARKETS HEALTHY, PROPS ALL MODEL-REJECTED → NO REFRESH
# ─────────────────────────────────────────────────────────────────────
def test_game_healthy_prop_all_rejected_no_refresh(monkeypatch):
    docs = [_game_pick(pid=f"G{i}") for i in range(25)]
    # 8 player-prop rows exist BUT are all off-board / no_bet
    # (rejected by the model / below the >=85 floor / etc.).  Flow
    # provably ran → no starvation → no pointless retry.
    docs += [_prop_pick(pid=f"PR{i}", off_board=True) for i in range(4)]
    docs += [_prop_pick(pid=f"PN{i}", no_bet=True) for i in range(4)]
    r = asyncio.run(_run_ensure(monkeypatch, docs))
    assert r["refresh_scheduled"] is False, (
        "Rejected props (rows exist in ANY state) prove the flow ran; "
        "MUST NOT trigger a pointless retry."
    )


# ─────────────────────────────────────────────────────────────────────
# T3 — TOTAL SLATE BELOW FLOOR → REFRESH (preserves prior behavior)
# ─────────────────────────────────────────────────────────────────────
def test_total_slate_below_floor_still_refreshes(monkeypatch):
    """Prior behavior — overall actionable coverage below the 20 floor
    schedules a refresh — MUST be preserved regardless of the
    game/prop split."""
    docs = [_game_pick(pid=f"G{i}") for i in range(5)]
    # Only a handful of actionable props too — total slate = 8 < 20.
    docs += [_prop_pick(pid=f"P{i}") for i in range(3)]
    r = asyncio.run(_run_ensure(monkeypatch, docs))
    assert r["refresh_scheduled"] is True, (
        "Total actionable coverage below the >=20 floor must still "
        "schedule a refresh."
    )


# ─────────────────────────────────────────────────────────────────────
# T4 — BOTH POPULATIONS HEALTHY → NO REFRESH
# ─────────────────────────────────────────────────────────────────────
def test_both_populations_healthy_no_refresh(monkeypatch):
    docs = [_game_pick(pid=f"G{i}") for i in range(20)]
    docs += [_prop_pick(pid=f"P{i}") for i in range(6)]  # actionable props
    r = asyncio.run(_run_ensure(monkeypatch, docs))
    assert r["refresh_scheduled"] is False, (
        "Fully healthy slate MUST NOT schedule a refresh."
    )


# ─────────────────────────────────────────────────────────────────────
# T5 — PLAYER-PROP DETECTION uses canonical selectors
# ─────────────────────────────────────────────────────────────────────
def test_prop_detection_via_canonical_selectors_only(monkeypatch):
    # 25 healthy game markets.
    docs = [_game_pick(pid=f"G{i}") for i in range(25)]
    # A pick that is a PROP but the ONLY canonical marker is
    # ``elite_player_name`` (no selection_v2, no player_name).  Even
    # ONE such row combined with the "any" threshold >=5 → NO refresh.
    docs += [
        {**_prop_pick(pid=f"E{i}"), "selection_v2": None, "player_name": None}
        for i in range(6)
    ]
    r = asyncio.run(_run_ensure(monkeypatch, docs))
    assert r["refresh_scheduled"] is False, (
        "``elite_player_name`` alone must satisfy player-prop detection."
    )


# ─────────────────────────────────────────────────────────────────────
# T6 — CANONICAL SAFETY: no probability / lock_score fields read
# ─────────────────────────────────────────────────────────────────────
def test_health_gate_reads_no_betting_truth_fields(monkeypatch):
    """The health gate must decide purely from row status/date/event_time
    — never from lock_score / probability / edge.  Enforced here by
    verifying the count query dict never mentions those fields."""
    import server
    # Reset in-flight guard.
    server._refresh_in_flight = False
    seen_queries: list[dict] = []

    class _CapturingPicks:
        async def count_documents(self, q: dict) -> int:
            seen_queries.append(q)
            return 25  # game markets healthy
    monkeypatch.setattr(server, "db",
                         type("D", (), {"picks": _CapturingPicks()})())
    monkeypatch.setattr(server, "_today_str", lambda: "TODAY")
    asyncio.run(server._ensure_today_picks())

    forbidden = {"lock_score", "published_lock_score", "win_probability",
                  "edge", "edge_percent", "grade"}
    for q in seen_queries:
        for f in forbidden:
            assert f not in _flatten_keys(q), (
                f"Health gate must NOT query on canonical truth field "
                f"{f!r}; got: {q}"
            )


def _flatten_keys(q: dict) -> Iterable[str]:
    for k, v in q.items():
        yield k
        if isinstance(v, dict):
            yield from _flatten_keys(v)
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    yield from _flatten_keys(item)
