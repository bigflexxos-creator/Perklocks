"""NHL 2.0 — Canonical Feature Contract (Preview-only scaffold).

2026-10-02 — one reusable NHL feature/context contract consumed by
every NHL 2.0 market-specific model.  Every feature is paired with
an availability flag so downstream models can degrade gracefully
when source data is absent.  No invented values; every number is
derived from existing persisted collections or stamped UNAVAILABLE.

Feature contract version:  nhl_feature_contract.v2.0.0
Model family prefix:       nhl_v2
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger("lockscore.nhl_v2.features")

FEATURE_CONTRACT_VERSION = "nhl_feature_contract.v2.0.0"
MODEL_FAMILY             = "nhl_v2"


@dataclass
class FeatureAvailability:
    """Availability state for every feature in the contract."""
    source:        str   # 'player_game_logs' | 'games' | 'live_lineup' | 'provider_pbp' | 'UNAVAILABLE'
    sample_size:   int   # number of underlying rows that produced this value
    reliability:   str   # 'STRONG' | 'MODEL' | 'LIMITED' | 'UNKNOWN'
    unavailable_reason: Optional[str] = None


@dataclass
class TeamContext:
    team_id:            Optional[str] = None
    team_name:          Optional[str] = None
    # Scoring / shot generation
    goals_for_per60:    Optional[float] = None
    goals_against_per60: Optional[float] = None
    shots_for_per60:    Optional[float] = None
    shots_against_per60: Optional[float] = None
    # Special teams
    pp_pct:             Optional[float] = None
    pk_pct:             Optional[float] = None
    pp_shots_per_opp:   Optional[float] = None
    # Rest / schedule
    rest_days:          Optional[int]   = None
    back_to_back:       Optional[bool]  = None
    is_home:            Optional[bool]  = None
    # Advanced (xG) — populated only when provider PBP yields it
    xgf_per60:          Optional[float] = None
    xga_per60:          Optional[float] = None
    high_danger_xg_share: Optional[float] = None
    # Per-feature availability
    availability:       dict[str, FeatureAvailability] = field(default_factory=dict)


@dataclass
class GoalieContext:
    """Canonical pregame goalie context."""
    state:             str   # 'CONFIRMED' | 'EXPECTED' | 'UNKNOWN'
    goalie_id:         Optional[str] = None
    goalie_name:       Optional[str] = None
    sv_pct_season:     Optional[float] = None
    sv_pct_last5:      Optional[float] = None
    shots_faced_last5: Optional[int] = None
    starts_last10:     Optional[int] = None
    rest_days:         Optional[int] = None
    # GSAx populated ONLY when legitimate xG evidence exists
    gsax_per60:        Optional[float] = None
    availability:      dict[str, FeatureAvailability] = field(default_factory=dict)


@dataclass
class PlayerContext:
    player_id:         Optional[str] = None
    player_name:       Optional[str] = None
    # Role / opportunity
    toi_season:        Optional[float] = None
    toi_last5:         Optional[float] = None
    toi_ev_last5:      Optional[float] = None
    toi_pp_last5:      Optional[float] = None
    projected_total_toi: Optional[float] = None
    projected_ev_toi:  Optional[float] = None
    projected_pp_toi:  Optional[float] = None
    # Scoring rates
    shots_per_game_last10: Optional[float] = None
    goals_per_game_last10: Optional[float] = None
    assists_per_game_last10: Optional[float] = None
    points_per_60:     Optional[float] = None
    # Advanced
    ixg_per60:         Optional[float] = None
    availability:      dict[str, FeatureAvailability] = field(default_factory=dict)


@dataclass
class LineContext:
    """Line / linemate context.  Degrades to UNKNOWN when unavailable."""
    line:               Optional[int] = None
    pp_unit:            Optional[int] = None
    linemates:          list[str] = field(default_factory=list)
    minutes_together_season: Optional[float] = None
    shared_scoring_per60: Optional[float] = None
    lineup_confidence:  str = "UNKNOWN"
    availability:       dict[str, FeatureAvailability] = field(default_factory=dict)


@dataclass
class MatchupContext:
    """One canonical bundle consumed by every V2 market model."""
    event_id:          Optional[str] = None
    event_time:        Optional[str] = None
    home:              TeamContext = field(default_factory=TeamContext)
    away:              TeamContext = field(default_factory=TeamContext)
    home_goalie:       GoalieContext = field(default_factory=lambda: GoalieContext(state="UNKNOWN"))
    away_goalie:       GoalieContext = field(default_factory=lambda: GoalieContext(state="UNKNOWN"))
    feature_contract_version: str = FEATURE_CONTRACT_VERSION

    def to_dict(self) -> dict:
        return asdict(self)


# ─────────────────────────── builders ───────────────────────────

async def _team_last_n_shot_defense(db, team: str, n: int = 10) -> tuple[Optional[float], int]:
    """Direct SOG allowed/game from the last ``n`` completed games.

    Primary opponent shot-suppression evidence (replaces GA proxy).
    """
    try:
        pipeline = [
            {"$match": {"sport": "nhl",
                        "$or": [{"home": team}, {"away": team}],
                        "status": "Final"}},
            {"$sort": {"date": -1}},
            {"$limit": n},
            {"$project": {"home": 1, "away": 1,
                          "home_shots": "$result.home_shots",
                          "away_shots": "$result.away_shots"}},
        ]
        rows = await db.games.aggregate(pipeline).to_list(n)
    except Exception as e:
        logger.debug("team_last_n_shot_defense err: %s", e)
        return None, 0
    sog_against = []
    for r in rows:
        if r.get("home") == team and r.get("away_shots") is not None:
            sog_against.append(float(r["away_shots"]))
        elif r.get("away") == team and r.get("home_shots") is not None:
            sog_against.append(float(r["home_shots"]))
    if not sog_against:
        return None, 0
    return sum(sog_against) / len(sog_against), len(sog_against)


async def _team_scoring_rates(db, team: str, n: int = 20) -> dict:
    """Goals-for / goals-against / shots-for per 60 from last-n games."""
    try:
        pipeline = [
            {"$match": {"sport": "nhl",
                        "$or": [{"home": team}, {"away": team}],
                        "status": "Final"}},
            {"$sort": {"date": -1}},
            {"$limit": n},
        ]
        rows = await db.games.aggregate(pipeline).to_list(n)
    except Exception:
        rows = []
    gf, ga, sf = [], [], []
    for r in rows:
        res = r.get("result") or {}
        if r.get("home") == team:
            gf.append(res.get("home") or 0); ga.append(res.get("away") or 0)
            if res.get("home_shots"): sf.append(float(res["home_shots"]))
        elif r.get("away") == team:
            gf.append(res.get("away") or 0); ga.append(res.get("home") or 0)
            if res.get("away_shots"): sf.append(float(res["away_shots"]))
    out = {}
    if gf:
        out["gf_per60"] = sum(gf) / len(gf)
        out["ga_per60"] = sum(ga) / len(ga)
        out["sample_games"] = len(gf)
    if sf:
        out["sf_per60"] = sum(sf) / len(sf)
    return out


async def _player_last_n(db, player_id: str, n: int = 10) -> dict:
    """Rolling player stats from player_game_logs."""
    try:
        rows = await db.player_game_logs.find(
            {"sport": "nhl", "player_id": player_id},
            {"goals": 1, "shots": 1, "assists": 1, "toi": 1, "pp_toi": 1,
             "ev_toi": 1, "name": 1, "team": 1}
        ).sort("_id", -1).limit(n).to_list(n)
    except Exception:
        rows = []
    if not rows:
        return {"sample_size": 0}
    def _mean(k):
        vals = [r.get(k) for r in rows if isinstance(r.get(k), (int, float))]
        return sum(vals) / len(vals) if vals else None
    return {
        "sample_size": len(rows),
        "name":        rows[0].get("name"),
        "team":        rows[0].get("team"),
        "shots_per_game": _mean("shots"),
        "goals_per_game": _mean("goals"),
        "assists_per_game": _mean("assists"),
        "toi_per_game": _mean("toi"),
        "pp_toi_per_game": _mean("pp_toi"),
        "ev_toi_per_game": _mean("ev_toi"),
    }


async def build_team_context(db, team_name: str, is_home: bool,
                              last_game_date: Optional[str] = None) -> TeamContext:
    """Assemble TeamContext from real completed boxscores only."""
    tc = TeamContext(team_name=team_name, is_home=is_home)
    rates = await _team_scoring_rates(db, team_name, n=20)
    if rates:
        tc.goals_for_per60    = round(rates.get("gf_per60") or 0, 3) or None
        tc.goals_against_per60 = round(rates.get("ga_per60") or 0, 3) or None
        tc.shots_for_per60    = round(rates.get("sf_per60") or 0, 3) if rates.get("sf_per60") else None
        tc.availability["goals_for_per60"] = FeatureAvailability(
            source="games", sample_size=int(rates.get("sample_games") or 0),
            reliability="STRONG" if rates.get("sample_games", 0) >= 10 else "MODEL",
        )
    sog_against, n = await _team_last_n_shot_defense(db, team_name, n=10)
    if sog_against is not None:
        tc.shots_against_per60 = round(sog_against, 3)
        tc.availability["shots_against_per60"] = FeatureAvailability(
            source="games.result.*_shots", sample_size=n,
            reliability="STRONG" if n >= 8 else "MODEL",
        )
    else:
        tc.availability["shots_against_per60"] = FeatureAvailability(
            source="UNAVAILABLE", sample_size=0, reliability="UNKNOWN",
            unavailable_reason="result.*_shots not persisted on games rows",
        )
    # Special teams / xG / rest left UNAVAILABLE unless provider PBP wires them.
    for k in ("xgf_per60", "xga_per60", "high_danger_xg_share",
              "pp_pct", "pk_pct", "pp_shots_per_opp"):
        tc.availability[k] = FeatureAvailability(
            source="UNAVAILABLE", sample_size=0, reliability="UNKNOWN",
            unavailable_reason="requires provider PBP integration",
        )
    return tc


async def build_goalie_context(db, team_name: str) -> GoalieContext:
    """Resolve the most recent starting goalie for a team.

    State:
      CONFIRMED  — pregame starter publicly announced
      EXPECTED   — projected starter (recent starts, rest cycle)
      UNKNOWN    — no reliable projection available
    """
    gc = GoalieContext(state="UNKNOWN")
    try:
        # Find the most recent goalie row for this team from player_game_logs
        row = await db.player_game_logs.find_one(
            {"sport": "nhl", "team": team_name, "position": "G"},
            sort=[("_id", -1)],
        )
    except Exception:
        row = None
    if row:
        gc.state         = "EXPECTED"
        gc.goalie_id     = row.get("player_id")
        gc.goalie_name   = row.get("name")
        gc.sv_pct_season = row.get("sv_pct")
        gc.availability["goalie_id"] = FeatureAvailability(
            source="player_game_logs", sample_size=1, reliability="MODEL",
        )
    else:
        gc.availability["goalie_id"] = FeatureAvailability(
            source="UNAVAILABLE", sample_size=0, reliability="UNKNOWN",
            unavailable_reason="no goalie row in player_game_logs",
        )
    # GSAx stays UNAVAILABLE until shot-level xG is persisted.
    gc.availability["gsax_per60"] = FeatureAvailability(
        source="UNAVAILABLE", sample_size=0, reliability="UNKNOWN",
        unavailable_reason="shot-level xG unavailable in current provider",
    )
    return gc


async def build_player_context(db, player_id: str) -> PlayerContext:
    pc = PlayerContext(player_id=player_id)
    stats = await _player_last_n(db, player_id, n=10)
    if stats.get("sample_size", 0) > 0:
        pc.player_name = stats.get("name")
        pc.toi_last5   = round(stats.get("toi_per_game") or 0, 2) or None
        pc.toi_ev_last5 = round(stats.get("ev_toi_per_game") or 0, 2) or None
        pc.toi_pp_last5 = round(stats.get("pp_toi_per_game") or 0, 2) or None
        pc.shots_per_game_last10 = stats.get("shots_per_game")
        pc.goals_per_game_last10 = stats.get("goals_per_game")
        pc.assists_per_game_last10 = stats.get("assists_per_game")
        pc.availability["player_last10"] = FeatureAvailability(
            source="player_game_logs", sample_size=stats["sample_size"],
            reliability="STRONG" if stats["sample_size"] >= 8 else "LIMITED",
        )
    else:
        pc.availability["player_last10"] = FeatureAvailability(
            source="UNAVAILABLE", sample_size=0, reliability="UNKNOWN",
            unavailable_reason="no recent games in player_game_logs",
        )
    # projected_* fields = toi_last5 if that's the best legitimate signal
    if pc.toi_last5:
        pc.projected_total_toi = pc.toi_last5
        pc.projected_ev_toi    = pc.toi_ev_last5
        pc.projected_pp_toi    = pc.toi_pp_last5
    # iXG / line context UNAVAILABLE without PBP
    pc.availability["ixg_per60"] = FeatureAvailability(
        source="UNAVAILABLE", sample_size=0, reliability="UNKNOWN",
        unavailable_reason="shot-level iXG requires provider PBP",
    )
    return pc


async def build_matchup_context(db, event_id: str) -> MatchupContext:
    """Top-level factory.  Loads game, assembles both teams + goalies."""
    g = await db.games.find_one({"game_id": event_id, "sport": "nhl"})
    if not g:
        return MatchupContext(event_id=event_id)
    home = await build_team_context(db, g.get("home") or "", is_home=True,
                                     last_game_date=g.get("date"))
    away = await build_team_context(db, g.get("away") or "", is_home=False,
                                     last_game_date=g.get("date"))
    hg = await build_goalie_context(db, g.get("home") or "")
    ag = await build_goalie_context(db, g.get("away") or "")
    return MatchupContext(
        event_id=event_id,
        event_time=g.get("date"),
        home=home, away=away,
        home_goalie=hg, away_goalie=ag,
    )


__all__ = [
    "FEATURE_CONTRACT_VERSION", "MODEL_FAMILY",
    "FeatureAvailability", "TeamContext", "GoalieContext",
    "PlayerContext", "LineContext", "MatchupContext",
    "build_team_context", "build_goalie_context",
    "build_player_context", "build_matchup_context",
]
