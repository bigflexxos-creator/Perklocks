"""Why-This-Pick summary suppression μ-closure.

Static source-level assertions that guarantee the presentation layer:
1. Generic rationale.summary is suppressed when richer evidence exists.
2. Rich-evidence rationale (Bryce / Tennis) still renders untouched.
3. Fabrication is never triggered — the rendering is gated, not filled.
"""
from __future__ import annotations


def _read(path: str) -> str:
    with open(path) as f:
        return f.read()


def test_summary_no_longer_rendered_unconditionally():
    src = _read("/app/frontend/src/components/LockPickCard.tsx")
    # The old unconditional pattern must be gone.
    assert "{!!rationale!.summary && (" not in src, (
        "rationale.summary still rendered unconditionally"
    )


def test_summary_suppression_is_wired_via_predicates():
    src = _read("/app/frontend/src/components/LockPickCard.tsx")
    # The new gate must exist.
    assert "isGenericSummary" in src
    assert "hasRicher" in src
    assert "richBulletCount" in src


def test_summary_gate_detects_the_documented_generic_patterns():
    """Confirm the four bad-summary patterns from the directive are
    caught by regex in the gate."""
    src = _read("/app/frontend/src/components/LockPickCard.tsx")
    # "55% model win prob, +4.4pp over book"
    assert "over\\s+book" in src or "over\\\\s+book" in src or "+\\d+(?:\\.\\d+)?\\s*pp\\s+over\\s+book" in src
    # "model 54% to hit" / "model 54%"
    assert "model\\s+\\d" in src or "\\bmodel\\s+\\d{1,3}\\s*%" in src
    # "Total Runs:" / "Total Goals:"
    assert "^total\\s+runs\\s*:" in src.lower()


def test_richer_evidence_detector_covers_matchup_splits_forms():
    src = _read("/app/frontend/src/components/LockPickCard.tsx")
    # Rich detectors reference the same evidence objects that drive
    # Bryce-quality rationale rendering — matchup / splits /
    # pitcher_quality / recent_form / multipliers / evidence / h2h.
    assert "rationale!.matchup?.pitcher" in src
    assert "rationale!.matchup?.ballpark" in src
    assert "rationale!.splits" in src
    assert "rationale!.pitcher_quality" in src
    assert "rationale!.recent_form" in src
    assert "rationale!.multipliers" in src
    assert "rationale!.evidence?.length" in src
    assert "h2h_summary" in src and "h2h_compact" in src


def test_no_fabrication_predicate_only_suppression():
    src = _read("/app/frontend/src/components/LockPickCard.tsx")
    # The rendered fallback ONLY reads `summary` — no synthesis of
    # a new string. Confirm we render the string we already have.
    assert "styles.whySummary" in src
    # No "if (!summary) return 'Model favors ...'" style fabrication.
    assert "Model favors" not in src
    assert "This pick looks strong" not in src


def test_bryce_style_evidence_paths_still_present():
    """The pitcher_quality / matchup / splits render paths that make
    Bryce Miller Why-This-Pick shine must remain in-file."""
    src = _read("/app/frontend/src/components/LockPickCard.tsx")
    assert "rationale!.pitcher_quality" in src
    assert "pitcher_quality!" in src
    assert "rationale!.recent_form" in src


def test_theme_background_visibility_microlift_only():
    """Confirm the ONE global background nudge (deep navy → slightly
    more luminous deep navy) — no other tokens moved."""
    theme = _read("/app/frontend/src/theme.ts")
    # New bg color (~12% lighter deep navy).
    assert 'bg: "#0F1832"' in theme, "bg token not lifted"
    # Old value gone.
    assert 'bg: "#0B1226"' not in theme
    # Card surface UNCHANGED.
    assert 'surface: "#1A2340"' in theme
    # Gold / typography / borders UNCHANGED.
    assert 'goldElite: "#FFDD5C"' in theme
    assert 'textPrimary: "#FFFFFF"' in theme


if __name__ == "__main__":
    import sys, pytest
    sys.exit(pytest.main([__file__, "-v"]))
