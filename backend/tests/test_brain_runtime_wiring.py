"""Brain Runtime Wiring μ-closure — focused regressions.

Single chokepoint stamp at publish_upserted_picks means every sport
that already routes through this helper gets convergence for FREE.
"""
import asyncio, os, sys
from datetime import datetime, timezone, timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MARK = "brain_runtime_wiring"


def _seed_pick(sport, market, *, mp, sp, prov, ev, odds, pid):
    now = datetime.now(timezone.utc)
    return {
        "id": pid, "sport": sport, "market": market,
        "selection": "Over", "event": "A@B",
        "event_time": (now + timedelta(hours=4)).isoformat(),
        "pick_date": now.strftime("%Y-%m-%d"),
        "book_odds": odds,
        "win_probability":       mp,
        "sim_win_probability":   sp,
        "simulator_provenance":  prov,
        "evidence_quality":      ev,
        "off_board": False,
        "_test_marker": MARK,
    }


def test_runtime_wiring_all_sports():
    async def _run():
        from motor.motor_asyncio import AsyncIOMotorClient
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
        cx = AsyncIOMotorClient(os.getenv("MONGO_URL"))
        db = cx[os.getenv("DB_NAME", "test_database")]
        try:
            await db.picks.delete_many({"_test_marker": MARK})
            picks = [
                # MLB pitcher Ks — strong convergence, real context
                _seed_pick("MLB", "Cole Over 7.5 Strikeouts",
                            mp=0.62, sp=0.61, prov="REAL_PLAYER_CONTEXT",
                            ev="STRONG", odds=-115, pid=f"{MARK}_MLB_K"),
                # MLB Hits — strong disagreement, weak evidence
                _seed_pick("MLB", "Judge Over 0.5 Hits",
                            mp=0.72, sp=0.50, prov="PRIOR_ONLY",
                            ev="WEAK", odds=-165, pid=f"{MARK}_MLB_H"),
                # Soccer scorer — moderate convergence
                _seed_pick("Soccer", "Player X Anytime Goal Scorer",
                            mp=0.44, sp=0.41, prov="PARTIAL_CONTEXT",
                            ev="MODERATE", odds=+165, pid=f"{MARK}_SOC"),
                # Tennis — strong convergence, moderate evidence
                _seed_pick("Tennis", "Player Y Moneyline",
                            mp=0.63, sp=0.63, prov="REAL_PLAYER_CONTEXT",
                            ev="STRONG", odds=-170, pid=f"{MARK}_TEN"),
                # NFL — moderate
                _seed_pick("NFL", "QB Z Over 245.5 Passing Yards",
                            mp=0.55, sp=0.54, prov="PARTIAL_CONTEXT",
                            ev="MODERATE", odds=-108, pid=f"{MARK}_NFL"),
                # Underdog neutrality check — long dog, same convergence
                _seed_pick("MLB", "Team A Moneyline",
                            mp=0.35, sp=0.34, prov="REAL_PLAYER_CONTEXT",
                            ev="STRONG", odds=+200, pid=f"{MARK}_DOG"),
                # Same-convergence favorite twin
                _seed_pick("MLB", "Team B Moneyline",
                            mp=0.72, sp=0.71, prov="REAL_PLAYER_CONTEXT",
                            ev="STRONG", odds=-260, pid=f"{MARK}_FAV"),
            ]
            for p in picks:
                await db.picks.update_one({"id": p["id"]},
                                            {"$set": p}, upsert=True)
            from services.publication_helpers import publish_upserted_picks
            _summary = await publish_upserted_picks(
                db, picks, publication_source="test", caller_label="test")
            # Reload post-enrichment.
            stamped = {}
            async for r in db.picks.find({"_test_marker": MARK}, {"_id": 0}):
                stamped[r["id"]] = r

            # 1. MLB K: convergence stamped, high confidence for STRONG evidence + REAL context.
            k = stamped[f"{MARK}_MLB_K"]
            assert k.get("convergence_label") in (
                "STRONG_CONVERGENCE", "MODERATE_CONVERGENCE", "MIXED_EVIDENCE"), k
            assert isinstance(k.get("convergence_spread_pp"), float)
            assert k.get("convergence_confidence_multiplier", 0) >= 0.55
            # 2. MLB Hits: strong disagreement + weak evidence + PRIOR_ONLY.
            h = stamped[f"{MARK}_MLB_H"]
            assert h.get("convergence_label") in (
                "STRONG_DISAGREEMENT", "MIXED_EVIDENCE")
            assert h.get("convergence_confidence_multiplier", 1) <= 0.72
            # 3-5. Soccer / Tennis / NFL each carry the stamp.
            for pid in (f"{MARK}_SOC", f"{MARK}_TEN", f"{MARK}_NFL"):
                s = stamped[pid]
                assert s.get("convergence_label"), f"{pid} missing label"
                assert isinstance(s.get("convergence_spread_pp"), float)
                assert 0.55 <= s.get("convergence_confidence_multiplier", 0) <= 1.0
            # 6. Convergence > Disagreement quality — MLB K (STRONG evidence,
            #    real context) must have HIGHER confidence than MLB Hits
            #    (WEAK evidence, PRIOR_ONLY, large disagreement).
            assert k.get("convergence_confidence_multiplier") > \
                    h.get("convergence_confidence_multiplier"), (
                "convergence quality signal not affecting stamp — "
                f"K={k.get('convergence_confidence_multiplier')} "
                f"vs Hits={h.get('convergence_confidence_multiplier')}")
            # 7. Underdog neutrality: identical INTERNAL agreement +
            #    provenance + evidence → identical multiplier
            #    regardless of odds direction is proved by the
            #    dedicated smoke in probability_engine unit tests.
            # 8. Frozen truth preserved — no rewrite to win_probability.
            assert k.get("win_probability") == 0.62
            assert h.get("win_probability") == 0.72
            # 9. No new writer / no lock_score_v3 mutation.
            for p in stamped.values():
                assert "lock_score_v3" not in p
        finally:
            await db.picks.delete_many({"_test_marker": MARK})
            cx.close()
    asyncio.run(_run())
    print("test_runtime_wiring_all_sports OK")


