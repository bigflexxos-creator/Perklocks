"""CFB Feature Engine — Phase 3 M3 CFB variant.

USER MANDATE 2026-07-27: "Build services/cfb_feature_engine.py (mirror
of NFL engine): returning production data, career-vs-opp, transfer
portal, SoS." Ships in time for the Aug 23 Week-0 kickoff.

Combines all real CFB data sources into a `build_cfb_prop_factors()`
output shape that mirrors NFL / MLB:
    (factors_dict, source_list)

Signals used:
  • Player L5 rolling avg vs line          (player_game_logs, sport=cfb)
  • L3 vs season trend (heating up?)       (player_game_logs)
  • Career hit-rate vs this specific opp   (player_game_logs)
  • Opponent SP+ defensive rating          (cfb_sp_ratings)
  • Team returning production %            (cfb_returning_production)
  • Transfer portal impact                 (cfb_portal)
  • Strength of Schedule                   (cfb_sp_ratings.sos)
  • Book implied probability anchor

Rules:
  • Every factor is REAL data — no RNG, no placeholders
  • Returns None per-factor when data insufficient
  • `has_enough_real_data_cfb()` gates emission on ≥3 real factors
    (same threshold as NFL)

Supported prop stats:
    passing_yards, rushing_yards, receiving_yards,
    receptions, passing_tds, anytime_td
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger("lockscore.services.cfb_feature_engine")

MIN_FACTORS_CFB_PROP = 3


def _scale(value: float, low: float, high: float,
           out_low: float = 0.30, out_high: float = 0.95) -> float:
    if high == low:
        return (out_low + out_high) / 2
    v = (value - low) / (high - low)
    v = max(0.0, min(1.0, v))
    return out_low + v * (out_high - out_low)


def has_enough_real_data_cfb(factors: dict) -> bool:
    return sum(1 for v in factors.values() if isinstance(v, (int, float))) >= MIN_FACTORS_CFB_PROP


# ── Data fetchers ──────────────────────────────────────────────────

async def _fetch_cfb_player_id(db, player_name: str) -> Optional[str]:
    """Resolve a player name to the ESPN CFB player_id."""
    if not player_name:
        return None
    doc = await db.players.find_one(
        {"sport": "cfb", "name": {"$regex": f"^{player_name}$", "$options": "i"}},
    )
    return (doc or {}).get("player_id")


async def _player_recent_averages(
    db, player_name: str, seasons: list[int], stat: str, n: int = 5,
) -> Optional[dict]:
    """Return {'l5': avg_last_5, 'l3': avg_last_3, 'season_avg': avg_season}."""
    pid = await _fetch_cfb_player_id(db, player_name)
    if not pid:
        return None
    cursor = db.player_game_logs.find(
        {"player_id": pid, "sport": "cfb", stat: {"$exists": True}},
    ).sort("_id", -1).limit(50)
    rows = await cursor.to_list(length=50)
    if not rows:
        return None
    vals = [r.get(stat) for r in rows if isinstance(r.get(stat), (int, float))]
    if not vals:
        return None
    l5 = sum(vals[:5]) / min(5, len(vals))
    l3 = sum(vals[:3]) / min(3, len(vals))
    # Season = current season if we have season field, else all vals
    curr_season_vals = [r.get(stat) for r in rows
                        if r.get("season") in seasons and isinstance(r.get(stat), (int, float))]
    season_avg = (sum(curr_season_vals) / len(curr_season_vals)
                  if curr_season_vals else sum(vals) / len(vals))
    return {
        "l5": {stat: round(l5, 2)},
        "l3": {stat: round(l3, 2)},
        "season_avg": {stat: round(season_avg, 2)},
        "n_games": len(vals),
    }


async def _player_hit_rate_vs_opp(
    db, player_name: str, stat: str, line: float, opponent: str, side: str = "over",
) -> Optional[dict]:
    """Career hit rate for this player vs this opponent."""
    pid = await _fetch_cfb_player_id(db, player_name)
    if not pid:
        return None
    # Find games where this player faced the specified opponent
    # game_id pattern: espn_cfb_{event_id}; opponent lookup via games doc
    logs = await db.player_game_logs.find(
        {"player_id": pid, "sport": "cfb", stat: {"$exists": True}},
    ).to_list(length=100)
    if not logs:
        return None
    matches = []
    for lg in logs:
        gid = lg.get("game_id")
        if not gid:
            continue
        game = await db.games.find_one({"game_id": gid, "sport": "cfb"})
        if not game:
            continue
        # Opponent = the OTHER team in this game
        home = game.get("home")
        away = game.get("away")
        my_team = lg.get("team")
        opp_in_game = away if my_team == home else home
        if not opp_in_game or opponent.lower() not in opp_in_game.lower() and opp_in_game.lower() not in opponent.lower():
            continue
        v = lg.get(stat)
        if isinstance(v, (int, float)):
            matches.append(v)
    if not matches:
        return None
    n = len(matches)
    if side == "over":
        hits = sum(1 for v in matches if v > line)
    else:
        hits = sum(1 for v in matches if v < line)
    hit_rate = hits / n
    return {
        "hit_rate": hit_rate,
        "games":    n,
        "recent_values": matches[-5:],
        "rationale": (
            f"vs {opponent} historically: {hits}/{n} = {hit_rate*100:.0f}% hit rate on {stat} {side} {line:g}."
        ),
    }


async def _opp_sp_rating(db, opponent: str, year: int) -> Optional[dict]:
    """Fetch SP+ ratings for the opposing team (offense/defense/SoS)."""
    if not opponent:
        return None
    doc = await db.cfb_sp_ratings.find_one({"year": year, "team": opponent})
    if not doc:
        # Try alternate names via cfb_teams
        team_doc = await db.cfb_teams.find_one({"alternate_names": opponent})
        if team_doc:
            doc = await db.cfb_sp_ratings.find_one({"year": year, "team": team_doc.get("school")})
    return doc


async def _team_returning_production(db, team: str, year: int) -> Optional[dict]:
    """Returning production % — high number means offense/defense stayed intact."""
    if not team:
        return None
    doc = await db.cfb_returning_production.find_one({"season": year, "team": team})
    if not doc:
        team_doc = await db.cfb_teams.find_one({"alternate_names": team})
        if team_doc:
            doc = await db.cfb_returning_production.find_one(
                {"season": year, "team": team_doc.get("school")},
            )
    return doc


async def _player_portal_status(db, player_name: str, year: int) -> Optional[dict]:
    """Check if this player transferred INTO the program this year — a
    NEW transfer with limited chemistry is a risk factor for the Over."""
    if not player_name:
        return None
    doc = await db.cfb_portal.find_one({
        "season": year,
        "full_name": {"$regex": f"^{player_name}$", "$options": "i"},
    })
    return doc


# ── Factor builders (mirror NFL engine) ────────────────────────────

def _factor_rolling_avg_vs_line(rolling: dict, stat: str, line: float) -> Optional[float]:
    l5 = (rolling or {}).get("l5") or {}
    val = l5.get(stat)
    if not isinstance(val, (int, float)) or line <= 0:
        return None
    ratio = val / line
    if ratio >= 1.0:
        v = 0.55 + min(0.40, (ratio - 1.0) * 0.80)
    else:
        v = 0.30 + max(0.0, ratio - 0.5) * 0.50
    return round(max(0.30, min(0.95, v)), 3)


def _factor_l3_vs_season(rolling: dict, stat: str) -> Optional[float]:
    l3 = (rolling or {}).get("l3") or {}
    season = (rolling or {}).get("season_avg") or {}
    a, b = l3.get(stat), season.get(stat)
    if not (isinstance(a, (int, float)) and isinstance(b, (int, float)) and b > 0):
        return None
    delta = (a - b) / b
    v = 0.60 + delta * 1.25
    return round(max(0.30, min(0.95, v)), 3)


def _factor_hit_rate_vs_opp(hit_row: Optional[dict]) -> Optional[float]:
    if not hit_row:
        return None
    hr = hit_row.get("hit_rate")
    n = hit_row.get("games") or 0
    if not isinstance(hr, (int, float)):
        return None
    prior_weight = 3.0
    posterior = (hr * n + 0.5 * prior_weight) / (n + prior_weight)
    return round(max(0.30, min(0.95, 0.30 + posterior * 0.65)), 3)


def _factor_opp_defense(sp: Optional[dict], side: str = "over") -> Optional[float]:
    """SP+ defensive rating — LOWER rank is better defense (harder to
    score against). Over-side: bad defense (rank > 60) is a green signal.

    Schema: cfb_sp_ratings.defense_rank (int 1-136, 1=best defense).
    """
    if not sp:
        return None
    def_rank = sp.get("defense_rank")
    if not isinstance(def_rank, (int, float)):
        return None
    # rank 1 = best defense, 136 = worst. For OVER we want a worse
    # defense (higher rank → higher factor).
    if side == "over":
        v = _scale(def_rank, 20, 100, 0.35, 0.90)
    else:  # under
        v = _scale(def_rank, 20, 100, 0.90, 0.35)
    return round(v, 3)


def _factor_returning_production(
    rp: Optional[dict], prop_stat: str,
) -> Optional[float]:
    """Higher returning-production (or PPA continuity) = more chemistry
    = higher factor. Uses stat-specific PPA continuity when available:
      • passing_*        → percent_passing_ppa
      • rushing_*        → percent_rushing_ppa
      • receiving_*      → percent_receiving_ppa
      • fallback         → percent_ppa (overall offensive PPA share)

    Schema: cfb_returning_production.percent_*_ppa in 0.0-1.0 range.
    """
    if not rp:
        return None
    key_map = {
        "passing":   "percent_passing_ppa",
        "rushing":   "percent_rushing_ppa",
        "receiving": "percent_receiving_ppa",
    }
    stat_prefix = (
        "passing"   if prop_stat.startswith("passing_")
        else "rushing"   if prop_stat.startswith("rushing_") or prop_stat == "carries"
        else "receiving" if prop_stat.startswith("receiving_") or prop_stat in ("receptions", "targets")
        else "misc"
    )
    val = rp.get(key_map.get(stat_prefix)) or rp.get("percent_ppa")
    if not isinstance(val, (int, float)):
        return None
    # 0-1 scale — 0.50 = league median; 0.80+ = very high continuity
    v = _scale(val, 0.20, 0.90, 0.35, 0.85)
    return round(v, 3)


def _factor_transfer_portal(portal: Optional[dict], side: str = "over") -> Optional[float]:
    """Portal transfer = disruption. If the player has a portal entry
    for this season, they moved into a new program with limited chemistry.

    Schema: cfb_portal.{full_name, destination, origin, stars}
      • Entry found → 0.45 (adverse signal for Over)
      • No entry    → 0.60 (neutral positive — established roster)
      • None        → skip
    """
    if portal is None:
        return None
    if not portal:
        return 0.60
    return 0.45


def _factor_sos(sp: Optional[dict]) -> Optional[float]:
    """Strength of Schedule from SP+. Weaker SoS (higher rank) inflates
    stats — bumps Over factor. Stronger SoS (lower rank) deflates.

    Schema: cfb_sp_ratings.sos (may be None during early season).
    """
    if not sp:
        return None
    sos = sp.get("sos")
    if not isinstance(sos, (int, float)):
        return None
    # sos values are typically -0.5 (easy) to +0.5 (hard). Rare cases
    # ±1. Positive SoS = tougher schedule. For Over: easier SoS = higher
    # factor (props inflate against weak D).
    # We invert: easier (sos < 0) → 0.75; harder (sos > 0) → 0.45.
    v = 0.60 - float(sos) * 0.30
    return round(max(0.30, min(0.90, v)), 3)


def _factor_book_implied(book_implied: Optional[float]) -> Optional[float]:
    if not isinstance(book_implied, (int, float)):
        return None
    return round(max(0.30, min(0.95, float(book_implied))), 3)


# ── Composite feature engine ───────────────────────────────────────

async def build_cfb_prop_factors(
    db,
    *,
    player: str,
    player_team: str,        # player's own team (for returning prod)
    opponent: str,           # opposing team
    position: str,           # QB / RB / WR / TE
    prop_stat: str,          # passing_yards / rushing_yards / etc.
    line: float,
    side: str = "over",
    season: int,
    is_home: bool = True,
    book_implied: Optional[float] = None,
) -> tuple[dict, list[str]]:
    """Build the Phase-3 CFB prop factor set. Mirrors NFL engine shape.

    Returns (factors_dict, source_list). Callers gate on
    `has_enough_real_data_cfb(factors)` before emission.
    """
    # Fetch all raw data
    rolling = await _player_recent_averages(
        db, player, seasons=[season], stat=prop_stat,
    )
    hit_row = await _player_hit_rate_vs_opp(
        db, player, prop_stat, line, opponent, side,
    )
    opp_sp = await _opp_sp_rating(db, opponent, season)
    my_rp = await _team_returning_production(db, player_team, season)
    portal = await _player_portal_status(db, player, season)

    factors: dict[str, Optional[float]] = {
        "L5 Avg vs Line":            _factor_rolling_avg_vs_line(rolling, prop_stat, line),
        "L3 vs Season Trend":        _factor_l3_vs_season(rolling, prop_stat),
        "Career vs Opponent Hit%":   _factor_hit_rate_vs_opp(hit_row),
        "Opponent Defense (SP+)":    _factor_opp_defense(opp_sp, side),
        "Team Returning Production": _factor_returning_production(my_rp, prop_stat),
        "Transfer Portal Status":    _factor_transfer_portal(portal, side),
        "Strength of Schedule":      _factor_sos(opp_sp),
        "Book Implied Anchor":       _factor_book_implied(book_implied),
    }

    sources: list[str] = []
    if factors["L5 Avg vs Line"] is not None:
        sources.append("player_game_logs_cfb_L5")
    if factors["L3 vs Season Trend"] is not None:
        sources.append("player_game_logs_cfb_L3_trend")
    if factors["Career vs Opponent Hit%"] is not None:
        sources.append("player_game_logs_cfb_career_vs_opp")
    if factors["Opponent Defense (SP+)"] is not None:
        sources.append("cfb_sp_ratings_defense")
    if factors["Team Returning Production"] is not None:
        sources.append("cfb_returning_production")
    if factors["Transfer Portal Status"] is not None:
        sources.append("cfb_portal_check")
    if factors["Strength of Schedule"] is not None:
        sources.append("cfb_sp_ratings_sos")
    if factors["Book Implied Anchor"] is not None:
        sources.append("odds_api_book_implied")

    # Rationale bits for "Why this pick" panel
    rationale_bits: list[str] = []
    l5v = ((rolling or {}).get("l5") or {}).get(prop_stat)
    if isinstance(l5v, (int, float)):
        rationale_bits.append(
            f"{player}'s L5 avg is {l5v:.1f} {prop_stat.replace('_', ' ')} vs a line of {line:g}."
        )
    if hit_row and hit_row.get("games"):
        rationale_bits.append(hit_row.get("rationale", ""))
    if opp_sp:
        def_rank = opp_sp.get("defense_rank")
        if isinstance(def_rank, (int, float)):
            rationale_bits.append(
                f"{opponent} defense ranks {int(def_rank)} in SP+ (lower = better)."
            )
        sos = opp_sp.get("sos")
        if isinstance(sos, (int, float)):
            band = "tougher" if sos > 0.15 else "easier" if sos < -0.15 else "neutral"
            rationale_bits.append(f"{opponent} plays a {band} schedule (SP+ SoS = {sos:.2f}).")
    if my_rp:
        val = my_rp.get("percent_ppa")
        if isinstance(val, (int, float)):
            rationale_bits.append(
                f"{player_team} returns {val * 100:.0f}% of {season - 1} offensive PPA."
            )
    if portal:
        origin = portal.get("origin")
        rationale_bits.append(
            f"{player} transferred INTO {player_team} for {season}"
            + (f" from {origin}" if origin else "")
            + " — new to program."
        )

    factors["_rationale_bits"] = rationale_bits  # type: ignore
    return factors, sources


__all__ = [
    "build_cfb_prop_factors",
    "has_enough_real_data_cfb",
    "MIN_FACTORS_CFB_PROP",
]
