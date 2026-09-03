"""PERKLOCKS ROOT FIX (2026-09-03) — Rejected Publication Healer.

Regression: MLB hitter / player-prop picks were staying permanently
``publication_state = REJECTED`` with an empty rejection-reason set
even though the underlying pick would pass the Canonical Publication
Boundary if it were re-evaluated (because the enrichers only run
once, at first ``publish_batch``, but the required fields —
``model_probability`` and ``identity_class`` — are stamped by later
pipeline stages).

These tests pin down the healer contract:

  * Picks that NOW pass the boundary are healed to PUBLISHED with
    off_board / no_bet cleared and enrichment fields persisted.

  * Picks that legitimately fail the boundary (missing lock score
    provenance, MODEL_LINE_NOT_REAL_OFFERING, etc.) are left
    untouched — the healer is NEVER a bypass of the boundary
    contract.

  * The healer is idempotent — running twice produces the same
    end state.

  * Filtering by ``pick_date`` narrows the sweep so nightly slates
    don't compete with the live current-slate healer.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone

import pytest  # noqa: F401
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

load_dotenv("/app/backend/.env")

MONGO_URL = os.getenv("MONGO_URL")
DB_NAME   = os.getenv("DB_NAME", "lockscore_db")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _seed_healable_pick(pid: str, pick_date: str) -> dict:
    """A pick whose enrichers CAN fill the missing fields to pass
    the boundary — reproduces the exact MLB hitter pattern that was
    stuck REJECTED in production.
    """
    return {
        "id":                  pid,
        "pick_date":           pick_date,
        "sport":               "MLB",
        "league":              "MLB · Props",
        "event":               "San Francisco Giants @ Pittsburgh Pirates",
        "event_time":          "2099-12-31T16:35:00Z",
        "market":              "Rafael Devers (SF) Over 0.5 Hits",
        "selection":           "Rafael Devers",
        "side":                "over",
        "line":                0.5,
        "book_odds":           -135,
        "odds_source":         "draftkings",
        # NO model_probability, NO identity_class — the pick predates
        # enrichment (this is the exact PRODUCTION shape).
        # These fields exist on the DB row and the enrichers should
        # fill in model_probability from win_probability.
        "win_probability":     82.0,
        "lock_score":          98.0,
        "published_lock_score": None,
        "publication_state":   "REJECTED",
        "publication_source":  "canonical_pipeline",
        "publication_last_state_at":  _now_iso(),
        "publication_rejected_at":    _now_iso(),
        "publication_rejection_reasons": [],
        "off_board":           False,
        "no_bet":              False,
    }


def _seed_permanent_reject(pid: str, pick_date: str) -> dict:
    """A pick that CANNOT be healed — no book_odds AND no model
    signal at all.  Boundary must still reject.
    """
    return {
        "id":                  pid,
        "pick_date":           pick_date,
        "sport":               "MLB",
        "event":               "San Francisco Giants @ Pittsburgh Pirates",
        "event_time":          "2099-12-31T16:35:00Z",
        "market":              "Some Player Over 0.5 Hits",
        "selection":           "Some Player",
        "side":                "over",
        "book_odds":           None,           # NO real line
        "no_real_book_line":   False,           # NO explicit MODEL_ONLY
        # NO probability at ANY key.
        "publication_state":   "REJECTED",
        "publication_source":  "canonical_pipeline",
        "publication_rejection_reasons": ["MISSING_MODEL_PROVENANCE"],
        "off_board":           True,
        "no_bet":              True,
    }


def test_healable_rejected_pick_is_published():
    """A REJECTED pick whose fields now pass the boundary is healed
    to PUBLISHED with off_board / no_bet cleared.
    """
    async def _run():
        client = AsyncIOMotorClient(MONGO_URL)
        db = client[DB_NAME]
        pd = "2099-12-31"
        pid = f"heal-test-{uuid.uuid4()}"
        try:
            # Clean up any prior test rows.
            await db.picks.delete_many({"pick_date": pd})
            await db.picks.insert_one(_seed_healable_pick(pid, pd))

            from services.publication_reconciliation import (
                heal_rejected_publications,
            )
            summary = await heal_rejected_publications(
                db, pick_date=pd, limit=100,
            )
            assert summary["ok"] is True
            assert summary["scanned"] >= 1
            assert summary["healed"] >= 1, f"expected healed>=1, got {summary}"

            row = await db.picks.find_one({"id": pid}, {"_id": 0})
            assert row["publication_state"] == "PUBLISHED", row
            assert row["off_board"] is False
            assert row["no_bet"] is False
            assert row.get("publication_published_at")
            assert row.get("identity_class") in (
                "AUTHORITATIVE", "MAPPED", "PROVISIONAL", "UNRESOLVED",
            )
            # model_probability filled from win_probability (82 → 0.82)
            mp = row.get("model_probability")
            assert isinstance(mp, (int, float)) and 0.0 <= mp <= 1.0, row
            # PERKLOCKS ROOT FIX §2 — canonical ``published_*`` mirror
            # fields MUST be stamped, otherwise the main-board
            # eligibility query (``published_lock_score >= 85``) will
            # skip the healed row and the pick stays invisible.
            assert row.get("published_lock_score") == 98.0, row
            assert isinstance(row.get("published_probability"), (int, float))
            assert 0.0 <= row["published_probability"] <= 1.0
            assert row.get("published_odds") == -135
            assert row.get("published_line") == 0.5
            # Stale-grade healer — pick had no ``grade`` field, so
            # canonical grade must be derived from the Lock Score
            # (98.0 → "Elite Lock" per ``sports_engine._grade``).
            assert row.get("published_grade") == "Elite Lock", row
        finally:
            await db.picks.delete_many({"pick_date": pd})
            client.close()
    asyncio.run(_run())


def test_permanent_reject_is_not_healed():
    """A pick whose boundary re-evaluation still returns REJECTED
    stays REJECTED.  The healer is NEVER a boundary bypass.
    """
    async def _run():
        client = AsyncIOMotorClient(MONGO_URL)
        db = client[DB_NAME]
        pd = "2099-12-30"
        pid = f"heal-test-{uuid.uuid4()}"
        try:
            await db.picks.delete_many({"pick_date": pd})
            await db.picks.insert_one(_seed_permanent_reject(pid, pd))

            from services.publication_reconciliation import (
                heal_rejected_publications,
            )
            summary = await heal_rejected_publications(
                db, pick_date=pd, limit=100,
            )
            assert summary["ok"] is True
            assert summary["scanned"] >= 1
            assert summary["healed"] == 0, summary

            row = await db.picks.find_one({"id": pid}, {"_id": 0})
            assert row["publication_state"] == "REJECTED"
            assert row["off_board"] is True
            assert row["no_bet"] is True
        finally:
            await db.picks.delete_many({"pick_date": pd})
            client.close()
    asyncio.run(_run())


def test_healer_is_idempotent():
    """Running the healer twice on the same slate produces the same
    end state — the second pass is a no-op on already-healed rows.
    """
    async def _run():
        client = AsyncIOMotorClient(MONGO_URL)
        db = client[DB_NAME]
        pd = "2099-12-29"
        pid = f"heal-test-{uuid.uuid4()}"
        try:
            await db.picks.delete_many({"pick_date": pd})
            await db.picks.insert_one(_seed_healable_pick(pid, pd))

            from services.publication_reconciliation import (
                heal_rejected_publications,
            )
            s1 = await heal_rejected_publications(db, pick_date=pd, limit=100)
            s2 = await heal_rejected_publications(db, pick_date=pd, limit=100)

            assert s1["healed"] == 1
            # After first pass no REJECTED rows remain in the slate.
            assert s2["scanned"] == 0
            assert s2["healed"] == 0
        finally:
            await db.picks.delete_many({"pick_date": pd})
            client.close()
    asyncio.run(_run())


def test_healer_scopes_by_pick_date():
    """pick_date param restricts the sweep — other-slate REJECTED
    picks are ignored.
    """
    async def _run():
        client = AsyncIOMotorClient(MONGO_URL)
        db = client[DB_NAME]
        pd_target = "2099-12-28"
        pd_other  = "2099-12-27"
        pid_target = f"heal-test-{uuid.uuid4()}"
        pid_other  = f"heal-test-{uuid.uuid4()}"
        try:
            await db.picks.delete_many({"pick_date": {"$in": [pd_target, pd_other]}})
            await db.picks.insert_one(_seed_healable_pick(pid_target, pd_target))
            await db.picks.insert_one(_seed_healable_pick(pid_other,  pd_other))

            from services.publication_reconciliation import (
                heal_rejected_publications,
            )
            summary = await heal_rejected_publications(
                db, pick_date=pd_target, limit=100,
            )
            assert summary["healed"] == 1

            row_t = await db.picks.find_one({"id": pid_target}, {"_id": 0})
            row_o = await db.picks.find_one({"id": pid_other},  {"_id": 0})
            assert row_t["publication_state"] == "PUBLISHED"
            assert row_o["publication_state"] == "REJECTED"
        finally:
            await db.picks.delete_many({"pick_date": {"$in": [pd_target, pd_other]}})
            client.close()
    asyncio.run(_run())



def test_stale_pass_grade_is_healed_from_lock_score():
    """PERKLOCKS ROOT FIX §3: a pick can enter REJECTED with
    ``grade='Pass'`` stamped by APEX / v2 engines even when the
    canonical Lock Score clears the >=85 floor.  The main-board
    filter requires ``published_grade != 'Pass'`` when the field
    exists, so a stale-Pass leak keeps the pick invisible even
    after healing.  The healer MUST re-derive ``published_grade``
    from the Lock Score whenever the incoming grade is Pass but
    the canonical score qualifies.
    """
    async def _run():
        client = AsyncIOMotorClient(MONGO_URL)
        db = client[DB_NAME]
        pd = "2099-12-26"
        pid = f"heal-test-{uuid.uuid4()}"
        try:
            await db.picks.delete_many({"pick_date": pd})
            row = _seed_healable_pick(pid, pd)
            row["grade"] = "Pass"                   # stale APEX label
            row["published_grade"] = "Pass"         # stale mirror
            row["lock_score"] = 95.0                # canonical Strong Lock
            await db.picks.insert_one(row)

            from services.publication_reconciliation import (
                heal_rejected_publications,
            )
            summary = await heal_rejected_publications(
                db, pick_date=pd, limit=100,
            )
            assert summary["healed"] == 1

            healed = await db.picks.find_one({"id": pid}, {"_id": 0})
            assert healed["publication_state"] == "PUBLISHED"
            # 95.0 → "Strong Lock" per sports_engine._grade.
            assert healed.get("published_grade") == "Strong Lock", healed
            # Legacy ``grade`` field also refreshed so downstream
            # readers that fall back to `grade` see the tier.
            assert healed.get("grade") == "Strong Lock", healed
        finally:
            await db.picks.delete_many({"pick_date": pd})
            client.close()
    asyncio.run(_run())
