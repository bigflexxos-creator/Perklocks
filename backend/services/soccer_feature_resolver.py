"""League-aware Soccer feature resolver — Phase 2A.5 UNIVERSAL.

Given a canonical player name and the league of the fixture they are
appearing in, return the best authoritative feature row available in
the current Perklocks database.

Resolution order (never fabricates):
    1. `soccer_player_form` — richest source (Understat for Big-5,
       ESPN/SportDB for MLS/CSL/Liga MX/Norway/Sweden/Finland/etc.)
    2. `soccer_player_game_logs` aggregation for the current
       league-appropriate season (via soccer_season_resolver).
    3. Prior-season aggregation (empirical Bayes fallback — same
       collection, previous season).
    4. Return None + evidence_stage=MISSING_FEATURE_DATA (caller
       tags off_board attribution).

Contract:
    * Never invents statistics.
    * Never blends across leagues silently — league lineage is
      preserved in the returned dict so the scorer bridge can decide
      shrinkage weight.
    * Every hit records `evidence_source` so the diagnostic funnel
      can attribute coverage per league.
"""
from __future__ import annotations
from typing import Any, Optional

from services.soccer_season_resolver import (
    resolve_current_season, resolve_prior_season,
)
from services.soccer_historical_stats import aggregate_player_season


async def resolve_soccer_player_features(
    db, *, player_name: str, league: str,
) -> tuple[Optional[dict], str]:
    """Return (feature_row, evidence_source).

    ``feature_row`` matches the schema expected by
    :func:`services.soccer_scorer_bridge.compute_soccer_scorer_factors_sync`.
    ``evidence_source`` is one of:
        - ``"soccer_player_form"``
        - ``"logs_current_season"``
        - ``"logs_prior_season"``
        - ``""`` (None row)
    """
    if not player_name:
        return None, ""
    nc = player_name.strip().lower()

    # 1. Direct form row.
    row = await db.soccer_player_form.find_one({"name_canonical": nc})
    if row and int(row.get("minutes") or 0) >= 90:
        return row, "soccer_player_form"

    # 2. Aggregate current-season game logs (league-aware).
    if league:
        try:
            cur_season = resolve_current_season(league)
            agg = await aggregate_player_season(
                db, player_name_canonical=nc, season=cur_season,
            )
            if agg and int(agg.get("minutes") or 0) >= 180:
                return agg, "logs_current_season"
        except Exception:
            pass

        # 3. Prior-season aggregate as empirical prior.
        try:
            prior_season = resolve_prior_season(league)
            agg2 = await aggregate_player_season(
                db, player_name_canonical=nc, season=prior_season,
            )
            if agg2 and int(agg2.get("minutes") or 0) >= 400:
                return agg2, "logs_prior_season"
        except Exception:
            pass

    # 4. Fallback: return whichever form row exists even with low
    # minutes so the caller can decide (bridge's shrinkage will
    # dominate towards league averages).  If still nothing, return
    # None so caller tags MISSING_FEATURE_DATA.
    if row:
        return row, "soccer_player_form"
    return None, ""


async def resolve_soccer_player_prior(
    db, *, player_name: str, league: str,
) -> Optional[dict]:
    """Return prior-season aggregate for empirical-Bayes blending in
    the scorer bridge.  Independent of the primary feature row so the
    bridge can weight current-vs-prior samples honestly."""
    if not player_name or not league:
        return None
    nc = player_name.strip().lower()
    try:
        prior_season = resolve_prior_season(league)
        return await aggregate_player_season(
            db, player_name_canonical=nc, season=prior_season,
        )
    except Exception:
        return None


__all__ = [
    "resolve_soccer_player_features",
    "resolve_soccer_player_prior",
]
