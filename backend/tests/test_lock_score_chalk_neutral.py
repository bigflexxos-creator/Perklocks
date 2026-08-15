"""Regression tests for the chalk-neutral Lock Score tier (2026-07-04).

User spec §8 + §9:
  • Lock Score should reflect model confidence / historical / EV /
    agreement / data quality — NOT just win probability.
  • A +150 pick with stronger evidence (edge, EV, bucket) should be
    able to OUT-RANK a -200 favorite.
  • No implicit chalk bias — favorites and underdogs graded by the same
    evidence bar.

Run: python -m pytest backend/tests/test_lock_score_chalk_neutral.py -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sports_engine import compute_lock_score  # noqa: E402


def _factors_agreeing():
    """Six model factors that closely agree — low variance, high alignment."""
    return {"a": 0.70, "b": 0.68, "c": 0.72, "d": 0.70, "e": 0.69, "f": 0.71}


def _factors_split():
    """Factors that disagree — high variance, low alignment."""
    return {"a": 0.30, "b": 0.85, "c": 0.40, "d": 0.75, "e": 0.55, "f": 0.60}


def test_plus150_dog_with_strong_evidence_can_reach_strong_lock():
    """PHASE 1D (G3/G6) UPDATE — the 90/95/98 evidence-floor LADDER
    this test originally asserted was retired as a score-inflation
    path.  The chalk-NEUTRALITY intent is preserved: a +150 dog with
    strong evidence must score at least as high as a -300 chalk
    favorite with weaker edge and identical factor/bucket evidence.
    No hardcoded tier floor is asserted anymore."""
    dog_pick = {
        "book_odds": 150,
        "edge_percent": 12.0,
        "win_probability": 45.0,
    }
    chalk_pick = {
        "book_odds": -300,
        "edge_percent": 3.0,
        "win_probability": 78.0,
    }
    bucket = {"n": 40, "wins": 22, "losses": 18, "roi": 8.0}
    dog_lock, _ = compute_lock_score(_factors_agreeing(), win_prob=45.0,
                                     pick=dog_pick, bucket_row=bucket,
                                     edge_percent=12.0)
    chalk_lock, _ = compute_lock_score(_factors_agreeing(), win_prob=45.0,
                                       pick=chalk_pick, bucket_row=bucket,
                                       edge_percent=3.0)
    assert dog_lock >= chalk_lock, (
        f"dog with 4x the edge must not score below equivalent-evidence "
        f"chalk: dog={dog_lock} chalk={chalk_lock}")
    assert dog_lock > 0


def test_minus300_chalk_with_low_edge_capped_below_strong_lock():
    """-300 chalk (78% wp, 3% edge, EV negative) should NOT reach Strong
    Lock. Under the old regime this would have hit floor=90 via wp≥70 AND
    edge≥5 (fails edge — good), but even with edge≥3 it wouldn't reach
    Strong. Verify EV-based logic never lifts it there."""
    pick = {
        "book_odds": -300,
        "edge_percent": 3.0,
        "win_probability": 78.0,
    }
    bucket = {"n": 40, "wins": 22, "losses": 18, "roi": 3.0}
    lock, _ = compute_lock_score(_factors_agreeing(), win_prob=78.0,
                                   pick=pick, bucket_row=bucket,
                                   edge_percent=3.0)
    assert lock < 95.0, f"-300 chalk with 3% edge should not hit Strong Lock, got {lock}"


def test_dog_beats_chalk_when_evidence_is_better():
    """Head-to-head: identical factors, but +150 has higher EV & better
    bucket hit rate. Chalk-neutral scoring should rank the dog higher."""
    dog = {"book_odds": 150, "edge_percent": 10.0, "win_probability": 45.0}
    dog_bucket = {"n": 60, "wins": 40, "losses": 20, "roi": 12.0}
    dog_lock, _ = compute_lock_score(_factors_agreeing(), win_prob=45.0,
                                       pick=dog, bucket_row=dog_bucket,
                                       edge_percent=10.0)
    chalk = {"book_odds": -200, "edge_percent": 2.0, "win_probability": 68.0}
    chalk_bucket = {"n": 60, "wins": 34, "losses": 26, "roi": -2.0}
    chalk_lock, _ = compute_lock_score(_factors_agreeing(), win_prob=68.0,
                                         pick=chalk, bucket_row=chalk_bucket,
                                         edge_percent=2.0)
    assert dog_lock > chalk_lock, (
        f"+150 with better evidence ({dog_lock}) must outrank -200 chalk "
        f"({chalk_lock})"
    )


def test_split_factors_hurt_alignment_component():
    """When model factors disagree (high stdev), the alignment component
    drops and picks shouldn't reach Elite even with good numbers on other
    axes — bad agreement is a red flag."""
    pick = {"book_odds": 150, "edge_percent": 12.0, "win_probability": 45.0}
    bucket = {"n": 40, "wins": 25, "losses": 15, "roi": 10.0}
    lock_agree, _ = compute_lock_score(_factors_agreeing(), win_prob=45.0,
                                          pick=pick, bucket_row=bucket,
                                          edge_percent=12.0)
    lock_split, _ = compute_lock_score(_factors_split(), win_prob=45.0,
                                          pick=pick, bucket_row=bucket,
                                          edge_percent=12.0)
    assert lock_agree > lock_split, (
        f"factor agreement ({lock_agree}) should beat factor disagreement "
        f"({lock_split})"
    )


def test_ev_negative_never_reaches_playable_floor():
    """Even at edge≥3, if EV is negative the Playable floor should NOT fire."""
    pick = {"book_odds": -350, "edge_percent": 3.5, "win_probability": 68.0}
    # 0.68 * (1 + 100/350 - 1) - 0.32 = 0.68 * 0.2857 - 0.32 = 0.194 - 0.32 = -0.126
    lock, _ = compute_lock_score(_factors_agreeing(), win_prob=68.0,
                                   pick=pick, bucket_row=None,
                                   edge_percent=3.5)
    components = pick.get("lock_components") or {}
    assert components.get("ev_units", 0) < 0
    # Floor of 85 requires EV≥0 — must not fire when EV negative.
    assert components.get("quality_floor", 0) == 0, (
        f"negative-EV chalk should not trigger Playable floor: {components}"
    )


def test_evidence_signals_exposed_in_components():
    """The new EV / bucket_hit / agreement fields must be present in
    `lock_components` for UI transparency (§9 spec)."""
    pick = {"book_odds": -110, "edge_percent": 5.0, "win_probability": 55.0}
    bucket = {"n": 25, "wins": 15, "losses": 10, "roi": 5.0}
    compute_lock_score(_factors_agreeing(), win_prob=55.0,
                       pick=pick, bucket_row=bucket, edge_percent=5.0)
    components = pick["lock_components"]
    assert "ev_units" in components
    assert "bucket_hit" in components
    assert "agreement" in components
    assert components["bucket_hit"] == 0.6  # 15/(15+10)
    assert components["bucket_n"] == 25
    assert 0.0 <= components["agreement"] <= 1.0
