"""NFL Anytime Touchdown (ATD) Prediction Engine.

Estimates the TRUE probability that an NFL player scores ≥ 1 TD in a
given game using a layered causal model. NOT an EV/edge engine.

Model:
    P(TD ≥ 1) = 1 − exp(−λ)
    λ = team_td_rate × opp_share × matchup_factor × game_script_factor × conv_eff

Layers:
  1. TEAM_TD_RATE        — team's offensive rush+rec TDs per game (recency weighted).
  2. OPP_SHARE           — player's share of team's touch volume (carries + 0.85·targets).
  3. CONV_EFFICIENCY     — player's TD rate per touch (career → empirical).
  4. MATCHUP_FACTOR      — opponent rush+rec TDs allowed / league mean.
  5. GAME_SCRIPT_FACTOR  — favorite RB / underdog WR bumps when caller supplies a spread.

Recency: exponential decay weight = exp(−i / TAU). Last 6–10 games dominate.

ALT RULES (hard guardrails — match nfl_safe_engine.py):
  • min games sample           ≥ 5
  • min total touches          ≥ 10
  • min recent TD involvement: at least 1 TD in last 10 games OR L10 avg touches ≥ 8
                                 (otherwise it's a "random dart")
  • drop any pick whose probability comes from a SINGLE TD outlier (≥ 60% of
    historical TDs in one game)
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("lockscore.nfl_atd")


# ── Recency / sample gates ──
RECENT_TAU = 4.5                 # exp-decay constant — games-ago/4.5
MIN_GAMES_SAMPLE = 5
MIN_TOTAL_TOUCHES = 10
MIN_RECENT_TD_OR_VOLUME = True
RECENT_WINDOW = 10              # used for the "recent TD involvement" gate

# Volume / opportunity rating thresholds (touches/game in last 10).
OPPORTUNITY_HIGH = 12.0
OPPORTUNITY_MED = 7.0

# Conversion efficiency floor — players below 1.5% TD-per-touch are
# usually special teams / depth players; reject.
MIN_CONV_EFF = 0.015

# League means (recomputed at runtime from data on first call, cached).
_LEAGUE_CACHE: dict[str, Any] = {"computed_at": None, "data": None}
_CACHE_TTL_SEC = 60 * 30        # 30 minutes


def _to_int(v: Any) -> Optional[int]:
    if v in (None, "", "—", "-"):
        return None
    try:
        return int(float(str(v).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None


def _exp_weights(n: int, tau: float = RECENT_TAU) -> list[float]:
    """Exponential decay weights — index 0 = most recent."""
    return [math.exp(-i / tau) for i in range(n)]


def _weighted_avg(values: list[float], weights: list[float]) -> float:
    if not values:
        return 0.0
    w_sum = sum(weights[: len(values)])
    if w_sum == 0:
        return 0.0
    s = sum(v * w for v, w in zip(values, weights[: len(values)]))
    return s / w_sum


def _opportunity_rating(touches_per_game: float) -> str:
    if touches_per_game >= OPPORTUNITY_HIGH:
        return "high"
    if touches_per_game >= OPPORTUNITY_MED:
        return "med"
    return "low"


# ───────────────── League-mean enrichment ─────────────────

async def _league_means(db) -> dict:
    """Compute (and cache) league-average offensive + defensive TD rates."""
    now = datetime.now(timezone.utc).timestamp()
    cached = _LEAGUE_CACHE
    if cached["data"] and cached["computed_at"]:
        if now - cached["computed_at"] < _CACHE_TTL_SEC:
            return cached["data"]

    # Offensive TDs/game per team (rush + rec)
    off_pipeline = [
        {"$match": {"sport": "nfl", "stat_block": {"$in": ["rushing", "receiving"]}}},
        {"$group": {
            "_id": {"team": "$team", "game": "$game_id"},
            "tds": {"$sum": {"$toInt": "$nfl_td"}},
        }},
        {"$group": {
            "_id": "$_id.team",
            "tds_per_game": {"$avg": "$tds"},
            "n_games": {"$sum": 1},
        }},
    ]
    team_off: dict[str, dict] = {}
    league_off_total = 0.0
    league_off_n = 0
    async for d in db.player_game_logs.aggregate(off_pipeline, allowDiskUse=True):
        team_off[d["_id"]] = {"tds_per_game": d["tds_per_game"], "n_games": d["n_games"]}
        league_off_total += d["tds_per_game"] * d["n_games"]
        league_off_n += d["n_games"]
    league_off_avg = (league_off_total / league_off_n) if league_off_n else 2.4

    # Defensive TDs allowed per game — we need to know which team each
    # offensive log was AGAINST. We can derive that from the `games`
    # collection by matching game_id + opponent.
    # Build {game_id: {teams: [home, away]}} once.
    game_index: dict[str, dict] = {}
    async for g in db.games.find({"sport": "nfl"}, {"_id": 0, "game_id": 1, "home": 1, "away": 1}):
        game_index[g.get("game_id")] = {"home": g.get("home"), "away": g.get("away")}

    def _opp_of(team: str, gid: str) -> Optional[str]:
        gi = game_index.get(gid)
        if not gi:
            return None
        if gi.get("home") == team:
            return gi.get("away")
        if gi.get("away") == team:
            return gi.get("home")
        return None

    # Defensive TDs allowed: for each (team, game) offensive TD count,
    # attribute against opponent.
    def_tds: dict[str, dict] = {}
    async for d in db.player_game_logs.aggregate(off_pipeline, allowDiskUse=True):
        team = d["_id"]
        # We need the per-game records — re-fetch lightly. Cheaper: aggregate
        # over the same join.
        pass

    # Easier: walk per-(team, game) sums in one aggregate stage and
    # join opponent in Python.
    per_game = [d async for d in db.player_game_logs.aggregate([
        {"$match": {"sport": "nfl", "stat_block": {"$in": ["rushing", "receiving"]}}},
        {"$group": {
            "_id": {"team": "$team", "game": "$game_id"},
            "tds": {"$sum": {"$toInt": "$nfl_td"}},
        }},
    ], allowDiskUse=True)]

    for r in per_game:
        team = r["_id"]["team"]
        gid = r["_id"]["game"]
        opp = _opp_of(team, gid)
        if not opp:
            continue
        rec = def_tds.setdefault(opp, {"tds_allowed": 0, "n_games": 0})
        rec["tds_allowed"] += r["tds"]
        rec["n_games"] += 1
    team_def = {
        t: {
            "tds_allowed_per_game": v["tds_allowed"] / max(1, v["n_games"]),
            "n_games": v["n_games"],
        }
        for t, v in def_tds.items()
    }
    league_def_avg = (
        sum(v["tds_allowed_per_game"] * v["n_games"] for v in team_def.values())
        / max(1, sum(v["n_games"] for v in team_def.values()))
    ) if team_def else 2.4

    out = {
        "team_off": team_off,
        "team_def": team_def,
        "league_off_avg": league_off_avg,
        "league_def_avg": league_def_avg,
        "n_teams_off": len(team_off),
        "n_teams_def": len(team_def),
    }
    _LEAGUE_CACHE["data"] = out
    _LEAGUE_CACHE["computed_at"] = now
    return out


# ───────────────── Per-player prediction ─────────────────

async def _player_profile(db, player_id: str) -> Optional[dict]:
    """Aggregate a player's NFL touch + TD distribution."""
    cursor = db.player_game_logs.find(
        {"player_id": player_id, "sport": "nfl",
         "stat_block": {"$in": ["rushing", "receiving"]}},
        {"_id": 0},
    ).sort("date", -1).limit(40)  # most recent 40 logs across rush+rec
    rows = [d async for d in cursor]
    if not rows:
        return None

    # Collapse per game_id (a player can have BOTH rushing + receiving
    # rows for the same game — one "appearance" per game).
    by_game: dict[str, dict] = {}
    for r in rows:
        gid = r.get("game_id")
        if not gid:
            continue
        car = _to_int(r.get("nfl_car")) or 0
        tgts = _to_int(r.get("nfl_tgts")) or 0
        td = _to_int(r.get("nfl_td")) or 0
        rec_yd = _to_int(r.get("nfl_yds")) or 0 if r.get("stat_block") == "receiving" else 0
        rush_yd = _to_int(r.get("nfl_yds")) or 0 if r.get("stat_block") == "rushing" else 0
        existing = by_game.get(gid)
        if existing:
            existing["car"] = max(existing["car"], car)
            existing["tgts"] = max(existing["tgts"], tgts)
            existing["td"] = max(existing["td"], td)
            existing["rush_yd"] += rush_yd
            existing["rec_yd"] += rec_yd
        else:
            by_game[gid] = {
                "game_id": gid, "date": r.get("date") or "",
                "team": r.get("team"), "name": r.get("name"),
                "car": car, "tgts": tgts, "td": td,
                "rush_yd": rush_yd, "rec_yd": rec_yd,
            }

    games = sorted(by_game.values(), key=lambda g: g.get("date") or "", reverse=True)
    if not games:
        return None
    return {
        "player_id": player_id,
        "name": games[0].get("name"),
        "team": games[0].get("team"),
        "games": games,
    }


