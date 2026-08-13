"""P0.1 (2026-08-11) — Acceptance tests for the migrated live settlers.

These tests prove the ACTUAL settler functions (not just the pure
contract) now honour the Universal Settlement Contract:

  * `prop_settlement._grade` returns "unresolved" when actual is None
    or missing — never "lost".
  * `prop_settlement._record` hard-gates any lost-with-None-actual
    write; also hard-gates the Seymour class (lost + actual=0 without
    an authoritative_zero flag).
  * MLB H+R+RBI derived combo uses `grade_derived` — a missing
    component returns None (was: (h or 0) + (r or 0) + (b or 0)).
  * ESPN _record_settlement refuses to write 'lost' when the
    scoreboard payload is empty.
  * Alt lines all grade from ONE authoritative actual (contract
    invariant, verified end-to-end).

Follow-up historical correction is NOT covered here (blocked by
user).
"""
from __future__ import annotations

import asyncio

import pytest


# ── _grade delegates to universal contract ─────────────────────
@pytest.mark.unit
def test_prop_settlement_grade_returns_unresolved_for_none_actual():
    from prop_settlement import _grade
    assert _grade(None, 5.5, "over") == "unresolved"


@pytest.mark.unit
def test_prop_settlement_grade_seymour_wins_at_7():
    """Ian Seymour 7 K / Over 5.5 → 'won'."""
    from prop_settlement import _grade
    assert _grade(7, 5.5, "over") == "won"


@pytest.mark.unit
def test_prop_settlement_grade_push_on_equal_line():
    from prop_settlement import _grade
    assert _grade(5.5, 5.5, "over") == "push"


@pytest.mark.unit
def test_prop_settlement_grade_normal_loss():
    from prop_settlement import _grade
    assert _grade(4, 5.5, "over") == "lost"


# ── _record hard-gates the Seymour failure class ───────────────
@pytest.mark.unit
def test_record_refuses_lost_with_none_actual():
    """The gate MUST refuse to write 'lost' when the detail lacks
    a real actual — this is the exact Seymour bug."""
    from prop_settlement import _record

    class FakeCol:
        def __init__(self): self.updates = []
        async def update_one(self, q, u):
            self.updates.append((q, u))

    class FakeDB:
        picks = FakeCol()

    async def go():
        db = FakeDB()
        counts = {}
        # Trip the gate: outcome=lost + detail.value=None
        await _record(db, {"id": "test_pick", "sport": "MLB"},
                      "lost",
                      {"player": "Ian Seymour", "stat": "strikeOuts",
                       "value": None, "line": 5.5}, counts)
        assert db.picks.updates == [], (
            "SEYMOUR REGRESSION: _record wrote a 'lost' with None actual")
        assert counts.get("unresolved_gated", 0) == 1
    asyncio.run(go())


@pytest.mark.unit
def test_record_refuses_lost_with_zero_actual_without_authoritative_flag():
    """The Seymour case had value=0.0 (never verified as a real 0).
    The gate MUST refuse unless the detail explicitly asserts
    ``authoritative_zero=True`` (i.e. the box-score confirmed 0)."""
    from prop_settlement import _record

    class FakeCol:
        def __init__(self): self.updates = []
        async def update_one(self, q, u):
            self.updates.append((q, u))

    class FakeDB:
        picks = FakeCol()

    async def go():
        db = FakeDB()
        counts = {}
        await _record(db, {"id": "test_pick", "sport": "MLB"},
                      "lost",
                      {"player": "Ian Seymour", "stat": "strikeOuts",
                       "value": 0.0, "line": 5.5}, counts)
        assert db.picks.updates == []
        assert counts.get("unresolved_gated_zero", 0) == 1
    asyncio.run(go())


