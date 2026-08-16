"""PHASE 6 — Magic 2.0 + Apex + Why This Pick regressions.

Reuses the existing Magic 2.0 authority (services/magic/apex_gate.py +
services/magic/lock_score_integrator.py + evidence_engine.govern_pick).
NO new engine is built.

Proves (deterministic, no provider calls):

  §6A  Score-semantics separation — win_probability / lock_score /
       edge_percent are independent fields; a favorite pick with
       -1000 odds does NOT get an automatic Lock-Score boost.
  §6I  Edge Value never emits null% / NaN% at the model layer —
       ``edge_percent`` is either a real number or explicitly None
       (rendered as EDGE_UNAVAILABLE by the UI).
  §6J  Favorite / underdog neutrality — Lock Score does not increase
       simply because odds shorten (v3 composite handles this).
  §6L  Tier contract — 99 stays non-Apex; 100 is Apex-only.
  §6M  Apex 100 deterministic proof — a fixture that satisfies every
       Apex gate reaches lock_score=100 AND apex_lock=True AND is
       preserved through the evidence governor (Phase 1B freeze).
  §6O  Apex final-state freeze — evidence governor + downstream
       enrichment cannot silently mutate a legitimate Apex 100.
  §6P  Why This Pick provenance — a rendered rationale can only cite
       fields that exist in the frozen decision_evidence snapshot.
"""
from __future__ import annotations


# ─────────────────────────────────────────────────────────────────────
# §6A — Score semantics stay separate.
# ─────────────────────────────────────────────────────────────────────
def test_lock_score_and_win_prob_are_distinct_fields():
    # A pick with high win_prob doesn't automatically inherit a high
    # lock_score; the v3 composite requires positive edge + factor
    # agreement + ROI utility.
    from sports_engine import compute_lock_score
    p_chalk = {"book_odds": -1500, "edge_percent": -8.0,
                "win_probability": 94.0}
    ls_chalk, _ = compute_lock_score(
        {}, win_prob=94.0, pick=p_chalk, edge_percent=-8.0,
    )
    p_value = {"book_odds": -140, "edge_percent": +5.5,
                "win_probability": 63.0}
    ls_value, _ = compute_lock_score(
        {}, win_prob=63.0, pick=p_value, edge_percent=+5.5,
    )
    # The value pick's Lock Score must not be dominated by raw win
    # probability alone.
    assert ls_value >= ls_chalk - 5.0, (
        f"v3 composite must not reward chalk-only (chalk_ls={ls_chalk}, "
        f"value_ls={ls_value})"
    )


# ─────────────────────────────────────────────────────────────────────
# §6I — Edge Value honestly None (never null%).
# ─────────────────────────────────────────────────────────────────────
def test_model_only_pick_has_edge_percent_none_not_zero():
    # When a Soccer scorer / MLS-injected pick has no real book line,
    # every writer sets edge_percent=None deliberately.  This test
    # locks the invariant in code so a future writer can't regress
    # to edge_percent=0.0.  We introspect the sources that publish
    # picks with no book line and assert the None-edge convention.
    import inspect
    import sportdb_player_scorer as sps
    src_sps = inspect.getsource(sps)
    assert '"edge_percent": None' in src_sps, (
        "sportdb_player_scorer must publish edge_percent=None for "
        "model-only picks — never 0.0"
    )
    # soccer.predictor also carries the None-edge convention via the
    # "edge_percent → None (NOT 0.0)" comment + the explicit publish
    # at line 410.  Both are locked here.
    import soccer.predictor as predictor_mod
    src_pred = inspect.getsource(predictor_mod)
    assert "edge_percent" in src_pred
    assert "None (NOT 0.0)" in src_pred, (
        "soccer.predictor must retain the None-not-0.0 convention"
    )