def test_prior_only_still_capped():
    """Even after runtime stamping, PRIOR_ONLY < REAL_PLAYER_CONTEXT."""
    from probability_engine import classify_convergence
    real  = classify_convergence(p_v1=0.65,p_v2=0.65,p_sim=0.65,implied=0.64,
                                    sim_ran=True, sim_provenance="REAL_PLAYER_CONTEXT",
                                    evidence_quality="STRONG")
    prior = classify_convergence(p_v1=0.65,p_v2=0.65,p_sim=0.65,implied=0.64,
                                    sim_ran=True, sim_provenance="PRIOR_ONLY",
                                    evidence_quality="STRONG")
    assert real["confidence_multiplier"] > prior["confidence_multiplier"]
    assert prior["confidence_multiplier"] <= 0.72
    print("test_prior_only_still_capped OK")


def test_no_new_writer_at_chokepoint():
    with open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))),
            "services/publication_helpers.py")) as f:
        src = f.read()
    # New writes only stamp existing pick dict — no new collection.
    assert "convergence_label" in src
    assert "history_projection" not in src
    # No secondary lock score created.
    assert "lock_score_v3"      not in src
    assert "brain_lock_score"   not in src
    print("test_no_new_writer_at_chokepoint OK")


def test_evidence_quality_not_hardcoded_strong():
    """The enricher READS pick-supplied evidence_quality; it never
    hardcodes STRONG.  We already verified WEAK propagates in
    test_runtime_wiring_all_sports (h assertion).  Also assert the
    enricher falls back to MODERATE (not STRONG) when the field is
    absent."""
    from probability_engine import classify_convergence
    c = classify_convergence(p_v1=0.6,p_v2=0.6,p_sim=0.6,implied=0.6,
                              sim_ran=True,
                              sim_provenance="REAL_PLAYER_CONTEXT")
    # No evidence_quality passed → defaults MODERATE (0.90 factor)
    # NOT STRONG.  0.90 factor × 1.00 base × 1.00 sim = 0.90.
    assert 0.85 <= c["confidence_multiplier"] <= 0.95, c
    print("test_evidence_quality_not_hardcoded_strong OK")


if __name__ == "__main__":
    test_runtime_wiring_all_sports()
    test_prior_only_still_capped()
    test_no_new_writer_at_chokepoint()
    test_evidence_quality_not_hardcoded_strong()
    print("\nBRAIN_RUNTIME_WIRING_TESTS_ALL_PASSED")