@pytest.mark.unit
def test_record_allows_lost_when_authoritative_zero_flag_set():
    """When the caller has PROVEN a real zero (e.g. authoritative
    box-score confirmed 0 strikeouts for a pitcher who fully played),
    the gate permits the write.

    P0.2b (2026-08-13): the write now flows through
    ``SettlementService.settle_from_pick`` (invoked inside
    ``prop_settlement._record``).  We therefore inspect the
    ``settlement_events`` collection AND the compat mirror on
    ``picks`` — the canonical write and mirror both must land.
    """
    from prop_settlement import _record
    from collections import defaultdict

    class FakeCol:
        def __init__(self): self.rows = []; self.updates = []
        async def find_one(self, q, proj=None):
            for r in self.rows:
                if all(r.get(k) == v for k, v in q.items()):
                    return dict(r)
            return None
        async def insert_one(self, doc): self.rows.append(dict(doc))
        async def update_many(self, q, u):
            n = 0
            for r in self.rows:
                if all(r.get(k) == v for k, v in q.items()):
                    r.update(u.get("$set", {})); n += 1
            return n
        async def update_one(self, q, u, upsert=False):
            self.updates.append((q, u))
            for r in self.rows:
                if all(r.get(k) == v for k, v in q.items()):
                    r.update(u.get("$set", {}))
                    return
            if upsert:
                row = dict(q); row.update(u.get("$set", {}))
                self.rows.append(row)

    class FakeDB:
        def __init__(self):
            self._colls = defaultdict(FakeCol)
        def __getitem__(self, k): return self._colls[k]
        def __getattr__(self, k): return self._colls[k]

    async def go():
        db = FakeDB()
        counts = {}
        try:
            await _record(db, {"id": "test_pick", "sport": "NBA",
                                "market": "Threes Over 1.5",
                                "side": "Over", "line": 1.5},
                          "lost",
                          {"player": "Some Player", "stat": "threes",
                           "value": 0, "line": 1.5,
                           "authoritative_zero": True}, counts)
        except Exception:
            # Downstream propagators may raise on the fake DB — what
            # matters is the settlement write.
            pass
        # Canonical row landed in settlement_events.
        assert len(db._colls["settlement_events"].rows) == 1
        assert db._colls["settlement_events"].rows[0]["result"] == "lost"
        # Compat mirror landed on picks.
        mirror = await db.picks.find_one({"id": "test_pick"})
        assert mirror is not None
        assert mirror["status"] == "lost"
    asyncio.run(go())


# ── ESPN _record_settlement refuses lost on empty ref ──────────
@pytest.mark.unit
def test_espn_record_refuses_lost_on_empty_ref():
    """Tennis / UFC settler MUST NOT write 'lost' from an empty
    scoreboard payload — that's how retirement / walkover / no-
    contest leak in as false losses."""
    from espn_settlement import _record_settlement

    class FakeCol:
        def __init__(self): self.updates = []
        async def update_one(self, q, u):
            self.updates.append((q, u))

    class FakeDB:
        picks = FakeCol()

    async def go():
        db = FakeDB()
        await _record_settlement(
            db, {"id": "t1", "sport": "Tennis"},
            outcome="lost", ref={}, source="espn_tennis")
        assert db.picks.updates == []
    asyncio.run(go())


@pytest.mark.unit
def test_espn_record_refuses_lost_when_no_winner_signal():
    """If all competitors have ``winner is None`` (Tennis retirement
    mid-match, UFC no-contest, cancellation) the settler cannot
    settle a loss."""
    from espn_settlement import _record_settlement

    class FakeCol:
        def __init__(self): self.updates = []
        async def update_one(self, q, u):
            self.updates.append((q, u))

    class FakeDB:
        picks = FakeCol()

    async def go():
        db = FakeDB()
        ref = {"competitors": [
            {"athlete": {"displayName": "Alcaraz"}, "winner": None},
            {"athlete": {"displayName": "Sinner"}, "winner": None},
        ]}
        await _record_settlement(
            db, {"id": "t2", "sport": "Tennis"},
            outcome="lost", ref=ref, source="espn_tennis")
        assert db.picks.updates == []
    asyncio.run(go())