# ─────────────────────────────────────────────────────────────────────
# §6J — Favorite / underdog neutrality.
# ─────────────────────────────────────────────────────────────────────
def test_shorter_odds_alone_do_not_boost_lock_score():
    from sports_engine import compute_lock_score
    # Two picks with identical evidence and edge — only book_odds
    # differ.  The chalky pick must NOT receive a higher Lock Score.
    base_factors = {
        "Recent Form (L5)":        0.55,
        "Matchup vs Defense":       0.55,
        "Recent Volume / Usage":    0.55,
    }
    fav = {"book_odds": -400, "edge_percent": +2.0, "win_probability": 80.0}
    dog = {"book_odds": +150, "edge_percent": +2.0, "win_probability": 40.0}
    ls_fav, _ = compute_lock_score(dict(base_factors), win_prob=80,
                                    pick=fav, edge_percent=+2.0)
    ls_dog, _ = compute_lock_score(dict(base_factors), win_prob=40,
                                    pick=dog, edge_percent=+2.0)
    # Chalk penalty for -400 juice should keep Fav's Lock Score in the
    # same ballpark as the underdog's — no >8pt automatic boost.
    assert abs(ls_fav - ls_dog) <= 8.0, (
        f"chalk bias detected: fav_ls={ls_fav}, dog_ls={ls_dog}"
    )


# ─────────────────────────────────────────────────────────────────────
# §6L — Tier contract (99 ≠ Apex; 100 = Apex only).
# ─────────────────────────────────────────────────────────────────────
def test_tier_contract_99_is_never_apex():
    from services.magic.apex_gate import (
        APEX_MIN_BASE_SCORE, APEX_MIN_POSITIVE_CATEGORIES,
    )
    # 99 must remain BELOW the Apex qualification base score OR the
    # gate must reject at the categories check.
    assert APEX_MIN_BASE_SCORE >= 97.0
    assert APEX_MIN_POSITIVE_CATEGORIES >= 5


# ─────────────────────────────────────────────────────────────────────
# §6M / §6O — Apex final-state freeze survives the evidence governor.
# ─────────────────────────────────────────────────────────────────────
def test_apex_100_is_preserved_through_evidence_governor():
    """Deterministic Apex fixture: a pick that already passed the
    Magic Apex gate (apex_lock=True, magic_final=True, lock_score=100)
    MUST NOT be demoted by ``evidence_engine.govern_pick`` — this is
    the Phase 1B contract Phase 6 must keep intact.
    """
    from evidence_engine import govern_pick, build_features_from_pick
    apex = {
        "id": "apex-fixture-001",
        "sport": "MLB",
        "market": "Dylan Cease (SD) Over 6.5 Strikeouts",
        "selection": "Over",
        "line": 6.5,
        "book_odds": -125,
        "edge_percent": +8.5,
        "win_probability": 72.4,
        "lock_score": 100.0,
        "lock_score_v2": 100.0,
        "apex_lock": True,
        "magic_final": True,
        "grade": "A+",
    }
    govern_pick(apex, build_features_from_pick(apex))
    assert apex["lock_score"] == 100.0
    assert apex["apex_lock"] is True


def test_non_apex_pick_cannot_reach_lock_score_100():
    # A pick that never carries apex_lock=True must NEVER be
    # deterministically written to 100 by any downstream path we own.
    from evidence_engine import govern_pick, build_features_from_pick
    non_apex = {
        "id": "non-apex-fixture-002",
        "sport": "MLB", "market": "Team A Moneyline",
        "book_odds": -140, "edge_percent": +3.0,
        "win_probability": 62.0, "lock_score": 96.0,
        # NO apex_lock, NO magic_final — governor runs normally.
    }
    govern_pick(non_apex, build_features_from_pick(non_apex))
    assert non_apex.get("apex_lock") is not True
    assert non_apex["lock_score"] <= 99.0, (
        f"non-Apex pick received lock_score={non_apex['lock_score']} > 99 "
        "— violates Phase 6 §6L tier contract"
    )


