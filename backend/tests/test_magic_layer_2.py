"""Magic Layer 2.0 — evidence-convergence tests.

Deterministic (mocks or real DB with `magic_test_` prefix).  Covers:
  A. Contract shape (Availability enum, EvidenceItem provenance)
  B. Exact-threshold hit rate + quantiles
  C. Exact-threshold PROVISIONAL id → UNAVAILABLE (safety gate)
  D. Model↔market convergence 5 states
  E. Contradiction detection
  F. Magic Score aggregation + tier
  G. Composite NBA (PRA) same-event rule
  H. Soccer adapter — goalscorer, creator, dual-threat archetypes
  I. Tennis adapter — favorite / underdog / surface-sensitive
  J. Magic Score never emitted for PROVISIONAL player-market
  K. Missing evidence stays UNAVAILABLE, not zero
  L. Lock Score / model / simulator outputs are UNCHANGED
"""
from __future__ import annotations

import asyncio
import os
import pytest

from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from motor.motor_asyncio import AsyncIOMotorClient


def _db():
    return AsyncIOMotorClient(os.environ["MONGO_URL"])[
        os.environ.get("DB_NAME", "lockscore_db")]


def _run(c): return asyncio.run(c)


async def _wipe(db):
    await db.player_game_actuals.delete_many(
        {"canonical_player_id": {"$regex": "^magic_test_"}})
    await db.tennis_players.delete_many(
        {"$or": [
            {"name_norm": {"$regex": "^magic_test_"}},
            {"name_norm": {"$regex": "^magictest"}},
        ]})
    await db.soccer_player_form.delete_many(
        {"team": "MagicTestTeam"})


# ══════════════════════════════════════════════════════════════════
# A. Contract shape
# ══════════════════════════════════════════════════════════════════
def test_A_evidence_item_provenance_shape():
    from services.magic.contract import (
        EvidenceItem, EvidenceType, Availability,
    )
    e = EvidenceItem(
        evidence_type=EvidenceType.MODEL_PROBABILITY,
        availability=Availability.AVAILABLE,
        sport="MLB", value=0.62, sample_size=20,
        source="test", source_class="authoritative",
    )
    d = e.to_dict()
    assert d["evidence_type"] == "MODEL_PROBABILITY"
    assert d["availability"]  == "AVAILABLE"
    assert d["value"] == 0.62
    assert d["source"] == "test"
    assert d["source_class"] == "authoritative"
    assert d["timestamp"]  # auto-populated


# ══════════════════════════════════════════════════════════════════
# B/C. Exact-threshold hit rate + safety
# ══════════════════════════════════════════════════════════════════
def test_B_exact_threshold_hit_rate_and_quantiles():
    async def run():
        db = _db(); await _wipe(db)
        # Seed 20 events with hits = [0,0,1,1,2,2,3,3,4,4] * 2
        # 12/20 games with hits > 1.5 → 60% rate at threshold 1.5.
        vals = [0,0,1,1,2,2,3,3,4,4] * 2
        for i,v in enumerate(vals):
            await db.player_game_actuals.insert_one({
                "canonical_player_id": "magic_test_p1",
                "sport": "MLB", "event_time": f"2026-06-{i+1:02d}T00:00:00Z",
                "event_id": f"magic_test_evt_{i:03d}",
                "actuals": {"hits": v},
            })
        from services.magic.exact_threshold import (
            compute_exact_threshold_evidence,
        )
        items = await compute_exact_threshold_evidence(
            db, canonical_player_id="magic_test_p1",
            identity_class="AUTHORITATIVE",
            stat_key="hits", threshold=1.5, direction="over",
            sport="MLB", windows=("last_10", "season"),
        )
        last10 = next(i for i in items if i.time_window == "last_10")
        season = next(i for i in items if i.time_window == "season")
        # last-10 subset expected: [4,4,3,3,4,4,3,3,2,2] → 6/10 > 1.5 = 60%
        # (rows returned newest-first; we seeded oldest first, so newest is v=4).
        assert last10.value == 0.6
        assert last10.sample_size == 10
        assert last10.availability.value == "AVAILABLE"
        assert last10.provenance["median"] is not None
        assert season.sample_size == 20
        await _wipe(db)
    _run(run())