@pytest.mark.unit
def test_espn_record_allows_lost_when_positive_winner_signal():
    """P0.2b (2026-08-13): the write flows through
    ``SettlementService.settle_from_pick`` inside
    ``_record_settlement``.  We assert the canonical row + compat
    mirror both land when a positive winner signal is present."""
    from espn_settlement import _record_settlement
    from collections import defaultdict

    class FakeCol:
        def __init__(self): self.rows = []; self.updates = []
        async def find_one(self, q, proj=None):
            for r in self.rows:
                if all(r.get(k) == v for k, v in q.items()):
                    return dict(r)
            return None
        async def insert_one(self, doc): self.rows.append(dict(doc))
        async def update_many(self, q, u):
            n = 0
            for r in self.rows:
                if all(r.get(k) == v for k, v in q.items()):
                    r.update(u.get("$set", {})); n += 1
            return n
        async def update_one(self, q, u, upsert=False):
            self.updates.append((q, u))
            for r in self.rows:
                if all(r.get(k) == v for k, v in q.items()):
                    r.update(u.get("$set", {}))
                    return
            if upsert:
                row = dict(q); row.update(u.get("$set", {}))
                self.rows.append(row)

    class FakeDB:
        def __init__(self):
            self._colls = defaultdict(FakeCol)
        def __getitem__(self, k): return self._colls[k]
        def __getattr__(self, k): return self._colls[k]

    async def go():
        db = FakeDB()
        ref = {"competitors": [
            {"athlete": {"displayName": "Alcaraz"}, "winner": True,
             "linescores": [{"value": 6}, {"value": 4}, {"value": 6}]},
            {"athlete": {"displayName": "Sinner"}, "winner": False,
             "linescores": [{"value": 3}, {"value": 6}, {"value": 3}]},
        ]}
        await _record_settlement(
            db, {"id": "t3", "sport": "Tennis", "market": "Match Winner",
                  "side": "Sinner", "line": None},
            outcome="lost", ref=ref, source="espn_tennis")
        # Canonical row landed.
        assert len(db._colls["settlement_events"].rows) == 1
        assert db._colls["settlement_events"].rows[0]["result"] == "lost"
        # Compat mirror landed.
        mirror = await db.picks.find_one({"id": "t3"})
        assert mirror is not None
        assert mirror["status"] == "lost"
    asyncio.run(go())


# ── MLB H+R+RBI derived combo ─────────────────────────────────
@pytest.mark.unit
def test_mlb_hrribi_missing_component_is_none_not_zero():
    """The pre-P0.1 path: (h or 0) + (r or 0) + (b or 0) — missing
    component silently graded as 0.  After migration: missing
    component → None → skip."""
    from services.universal_settlement_contract import grade_derived
    # All three present.
    assert grade_derived({"hits": 1, "runs": 1, "rbi": 1}) == 3
    # One missing.
    assert grade_derived({"hits": 1, "runs": 1, "rbi": None}) is None
    # All missing.
    assert grade_derived(
        {"hits": None, "runs": None, "rbi": None}) is None
    # Zero component is a real zero.
    assert grade_derived({"hits": 0, "runs": 0, "rbi": 0}) == 0


# ── One authoritative actual → many alt lines ────────────────
@pytest.mark.unit
def test_alt_lines_share_one_actual_via_migrated_grade():
    """The contract invariant end-to-end via _grade."""
    from prop_settlement import _grade
    # 267 passing yards
    assert _grade(267, 200.5, "over") == "won"
    assert _grade(267, 225.5, "over") == "won"
    assert _grade(267, 250.5, "over") == "won"
    assert _grade(267, 275.5, "over") == "lost"


# ── Post-mortem gate ───────────────────────────────────────────
@pytest.mark.unit
def test_post_mortem_only_on_verified_settlement():
    """AI loss analysis may ONLY run when settlement_verified=True."""
    from services.universal_settlement_contract import (
        settlement_envelope, RESULT_UNRESOLVED, RESULT_LOST,
    )
    # Unresolved envelopes are NOT verified.
    unresolved = settlement_envelope(
        result=RESULT_UNRESOLVED, actual=None,
        reason="missing_actual")
    assert unresolved["settlement_verified"] is False
    # A properly graded loss IS verified.
    graded_loss = settlement_envelope(
        result=RESULT_LOST, actual=4, line=5.5, side="over")
    assert graded_loss["settlement_verified"] is True
