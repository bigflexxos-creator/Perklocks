"""Universal per-sport × per-family pipeline funnel (Blocks 44-47).

Thin aggregator that composes a per-sport × per-family funnel view
from the existing ``funnel_telemetry`` collection.  Reports **true
failure stage** instead of ``HEALTHY_NO_QUALIFIED_PICKS`` when
upstream dominates with ``SYNTHETIC_BOOK_ODDS`` or
``PUBLICATION_PROVENANCE_FAILURE``.

This module reads only.  It does NOT write pipeline reasons on its
own — it diagnoses them.
"""
from __future__ import annotations

from typing import Any, Optional

HEALTH_STATUSES = (
    "HEALTHY",
    "HEALTHY_NO_QUALIFIED_PICKS",
    "PUBLICATION_PROVENANCE_FAILURE",
    "MODEL_UNAVAILABLE",
    "IDENTITY_FAILURE",
    "NO_REAL_BOOK_LINE",
    "INSUFFICIENT_EVIDENCE",
    "SPORT_REFRESH_STARVED",
    "INGESTION_LAG",
    "NORMALIZATION_LAG",
    "PROVIDER_COVERAGE_LIMITED",
)


def diagnose_funnel(funnel: dict[str, int]) -> str:
    """Return a canonical health status given the funnel counts.

    Block 45: if provider_outcomes > 0 AND model_candidates > 0 AND
    at_or_above_85 > 0 AND published == 0 AND SYNTHETIC_BOOK_ODDS
    dominates the rejection split → ``PUBLICATION_PROVENANCE_FAILURE``.
    """
    g = lambda k: int(funnel.get(k, 0) or 0)
    provider_events  = g("provider_events")
    provider_outcomes = g("provider_outcomes") or g("provider_real_markets")
    canonicalized   = g("canonicalized") or g("normalized_candidates")
    model_candidates = g("model_candidates") or g("model_evaluated")
    at_or_above_85   = g("at_or_above_85")
    published        = g("published")
    rejections       = funnel.get("rejections", {}) or {}
    synthetic        = int(rejections.get("SYNTHETIC_BOOK_ODDS", 0) or 0)
    model_unavail    = int(rejections.get("MODEL_UNAVAILABLE", 0) or 0)
    identity_fail    = int(rejections.get("IDENTITY_FAILURE", 0) or 0)

    if provider_events == 0:
        return "SPORT_REFRESH_STARVED"
    if provider_outcomes == 0:
        return "PROVIDER_COVERAGE_LIMITED"
    if canonicalized == 0:
        return "NORMALIZATION_LAG"
    if model_candidates == 0:
        if model_unavail > 0:
            return "MODEL_UNAVAILABLE"
        return "INGESTION_LAG"
    if at_or_above_85 == 0:
        return "INSUFFICIENT_EVIDENCE"
    if published == 0:
        # Something passed 85 but didn't publish.  Blame the right stage.
        if synthetic > 0 and synthetic >= max(1, at_or_above_85 // 2):
            return "PUBLICATION_PROVENANCE_FAILURE"
        if identity_fail > 0:
            return "IDENTITY_FAILURE"
        return "PUBLICATION_PROVENANCE_FAILURE"
    return "HEALTHY"


def summarize_universal(funnels_by_sport_family: dict[tuple[str, str], dict]) -> dict:
    """Collapse a per-(sport,family) map of funnels into a summary.
    Returns:
        {
          "by_sport_family": { "nfl::qb_passing_yards": {funnel, health}, ... },
          "unhealthy":       [ ("nfl","qb_passing_yards","PUBLICATION_...") ],
          "total":           int,
        }
    """
    out: dict[str, dict] = {}
    unhealthy: list[tuple[str, str, str]] = []
    for (sport, family), funnel in funnels_by_sport_family.items():
        health = diagnose_funnel(funnel)
        key = f"{sport}::{family}"
        out[key] = {"funnel": dict(funnel), "health": health}
        if health != "HEALTHY":
            unhealthy.append((sport, family, health))
    return {"by_sport_family": out, "unhealthy": unhealthy,
            "total": len(out)}


__all__ = ["HEALTH_STATUSES", "diagnose_funnel", "summarize_universal"]
