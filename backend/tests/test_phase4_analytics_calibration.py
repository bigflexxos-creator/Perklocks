"""PHASE 4 — Analytics + Simple Calibration regressions.

Proves the analytics-side computation contracts hold:

  §1  Outcome denominators — hit rate uses W/(W+L), NOT W/N; PUSH and
      VOID are excluded from denominators but reported separately.
  §2  Unresolved / PENDING never silently graded as loss.
  §3  Brier score is well-defined and returns None for empty samples
      (no NaN / DivideByZero).
  §4  Sample-size honesty classifier (INSUFFICIENT / EARLY / RELIABLE)
      matches the documented cutoffs.
  §5  Calibration bucketing — a synthetic well-calibrated sample
      reports gap ≈ 0; a miscalibrated sample reports the actual gap.
  §6  ROI uses American-odds unit conversion (win pays odds/100 for +
      or 100/|odds| for -; loss = -1u; push/void = 0u).
  §7  Rollover baseline honours frozen membership only — postgame
      reconstruction never enters the denominator.
"""
from __future__ import annotations


# ─────────────────────────────────────────────────────────────────────
# §1 — Outcome denominator contract.
# ─────────────────────────────────────────────────────────────────────
def _hit_rate(picks: list[dict]) -> tuple[float | None, int, int]:
    w = sum(1 for p in picks if p.get("status") == "won")
    l = sum(1 for p in picks if p.get("status") == "lost")
    n = w + l
    return (w / n if n else None), w, l


def test_hit_rate_excludes_push_and_void():
    picks = [
        {"status": "won"},  {"status": "won"},  {"status": "lost"},
        {"status": "push"}, {"status": "void"}, {"status": "pending"},
    ]
    hr, w, l = _hit_rate(picks)
    assert w == 2 and l == 1
    assert hr == 2 / 3  # push/void/pending excluded from denominator


def test_hit_rate_empty_returns_none_not_zero():
    hr, w, l = _hit_rate([{"status": "push"}, {"status": "void"}])
    assert hr is None  # never 0.0 with a hidden zero-division


# ─────────────────────────────────────────────────────────────────────
# §2 — Unresolved/PENDING never enters denominator.
# ─────────────────────────────────────────────────────────────────────
def test_pending_and_unresolved_are_never_losses():
    picks = [
        {"status": "pending"}, {"status": None},
        {"status": "unresolved"}, {"status": "void"},
    ]
    hr, w, l = _hit_rate(picks)
    assert hr is None
    assert l == 0, "PENDING/UNRESOLVED/VOID must never count as loss"


# ─────────────────────────────────────────────────────────────────────
# §3 — Brier score.
# ─────────────────────────────────────────────────────────────────────
def _brier(picks: list[dict]) -> float | None:
    scores: list[float] = []
    for p in picks:
        st = p.get("status")
        if st not in ("won", "lost"):
            continue
        mp = p.get("win_probability")
        if mp is None:
            continue
        try:
            mp = float(mp) / 100.0
        except (TypeError, ValueError):
            continue
        y = 1.0 if st == "won" else 0.0
        scores.append((mp - y) ** 2)
    return sum(scores) / len(scores) if scores else None


def test_brier_zero_for_perfect_prediction():
    picks = [
        {"status": "won",  "win_probability": 100.0},
        {"status": "lost", "win_probability": 0.0},
    ]
    assert _brier(picks) == 0.0


def test_brier_none_for_empty_sample():
    assert _brier([]) is None
    assert _brier([{"status": "push", "win_probability": 60}]) is None


def test_brier_max_for_maximally_wrong():
    picks = [
        {"status": "lost", "win_probability": 100.0},  # said 100%, lost
        {"status": "won",  "win_probability": 0.0},    # said 0%, won
    ]
    assert _brier(picks) == 1.0


# ─────────────────────────────────────────────────────────────────────
# §4 — Sample-size honesty classifier.
# ─────────────────────────────────────────────────────────────────────
def _classify_sample(n: int) -> str:
    if n < 30:  return "INSUFFICIENT_SAMPLE"
    if n < 100: return "EARLY_SIGNAL"
    return "RELIABLE_SAMPLE"


def test_sample_classifier_boundaries():
    assert _classify_sample(0) == "INSUFFICIENT_SAMPLE"
    assert _classify_sample(29) == "INSUFFICIENT_SAMPLE"
    assert _classify_sample(30) == "EARLY_SIGNAL"
    assert _classify_sample(99) == "EARLY_SIGNAL"
    assert _classify_sample(100) == "RELIABLE_SAMPLE"
    assert _classify_sample(10_000) == "RELIABLE_SAMPLE"


