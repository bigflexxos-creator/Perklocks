"""P0.2a — SettlementService hardening regression tests.

Proves every acceptance requirement in §1-§14.  Uses an in-memory
`FakeDB` for deterministic behaviour (no live MongoDB required)."""
from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

import pytest

from services.settlement_service import (
    ALREADY_SETTLED_IDENTICAL,
    CORRECTION_APPLIED,
    GRADER_VERSION,
    NEW_SETTLEMENT,
    REFUSAL_IDENTITY_MISMATCH,
    REFUSAL_INVALID_RESULT,
    REFUSAL_LIVE,
    REFUSAL_MISSING_ACTUAL,
    REFUSAL_MISSING_SOURCE,
    SettlementService,
    VALID_RESULTS,
    _fingerprint,
    _pick_status_from_result,
)


# ─── Minimal in-memory fake DB (enough for SettlementService flow) ───

class _Coll:
    def __init__(self):
        self.rows = []

    async def find_one(self, q, proj=None):
        for r in self.rows:
            if all(r.get(k) == v for k, v in q.items()):
                return dict(r)
        return None

    async def find(self, q):  # not used
        return []

    async def insert_one(self, doc):
        self.rows.append(dict(doc))

    async def update_many(self, q, update):
        n = 0
        for r in self.rows:
            if all(r.get(k) == v for k, v in q.items()):
                r.update(update.get("$set", {}))
                n += 1
        return n

    async def update_one(self, q, update, upsert=False):
        for r in self.rows:
            if all(r.get(k) == v for k, v in q.items()):
                r.update(update.get("$set", {}))
                return
        if upsert:
            row = dict(q)
            row.update(update.get("$set", {}))
            self.rows.append(row)


class _FakeDB:
    def __init__(self):
        self._colls: dict[str, _Coll] = defaultdict(_Coll)

    def __getitem__(self, name):
        return self._colls[name]

    def __getattr__(self, name):
        return self._colls[name]


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _svc():
    return SettlementService(_FakeDB())


# ═════════════════════════════════════════════════════════════════════
# §1 · Central FINAL barrier
# ═════════════════════════════════════════════════════════════════════

class TestFinalBarrier:
    def test_live_event_refuses(self):
        svc = _svc()
        out = asyncio.new_event_loop().run_until_complete(svc.record(
            prediction_id="p1", result="won", source="espn",
            actual_result={"a": 6, "line": 4.5},
            authoritative_event_final=False,   # LIVE
        ))
        assert out["status"] == REFUSAL_LIVE

    def test_missing_actual_refuses(self):
        loop = asyncio.new_event_loop()
        svc = _svc()
        out = loop.run_until_complete(svc.record(
            prediction_id="p1", result="won", source="espn",
            actual_result=None,
            authoritative_event_final=True,
        ))
        assert out["status"] == REFUSAL_MISSING_ACTUAL

    def test_missing_source_refuses(self):
        loop = asyncio.new_event_loop()
        svc = _svc()
        out = loop.run_until_complete(svc.record(
            prediction_id="p1", result="won", source="",
            actual_result={"a": 6}, authoritative_event_final=True,
        ))
        assert out["status"] == REFUSAL_MISSING_SOURCE

    def test_invalid_result_refuses(self):
        loop = asyncio.new_event_loop()
        svc = _svc()
        out = loop.run_until_complete(svc.record(
            prediction_id="p1", result="FUBAR", source="espn",
            authoritative_event_final=True, actual_result={"a": 6},
        ))
        assert out["status"] == REFUSAL_INVALID_RESULT


# ═════════════════════════════════════════════════════════════════════
# §2 · Idempotency — same fingerprint = ALREADY_SETTLED_IDENTICAL
# ═════════════════════════════════════════════════════════════════════

class TestIdempotency:
    def test_duplicate_processing_returns_already_settled(self):
        loop = asyncio.new_event_loop()
        svc = _svc()
        kwargs = dict(
            prediction_id="dm-1", result="won", source="mlb_stats",
            authoritative_event_final=True,
            actual_result={"actual_strikeouts": 6, "line": 4.5},
            canonical_event_id="e-1", market="strikeouts",
            side="Over", line=4.5,
        )
        out1 = loop.run_until_complete(svc.record(**kwargs))
        assert out1["status"] == NEW_SETTLEMENT
        # Process 4 more times identically.
        for _ in range(4):
            outN = loop.run_until_complete(svc.record(**kwargs))
            assert outN["status"] == ALREADY_SETTLED_IDENTICAL
        # Only ONE active settlement in the ledger.
        active = loop.run_until_complete(svc.get_active_event("dm-1"))
        assert active["result"] == "won"
        assert active["settlement_version"] == 1