def _td_outlier_check(games: list[dict]) -> Optional[str]:
    """Reject if 60%+ of historical TDs came from ONE game."""
    tds = [g["td"] for g in games if g["td"] > 0]
    if len(tds) < 3:
        return None
    total = sum(tds)
    if total == 0:
        return None
    max_one = max(tds)
    if max_one / total >= 0.60:
        return "td_outlier_inflated"
    return None


async def predict_player_atd(
    db, *, player_id: str, opponent: Optional[str] = None,
    spread: Optional[float] = None,
) -> dict:
    """Predict P(TD ≥ 1) for a player vs an optional opponent."""
    profile = await _player_profile(db, player_id)
    if not profile or len(profile["games"]) < MIN_GAMES_SAMPLE:
        return {"reject": "insufficient_history",
                "games_logged": len(profile["games"]) if profile else 0}

    games = profile["games"]
    league = await _league_means(db)

    # Build recency-weighted touch + TD averages
    last_n = games[: RECENT_WINDOW]
    weights = _exp_weights(len(last_n))
    touches_series = [g["car"] + 0.85 * g["tgts"] for g in last_n]
    td_series = [g["td"] for g in last_n]

    w_touches = _weighted_avg(touches_series, weights)
    w_td = _weighted_avg(td_series, weights)

    # Volume / outlier gates
    total_touches = sum(g["car"] + g["tgts"] for g in games)
    if total_touches < MIN_TOTAL_TOUCHES:
        return {"reject": "volume_too_low", "total_touches": total_touches}
    outlier = _td_outlier_check(games)
    if outlier:
        return {"reject": outlier}
    # Random-dart gate: must have either ≥1 TD in last 10 OR weighted touches ≥ 8
    if MIN_RECENT_TD_OR_VOLUME:
        recent_tds = sum(1 for g in last_n if g["td"] > 0)
        if recent_tds == 0 and w_touches < 8.0:
            return {"reject": "no_recent_red_zone_path",
                    "recent_tds_L10": recent_tds,
                    "weighted_touches_L10": round(w_touches, 2)}

    team = profile["team"] or ""
    team_off = league["team_off"].get(team) or {}
    team_td_rate = team_off.get("tds_per_game") or league["league_off_avg"]

    # Player opportunity share — touches / team_avg_touches.
    # Approximate team_avg_touches per game from offensive plays. We don't
    # have raw plays, so use team_td_rate × ~18 (rough touches per TD ratio
    # in NFL: ~50 touches per game / 2.8 TDs).
    team_avg_touches = max(35.0, (team_off.get("tds_per_game") or 2.5) * 18.0)
    opp_share = max(0.02, min(0.50, w_touches / team_avg_touches))

    # Conversion efficiency — historical TDs/touch over ALL games we have.
    total_tds = sum(g["td"] for g in games)
    conv_eff = (total_tds / total_touches) if total_touches else 0.0
    if conv_eff < MIN_CONV_EFF:
        return {"reject": "conversion_eff_low", "conv_eff": round(conv_eff, 4)}

    # Matchup factor — defensive TDs allowed / league mean (defaults to 1.0
    # if opponent unknown or no def stats yet).
    if opponent:
        team_def = league["team_def"].get(opponent)
        if team_def:
            matchup_factor = team_def["tds_allowed_per_game"] / max(0.01, league["league_def_avg"])
        else:
            matchup_factor = 1.0
    else:
        matchup_factor = 1.0
    matchup_factor = max(0.7, min(1.4, matchup_factor))  # cap extremes

    # Game-script factor — if user supplies spread:
    #   • RBs benefit when favored (positive script).
    #   • WRs benefit when trailing.
    # Cheap heuristic: estimate role via touch composition (carries-heavy ⇒ RB).
    car_share = sum(g["car"] for g in last_n) / max(1, sum((g["car"] + g["tgts"]) for g in last_n))
    is_rb = car_share >= 0.55
    game_script_factor = 1.0
    if spread is not None:
        # spread < 0 means player's TEAM is favored.
        if is_rb and spread <= -3:
            game_script_factor = 1.08 + min(0.10, abs(spread + 3) * 0.012)
        elif not is_rb and spread >= 3:
            game_script_factor = 1.06 + min(0.08, (spread - 3) * 0.010)
        elif is_rb and spread >= 4:
            game_script_factor = 0.92
        elif not is_rb and spread <= -7:
            game_script_factor = 0.94
    game_script_factor = max(0.85, min(1.20, game_script_factor))

    # Combine — Poisson-style λ then convert to ≥1 TD probability.
    lam = team_td_rate * opp_share * matchup_factor * game_script_factor
    # Conversion efficiency is implicit in opp_share×team_td_rate, but we
    # blend it as a soft correction: 70% structural, 30% conv-eff anchored.
    lam_conv = w_touches * conv_eff * matchup_factor * game_script_factor
    blended = 0.7 * lam + 0.3 * lam_conv

    probability = 1.0 - math.exp(-blended)
    probability = max(0.0, min(0.95, probability))

    # Confidence: anchor on probability, penalise low sample + variance.
    n_used = len(games)
    sample_penalty = max(0.0, 0.18 - (n_used / 60.0))
    variance_penalty = 0.0
    if td_series:
        ev = sum(td_series) / len(td_series)
        var = sum((x - ev) ** 2 for x in td_series) / len(td_series)
        cv = math.sqrt(var) / max(0.1, ev) if ev > 0 else 0.0
        variance_penalty = min(0.15, cv * 0.10)
    confidence = max(0.0, probability - sample_penalty - variance_penalty)

    # Reasons
    rating = _opportunity_rating(w_touches)
    reasons = [
        f"{round(w_touches, 1)} touches/g L{len(last_n)} ({rating})",
        f"{round(w_td, 2)} TD/g L{len(last_n)}",
        f"team {round(team_td_rate, 2)} TD/g",
    ]
    if opponent:
        reasons.append(
            f"vs {opponent} (matchup ×{round(matchup_factor, 2)})"
        )
    if spread is not None:
        reasons.append(f"spread {spread:+.1f} (script ×{round(game_script_factor, 2)})")

    return {
        "player_id": player_id,
        "player_name": profile["name"],
        "team": team,
        "opponent": opponent,
        "td_probability": round(probability, 4),
        "confidence": round(confidence, 4),
        "opportunity_rating": rating,
        "weighted_touches_recent": round(w_touches, 2),
        "weighted_tds_recent": round(w_td, 3),
        "team_td_rate": round(team_td_rate, 3),
        "conv_efficiency": round(conv_eff, 4),
        "opportunity_share": round(opp_share, 4),
        "matchup_factor": round(matchup_factor, 3),
        "game_script_factor": round(game_script_factor, 3),
        "lambda_structural": round(lam, 4),
        "lambda_conv": round(lam_conv, 4),
        "lambda_blended": round(blended, 4),
        "is_rb_archetype": is_rb,
        "sample_games": len(games),
        "total_touches": int(total_touches),
        "total_tds": int(total_tds),
        "reasons": reasons,
    }


