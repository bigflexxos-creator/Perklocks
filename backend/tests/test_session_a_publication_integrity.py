"""Session A — P0 Publication Integrity + No-Stuck-Pick Closure.

Tests A-K per the P0 SESSION A directive.  These are the definitive
regression tests that lock the Session A contract in place:

  A. Synthetic-odds rejection — a producer that emits ``book_odds``
     with a KNOWN synthetic ``odds_source`` label is REJECTED.
  B. Null-edge preservation — ``edge_percent = None`` round-trips
     without being coerced to 0.
  C. Real calculated-zero edge — ``edge_percent = 0`` is preserved
     as a legitimate calculated value, not converted to None.
  D. Canonical boundary enforcement — every candidate ``publish_batch``
     accepts crosses ``evaluate_publication``.
  E. Main-Lock fail-CLOSED — a boundary-rejected pick is marked
     ``off_board = True`` + ``no_bet = True`` and its
     ``publication_state`` is REJECTED — the user-visible board CANNOT
     surface it.
  F. Lifecycle: PENDING → PUBLISHED transition on success.
  G. Lifecycle: PENDING → REJECTED with a reason on policy failure.
  H. Lifecycle: PENDING → FAILED on transient error, then
     reconciliation successfully republishes → PUBLISHED.
  I. Max-retry bound — a pick exceeding MAX_PUBLICATION_ATTEMPTS
     transitions permanently to REJECTED with reason
     ``MAX_ATTEMPTS_EXCEEDED``; the reconciler never retries it again.
  J. Producer health telemetry — batch counts increment lifetime
     counters, ``last_success_at`` / ``last_failure_at`` update.
  K. Admin lifecycle endpoint — read-only counts + oldest pending
     + rejection reason counts + producer health.  NO secrets or raw
     provider payloads are exposed.

Also included: runtime-proof integration
  L. NO_CURRENT_EVENTS vs STALE/BROKEN — a producer running with 0
     picks records ``last_no_events_at`` distinctly from a failure
     state.
  M. One real active producer (``publication_helpers.publish_upserted_picks``)
     flows through the canonical boundary and reaches PUBLISHED.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from motor.motor_asyncio import AsyncIOMotorClient


def _run(c):
    return asyncio.run(c)


def _fresh_db():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    return client[os.environ.get("DB_NAME", "lockscore_db")]


async def _wipe(db, prefix: str = "sess_a_"):
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
        {"publication_source": {"$regex": "^sess_a_"}})


def _real_candidate(pid: str, *, lock=88.0, prob=0.62,
                     edge=3.5, book_odds=-140,
                     odds_source="the_odds_api", grade="Strong Lock",
                     no_real_book_line=False,
                     identity_class="AUTHORITATIVE",
                     ) -> dict:
    """Session-A-compliant candidate: model_probability + real odds +
    identity_class populated.  Passes every rule of the boundary.

    Phase 10C fixture refresh: added canonical event participants
    (`event`, `home_team`, `away_team`) so the Phase 10A player→event
    identity gate can prove that "Aaron Judge / NYY" belongs to the
    Yankees @ Red Sox event.  This was implicit in real production
    picks (the enricher populates these) but the legacy fixture
    predates the identity gate."""
    return {
        "id":                 pid,
        "sport":              "MLB",
        "event":              "Yankees @ Red Sox",
        "home_team":          "Red Sox",
        "away_team":          "Yankees",
        "event_id":           "sess_a_evt_1",
        "market":             "Aaron Judge Over 1.5 hits",
        "player_name":        "Aaron Judge",
        "team":               "NYY",
        "player_team":        "Yankees",
        "lock_score":         lock,
        "win_probability":    prob * 100.0,
        "model_probability":  prob,
        "edge_percent":       edge,
        "grade":              grade,
        "confidence":         "High",
        "line":               1.5,
        "book_odds":          book_odds,
        "odds_source":        odds_source,
        "no_real_book_line":  no_real_book_line,
        "identity_class":     identity_class,
        "model_version":      "sess_a_test.v1",
    }


# ═══════════════════════════════════════════════════════════════════
# A. Synthetic-odds rejection
# ═══════════════════════════════════════════════════════════════════
def test_A_synthetic_odds_rejected():
    from services.canonical_publication_boundary import (
        evaluate_publication, PublicationState, RejectionReason,
    )
    # Synthetic label + book_odds present → REJECTED.
    p = _real_candidate("sess_a_A1",
                          odds_source="model_derived", book_odds=-150)
    v = evaluate_publication(p)
    assert v.state == PublicationState.REJECTED
    assert RejectionReason.SYNTHETIC_BOOK_ODDS.value in v.reasons

    # Model-only label + book_odds → also REJECTED (contradiction).
    p2 = _real_candidate("sess_a_A2",
                          odds_source="MODEL_ONLY", book_odds=-150)
    v2 = evaluate_publication(p2)
    assert v2.state == PublicationState.REJECTED

    # no_real_book_line + book_odds → REJECTED.
    p3 = _real_candidate("sess_a_A3",
                          odds_source="model_derived", book_odds=-150,
                          no_real_book_line=True)
    v3 = evaluate_publication(p3)
    assert v3.state == PublicationState.REJECTED

    # Correct MODEL_ONLY shape (no book_odds) → ACCEPTED.
    p4 = _real_candidate("sess_a_A4",
                          odds_source="MODEL_ONLY", book_odds=None,
                          no_real_book_line=True, edge=None)
    v4 = evaluate_publication(p4)
    assert v4.state == PublicationState.PUBLISHED


# ═══════════════════════════════════════════════════════════════════
# B/C. Null-edge preservation + real calculated zero
# ═══════════════════════════════════════════════════════════════════
def test_B_null_edge_preserved_none_vs_zero():
    async def run():
        db = _fresh_db()
        await _wipe(db)
        from services.prediction_publication_service import (
            PredictionPublicationService, SNAPSHOT_COLLECTION,
        )
        pub = PredictionPublicationService(db)
        await pub.ensure_indices()

        # None edge (no book line for this pick).
        p_none = _real_candidate("sess_a_B_none",
                                   edge=None,
                                   book_odds=None,
                                   odds_source="MODEL_ONLY",
                                   no_real_book_line=True)
        # Real calculated zero edge (book line exists, edge computed to 0).
        p_zero = _real_candidate("sess_a_B_zero", edge=0.0)
        # Positive edge sanity.
        p_pos  = _real_candidate("sess_a_B_pos",  edge=4.2)

        summary = await pub.publish_batch(
            [p_none, p_zero, p_pos], dual_write=False,
            publication_source="sess_a_test",
        )
        assert summary["new_snapshots"] == 3, (
            f"expected 3 published; got summary={summary}")

        snaps = {s["prediction_id"]: s async for s in
                  db[SNAPSHOT_COLLECTION].find({
                      "prediction_id": {"$regex": "^sess_a_B_"},
                  })}
        # C: None round-trips.
        assert snaps["sess_a_B_none"]["published_edge"] is None
        # B: 0.0 round-trips as 0.0, not None.
        assert snaps["sess_a_B_zero"]["published_edge"] == 0.0
        assert snaps["sess_a_B_pos"]["published_edge"] == 4.2

        await _wipe(db)
    _run(run())


# ═══════════════════════════════════════════════════════════════════
# D. Canonical boundary enforcement — every candidate crosses it
# ═══════════════════════════════════════════════════════════════════
def test_D_canonical_boundary_enforcement():
    from services.canonical_publication_boundary import evaluate_publication
    # Pass-through pick.
    good = _real_candidate("sess_a_D_good")
    assert evaluate_publication(good).accepted is True

    # Missing pick id → REJECTED.
    bad = _real_candidate("")
    bad.pop("id", None)
    v = evaluate_publication(bad)
    assert v.accepted is False
    assert "MISSING_PICK_ID" in v.reasons

    # Missing model provenance → REJECTED.
    no_model = _real_candidate("sess_a_D_no_model")
    no_model.pop("model_probability", None)
    no_model.pop("model_win_prob", None)
    no_model["win_probability"] = None  # so extract_model_evidence can't help
    v2 = evaluate_publication(no_model)
    assert v2.accepted is False
    assert "MISSING_MODEL_PROVENANCE" in v2.reasons

    # Missing identity class → REJECTED.
    no_ic = _real_candidate("sess_a_D_no_ic")
    no_ic.pop("identity_class", None)
    v3 = evaluate_publication(no_ic)
    assert v3.accepted is False
    assert "MISSING_IDENTITY_CLASS" in v3.reasons


# ═══════════════════════════════════════════════════════════════════
# E. Main-Lock fail-CLOSED — REJECTED pick cannot surface
# ═══════════════════════════════════════════════════════════════════
def test_E_main_lock_fail_closed_on_boundary_reject():
    async def run():
        db = _fresh_db()
        await _wipe(db)
        from services.prediction_publication_service import (
            PredictionPublicationService,
        )
        pub = PredictionPublicationService(db)
        await pub.ensure_indices()

        # Seed the picks doc first (upsert) so lifecycle mark can find it.
        bad = _real_candidate("sess_a_E_synth",
                                odds_source="model_derived",
                                book_odds=-150)
        # Seed as visible (off_board=False, no_bet=False).
        await db.picks.update_one(
            {"id": "sess_a_E_synth"},
            {"$set": {**bad, "off_board": False, "no_bet": False}},
            upsert=True,
        )
        summary = await pub.publish_batch(
            [bad], dual_write=False,
            publication_source="sess_a_e_test",
        )
        assert summary["new_snapshots"] == 0
        assert summary["boundary_rejected"] == 1

        stored = await db.picks.find_one({"id": "sess_a_E_synth"},
                                            projection={"_id": 0})
        # Fail CLOSED: user-visible board cannot surface it.
        assert stored["publication_state"] == "REJECTED"
        assert stored["off_board"] is True
        assert stored["no_bet"] is True
        assert "SYNTHETIC_BOOK_ODDS" in (
            stored.get("publication_rejection_reasons") or [])
        await _wipe(db)
    _run(run())


# ═══════════════════════════════════════════════════════════════════
# F. Lifecycle: PENDING → PUBLISHED
# ═══════════════════════════════════════════════════════════════════
def test_F_lifecycle_pending_to_published():
    async def run():
        db = _fresh_db()
        await _wipe(db)
        from services.prediction_publication_service import (
            PredictionPublicationService,
        )
        pub = PredictionPublicationService(db)
        await pub.ensure_indices()

        p = _real_candidate("sess_a_F_ok")
        await db.picks.update_one(
            {"id": p["id"]},
            {"$set": {**p, "off_board": False, "no_bet": False}},
            upsert=True,
        )
        summary = await pub.publish_batch(
            [p], dual_write=False,
            publication_source="sess_a_f_test",
        )
        assert summary["new_snapshots"] == 1
        stored = await db.picks.find_one({"id": p["id"]},
                                            projection={"_id": 0})
        assert stored["publication_state"] == "PUBLISHED"
        assert stored.get("publication_published_at") is not None
        assert stored.get("publication_pending_at") is not None
        assert int(stored.get("publication_attempts") or 0) >= 1
        await _wipe(db)
    _run(run())


# ═══════════════════════════════════════════════════════════════════
# G. Lifecycle: PENDING → REJECTED with reason
# ═══════════════════════════════════════════════════════════════════
def test_G_lifecycle_pending_to_rejected():
    async def run():
        db = _fresh_db()
        await _wipe(db)
        from services.prediction_publication_service import (
            PredictionPublicationService,
        )
        pub = PredictionPublicationService(db)
        await pub.ensure_indices()

        p = _real_candidate("sess_a_G_reject",
                              odds_source="synthetic",
                              book_odds=-140)
        await db.picks.update_one(
            {"id": p["id"]}, {"$set": p}, upsert=True,
        )
        summary = await pub.publish_batch(
            [p], dual_write=False,
            publication_source="sess_a_g_test",
        )
        assert summary["boundary_rejected"] == 1
        stored = await db.picks.find_one({"id": p["id"]},
                                            projection={"_id": 0})
        assert stored["publication_state"] == "REJECTED"
        assert "SYNTHETIC_BOOK_ODDS" in (
            stored.get("publication_rejection_reasons") or [])
        assert stored.get("publication_rejected_at") is not None
        await _wipe(db)
    _run(run())


# ═══════════════════════════════════════════════════════════════════
# H. Lifecycle: PENDING → FAILED → reconciled → PUBLISHED
# ═══════════════════════════════════════════════════════════════════
def test_H_lifecycle_transient_failure_then_reconciled():
    async def run():
        db = _fresh_db()
        await _wipe(db)
        from services.prediction_publication_service import (
            PredictionPublicationService,
        )
        pub = PredictionPublicationService(db)
        await pub.ensure_indices()

        p = _real_candidate("sess_a_H_transient")
        await db.picks.update_one(
            {"id": p["id"]},
            {"$set": {**p, "off_board": False, "no_bet": False}},
            upsert=True,
        )

        # Force a transient failure by monkey-patching `publish` to raise
        # once.  publish_batch's per-candidate try/except handles it and
        # marks the pick FAILED.
        original_publish = pub.publish
        _calls = {"n": 0}

        async def flaky_publish(cand, **kw):
            _calls["n"] += 1
            if _calls["n"] == 1:
                raise RuntimeError("simulated transient DB error")
            return await original_publish(cand, **kw)

        pub.publish = flaky_publish   # type: ignore[assignment]
        summary_1 = await pub.publish_batch(
            [p], dual_write=False,
            publication_source="sess_a_h_test",
        )
        assert summary_1["publication_failed"] == 1
        stored_1 = await db.picks.find_one({"id": p["id"]},
                                              projection={"_id": 0})
        assert stored_1["publication_state"] == "FAILED"
        assert stored_1.get("publication_last_error") is not None

        # Age the last_state_at so the reconciler picks it up.
        older = (datetime.now(timezone.utc)
                 - timedelta(minutes=30)).isoformat().replace(
                     "+00:00", "Z")
        await db.picks.update_one(
            {"id": p["id"]},
            {"$set": {"publication_last_state_at": older}},
        )
        # Restore publish to non-flaky.
        pub.publish = original_publish
        # Run reconciler.
        from services.publication_reconciliation import (
            reconcile_stuck_publications,
        )
        rec = await reconcile_stuck_publications(
            db, max_age_minutes=5, limit=10,
        )
        assert rec["retried"] == 1
        assert rec["published"] >= 1
        stored_2 = await db.picks.find_one({"id": p["id"]},
                                              projection={"_id": 0})
        assert stored_2["publication_state"] == "PUBLISHED"
        await _wipe(db)
    _run(run())


# ═══════════════════════════════════════════════════════════════════
# I. Max-retry bound — permanent FAILED → REJECTED, no infinite retry
# ═══════════════════════════════════════════════════════════════════
def test_I_max_retry_bound_no_infinite_retry():
    async def run():
        db = _fresh_db()
        await _wipe(db)
        from services.publication_reconciliation import (
            reconcile_stuck_publications,
        )
        from services.canonical_publication_boundary import (
            MAX_PUBLICATION_ATTEMPTS,
        )
        # Seed a pick already at MAX_PUBLICATION_ATTEMPTS in FAILED.
        older = (datetime.now(timezone.utc)
                 - timedelta(minutes=30)).isoformat().replace(
                     "+00:00", "Z")
        await db.picks.insert_one({
            "id":                        "sess_a_I_exhausted",
            "sport":                     "Soccer",
            "market":                    "Team X ML",
            "publication_state":         "FAILED",
            "publication_last_state_at": older,
            "publication_attempts":      MAX_PUBLICATION_ATTEMPTS,
            "publication_source":        "sess_a_i_test",
            "off_board":                 False,
            "no_bet":                    False,
        })
        rec = await reconcile_stuck_publications(
            db, max_age_minutes=5, limit=10,
        )
        assert rec["exhausted"] == 1
        assert rec["retried"] == 0
        stored = await db.picks.find_one({"id": "sess_a_I_exhausted"},
                                            projection={"_id": 0})
        assert stored["publication_state"] == "REJECTED"
        assert "MAX_ATTEMPTS_EXCEEDED" in (
            stored.get("publication_rejection_reasons") or [])
        assert stored["off_board"] is True
        assert stored["no_bet"] is True
        # Second reconciler pass touches nothing (permanent).
        rec2 = await reconcile_stuck_publications(
            db, max_age_minutes=5, limit=10,
        )
        assert rec2["retried"] == 0
        assert rec2["exhausted"] == 0
        await _wipe(db)
    _run(run())


# ═══════════════════════════════════════════════════════════════════
# J. Producer health telemetry counters
# ═══════════════════════════════════════════════════════════════════
def test_J_producer_health_counters():
    async def run():
        db = _fresh_db()
        await _wipe(db)
        from services.prediction_publication_service import (
            PredictionPublicationService,
        )
        from services import producer_health as ph
        pub = PredictionPublicationService(db)
        await pub.ensure_indices()

        # 2 good + 1 rejected + 1 (with no id) → 2 published, 2 rejected.
        good1 = _real_candidate("sess_a_J_ok1")
        good2 = _real_candidate("sess_a_J_ok2")
        synth = _real_candidate("sess_a_J_synth",
                                  odds_source="synthetic",
                                  book_odds=-120)
        no_id = _real_candidate("sess_a_J_no_id")
        no_id.pop("id")
        # Seed picks doc.
        for p in (good1, good2, synth):
            await db.picks.update_one(
                {"id": p["id"]},
                {"$set": {**p, "off_board": False, "no_bet": False}},
                upsert=True,
            )
        summary = await pub.publish_batch(
            [good1, synth, good2, no_id],
            dual_write=False,
            publication_source="sess_a_j_producer",
        )
        assert summary["new_snapshots"] == 2
        assert summary["boundary_rejected"] == 2

        rows = await ph.summary(db)
        row = next((r for r in rows
                      if r.get("publication_source") == "sess_a_j_producer"),
                    None)
        assert row is not None
        assert int(row["picks_generated"])  == 4
        assert int(row["picks_published"])  == 2
        assert int(row["picks_rejected"])   == 2
        assert row.get("last_success_at")   is not None
        assert row.get("last_rejection_at") is not None
        assert row.get("last_batch", {}).get("attempted") == 4
        counts = row.get("rejection_reason_counts") or {}
        assert counts.get("SYNTHETIC_BOOK_ODDS", 0) >= 1
        assert counts.get("MISSING_PICK_ID", 0) >= 1
        await _wipe(db)
    _run(run())


# ═══════════════════════════════════════════════════════════════════
# K. Admin lifecycle endpoint — read-only, no secrets exposed
# ═══════════════════════════════════════════════════════════════════
def test_K_admin_lifecycle_endpoint():
    async def run():
        db = _fresh_db()
        await _wipe(db)
        from services.prediction_publication_service import (
            PredictionPublicationService,
        )
        pub = PredictionPublicationService(db)
        await pub.ensure_indices()
        good = _real_candidate("sess_a_K_ok")
        synth = _real_candidate("sess_a_K_synth",
                                  odds_source="model_derived",
                                  book_odds=-150)
        await db.picks.update_one(
            {"id": good["id"]},
            {"$set": {**good, "off_board": False, "no_bet": False}},
            upsert=True,
        )
        await db.picks.update_one(
            {"id": synth["id"]}, {"$set": synth}, upsert=True,
        )
        await pub.publish_batch(
            [good, synth], dual_write=False,
            publication_source="sess_a_k_test",
        )

        # Test the endpoint LOGIC directly (avoid needing an admin token
        # or a running FastAPI server).  It returns a JSON-serialisable
        # dict — verify the shape + safety guarantees.
        from routes.publication_lifecycle_routes import (
            publication_lifecycle,
        )
        # Fake admin (dependency bypass).
        class _AdminStub: id = "test-admin"
        body = await publication_lifecycle(user=_AdminStub(),  # type: ignore
                                              recent_limit=20)
        assert body["ok"] is True
        c = body["counts"]
        assert c["published"] >= 1
        assert c["rejected"]  >= 1
        # Rejection reason counts include the synthetic reason.
        rr = body["rejection_reason_counts"]
        assert rr.get("SYNTHETIC_BOOK_ODDS", 0) >= 1
        # Producer health surfaces the producer we just wrote.
        srcs = {p["publication_source"]
                 for p in body["producer_health"]}
        assert "sess_a_k_test" in srcs
        # Safety: recent_failures / producer_health MUST NOT contain any
        # provider secrets or raw provider payloads.
        payload_str = str(body).lower()
        for banned in ("api_key", "apikey", "authorization",
                        "bearer ", "password", "secret",
                        "the_odds_api_key"):
            assert banned not in payload_str, \
                f"admin lifecycle payload leaks {banned!r}"
        await _wipe(db)
    _run(run())


# ═══════════════════════════════════════════════════════════════════
# L. NO_CURRENT_EVENTS distinguished from broken producer
# ═══════════════════════════════════════════════════════════════════
def test_L_no_current_events_vs_broken():
    async def run():
        db = _fresh_db()
        await _wipe(db)
        from services import producer_health as ph
        # Producer ran with 0 events — record as attempted=0.
        await ph.record_batch(
            db, publication_source="sess_a_L_producer",
            attempted=0, published=0, rejected=0, failed=0,
        )
        rows = await ph.summary(db)
        row = next((r for r in rows
                      if r.get("publication_source") == "sess_a_L_producer"),
                    None)
        assert row is not None
        assert row["last_no_events_at"] is not None
        assert row["last_failure_at"]   is None
        # Second call: real failure.
        await ph.record_batch(
            db, publication_source="sess_a_L_producer",
            attempted=1, published=0, rejected=0, failed=1,
            error_message="simulated provider error",
        )
        rows2 = await ph.summary(db)
        row2 = next((r for r in rows2
                       if r.get("publication_source") == "sess_a_L_producer"),
                     None)
        assert row2["last_failure_at"] is not None
        assert row2["last_batch"].get("error_message") == \
            "simulated provider error"
        await _wipe(db)
    _run(run())


# ═══════════════════════════════════════════════════════════════════
# M. One real active producer path flows through the boundary
# ═══════════════════════════════════════════════════════════════════
def test_M_real_producer_publish_upserted_picks_flows_through_boundary():
    async def run():
        db = _fresh_db()
        await _wipe(db)
        from services.publication_helpers import publish_upserted_picks

        p = _real_candidate("sess_a_M_helper")
        # publication_helpers requires the pick to already exist in
        # db.picks — the helper is post-upsert.
        await db.picks.update_one(
            {"id": p["id"]},
            {"$set": {**p, "off_board": False, "no_bet": False}},
            upsert=True,
        )
        out = await publish_upserted_picks(
            db, [p], publication_source="sess_a_helper_producer",
            caller_label="session_a_helper_test",
        )
        # Real active producer path succeeded.
        assert out.get("new_snapshots", 0) + out.get(
            "existing_snapshots", 0) == 1
        stored = await db.picks.find_one({"id": p["id"]},
                                            projection={"_id": 0})
        assert stored["publication_state"] == "PUBLISHED"
        await _wipe(db)
    _run(run())


# ═══════════════════════════════════════════════════════════════════
# Purge stubs — the deprecated synthetic-odds helpers raise on use
# ═══════════════════════════════════════════════════════════════════
def test_N_purged_synthetic_odds_stubs_raise():
    from services.espn_soccer_fixtures import _prob_to_american \
        as _prob_to_american_espn
    from services.mls_direct_inject import _american \
        as _american_mls
    from services.soccer_prop_inject import _american \
        as _american_spi
    for fn, arg in (
        (_prob_to_american_espn, 0.5),
        (_american_mls, 0.5),
        (_american_spi, 0.5),
    ):
        try:
            fn(arg)
        except NotImplementedError:
            continue
        raise AssertionError(
            f"{fn.__name__} MUST raise NotImplementedError after "
            "Session A synthetic-odds purge")
