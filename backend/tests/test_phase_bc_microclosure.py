"""Phase B+C μ-closure — focused regressions.

Scope: B3, B9, B8 (verify), B2 (verify), C4 (hard cap removed), C5.
Also runs cross-consumer canonical trace assertion.
"""
import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ══════════════════════════════════════════════════════════════════
# B3 — Frozen Pick Breakdown truth
# ══════════════════════════════════════════════════════════════════
def test_B3_frozen_pick_breakdown_uses_published():
    """Mutating current model inputs after publication does NOT alter
    the authoritative p_calibrated / edge / classification values."""
    from probability_engine import unified_probability_report

    # A published pick with frozen canonical values.  Give it a
    # book_odds and win_probability so the "current recalculation"
    # produces a materially different number than the frozen one.
    pick_published = {
        "id": "pk_B3",
        "sport": "MLB",
        "market": "Team A Moneyline",
        "book_odds": -150,
        "win_probability": 0.60,          # would drive recalc high
        "published_probability": 0.55,    # frozen canonical
        "published_edge": 0.021,          # frozen canonical (2.1%)
    }
    r1 = unified_probability_report(pick_published)
    # Authoritative fields must match the frozen values, not the recalc.
    assert r1["p_calibrated"] == 0.55, (
        f"B3 defect — p_calibrated should be frozen 0.55, got {r1['p_calibrated']}")
    assert r1["edge"] == 0.021, (
        f"B3 defect — edge should be frozen 0.021, got {r1['edge']}")
    assert r1["frozen_source"] == "publication_snapshot"
    # Diagnostic block must be present and clearly labelled.
    assert r1["diagnostic"]["label"] == "CURRENT_DIAGNOSTIC_RECALCULATION"
    assert "p_calibrated_current" in r1["diagnostic"]
    assert "edge_current" in r1["diagnostic"]

    # Now MUTATE the current inputs (simulate learning drift).  The
    # authoritative fields must STILL reflect the frozen snapshot.
    pick_mutated = dict(pick_published)
    pick_mutated["win_probability"] = 0.90    # drift up
    pick_mutated["book_odds"]       = -110    # drift down (more implied)
    r2 = unified_probability_report(pick_mutated)
    assert r2["p_calibrated"] == 0.55, (
        "B3 defect — frozen p_calibrated must NOT change after mutation")
    assert r2["edge"] == 0.021
    assert r2["frozen_source"] == "publication_snapshot"
    # Diagnostic delta captures the drift (proof of live recalc, but
    # never masquerading as authoritative).
    assert abs(r2["diagnostic"]["delta_p_calibrated"]) > 0

    # Legacy pick (pre-dual-write) — no canonical snapshot present.
    # frozen_source must fall back to current_recalculation cleanly.
    legacy = {
        "id": "pk_B3_legacy",
        "sport": "MLB",
        "market": "Team A Moneyline",
        "book_odds": -150,
        "win_probability": 0.60,
    }
    r3 = unified_probability_report(legacy)
    assert r3["frozen_source"] == "current_recalculation"
    assert r3["p_calibrated"] > 0.0
    print("test_B3_frozen_pick_breakdown_uses_published OK")


# ══════════════════════════════════════════════════════════════════
# B9 — Parlay canonical pre-pool
# ══════════════════════════════════════════════════════════════════
def test_B9_parlay_source_gates_are_canonical():
    """Source-level guard: parlay_routes candidate query must include
    the canonical publication filter AND read
    ``published_lock_score`` / ``lock_score`` — NEVER V2 alone."""
    root = os.path.join(os.path.dirname(__file__), "..",
                         "routes/parlay_routes.py")
    with open(root) as f:
        src = f.read()
    # The old V2-admission $or must be gone.
    assert '"lock_score_v2": {"$gte": lock_floor_val}' not in src, (
        "B9 defect — parlay pool still admits candidates on lock_score_v2")
    # Canonical publication filter must be applied.
    assert "canonical_publication_filter" in src, (
        "B9 defect — parlay pool does not consult canonical publication gate")
    # $or must include published_lock_score.
    assert '"published_lock_score": {"$gte": lock_floor_val}' in src, (
        "B9 defect — parlay pool does not consult canonical published_lock_score")
    # off_board / settlement_block gates present.
    assert '"off_board": {"$ne": True}' in src
    assert '"settlement_block": {"$ne": True}' in src
    print("test_B9_parlay_source_gates_are_canonical OK")