async def atd_leaderboard(
    db, *, limit: int = 20, min_probability: float = 0.30,
    min_opportunity_rating: str = "med",
) -> dict:
    """Rank every NFL player by P(TD ≥ 1) given a NEUTRAL opponent.

    Used as the default board view — useful for picking out structural
    locks before matchups are even set. The user can then call
    `predict_player_atd(opponent=…, spread=…)` for any candidate for the
    matchup-adjusted version.
    """
    # Iterate every NFL player with rushing OR receiving logs.
    pipeline = [
        {"$match": {"sport": "nfl", "stat_block": {"$in": ["rushing", "receiving"]}}},
        {"$group": {"_id": "$player_id", "n": {"$sum": 1}}},
        {"$match": {"n": {"$gte": MIN_GAMES_SAMPLE}}},
        {"$sort": {"n": -1}},
        {"$limit": 800},   # safety cap so a runaway DB doesn't blow up
    ]
    candidates = [d["_id"] async for d in db.player_game_logs.aggregate(pipeline, allowDiskUse=True)]

    rank_order = {"low": 0, "med": 1, "high": 2}
    min_rank = rank_order.get(min_opportunity_rating, 1)

    rows: list[dict] = []
    rejects: dict[str, int] = {}
    for pid in candidates:
        out = await predict_player_atd(db, player_id=pid)
        if out.get("reject"):
            r = out["reject"]
            rejects[r] = rejects.get(r, 0) + 1
            continue
        if rank_order.get(out["opportunity_rating"], 0) < min_rank:
            continue
        if out["td_probability"] < min_probability:
            continue
        rows.append(out)

    rows.sort(key=lambda r: (r["confidence"], r["td_probability"]), reverse=True)
    return {
        "total_candidates": len(candidates),
        "passed_filters": len(rows),
        "rejected": rejects,
        "rules": {
            "min_games_sample": MIN_GAMES_SAMPLE,
            "min_total_touches": MIN_TOTAL_TOUCHES,
            "min_conv_efficiency": MIN_CONV_EFF,
            "min_recent_td_or_volume": MIN_RECENT_TD_OR_VOLUME,
            "min_opportunity_rating": min_opportunity_rating,
            "min_probability": min_probability,
        },
        "league_means": {
            "off_td_per_game": round(_LEAGUE_CACHE["data"]["league_off_avg"], 3)
                                 if _LEAGUE_CACHE["data"] else None,
            "def_td_per_game": round(_LEAGUE_CACHE["data"]["league_def_avg"], 3)
                                 if _LEAGUE_CACHE["data"] else None,
        },
        "picks": rows[: max(1, int(limit))],
    }
