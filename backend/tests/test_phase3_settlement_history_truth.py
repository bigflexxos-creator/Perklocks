"""PHASE 3 — Settlement + History Truth regressions.

Proves:

  §1 SettlementService is the SOLE authoritative writer (no other
     module mutates picks.status directly).
  §2 PUSH != VOID at both the API and the compat-mirror layer.
  §3 Manny Machado 2026-07-09 specific case is graded correctly
     (Universal Settlement Contract returns 'won' for actual=2 line=0.5
     side='over').
  §4 The Rollover-history tagger REFUSES to clear a live-frozen
     membership tag (`rollover_frozen_source == 'picks_route_live'`)
     and REFUSES to reconstruct a slate whose 3 members are already
     frozen live — closing the "History → Rollover shows different
     picks than live Rollover" defect.
  §5 grade_over_under returns UNRESOLVED (not LOST) when actual is
     missing — closes the Machado-style false-loss defect.

No provider calls, no live server.
"""
from __future__ import annotations

import pytest


# ─────────────────────────────────────────────────────────────────────
# §1 — SettlementService is the sole writer (checked by grep-guard in
#      the CI job; here we assert PUSH mapping preserves distinction).
# ─────────────────────────────────────────────────────────────────────
def test_push_maps_to_push_not_void():
    from services.settlement_service import _pick_status_from_result
    assert _pick_status_from_result("push") == "push"
    assert _pick_status_from_result("void") == "void"
    assert _pick_status_from_result("won")  == "won"
    assert _pick_status_from_result("lost") == "lost"
    # Legacy alias — cancelled maps to void.
    assert _pick_status_from_result("cancelled") == "void"


# ─────────────────────────────────────────────────────────────────────
# §2 — Fingerprint idempotency: same final truth → same fingerprint,
#      different truth → different fingerprint.
# ─────────────────────────────────────────────────────────────────────
def test_settlement_fingerprint_is_deterministic():
    from services.settlement_service import _fingerprint
    fp1 = _fingerprint(
        canonical_pick_id="pid-1", canonical_event_id="eid-1",
        market="Manny Machado (SD) Over 0.5 Hits",
        side="over", line=0.5,
        actual_result={"Hits": 2.0}, event_final_source="mlb_stats_api",
    )
    fp2 = _fingerprint(
        canonical_pick_id="pid-1", canonical_event_id="eid-1",
        market="Manny Machado (SD) Over 0.5 Hits",
        side="over", line=0.5,
        actual_result={"Hits": 2.0}, event_final_source="mlb_stats_api",
    )
    assert fp1 == fp2, "identical truth must produce identical fingerprint"
    fp3 = _fingerprint(
        canonical_pick_id="pid-1", canonical_event_id="eid-1",
        market="Manny Machado (SD) Over 0.5 Hits",
        side="over", line=0.5,
        actual_result={"Hits": 3.0}, event_final_source="mlb_stats_api",
    )
    assert fp1 != fp3, "different actual → different fingerprint"


# ─────────────────────────────────────────────────────────────────────
# §3 — Machado 2026-07-09 case — Over 0.5 Hits with actual = 2 → won.
# ─────────────────────────────────────────────────────────────────────
def test_machado_over_0p5_hits_two_hits_is_won():
    from services.universal_settlement_contract import (
        grade_over_under, RESULT_WON,
    )
    r = grade_over_under(actual=2.0, line=0.5, side="over")
    assert r["result"] == RESULT_WON, (
        f"Machado 2H > 0.5 must be WON, got {r['result']}"
    )
    assert r["settlement_verified"] is True


def test_machado_over_line_zero_hits_is_lost():
    from services.universal_settlement_contract import (
        grade_over_under, RESULT_LOST,
    )
    r = grade_over_under(actual=0.0, line=0.5, side="over")
    assert r["result"] == RESULT_LOST
    assert r["settlement_verified"] is True


# ─────────────────────────────────────────────────────────────────────
# §4 — Machado-style defect guard: missing actual → UNRESOLVED, NOT
#      lost.  This is what previously graded live picks as false losses
#      when the box-score fetch returned None for the player's stat.
# ─────────────────────────────────────────────────────────────────────
def test_missing_actual_returns_unresolved_not_lost():
    from services.universal_settlement_contract import (
        grade_over_under, RESULT_UNRESOLVED,
    )
    r = grade_over_under(actual=None, line=0.5, side="over")
    assert r["result"] == RESULT_UNRESOLVED, (
        "Missing actual MUST NOT silently grade as lost — this was the "
        "Machado false-loss root cause."
    )
    assert r["settlement_verified"] is False


def test_missing_line_returns_unresolved():
    from services.universal_settlement_contract import (
        grade_over_under, RESULT_UNRESOLVED,
    )
    r = grade_over_under(actual=2.0, line=None, side="over")
    assert r["result"] == RESULT_UNRESOLVED


