"""Sport-specific feature-builder modules (2026-07-28).

Layout mirrors `train_prop_model.py`'s sport dispatch:

    ml/features/
      nfl.py     — imported lazily from feature_builder.py (existing)
      mlb.py     — batter + pitcher props
      nba.py     — scaffold only (no player_game_logs yet)
      soccer.py  — season-aggregate features
      tennis.py  — match-level features from tennis_matches_history

Each module MUST export:
  • `build_training_frame(db_rows, stat, ...) -> TrainingFrame`
  • `build_live_features(db, player_name, opponent, stat, ...) -> tuple`

Do NOT import this package from `feature_builder.py` at module load
time — feature_builder.py stays authoritative for NFL to keep the
existing test suite untouched. This package is picked up by the
generalised trainer.
"""
