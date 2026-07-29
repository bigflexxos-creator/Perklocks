"""Parlay Intelligence — API enrichment helpers (Phase 5, 2026-06-30).

Bridges the existing `/api/picks/parlay` handler to the new engine WITHOUT
changing the endpoint's response contract.

`enrich_parlay_payload(card)`
    Adds an `intelligence` metadata block to a parlay card:
        {
          "mode": "safe" | "balanced" | "aggressive",
          "leg_rankings":   [{pick_id, parlay_score, grade, risk, ...}],
          "correlation":    {tier, pairs, positive_pairs, ...},
          "aggregate": {
              avg_parlay_score, grade_distribution,
              risk_distribution, downweight_factor,
          },
        }
    All original card keys are preserved.

`enrich_parlays(cards, mode=None)`
    Batch helper for the `parlays: [...]` array returned by the endpoint.
"""
from __future__ import annotations

import logging
from collections import Counter
from typing import Optional

from services.parlay_intelligence import (
    rank_leg, analyze_correlations, resolve_mode, MODE_PROFILES,
    summarize_backtest,
)

logger = logging.getLogger("lockscore.services.parlay_intelligence.api")


def _grade_dist(rankings: list) -> dict:
    return dict(Counter(r.confidence_grade for r in rankings))


def _risk_dist(rankings: list) -> dict:
    return dict(Counter(r.risk_level for r in rankings))


def enrich_parlay_payload(card: dict, *, mode: Optional[str] = None) -> dict:
    """Attach an `intelligence` block to a parlay card. Non-destructive."""
    if not isinstance(card, dict):
        return card
    legs = card.get("legs") or []
    if not isinstance(legs, list) or not legs:
        return card

    try:
        rankings = [rank_leg(L) for L in legs if isinstance(L, dict)]
    except Exception as e:
        logger.warning("leg ranking failed: %s", e)
        rankings = []

    try:
        correlation = analyze_correlations(legs)
    except Exception as e:
        logger.warning("correlation analysis failed: %s", e)
        correlation = None

    resolved_mode = resolve_mode(mode or card.get("mode") or card.get("label"))
    profile = MODE_PROFILES.get(resolved_mode)

    avg_score = (
        sum(r.parlay_score for r in rankings) / len(rankings)
        if rankings else 0.0
    )

    intel = {
        "mode": resolved_mode,
        "mode_label": profile.label if profile else resolved_mode.upper(),
        "mode_description": profile.description if profile else "",
        "leg_rankings": [
            {
                "pick_id": r.pick_id,
                "parlay_score": r.parlay_score,
                "confidence_grade": r.confidence_grade,
                "risk_level": r.risk_level,
                "notes": r.notes,
                "components": r.components,
            }
            for r in rankings
        ],
        "correlation": (
            correlation.to_dict() if correlation is not None else None
        ),
        "aggregate": {
            "avg_parlay_score": round(avg_score, 2),
            "grade_distribution": _grade_dist(rankings),
            "risk_distribution": _risk_dist(rankings),
            "downweight_factor": (correlation.downweight_factor
                                   if correlation is not None else 1.0),
            "correlation_tier": (correlation.tier
                                  if correlation is not None else "none"),
        },
    }
    out = dict(card)
    out["intelligence"] = intel
    return out


def enrich_parlays(cards: list, *, mode: Optional[str] = None) -> list:
    if not isinstance(cards, list):
        return cards
    return [enrich_parlay_payload(c, mode=mode) for c in cards]


async def backtest_summary_for(db) -> list[str]:
    """Return one-line explainer bullets from the latest backtest snapshot,
    or freshly compute a report if none exists."""
    try:
        from services.parlay_intelligence import backtest_parlays
        report = await backtest_parlays(db, days=60, persist=False)
        return summarize_backtest(report)
    except Exception as e:
        logger.warning("backtest_summary_for failed: %s", e)
        return []