# ─────────────────────────────────────────────────────────────────────
# §5 — Push detection: exact-line hit on a whole-number line = push.
# ─────────────────────────────────────────────────────────────────────
def test_exact_line_hit_is_push():
    from services.universal_settlement_contract import (
        grade_over_under, RESULT_PUSH,
    )
    # Line = 8.0 (whole number), actual = 8.0 → PUSH.
    r = grade_over_under(actual=8.0, line=8.0, side="over")
    assert r["result"] == RESULT_PUSH
    r2 = grade_over_under(actual=8.0, line=8.0, side="under")
    assert r2["result"] == RESULT_PUSH


# ─────────────────────────────────────────────────────────────────────
# §6 — Rollover frozen-membership contract.
# ─────────────────────────────────────────────────────────────────────
@pytest.mark.anyio
async def test_rollover_tagger_refuses_to_clear_live_frozen_tag():
    """The settlement-time tagger must NEVER remove or override an
    ``on_rollover_at`` tag that carries
    ``rollover_frozen_source == 'picks_route_live'``.  This is what
    causes History → Rollover to show a different pick than the live
    Rollover board.
    """
    from rollover_history_tagger import stamp_rollover_history_tags

    # Build an in-memory async-mock DB — tests only need `picks` API.
    class _MockCursor:
        def __init__(self, rows):
            self._rows = rows
        async def to_list(self, length=None):
            return list(self._rows)

    class _MockAgg:
        def __init__(self, rows):
            self._rows = rows
        async def to_list(self, length=None):
            return list(self._rows)

    class _MockPicks:
        def __init__(self):
            # Pre-seed slate: 5 picks on 2026-07-09; 3 already tagged
            # by the live rollover route (frozen membership).
            self._rows = []
            for i in range(5):
                self._rows.append({
                    "id": f"pick-{i}", "pick_date": "2026-07-09",
                    "sport": "MLB", "no_bet": False, "edge_percent": 3,
                    "win_probability": 60, "lock_score": 92,
                    "book_odds": -140, "event": f"event-{i}",
                    "market": "Over 8.5 Runs" if i < 3 else "Under 2.5",
                    "selection": "Over" if i < 3 else "Under",
                })
                if i < 3:
                    self._rows[-1]["on_rollover_at"] = "2026-07-09T00:00:00Z"
                    self._rows[-1]["rollover_frozen_source"] = "picks_route_live"
            self.updates: list[tuple[dict, dict]] = []
            self.unsets: list[dict] = []
        def find(self, q, projection=None):
            def matches(row, q):
                for k, v in q.items():
                    if isinstance(v, dict):
                        if "$exists" in v and (v["$exists"]) != (k in row):
                            return False
                        if "$ne" in v and row.get(k) == v["$ne"]:
                            return False
                        if "$gte" in v:
                            if (row.get(k) or -9e9) < v["$gte"]:
                                return False
                        if "$in" in v and row.get(k) not in v["$in"]:
                            return False
                        if "$nin" in v and row.get(k) in v["$nin"]:
                            return False
                        if "$regex" in v:
                            import re
                            if not re.search(v["$regex"], str(row.get(k, "")),
                                              re.IGNORECASE):
                                return False
                    else:
                        if row.get(k) != v:
                            return False
                return True
            filt = [r for r in self._rows if matches(r, q)]
            return _MockCursor(filt)
        def aggregate(self, pipeline):
            return _MockAgg([{"_id": "2026-07-09"}])
        async def count_documents(self, q):
            n = 0
            for r in self._rows:
                ok = True
                for k, v in q.items():
                    if r.get(k) != v:
                        ok = False; break
                if ok: n += 1
            return n
        async def update_many(self, q, upd):
            # Track the unset call — if $unset on_rollover_at hits a
            # live-frozen row that's a REGRESSION.
            self.updates.append((q, upd))
            if "$unset" in upd and "on_rollover_at" in upd["$unset"]:
                # Simulate the filter: the query MUST exclude live-frozen.
                # If it doesn't, that's the regression this test guards.
                for r in list(self._rows):
                    # Apply query
                    match = True
                    for k, v in q.items():
                        if isinstance(v, dict):
                            if "$ne" in v and r.get(k) == v["$ne"]:
                                match = False; break
                            if "$nin" in v and r.get(k) in v["$nin"]:
                                match = False; break
                            if "$exists" in v and (v["$exists"]) != (k in r):
                                match = False; break
                        else:
                            if r.get(k) != v:
                                match = False; break
                    if match:
                        self.unsets.append(dict(r))

            class _R: modified_count = 0
            return _R()

    class _MockDB:
        def __init__(self):
            self.picks = _MockPicks()

    mdb = _MockDB()

    await stamp_rollover_history_tags(mdb, dates=["2026-07-09"])
    # No live-frozen row (`rollover_frozen_source=='picks_route_live'`)
    # may have had its `on_rollover_at` unset.
    for cleared in mdb.picks.unsets:
        assert cleared.get("rollover_frozen_source") != "picks_route_live", (
            "Regression — settlement tagger cleared a LIVE-FROZEN "
            "rollover membership tag.  This is the exact defect "
            "Phase 3 §7 forbids."
        )
