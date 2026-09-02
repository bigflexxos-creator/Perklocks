"""Phase 17 defect B closure — LockPickCard runtime gold audit.

Live evidence: Preview screenshot showed Lock 92 rendered with
gold value + gold border + gold glow.  Root cause: the LOCK
HeroBadge was invoked with `color={COLORS.goldElite} variant="gold"`
UNCONDITIONALLY.

Fix: LOCK badge color + variant now derive from the canonical
`tierVisual.key`.  Only APEX (100) uses gold; 99 PEAK uses the new
`purple` variant; 96-98/93-95/90-92/85-89 use tier accents.

This test guards against re-introduction by source-scanning the
LockPickCard render path for gold LOCK badge invocations.
"""
from __future__ import annotations
import pathlib
import re


CARD = pathlib.Path("/app/frontend/src/components/LockPickCard.tsx")


def _read():
    return CARD.read_text(encoding="utf-8")


def test_lock_hero_badge_color_derives_from_tier_visual():
    """The LOCK HeroBadge invocation MUST pass `tierVisual.accent`
    (or an equivalent tier-derived expression), NEVER a hardcoded
    `COLORS.goldElite`."""
    src = _read()
    # Find the LOCK HeroBadge invocation.
    m = re.search(
        r'<HeroBadge\s+icon="🔒"[\s\S]*?/>',
        src,
    )
    assert m, "LOCK HeroBadge invocation not found"
    invocation = m.group(0)
    assert "tierVisual" in invocation, (
        "LOCK badge must derive color from tierVisual — got:\n"
        + invocation
    )
    # Must NOT hardcode goldElite unconditionally.
    assert 'color={COLORS.goldElite}' not in invocation, (
        "LOCK badge unconditionally uses COLORS.goldElite — "
        "this is the exact Phase-17 defect B regression!"
    )
    # Must NOT hardcode variant="gold" unconditionally.
    assert 'variant="gold"' not in invocation.replace('"APEX"', ''), (
        "LOCK badge unconditionally uses variant=\"gold\" — "
        "must be tier-derived"
    )


def test_hero_badge_supports_purple_variant():
    """New `purple` variant added for 99 PEAK."""
    src = _read()
    # Type union must include "purple".
    assert '"gold" | "green" | "red" | "neutral" | "purple"' in src \
        or '"purple"' in src
    # A `heroBadgePurple` style token must exist.
    assert "heroBadgePurple:" in src


def test_lock_badge_maps_apex_to_gold_and_peak_to_purple():
    """The variant conditional MUST map APEX→gold, PEAK→purple, and
    the remaining tiers (RARE, STRONG, ELITE, STANDARD) to non-gold."""
    src = _read()
    # Look for the conditional in the LOCK invocation.
    m = re.search(
        r'<HeroBadge\s+icon="🔒"[\s\S]*?variant=\{([\s\S]*?)\}',
        src,
    )
    assert m, "LOCK HeroBadge variant conditional not found"
    variant_expr = m.group(1)
    # APEX must map to gold.
    assert '"APEX"' in variant_expr and '"gold"' in variant_expr
    # PEAK must map to purple.
    assert '"PEAK"' in variant_expr and '"purple"' in variant_expr
    # RARE/STRONG/ELITE/STANDARD must NOT map to gold.
    # Verify none of them appear on the same line as "gold".
    for tier in ("RARE", "STRONG", "ELITE", "STANDARD"):
        # Line where the tier maps.
        line_match = re.search(
            rf'"{tier}"\s*\?\s*"(\w+)"', variant_expr,
        )
        if line_match:
            assert line_match.group(1) != "gold", (
                f"tier {tier} still maps to gold — Phase-17 regression"
            )


def test_perklocks_purple_used_in_pick_card():
    """The card must reference perklocksPurple somewhere (either
    directly or via tierVisual.accent from theme)."""
    src = _read()
    assert "perklocksPurple" in src, (
        "LockPickCard does not reference perklocksPurple — "
        "99 PEAK cannot render its intended color"
    )