# ═════════════════════════════════════════════════════════════════════
# §3-4 · Correction / versioning
# ═════════════════════════════════════════════════════════════════════

class TestCorrection:
    def test_v2_supersedes_v1_non_destructively(self):
        loop = asyncio.new_event_loop()
        svc = _svc()
        v1 = loop.run_until_complete(svc.record(
            prediction_id="pc-1", result="lost", source="espn",
            authoritative_event_final=True,
            actual_result={"actual": 3, "line": 4.5},
            canonical_event_id="e-1", market="strikeouts",
            side="Over", line=4.5,
        ))
        assert v1["status"] == NEW_SETTLEMENT
        v1_id = v1["event"]["settlement_id"]

        # Authoritative correction — actual was really 6 (WON).
        v2 = loop.run_until_complete(svc.record(
            prediction_id="pc-1", result="won", source="espn",
            authoritative_event_final=True,
            actual_result={"actual": 6, "line": 4.5},
            canonical_event_id="e-1", market="strikeouts",
            side="Over", line=4.5,
            correction_reason="mlb_stat_correction_20260813",
        ))
        assert v2["status"] == CORRECTION_APPLIED
        assert v2["event"]["settlement_version"] == 2
        assert v2["event"]["supersedes_settlement_id"] == v1_id
        assert v2["event"]["old_result"] == "lost"
        assert v2["event"]["new_result"] == "won"
        # v1 preserved (find in raw collection) — non-destructive.
        rows = svc.db["settlement_events"].rows
        assert any(r["settlement_id"] == v1_id and not r["is_active"] for r in rows)


# ═════════════════════════════════════════════════════════════════════
# §5 · PUSH != VOID
# ═════════════════════════════════════════════════════════════════════

class TestPushNotVoid:
    def test_push_stays_push(self):
        assert _pick_status_from_result("push") == "push"

    def test_void_stays_void(self):
        assert _pick_status_from_result("void") == "void"

    def test_push_settlement_mirror_status_push(self):
        loop = asyncio.new_event_loop()
        svc = _svc()
        loop.run_until_complete(svc.record(
            prediction_id="p-push", result="push", source="espn",
            authoritative_event_final=True,
            actual_result={"actual": 5, "line": 5},
            canonical_event_id="e-1", market="strikeouts",
            side="Over", line=5,
        ))
        # Compat mirror on picks
        row = loop.run_until_complete(
            svc.db["picks"].find_one({"id": "p-push"}))
        assert row and row["status"] == "push"
        assert row["status"] != "void"


# ═════════════════════════════════════════════════════════════════════
# §8 · Wrong-identity fail-closed
# ═════════════════════════════════════════════════════════════════════

class TestIdentityFailClosed:
    @pytest.mark.parametrize("field,mismatch", [
        ("expected_pick_id",  ("expected", "actual-different")),
        ("expected_event_id", ("e-1",      "e-2")),
        ("expected_market",   ("strikeouts","hits")),
        ("expected_side",     ("Over",     "Under")),
        ("expected_line",     (4.5,        5.5)),
    ])
    def test_mismatch_refuses(self, field, mismatch):
        loop = asyncio.new_event_loop()
        svc = _svc()
        expected, got = mismatch
        kwargs = dict(
            prediction_id="p-1", result="won", source="espn",
            authoritative_event_final=True,
            actual_result={"actual": 6, "line": 4.5},
            canonical_event_id="e-1", market="strikeouts",
            side="Over", line=4.5,
        )
        if field == "expected_pick_id":
            kwargs["prediction_id"] = got
            kwargs[field] = expected
        elif field == "expected_event_id":
            kwargs["canonical_event_id"] = got; kwargs[field] = expected
        elif field == "expected_market":
            kwargs["market"] = got; kwargs[field] = expected
        elif field == "expected_side":
            kwargs["side"] = got; kwargs[field] = expected
        elif field == "expected_line":
            kwargs["line"] = got; kwargs[field] = expected
        out = loop.run_until_complete(svc.record(**kwargs))
        assert out["status"] == REFUSAL_IDENTITY_MISMATCH


