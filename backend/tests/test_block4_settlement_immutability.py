"""Block 4A focused test — settlement immutability.

Certifies that ``grading_validator._run_cross_check`` no longer
reopens canonical settled picks to PENDING when the external
verifier disagrees.
"""
from __future__ import annotations
import asyncio, os, sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _StubPicksCollection:
    def __init__(self, docs):
        self.docs = {d["id"]: d for d in docs}
        self.updates = []

    def find(self, q):
        # limit(N) chain
        class _Cur:
            def __init__(self, rows): self.rows = rows
            def limit(self, n): return _Cur(self.rows[:n])
            def __aiter__(self):
                self._it = iter(self.rows); return self
            async def __anext__(self):
                try:    return next(self._it)
                except StopIteration: raise StopAsyncIteration
        return _Cur(list(self.docs.values()))

    async def update_one(self, flt, update):
        pid = flt["id"]
        self.updates.append((pid, update))
        row = self.docs[pid]
        for k, v in (update.get("$set") or {}).items():
            row[k] = v
        for k in (update.get("$unset") or {}).keys():
            row.pop(k, None)


class _StubDB:
    def __init__(self, picks): self.picks = _StubPicksCollection(picks)


def test_disagreement_preserves_canonical_settlement():
    """Canonical WIN + verifier says LOST → row keeps status=won,
    keeps settled_at, and gains bounded disagreement metadata."""
    settled_at = "2026-06-01T00:00:00+00:00"
    row = {
        "id": "P1", "sport": "MLB", "event": "NYY @ BOS",
        "market": "NYY Moneyline", "selection": "NYY",
        "status": "won", "settled_at": settled_at,
        "settle_source": "mlb_statsapi",
    }
    db = _StubDB([row])

    async def _verifier(_p):
        return "lost"  # DISAGREEMENT

    from grading_validator import _run_cross_check
    q = {"sport": "MLB", "status": {"$in": ["won", "lost", "push"]}}
    summary = asyncio.run(_run_cross_check(db, q, _verifier, "mlb_statsapi"))

    assert summary["mismatched"] == 1
    # ── CANONICAL IMMUTABILITY ──
    assert db.picks.docs["P1"]["status"] == "won", (
        "Canonical status MUST be preserved — validator must NOT "
        "reopen settled picks to pending."
    )
    assert db.picks.docs["P1"]["settled_at"] == settled_at, (
        "``settled_at`` MUST NOT be unset by the validator."
    )
    assert db.picks.docs["P1"]["settle_source"] == "mlb_statsapi", (
        "``settle_source`` MUST NOT be unset."
    )
    # ── DISAGREEMENT DISPOSITION ──
    gd = db.picks.docs["P1"]["grade_disagreement"]
    assert gd["our_grade_was"] == "won"
    assert gd["mlb_statsapi_said"] == "lost"
    assert gd["attempts"] == 1
    assert gd["disposition"] == "correction_required"


def test_agreement_clears_prior_disagreement_and_marks_verified():
    row = {
        "id": "P2", "sport": "MLB", "event": "X @ Y",
        "market": "X ML", "selection": "X",
        "status": "won", "settled_at": "2026-06-01T00:00:00+00:00",
        "grade_disagreement": {
            "detected_at": "prior", "attempts": 2,
            "disposition": "correction_required",
        },
    }
    db = _StubDB([row])

    async def _verifier(_p): return "won"

    from grading_validator import _run_cross_check
    q = {}
    asyncio.run(_run_cross_check(db, q, _verifier, "mlb_statsapi"))
    assert "grade_disagreement" not in db.picks.docs["P2"]
    assert db.picks.docs["P2"]["status"] == "won"
    assert db.picks.docs["P2"]["grade_verify_result"] == "agreed"


def test_bounded_attempts_terminates_at_max():
    row = {
        "id": "P3", "sport": "MLB", "event": "X @ Y",
        "market": "X ML", "selection": "X",
        "status": "won", "settled_at": "2026-06-01T00:00:00+00:00",
        "grade_disagreement": {"attempts": 4},   # one before the max
    }
    db = _StubDB([row])

    async def _verifier(_p): return "lost"
    from grading_validator import _run_cross_check
    asyncio.run(_run_cross_check(db, {}, _verifier, "mlb_statsapi"))
    gd = db.picks.docs["P3"]["grade_disagreement"]
    assert gd["attempts"] == 5
    assert gd["disposition"] == "terminal_unresolved", (
        "After MAX_DISAGREE_ATTEMPTS the disposition must terminate "
        "so downstream reapers/monitors don't loop forever."
    )
    assert db.picks.docs["P3"]["status"] == "won"  # still preserved
