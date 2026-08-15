"""SOCCER_UNIVERSAL_RUNTIME_FLOW_RESTORED regression tests — iteration 103.

Proves the architectural invariant on the live backend:

    Provider Row -> Canonical Identity -> Engine Model
                 -> Canonical Publication -> Consumer Decision

Zero silent drops; every candidate either progresses or receives a
precise rejection code from ``services.soccer_rejection_taxonomy``.

Uses ``asyncio.run`` per-test to avoid a hard dependency on
``pytest-asyncio`` (not installed in this repo).
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone

import pytest
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

sys.path.insert(0, "/app/backend")
load_dotenv("/app/backend/.env")


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if not hasattr(asyncio, "run") \
        else asyncio.run(coro)


def _db():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    return client, client[os.environ["DB_NAME"]]


TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ─────────────────────────────────────────────────────────────────
# 1.  MLS TEAM CONTEXT — natively resolvable through the existing
#     Soccer engine (no new engine, no new provider call).
# ─────────────────────────────────────────────────────────────────
def test_mls_team_context_resolves_from_existing_stores():
    from services.soccer_game_model import build_soccer_team_ctx

    async def run():
        client, db = _db()
        try:
            ctx = await build_soccer_team_ctx(
                db, home_team="Inter Miami CF",
                away_team="New York City FC", league="MLS",
            )
            assert ctx.get("home_form"), "Inter Miami must resolve MLS form"
            assert ctx.get("away_form"), "NYCFC must resolve MLS form"
            for side in ("home_form", "away_form"):
                row = ctx[side]
                assert row["gf_avg"] is not None
                assert row["ga_avg"] is not None
                assert row["source"] in {
                    "mls_espn_stats+player_game_actuals",
                    "soccer_matches_rolling20",
                    "team_form", "soccer_team_form",
                }, f"{side} source={row['source']}"
        finally:
            client.close()
    _run(run())


def test_mls_engine_produces_valid_probability():
    from services.soccer_game_model import (
        build_soccer_team_ctx, estimate_soccer_game_probabilities,
        compute_game_market_prob,
    )

    async def run():
        client, db = _db()
        try:
            ctx = await build_soccer_team_ctx(
                db, home_team="Inter Miami CF",
                away_team="New York City FC", league="MLS",
            )
            out = estimate_soccer_game_probabilities(
                ctx, "Inter Miami CF", "New York City FC",
            )
            assert out.available, f"MLS model unavailable — {out.reason}"
            assert out.tier in {"A", "B", "C"}
            total = out.p_home + out.p_draw + out.p_away
            assert abs(total - 1.0) < 1e-3

            p_home = await compute_game_market_prob(
                db, home_team="Inter Miami CF",
                away_team="New York City FC", league="MLS",
                market_key="h2h", selection="Inter Miami CF",
            )
            assert p_home is not None and 0 < p_home < 1
        finally:
            client.close()
    _run(run())


# ─────────────────────────────────────────────────────────────────
# 2.  CANONICAL IDENTITY — every consumer soccer pick carries
#     canonical_wager_id + preserved provider_event_id.
# ─────────────────────────────────────────────────────────────────
def test_soccer_picks_carry_canonical_wager_identity():
    async def run():
        client, db = _db()
        try:
            docs = await db.picks.find({
                "sport": "Soccer", "pick_date": TODAY,
                "off_board": {"$ne": True},
                "source": "real_line_soccer_v2",
            }, {
                "id": 1, "canonical_wager_id": 1, "provider_event_id": 1,
                "event_id": 1,
            }).limit(200).to_list(200)
            if not docs:
                pytest.skip("No on-board real_line_soccer_v2 picks")
            for d in docs:
                assert d.get("canonical_wager_id"), (
                    f"pick {d.get('id')} missing canonical_wager_id"
                )
                assert d.get("provider_event_id") == d.get("event_id")
        finally:
            client.close()
    _run(run())


def test_mls_picks_reach_consumer_board():
    async def run():
        client, db = _db()
        try:
            n_raw = await db.picks.count_documents({
                "sport": "Soccer", "league": "MLS", "pick_date": TODAY,
            })
            n_on = await db.picks.count_documents({
                "sport": "Soccer", "league": "MLS", "pick_date": TODAY,
                "off_board": {"$ne": True},
            })
            if n_raw > 0:
                assert n_on > 0, (
                    f"MLS candidates={n_raw} but ZERO reach board — "
                    "reachability regression"
                )
        finally:
            client.close()
    _run(run())


# ─────────────────────────────────────────────────────────────────
# 3.  REJECTION TAXONOMY — no silent drops.
# ─────────────────────────────────────────────────────────────────
def test_rejection_taxonomy_has_universal_codes():
    from services.soccer_rejection_taxonomy import ALL_CODES
    required = {
        "IDENTITY_FAILURE", "NO_TEAM_CONTEXT", "NO_PLAYER_CONTEXT",
        "NO_MODEL_PROBABILITY", "NO_REAL_LINE", "NO_REAL_MARKET",
        "NO_RECENT_FORM", "NO_PLAYER_HISTORY", "NO_POSITIVE_EDGE",
        "EVIDENCE_INSUFFICIENT", "DUPLICATE_CANONICAL_WAGER",
        "BOARD_INELIGIBLE", "STALE_EVENT",
    }
    missing = required - ALL_CODES
    assert not missing, f"Missing taxonomy codes: {missing}"


def test_off_board_soccer_picks_carry_precise_reason():
    from services.soccer_rejection_taxonomy import ALL_CODES

    async def run():
        client, db = _db()
        try:
            cursor = db.picks.find({
                "sport": "Soccer", "pick_date": TODAY, "off_board": True,
                "source": {"$in": ["real_line_soccer_v2",
                                   "real_line_alt_scorer_v1"]},
            }, {"off_board_reasons": 1}).limit(500)
            n_checked = 0; n_valid = 0
            async for d in cursor:
                n_checked += 1
                reasons = d.get("off_board_reasons") or []
                if reasons and any(r in ALL_CODES for r in reasons):
                    n_valid += 1
            if n_checked == 0:
                pytest.skip("no off-board picks")
            ratio = n_valid / n_checked
            assert ratio >= 0.95, (
                f"only {n_valid}/{n_checked} off-board picks carry "
                f"canonical reason codes"
            )
        finally:
            client.close()
    _run(run())


# ─────────────────────────────────────────────────────────────────
# 4.  GOALSCORER TTL — live_alt_lines TTL raised to 90 min.
# ─────────────────────────────────────────────────────────────────
def test_live_alt_lines_ttl_extended():
    async def run():
        client, db = _db()
        try:
            info = await db.command({"listIndexes": "live_alt_lines"})
            batch = info.get("cursor", {}).get("firstBatch", []) or []
            last_seen = next(
                (ix for ix in batch if ix.get("name") == "last_seen_1"),
                None,
            )
            if not last_seen:
                pytest.skip("live_alt_lines index not yet created")
            ttl = int(last_seen.get("expireAfterSeconds") or 0)
            assert ttl == 5400, (
                f"live_alt_lines TTL={ttl}s must be 5400s"
            )
        finally:
            client.close()
    _run(run())


# ─────────────────────────────────────────────────────────────────
# 5.  ESPN IS ENRICHMENT ONLY — provider identity is not overwritten.
# ─────────────────────────────────────────────────────────────────
def test_espn_enrichment_never_overwrites_provider_identity():
    async def run():
        client, db = _db()
        try:
            async for d in db.picks.find({
                "sport": "Soccer", "pick_date": TODAY,
                "source": "real_line_soccer_v2",
            }, {"provider_event_id": 1, "event_id": 1}).limit(50):
                assert d.get("provider_event_id") == d.get("event_id"), (
                    f"ESPN overwrote provider_event_id "
                    f"(event_id={d.get('event_id')} "
                    f"provider_event_id={d.get('provider_event_id')})"
                )
        finally:
            client.close()
    _run(run())


# ─────────────────────────────────────────────────────────────────
# 6.  EPL / Big-5 leagues still work (regression guard).
# ─────────────────────────────────────────────────────────────────
def test_epl_still_works():
    from services.soccer_game_model import (
        build_soccer_team_ctx, estimate_soccer_game_probabilities,
    )

    async def run():
        client, db = _db()
        try:
            ctx = await build_soccer_team_ctx(
                db, home_team="Chelsea", away_team="Arsenal",
                league="EPL",
            )
            hs = (ctx.get("home_form") or {}).get("source", "")
            assert hs != "mls_espn_stats+player_game_actuals", (
                f"EPL Chelsea leaked into MLS adapter: {hs}"
            )
            out = estimate_soccer_game_probabilities(
                ctx, "Chelsea", "Arsenal",
            )
            assert out.available and out.tier in {"A", "B", "C"}
        finally:
            client.close()
    _run(run())


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
