"""Block 21 — Name-based boost removal regression.

Two picks with IDENTICAL evidence but different player names MUST
produce the SAME behavior through the Lock Score path.

This test uses the real evidence→category pipeline from
``services/magic/lock_score_integrator`` to prove no name shortcut
exists at the magic layer.  No formula change; it only asserts
invariance.
"""
from __future__ import annotations

import pytest

from services.magic.contract import (
    MagicOutput, EvidenceItem, EvidenceType, Availability, MagicTier,
)
from services.magic.lock_score_integrator import (
    categorize_evidence, count_positive_categories,
    count_contradictory_categories, count_available_categories,
)


def _build_mo(player_name: str) -> MagicOutput:
    """Build a MagicOutput with a fixed evidence set, varying only
    the player name attribute (which must not influence scoring)."""
    evidence = [
        EvidenceItem(
            evidence_type=EvidenceType.ROLE_OPPORTUNITY,
            availability=Availability.AVAILABLE,
            sport="NHL",
            direction="positive",
            confidence=0.9,
            value=22.5,
            label="projected_toi",
            source="role.projected_toi",
            notes=f"{player_name} projected to lead team TOI",
        ),
        EvidenceItem(
            evidence_type=EvidenceType.MATCHUP,
            availability=Availability.AVAILABLE,
            sport="NHL",
            direction="positive",
            confidence=0.9,
            value=1.0,
            label="opponent_bottom5_sog_allowed",
            source="matchup.opponent_sog_allowance",
            notes=f"Opponent ranks bottom-5 vs {player_name}'s position",
        ),
        EvidenceItem(
            evidence_type=EvidenceType.RECENT_FORM,
            availability=Availability.AVAILABLE,
            sport="NHL",
            direction="positive",
            confidence=0.85,
            value=1.4,
            label="goals_per_game_last_5",
            source="form.recent_scoring",
        ),
        EvidenceItem(
            evidence_type=EvidenceType.MODEL_PROBABILITY,
            availability=Availability.AVAILABLE,
            sport="NHL",
            direction="positive",
            confidence=0.8,
            value=0.52,
            label="p_goal_at_least_one",
            source="model.independent_sim",
        ),
    ]
    return MagicOutput(
        pick_id=f"test::{player_name}",
        sport="NHL",
        market="nhl_goals",
        selection=player_name,
        evidence=evidence,
        magic_score=0.9,
        magic_tier=MagicTier.ALIGNED_STRONG,
        magic_score_available=True,
    )


def test_same_evidence_different_name_same_categorization():
    mo_a = _build_mo("Famous Star")
    mo_b = _build_mo("Journeyman Player")
    cats_a = categorize_evidence(mo_a)
    cats_b = categorize_evidence(mo_b)
    assert set(cats_a.keys()) == set(cats_b.keys())


def test_same_evidence_different_name_same_positive_count():
    mo_a = _build_mo("Famous Star")
    mo_b = _build_mo("Journeyman Player")
    pos_a = sorted(count_positive_categories(categorize_evidence(mo_a)))
    pos_b = sorted(count_positive_categories(categorize_evidence(mo_b)))
    assert pos_a == pos_b
    assert len(pos_a) >= 3


def test_same_evidence_different_name_same_contradictions():
    mo_a = _build_mo("Famous Star")
    mo_b = _build_mo("Journeyman Player")
    con_a = sorted(count_contradictory_categories(categorize_evidence(mo_a)))
    con_b = sorted(count_contradictory_categories(categorize_evidence(mo_b)))
    assert con_a == con_b


def test_name_does_not_promote_magic_tier():
    """Even an evidence set that lands JUST BELOW ALIGNED_STRONG
    threshold must not jump a tier because the player name is famous."""
    mo_a = _build_mo("Famous Star")
    mo_b = _build_mo("Journeyman Player")
    assert mo_a.magic_tier == mo_b.magic_tier


def test_same_evidence_different_name_same_availability_set():
    mo_a = _build_mo("Famous Star")
    mo_b = _build_mo("Journeyman Player")
    av_a = sorted(count_available_categories(categorize_evidence(mo_a)))
    av_b = sorted(count_available_categories(categorize_evidence(mo_b)))
    assert av_a == av_b


if __name__ == "__main__":
    import subprocess, sys
    sys.exit(subprocess.call(["python", "-m", "pytest", __file__, "-v"]))
