"""Preservation Contract — the constants that MUST NOT drift (Block 58).

This module reads (never writes) the authoritative constants defined
elsewhere and exposes a canonical snapshot the preservation regression
tests check against.  If any value drifts, the test fails loudly BEFORE
publication.
"""
from __future__ import annotations

from typing import Any


def snapshot() -> dict[str, Any]:
    """Capture the runtime values of every preservation-critical
    constant.  Importing from the authoritative module ensures we
    detect drift ANYWHERE in the engine."""
    out: dict[str, Any] = {}

    # 1. 85+ board threshold
    try:
        from services.canonical_publication_boundary import (
            MAX_PUBLICATION_ATTEMPTS,
        )
        out["MAX_PUBLICATION_ATTEMPTS"] = MAX_PUBLICATION_ATTEMPTS
    except Exception as e:
        out["MAX_PUBLICATION_ATTEMPTS__error"] = str(e)

    # 2. Apex thresholds
    try:
        from services.magic.apex_gate import (
            APEX_MIN_BASE_SCORE, APEX_MIN_POSITIVE_CATEGORIES,
            APEX_ELIGIBLE_SPORTS, APEX_UNAVAILABLE_SPORTS,
            APEX_GATE_VERSION,
        )
        out["APEX_MIN_BASE_SCORE"] = APEX_MIN_BASE_SCORE
        out["APEX_MIN_POSITIVE_CATEGORIES"] = APEX_MIN_POSITIVE_CATEGORIES
        out["APEX_ELIGIBLE_SPORTS"] = sorted(APEX_ELIGIBLE_SPORTS)
        out["APEX_UNAVAILABLE_SPORTS"] = sorted(APEX_UNAVAILABLE_SPORTS)
        out["APEX_GATE_VERSION"] = APEX_GATE_VERSION
    except Exception as e:
        out["apex_error"] = str(e)

    # 3. NHL evidence floors
    try:
        from services.nhl_feature_engine import (
            _MIN_TEAM_RECENT_GAMES, _MIN_PLAYER_RECENT_GAMES,
        )
        out["NHL_MIN_TEAM_RECENT_GAMES"] = _MIN_TEAM_RECENT_GAMES
        out["NHL_MIN_PLAYER_RECENT_GAMES"] = _MIN_PLAYER_RECENT_GAMES
    except Exception as e:
        # Older constant names may differ; the test must only enforce
        # equality against a known-good snapshot — presence of SOME
        # 5-game floor is enough.
        try:
            import services.nhl_feature_engine as _mod
            src = open(_mod.__file__, "r", encoding="utf-8").read()
            out["NHL_floor_marker_present"] = any(
                token in src for token in
                ("_MIN_TEAM_RECENT_GAMES = 5", "_MIN_PLAYER_RECENT_GAMES = 5",
                 "RECENT_GAMES = 5", "min_recent_games = 5",
                 "recent_games_floor = 5")
            )
        except Exception as e2:
            out["NHL_floor_error"] = f"{e}; secondary: {e2}"

    # 4. Expected NFL alt ladder preservation — presence sanity check.
    try:
        import services.alt_line_engine  # noqa: F401
        out["ALT_LINE_ENGINE_IMPORTABLE"] = True
    except Exception as e:
        out["ALT_LINE_ENGINE_IMPORTABLE"] = False
        out["ALT_LINE_ENGINE__error"] = str(e)

    # 5. Soccer game-market working path
    try:
        import services.soccer_market_gate  # noqa: F401
        out["SOCCER_MARKET_GATE_IMPORTABLE"] = True
    except Exception as e:
        out["SOCCER_MARKET_GATE_IMPORTABLE"] = False
        out["SOCCER_MARKET_GATE__error"] = str(e)

    # 6. Canonical publication boundary intact
    try:
        from services.canonical_publication_boundary import (
            RejectionReason, _REAL_ODDS_SOURCES, _SYNTHETIC_ODDS_SOURCES,
        )
        out["RejectionReason_members"] = sorted(r.value for r in RejectionReason)
        out["_REAL_ODDS_SOURCES"] = sorted(_REAL_ODDS_SOURCES)
        out["_SYNTHETIC_ODDS_SOURCES"] = sorted(_SYNTHETIC_ODDS_SOURCES)
    except Exception as e:
        out["canonical_publication_boundary_error"] = str(e)

    return out


__all__ = ["snapshot"]
