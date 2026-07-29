"""Adaptive Learning System (2026-07-28).

Read-only observability + auto-tuning layer built on top of the
existing `fusion_predictions` telemetry collection.  This package
ONLY reads and analyses — it does NOT alter simulator math, retrain
NFL/MLB/Tennis models on its own, or introduce sportsbook odds.

Modules
───────
  calibration          — reliability curves + Brier + tier/sport/market
  performance_tracker  — per-engine (ml/similar/h2h/simulator/fused) win-rate
  weight_optimizer     — learns fusion weights per (sport, market)
  retraining_pipeline  — orchestrates retraining + new-vs-old comparison
  drift_detector       — accuracy drop + feature-importance shift alerts

Public API
──────────
  from services.adaptive_learning import (
      build_calibration_report,
      build_engine_performance_report,
      optimise_fusion_weights,
      RetrainingOrchestrator,
      detect_drift,
  )
"""
from .calibration import build_calibration_report          # noqa: F401
from .performance_tracker import build_engine_performance_report  # noqa: F401
from .weight_optimizer import (                              # noqa: F401
    optimise_fusion_weights,
    load_learned_weights,
)
from .retraining_pipeline import RetrainingOrchestrator     # noqa: F401
from .drift_detector import detect_drift                    # noqa: F401
from .daily_learning_job import (                            # noqa: F401
    run_daily_learning_job,
    load_latest_snapshot,
    compute_win_probability_calibration,
    compute_sport_performance,
    compute_market_performance,
    SNAPSHOT_COLL,
)
