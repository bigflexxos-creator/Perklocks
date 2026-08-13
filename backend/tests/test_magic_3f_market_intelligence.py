"""MAGIC 3F — Market Intelligence + Immutable CLV tests.

Covers all failure cases required by the 3F directive:
  * odds conversion, de-vig, consensus
  * exact-threshold safety
  * timestamp / no-future-leakage
  * stage isolation (OPENING/CURRENT/CLOSING/UNKNOWN)
  * closing immutability
  * CLV never pregame
  * model-market disagreement preserved
  * DB-first behavior
"""
from __future__ import annotations

import asyncio
import pytest
from datetime import datetime, timezone, timedelta

from services.magic.market_math import (
    american_to_decimal, decimal_to_american, implied_probability,
    two_way_devig, multi_way_devig, consensus_devig,
    line_delta, price_delta,
)
from services.magic.market_snapshot_store import (
    SnapshotStage, MarketEvidenceState,
    upsert_market_snapshot, latest_current_snapshot,
    closing_snapshot, compute_pregame_market_evidence,
    finalize_pick_clv, clv_for_postgame_only, ClvAvailabilityError,
)


# ── In-memory Mongo fake ─────────────────────────────────────────
class _Coll:
    def __init__(self, docs=None):
        self._docs = list(docs or [])
    async def find_one(self, q=None, projection=None, sort=None):
        q = q or {}
        m = [d for d in self._docs if self._match(d, q)]
        if sort:
            k, dr = sort[0]
            m.sort(key=lambda d: d.get(k) or "", reverse=(dr == -1))
        return m[0] if m else None
    def find(self, q=None, projection=None):
        q = q or {}
        m = [d for d in self._docs if self._match(d, q)]
        class _C:
            def __init__(self, a): self.a=a; self.i=0
            def sort(self, s):
                k,dr = s[0]
                self.a = sorted(self.a, key=lambda d: d.get(k) or "",
                                 reverse=(dr==-1))
                return self
            def limit(self,n): self.a=self.a[:n]; return self
            def __aiter__(self): return self
            async def __anext__(self):
                if self.i>=len(self.a): raise StopAsyncIteration
                d=self.a[self.i]; self.i+=1; return d
        return _C(m)
    async def update_one(self, filt, update, upsert=False):
        for d in self._docs:
            if self._match(d, filt):
                for k,v in (update.get("$set") or {}).items():
                    d[k] = v
                class _R: matched_count=1; modified_count=1
                return _R()
        if upsert:
            new = dict((update.get("$set") or {}))
            new.update({k:v for k,v in filt.items() if not isinstance(v,dict)})
            self._docs.append(new)
        class _R: matched_count=0; modified_count=0
        return _R()
    @staticmethod
    def _match(d, q):
        for k,v in q.items():
            dv = d.get(k)
            if isinstance(v, dict):
                if "$in" in v and dv not in v["$in"]: return False
                if "$lte" in v and not (dv is not None and dv <= v["$lte"]): return False
                if "$gte" in v and not (dv is not None and dv >= v["$gte"]): return False
                if "$lt"  in v and not (dv is not None and dv <  v["$lt"]):  return False
            else:
                if dv != v: return False
        return True


class _DB:
    def __init__(self, colls=None): self._c = colls or {}
    def __getattr__(self, n): return self._c.setdefault(n, _Coll())
    def __getitem__(self, n): return self._c.setdefault(n, _Coll())


# ═══════════════════════════════════════════════════════════════════
# Odds conversion
# ═══════════════════════════════════════════════════════════════════
def test_american_to_decimal_negative():
    assert abs(american_to_decimal(-110) - 1.9090909) < 1e-4

def test_american_to_decimal_positive():
    assert american_to_decimal(200) == 3.0

def test_american_to_decimal_none_and_zero():
    assert american_to_decimal(None) is None
    assert american_to_decimal(0) is None
    assert american_to_decimal(50) is None   # invalid

def test_decimal_to_american_roundtrip():
    for a in (-200, -110, +100, +150, +250, -400):
        d = american_to_decimal(a)
        back = decimal_to_american(d)
        assert back == a, f"{a} → {d} → {back}"

def test_implied_probability_examples():
    assert abs(implied_probability(-110) - 0.5238) < 1e-3
    assert abs(implied_probability(+200) - 0.3333) < 1e-3
    assert implied_probability(None) is None
    assert implied_probability(50) is None


