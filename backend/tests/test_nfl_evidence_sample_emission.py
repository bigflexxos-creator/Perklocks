"""NFL EvidenceFeature Sample-Size Emission — Stage-2 FINAL ROOT FIX proof
──────────────────────────────────────────────────────────────────────

Contract (2026-06-25 · user directive):
    L3 features MUST bind to `sample_size = min(3, distribution_n)`.
    L5 features MUST bind to `sample_size = min(5, distribution_n)`.
    Threshold Distribution Support MUST bind to actual distribution n
    (not a shared/hardcoded default).
    Historical Threshold Rate MUST bind to actual distribution n.
    Home/Away / Career vs Opp / Opponent Defense MUST bind to a
    realistic subset count (not the blind form-default 10).

This test simulates a pick shape produced by the NFL feature engine
after `recompute_line_dependent_factors` writes both the factors
dict AND the `factor_sample_sizes` metadata onto the pick, then
verifies the universal `_universal_build_features_from_pick` reads
that metadata and emits per-EvidenceFeature `sample_size` values
that honestly reflect the TRUE per-feature evidence horizon.

Zero mocks — pure function test.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evidence_engine import _universal_build_features_from_pick


def test_evidence_features_bind_true_sample_size():
    """A 12-sample distribution, 5-game L5, 3-game L3 pick must
    produce EvidenceFeatures with matching sample_size values.
    """
    pick = {
        "sport": "NFL",
        "market": "PASS YDS",
        "player": "Joe Burrow",
        "factors": {
            "L5 Avg vs Line":            0.72,
            "L3 vs Season Trend":        0.58,
            "Home/Away Split":           0.55,
            "Career vs Opponent Hit%":   0.63,
            "Opponent Defense Allowance": 0.65,
            "Threshold Distribution Support": 0.82,
            "L5 Threshold Support":      0.62,
            "L3 Threshold Support":      0.60,
            "Historical Threshold Rate": 0.65,
        },
        "factor_sample_sizes": {
            "L5 Avg vs Line":            5,
            "L3 vs Season Trend":        3,
            "L5 Threshold Support":      5,
            "L3 Threshold Support":      3,
            "Threshold Distribution Support": 12,
            "Historical Threshold Rate": 12,
            "Home/Away Split":           6,
            "Career vs Opponent Hit%":   6,
            "Opponent Defense Allowance": 8,
        },
    }
    feats = _universal_build_features_from_pick(pick)
    by_name = {f.name: f for f in feats}

    expect = {
        "L5 Avg vs Line":                 5,
        "L3 vs Season Trend":             3,
        "L5 Threshold Support":           5,
        "L3 Threshold Support":           3,
        "Threshold Distribution Support": 12,
        "Historical Threshold Rate":      12,
        "Home/Away Split":                6,
        "Career vs Opponent Hit%":        6,
        "Opponent Defense Allowance":     8,
    }
    for name, want_n in expect.items():
        assert name in by_name, f"missing feature: {name}"
        got_n = by_name[name].sample_size
        assert got_n == want_n, (
            f"{name}: expected sample_size={want_n}, got {got_n}"
        )
    print("[emission] TRUE per-factor sample-size map honoured for all 9 NFL factors ✓")


def test_short_window_bounds_without_meta():
    """Even when a pick has NO `factor_sample_sizes` attached (e.g.
    an older cached pick), the emission logic must NOT default L3
    to 10 and L5 to 10 — the name-based cap must apply so short-
    window features can never inherit the form default.
    """
    pick = {
        "sport": "NFL",
        "market": "RUSH YDS",
        "factors": {
            "L5 Avg vs Line":     0.68,
            "L3 vs Season Trend": 0.55,
            "Season Form":        0.60,   # generic form → 10 is fine
        },
        # No factor_sample_sizes attached — legacy path.
    }
    feats = _universal_build_features_from_pick(pick)
    by_name = {f.name: f for f in feats}
    assert by_name["L5 Avg vs Line"].sample_size <= 5, (
        f"L5 leaked to {by_name['L5 Avg vs Line'].sample_size} without meta"
    )
    assert by_name["L3 vs Season Trend"].sample_size <= 3, (
        f"L3 leaked to {by_name['L3 vs Season Trend'].sample_size} without meta"
    )
    # Generic form is allowed at 10 (baseline reliability tier).
    assert by_name["Season Form"].sample_size >= 5
    print("[emission] name-based L3/L5 caps applied without meta ✓")


def test_threshold_features_are_form_category_not_intangible():
    """Prior bug: 'Threshold Distribution Support' did not match any
    keyword in the router, so it was categorized as `intangible` with
    n=1 — starving the strongest per-rung signal of any evidence
    weight.  Assert it now routes to the `form` category.
    """
    pick = {
        "sport": "NFL",
        "factors": {
            "Threshold Distribution Support": 0.88,
            "Historical Threshold Rate":      0.72,
            "L5 Threshold Support":           0.68,
            "L3 Threshold Support":           0.66,
        },
        "factor_sample_sizes": {
            "Threshold Distribution Support": 12,
            "Historical Threshold Rate":      12,
            "L5 Threshold Support":           5,
            "L3 Threshold Support":           3,
        },
    }
    feats = _universal_build_features_from_pick(pick)
    by_name = {f.name: f for f in feats}
    for name in ("Threshold Distribution Support",
                 "Historical Threshold Rate",
                 "L5 Threshold Support",
                 "L3 Threshold Support"):
        assert by_name[name].category == "form", (
            f"{name} categorized as {by_name[name].category} — must be `form`"
        )
    print("[emission] all threshold/distribution/L3/L5 features routed to `form` ✓")


def test_sentinel_keys_are_stripped():
    """`__factor_sample_sizes` and `__rung_p_hat` are internal
    diagnostics.  The universal builder must NOT emit them as
    scoring features.
    """
    pick = {
        "sport": "NFL",
        "factors": {
            "L5 Avg vs Line":       0.72,
            "__factor_sample_sizes": {"L5 Avg vs Line": 5},   # sentinel
            "__rung_p_hat":         0.78,                     # sentinel
        },
    }
    feats = _universal_build_features_from_pick(pick)
    names = [f.name for f in feats]
    for sentinel in ("__factor_sample_sizes", "__rung_p_hat"):
        assert sentinel not in names, (
            f"internal sentinel {sentinel} leaked into features"
        )
    print("[emission] internal sentinel keys correctly stripped ✓")


if __name__ == "__main__":
    test_evidence_features_bind_true_sample_size()
    test_short_window_bounds_without_meta()
    test_threshold_features_are_form_category_not_intangible()
    test_sentinel_keys_are_stripped()
    print("=" * 60)
    print("NFL EVIDENCE-FEATURE SAMPLE-EMISSION CONTRACT · 4/4 PASS")
