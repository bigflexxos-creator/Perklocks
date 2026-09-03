"""STEP 6 · MLB alt run-line model-before-edge ordering
============================================================

`quality_gate.apply_quality_gate` used to reject MLB alt run-line /
team-total picks solely on `edge_percent` — even for picks that had
not yet been scored by the specialized MLB model (i.e. the edge value
was a pre-model estimate or default 0). That caused authoritative
alt-line candidates to disappear before the model computed their real
edge.

Fix (STEP 6): gate the alt-line edge check on the presence of an
AUTHORITATIVE model probability (`model_win_prob` /
`win_probability` / `published_probability`). If no model probability
exists, the alt-line edge gate is skipped and the pick continues
through the pipeline where the authoritative model runs.
"""
from __future__ import annotations
import os, sys
import pytest

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from quality_gate import _block_reason


def _pick(**kwargs):
    return {
        "sport": "mlb",
        "market": "Alt Run Line +2.5",
        "is_alt": True,
        "edge_percent": 5.0,   # below the 8% floor
        "lock_score": 92,
        "grade": "Strong Lock",
        **kwargs,
    }


def test_step6_alt_run_line_gate_skipped_when_no_model_prob():
    """No authoritative model probability → edge gate must be skipped
    so downstream can run the model and compute a real edge."""
    p = _pick()
    reason = _block_reason(p)
    assert reason is None or "run_line" not in (reason or ""), (
        f"STEP 6: alt run-line edge gate fired without a model "
        f"probability — pre-model rejection regression. reason={reason!r}"
    )


def test_step6_alt_run_line_gate_fires_when_model_prob_present():
    """When authoritative model prob IS set and edge is below the
    8% floor, the historical gate still fires (behavior preserved)."""
    p = _pick(model_win_prob=0.55, edge_percent=4.0)
    reason = _block_reason(p)
    assert reason and "run_line" in reason, (
        f"STEP 6: alt run-line gate must still fire on authoritative "
        f"below-8% edge. reason={reason!r}"
    )


def test_step6_alt_run_line_passes_when_authoritative_edge_ok():
    p = _pick(model_win_prob=0.60, edge_percent=12.0)
    reason = _block_reason(p)
    if reason and "run_line" in reason:
        pytest.fail(
            f"STEP 6: alt run-line with authoritative 12% edge should "
            f"pass the model-before-edge gate. reason={reason!r}"
        )


def test_step6_alt_team_total_gate_also_guarded():
    """The same guard applies to the sibling alt-team-total gate."""
    p = _pick(market="Team Total Over 3.5")
    reason = _block_reason(p)
    assert reason is None or "team_total" not in (reason or ""), (
        f"STEP 6: alt team-total gate fired without model prob. "
        f"reason={reason!r}"
    )
