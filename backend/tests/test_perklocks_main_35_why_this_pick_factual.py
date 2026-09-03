"""PERKLOCKS-MAIN 35 · P1-9 — WHY THIS PICK / FACTUAL EVIDENCE ONLY.

Contracts:
  * Rationale/insight builders return empty list when no real factors
    or components exist — never emit filler.
  * `build_why` never emits a bullet that isn't backed by a real
    signal component `details` string.
  * `_insights_for` never emits a hardcoded stat (regression against
    "39-5 L12 months", ".275 BAA", etc.).
  * Every insight bullet references a real breakdown factor or the
    model probability from the pick — never a fabricated number.
  * Missing model probability → no rationale bullet with a fake %
    (i.e. the "% model win probability" line only appears when
    win_probability is a real positive number).
"""
from __future__ import annotations

import re

import pytest


def test_insights_for_returns_empty_when_no_breakdown():
    from sports_engine import _insights_for

    assert _insights_for("MLB", {}, "away", "Home", "Away") == []
    assert _insights_for("Tennis", None, "away", "Home", "Away") == []


def test_prop_insights_returns_empty_when_no_breakdown():
    from sports_engine import _prop_insights

    assert _prop_insights("MLB", {}, "Aaron Judge") == []
    assert _prop_insights("NBA", None, "Nikola Jokic") == []


def test_build_why_never_invents_details():
    """Every positive/negative bullet must have a real details string
    from the signal component. Components without details are skipped."""
    from services.signal_engine.rationale import build_why

    pick = {"win_probability": 75, "market": "Aaron Judge Over 0.5 Home Runs"}
    components_with_no_details = [
        {"label": "Volume", "points": 20},   # no details → must not surface
        {"label": "Matchup", "points": -10}, # no details → must not surface
    ]
    out = build_why(pick, 82, components_with_no_details)
    # Only the headline should surface — no fabricated per-component text.
    for bullet in out:
        # Filter out headline (contains "Signal Score")
        if "Signal Score" in bullet:
            continue
        # Every non-headline bullet must reference a real details string.
        assert "Volume" not in bullet and "Matchup" not in bullet, bullet


def test_build_why_omits_headline_when_win_probability_missing():
    from services.signal_engine.rationale import build_why

    pick = {"market": "Some Market"}  # no win_probability
    out = build_why(pick, 70, [])
    # No headline should be emitted → out has no "% model win probability".
    for b in out:
        assert "model win probability" not in b, b


def test_insights_for_bullets_reference_real_factor_names():
    from sports_engine import _insights_for

    breakdown = {
        "Recent Form": 88.0,
        "Matchup Rating": 75.0,
        "Volume Trend": 62.0,
    }
    out = _insights_for("MLB", breakdown, "away", "Home", "Away")
    # Every bullet (except the trailing sport-context sentence) must
    # start with one of the factor names.
    factor_bullets = [b for b in out if "/100" in b]
    assert factor_bullets, out
    for b in factor_bullets:
        assert any(fn in b for fn in breakdown), b


_KNOWN_FABRICATED_PATTERNS = (
    re.compile(r"\b\d{1,2}-\d{1,2}\s+L\d+"),        # "39-5 L12"
    re.compile(r"\b\.\d{3}\s+B[AA]{2}"),             # ".275 BAA"
    re.compile(r"\b\d{2,3}%\s+finish\s+rate"),       # "78% finish rate"
    re.compile(r"last\s+\d+\s+games?\s+at", re.I),   # "last 10 games at ..."
    re.compile(r"has\s+\d+\s+hits\s+in\s+his\s+last", re.I),
)


def test_insights_for_never_emits_hardcoded_fake_stats():
    from sports_engine import _insights_for

    breakdown = {"Recent Form": 88.0, "Matchup Rating": 75.0}
    out = _insights_for("MLB", breakdown, "New York Yankees",
                        "New York Yankees", "Baltimore Orioles")
    for b in out:
        for pat in _KNOWN_FABRICATED_PATTERNS:
            assert not pat.search(b), (pat.pattern, b)


def test_prop_insights_never_emits_hardcoded_fake_stats():
    from sports_engine import _prop_insights

    breakdown = {"Volume": 78, "Matchup": 65}
    out = _prop_insights("MLB", breakdown, "Aaron Judge")
    for b in out:
        for pat in _KNOWN_FABRICATED_PATTERNS:
            assert not pat.search(b), (pat.pattern, b)


def test_build_why_max_bullets_is_six():
    """Cap at 6 bullets so the panel never becomes a wall of text."""
    from services.signal_engine.rationale import build_why

    components = [
        {"label": f"Factor{i}", "points": (10 - i),
         "details": [f"detail {i}"]}
        for i in range(10)
    ]
    pick = {"win_probability": 75, "market": "Test"}
    out = build_why(pick, 90, components)
    assert len(out) <= 6