# ═══════════════════════════════════════════════════════════════════
# De-vig
# ═══════════════════════════════════════════════════════════════════
def test_two_way_devig_symmetric():
    r = two_way_devig(-110, -110)
    assert r is not None
    assert abs(r[0] - 0.5) < 1e-6 and abs(r[1] - 0.5) < 1e-6

def test_two_way_devig_asymmetric():
    r = two_way_devig(-200, +170)
    assert r is not None
    assert 0.6 < r[0] < 0.7
    assert abs(sum(r) - 1.0) < 1e-6

def test_two_way_devig_missing_side_returns_none():
    assert two_way_devig(-110, None) is None

def test_multi_way_devig_3way_sum_to_one():
    r = multi_way_devig([+140, +230, +190])
    assert r is not None
    assert abs(sum(r) - 1.0) < 1e-6

def test_consensus_devig_medians():
    snaps = [
        {"american_side": -110, "opposing_american": -110, "book": "A"},
        {"american_side": -115, "opposing_american": -105, "book": "B"},
        {"american_side": -108, "opposing_american": -112, "book": "C"},
    ]
    c = consensus_devig(snaps)
    assert c["book_count"] == 3
    assert c["devig_book_count"] == 3
    assert 0.5 - 0.03 < c["median_side_prob_devig"] < 0.5 + 0.03
    assert c["best_side_price_american"] == -108


# ═══════════════════════════════════════════════════════════════════
# Movement
# ═══════════════════════════════════════════════════════════════════
def test_line_delta_and_price_delta():
    assert line_delta(5.5, 6.5) == 1.0
    assert line_delta(None, 6.5) is None
    pd = price_delta(-110, -145)   # steamed toward this side
    assert pd is not None and pd > 0


# ═══════════════════════════════════════════════════════════════════
# Snapshot storage + immutability
# ═══════════════════════════════════════════════════════════════════
def test_upsert_snapshot_and_stage_isolation():
    async def _run():
        db = _DB()
        await upsert_market_snapshot(db,
            stage=SnapshotStage.OPENING, canonical_event_id="e1",
            sport="MLB", market="Over/Under", side="Over", line=8.5,
            american_odds=-110, book="dk", captured_at="2026-06-15T13:00:00Z",
            source="odds_api")
        await upsert_market_snapshot(db,
            stage=SnapshotStage.CURRENT, canonical_event_id="e1",
            sport="MLB", market="Over/Under", side="Over", line=8.5,
            american_odds=-125, book="dk", captured_at="2026-06-15T17:00:00Z",
            source="odds_api")
        r = await latest_current_snapshot(db,
            canonical_event_id="e1", market="Over/Under",
            side="Over", line=8.5, as_of_iso="2026-06-15T18:00:00Z")
        assert r is not None
        assert r["stage"] == SnapshotStage.CURRENT
        assert r["american_odds"] == -125
    asyncio.run(_run())


def test_no_future_leakage_in_pregame_evidence():
    async def _run():
        db = _DB()
        # Snapshot AFTER our pregame cutoff — must be excluded.
        await upsert_market_snapshot(db,
            stage=SnapshotStage.CURRENT, canonical_event_id="e2",
            sport="MLB", market="ML", side="Home", line=None,
            american_odds=-140, book="dk", captured_at="2026-06-15T22:00:00Z")
        # Two snapshots BEFORE cutoff.
        await upsert_market_snapshot(db,
            stage=SnapshotStage.OPENING, canonical_event_id="e2",
            sport="MLB", market="ML", side="Home", line=None,
            american_odds=-120, book="dk", captured_at="2026-06-15T13:00:00Z")
        await upsert_market_snapshot(db,
            stage=SnapshotStage.CURRENT, canonical_event_id="e2",
            sport="MLB", market="ML", side="Home", line=None,
            american_odds=-130, book="fd", captured_at="2026-06-15T18:00:00Z")
        ev = await compute_pregame_market_evidence(
            db, canonical_event_id="e2", market="ML", side="Home",
            line=None, as_of_iso="2026-06-15T20:00:00Z",
            model_probability=0.60)
        assert ev["book_count"] == 2   # future snapshot excluded
        assert ev["opening_odds"] == -120
        assert ev["current_odds"] == -130
    asyncio.run(_run())


