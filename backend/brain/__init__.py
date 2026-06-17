"""PerksLocks Prediction Brain (v1).

Seven-layer internal upgrade that runs AFTER existing pick generation and
BEFORE picks land in the feed. Adds:

  1. Prediction Memory  — fast-read summary of every settled prediction
                          (roi, win rate, calibration, edge perf, market perf)
  2. Candidate Ranker   — composite score (edge + confidence + ROI +
                          consistency + data completeness)
  3. Monte Carlo Sim    — hidden Beta-Bernoulli simulator on top candidates
                          returning {win_probability, expected_value,
                          variance, agreement_score}
  4. Decision Filter    — formal PASS verdict; sets the existing `no_bet`
                          flag so feed endpoints silently drop them
  5. Confidence Calib.  — band-level isotonic-style mapping from raw lock
                          score to historical hit rate
  6. Dynamic Learning   — wires into the existing learning_engine /
                          learning_system_v2 reward signals
  7. Performance Cache  — single in-process memory snapshot reused across
                          a refresh; rebuilt only on settle

Zero UI changes — every signal is stored on the pick document as `brain`
sub-dict. PASS picks set `no_bet=True` so the existing feed filters drop
them automatically.
"""
from .pipeline import process_brain, BRAIN_VERSION

__all__ = ["process_brain", "BRAIN_VERSION"]
