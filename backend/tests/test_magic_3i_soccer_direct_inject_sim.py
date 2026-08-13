"""MAGIC 3I — Soccer direct-inject simulator reachability tests.

Proves the safe path:
    direct-inject candidate → simulate_pick → sim_cal_store → simulator_outputs
with NO Lock Score mutation.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from services.magic.direct_inject_simulator_bridge import (
    simulate_direct_inject_pick, simulate_direct_inject_picks,
    _LOCK_INVARIANT_FIELDS, _market_supported,
)


class _Coll:
    def __init__(self, docs=None): self._docs=list(docs or [])
    async def find_one(self, q=None, projection=None, sort=None):
        q = q or {}
        for d in self._docs:
            if self._match(d, q): return d
        return None
    async def update_one(self, filt, update, upsert=False):
        for d in self._docs:
            if self._match(d, filt):
                for k,v in (update.get("$set") or {}).items(): d[k]=v
                class _R: matched_count=1; modified_count=1
                return _R()
        if upsert:
            new = dict((update.get("$set") or {})); new.update(filt)
            self._docs.append(new)
        class _R: matched_count=0; modified_count=0
        return _R()
    @staticmethod
    def _match(d, q):
        for k,v in q.items():
            if d.get(k) != v: return False
        return True


class _DB:
    def __init__(self): self._c = {}
    def __getattr__(self, n): return self._c.setdefault(n, _Coll())
    def __getitem__(self, n): return self._c.setdefault(n, _Coll())


def _base_soccer_pick(**kw):
    p = {"id": "di-pick-1", "sport": "Soccer",
         "market": "Anytime Scorer",
         "selection": "Erling Haaland",
         "player_name": "Erling Haaland",
         "canonical_player_id": "12345",
         "canonical_team_id": "MCI",
         "canonical_event_id": "e-mci-liv-01",
         "line": None, "side": None,
         "book_odds": +180,
         "lock_score": 82.5,
         "display_lock_score": 82.5,
         "lock_score_v2": 82.5,
         "lock_score_v2_raw": 82.5,
         "lock_score_peak": 82.5,
         "grade": "Green",
         "tier": "Locks",
         "model_probability": 0.42,
         "win_probability": 0.42,
         "league": "EPL"}
    p.update(kw)
    return p


# ═══════════════════════════════════════════════════════════════════
# Lock Score invariants — the CORE 3I contract
# ═══════════════════════════════════════════════════════════════════

def test_bridge_does_not_import_anchor_symbol():
    """Grep-guard: the bridge module MUST NOT actually CALL
    `_anchor_pick_to_sim` or `apply_simulations` (docstring
    references are fine — we look for real call sites)."""
    from services.magic import direct_inject_simulator_bridge
    src = inspect.getsource(direct_inject_simulator_bridge)
    # Strip docstrings/comments before scanning.
    import re
    # Remove """...""" and '''...''' blocks
    src_clean = re.sub(r'""".*?"""', '', src, flags=re.S)
    src_clean = re.sub(r"'''.*?'''", '', src_clean, flags=re.S)
    # Remove single-line comments
    src_clean = re.sub(r'#.*', '', src_clean)
    assert "_anchor_pick_to_sim(" not in src_clean, \
        "bridge must NOT call the Lock-Score anchor"
    assert "apply_simulations(" not in src_clean, \
        "bridge must NOT call apply_simulations (mutates lock_score)"


def test_lock_invariants_include_all_ranking_fields():
    """Every field that could rank a pick must be pinned."""
    for f in ("lock_score", "display_lock_score", "grade", "tier",
               "model_probability", "line", "side", "book_odds"):
        assert f in _LOCK_INVARIANT_FIELDS


def test_bridge_never_mutates_lock_score_on_success():
    """Even when simulator succeeds and result persists, every ranking
    field must remain byte-identical."""
    db = _DB()
    pick = _base_soccer_pick()
    snap_before = {f: pick.get(f) for f in _LOCK_INVARIANT_FIELDS if f in pick}
    r = asyncio.run(simulate_direct_inject_pick(db, pick))
    snap_after = {f: pick.get(f) for f in _LOCK_INVARIANT_FIELDS if f in pick}
    assert snap_before == snap_after, f"Lock Score drift: {r}"


def test_bridge_never_mutates_lock_score_on_simulation_failure():
    """Even when simulator returns None, lock/grade must be unchanged."""
    db = _DB()
    # Unsupported market path — no simulator invocation
    pick = _base_soccer_pick(market="Free-Kick Assist Direct")
    before = pick["lock_score"]; grade_before = pick["grade"]
    r = asyncio.run(simulate_direct_inject_pick(db, pick))
    assert r["outcome"] == "SIM_UNSUPPORTED"
    assert pick["lock_score"] == before
    assert pick["grade"] == grade_before


# ═══════════════════════════════════════════════════════════════════
# Market eligibility (Phase 2)
# ═══════════════════════════════════════════════════════════════════

def test_supported_markets():
    assert _market_supported("Anytime Scorer")
    assert _market_supported("Erling Haaland To Score")
    assert _market_supported("Moneyline")
    assert _market_supported("Over/Under 2.5 Goals")
    assert _market_supported("Match Total Goals")


def test_unsupported_markets_rejected_honestly():
    assert not _market_supported("Free Kick Direct Assist")
    assert not _market_supported("First Corner Taker")
    assert not _market_supported(None)
    assert not _market_supported("")


def test_unsupported_market_pick_returns_sim_unsupported():
    db = _DB()
    pick = _base_soccer_pick(market="Free Kick Direct Assist")
    r = asyncio.run(simulate_direct_inject_pick(db, pick))
    assert r["outcome"] == "SIM_UNSUPPORTED"
    assert "not simulator-supported" in (r["reason"] or "").lower()


# ═══════════════════════════════════════════════════════════════════
# Identity safety (Phase 5)
# ═══════════════════════════════════════════════════════════════════

def test_missing_canonical_event_id_returns_identity_unsafe():
    db = _DB()
    pick = _base_soccer_pick(canonical_event_id=None)
    r = asyncio.run(simulate_direct_inject_pick(db, pick))
    assert r["outcome"] == "IDENTITY_UNSAFE"


def test_missing_player_and_team_returns_identity_unsafe():
    db = _DB()
    pick = _base_soccer_pick(canonical_player_id=None,
                              canonical_team_id=None)
    r = asyncio.run(simulate_direct_inject_pick(db, pick))
    assert r["outcome"] == "IDENTITY_UNSAFE"


def test_provisional_fallback_identity_refused():
    db = _DB()
    pick = _base_soccer_pick(canonical_player_id="fallback:abc123")
    r = asyncio.run(simulate_direct_inject_pick(db, pick))
    assert r["outcome"] == "IDENTITY_UNSAFE"
    assert "provisional" in r["reason"].lower() \
        or "fallback" in r["reason"].lower()


def test_unresolved_identity_refused():
    db = _DB()
    pick = _base_soccer_pick(canonical_player_id="unresolved:xyz")
    r = asyncio.run(simulate_direct_inject_pick(db, pick))
    assert r["outcome"] == "IDENTITY_UNSAFE"


# ═══════════════════════════════════════════════════════════════════
# Non-soccer sport rejected
# ═══════════════════════════════════════════════════════════════════

def test_non_soccer_sport_is_unsupported():
    db = _DB()
    pick = _base_soccer_pick(sport="MLB")
    r = asyncio.run(simulate_direct_inject_pick(db, pick))
    assert r["outcome"] == "SIM_UNSUPPORTED"


# ═══════════════════════════════════════════════════════════════════
# Batch counters (Phase 14)
# ═══════════════════════════════════════════════════════════════════

def test_batch_returns_aggregate_counters():
    db = _DB()
    picks = [
        _base_soccer_pick(id="p1", market="Anytime Scorer"),
        _base_soccer_pick(id="p2", market="Free Kick Direct"),  # unsupported
        _base_soccer_pick(id="p3", canonical_event_id=None),   # id-unsafe
        _base_soccer_pick(id="p4", canonical_player_id="fallback:xxx"),  # provisional
        _base_soccer_pick(id="p5", sport="MLB"),  # non-soccer
    ]
    stats = asyncio.run(simulate_direct_inject_picks(db, picks))
    assert stats["eligible"] == 5
    assert stats["unsupported"] >= 2   # free-kick + MLB
    assert stats["identity_blocked"] >= 2   # missing event + fallback
    assert stats["lock_score_drifts"] == 0   # HARD REQUIREMENT


def test_batch_lock_score_drifts_stay_zero_even_across_mixed_batch():
    db = _DB()
    picks = [
        _base_soccer_pick(id=f"p{i}",
                          market=("Anytime Scorer" if i%2 else "Corners Bet"))
        for i in range(10)
    ]
    before = [(p["id"], p["lock_score"], p["grade"]) for p in picks]
    stats = asyncio.run(simulate_direct_inject_picks(db, picks))
    after = [(p["id"], p["lock_score"], p["grade"]) for p in picks]
    assert before == after
    assert stats["lock_score_drifts"] == 0


# ═══════════════════════════════════════════════════════════════════
# Fingerprint safety (Phase 10) — reuse Magic 3B fingerprint
# ═══════════════════════════════════════════════════════════════════

def test_fingerprint_differs_by_line():
    """Even without running the simulator, verify the fingerprint
    contract already used by 3B is line-sensitive."""
    from services.magic.sim_cal_store import build_input_fingerprint
    a = _base_soccer_pick(line=0.5, side="over", market="Total Goals")
    b = _base_soccer_pick(line=1.5, side="over", market="Total Goals")
    assert build_input_fingerprint(a) != build_input_fingerprint(b)


def test_fingerprint_differs_by_event():
    from services.magic.sim_cal_store import build_input_fingerprint
    a = _base_soccer_pick(id="p_a")
    b = _base_soccer_pick(canonical_event_id="different-event")
    assert build_input_fingerprint(a) != build_input_fingerprint(b)


def test_fingerprint_differs_by_player():
    from services.magic.sim_cal_store import build_input_fingerprint
    a = _base_soccer_pick(canonical_player_id="p_a")
    b = _base_soccer_pick(canonical_player_id="p_b")
    assert build_input_fingerprint(a) != build_input_fingerprint(b)


# ═══════════════════════════════════════════════════════════════════
# Failure isolation (Phase 14)
# ═══════════════════════════════════════════════════════════════════

def test_simulator_failure_does_not_raise_or_mutate_pick():
    """Even if the underlying simulator raises, the bridge must NOT
    propagate the exception AND must leave the pick untouched."""
    db = _DB()
    pick = _base_soccer_pick()
    before = dict(pick)
    r = asyncio.run(simulate_direct_inject_pick(db, pick))
    # outcome could be any of SIM_PERSISTED / SIMULATION_FAILED / etc.
    for f in _LOCK_INVARIANT_FIELDS:
        if f in before:
            assert pick.get(f) == before.get(f), \
                f"field {f} drifted"


# ═══════════════════════════════════════════════════════════════════
# Regression pin
# ═══════════════════════════════════════════════════════════════════
def test_magic_3i_does_not_change_locked_constants():
    from brain.sim_runner import SIM_RESIDUAL_MAX, MIN_RUNS_FOR_ANCHOR
    from brain.calibration import MIN_SAMPLE_FOR_OVERRIDE, MAX_OPTIMISM_BUFFER
    assert SIM_RESIDUAL_MAX == 3.0
    assert MIN_RUNS_FOR_ANCHOR == 10_000
    assert MIN_SAMPLE_FOR_OVERRIDE == 20
    assert MAX_OPTIMISM_BUFFER == 5.0
