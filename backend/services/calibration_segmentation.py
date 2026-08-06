"""Phase 4B — Calibration Segmentation Foundation.

**READ-ONLY module.** Defines the segmentation policy, bucket-key
generation, sample-size gates, and the fallback hierarchy that future
Phase 4B+ calibration curves will follow.  It does NOT fit or
overwrite production calibrators — that ships in a later Phase 4B
sub-step after the baseline report is reviewed.

Design goals
============
  1. No small bucket receives an unstable custom calibrator.
  2. Minimum sample thresholds are configurable per axis-depth.
  3. Calibration version is recorded on every fitted curve.
  4. Training range (min/max ``settled_at``) and sample size are
     recorded on every fitted curve.
  5. Out-of-sample evaluation is REQUIRED before a curve is promoted
     to runtime.

Fallback hierarchy
==================
When resolving a calibrator at read-time, the resolver walks the
following key hierarchy top-down; the FIRST key whose fitted curve
has ``n_settled >= MIN_SAMPLE`` (per level) is returned.

  L1  sport + market_family + side + line_band + odds_band + main/alt
  L2  sport + market_family + side + odds_band
  L3  sport + market_family + side
  L4  sport + market_family
  L5  sport
  L6  global  (labelled 'global_fallback' in metadata)

Sample-size gates
=================
  L1  MIN_SAMPLE = 200
  L2  MIN_SAMPLE = 100
  L3  MIN_SAMPLE = 60
  L4  MIN_SAMPLE = 40
  L5  MIN_SAMPLE = 30
  L6  no gate (always fitable — but explicitly labelled fallback)

These thresholds are the DEFAULT — a runtime config knob
(``PHASE4B_MIN_SAMPLE_OVERRIDES``) can override any level.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional, Any


CALIBRATION_SEGMENTATION_VERSION = "4B.0.0"

# Per-level sample-size gates.  Tuned to give L1 curves confidence
# intervals ≤ ±3 pp at 95% confidence.
DEFAULT_MIN_SAMPLE = {
    "L1": 200,
    "L2": 100,
    "L3": 60,
    "L4": 40,
    "L5": 30,
    "L6": 0,       # global fallback — always fitable
}

# Odds bands (American odds).
ODDS_BANDS = [
    ("deep_chalk",   -1_000_000, -300),
    ("chalk",        -299,       -180),
    ("moderate_fav", -179,       -140),
    ("light_fav",    -139,       -101),
    ("even",         -100,        100),
    ("light_dog",    +101,       +200),
    ("mid_dog",      +201,       +400),
    ("deep_dog",     +401,   1_000_000),
]

# Line bands (for player-prop half-lines).
LINE_BANDS = [
    ("0.5",   0.4,  0.6),
    ("1.5",   1.4,  1.6),
    ("2.5",   2.4,  2.6),
    ("3.5",   3.4,  3.6),
    ("4.5",   4.4,  4.6),
    ("5.5+",  5.4,  1_000.0),
    ("integer_low",  0.0,  0.39),
    ("integer_high", 5.61, 1_000.0),  # ← overlap by design; never used with half-lines above
    ("non_half",     -1_000.0, 0.0),   # negative lines (spreads)
]


@dataclass(frozen=True)
class BucketKey:
    """Immutable calibration bucket identity."""
    sport:         Optional[str]
    market_family: Optional[str]
    side:          Optional[str]
    line_band:     Optional[str]
    odds_band:     Optional[str]
    main_or_alt:   Optional[str]        # "main" or "alt"

    def as_dict(self) -> dict:
        return asdict(self)

    def to_string_id(self) -> str:
        """Stable string id for persistence."""
        return "|".join(
            f"{k}={v or '-'}" for k, v in self.as_dict().items()
        )


def classify_odds_band(american: Optional[int]) -> Optional[str]:
    if american is None:
        return None
    try:
        a = int(american)
    except (TypeError, ValueError):
        return None
    for name, lo, hi in ODDS_BANDS:
        if lo <= a <= hi:
            return name
    return None


def classify_line_band(line: Optional[float]) -> Optional[str]:
    if line is None:
        return None
    try:
        f = float(line)
    except (TypeError, ValueError):
        return None
    # Half-lines first (0.5, 1.5, 2.5, 3.5, 4.5, 5.5+).
    for name, lo, hi in LINE_BANDS:
        if lo <= f <= hi:
            return name
    return None


def build_bucket_key(
    *,
    sport: Optional[str],
    market_family: Optional[str],
    side: Optional[str] = None,
    line: Optional[float] = None,
    american_odds: Optional[int] = None,
    is_alt: Optional[bool] = None,
) -> BucketKey:
    return BucketKey(
        sport=sport,
        market_family=market_family,
        side=side,
        line_band=classify_line_band(line),
        odds_band=classify_odds_band(american_odds),
        main_or_alt=("alt" if is_alt else "main") if is_alt is not None else None,
    )


def hierarchy(key: BucketKey) -> list[tuple[str, BucketKey]]:
    """Return the ordered fallback hierarchy for a bucket key."""
    return [
        ("L1", key),
        ("L2", BucketKey(key.sport, key.market_family, key.side, None,
                          key.odds_band, None)),
        ("L3", BucketKey(key.sport, key.market_family, key.side, None,
                          None, None)),
        ("L4", BucketKey(key.sport, key.market_family, None, None, None, None)),
        ("L5", BucketKey(key.sport, None, None, None, None, None)),
        ("L6", BucketKey(None, None, None, None, None, None)),
    ]


def min_sample_for(level: str,
                    overrides: Optional[dict[str, int]] = None) -> int:
    """Return the min-sample threshold for a hierarchy level."""
    if overrides and level in overrides:
        return int(overrides[level])
    return DEFAULT_MIN_SAMPLE.get(level, 0)


@dataclass
class SegmentedCalibrator:
    """A single fitted calibrator with all required metadata.

    The FITTING logic (isotonic PAV) ships in a later Phase 4B
    sub-step — this dataclass is the CONTRACT that any fitted
    calibrator must produce.
    """
    key:              BucketKey
    level:            str                 # "L1".."L6"
    version:          str
    n_settled:        int
    train_min_date:   Optional[str]       # ISO 8601 or None
    train_max_date:   Optional[str]
    knots_x:          list[float]         # raw score / lock (sorted)
    knots_y:          list[float]         # calibrated probability (monotone)
    out_of_sample_brier: Optional[float]
    out_of_sample_log_loss: Optional[float]
    out_of_sample_hit_rate: Optional[float]
    promoted:         bool                # gate — read-time uses only when True
    labelled_as_fallback: bool = False


__all__ = [
    "CALIBRATION_SEGMENTATION_VERSION",
    "DEFAULT_MIN_SAMPLE",
    "ODDS_BANDS",
    "LINE_BANDS",
    "BucketKey",
    "SegmentedCalibrator",
    "classify_odds_band",
    "classify_line_band",
    "build_bucket_key",
    "hierarchy",
    "min_sample_for",
]