def test_C_provisional_identity_blocks_history():
    async def run():
        db = _db(); await _wipe(db)
        await db.player_game_actuals.insert_one({
            "canonical_player_id": "magic_test_p_prov", "sport": "MLB",
            "event_time": "2026-06-01T00:00:00Z",
            "actuals": {"hits": 3},
        })
        from services.magic.exact_threshold import (
            compute_exact_threshold_evidence,
        )
        items = await compute_exact_threshold_evidence(
            db, canonical_player_id="magic_test_p_prov",
            identity_class="PROVISIONAL",
            stat_key="hits", threshold=1.5,
        )
        assert len(items) == 1
        assert items[0].availability.value == "UNAVAILABLE"
        assert "identity_class" in items[0].notes
        await _wipe(db)
    _run(run())


# ══════════════════════════════════════════════════════════════════
# D. Model↔market convergence states
# ══════════════════════════════════════════════════════════════════
def test_D_model_market_convergence_states():
    from services.magic.model_market import (
        evaluate_model_market_convergence, ModelMarketState,
    )
    # -140 → 58.3% market prob.
    # Strong agreement: mp = 0.58.
    r1 = evaluate_model_market_convergence(
        model_probability=0.58, book_odds=-140)
    assert r1["state"] == ModelMarketState.MODEL_MARKET_STRONG_AGREEMENT.value

    # Model edge, market neutral: mp = 0.66, market ~ 0.58 → +8 pts
    r2 = evaluate_model_market_convergence(
        model_probability=0.66, book_odds=-140)
    assert r2["state"] == ModelMarketState.MODEL_EDGE_MARKET_NEUTRAL.value

    # Market stronger than model: mp = 0.50, market = 0.58 → -8 pts
    r3 = evaluate_model_market_convergence(
        model_probability=0.50, book_odds=-140)
    assert r3["state"] == ModelMarketState.MARKET_STRONGER_THAN_MODEL.value

    # Disagreement: mp = 0.90, market = 0.58 → +32 pts
    r4 = evaluate_model_market_convergence(
        model_probability=0.90, book_odds=-140)
    assert r4["state"] == ModelMarketState.MODEL_MARKET_DISAGREEMENT.value

    # Insufficient market: no_real_book_line
    r5 = evaluate_model_market_convergence(
        model_probability=0.60, book_odds=None,
        no_real_book_line=True)
    assert r5["state"] == ModelMarketState.INSUFFICIENT_MARKET_DATA.value

    # Agreement (moderate): mp=0.62, market=0.583 → +3.7 pts
    r6 = evaluate_model_market_convergence(
        model_probability=0.62, book_odds=-140)
    assert r6["state"] == ModelMarketState.MODEL_MARKET_AGREEMENT.value


# ══════════════════════════════════════════════════════════════════
# E. Contradiction detection
# ══════════════════════════════════════════════════════════════════
def test_E_contradiction_detection():
    from services.magic.contract import (
        EvidenceItem, EvidenceType, Availability,
    )
    from services.magic.contradictions import (
        detect_contradictions, RiskFlag,
    )
    hist = EvidenceItem(
        evidence_type=EvidenceType.HISTORICAL_EXACT_THRESHOLD,
        availability=Availability.AVAILABLE,
        sport="NBA", value=0.75, sample_size=20, direction="positive",
    )
    flags = detect_contradictions(
        evidence=[hist],
        identity_class="MAPPED",
        starter_status="BENCH",
        role_recently_reduced=True,
        line_movement_pts=-3.0,
        model_probability=0.60,
        goals_over_xg_ratio=1.5,
        injury_probability=0.25,
    )
    assert RiskFlag.HISTORICAL_STRONG_BUT_ROLE_REDUCED.value in flags
    assert RiskFlag.HISTORICAL_STRONG_BUT_NOT_STARTER.value in flags
    assert RiskFlag.MODEL_STRONG_BUT_ADVERSE_LINE_MOVEMENT.value in flags
    assert RiskFlag.FINISHING_UNSUPPORTED_BY_SHOT_QUALITY.value in flags
    assert RiskFlag.INJURY_UNCERTAINTY.value in flags
    # No IDENTITY_PROVISIONAL because MAPPED.
    assert RiskFlag.IDENTITY_PROVISIONAL.value not in flags


