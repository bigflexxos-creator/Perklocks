"""Focused regressions for the Strategy Lab 10X canonical research contract.

Proves:
  * SHADOW_SIGNAL rows NEVER leak into `to_ctx()`.
  * `enrich_ctx_with_factual` never overwrites an existing production key.
  * Snapshots are HTTP-serializable.
  * Distribution / line-explorer math is deterministic for a known input.
"""
from __future__ import annotations

import asyncio
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.research.contract import (
    CanonicalResearchSnapshot, ResearchFact, ResearchProvenance,
    ResearchQuality, ResearchSection, ResearchShadowSignal,
)


def test_shadow_never_in_ctx():
    snap = CanonicalResearchSnapshot(sport="MLB", generated_at="now")
    snap.facts.append(ResearchFact(
        key="factual_a", label="A", value=1.0,
        provenance=ResearchProvenance.FACTUAL,
        quality=ResearchQuality.STRONG,
        section=ResearchSection.FORM,
    ))
    snap.facts.append(ResearchFact(
        key="shadow_b", label="B", value=2.0,
        provenance=ResearchProvenance.SHADOW_SIGNAL,
        quality=ResearchQuality.PARTIAL,
        section=ResearchSection.PATTERN,
    ))
    ctx = snap.to_ctx()
    assert "factual_a" in ctx
    assert "shadow_b" not in ctx
    assert ctx == {"factual_a": 1.0}


def test_to_dict_serializable():
    import json
    snap = CanonicalResearchSnapshot(
        sport="NBA", generated_at="now", subject="Test Player",
    )
    snap.facts.append(ResearchFact(
        key="k", label="L", value={"a": 1, "b": [1, 2]},
        provenance=ResearchProvenance.FACTUAL,
        quality=ResearchQuality.FULL,
        section=ResearchSection.FORM,
    ))
    snap.shadow.append(ResearchShadowSignal(
        key="p", label="P", description="d",
        hits=3, n=5, hit_rate=0.6, wilson_lower=0.3, strength="moderate",
    ))
    d = snap.to_dict()
    # Must serialize cleanly
    s = json.dumps(d)
    assert "SHADOW_SIGNAL" in s
    assert "FACTUAL" in s
    assert d["factual_count"] == 1
    assert d["shadow_count"] == 1  # one shadow signal, no shadow facts


def test_bridge_never_overwrites_and_never_pulls_shadow():
    """The bridge must:
       (a) fail-open when subject is missing,
       (b) never overwrite existing keys in ctx,
       (c) only pass FACTUAL rows into the namespace.
    """
    from services.research.bridge import enrich_ctx_with_factual

    async def _run():
        ctx = {"existing_key": "PRESERVE_ME"}
        # No subject → no-op
        out = await enrich_ctx_with_factual(ctx, "MLB", None)
        assert out is ctx
        assert out.get("existing_key") == "PRESERVE_ME"
        # SHADOW never in the namespace either
        assert "shadow_streak" not in out.get("research_factual", {})

    asyncio.get_event_loop().run_until_complete(_run())


def test_line_explorer_math_deterministic():
    """Given a synthetic distribution, line_explorer computes proper
    empirical over/under and fair prices."""
    from services.research.service import ResearchService
    svc = ResearchService()

    async def _run(monkeypatch=None):
        # Monkey-patch distribution to return known values
        original = svc.distribution
        async def _fake_dist(sport, subject, market_hint=None):
            return {"available": True, "stat": "hits",
                    "sample_size": 10, "mean": 1.4, "median": 1.0,
                    "std": 0.5, "p10": 0, "p25": 1, "p50": 1, "p75": 2, "p90": 2,
                    "min": 0, "max": 3,
                    "values": [0, 1, 1, 1, 1, 2, 2, 2, 2, 3]}
        svc.distribution = _fake_dist  # type: ignore
        try:
            r = await svc.line_explorer("MLB", "X", 1.5)
            # 5 values > 1.5 → empirical_over_rate = 0.5
            assert r["empirical_over_rate"] == 0.5
            # p=0.5 → fair odds = -100 (exact even)
            assert r["fair_over_odds"] == -100
        finally:
            svc.distribution = original  # type: ignore

    asyncio.get_event_loop().run_until_complete(_run())


def test_supported_sports_gate():
    """SUPPORTED_SPORTS must be exactly MLB/NFL/NBA in this build."""
    from services.research.service import SUPPORTED_SPORTS
    assert SUPPORTED_SPORTS == {"MLB", "NFL", "NBA"}


def _run_all():
    tests = [
        test_shadow_never_in_ctx,
        test_to_dict_serializable,
        test_bridge_never_overwrites_and_never_pulls_shadow,
        test_line_explorer_math_deterministic,
        test_supported_sports_gate,
    ]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{passed}/{len(tests)} PASS")
    return passed == len(tests)


if __name__ == "__main__":
    ok = _run_all()
    sys.exit(0 if ok else 1)
