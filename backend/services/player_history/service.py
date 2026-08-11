"""Player History service — top-level dispatcher.

Routes by ``sport`` to the appropriate sport adapter and returns
the universal ``PlayerHistoryEvidence`` contract.

Stage 1 (this session): MLB only.
Stage 2 (deferred): NFL / NBA / Soccer / Tennis / UFC.
Stage 3 (deferred): Team History.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .models import PlayerHistoryEvidence, DataQuality


async def get_player_history(
    db,
    *,
    sport: str,
    player_id: Optional[str] = None,
    canonical_player_id: Optional[str] = None,
    player_name: Optional[str] = None,
    market: str,
    threshold: float,
    direction: str = "over",
    opponent: Optional[str] = None,
    event_time: Optional[str] = None,     # ISO — history_as_of cutoff
    home_away: Optional[str] = None,       # "home" | "away" | None
    current_team: Optional[str] = None,   # Phase 5.3 §19 — current team
    context: Optional[dict] = None,
) -> PlayerHistoryEvidence:
    """Universal Magic-ready history request.

    Returns ``PlayerHistoryEvidence`` with:
      * Every unavailable field → ``None`` (never 0).
      * Every game with a missing actual excluded from decisions.
      * Only games strictly before ``event_time`` considered
        (no future-data leakage).
      * ``historical_team`` and ``current_team`` reported
        separately (current_team NEVER overwritten by history).

    Adapters are loaded lazily to avoid circular imports.
    """
    sport_u = (sport or "").upper()
    ev = PlayerHistoryEvidence(
        sport=sport_u,
        player_id=player_id,
        canonical_player_id=canonical_player_id,
        player_name=player_name,
        current_team=current_team,
        market=market,
        threshold=float(threshold) if threshold is not None else None,
        direction=direction,
        history_as_of=event_time or datetime.now(timezone.utc).isoformat(),
    )
    if sport_u == "MLB":
        from .mlb import populate_mlb_evidence
        return await populate_mlb_evidence(
            db, ev,
            player_id=player_id,
            canonical_player_id=canonical_player_id,
            player_name=player_name,
            opponent=opponent,
            home_away=home_away,
        )
    if sport_u == "NFL":
        from .nfl import populate_nfl_evidence
        return await populate_nfl_evidence(
            db, ev,
            player_id=player_id,
            canonical_player_id=canonical_player_id,
            player_name=player_name,
            opponent=opponent,
            home_away=home_away,
        )
    if sport_u == "NBA":
        from .nba import populate_nba_evidence
        return await populate_nba_evidence(
            db, ev,
            player_id=player_id,
            canonical_player_id=canonical_player_id,
            player_name=player_name,
            opponent=opponent,
            home_away=home_away,
        )
    if sport_u == "SOCCER":
        from .soccer import populate_soccer_evidence
        return await populate_soccer_evidence(
            db, ev,
            player_id=player_id,
            canonical_player_id=canonical_player_id,
            player_name=player_name,
            opponent=opponent,
            home_away=home_away,
        )
    if sport_u == "TENNIS":
        from .tennis import populate_tennis_evidence
        return await populate_tennis_evidence(
            db, ev,
            player_id=player_id,
            canonical_player_id=canonical_player_id,
            player_name=player_name,
            opponent=opponent,
            home_away=home_away,
        )
    if sport_u == "UFC":
        from .ufc import populate_ufc_evidence
        return await populate_ufc_evidence(
            db, ev,
            player_id=player_id,
            canonical_player_id=canonical_player_id,
            player_name=player_name,
            opponent=opponent,
            home_away=home_away,
        )
    # Sports that legitimately do not participate in Stage 2 (CFB /
    # NHL — no player-prop coverage per capability registry) return
    # an honest UNAVAILABLE — never a fake PASS (§14).
    ev.data_quality = DataQuality.UNAVAILABLE.value
    ev.source = "SPORT_NOT_SUPPORTED"
    return ev


__all__ = ["get_player_history"]