# ══════════════════════════════════════════════════════════════════
# F/J. Magic Score + PROVISIONAL blockade
# ══════════════════════════════════════════════════════════════════
def test_F_magic_score_and_tier():
    from services.magic.contract import (
        EvidenceItem, EvidenceType, Availability, MagicOutput, MagicTier,
    )
    from services.magic.magic_score import compute_magic_score
    out = MagicOutput(pick_id="magic_test_out1", sport="MLB",
                        canonical_player_id="magic_test_p1",
                        identity_class="MAPPED")
    out.add(EvidenceItem(
        evidence_type=EvidenceType.HISTORICAL_EXACT_THRESHOLD,
        availability=Availability.AVAILABLE, sport="MLB",
        value=0.75, direction="positive", sample_size=20,
    ))
    out.add(EvidenceItem(
        evidence_type=EvidenceType.MODEL_PROBABILITY,
        availability=Availability.AVAILABLE, sport="MLB",
        value=0.62, direction="positive",
    ))
    out.add(EvidenceItem(
        evidence_type=EvidenceType.SPORTSBOOK_CONSENSUS,
        availability=Availability.AVAILABLE, sport="MLB",
        value=0.58, direction="positive",
    ))
    compute_magic_score(out, identity_class="MAPPED")
    assert out.magic_score_available is True
    assert out.magic_score is not None and out.magic_score > 60
    assert out.magic_tier in (MagicTier.ALIGNED_STRONG,
                                 MagicTier.ALIGNED,
                                 MagicTier.NEUTRAL)
    assert out.strongest_positive is not None


def test_J_provisional_player_market_gets_no_score():
    from services.magic.contract import (
        EvidenceItem, EvidenceType, Availability, MagicOutput, MagicTier,
    )
    from services.magic.magic_score import compute_magic_score
    out = MagicOutput(pick_id="magic_test_prov", sport="Tennis",
                        canonical_player_id="fallback:xxxx",
                        identity_class="PROVISIONAL")
    out.add(EvidenceItem(
        evidence_type=EvidenceType.MODEL_PROBABILITY,
        availability=Availability.AVAILABLE, sport="Tennis", value=0.65,
        direction="positive",
    ))
    compute_magic_score(out, identity_class="PROVISIONAL")
    assert out.magic_score is None
    assert out.magic_tier == MagicTier.INSUFFICIENT_EVIDENCE
    assert out.magic_score_available is False


# ══════════════════════════════════════════════════════════════════
# G. NBA composite same-event rule
# ══════════════════════════════════════════════════════════════════
def test_G_nba_composite_same_event_rule():
    async def run():
        db = _db(); await _wipe(db)
        # Two events — one with all parts, one missing rebounds.
        await db.player_game_actuals.insert_one({
            "canonical_player_id": "magic_test_nba1", "sport": "NBA",
            "event_time": "2026-06-01T00:00:00Z",
            "event_id": "magic_test_nba_evt_1",
            "actuals": {"points": 30, "rebounds": 5, "assists": 5},
        })
        await db.player_game_actuals.insert_one({
            "canonical_player_id": "magic_test_nba1", "sport": "NBA",
            "event_time": "2026-06-02T00:00:00Z",
            "event_id": "magic_test_nba_evt_2",
            "actuals": {"points": 25, "assists": 5},  # missing rebounds
        })
        from services.magic.adapters.playerprop import (
            _composite_evidence,
        )
        # PRA @ threshold 39.5 → 30+5+5=40 hit; missing-rebounds row
        # is excluded from the sample (never treated as 0).
        item = await _composite_evidence(
            db, canonical_player_id="magic_test_nba1",
            identity_class="MAPPED",
            parts=("points","rebounds","assists"),
            threshold=39.5, market="PRA Over 39.5",
            selection="Over",
        )
        assert item.sample_size == 1  # missing row EXCLUDED, not zero-filled
        assert item.value == 1.0     # 1/1 hit
        assert "same_event_rule" in item.provenance
        assert item.provenance["same_event_rule"] is True
        await _wipe(db)
    _run(run())