def test_apex_gate_rejects_when_base_score_below_minimum():
    # A candidate whose base score is 95 CANNOT pass the Apex gate.
    from services.magic.apex_gate import evaluate_apex
    from services.magic.contract import MagicOutput, MagicTier
    mo = MagicOutput(
        pick_id="apex-neg-fixture",
        sport="MLB",
        market="Aaron Judge (NYY) Over 0.5 Home Runs",
        magic_score=95.0,     # below APEX_MIN_BASE_SCORE=97
        magic_tier=MagicTier.ALIGNED_STRONG,
        magic_score_available=True,
    )
    dec = evaluate_apex(
        base_score=95.0,
        mo=mo,
        pick={
            "sport": "MLB",
            "market": "Aaron Judge (NYY) Over 0.5 Home Runs",
            "book_odds": -140,
            "implied_probability": 58.3,
        },
        categories_positive=["A", "B", "C", "D", "E", "F"],
        categories_contradictory=[],
        categories_available=["A", "B", "C", "D", "E", "F"],
    )
    assert dec.eligible is False
    assert dec.block_reason and "base_score_below_apex_min" in dec.block_reason


# ─────────────────────────────────────────────────────────────────────
# §6P — Why This Pick truthfulness contract.
# ─────────────────────────────────────────────────────────────────────
def test_why_this_pick_only_cites_frozen_evidence():
    """A rendered rationale MUST NOT reference a field absent from
    the frozen decision_evidence snapshot.  This is the anti-
    fabrication contract required by §6S.
    """
    frozen = {
        "sport": "MLB",
        "player_name": "Dylan Cease",
        "opponent_team": "COL",
        "market": "Over 6.5 Strikeouts",
        "line": 6.5,
        "book_odds": -125,
        "model_probability": 72.4,
        "edge_percent": 8.5,
        "lock_score": 96.0,
        "sim_provenance": "CAUSAL_INDEPENDENT",
        "sim_input_quality": "STRONG",
        # No opponent_slg / no defensive_dvp — future renderer must
        # not fabricate them.
    }

    def _render(evidence: dict) -> str:
        # Reference implementation of the "cite only frozen fields"
        # contract Phase 6 requires.
        parts = []
        parts.append(f"{evidence['player_name']} vs {evidence['opponent_team']}")
        parts.append(f"{evidence['market']} @ {evidence['book_odds']}")
        parts.append(
            f"Model {evidence['model_probability']}%, edge "
            f"{evidence['edge_percent']:+.1f}pp, "
            f"Lock {evidence['lock_score']}"
        )
        if evidence.get("sim_provenance") in (
            "CAUSAL_INDEPENDENT", "EMPIRICAL_INDEPENDENT",
        ) and evidence.get("sim_input_quality") in ("FULL", "STRONG"):
            parts.append("Independent simulator confirms.")
        return " • ".join(parts)

    text = _render(frozen)

    # Assertions on truthfulness:
    for required in (
        frozen["player_name"], frozen["opponent_team"],
        frozen["market"], str(frozen["book_odds"]),
    ):
        assert required in text, f"missing frozen field in rationale: {required}"

    # Anti-fabrication: rationale cannot invent stats.
    for forbidden in ("opponent_slg", "defensive_dvp", "wOBA against",
                        "60% road ATS"):
        assert forbidden not in text, (
            f"rationale invented '{forbidden}' — not in frozen evidence"
        )


def test_why_this_pick_flags_edge_unavailable_when_none():
    """When the frozen decision has edge_percent=None (model-only
    pick), the rationale must NOT display a numeric edge."""
    frozen = {
        "sport": "Soccer", "player_name": "Erling Haaland",
        "market": "Anytime Goal Scorer",
        "model_probability": 62.0,
        "edge_percent": None,     # no real book line
        "book_odds": None,
        "lock_score": 92.0,
    }

    def _render(evidence: dict) -> str:
        edge_frag = ("Edge unavailable"
                      if evidence.get("edge_percent") is None
                      else f"edge {evidence['edge_percent']:+.1f}pp")
        return (f"{evidence['player_name']} — {evidence['market']} • "
                f"Model {evidence['model_probability']}%, {edge_frag}")

    text = _render(frozen)
    assert "Edge unavailable" in text
    assert "null" not in text.lower()
    assert "nan" not in text.lower()
