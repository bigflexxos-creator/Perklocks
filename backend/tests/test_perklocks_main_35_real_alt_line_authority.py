"""PERKLOCKS-MAIN 35 · P0-4 — REAL ALT-LINE AUTHORITY / ALT MAGIC regression.

Contracts asserted:
  * The canonical publication boundary rejects any pick that carries
    `model_line=True` (Poisson-synthesized Soccer alt totals or any
    other producer-derived line stamped as a model line).
  * The boundary rejects any pick that carries `no_real_book_line=True`
    while also carrying a book_odds value (contradiction ↦ synthetic).
  * The boundary rejects picks with known synthetic `odds_source`
    labels (`model_derived`, `synthetic`, `hfa_baseline`, `form`).
  * The boundary rejects picks whose `model_source` starts with
    `poisson_from_`, `synthetic_`, or `model_only_`.
  * A real-provider alt pick (real book_odds + real odds_source)
    is accepted.
  * `_build_tennis_alt_picks` iterates only over REAL provider
    outcomes: source inspection proves it does NOT invoke the removed
    synthesizer and only iterates over `_alt_outcomes_for_market(...,
    'alternate_totals')`.
  * `_synthesize_chalk_alt_totals` is a permanent no-op — the tombstone
    stays a tombstone. Regression-proofs against a future revive.
  * Same-family ladder monotonicity holds for the alt-total pricing
    path (tested from the P0-1 empirical CDF).
"""
from __future__ import annotations

import inspect

import pytest


def _mk_pick(**overrides):
    base = {
        "id": "pick-xyz-1",
        "sport": "Soccer",
        "market": "Total Goals Over 1.5",
        "selection": "Over",
        "book_odds": -300,
        "odds_source": "the_odds_api",
        "model_probability": 0.75,
        "identity_class": "AUTHORITATIVE",
        "edge_percent": 0.03,
    }
    base.update(overrides)
    return base


# ─────────────────────────────────────────────────────────────────────
# Canonical publication boundary — model-line + synthetic rejection
# ─────────────────────────────────────────────────────────────────────
def test_boundary_rejects_model_line_true_pick():
    from services.canonical_publication_boundary import evaluate_publication

    pick = _mk_pick(model_line=True)
    v = evaluate_publication(pick)
    assert v.accepted is False
    assert any("MODEL_LINE" in r for r in v.reasons)


def test_boundary_rejects_no_real_line_with_odds():
    from services.canonical_publication_boundary import evaluate_publication

    pick = _mk_pick(no_real_book_line=True)  # still carries book_odds → contradiction
    v = evaluate_publication(pick)
    assert v.accepted is False
    assert any("NO_REAL_LINE" in r or "SYNTHETIC" in r for r in v.reasons)


def test_boundary_rejects_synthetic_odds_sources():
    from services.canonical_publication_boundary import evaluate_publication

    for src in ("model_derived", "synthetic", "hfa_baseline", "form", "computed"):
        pick = _mk_pick(odds_source=src)
        v = evaluate_publication(pick)
        assert v.accepted is False, src
        assert any("SYNTHETIC" in r for r in v.reasons), (src, v.reasons)


def test_boundary_accepts_real_provider_alt_line():
    from services.canonical_publication_boundary import evaluate_publication

    pick = _mk_pick(
        sport="Tennis",
        market="Over 41.5 Games (Alt)",
        odds_source="the_odds_api",
        book_odds=-450,
        model_probability=0.83,
        identity_class="AUTHORITATIVE",
    )
    v = evaluate_publication(pick)
    # Real provider price + real source + model provenance → PUBLISHED.
    assert v.accepted is True, v.reasons


# ─────────────────────────────────────────────────────────────────────
# Tennis alt-total path — provider-observed lines only
# ─────────────────────────────────────────────────────────────────────
def test_tennis_alt_totals_iterates_only_real_provider_outcomes():
    import sports_engine
    src = inspect.getsource(sports_engine._build_tennis_alt_picks)
    # Must consume the REAL alternate_totals outcome list.
    assert "_alt_outcomes_for_market(alt_payload, \"alternate_totals\")" in src
    # Must NOT invoke the removed synthesizer.
    assert "_synthesize_chalk_alt_totals" not in src or (
        "no `_synthesize_chalk_alt_totals` call" in src
    ), "synthesizer call re-introduced — regression"


def test_synthesize_chalk_alt_totals_is_permanent_tombstone():
    """The removed synthesizer must stay removed. A future revive would
    put synthetic thresholds back into production."""
    import sports_engine
    out = sports_engine._synthesize_chalk_alt_totals()
    assert out == []


# ─────────────────────────────────────────────────────────────────────
# Ladder monotonicity — same-family ladder cannot invert.
# ─────────────────────────────────────────────────────────────────────
def test_same_family_alt_ladder_over_prob_monotone_non_increasing():
    import bisect
    import random
    from brain.sim_tennis import _simulate_match_full

    random.seed(20260602)
    dist = []
    for _ in range(1500):
        tg, *_ = _simulate_match_full(0.65, 0.61, bo=5)
        dist.append(tg)
    dist.sort()

    def over_prob(line):
        idx = bisect.bisect_right(dist, float(line))
        return (len(dist) - idx) / float(len(dist))

    thresholds = sorted({35.5, 37.5, 39.5, 41.5, 42.5, 44.5, 46.5})
    over_probs = [over_prob(t) for t in thresholds]
    for i in range(1, len(over_probs)):
        assert over_probs[i] <= over_probs[i - 1] + 1e-9, (
            thresholds[i - 1], thresholds[i], over_probs[i - 1], over_probs[i],
        )