# ══════════════════════════════════════════════════════════════════
# H. Soccer adapter — 3 archetypes
# ══════════════════════════════════════════════════════════════════
def test_H_soccer_adapter_three_archetypes():
    async def run():
        db = _db(); await _wipe(db)
        # Seed 3 players with distinct archetypes.
        await db.soccer_player_form.insert_many([
            # GOAL_SCORER: high g90, low ast
            {"name_canonical": "magictest scorer x", "league": "EPL",
             "goals_per_90": 0.85, "npxg_per_90": 0.75, "assists": 3,
             "key_passes": 8, "games": 30, "minutes": 2700,
             "form_score": 82, "form_label": "HOT",
             "goals_over_xg": 1.15, "position": "FW",
             "shots_per_90": 3.2, "goals": 25, "shots": 96,
             "season": "2025-2026", "source": "test",
             "team": "MagicTestTeam", "player_name": "MagicTest Scorer X"},
            # CREATOR: high ast+kp, low g90
            {"name_canonical": "magictest creator y", "league": "EPL",
             "goals_per_90": 0.12, "npxg_per_90": 0.10, "assists": 12,
             "key_passes": 55, "games": 32, "minutes": 2880,
             "form_score": 74, "form_label": "STRONG",
             "goals_over_xg": 1.20, "position": "AM",
             "shots_per_90": 1.5, "goals": 4, "shots": 48,
             "season": "2025-2026", "source": "test",
             "team": "MagicTestTeam", "player_name": "MagicTest Creator Y"},
            # DUAL_THREAT: high both
            {"name_canonical": "magictest dual z", "league": "EPL",
             "goals_per_90": 0.50, "npxg_per_90": 0.45, "assists": 10,
             "key_passes": 42, "games": 32, "minutes": 2880,
             "form_score": 90, "form_label": "ELITE",
             "goals_over_xg": 1.11, "position": "AM",
             "shots_per_90": 2.8, "goals": 16, "shots": 90,
             "season": "2025-2026", "source": "test",
             "team": "MagicTestTeam", "player_name": "MagicTest Dual Z"},
        ])
        from services.magic.adapters.soccer import build_soccer_evidence
        # Goalscorer pick
        out_s = await build_soccer_evidence(db, {
            "id": "magic_test_soccer_1", "sport": "Soccer",
            "league": "EPL", "market": "MagicTest Scorer X Anytime Goal Scorer",
            "player_name": "MagicTest Scorer X",
            "canonical_player_id": "cpid_test_scorer",
            "identity_class": "MAPPED",
            "model_probability": 0.42, "book_odds": +160,
            "no_real_book_line": False, "line": 0.5,
            "selection": "MagicTest Scorer X",
        })
        # Creator pick
        out_c = await build_soccer_evidence(db, {
            "id": "magic_test_soccer_2", "sport": "Soccer",
            "league": "EPL", "market": "MagicTest Creator Y To Score or Assist",
            "player_name": "MagicTest Creator Y",
            "canonical_player_id": "cpid_test_creator",
            "identity_class": "MAPPED",
            "model_probability": 0.55, "book_odds": -110,
            "no_real_book_line": False, "line": 0.5,
        })
        # Dual-threat pick
        out_d = await build_soccer_evidence(db, {
            "id": "magic_test_soccer_3", "sport": "Soccer",
            "league": "EPL", "market": "MagicTest Dual Z To Score or Assist",
            "player_name": "MagicTest Dual Z",
            "canonical_player_id": "cpid_test_dual",
            "identity_class": "MAPPED",
            "model_probability": 0.72, "book_odds": -140,
            "no_real_book_line": False, "line": 0.5,
        })
        # Every archetype produced at least model + form + market evidence.
        for out, arch in ((out_s, "GOAL_SCORER"), (out_c, "CREATOR"),
                             (out_d, "DUAL_THREAT")):
            types = {e.evidence_type.value for e in out.evidence}
            assert "RECENT_FORM" in types
            assert "MODEL_PROBABILITY" in types
            assert "SPORTSBOOK_CONSENSUS" in types
            # Archetype is captured in the form-evidence provenance.
            form_ev = next(
                e for e in out.evidence
                if e.evidence_type.value == "RECENT_FORM")
            assert form_ev.provenance["archetype"] == arch, (
                f"expected {arch}, got {form_ev.provenance['archetype']}")
            # Magic score emitted (MAPPED identity).
            assert out.magic_score is not None
            assert out.magic_score_available is True
        await _wipe(db)
    _run(run())


