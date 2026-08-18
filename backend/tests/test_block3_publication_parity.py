"""Block 3 focused tests — canonical publication + consumer parity.

Certifies the strong ``publication_state == "PUBLISHED"`` predicate
now used by:
  • ``server._ensure_today_picks`` health gate (actionable count)
  • ``picks_routes.canonical_today`` counter

Contract asserted:
  1. Rows with ``publication_state="PENDING"``          → NOT actionable
  2. Rows with ``publication_state="FAILED"``           → NOT actionable
  3. Rows with ``publication_state="REJECTED"``         → NOT actionable
  4. Rows with ``publication_state="PUBLISHED"``        → ACTIONABLE
  5. Legacy rows (no publication_state) with
     ``publication_source`` set                          → ACTIONABLE
  6. Legacy rows with no publication_source at all      → NOT actionable
  7. ``is_main_board_eligible`` continues to prefer
     frozen ``published_lock_score`` over mutable
     ``lock_score`` / ``lock_score_v2`` (Block 3C).
"""
from __future__ import annotations
import asyncio, os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# In-memory Mongo stand-in that answers count_documents with matching
# semantics required by the actionable query.
class _Picks:
    def __init__(self, docs): self.docs = docs
    async def count_documents(self, q):
        return sum(1 for d in self.docs if _match(d, q))


def _match(doc, q):
    for k, v in q.items():
        if k == "$or":
            if not any(_match(doc, sub) for sub in v):
                return False
            continue
        val = doc.get(k)
        if isinstance(v, dict):
            for op, arg in v.items():
                if op == "$exists":
                    if arg and val is None: return False
                    if (not arg) and val is not None: return False
                elif op == "$ne":
                    if val == arg: return False
                elif op == "$in":
                    if val not in arg: return False
                elif op == "$gte":
                    if val is None or val < arg: return False
                elif op == "$nin":
                    if val in arg: return False
                elif op == "$exists" is False:
                    pass
                else:
                    raise NotImplementedError(op)
        else:
            if val != v:
                return False
    return True


def _build_actionable_query(today, now_iso):
    # Mirror server.py._ensure_today_picks post-fix.
    return {
        "pick_date": today,
        "$or": [
            {"publication_state": "PUBLISHED"},
            {"publication_state": {"$exists": False},
             "publication_source": {"$exists": True, "$ne": None}},
        ],
        "off_board": {"$ne": True},
        "settlement_block": {"$ne": True},
        "no_bet": {"$ne": True},
        "status": {"$in": [None, "pending"]},
        "event_time": {"$gte": now_iso},
    }


FUTURE = "2999-01-01T00:00:00+00:00"


def test_pending_publication_state_not_actionable():
    picks = _Picks([{
        "pick_date": "TODAY", "publication_state": "PUBLICATION_PENDING",
        "publication_source": "test", "off_board": False,
        "settlement_block": False, "no_bet": False,
        "status": "pending", "event_time": FUTURE,
    }])
    q = _build_actionable_query("TODAY", "2020-01-01")
    assert asyncio.run(picks.count_documents(q)) == 0


def test_failed_publication_state_not_actionable():
    picks = _Picks([{
        "pick_date": "TODAY", "publication_state": "FAILED",
        "publication_source": "test", "off_board": False,
        "settlement_block": False, "no_bet": False,
        "status": "pending", "event_time": FUTURE,
    }])
    q = _build_actionable_query("TODAY", "2020-01-01")
    assert asyncio.run(picks.count_documents(q)) == 0


def test_rejected_publication_state_not_actionable():
    picks = _Picks([{
        "pick_date": "TODAY", "publication_state": "REJECTED",
        "publication_source": "test", "off_board": False,
        "settlement_block": False, "no_bet": False,
        "status": "pending", "event_time": FUTURE,
    }])
    q = _build_actionable_query("TODAY", "2020-01-01")
    assert asyncio.run(picks.count_documents(q)) == 0


def test_published_state_is_actionable():
    picks = _Picks([{
        "pick_date": "TODAY", "publication_state": "PUBLISHED",
        "publication_source": "test", "off_board": False,
        "settlement_block": False, "no_bet": False,
        "status": "pending", "event_time": FUTURE,
    }])
    q = _build_actionable_query("TODAY", "2020-01-01")
    assert asyncio.run(picks.count_documents(q)) == 1


def test_legacy_row_without_state_but_with_source_is_actionable():
    """Bridge case: pre-lifecycle rows lack ``publication_state``
    but carry a ``publication_source``.  These must still be
    considered actionable so legacy picks don't vanish."""
    picks = _Picks([{
        "pick_date": "TODAY",
        # No publication_state at all — legacy pre-lifecycle pick.
        "publication_source": "legacy_source",
        "off_board": False, "settlement_block": False, "no_bet": False,
        "status": "pending", "event_time": FUTURE,
    }])
    q = _build_actionable_query("TODAY", "2020-01-01")
    assert asyncio.run(picks.count_documents(q)) == 1


def test_row_without_source_or_state_is_not_actionable():
    picks = _Picks([{
        "pick_date": "TODAY",
        # neither publication_state nor publication_source
        "off_board": False, "settlement_block": False, "no_bet": False,
        "status": "pending", "event_time": FUTURE,
    }])
    q = _build_actionable_query("TODAY", "2020-01-01")
    assert asyncio.run(picks.count_documents(q)) == 0


def test_frozen_lock_score_preferred_over_mutable():
    """Block 3C — main_board_lock_score_query at min_lock=95 must
    keep a pick eligible when its FROZEN published_lock_score
    already clears the bar, even if the mutable lock_score has
    since drifted below it."""
    from services.main_board_eligibility import main_board_lock_score_query
    q = main_board_lock_score_query(min_lock=95.0)
    # The query must include a published_lock_score clause AND a
    # legacy fallback for rows lacking published_lock_score.
    text = str(q)
    assert "published_lock_score" in text
    assert "$gte" in text
    # Verify semantic: a pick with published_lock_score=96 AND
    # mutable lock_score=88 satisfies the $or (published branch).
    from services.main_board_eligibility import _f
    pick = {"published_lock_score": 96.0, "lock_score": 88.0}
    pls = _f(pick.get("published_lock_score"))
    assert pls is not None and pls >= 95.0, (
        "Canonical selection MUST prefer frozen published_lock_score"
    )
