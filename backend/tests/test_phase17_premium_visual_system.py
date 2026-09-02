"""Phase 17 — PREMIUM VISUAL SYSTEM 2.0 invariants (theme-level).

  V1. Design tokens declare a single source of truth (`src/theme.ts`).
  V2. Gold (goldElite / goldRich) is RESERVED for TRUE 100 APEX in
      the tier system — 99 PEAK MUST use the new Perklocks Purple.
  V3. Perklocks Purple identity tokens exist (canonical intelligence
      accent).
  V4. Layered dark-surface tokens exist (bg / surface / elevated
      / raised).
  V5. Tier vocabulary covers 85-89 STANDARD, 90-92 ELITE, 93-95
      STRONG, 96-98 RARE, 99 PEAK, 100 APEX.
  V6. Semantic result tokens exist for win (green) / loss (red) /
      push (neutral).
  V7. Confidence gradient (green→lime→gold→orange→red) declared.
  V8. GRADE_COLORS maps "APEX Lock" to goldElite (only APEX tier
      shows gold text) — "Elite Lock" (98/99 non-APEX) maps to
      Perklocks Purple.
"""
from __future__ import annotations
import pathlib
import re


THEME = pathlib.Path("/app/frontend/src/theme.ts").read_text(encoding="utf-8")


def test_theme_file_exists():
    assert pathlib.Path("/app/frontend/src/theme.ts").exists()


# ── V2 · gold gated to APEX only ────────────────────────────────
def test_99_peak_tier_no_longer_uses_gold():
    """Extract the `if (s === 99)` branch in getLockTierVisual and
    verify it does NOT reference goldElite/goldRich anymore."""
    m = re.search(
        r"if\s*\(s\s*===\s*99\)\s*\{([\s\S]*?)^\s*\}",
        THEME, re.MULTILINE,
    )
    assert m, "99-tier branch missing"
    branch = m.group(1)
    assert "goldElite" not in branch, "99 tier still uses goldElite"
    assert "goldRich" not in branch, "99 tier still uses goldRich"
    assert "perklocksPurple" in branch, "99 tier must use perklocksPurple"


def test_apex_100_tier_uses_gold():
    m = re.search(
        r"if\s*\(s\s*>=\s*100\)\s*\{([\s\S]*?)^\s*\}",
        THEME, re.MULTILINE,
    )
    assert m, "APEX 100-tier branch missing"
    branch = m.group(1)
    assert "goldElite" in branch, "APEX tier must use goldElite"


# ── V3 · Perklocks Purple identity tokens exist ────────────────
def test_perklocks_purple_tokens_declared():
    for token in (
        "perklocksPurple:",
        "perklocksPurpleRich:",
        "perklocksPurpleDeep:",
        "perklocksPurpleSoft:",
        "perklocksPurpleBorder:",
        "perklocksPurpleGlow:",
    ):
        assert token in THEME, f"missing purple token: {token}"


# ── V4 · layered dark surfaces ──────────────────────────────────
def test_layered_dark_surface_tokens():
    for token in ("bg:", "surface:", "surfaceElevated:",
                   "surfaceRaised:", "surfaceGloss:"):
        assert token in THEME, f"missing surface token: {token}"


# ── V5 · full tier vocabulary ──────────────────────────────────
def test_tier_vocabulary_complete():
    """The LockTierKey union must include all six tiers."""
    m = re.search(
        r'export\s+type\s+LockTierKey\s*=\s*([^;]+);',
        THEME,
    )
    assert m, "LockTierKey type missing"
    for tier in ("STANDARD", "ELITE", "STRONG", "RARE", "PEAK", "APEX"):
        assert tier in m.group(1), f"tier {tier} missing from union"


# ── V6 · semantic result tokens ─────────────────────────────────
def test_semantic_result_tokens():
    for token in ("winSurface:", "winBorder:", "lossSurface:",
                   "lossBorder:", "pushSurface:", "pushBorder:"):
        assert token in THEME, f"missing result token: {token}"


# ── V7 · confidence gradient ────────────────────────────────────
def test_confidence_gradient_declared():
    m = re.search(
        r"CONFIDENCE_GRADIENT\s*=\s*\[([\s\S]*?)\]",
        THEME,
    )
    assert m, "CONFIDENCE_GRADIENT missing"
    body = m.group(1)
    # Must contain green → red spectrum (5 stops).
    for c in ("#4DE68A", "#FF5F5C"):
        assert c in body, f"gradient missing {c}"


# ── V8 · GRADE_COLORS mapping ──────────────────────────────────
def test_grade_colors_apex_uses_gold_only():
    m = re.search(
        r"GRADE_COLORS\s*=\s*\{([\s\S]*?)\}\s+as\s+const",
        THEME,
    )
    assert m, "GRADE_COLORS map missing"
    body = m.group(1)
    # APEX Lock must map to gold; Elite Lock must NOT (98/99 non-APEX).
    apex_line = re.search(
        r'["\']APEX Lock["\']\s*:\s*COLORS\.(\w+)', body,
    )
    assert apex_line, "APEX Lock missing from GRADE_COLORS"
    assert apex_line.group(1) == "goldElite"
    # "Elite Lock" (98/99) must NOT map to goldElite.
    elite_line = re.search(
        r'["\']Elite Lock["\']\s*:\s*COLORS\.(\w+)', body,
    )
    assert elite_line, "Elite Lock missing from GRADE_COLORS"
    assert elite_line.group(1) != "goldElite", (
        "Elite Lock (98/99 non-APEX) must not use goldElite — "
        f"got COLORS.{elite_line.group(1)}"
    )
    assert elite_line.group(1) == "perklocksPurple"


# ── Layered dark bg values (deep near-black) ───────────────────
def test_bg_and_surface_are_deep_dark():
    """Extract bg + surface hex values and verify they're deep dark
    (all channels < 0x25)."""
    for token_name in ("bg", "surface"):
        m = re.search(rf'{token_name}:\s*"(#[0-9A-Fa-f]{{6}})"', THEME)
        assert m, f"{token_name} hex not found"
        hex_val = m.group(1)
        r, g, b = int(hex_val[1:3], 16), int(hex_val[3:5], 16), int(hex_val[5:7], 16)
        assert max(r, g, b) < 0x40, (
            f"{token_name}={hex_val} is not deep dark "
            f"(rgb=({r},{g},{b}))"
        )


if __name__ == "__main__":
    import sys, pytest
    sys.exit(pytest.main([__file__, "-v"]))