# ══════════════════════════════════════════════════════════════════
# I. Tennis adapter — favorite / underdog / surface
# ══════════════════════════════════════════════════════════════════
def test_I_tennis_adapter_favorite_underdog_surface():
    async def run():
        db = _db(); await _wipe(db)
        await db.tennis_players.insert_many([
            {"name": "MagicTest Fav", "name_norm": "magictest fav",
             "elo_overall": 1900, "elo_clay": 1950, "elo_hard": 1850,
             "elo_grass": 1800},
            {"name": "MagicTest Dog", "name_norm": "magictest dog",
             "elo_overall": 1400, "elo_clay": 1350, "elo_hard": 1420,
             "elo_grass": 1400},
        ])
        from services.magic.adapters.tennis import build_tennis_evidence
        # Favorite on clay
        out_fav = await build_tennis_evidence(db, {
            "id": "magic_test_tennis_1", "sport": "Tennis",
            "league": "ATP Roland Garros",  # clay
            "market": "MagicTest Fav Moneyline",
            "player_name": "MagicTest Fav",
            "opponent_team": "MagicTest Dog",
            "canonical_player_id": "tp:magic_test_fav",
            "identity_class": "MAPPED",
            "model_probability": 0.75, "book_odds": -300,
        })
        types = {e.evidence_type.value for e in out_fav.evidence}
        assert "SURFACE_CONTEXT" in types
        surf = next(e for e in out_fav.evidence
                     if e.evidence_type.value == "SURFACE_CONTEXT")
        assert surf.value == 1950   # clay ELO
        opp = next(e for e in out_fav.evidence
                     if e.evidence_type.value == "OPPONENT_STRENGTH")
        assert opp.value == 500    # 1900-1400
        assert opp.direction == "positive"
        assert out_fav.magic_score is not None
        assert out_fav.magic_score > 55  # favorite → strong

        # Underdog opposite side
        out_dog = await build_tennis_evidence(db, {
            "id": "magic_test_tennis_2", "sport": "Tennis",
            "league": "ATP Roland Garros",
            "market": "MagicTest Dog Moneyline",
            "player_name": "MagicTest Dog",
            "opponent_team": "MagicTest Fav",
            "canonical_player_id": "tp:magic_test_dog",
            "identity_class": "MAPPED",
            "model_probability": 0.25, "book_odds": +260,
        })
        opp_dog = next(e for e in out_dog.evidence
                         if e.evidence_type.value == "OPPONENT_STRENGTH")
        assert opp_dog.value == -500
        assert opp_dog.direction == "negative"
        assert out_dog.magic_score < out_fav.magic_score
        await _wipe(db)
    _run(run())


# ══════════════════════════════════════════════════════════════════
# K. Missing evidence stays UNAVAILABLE not zero
# ══════════════════════════════════════════════════════════════════
def test_K_missing_evidence_stays_unavailable_not_zero():
    async def run():
        db = _db()
        # Player with no history at all.
        from services.magic.exact_threshold import (
            compute_exact_threshold_evidence,
        )
        items = await compute_exact_threshold_evidence(
            db, canonical_player_id="magic_test_no_history",
            identity_class="MAPPED", stat_key="hits", threshold=1.5,
        )
        assert len(items) == 1
        assert items[0].availability.value == "UNAVAILABLE"
        assert items[0].value is None    # NOT 0.0
    _run(run())


# ══════════════════════════════════════════════════════════════════
# L. Lock Score / model / simulator outputs UNCHANGED
# ══════════════════════════════════════════════════════════════════
def test_L_lock_score_and_model_outputs_unchanged():
    """Magic modules do NOT import from sports_engine /
    lock_score_performance and never mutate their outputs.  This is
    a static contract check."""
    import services.magic.contract as mc
    import services.magic.exact_threshold as met
    import services.magic.model_market as mmm
    import services.magic.contradictions as mcc
    import services.magic.magic_score as mms
    import services.magic.adapters.soccer as msa
    import services.magic.adapters.tennis as mta
    import services.magic.adapters.playerprop as mpa
    banned = ("sports_engine", "lock_score_performance",
                "sports_engine.py", "lock_score_v2")
    for mod in (mc, met, mmm, mcc, mms, msa, mta, mpa):
        src_file = getattr(mod, "__file__", None)
        assert src_file, f"module {mod} lacks __file__"
        with open(src_file) as f:
            text = f.read()
        for b in banned:
            assert b not in text, (
                f"{mod.__name__} references {b!r} — Magic must not "
                f"touch Lock Score")