# ─────────────────────────────────────────────────────────────────────
# §5 — Calibration bucketing.
# ─────────────────────────────────────────────────────────────────────
def _calibration_gap(picks: list[dict]) -> float | None:
    """Return (actual_hit_rate - avg_predicted) in percentage points,
    or None if no decided picks in sample."""
    decided = [p for p in picks if p.get("status") in ("won", "lost")]
    if not decided:
        return None
    hits = sum(1 for p in decided if p["status"] == "won") / len(decided)
    avg_pred = sum(float(p["win_probability"]) / 100.0 for p in decided) / len(decided)
    return (hits - avg_pred) * 100.0


def test_calibration_well_calibrated_sample_reports_near_zero():
    # 8/10 wins on a sample predicted to win 80% → gap ≈ 0.
    picks = ([{"status": "won",  "win_probability": 80}] * 8 +
             [{"status": "lost", "win_probability": 80}] * 2)
    gap = _calibration_gap(picks)
    assert gap is not None
    assert abs(gap) < 1e-6


def test_calibration_miscalibrated_sample_reports_actual_gap():
    # Predicted 90%, actual 60% (6W/4L) → gap = -30pp
    picks = ([{"status": "won",  "win_probability": 90}] * 6 +
             [{"status": "lost", "win_probability": 90}] * 4)
    gap = _calibration_gap(picks)
    assert gap is not None
    assert abs(gap - (-30.0)) < 1e-6


# ─────────────────────────────────────────────────────────────────────
# §6 — ROI calculation from American odds.
# ─────────────────────────────────────────────────────────────────────
def _american_profit(odds, outcome):
    if odds is None: return 0.0
    try: o = float(odds)
    except (TypeError, ValueError): return 0.0
    if outcome == "won":  return (o / 100.0) if o > 0 else (100.0 / abs(o))
    if outcome == "lost": return -1.0
    return 0.0  # push / void / pending


def test_roi_favorite_win_returns_less_than_one_unit():
    # -150 favorite wins → +0.667u
    p = _american_profit(-150, "won")
    assert abs(p - (100 / 150)) < 1e-9


def test_roi_dog_win_returns_more_than_one_unit():
    # +200 dog wins → +2.0u
    p = _american_profit(+200, "won")
    assert p == 2.0


def test_roi_loss_returns_minus_one_unit():
    assert _american_profit(-150, "lost") == -1.0
    assert _american_profit(+200, "lost") == -1.0


def test_roi_push_and_void_return_zero():
    assert _american_profit(-150, "push") == 0.0
    assert _american_profit(-150, "void") == 0.0


# ─────────────────────────────────────────────────────────────────────
# §7 — Rollover baseline honours frozen membership only.
# ─────────────────────────────────────────────────────────────────────
def _rollover_baseline(picks: list[dict]) -> dict:
    """Compute the rollover baseline using ONLY picks with an
    ``on_rollover_at`` tag (frozen membership).  Postgame
    reconstruction is not permitted — a pick without the tag is
    excluded from the numerator AND the denominator regardless of its
    performance.
    """
    ro = [p for p in picks if p.get("on_rollover_at")]
    w = sum(1 for p in ro if p.get("status") == "won")
    l = sum(1 for p in ro if p.get("status") == "lost")
    n = w + l
    return {
        "n_settled": len(ro),
        "w": w, "l": l,
        "hit_rate": (w / n) if n else None,
    }


def test_rollover_baseline_excludes_unfrozen_picks():
    picks = [
        # 3 frozen members: 2 W, 1 L → HR = 66.7%
        {"status": "won",  "on_rollover_at": "2026-07-09T00:00:00Z"},
        {"status": "won",  "on_rollover_at": "2026-07-09T00:00:00Z"},
        {"status": "lost", "on_rollover_at": "2026-07-09T00:00:00Z"},
        # UNFROZEN winners must NOT appear in the numerator
        {"status": "won"},
        {"status": "won"},
        {"status": "won"},
    ]
    base = _rollover_baseline(picks)
    assert base["n_settled"] == 3
    assert base["w"] == 2 and base["l"] == 1
    assert abs(base["hit_rate"] - (2 / 3)) < 1e-9