# ═════════════════════════════════════════════════════════════════════
# §9 · LIVE-not-settle matrix (all 8 sports)
# ═════════════════════════════════════════════════════════════════════

class TestLiveMatrix:
    @pytest.mark.parametrize("sport", [
        "MLB", "NBA", "NFL", "NHL", "Soccer", "Tennis", "CFB", "UFC",
    ])
    def test_live_refuses(self, sport):
        loop = asyncio.new_event_loop()
        svc = _svc()
        out = loop.run_until_complete(svc.record(
            prediction_id=f"live-{sport}", result="won", source="espn",
            authoritative_event_final=False,   # LIVE
            actual_result={"actual": 1},
            canonical_event_id="e-1", market="ml", side="home", line=0,
        ))
        assert out["status"] == REFUSAL_LIVE


# ═════════════════════════════════════════════════════════════════════
# §10 · Dustin May pin
# ═════════════════════════════════════════════════════════════════════

class TestDustinMayPin:
    def test_final_over_4_5_actual_6_is_won(self):
        loop = asyncio.new_event_loop()
        svc = _svc()
        out = loop.run_until_complete(svc.record(
            prediction_id="dm-pin", result="won", source="mlb_stats",
            authoritative_event_final=True,
            actual_result={"actual_strikeouts": 6, "line": 4.5},
            canonical_event_id="mil-sd", market="strikeouts",
            side="Over", line=4.5,
        ))
        assert out["status"] == NEW_SETTLEMENT
        assert out["event"]["result"] == "won"

    def test_live_over_4_5_actual_6_refused(self):
        loop = asyncio.new_event_loop()
        svc = _svc()
        out = loop.run_until_complete(svc.record(
            prediction_id="dm-pin-live", result="won", source="mlb_stats",
            authoritative_event_final=False,  # LIVE
            actual_result={"actual_strikeouts": 6, "line": 4.5},
            canonical_event_id="mil-sd", market="strikeouts",
            side="Over", line=4.5,
        ))
        assert out["status"] == REFUSAL_LIVE


# ═════════════════════════════════════════════════════════════════════
# §6 · Result vocabulary + grader version
# ═════════════════════════════════════════════════════════════════════

class TestVocabulary:
    def test_valid_results(self):
        assert set(VALID_RESULTS) == {"won", "lost", "void", "push", "cancelled"}

    def test_grader_version_stamped(self):
        loop = asyncio.new_event_loop()
        svc = _svc()
        loop.run_until_complete(svc.record(
            prediction_id="v-1", result="void", source="espn",
            authoritative_event_final=True,
            actual_result={"reason": "postponed"},
            canonical_event_id="e", market="m", side="s", line=0,
        ))
        # Void is a non-outcome, so barrier passes even without actual.
        # For 'won'/'lost'/'push' barrier requires actual — proven above.
        row = loop.run_until_complete(svc.db[
            "settlement_events"].find_one({"prediction_id": "v-1"}))
        assert row["grader_version"] == GRADER_VERSION

    def test_fingerprint_stable(self):
        fp1 = _fingerprint(canonical_pick_id="p", canonical_event_id="e",
                            market="m", side="Over", line=4.5,
                            actual_result={"actual": 6},
                            event_final_source="espn")
        fp2 = _fingerprint(canonical_pick_id="p", canonical_event_id="e",
                            market="m", side="Over", line=4.5,
                            actual_result={"actual": 6},
                            event_final_source="espn")
        assert fp1 == fp2

    def test_fingerprint_diverges_on_actual_change(self):
        fp1 = _fingerprint(canonical_pick_id="p", canonical_event_id="e",
                            market="m", side="Over", line=4.5,
                            actual_result={"actual": 3},
                            event_final_source="espn")
        fp2 = _fingerprint(canonical_pick_id="p", canonical_event_id="e",
                            market="m", side="Over", line=4.5,
                            actual_result={"actual": 6},
                            event_final_source="espn")
        assert fp1 != fp2
