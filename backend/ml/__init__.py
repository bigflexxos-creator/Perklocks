"""ML package for LockScore player-prop prediction (2026-07-28).

Modules
───────
  feature_builder.py       — Pure pandas feature engineering (time-safe).
  train_prop_model.py      — Batch trainer (LightGBM + XGBoost).
  __init__.py              — this file (package marker).

Contract
────────
  • Never uses sportsbook odds, betting lines, or public consensus as
    features or training targets. Models predict player performance
    only — the market line is applied ONLY at inference to convert
    predicted mean/σ → P(exceed line).
  • Time-safe: every training feature is computed from data strictly
    before the target row's (season, week). No random shuffle.
"""
