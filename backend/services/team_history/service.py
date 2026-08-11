"""Team History dispatcher (Phase 5.3 Stage 3).

Supported team sports:
    MLB, NFL, NBA, Soccer, NHL, CFB

NOT_APPLICABLE (§2):
    Tennis, UFC — the dispatcher returns a Team-History evidence
    object with ``status = NOT_APPLICABLE`` so callers can honour
    the same universal contract without inventing team semantics.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .models import (
    TeamHistoryEvidence,
    TeamHistoryStatus,
    TeamHistoryQuality,
)
from ._shared import populate_team_history
from .adapters import (
    mlb_context,
    nfl_context,
    nba_context,
    soccer_context,
    nhl_context,
    cfb_context,
)


SUPPORTED_TEAM_SPORTS = {"MLB", "NFL", "NBA", "SOCCER", "NHL", "CFB"}
NOT_APPLICABLE_SPORTS = {"TENNIS", "UFC"}


async def get_team_history(
    db,
    *,
    sport: str,
    canonical_team_id: Optional[str] = None,
    team_name: Optional[str] = None,
    opponent_id: Optional[str] = None,
    home_away: Optional[str] = None,
    competition: Optional[str] = None,
    metric: Optional[str] = None,
    as_of: Optional[str] = None,
) -> TeamHistoryEvidence:
    """Universal team-history query.

    Returns a fully-populated ``TeamHistoryEvidence`` object.  When
    the sport is not applicable (Tennis / UFC) the evidence status
    is ``NOT_APPLICABLE`` — never a fake PASS (§2 / §14).
    """
    sport_u = (sport or "").upper()
    ev = TeamHistoryEvidence(
        canonical_team_id=canonical_team_id,
        canonical_opponent_id=opponent_id,
        team_name=team_name,
        sport=sport_u,
        metric=metric,
        home_away=home_away,
        competition=competition,
        as_of=as_of or datetime.now(timezone.utc).isoformat(),
    )

    if sport_u in NOT_APPLICABLE_SPORTS:
        ev.status = TeamHistoryStatus.NOT_APPLICABLE.value
        ev.data_quality = TeamHistoryQuality.UNAVAILABLE.value
        ev.source = "SPORT_NOT_APPLICABLE"
        return ev

    if sport_u not in SUPPORTED_TEAM_SPORTS:
        # Anything else (e.g. an unenabled sport) → UNAVAILABLE.
        ev.status = TeamHistoryStatus.UNAVAILABLE.value
        ev.data_quality = TeamHistoryQuality.UNAVAILABLE.value
        ev.source = "SPORT_NOT_SUPPORTED"
        return ev

    # Identity gate (§3) — a query with no canonical id and no
    # team_name cannot be resolved.  Fail honestly.
    if not canonical_team_id and not team_name:
        ev.status = TeamHistoryStatus.IDENTITY_UNRESOLVED.value
        ev.data_quality = TeamHistoryQuality.UNAVAILABLE.value
        ev.source = "IDENTITY_UNRESOLVED"
        return ev

    context_fn = {
        "MLB":    mlb_context,
        "NFL":    nfl_context,
        "NBA":    nba_context,
        "SOCCER": soccer_context,
        "NHL":    nhl_context,
        "CFB":    cfb_context,
    }[sport_u]

    return await populate_team_history(
        db, ev,
        sport=sport_u,
        canonical_team_id=canonical_team_id,
        team_name=team_name,
        opponent_id=opponent_id,
        home_away=home_away,
        competition=competition,
        row_context_fn=context_fn,
    )


__all__ = [
    "get_team_history",
    "SUPPORTED_TEAM_SPORTS",
    "NOT_APPLICABLE_SPORTS",
]
