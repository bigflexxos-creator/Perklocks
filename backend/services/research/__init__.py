"""Canonical Research Contract Layer (Strategy Lab 10X).

FACTUAL research data (real, provider-verified facts like Statcast rows,
opportunity stats, matchup counts) may be threaded into existing per-sport
production models via `research_ctx` on their input context. Existing
production simulators (mlb_feature_engine, platinum_nfl, nba_feature_engine,
etc.) already consume this — we simply provide a single canonical
aggregation adapter here.

SHADOW signals (experimental trend detectors, pattern discoveries, learned
correlations) are tagged `provenance="SHADOW_SIGNAL"` and are NEVER read by
the production Lock scorer. They are surfaced to the Strategy Lab
workstation UI only.

Public API:
    from services.research import get_research_service
    svc = get_research_service()
    snapshot = await svc.build_snapshot(sport="MLB", event_id=...)
    factual  = svc.factual_ctx(snapshot)   # safe to feed production model
    shadow   = svc.shadow_signals(snapshot) # UI-only
"""
from .contract import (
    CanonicalResearchSnapshot,
    ResearchFact,
    ResearchShadowSignal,
    ResearchProvenance,
    ResearchQuality,
    ResearchSection,
)
from .service import get_research_service, ResearchService
from .bridge import enrich_ctx_with_factual

__all__ = [
    "CanonicalResearchSnapshot",
    "ResearchFact",
    "ResearchShadowSignal",
    "ResearchProvenance",
    "ResearchQuality",
    "ResearchSection",
    "ResearchService",
    "get_research_service",
    "enrich_ctx_with_factual",
]