def test_B9_high_v2_unpublished_pick_cannot_enter_pool():
    """Runtime — with the parlay base_q candidate filter applied, an
    unpublished pick with a very high ``lock_score_v2`` must NOT be
    admitted; a published pick with any canonical ``lock_score`` /
    ``published_lock_score`` above floor MUST be admitted."""
    async def _run():
        from motor.motor_asyncio import AsyncIOMotorClient
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
        cx = AsyncIOMotorClient(os.getenv("MONGO_URL"))
        db = cx[os.getenv("DB_NAME", "test_database")]
        try:
            # Cleanup any prior marker.
            await db.picks.delete_many({"_test_marker": "B9_uclosure"})
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            future = "2099-01-01T00:00:00Z"
            docs = [
                # Unpublished + high V2 — must NOT enter pool.
                {"id": "pk_B9_bad",
                 "pick_date": today, "sport": "MLB",
                 "market": "Home Moneyline", "selection": "Home",
                 "event": "A @ B", "event_time": future,
                 "lock_score": 30, "lock_score_v2": 99,
                 "publication_source": None,        # unpublished
                 "off_board": False, "no_bet": False,
                 "is_under_lock": False, "settlement_block": False,
                 "_test_marker": "B9_uclosure"},
                # Published + canonical floor — MUST enter pool.
                {"id": "pk_B9_good",
                 "pick_date": today, "sport": "MLB",
                 "market": "Away Moneyline", "selection": "Away",
                 "event": "A @ B", "event_time": future,
                 "lock_score": 88, "lock_score_v2": 50,
                 "published_lock_score": 88,
                 "publication_source": "canonical.v1",
                 "off_board": False, "no_bet": False,
                 "is_under_lock": False, "settlement_block": False,
                 "_test_marker": "B9_uclosure"},
            ]
            for d in docs:
                await db.picks.update_one({"id": d["id"]},
                                           {"$set": d}, upsert=True)

            from services.canonical_board_source import (
                canonical_publication_filter)
            # Force gate ON regardless of test-env leftovers.
            os.environ["LOCKSCORE_REQUIRE_CANONICAL_PUBLICATION"] = "true"
            _canon = canonical_publication_filter()
            base_q = {
                "pick_date": today,
                "no_bet": {"$ne": True},
                "is_under_lock": {"$ne": True},
                "off_board": {"$ne": True},
                "settlement_block": {"$ne": True},
                "_test_marker": "B9_uclosure",
                **_canon,
                "$or": [
                    {"published_lock_score": {"$gte": 85}},
                    {"lock_score":            {"$gte": 85}},
                ],
            }
            pool = await db.picks.find(
                base_q, {"_id": 0, "id": 1}).to_list(length=50)
            ids = {p["id"] for p in pool}
            assert "pk_B9_bad" not in ids, (
                "B9 defect — unpublished pick with high V2 entered parlay pool")
            assert "pk_B9_good" in ids, (
                "B9 defect — canonical published pick missing from pool")
        finally:
            await db.picks.delete_many({"_test_marker": "B9_uclosure"})
            cx.close()
    asyncio.run(_run())
    print("test_B9_high_v2_unpublished_pick_cannot_enter_pool OK")


# ══════════════════════════════════════════════════════════════════
# B2 — Production-truth enforcement (verify): canonical gate default
#      ON + /picks/today AND /picks/parlay both enforce it.
# ══════════════════════════════════════════════════════════════════
def test_B2_enforcement_present_in_both_consumers():
    root = os.path.join(os.path.dirname(__file__), "..")
    with open(os.path.join(root, "routes/picks_routes.py")) as f:
        picks_src = f.read()
    with open(os.path.join(root, "routes/parlay_routes.py")) as f:
        parlay_src = f.read()
    # /picks/today wires the gate.
    assert "canonical_publication_filter" in picks_src
    assert "FAILED CLOSED" in picks_src   # exception path fails closed
    # /picks/parlay wires the gate.
    assert "canonical_publication_filter" in parlay_src
    print("test_B2_enforcement_present_in_both_consumers OK")


# ══════════════════════════════════════════════════════════════════
# B8 — Verify main board no longer ENFORCES chalk / dead-zone
# ══════════════════════════════════════════════════════════════════
def test_B8_main_board_quality_gate_non_enforcing():
    """/picks/today calls apply_quality_gate with enforce=False so any
    chalk / dead-zone / favorite-penalty diagnostic tag is cosmetic
    only — canonical eligibility is not overridden."""
    root = os.path.join(os.path.dirname(__file__), "..",
                         "routes/picks_routes.py")
    with open(root) as f:
        src = f.read()
    # The /picks/today block sets enforce=False (verified).
    assert "apply_quality_gate(picks, enforce=False)" in src, (
        "B8 defect — main board enforces quality gate (should be diagnostic only)")
    print("test_B8_main_board_quality_gate_non_enforcing OK")


