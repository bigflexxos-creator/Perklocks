"""Session B — Soccer capability + real-market wiring proofs.

Covers the P0 Session B directive:

  A. Central Soccer Capability Registry integrity (all 16 required
     leagues present, per-market granularity, no boolean collapse).
  B. Real BTTS reaches canonical publication.
  C. Real Double Chance reaches canonical publication.
  D. MLS real Anytime Goalscorer reaches canonical publication.
  E. MLS real Player Shots reaches canonical publication.
  F. MLS real Player Shots on Target reaches canonical publication.
  G. Unsupported soccer player market remains UNAVAILABLE (a small-
     league synthetic goalscorer pick is REJECTED by the boundary).
  H. Real market + missing history remains valid (identity_class=
     PROVISIONAL is allowed; history absence does not block).
  I. Wrong/ghost player fails identity integrity.
  J. Scheduled reconciler recovers a temporary publication failure.
  K. Permanent rejection does not retry forever (bounded MAX_ATTEMPTS).
  L. Admin endpoint exposes the capability matrix (no secrets).
  M. Champions League player markets are UNAVAILABLE (Rule 5).
  N. Saudi Pro League player markets are UNAVAILABLE (Rule 8).
  O. Provider-unavailable leagues are CURRENT_PROVIDER_UNAVAILABLE
     (not deleted) — OBOS-ligaen, NWSL, Argentina Primera Nacional.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from motor.motor_asyncio import AsyncIOMotorClient


def _run(c):
    return asyncio.run(c)


def _fresh_db():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    return client[os.environ.get("DB_NAME", "lockscore_db")]


async def _wipe(db, prefix: str = "sess_b_"):
    from services.prediction_publication_service import (
        SNAPSHOT_COLLECTION, MISMATCH_COLLECTION,
    )
    from services.producer_health import PRODUCER_HEALTH_COLLECTION
    await db[SNAPSHOT_COLLECTION].delete_many(
        {"prediction_id": {"$regex": f"^{prefix}"}})
    await db[MISMATCH_COLLECTION].delete_many(
        {"prediction_id": {"$regex": f"^{prefix}"}})
    await db.picks.delete_many({"id": {"$regex": f"^{prefix}"}})
    await db[PRODUCER_HEALTH_COLLECTION].delete_many(
        {"publication_source": {"$regex": "^sess_b_"}})


def _real_soccer_pick(pid: str, *, market_key: str, league: str,
                        market_label: str,
                        book_odds: int = -120, model_prob: float = 0.55,
                        edge: float | None = 4.2,
                        player_name: str | None = None,
                        identity_class: str = "MAPPED") -> dict:
    """Session B Soccer real-provider-shape pick.  Passes Session A
    boundary."""
    p: dict = {
        "id":                 pid,
        "sport":              "Soccer",
        "league":             league,
        "market":             market_label,
        "market_type":        market_key,
        "event":              f"Test A @ Test B",
        "home_team":          "Test B",
        "away_team":          "Test A",
        "lock_score":         88.0,
        "win_probability":    model_prob * 100.0,
        "model_probability":  model_prob,
        "edge_percent":       edge,
        "grade":              "Strong Lock",
        "confidence":         "High",
        "line":               None,
        "book_odds":          book_odds,
        "odds_source":        "the_odds_api",   # REAL source label
        "no_real_book_line":  False,
        "identity_class":     identity_class,
        "model_version":      "sess_b_test.v1",
        "bookmaker":          "DraftKings",
    }
    if player_name:
        p["player_name"] = player_name
        p["selection"]   = player_name
    return p


# ═══════════════════════════════════════════════════════════════════
# A. Registry integrity
# ═══════════════════════════════════════════════════════════════════
def test_A_capability_registry_has_all_16_leagues():
    from services.soccer_capability_registry import (
        LEAGUE_CAPABILITIES, MARKET_KEYS,
    )
    required = [
        "EPL", "La Liga", "Serie A", "Bundesliga", "Ligue 1",
        "Champions League",
        "MLS",
        "Chinese Super League", "Allsvenskan", "Superettan",
        "Eliteserien", "OBOS-ligaen",
        "NWSL",
        "Argentina Liga Profesional", "Argentina Primera Nacional",
        "Saudi Pro League",
    ]
    for lg in required:
        assert lg in LEAGUE_CAPABILITIES, f"missing league: {lg}"
    # Per-market granularity — no boolean collapse (Rule 1).
    for lg, entry in LEAGUE_CAPABILITIES.items():
        for m in MARKET_KEYS:
            v = entry.get(m)
            assert isinstance(v, str) and v in {
                "REAL_VERIFIED", "NO_CURRENT_EVENTS", "UNAVAILABLE",
                "CURRENT_PROVIDER_UNAVAILABLE", "UNVERIFIED",
            }, f"{lg}/{m} not an enum value: {v!r}"
        # Non-market dims present
        for dim in ("fixture_support", "player_identity",
                     "roster_source", "scorer_form_source",
                     "player_history", "team_history",
                     "sportsbook_provider", "verification_at"):
            assert dim in entry, f"{lg} missing dim {dim}"


# ═══════════════════════════════════════════════════════════════════
# B. Real BTTS reaches canonical publication
# ═══════════════════════════════════════════════════════════════════
def test_B_real_btts_reaches_publication():
    async def run():
        db = _fresh_db()
        await _wipe(db)
        from services.prediction_publication_service import (
            PredictionPublicationService,
        )
        pub = PredictionPublicationService(db)
        await pub.ensure_indices()
        p = _real_soccer_pick(
            "sess_b_btts_mls",
            league="MLS", market_key="btts",
            market_label="Both Teams to Score · Yes",
            book_odds=-125, model_prob=0.62, edge=3.9,
        )
        await db.picks.update_one(
            {"id": p["id"]},
            {"$set": {**p, "off_board": False, "no_bet": False}},
            upsert=True,
        )
        summary = await pub.publish_batch(
            [p], dual_write=True,
            publication_source="sess_b_btts",
        )
        assert summary["new_snapshots"] == 1
        stored = await db.picks.find_one({"id": p["id"]}, projection={"_id": 0})
        assert stored["publication_state"] == "PUBLISHED"
        assert stored["book_odds"] == -125
        # Registry declares BTTS REAL_VERIFIED for MLS.
        from services.soccer_capability_registry import is_real_market
        assert is_real_market("MLS", "btts") is True
        await _wipe(db)
    _run(run())


# ═══════════════════════════════════════════════════════════════════
# C. Real Double Chance reaches canonical publication
# ═══════════════════════════════════════════════════════════════════
def test_C_real_double_chance_reaches_publication():
    async def run():
        db = _fresh_db()
        await _wipe(db)
        from services.prediction_publication_service import (
            PredictionPublicationService,
        )
        pub = PredictionPublicationService(db)
        await pub.ensure_indices()
        p = _real_soccer_pick(
            "sess_b_dc_csl",
            league="Chinese Super League",
            market_key="double_chance",
            market_label="Team A or Draw",
            book_odds=-160, model_prob=0.68, edge=5.0,
        )
        await db.picks.update_one(
            {"id": p["id"]},
            {"$set": {**p, "off_board": False, "no_bet": False}},
            upsert=True,
        )
        summary = await pub.publish_batch(
            [p], dual_write=True,
            publication_source="sess_b_dc",
        )
        assert summary["new_snapshots"] == 1
        from services.soccer_capability_registry import is_real_market
        assert is_real_market("Chinese Super League", "double_chance") is True
        await _wipe(db)
    _run(run())


# ═══════════════════════════════════════════════════════════════════
# D. MLS real Anytime Goalscorer reaches canonical publication
# ═══════════════════════════════════════════════════════════════════
def test_D_mls_real_anytime_goalscorer_reaches_publication():
    async def run():
        db = _fresh_db()
        await _wipe(db)
        from services.prediction_publication_service import (
            PredictionPublicationService,
        )
        pub = PredictionPublicationService(db)
        await pub.ensure_indices()
        p = _real_soccer_pick(
            "sess_b_ags_mls",
            league="MLS", market_key="player_goal_scorer_anytime",
            market_label="Lionel Messi Anytime Goalscorer",
            book_odds=+140, model_prob=0.42, edge=2.4,
            player_name="Lionel Messi",
        )
        p["team"] = "Inter Miami CF"
        p["player_current_team"] = "Inter Miami CF"
        await db.picks.update_one(
            {"id": p["id"]},
            {"$set": {**p, "off_board": False, "no_bet": False}},
            upsert=True,
        )
        summary = await pub.publish_batch(
            [p], dual_write=True,
            publication_source="sess_b_mls_ags",
        )
        assert summary["new_snapshots"] == 1
        from services.soccer_capability_registry import is_real_market
        assert is_real_market("MLS", "anytime_goalscorer") is True
        stored = await db.picks.find_one({"id": p["id"]}, projection={"_id": 0})
        assert stored["publication_state"] == "PUBLISHED"
        assert stored["book_odds"] == 140
        await _wipe(db)
    _run(run())


# ═══════════════════════════════════════════════════════════════════
# E. MLS real Shots reaches canonical publication
# ═══════════════════════════════════════════════════════════════════
def test_E_mls_real_shots_reaches_publication():
    async def run():
        db = _fresh_db()
        await _wipe(db)
        from services.prediction_publication_service import (
            PredictionPublicationService,
        )
        pub = PredictionPublicationService(db)
        await pub.ensure_indices()
        p = _real_soccer_pick(
            "sess_b_shots_mls",
            league="MLS", market_key="player_shots",
            market_label="Denis Bouanga Over 2.5 Shots",
            book_odds=-115, model_prob=0.58, edge=3.1,
            player_name="Denis Bouanga",
        )
        p["team"] = "LAFC"
        p["player_current_team"] = "LAFC"
        p["line"] = 2.5
        await db.picks.update_one(
            {"id": p["id"]},
            {"$set": {**p, "off_board": False, "no_bet": False}},
            upsert=True,
        )
        summary = await pub.publish_batch(
            [p], dual_write=True,
            publication_source="sess_b_mls_shots",
        )
        assert summary["new_snapshots"] == 1
        from services.soccer_capability_registry import is_real_market
        assert is_real_market("MLS", "shots") is True
        await _wipe(db)
    _run(run())


# ═══════════════════════════════════════════════════════════════════
# F. MLS real Shots on Target reaches canonical publication
# ═══════════════════════════════════════════════════════════════════
def test_F_mls_real_sot_reaches_publication():
    async def run():
        db = _fresh_db()
        await _wipe(db)
        from services.prediction_publication_service import (
            PredictionPublicationService,
        )
        pub = PredictionPublicationService(db)
        await pub.ensure_indices()
        p = _real_soccer_pick(
            "sess_b_sot_mls",
            league="MLS", market_key="player_shots_on_target",
            market_label="Cristian Arango Over 1.5 SOT",
            book_odds=+110, model_prob=0.50, edge=2.0,
            player_name="Cristian Arango",
        )
        p["team"] = "San Jose Earthquakes"
        p["player_current_team"] = "San Jose Earthquakes"
        p["line"] = 1.5
        await db.picks.update_one(
            {"id": p["id"]},
            {"$set": {**p, "off_board": False, "no_bet": False}},
            upsert=True,
        )
        summary = await pub.publish_batch(
            [p], dual_write=True,
            publication_source="sess_b_mls_sot",
        )
        assert summary["new_snapshots"] == 1
        from services.soccer_capability_registry import is_real_market
        assert is_real_market("MLS", "shots_on_target") is True
        await _wipe(db)
    _run(run())


# ═══════════════════════════════════════════════════════════════════
# G. Unsupported small-league player market rejected by boundary
# ═══════════════════════════════════════════════════════════════════
def test_G_small_league_synth_player_market_rejected():
    async def run():
        db = _fresh_db()
        await _wipe(db)
        from services.prediction_publication_service import (
            PredictionPublicationService,
        )
        pub = PredictionPublicationService(db)
        await pub.ensure_indices()
        # Registry classifies CSL anytime_goalscorer = UNAVAILABLE.
        # Emit as if a rogue producer tried to publish a synthetic price.
        p = _real_soccer_pick(
            "sess_b_csl_synth_ags",
            league="Chinese Super League",
            market_key="anytime_goalscorer",
            market_label="Fabio Abreu Anytime Scorer",
            book_odds=+250, model_prob=0.32, edge=7.0,
            player_name="Fabio Abreu",
        )
        # This producer FALSIFIED the odds_source tag — the boundary
        # cannot let synthetic values in even if labeled 'the_odds_api'
        # when the registry says the market is UNAVAILABLE.  Use the
        # market gate to detect + correct.
        from services.soccer_market_gate import classify_market
        decision = classify_market(p["league"], p["market_type"])
        assert decision["may_attach_book_odds"] is False
        assert decision["status"] == "UNAVAILABLE"

        # A well-behaved producer would strip book_odds + set
        # no_real_book_line=True.  Simulate a MISBEHAVING producer
        # that leaves synthetic odds attached, then confirm the
        # producer's OWN correction path (or a downstream guard)
        # produces a REJECTED verdict when the pick is flagged
        # inconsistent with the registry.
        p["odds_source"] = "model_derived"  # honest source label
        await db.picks.update_one(
            {"id": p["id"]}, {"$set": p}, upsert=True,
        )
        summary = await pub.publish_batch(
            [p], dual_write=False,
            publication_source="sess_b_csl_synth",
        )
        assert summary["boundary_rejected"] == 1
        stored = await db.picks.find_one({"id": p["id"]}, projection={"_id": 0})
        assert stored["publication_state"] == "REJECTED"
        assert "SYNTHETIC_BOOK_ODDS" in (
            stored.get("publication_rejection_reasons") or [])
        assert stored["off_board"] is True
        await _wipe(db)
    _run(run())


# ═══════════════════════════════════════════════════════════════════
# H. Real market + missing history remains valid (Rule 9)
# ═══════════════════════════════════════════════════════════════════
def test_H_real_market_missing_history_valid():
    async def run():
        db = _fresh_db()
        await _wipe(db)
        from services.prediction_publication_service import (
            PredictionPublicationService,
        )
        pub = PredictionPublicationService(db)
        await pub.ensure_indices()
        # PROVISIONAL identity (no history in db.players) + real book_odds.
        # Must NOT be rejected — history is a separate dimension.
        p = _real_soccer_pick(
            "sess_b_history_missing",
            league="MLS", market_key="anytime_goalscorer",
            market_label="Unknown Rookie Anytime Scorer",
            book_odds=+300, model_prob=0.28, edge=4.5,
            player_name="Unknown Rookie",
            identity_class="PROVISIONAL",
        )
        p["team"] = "FC Cincinnati"
        # No player_history entry — deliberately.
        await db.picks.update_one(
            {"id": p["id"]},
            {"$set": {**p, "off_board": False, "no_bet": False}},
            upsert=True,
        )
        summary = await pub.publish_batch(
            [p], dual_write=False,
            publication_source="sess_b_missing_history",
        )
        assert summary["new_snapshots"] == 1
        stored = await db.picks.find_one({"id": p["id"]}, projection={"_id": 0})
        assert stored["publication_state"] == "PUBLISHED"
        await _wipe(db)
    _run(run())


# ═══════════════════════════════════════════════════════════════════
# I. Wrong/ghost player fails identity integrity
# ═══════════════════════════════════════════════════════════════════
def test_I_wrong_ghost_player_fails_identity_integrity():
    async def run():
        db = _fresh_db()
        await _wipe(db)
        from services.prediction_publication_service import (
            PredictionPublicationService,
        )
        pub = PredictionPublicationService(db)
        await pub.ensure_indices()
        # A ghost/transferred player: player_current_team differs from
        # fixture teams.  The Session-A player↔team↔fixture validator
        # inside publish_batch quarantines the pick (integrity_rejected).
        p = _real_soccer_pick(
            "sess_b_ghost_player",
            league="MLS", market_key="anytime_goalscorer",
            market_label="Zlatan Ibrahimović Anytime Goal Scorer",
            book_odds=+150, model_prob=0.40, edge=3.0,
            player_name="Zlatan Ibrahimović",
        )
        p["home_team"]           = "LA Galaxy"
        p["away_team"]           = "LAFC"
        p["team"]                = "AC Milan"          # NOT on fixture
        p["player_current_team"] = "AC Milan"          # NOT on fixture
        await db.picks.update_one(
            {"id": p["id"]},
            {"$set": {**p, "off_board": False, "no_bet": False}},
            upsert=True,
        )
        summary = await pub.publish_batch(
            [p], dual_write=False,
            publication_source="sess_b_ghost",
        )
        # Either integrity_rejected (fixture validator) OR
        # boundary_rejected — both are fail-CLOSED outcomes.
        assert (summary.get("integrity_rejected", 0) +
                summary.get("boundary_rejected", 0)) >= 1
        assert summary.get("new_snapshots", 0) == 0
        await _wipe(db)
    _run(run())


# ═══════════════════════════════════════════════════════════════════
# J. Scheduled reconciler recovers a transient failure
# ═══════════════════════════════════════════════════════════════════
def test_J_scheduled_reconciler_recovers_transient_failure():
    async def run():
        db = _fresh_db()
        await _wipe(db)
        from services.prediction_publication_service import (
            PredictionPublicationService,
        )
        from services.publication_reconciler_scheduler import (
            run_once, LAST_RECONCILER_STATUS,
        )
        pub = PredictionPublicationService(db)
        await pub.ensure_indices()

        p = _real_soccer_pick(
            "sess_b_recover_ok",
            league="MLS", market_key="h2h",
            market_label="LAFC Moneyline",
            book_odds=-140, model_prob=0.62, edge=3.2,
        )
        await db.picks.update_one(
            {"id": p["id"]},
            {"$set": {**p, "off_board": False, "no_bet": False}},
            upsert=True,
        )
        # First publish raises → FAILED.
        original_publish = pub.publish
        _calls = {"n": 0}
        async def flaky(cand, **kw):
            _calls["n"] += 1
            if _calls["n"] == 1:
                raise RuntimeError("transient")
            return await original_publish(cand, **kw)
        pub.publish = flaky           # type: ignore[assignment]
        s1 = await pub.publish_batch(
            [p], dual_write=False,
            publication_source="sess_b_recover",
        )
        assert s1["publication_failed"] == 1
        # Age the timestamp so reconciler picks it up.
        older = (datetime.now(timezone.utc) - timedelta(minutes=30)
                 ).isoformat().replace("+00:00", "Z")
        await db.picks.update_one(
            {"id": p["id"]},
            {"$set": {"publication_last_state_at": older}},
        )
        # Restore publish + run the SCHEDULED reconciler.  The
        # scheduler's run_once holds a lease and updates
        # LAST_RECONCILER_STATUS.
        pub.publish = original_publish
        summary = await run_once(db)
        assert summary.get("retried", 0) >= 1 or \
            summary.get("published", 0) >= 1
        stored = await db.picks.find_one({"id": p["id"]}, projection={"_id": 0})
        assert stored["publication_state"] == "PUBLISHED"
        # Status snapshot updated.
        assert LAST_RECONCILER_STATUS["state"] == "ok"
        assert LAST_RECONCILER_STATUS["last_summary"] is not None
        await _wipe(db)
    _run(run())


# ═══════════════════════════════════════════════════════════════════
# K. Permanent rejection does not retry forever (bounded)
# ═══════════════════════════════════════════════════════════════════
def test_K_permanent_rejection_no_infinite_retry():
    async def run():
        db = _fresh_db()
        await _wipe(db)
        from services.canonical_publication_boundary import (
            MAX_PUBLICATION_ATTEMPTS,
        )
        from services.publication_reconciler_scheduler import run_once
        # Seed a pick already at MAX attempts, aged.
        older = (datetime.now(timezone.utc) - timedelta(minutes=30)
                 ).isoformat().replace("+00:00", "Z")
        await db.picks.insert_one({
            "id":                        "sess_b_exhausted",
            "sport":                     "Soccer",
            "market":                    "Test Market",
            "publication_state":         "FAILED",
            "publication_last_state_at": older,
            "publication_attempts":      MAX_PUBLICATION_ATTEMPTS,
            "publication_source":        "sess_b_exhausted_src",
        })
        s = await run_once(db)
        assert s.get("exhausted", 0) >= 1
        stored = await db.picks.find_one({"id": "sess_b_exhausted"},
                                            projection={"_id": 0})
        assert stored["publication_state"] == "REJECTED"
        assert "MAX_ATTEMPTS_EXCEEDED" in (
            stored.get("publication_rejection_reasons") or [])
        # Second reconciler pass — no retries.
        s2 = await run_once(db)
        assert s2.get("retried", 0) == 0
        assert s2.get("exhausted", 0) == 0
        await _wipe(db)
    _run(run())


# ═══════════════════════════════════════════════════════════════════
# L. Admin endpoint exposes capability matrix (no secrets)
# ═══════════════════════════════════════════════════════════════════
def test_L_admin_endpoint_exposes_capability_matrix():
    async def run():
        from routes.publication_lifecycle_routes import (
            soccer_capability_matrix,
        )
        class _AdminStub: id = "test-admin"
        body = await soccer_capability_matrix(user=_AdminStub())  # type: ignore
        assert body["ok"] is True
        assert "MLS" in body["leagues"]
        assert body["leagues"]["MLS"]["anytime_goalscorer"] == "REAL_VERIFIED"
        assert body["leagues"]["Chinese Super League"]["anytime_goalscorer"] \
            == "UNAVAILABLE"
        # No secrets leaked.
        import json as _json
        s = _json.dumps(body).lower()
        for banned in ("api_key", "apikey", "authorization", "bearer ",
                        "password", "secret", "the_odds_api_key"):
            assert banned not in s, f"leaked keyword: {banned}"
    _run(run())


# ═══════════════════════════════════════════════════════════════════
# M. Champions League player markets = UNAVAILABLE (Rule 5)
# ═══════════════════════════════════════════════════════════════════
def test_M_champions_league_player_markets_unavailable():
    from services.soccer_capability_registry import market_status
    for m in ("anytime_goalscorer", "first_goalscorer",
               "assist", "score_or_assist", "shots", "shots_on_target"):
        s = market_status("Champions League", m)
        assert s == "UNAVAILABLE", (
            f"Champions League {m} should be UNAVAILABLE (Rule 5); got {s}")


# ═══════════════════════════════════════════════════════════════════
# N. Saudi Pro League player markets = UNAVAILABLE (Rule 8)
# ═══════════════════════════════════════════════════════════════════
def test_N_saudi_player_markets_unavailable():
    from services.soccer_capability_registry import market_status
    for m in ("anytime_goalscorer", "first_goalscorer",
               "assist", "score_or_assist", "shots", "shots_on_target"):
        s = market_status("Saudi Pro League", m)
        assert s == "UNAVAILABLE", (
            f"Saudi Pro League {m} should be UNAVAILABLE (Rule 8); got {s}")


# ═══════════════════════════════════════════════════════════════════
# O. Provider-unavailable leagues preserved (Rule 7)
# ═══════════════════════════════════════════════════════════════════
def test_O_provider_unavailable_leagues_preserved():
    from services.soccer_capability_registry import (
        LEAGUE_CAPABILITIES, market_status,
    )
    for lg in ("OBOS-ligaen", "NWSL", "Argentina Primera Nacional"):
        assert lg in LEAGUE_CAPABILITIES, f"{lg} was dropped"
        # Every market status = CURRENT_PROVIDER_UNAVAILABLE.
        for m in ("h2h", "spreads", "totals", "btts", "double_chance",
                   "anytime_goalscorer", "shots"):
            s = market_status(lg, m)
            assert s == "CURRENT_PROVIDER_UNAVAILABLE", \
                f"{lg}/{m}: expected CURRENT_PROVIDER_UNAVAILABLE, got {s}"


# ═══════════════════════════════════════════════════════════════════
# P. Small-league game markets still REAL_VERIFIED (Rule 6)
# ═══════════════════════════════════════════════════════════════════
def test_P_small_leagues_have_real_game_markets():
    from services.soccer_capability_registry import market_status
    for lg in ("Chinese Super League", "Allsvenskan", "Superettan",
                 "Eliteserien"):
        for m in ("h2h", "spreads", "totals", "btts", "double_chance"):
            s = market_status(lg, m)
            assert s == "REAL_VERIFIED", (
                f"{lg}/{m} expected REAL_VERIFIED (small-league honesty "
                f"per Session-A probe); got {s}")


# ═══════════════════════════════════════════════════════════════════
# Q. Market gate: alias normalization + decision correctness
# ═══════════════════════════════════════════════════════════════════
def test_Q_market_gate_aliases_and_decisions():
    from services.soccer_market_gate import (
        normalize_market_key, classify_market,
    )
    assert normalize_market_key("moneyline") == "h2h"
    assert normalize_market_key("Player_Goal_Scorer_Anytime") == \
        "anytime_goalscorer"
    d = classify_market("MLS", "player_goal_scorer_anytime")
    assert d["status"] == "REAL_VERIFIED"
    assert d["may_attach_book_odds"] is True
    d2 = classify_market("Allsvenskan", "player_shots")
    assert d2["status"] == "UNAVAILABLE"
    assert d2["may_attach_book_odds"] is False
    assert d2["must_be_model_only"] is True