def test_closing_snapshot_is_immutable_once_written():
    async def _run():
        db = _DB()
        first = await upsert_market_snapshot(db,
            stage=SnapshotStage.CLOSING, canonical_event_id="e3",
            sport="MLB", market="ML", side="Home", line=None,
            american_odds=-125, book="dk", captured_at="2026-06-15T21:59:00Z")
        assert first["is_immutable"] is True
        # Attempt to overwrite (should refuse — return original).
        await upsert_market_snapshot(db,
            stage=SnapshotStage.CLOSING, canonical_event_id="e3",
            sport="MLB", market="ML", side="Home", line=None,
            american_odds=-500, book="dk", captured_at="2026-06-15T21:59:00Z")
        r = await closing_snapshot(db, canonical_event_id="e3",
            market="ML", side="Home", line=None)
        assert r["american_odds"] == -125
    asyncio.run(_run())


def test_exact_threshold_isolation():
    """Over 0.5 must NOT match Over 1.5."""
    async def _run():
        db = _DB()
        await upsert_market_snapshot(db,
            stage=SnapshotStage.CURRENT, canonical_event_id="e4",
            sport="MLB", market="Total Hits", side="Over", line=0.5,
            american_odds=-300, book="dk", captured_at="2026-06-15T18:00:00Z")
        # Ask for Over 1.5 — must be UNAVAILABLE
        ev = await compute_pregame_market_evidence(
            db, canonical_event_id="e4", market="Total Hits",
            side="Over", line=1.5, as_of_iso="2026-06-15T19:00:00Z",
            model_probability=0.55)
        assert ev["availability"] == "UNAVAILABLE"
    asyncio.run(_run())


def test_wrong_event_rejected():
    async def _run():
        db = _DB()
        await upsert_market_snapshot(db,
            stage=SnapshotStage.CURRENT, canonical_event_id="e-real",
            sport="MLB", market="ML", side="Home", line=None,
            american_odds=-120, book="dk", captured_at="2026-06-15T18:00:00Z")
        ev = await compute_pregame_market_evidence(
            db, canonical_event_id="e-wrong", market="ML", side="Home",
            line=None, as_of_iso="2026-06-15T19:00:00Z",
            model_probability=0.55)
        assert ev["availability"] == "UNAVAILABLE"
    asyncio.run(_run())


# ═══════════════════════════════════════════════════════════════════
# Model vs market states
# ═══════════════════════════════════════════════════════════════════
def test_model_market_strong_agreement():
    async def _run():
        db = _DB()
        # -110 raw prob ≈ 0.524; model 0.53 → STRONG_AGREEMENT
        await upsert_market_snapshot(db,
            stage=SnapshotStage.CURRENT, canonical_event_id="e5",
            sport="MLB", market="ML", side="Home", line=None,
            american_odds=-110, book="dk", captured_at="2026-06-15T18:00:00Z")
        ev = await compute_pregame_market_evidence(
            db, canonical_event_id="e5", market="ML", side="Home",
            line=None, as_of_iso="2026-06-15T19:00:00Z",
            model_probability=0.53)
        assert ev["model_market_state"] == MarketEvidenceState.STRONG_AGREEMENT
    asyncio.run(_run())


def test_model_higher_than_market_disagreement_preserved():
    async def _run():
        db = _DB()
        await upsert_market_snapshot(db,
            stage=SnapshotStage.CURRENT, canonical_event_id="e6",
            sport="MLB", market="ML", side="Home", line=None,
            american_odds=+150, book="dk", captured_at="2026-06-15T18:00:00Z")
        ev = await compute_pregame_market_evidence(
            db, canonical_event_id="e6", market="ML", side="Home",
            line=None, as_of_iso="2026-06-15T19:00:00Z",
            model_probability=0.65)
        assert ev["model_market_state"] == MarketEvidenceState.MODEL_HIGHER_THAN_MARKET
        # numeric probabilities preserved
        assert ev["median_side_prob_raw"] < 0.42
    asyncio.run(_run())


def test_insufficient_evidence_when_no_model():
    async def _run():
        db = _DB()
        await upsert_market_snapshot(db,
            stage=SnapshotStage.CURRENT, canonical_event_id="e7",
            sport="MLB", market="ML", side="Home", line=None,
            american_odds=-110, book="dk", captured_at="2026-06-15T18:00:00Z")
        ev = await compute_pregame_market_evidence(
            db, canonical_event_id="e7", market="ML", side="Home",
            line=None, as_of_iso="2026-06-15T19:00:00Z",
            model_probability=None)
        assert ev["model_market_state"] == MarketEvidenceState.INSUFFICIENT_EVIDENCE
    asyncio.run(_run())


