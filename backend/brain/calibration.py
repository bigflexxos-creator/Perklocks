"""Confidence Calibration Layer.

Replaces RAW lock_score with a CALIBRATED probability built from the
historical band hit rates. Picks aren't re-bucketed; we just attach a
second confidence number (`confidence_calibrated`) the brain uses
downstream.

Logic:
  • Look up the band the pick's lock_score belongs to (99 / 95-98 / …).
  • If we have ≥ 20 settled picks in that band, the calibrated confidence
    is the band's empirical actual hit rate.
  • Otherwise fall back to the band's spec-expected hit rate.
  • Monotone safety guard: never let a calibrated value be MORE optimistic
    than (expected + 5pp) — protects against small-sample over-fitting.

Returns a value 0..1 stored at `pick['brain']['confidence_calibrated']`.
"""
from __future__ import annotations

import logging

from .memory import BrainMemory, band_for_score, CAL_BANDS

logger = logging.getLogger("lockscore.brain.calibration")

MIN_SAMPLE_FOR_OVERRIDE = 20    # below this we trust the spec expected
MAX_OPTIMISM_BUFFER = 5.0       # pp — never quote more than expected+5pp from data


def apply_calibration(picks: list[dict], memory: BrainMemory) -> dict:
    """Mutates each pick to add `brain.confidence_calibrated` (0..1)."""
    counts = {"calibrated_from_data": 0, "calibrated_from_spec": 0}
    for p in picks:
        lock = float(p.get("lock_score") or 0)
        band_name = band_for_score(lock)
        band = memory.band(band_name)
        spec = next((b["expected"] for b in CAL_BANDS if b["name"] == band_name), 50.0)
        if band and band.n >= MIN_SAMPLE_FOR_OVERRIDE:
            # Cap data-driven calibration at expected + buffer so a hot
            # streak doesn't push us into over-confident territory.
            calibrated_pct = min(band.actual_pct, spec + MAX_OPTIMISM_BUFFER)
            counts["calibrated_from_data"] += 1
        else:
            calibrated_pct = spec
            counts["calibrated_from_spec"] += 1
        brain = p.setdefault("brain", {})
        brain["confidence_calibrated"] = round(calibrated_pct / 100.0, 4)
        brain["confidence_band"] = band_name
        brain["confidence_band_expected"] = round(spec / 100.0, 4)
        brain["confidence_band_actual"] = round((band.actual_pct if band else spec) / 100.0, 4)
        brain["confidence_band_n"] = band.n if band else 0
    return counts
