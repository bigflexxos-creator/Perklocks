"""Phase-1 completion regression tests.

Covers:
  1-3.  Strict >85 board-eligibility contract.
  4.    Central eligibility service used by all board-facing paths.
  5-6.  Candidate disposition / accountability.
  7.    Published-but-≤85 stays off the board.
  8.    >85 published candidate reaches the board.
  9.    Market-surfacing chips unchanged.
  10.   P0 canonical publication regression unaffected.
"""
from __future__ import annotations

import asyncio
import os
import re
import pathlib
import uuid
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from motor.motor_asyncio import AsyncIOMotorClient


_BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
_TEST_ID_PREFIX = "p1elig_"


def _run(c):
    async def _w():
        client = AsyncIOMotorClient(os.environ["MONGO_URL"])
        try:
            return await c
        finally:
            client.close()
    return asyncio.run(_w())


def _fresh_db():
    return AsyncIOMotorClient(os.environ["MONGO_URL"])[
        os.environ.get("DB_NAME", "lockscore_db")]


# ── 1-3. Strict >85 boundary ────────────────────────────────────────
def test_eligibility_helper_boundary_84_99_off():
    from services.main_board_eligibility import is_main_board_eligible
    assert is_main_board_eligible({"lock_score": 84.99}) is False


def test_eligibility_helper_boundary_85_00_off():
    from services.main_board_eligibility import is_main_board_eligible
    assert is_main_board_eligible({"lock_score": 85.00}) is False
    assert is_main_board_eligible({"lock_score_v2": 85.00}) is False


def test_eligibility_helper_boundary_85_01_on():
    from services.main_board_eligibility import is_main_board_eligible
    assert is_main_board_eligible({"lock_score": 85.01}) is True
    assert is_main_board_eligible({"lock_score_v2": 85.01}) is True


def test_eligibility_helper_86_00_on():
    from services.main_board_eligibility import is_main_board_eligible
    assert is_main_board_eligible({"lock_score": 86.00}) is True


def test_eligibility_helper_max_of_two_aliases():
    from services.main_board_eligibility import is_main_board_eligible
    # Either alias clearing 85.01 is sufficient.
    assert is_main_board_eligible({"lock_score": 80.0,
                                     "lock_score_v2": 90.0}) is True


def test_eligibility_helper_rejects_bad_input():
    from services.main_board_eligibility import is_main_board_eligible
    assert is_main_board_eligible({"lock_score": "bogus"}) is False
    assert is_main_board_eligible({}) is False


def test_query_helper_uses_gte_85_01():
    from services.main_board_eligibility import (
        MAIN_BOARD_LOCK_FLOOR_INCLUSIVE, main_board_lock_score_query,
    )
    assert MAIN_BOARD_LOCK_FLOOR_INCLUSIVE == 85.01
    q = main_board_lock_score_query()
    assert q == {"$or": [
        {"lock_score":    {"$gte": 85.01}},
        {"lock_score_v2": {"$gte": 85.01}},
    ]}


# ── 4. Central eligibility service wired into /picks/today ──────────
def test_picks_routes_imports_central_eligibility_module():
    src = (_BACKEND_ROOT / "routes" / "picks_routes.py").read_text()
    assert "from services.main_board_eligibility import" in src
    assert "MAIN_BOARD_LOCK_FLOOR_INCLUSIVE" in src


def test_no_active_thin_slate_fallback_to_75_65_55_on_main_feed():
    src = (_BACKEND_ROOT / "routes" / "picks_routes.py").read_text()
    # The old auto-relax block still exists textually (retained comment
    # + `if False and _is_main_board_view:` gate).  What must be true:
    # the gate is DEAD (`if False`), so no runtime path can lower the
    # main-board floor to 75 / 65 / 55.
    assert "if False and _is_main_board_view:" in src, (
        "main-board thin-slate fallback must be explicitly disabled"
    )
    # And the new default_floor uses the strict constant, not 85.0.
    idx = src.find("default_floor =")
    assert idx > 0
    window = src[idx:idx + 400]
    assert "MAIN_BOARD_LOCK_FLOOR_INCLUSIVE" in window


# ── 5-6. Candidate disposition ──────────────────────────────────────
def test_disposition_module_defines_all_required_reason_codes():
    from services import candidate_disposition as cd
    for r in ("market_not_discovered", "unsupported_market",
              "normalization_failed", "insufficient_data",
              "model_rejected", "line_stale", "lineup_uncertain",
              "lock_score_below_board_threshold", "publication_failed",
              "duplicate", "no_bet"):
        assert r in cd.ALL_REASONS, f"missing reason code {r!r}"


