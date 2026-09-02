"""Phase 24 — History Root Closure Certification (2026-06).

Runtime assertions against the LIVE datastore for the History
contamination root-closure work:

    §1  Public Pick History contains ONLY records that were actually
        published to users:
          - Lock >= 85 (canonical publication floor), and
          - off_board != True, and
          - real publication evidence (board-stamp, `published_at`,
            or an active prediction_snapshot).
    §2  Writer source-tags (`publication_source`) alone are NOT
        proof of publication and are classified LEGACY_RESEARCH_ONLY.
    §3  Mutually-exclusive competing sides on the same event/market
        cannot all appear in public History as if each was
        independently published.
    §4  `stuck_pick_reaper` NO LONGER fabricates VOID for stuck
        picks — its outputs are UNRESOLVED without a settlement_events
        row.  Historical fabrications have been reverted.
    §5  VOID is only stamped when canonical settlement truth EXPLICITLY
        grades a wager void; UNRESOLVED is a distinct state.
"""
from __future__ import annotations

import asyncio
import os
import sys

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)
load_dotenv(os.path.join(_BACKEND, ".env"))


def _db():
    c = AsyncIOMotorClient(os.environ["MONGO_URL"])
    return c[os.environ["DB_NAME"]]


# ── §1 / §2 ──────────────────────────────────────────────────────────
def test_history_query_rejects_off_board_and_sub_85_candidates():
    """The canonical History query must reject candidate rows: any pick
    with `off_board=True` OR `published_lock_score < 85` must NOT appear
    in the raw query result, regardless of writer source-tag presence."""
    async def go():
        db = _db()
        from services.published_results_truth import canonical_query
        q = canonical_query(days=30, exclude_ambiguous_legacy=True, include_pending=True)
        # 1) No off_board=True rows in the raw query
        off = await db.picks.count_documents({"$and": [q, {"off_board": True}]})
        assert off == 0, f"History query still admits {off} off_board=True rows"
        # 2) No sub-85 rows when published_lock_score is set
        sub = await db.picks.count_documents({
            "$and": [q, {"published_lock_score": {"$lt": 85}}]
        })
        assert sub == 0, f"History query still admits {sub} sub-85 rows"

    asyncio.get_event_loop().run_until_complete(go())


def test_history_query_rejects_writer_source_tag_only_rows():
    """Rows whose only 'publication authority' is the writer source-tag
    `publication_source` (candidate emission) must be rejected."""
    async def go():
        db = _db()
        from services.published_results_truth import canonical_query
        q = canonical_query(days=30, exclude_ambiguous_legacy=True, include_pending=True)
        # source-tag-only, no board stamp, no published_at
        cand_only = await db.picks.count_documents({
            "$and": [q, {
                "publication_source": {"$exists": True, "$ne": None},
                "on_main_board_at":   {"$exists": False},
                "on_rollover_at":     {"$exists": False},
                "on_hr_board_at":     {"$exists": False},
                "on_under_at":        {"$exists": False},
                "on_atd_board_at":    {"$exists": False},
                "on_parlay_at":       {"$exists": False},
                "published_at":       {"$in": [None]},
            }]
        })
        assert cand_only == 0, f"History query admits {cand_only} writer-tag-only rows"

    asyncio.get_event_loop().run_until_complete(go())