# ═══════════════════════════════════════════════════════════════════
# CLV
# ═══════════════════════════════════════════════════════════════════
def test_clv_unavailable_before_event_start():
    async def _run():
        db = _DB()
        r = await finalize_pick_clv(db, pick_id="p1",
            pick_line=8.5, pick_odds=-110, pick_timestamp="2026-06-15T13:00:00Z",
            canonical_event_id="e10", market="Over/Under", side="Over",
            event_start_iso="2026-06-15T22:00:00Z",
            now_iso="2026-06-15T20:00:00Z")   # BEFORE event
        assert r is None
    asyncio.run(_run())


def test_clv_unavailable_when_no_closing_snapshot():
    async def _run():
        db = _DB()
        r = await finalize_pick_clv(db, pick_id="p1",
            pick_line=8.5, pick_odds=-110, pick_timestamp="2026-06-15T13:00:00Z",
            canonical_event_id="e11", market="Over/Under", side="Over",
            event_start_iso="2026-06-15T22:00:00Z",
            now_iso="2026-06-15T23:00:00Z")
        assert r is not None
        assert r["clv_available"] is False
    asyncio.run(_run())


def test_clv_calculates_price_and_line_clv():
    async def _run():
        db = _DB()
        # Real CLOSING snapshot: Over 8.5 -150
        await upsert_market_snapshot(db,
            stage=SnapshotStage.CLOSING, canonical_event_id="e12",
            sport="MLB", market="Over/Under", side="Over", line=8.5,
            american_odds=-150, book="dk", captured_at="2026-06-15T21:59:00Z")
        r = await finalize_pick_clv(db, pick_id="p12",
            pick_line=8.5, pick_odds=-110,
            pick_timestamp="2026-06-15T13:00:00Z",
            canonical_event_id="e12", market="Over/Under", side="Over",
            event_start_iso="2026-06-15T22:00:00Z",
            now_iso="2026-06-15T23:00:00Z")
        assert r is not None
        assert r["closing_odds"] == -150
        assert r["closing_line"] == 8.5
        assert r["price_clv"] > 0   # closed at worse price → positive CLV
        assert r["line_clv"] == 0.0
        assert r["is_immutable"] is True
        assert r["clv_version"] == "3f.v1"
    asyncio.run(_run())


def test_clv_immutable_once_written():
    async def _run():
        db = _DB()
        await upsert_market_snapshot(db,
            stage=SnapshotStage.CLOSING, canonical_event_id="e13",
            sport="MLB", market="ML", side="Home", line=None,
            american_odds=-140, book="dk", captured_at="2026-06-15T21:59:00Z")
        r1 = await finalize_pick_clv(db, pick_id="p13",
            pick_line=None, pick_odds=-110,
            pick_timestamp="2026-06-15T13:00:00Z",
            canonical_event_id="e13", market="ML", side="Home",
            event_start_iso="2026-06-15T22:00:00Z",
            now_iso="2026-06-15T23:00:00Z")
        assert r1["is_immutable"] is True
        # Second attempt with a "different" closing must NOT overwrite.
        await upsert_market_snapshot(db,
            stage=SnapshotStage.CLOSING, canonical_event_id="e13",
            sport="MLB", market="ML", side="Home", line=None,
            american_odds=+9999, book="fd", captured_at="2026-06-15T21:59:30Z")
        r2 = await finalize_pick_clv(db, pick_id="p13",
            pick_line=None, pick_odds=-110,
            pick_timestamp="2026-06-15T13:00:00Z",
            canonical_event_id="e13", market="ML", side="Home",
            event_start_iso="2026-06-15T22:00:00Z",
            now_iso="2026-06-15T23:30:00Z")
        assert r2["closing_odds"] == r1["closing_odds"]  # original preserved
    asyncio.run(_run())


def test_clv_never_available_pregame():
    pick = {"line_clv": 0.5, "price_clv": 0.02}
    with pytest.raises(ClvAvailabilityError):
        clv_for_postgame_only(pick, allow_pregame=False)
    # Postgame allowed
    r = clv_for_postgame_only(pick, allow_pregame=True)
    assert r["line_clv"] == 0.5


# ═══════════════════════════════════════════════════════════════════
# Regression pins
# ═══════════════════════════════════════════════════════════════════
def test_magic_3f_does_not_change_locked_constants():
    from brain.sim_runner import SIM_RESIDUAL_MAX, MIN_RUNS_FOR_ANCHOR
    from brain.calibration import (
        MIN_SAMPLE_FOR_OVERRIDE, MAX_OPTIMISM_BUFFER,
    )
    assert SIM_RESIDUAL_MAX == 3.0
    assert MIN_RUNS_FOR_ANCHOR == 10_000
    assert MIN_SAMPLE_FOR_OVERRIDE == 20
    assert MAX_OPTIMISM_BUFFER == 5.0
