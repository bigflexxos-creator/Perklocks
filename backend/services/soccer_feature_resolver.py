"""League-aware Soccer feature resolver — SOCCER_UNIVERSAL_RUNTIME.

Consumes every legitimate existing history / form store in the current
Perklocks database.  Never fabricates statistics.  Returns a normalized
feature dict PLUS a precise taxonomy code describing which stage
failed when no evidence is found.

Resolution chain (all league-aware; sample-size honest):

    1. `soccer_player_form`               — Understat / SportDB / ESPN
       pre-aggregated form (2,774 rows across Big-5 + top MLS players).
    2. `player_game_actuals` aggregation  — 305,132-row universal actuals
       store; filter to `sport="soccer"` + `player_name` match; aggregate
       recent N appearances into rolling goals / assists / shots /
       shots-on-target rates.  This is what unlocks Messi / Evander /
       Bouanga / Suárez etc. — they exist here even when
       `soccer_player_form` is empty for MLS.
    3. `soccer_player_game_logs` aggregation — 50,112-row per-fixture
       logs (canonicalized by short name — used when the actuals store
       doesn't cover the league).

Each hit returns an ``evidence_source`` label so downstream
attribution / diagnostics can report exactly which layer produced the
row.
"""
from __future__ import annotations
from typing import Any, Optional

from services.soccer_season_resolver import (
    resolve_current_season, resolve_prior_season,
)
from services.soccer_historical_stats import aggregate_player_season


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────
async def resolve_soccer_player_features(
    db, *, player_name: str, league: str,
) -> tuple[Optional[dict], str]:
    """Return ``(feature_row, evidence_source)`` for the player.

    ``feature_row`` matches the schema expected by
    :func:`services.soccer_scorer_bridge.compute_soccer_scorer_factors_sync`.
    """
    if not player_name:
        return None, ""
    nc = player_name.strip().lower()

    # ── 1.  soccer_player_form (pre-aggregated) ─────────────────
    row = await db.soccer_player_form.find_one({"name_canonical": nc})
    if row and int(row.get("minutes") or 0) >= 90:
        return row, "soccer_player_form"

    # ── 2.  player_game_actuals rolling aggregate ──────────────
    # 305k-row universal store, populated by History/backfill.
    # Filter to Soccer + fuzzy player_name match.  Aggregate the
    # last 25 appearances into a normalized form dict.
    try:
        agg = await _aggregate_from_actuals(db, player_name=player_name)
    except Exception:
        agg = None
    if agg and (agg.get("minutes") or 0) >= 90:
        return agg, "player_game_actuals"

    # ── 3.  soccer_player_game_logs current-season aggregate ───
    if league:
        try:
            cur_season = resolve_current_season(league)
            g = await aggregate_player_season(
                db, player_name_canonical=nc, season=cur_season,
            )
            if g and int(g.get("minutes") or 0) >= 180:
                return g, "logs_current_season"
        except Exception:
            pass
        try:
            prior_season = resolve_prior_season(league)
            g2 = await aggregate_player_season(
                db, player_name_canonical=nc, season=prior_season,
            )
            if g2 and int(g2.get("minutes") or 0) >= 400:
                return g2, "logs_prior_season"
        except Exception:
            pass

    # ── 4.  Fall through — return whichever we found (even sparse) ─
    if row:
        return row, "soccer_player_form"
    if agg:
        return agg, "player_game_actuals"
    return None, ""


async def resolve_soccer_player_prior(
    db, *, player_name: str, league: str,
) -> Optional[dict]:
    """Prior-season aggregate for empirical-Bayes blending."""
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


async def resolve_soccer_player_matchup(
    db, *, player_name: str, opponent_team: str,
) -> Optional[dict]:
    """Return H2H matchup dossier for player-vs-opponent, or None.

    Uses `mls_player_matchup_history` — the existing MLS matchup
    store populated by prior History backfill work.  Structure:
    ``{player_name, by_opponent: [ ... ], total_events, refreshed_at}``.
    """
    if not (player_name and opponent_team):
        return None
    try:
        row = await db.mls_player_matchup_history.find_one({
            "player_name": {"$regex": f"^{player_name.strip()}$", "$options": "i"},
        })
    except Exception:
        row = None
    if not row:
        return None
    bo = row.get("by_opponent")
    # by_opponent can be a list of dicts OR a dict keyed by opponent —
    # accept both shapes.
    match_data: Optional[dict] = None
    if isinstance(bo, list):
        for item in bo:
            if not isinstance(item, dict):
                continue
            opp = (item.get("opponent") or item.get("team") or "").strip().lower()
            if opp == opponent_team.strip().lower():
                match_data = item
                break
    elif isinstance(bo, dict):
        for k, v in bo.items():
            if k.strip().lower() == opponent_team.strip().lower():
                match_data = v if isinstance(v, dict) else {"opponent": k, "data": v}
                break
    if not match_data:
        return None
    return {
        "opponent":        opponent_team,
        "events":          int(match_data.get("events") or match_data.get("total_events") or 0),
        "goals":           float(match_data.get("goals") or 0),
        "assists":         float(match_data.get("assists") or 0),
        "shots":           float(match_data.get("shots") or 0),
        "shots_on_target": float(match_data.get("shots_on_target") or 0),
        "source":          "mls_player_matchup_history",
    }


