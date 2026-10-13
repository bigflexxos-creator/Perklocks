"""Block 45 — Universal funnel health reasoner.

If the funnel shows provider_outcomes > 0, model_candidates > 0,
at_or_above_85 > 0, but published == 0 AND SYNTHETIC_BOOK_ODDS
dominates: report ``PUBLICATION_PROVENANCE_FAILURE``, NOT
``HEALTHY_NO_QUALIFIED_PICKS``.
"""
from __future__ import annotations

import pytest

from services.universal_funnel_telemetry import (
    diagnose_funnel, summarize_universal,
)


def test_publication_provenance_failure_dominates():
    funnel = {
        "provider_events":   10,
        "provider_outcomes": 50,
        "canonicalized":     40,
        "model_candidates":  35,
        "at_or_above_85":    10,
        "published":          0,
        "rejections": {
            "SYNTHETIC_BOOK_ODDS": 10,
        },
    }
    assert diagnose_funnel(funnel) == "PUBLICATION_PROVENANCE_FAILURE"


def test_healthy_when_rows_publish():
    funnel = {
        "provider_events":   10,
        "provider_outcomes": 50,
        "canonicalized":     40,
        "model_candidates":  35,
        "at_or_above_85":    10,
        "published":          8,
        "rejections": {"SYNTHETIC_BOOK_ODDS": 2},
    }
    assert diagnose_funnel(funnel) == "HEALTHY"


def test_model_unavailable_bucket():
    funnel = {
        "provider_events":   10,
        "provider_outcomes": 50,
        "canonicalized":     40,
        "model_candidates":  0,
        "rejections": {"MODEL_UNAVAILABLE": 40},
    }
    assert diagnose_funnel(funnel) == "MODEL_UNAVAILABLE"


def test_sport_refresh_starved():
    funnel = {"provider_events": 0}
    assert diagnose_funnel(funnel) == "SPORT_REFRESH_STARVED"


def test_normalization_lag_distinct_from_coverage():
    funnel = {
        "provider_events":   10,
        "provider_outcomes": 50,
        "canonicalized":     0,
    }
    assert diagnose_funnel(funnel) == "NORMALIZATION_LAG"


def test_summarize_universal_lists_unhealthy():
    data = {
        ("mlb", "hitter_hits"): {
            "provider_events": 5, "provider_outcomes": 20,
            "canonicalized": 20, "model_candidates": 15,
            "at_or_above_85": 5, "published": 0,
            "rejections": {"SYNTHETIC_BOOK_ODDS": 5},
        },
        ("nfl", "qb_passing_yards"): {
            "provider_events": 2, "provider_outcomes": 10,
            "canonicalized": 10, "model_candidates": 8,
            "at_or_above_85": 3, "published": 3,
            "rejections": {},
        },
    }
    summary = summarize_universal(data)
    assert summary["total"] == 2
    unhealthy_keys = {(s, f) for (s, f, _h) in summary["unhealthy"]}
    assert ("mlb", "hitter_hits") in unhealthy_keys
    assert ("nfl", "qb_passing_yards") not in unhealthy_keys


if __name__ == "__main__":
    import subprocess, sys
    sys.exit(subprocess.call(["python", "-m", "pytest", __file__, "-v"]))
