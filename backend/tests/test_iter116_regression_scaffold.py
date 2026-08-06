"""Phase 1a — Regression Test SCAFFOLDING for endpoint parity.

The Phase 1a scaffolding intentionally does NOT yet assert equality
across endpoints — that's the Phase 1b deliverable.  Instead, this file
establishes the fixture + inventory used by Phase 1b so the wiring is
in place and reviewable now:

  1. `ENDPOINTS_UNDER_CONTRACT` — the definitive list of endpoints that
     must return identical values for every prediction after Phase 1b.
  2. `CONSUMER_FILES` — every backend file that reads (not writes)
     `lock_score` / `win_probability` / `edge` / `grade` etc.  Phase 1b
     converts these to read `published_*`.
  3. A `fixture_publish_candidate()` helper that seeds a synthetic
     candidate through the publication service and verifies the
     snapshot lands.
  4. A `values_across_endpoints_scaffolding()` helper stub that Phase 1b
     will fill in with the actual `httpx.AsyncClient` calls.

Running this file today produces a PASS result but simply asserts the
scaffolding is in place.  Phase 1b will add the real cross-endpoint
comparisons.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from motor.motor_asyncio import AsyncIOMotorClient


# ─────────────────────────────────────────────────────────────────
# Endpoint inventory — Phase 1a locks this list in.  Phase 1b will
# add HTTP calls that verify each endpoint returns the same
# published_* values.
# ─────────────────────────────────────────────────────────────────
ENDPOINTS_UNDER_CONTRACT: list[dict[str, str]] = [
    {"name": "picks_today",         "method": "GET", "path": "/api/picks/today"},
    {"name": "pick_detail",         "method": "GET", "path": "/api/picks/{id}"},
    {"name": "hot_picks",           "method": "GET", "path": "/api/picks/hot"},
    {"name": "upset_picks",         "method": "GET", "path": "/api/picks/upset"},
    {"name": "bet_slip",            "method": "GET", "path": "/api/bet-slip"},
    {"name": "parlay_builder",      "method": "POST", "path": "/api/parlays/optimize"},
    {"name": "parlay_history",      "method": "GET", "path": "/api/parlay-history"},
    {"name": "user_bets",           "method": "GET", "path": "/api/user/bets"},
    {"name": "settlement_input",    "method": "GET", "path": "/api/admin/settlement-preview"},
    {"name": "history_analytics",   "method": "GET", "path": "/api/analytics/history"},
    {"name": "lab_signals",         "method": "GET", "path": "/api/lab/signals"},
    {"name": "lab_dashboard",       "method": "GET", "path": "/api/lab/dashboard"},
    {"name": "alt_lines_pick",      "method": "GET", "path": "/api/alt-lines/{id}"},
    {"name": "alt_lines_board",     "method": "GET", "path": "/api/alt-lines/board"},
    {"name": "admin_pick_evidence", "method": "GET", "path": "/api/admin/pick-evidence/{id}"},
]


# ─────────────────────────────────────────────────────────────────
# Consumer file inventory — every file that READS one of the
# published fields.  Phase 1b will convert these to `published_*`.
# Generated from grep results 2026-08-06; refresh with:
#   grep -rn -E "get\(['\"]lock_score['\"]|pick\.lock_score" \
#     backend --include='*.py' | grep -v '=\s*'
# ─────────────────────────────────────────────────────────────────
CONSUMER_FILES: list[str] = [
    "server.py",                                # canonicalizer + dozens of read paths
    "routes/picks_routes.py",                    # /api/picks/today, /hot, /upset
    "routes/admin_routes.py",                    # /api/admin/* + /api/alt-lines/*
    "routes/analytics_routes.py",                # /api/analytics/*
    "lab_routes.py",                             # /api/lab/*
    "sports_engine.py",                          # elite tier composite reads
    "services/prediction_fusion_engine.py",      # fusion output surfaces
    "services/pick_matchup_wiring.py",           # matchup decorator
    "services/pick_fusion_decorator.py",         # fusion decorator
    "services/signal_engine/engine.py",          # signal rank
    "services/signal_engine/rank.py",            # signal rank
    "services/lock_score_performance.py",        # bucket ROI reads
    "services/data_driven_model.py",             # confidence reads
    "services/trained_prediction_engine.py",     # line reads
    "services/mls_direct_inject.py",             # direct-inject read path
    "services/soccer_prop_inject.py",            # direct-inject read path
    "services/espn_soccer_fixtures.py",          # ESPN fixture read path
    "services/odds_provider.py",                 # odds surface reads
    "analytics.py",                              # analytics reads
    "learning_engine.py",                        # learning reads
    "learning_system_v2.py",                     # learning v2
    "lock_calibration.py",                       # calibration
    "pick_validator.py",                         # validator reads
    "board_validator.py",                        # board validator
    "quality_gate.py",                           # quality gate
    "elite_players.py",                          # elite reads
    "evidence_engine.py",                        # evidence reads
    "market_competition/routes.py",              # market comp
    "tennis_extra/picks.py",                     # tennis extra
    "tennis_engine.py",                          # tennis engine
    "soccer_lab.py",                             # soccer lab
    "soccer/predictor.py",                       # soccer predictor
    "soccer_hot_scorers.py",                     # soccer hot scorers
    "backtest.py",                               # backtest
    "sportdb_player_scorer.py",                  # sportdb scorer
    "sportdb_xg_totals.py",                      # xG totals
    "uefa_espn_ingest.py",                       # UEFA ingest
    "ufc_espn_ingest.py",                        # UFC ingest
    "prop_settlement.py",                        # settlement reads
    "settlement_engine.py",                      # settlement engine
    "grading_validator.py",                      # grading
    "steam_detector.py",                         # steam detector
    "brain/nrfi_engine.py",                      # NRFI
    "brain/sim_runner.py",                       # simulator
    "brain/candidates.py",                       # candidates
    "pick_enrichment.py",                        # enrichment
    "thesportsdb_scorer.py",                     # sportsdb scorer
]


def _run(c):
    return asyncio.run(c)


def _fresh_db():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    return client[os.environ.get("DB_NAME", "lockscore_db")]


async def fixture_publish_candidate(db, prediction_id: str,
                                     **overrides: Any) -> dict:
    """Seed a synthetic candidate through the publication service and
    return the resulting snapshot.  Phase 1b will call this in its
    real cross-endpoint parity assertions."""
    from services.prediction_publication_service import (
        PredictionPublicationService,
    )
    pub = PredictionPublicationService(db)
    await pub.ensure_indices()
    candidate = {
        "id": prediction_id,
        "sport": "MLB",
        "market": "Test Player (TEA) Over 1.5 hits",
        "lock_score": 82.0,
        "win_probability": 0.58,
        "edge_percent": 2.4,
        "grade": "Strong Lock",
        "confidence": 82.0,
        "line": 1.5,
        "book_odds": -130,
        "reasoning": {"summary": "regression-scaffold fixture"},
    }
    candidate.update(overrides)
    await db.picks.delete_one({"id": prediction_id})
    await db.picks.insert_one({**candidate, "pick_date": "2026-08-06"})
    await pub.publish(candidate)
    snap = await pub.get_active_snapshot(prediction_id)
    return snap


# ─────────────────────────────────────────────────────────────────
# Scaffolding assertions — Phase 1a only checks structure.
# Phase 1b will add cross-endpoint value comparisons.
# ─────────────────────────────────────────────────────────────────
def test_endpoint_inventory_is_stable():
    """The endpoint list under contract must not be empty and every
    entry must carry a method + path."""
    assert len(ENDPOINTS_UNDER_CONTRACT) >= 6, \
        "user asked for at least these 6: today, detail, hot, upset, " \
        "bet slip, parlay builder"
    for ep in ENDPOINTS_UNDER_CONTRACT:
        assert ep["method"] in ("GET", "POST"), ep
        assert ep["path"].startswith("/api/"), ep


def test_consumer_inventory_covers_top_hotspots():
    """The audit identified ~91 files touching published fields.
    Phase 1a scaffolding must at least list the top hotspots as
    consumers so Phase 1b knows what to convert."""
    required = {
        "server.py",
        "routes/picks_routes.py",
        "routes/admin_routes.py",
        "lab_routes.py",
        "sports_engine.py",
        "services/prediction_fusion_engine.py",
        "prop_settlement.py",
    }
    missing = required - set(CONSUMER_FILES)
    assert not missing, f"top hotspots missing from inventory: {missing}"


def test_scaffolding_fixture_publishes_and_reads_back():
    async def run():
        db = _fresh_db()
        pid = "pub_test_regr_scaffold"
        try:
            snap = await fixture_publish_candidate(db, pid, lock_score=91.0)
            assert snap is not None
            assert snap["published_lock_score"] == 91.0
        finally:
            await db.picks.delete_one({"id": pid})
            from services.prediction_publication_service import (
                SNAPSHOT_COLLECTION, MISMATCH_COLLECTION,
            )
            await db[SNAPSHOT_COLLECTION].delete_many(
                {"prediction_id": pid})
            await db[MISMATCH_COLLECTION].delete_many(
                {"prediction_id": pid})
    _run(run())