def test_disposition_module_defines_all_required_stages():
    from services import candidate_disposition as cd
    for s in ("discovered", "ingested", "normalized", "evaluated",
              "accepted", "rejected", "published", "board_eligible"):
        assert s in cd.ALL_STAGES


def test_disposition_record_and_readback():
    from services.candidate_disposition import (
        record_disposition, why_missing,
        STAGE_DISCOVERED, STAGE_EVALUATED, STAGE_REJECTED,
        REASON_LOCK_SCORE_BELOW_FLOOR,
    )
    key = _TEST_ID_PREFIX + uuid.uuid4().hex[:12]

    async def go():
        db = _fresh_db()
        await db.candidate_dispositions.delete_many({"candidate_key": key})
        try:
            await record_disposition(db, candidate_key=key,
                                        stage=STAGE_DISCOVERED,
                                        sport="MLB",
                                        market="Judge Over 1.5 hits")
            await record_disposition(db, candidate_key=key,
                                        stage=STAGE_EVALUATED,
                                        sport="MLB", lock_score=84.5)
            await record_disposition(db, candidate_key=key,
                                        stage=STAGE_REJECTED,
                                        sport="MLB", lock_score=84.5,
                                        reason=REASON_LOCK_SCORE_BELOW_FLOOR,
                                        detail="cleared model but 84.5 < 85.01")
            trail = await why_missing(db, key)
            assert [t["stage"] for t in trail] == [
                "discovered", "evaluated", "rejected",
            ]
            assert trail[-1]["reason"] == "lock_score_below_board_threshold"
            assert trail[-1]["detail"].startswith("cleared model")
        finally:
            await db.candidate_dispositions.delete_many({"candidate_key": key})
    _run(go())


def test_disposition_detail_is_capped():
    from services.candidate_disposition import record_disposition
    key = _TEST_ID_PREFIX + uuid.uuid4().hex[:12]
    async def go():
        db = _fresh_db()
        await db.candidate_dispositions.delete_many({"candidate_key": key})
        try:
            await record_disposition(db, candidate_key=key,
                                        stage="rejected",
                                        detail="X" * 5000)
            d = await db.candidate_dispositions.find_one(
                {"candidate_key": key}, {"_id": 0})
            assert d and len(d["detail"]) == 240
        finally:
            await db.candidate_dispositions.delete_many({"candidate_key": key})
    _run(go())


# ── 7-8. Published-but-≤85 stays off, >85 reaches board ─────────────
def test_published_pick_at_exactly_85_is_off_board():
    from services.main_board_eligibility import is_main_board_eligible
    pub = {"lock_score": 85.00, "lock_score_v2": 85.00,
           "publication_source": "canonical_pipeline"}
    assert is_main_board_eligible(pub) is False


def test_published_pick_above_85_reaches_board():
    from services.main_board_eligibility import is_main_board_eligible
    pub = {"lock_score": 85.05, "publication_source": "canonical_pipeline"}
    assert is_main_board_eligible(pub) is True


# ── 9. Market-surfacing chips unchanged (protection) ────────────────
def test_market_chips_unchanged_from_phase1_surfacing_task():
    import server as srv
    # Just spot-check the additions we made — nothing was removed.
    mlb_toks = {m["token"] for m in srv.SPORT_MARKETS["MLB"]}
    assert "batter_total_bases" in mlb_toks
    assert "batter_rbis" in mlb_toks
    assert "batter_home_runs" not in mlb_toks
    assert "nrfi_yrfi" not in mlb_toks and "1st_inning_runs" not in mlb_toks
    for tok in ("player_points_rebounds_assists", "player_threes"):
        assert tok in {m["token"] for m in srv.SPORT_MARKETS["NBA"]}
    for sport in ("NFL", "CFB"):
        tks = {m["token"] for m in srv.SPORT_MARKETS[sport]}
        for t in ("player_1st_td", "player_pass_tds", "player_pass_attempts",
                    "player_pass_completions", "player_rush_attempts",
                    "player_rush_tds", "player_receptions",
                    "player_reception_tds"):
            assert t in tks
    soc = {m["token"] for m in srv.SPORT_MARKETS["Soccer"]}
    assert "spread" in soc
    ten = {m["token"] for m in srv.SPORT_MARKETS["Tennis"]}
    assert "spread" in ten