# ─────────────────────────────────────────────────────────────────────
# Rejection classification — replaces the 783-row MISSING_FEATURE_DATA
# black-hole with precise per-stage codes.
# ─────────────────────────────────────────────────────────────────────
async def classify_missing_feature_reason(
    db, *, player_name: str, league: str,
) -> str:
    """Return a taxonomy code describing exactly why the resolver
    could not produce evidence for this player."""
    from services.soccer_rejection_taxonomy import SoccerRejection

    if not player_name:
        return SoccerRejection.PLAYER_IDENTITY_FAILURE.value
    nc = player_name.strip().lower()

    form = await db.soccer_player_form.find_one({"name_canonical": nc})
    n_actuals = 0
    try:
        n_actuals = await db.player_game_actuals.count_documents({
            "sport":       "soccer",
            "player_name": {"$regex": f"^{player_name.strip()}$", "$options": "i"},
        })
    except Exception:
        pass
    n_logs = 0
    try:
        n_logs = await db.soccer_player_game_logs.count_documents({
            "name_canonical": nc,
        })
    except Exception:
        pass

    # No trace anywhere → identity failure (or truly unknown player).
    if not form and n_actuals == 0 and n_logs == 0:
        return SoccerRejection.PLAYER_IDENTITY_FAILURE.value

    # Some history, but < minimum sample → precise reason.
    if form and int(form.get("minutes") or 0) < 90:
        return "NO_RECENT_FORM"
    if n_actuals > 0 and n_actuals < 3:
        return "NO_RECENT_FORM"
    if not form and n_actuals == 0 and n_logs > 0:
        # game logs exist but under a different canonical name mapping
        return "PLAYER_IDENTITY_FAILURE"
    return "NO_PLAYER_HISTORY"


# ─────────────────────────────────────────────────────────────────────
# Actuals aggregation — pure DB read, no fabrication.
# ─────────────────────────────────────────────────────────────────────
async def _aggregate_from_actuals(
    db, *, player_name: str, sample: int = 25,
) -> Optional[dict]:
    """Aggregate the last ``sample`` Soccer entries from
    ``player_game_actuals`` into a form-row-shaped dict.

    Population size: 305,132 rows across sports.  Currently populated
    for many MLS players (Messi/Evander/Bouanga/Suárez/etc), plus
    other leagues that the History backfill has hit.
    """
    q = {
        "sport": "soccer",
        "player_name": {"$regex": f"^{player_name.strip()}$", "$options": "i"},
    }
    docs = await db.player_game_actuals.find(q).sort(
        [("event_time", -1)]
    ).limit(sample).to_list(sample)
    if not docs:
        return None

    goals = 0.0
    assists = 0.0
    shots = 0.0
    sot = 0.0
    minutes_est = 0.0
    n = 0
    for d in docs:
        a = d.get("actuals") or {}
        # Actuals may store None for shots_on_target — skip nulls.
        goals   += float(a.get("goals")   or 0)
        assists += float(a.get("assists") or 0)
        shots   += float(a.get("shots")   or 0)
        sot_v = a.get("shots_on_target")
        if sot_v is not None:
            sot += float(sot_v)
        # Assume ~90 minutes per appearance since minutes are not
        # in the actuals store — this is a documented approximation
        # (the bridge's shrinkage handles the noise).
        minutes_est += 90.0
        n += 1
    if n == 0:
        return None
    return {
        "name_canonical":     player_name.strip().lower(),
        "player_name":        player_name,
        "goals":              goals,
        "assists":            assists,
        "shots":              shots,
        "shots_on_target":    sot,
        "games":              n,
        "minutes":            int(minutes_est),
        "goals_per_90":       (goals * 90.0) / minutes_est if minutes_est else 0.0,
        "assists_per_90":     (assists * 90.0) / minutes_est if minutes_est else 0.0,
        "shots_per_90":       (shots * 90.0) / minutes_est if minutes_est else 0.0,
        "source":             "player_game_actuals",
        "sample_size":        n,
    }


__all__ = [
    "resolve_soccer_player_features",
    "resolve_soccer_player_prior",
    "resolve_soccer_player_matchup",
    "classify_missing_feature_reason",
]
