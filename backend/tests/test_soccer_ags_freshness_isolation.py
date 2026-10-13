"""Block 18 — Soccer AGS Freshness Isolation.

AGS and Score-or-Assist must be measured as independent families.
A fresh Score-or-Assist count MUST NOT mask zero AGS rows.
"""
from __future__ import annotations

import pytest
from datetime import datetime, timezone, timedelta

from services.soccer_ags_freshness_isolator import (
    SOCCER_PLAYER_FAMILIES, ags_needs_refresh,
    STATUS_FRESH, STATUS_STALE, STATUS_EMPTY,
)


def test_ags_needs_refresh_when_empty_despite_fresh_score_or_assist():
    report = {
        "goalscorer_anytime": {"status": STATUS_EMPTY, "count": 0,
                                 "age_minutes": None},
        "goalscorer_score_or_assist": {"status": STATUS_FRESH, "count": 12,
                                         "age_minutes": 5},
    }
    assert ags_needs_refresh(report) is True


def test_ags_does_not_refresh_when_itself_fresh():
    report = {
        "goalscorer_anytime": {"status": STATUS_FRESH, "count": 20,
                                 "age_minutes": 10},
        "goalscorer_score_or_assist": {"status": STATUS_FRESH, "count": 12,
                                         "age_minutes": 5},
    }
    assert ags_needs_refresh(report) is False


def test_ags_refresh_when_stale_even_if_sibling_is_fresh():
    report = {
        "goalscorer_anytime": {"status": STATUS_STALE, "count": 2,
                                 "age_minutes": 500},
        "goalscorer_score_or_assist": {"status": STATUS_FRESH, "count": 12,
                                         "age_minutes": 5},
    }
    assert ags_needs_refresh(report) is True


def test_all_soccer_player_families_declared():
    assert "goalscorer_anytime" in SOCCER_PLAYER_FAMILIES
    assert "goalscorer_score_or_assist" in SOCCER_PLAYER_FAMILIES
    assert "player_shots_on_target" in SOCCER_PLAYER_FAMILIES


if __name__ == "__main__":
    import subprocess, sys
    sys.exit(subprocess.call(["python", "-m", "pytest", __file__, "-v"]))
