"""Research Bridge — FACTUAL-only hook into existing production contexts.

This module is intentionally LEAN. It exposes ONE function that any
per-sport context builder can optionally call to receive FACTUAL research
signals under a namespaced key. It NEVER surfaces SHADOW_SIGNAL rows to
the caller.

Design contract (HARD FREEZE):
  * `enrich_ctx_with_factual(ctx, sport, subject, opponent, role)` mutates
    `ctx` in place ONLY if new factual keys are available AND the key does
    NOT already exist. Existing production ctx values are never overwritten.
  * SHADOW_SIGNAL rows are always dropped. Only ResearchProvenance.FACTUAL
    passes through.
  * If the ResearchService is unavailable / errors, the ctx is unchanged
    and the call is a no-op (fail-open safe).

This bridge is OPT-IN. Existing production paths that already query
`mlb_statcast`, `nfl_players_intel`, `nba_team_form`, etc. continue to
work exactly as before. New callers (e.g. a promoted SHADOW signal that
graduates to FACTUAL) can start receiving via this bridge without any
scoring code changes.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("lockscore.research.bridge")


async def enrich_ctx_with_factual(
    ctx: dict[str, Any],
    sport: str,
    subject: str | None = None,
    opponent: str | None = None,
    role: str = "player",
) -> dict[str, Any]:
    """Return `ctx` optionally enriched with a `research_factual` namespace.

    Never overwrites existing keys. SHADOW rows are excluded by contract.
    Returns the same dict (mutated) for chaining convenience.
    """
    if not subject:
        return ctx
    try:
        from services.research import get_research_service, ResearchProvenance
    except Exception:
        return ctx
    try:
        svc = get_research_service()
        snap = await svc.build_snapshot(
            sport=sport, subject=subject, opponent=opponent,
            role=role, include_shadow=False,   # never pull shadow
            include_distribution=False, include_calibration=False,
        )
    except Exception as e:
        log.debug("research bridge fail-open for %s / %s: %s", sport, subject, e)
        return ctx

    factual_facts = [
        f for f in snap.facts
        if getattr(f, "provenance", None) == ResearchProvenance.FACTUAL
    ]
    if not factual_facts:
        return ctx

    # Namespaced injection — never collides with production ctx keys.
    ns = ctx.setdefault("research_factual", {})
    for f in factual_facts:
        if f.key not in ns:
            ns[f.key] = {
                "value": f.value,
                "quality": (f.quality.value if hasattr(f.quality, "value") else f.quality),
                "section": (f.section.value if hasattr(f.section, "value") else f.section),
                "sample_size": f.sample_size,
                "source": f.source,
                "label": f.label,
            }
    # Also stamp the snapshot version and count for audit trace.
    ctx.setdefault("research_meta", {})
    ctx["research_meta"].update({
        "generation_version": snap.generation_version,
        "generated_at": snap.generated_at,
        "factual_count": len(factual_facts),
    })
    return ctx