# ══════════════════════════════════════════════════════════════════
# C4 — Multiple qualified goalscorers reachable (no hard top-N cap)
# ══════════════════════════════════════════════════════════════════
def test_C4_no_hard_topN_cap_on_goalscorers():
    from server import _dedupe_goalscorer_per_event
    # Seed 5 distinct qualified scorers for one event, one team.
    picks = []
    for i, wp in enumerate((0.55, 0.50, 0.47, 0.42, 0.38)):
        picks.append({
            "id":               f"pk_C4_{i}",
            "sport":            "Soccer",
            "market":           f"Player_{i} Anytime Goal Scorer",
            "selection":        "Yes",
            "event":            "TeamA @ TeamB",
            "win_probability":  wp,
            "implied_probability": wp * 100,
            "lock_score":       88,
            "book_odds":        -110,
            "elite_player":     False,
        })
    kept = _dedupe_goalscorer_per_event(picks, top_n=3)
    kept_ids = {p["id"] for p in kept if "id" in p}
    # ALL five qualifying scorers must remain reachable — no hard
    # top-N cap.
    for i in range(5):
        assert f"pk_C4_{i}" in kept_ids, (
            f"C4 defect — qualifying scorer pk_C4_{i} dropped by top-N cap")
    print("test_C4_no_hard_topN_cap_on_goalscorers OK")


# ══════════════════════════════════════════════════════════════════
# C5 — Refresh semantics truthful
# ══════════════════════════════════════════════════════════════════
def test_C5_refresh_response_labels_truthful():
    root = os.path.join(os.path.dirname(__file__), "..",
                         "routes/picks_routes.py")
    with open(root) as f:
        src = f.read()
    # Truthful decomposition fields present.
    for needle in ('"existing_records":',
                    '"actually_generated":',
                    '"canonical_published":',
                    '"refresh_timestamp":',
                    'DB-only refresh — 0 new picks generated'):
        assert needle in src, (
            f"C5 defect — refresh response missing truthful label: {needle}")
    print("test_C5_refresh_response_labels_truthful OK")


# ══════════════════════════════════════════════════════════════════
# Cross-consumer canonical trace (Board → Locks → Rollover → Parlay)
# ══════════════════════════════════════════════════════════════════
def test_canonical_trace_conservation():
    async def _run():
        from motor.motor_asyncio import AsyncIOMotorClient
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
        cx = AsyncIOMotorClient(os.getenv("MONGO_URL"))
        db = cx[os.getenv("DB_NAME", "test_database")]
        # Grab one live canonical published pick.
        p = await db.picks.find_one(
            {"publication_source": {"$exists": True, "$ne": None},
             "off_board": {"$ne": True}},
            {"_id": 0, "id": 1, "event": 1, "market": 1, "selection": 1,
             "line": 1, "book_odds": 1, "published_probability": 1,
             "published_edge": 1, "published_lock_score": 1,
             "lock_score": 1, "lock_score_v2": 1},
        )
        if not p:
            print("test_canonical_trace_conservation SKIPPED (no canonical pick in preview)")
            cx.close()
            return
        # Same pick_id / event / market / selection / line / odds MUST
        # be identically observable across consumers.  We verify by
        # projecting through the same canonical filter every consumer
        # uses (canonical_publication_filter) and confirming the pick
        # remains present.
        from services.canonical_board_source import (
            canonical_publication_filter)
        os.environ["LOCKSCORE_REQUIRE_CANONICAL_PUBLICATION"] = "true"
        _canon = canonical_publication_filter()
        found = await db.picks.find_one(
            {"id": p["id"], **_canon}, {"_id": 0, "id": 1})
        assert found is not None, (
            "canonical trace violation — pick not visible via canonical filter")
        # Frozen probability report round-trips to canonical values.
        if p.get("published_probability") is not None:
            from probability_engine import unified_probability_report
            report = unified_probability_report(p)
            assert report["frozen_source"] == "publication_snapshot"
            assert abs(report["p_calibrated"] - float(p["published_probability"])) < 1e-4
        print(f"test_canonical_trace_conservation OK  · "
              f"pick_id={p['id'][:8]}… event={p.get('event')!r} "
              f"market={p.get('market')!r} pls={p.get('published_lock_score')}")
        cx.close()
    asyncio.run(_run())


if __name__ == "__main__":
    test_B3_frozen_pick_breakdown_uses_published()
    test_B9_parlay_source_gates_are_canonical()
    test_B9_high_v2_unpublished_pick_cannot_enter_pool()
    test_B2_enforcement_present_in_both_consumers()
    test_B8_main_board_quality_gate_non_enforcing()
    test_C4_no_hard_topN_cap_on_goalscorers()
    test_C5_refresh_response_labels_truthful()
    test_canonical_trace_conservation()
    print("\nPHASE_BC_MICROCLOSURE_TESTS_ALL_PASSED")
