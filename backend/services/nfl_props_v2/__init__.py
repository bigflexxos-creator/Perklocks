"""NFL Player Props 2.0 — Universal Game Intelligence + Best-Bet Discovery
==============================================================================

P0 Universal Closure (2026-06-21).

Architecture:
  GAME → PLAYER ROLE → DISTRIBUTION → THRESHOLD → SAFEST/BEST → FROZEN

Design contract (per spec):
  * Reuse existing PerkLocks providers — no new external calls in this pass.
  * Weather + Injury enrichment are FEATURES, not gates.  Missing data
    degrades CONFIDENCE, never suppresses a legitimate real-line prop.
  * Monotonicity guarantee across ordered thresholds.
  * Safest-bet ≠ best-value ≠ Lock Score — three distinct outputs.
  * Compute game context ONCE, player context ONCE, distribution ONCE.
  * Adapters (WeatherProvider, AvailabilityProvider) are replaceable —
    plug in a real provider later with zero probability-architecture change.
"""
from __future__ import annotations

from .adapters import (
    WeatherProvider, WeatherReport, AvailabilityProvider, AvailabilityReport,
    WEATHER_STATUS_AVAILABLE, WEATHER_STATUS_UNAVAILABLE,
    INJURY_STATUS_AVAILABLE, INJURY_STATUS_PARTIAL,
    INJURY_STATUS_UNAVAILABLE,
)