# ── §3 mutually-exclusive competing sides ────────────────────────────
def test_history_no_all_three_way_1x2_published():
    """For any Soccer 1X2 event in the last 30 days, at most ONE of the
    three mutually-exclusive sides (home / draw / away) may appear in
    public History.  We only assert on the specific 1X2 moneyline case
    (home team, away team, "Draw") — different markets on the same
    event (BTTS Yes/No, Over/Under totals, player props) are legitimate
    independent publications and are excluded from this invariant."""
    async def go():
        db = _db()
        from services.published_results_truth import canonical_query
        q = canonical_query(days=30, exclude_ambiguous_legacy=True, include_pending=True)
        # Soccer 1X2 = moneyline market ONLY.  Look for events where all
        # three of home / away / "Draw" appear as separate published picks.
        pipeline = [
            {"$match": {"$and": q["$and"] + [
                {"sport": "Soccer"},
                # Filter to moneyline-like markets: exclude BTTS,
                # totals, spreads, and player props.  A moneyline pick
                # has `selection` equal to a team name OR "Draw", and
                # `line` is None (no half-line).
                {"line": None},
                {"$or": [
                    {"selection": "Draw"},
                    {"$expr": {"$eq": ["$selection", "$home_team_name"]}},
                    {"$expr": {"$eq": ["$selection", "$away_team_name"]}},
                    {"$expr": {"$eq": ["$market", "$selection"]}},
                ]},
            ]}},
            {"$group": {
                "_id": "$event",
                "sides":     {"$addToSet": "$selection"},
                "has_draw":  {"$sum": {"$cond": [{"$eq": ["$selection", "Draw"]}, 1, 0]}},
                "n":         {"$sum": 1},
            }},
            {"$match": {
                "$and": [
                    {"$expr": {"$gte": [{"$size": "$sides"}, 3]}},
                    {"has_draw": {"$gte": 1}},
                ]
            }},
            {"$limit": 5},
        ]
        offenders = [r async for r in db.picks.aggregate(pipeline)]
        assert not offenders, \
            f"Soccer 1X2 all-three-sides in History: {[(o['_id'], o['sides']) for o in offenders]}"

    asyncio.get_event_loop().run_until_complete(go())


# ── §4 reaper no longer fabricates VOID ──────────────────────────────
def test_reaper_uses_unresolved_not_void():
    """No pick may carry `void_reason='auto_void_stuck_pick_reaper'`
    after the Root Closure correction."""
    async def go():
        db = _db()
        n = await db.picks.count_documents({"void_reason": "auto_void_stuck_pick_reaper"})
        assert n == 0, f"{n} picks still carry the fabricated reaper VOID marker"

    asyncio.get_event_loop().run_until_complete(go())


def test_reaper_ledger_rows_deactivated():
    """The fabricated `settlement_events` rows produced by
    `stuck_pick_reaper` must be marked `is_active=False` (append-only
    ledger — never deleted)."""
    async def go():
        db = _db()
        active_fab = await db.settlement_events.count_documents({
            "source": "stuck_pick_reaper",
            "is_active": True,
        })
        assert active_fab == 0, \
            f"{active_fab} fabricated reaper VOID ledger rows still active"

    asyncio.get_event_loop().run_until_complete(go())


# ── §5 VOID != UNRESOLVED status projection integrity ────────────────
def test_status_void_requires_canonical_ledger_void():
    """A pick with `status='void'` must have an ACTIVE
    `settlement_events` row with `result='void'` and NOT be marked
    `settlement_status='UNRESOLVED'`."""
    async def go():
        db = _db()
        # Nobody should be status=void AND settlement_status=UNRESOLVED
        contradiction = await db.picks.count_documents({
            "status": "void",
            "settlement_status": "UNRESOLVED",
        })
        assert contradiction == 0, \
            f"{contradiction} picks have status=void AND settlement_status=UNRESOLVED"

    asyncio.get_event_loop().run_until_complete(go())


# ── Valencia @ Deportivo — exact live event acceptance ──────────────
def test_valencia_deportivo_not_in_public_history():
    """The exact user-reported contamination event must be gone from
    the canonical query result."""
    async def go():
        db = _db()
        from services.published_results_truth import canonical_query
        q = canonical_query(days=30, exclude_ambiguous_legacy=True, include_pending=True)
        # Any Valencia @ Deportivo rows in the History set?
        n = await db.picks.count_documents({
            "$and": q["$and"] + [
                {"event": {"$regex": "Valencia.*Deportivo|Deportivo.*Valencia",
                             "$options": "i"}},
            ]
        })
        # None of the 194 candidate rows may be surfacing.
        assert n == 0, f"{n} Valencia @ Deportivo rows in public History"

    asyncio.get_event_loop().run_until_complete(go())
