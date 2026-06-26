"""NFL Game Bets Engine — Moneyline / Spread / Total true-probability models.

Completely separate from the player-prop layer. Reads team-level
performance from `games` collection (final scores per game) and derives:

  • Team ratings via points-scored − points-allowed differential
  • Expected margin = home_rating − away_rating + HFA
  • ML probability via logistic transform of expected margin
  • Spread cover probability via Normal CDF
  • Total over/under probability via Normal CDF on expected total

NO edge / EV optimization. Output is pure TRUE PROBABILITY only.

Hard guardrails (ALT RULES extended for team data):
  • Each team must have ≥ 6 game results in scope (`MIN_TEAM_GAMES`)
  • Reject blowout-heavy ratings (clamped at ±14 to avoid overfit)
  • Sample weighted exponentially by recency (last season = full weight,
    older seasons taper)

References:
  • NFL HFA ~2.5 pts (well established empirically).
  • σ_margin ≈ 13.5 pts (standard NFL margin spread).
  • σ_total  ≈ 10 pts (standard NFL total spread).
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("lockscore.nfl_games")

# ── Tunable constants (NFL-established) ──
HFA_POINTS = 2.5
SIGMA_MARGIN = 13.5
SIGMA_TOTAL = 10.0
MIN_TEAM_GAMES = 6
RATING_CLAMP = 14.0
# Recency: weight by season distance from current.
SEASON_HALF_LIFE = 1.0

# Alt-line ladders for "safe" picks. Same shape as player-prop engine.
ALT_SPREAD_LINES: list[float] = [-1.5, -2.5, -3.5, -4.5, -6.5, -7.5, -9.5, -10.5, -13.5, -14.5]
ALT_TOTAL_LINES: list[float] = [33.5, 35.5, 37.5, 39.5, 41.5, 43.5, 45.5, 47.5, 49.5, 51.5]

# Hard probability floor for the "safe locks" view.
MIN_PROBABILITY = 0.78
PREF_PROBABILITY = 0.857

_CACHE: dict[str, Any] = {"computed_at": None, "ratings": None}
_CACHE_TTL = 30 * 60


def _norm_cdf(x: float) -> float:
    """Approximation of the standard normal CDF via erf."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _logistic_margin(expected_margin: float) -> float:
    """Convert expected point margin → win probability."""
    return 1.0 / (1.0 + math.exp(-expected_margin / (SIGMA_MARGIN * 0.55)))


async def _team_ratings(db) -> dict:
    """Recompute (and cache) each NFL team's offensive + defensive rating
    from the `games` collection.

    Rating = recency-weighted (points_scored − points_allowed) per game.
    Capped at ±14 to suppress outlier seasons.
    """
    now = datetime.now(timezone.utc).timestamp()
    cached = _CACHE
    if cached["ratings"] and cached["computed_at"]:
        if now - cached["computed_at"] < _CACHE_TTL:
            return cached["ratings"]

    current_year = datetime.now(timezone.utc).year
    accum: dict[str, dict] = {}
    async for g in db.games.find(
        {"sport": "nfl", "status": "Final"},
        {"_id": 0, "home": 1, "away": 1, "result": 1, "season": 1},
    ):
        season = int(g.get("season") or current_year)
        # Recency weight: most recent season = 1.0, prior season = 0.5, etc.
        gap = max(0, current_year - season)
        weight = math.exp(-gap / SEASON_HALF_LIFE)
        result = g.get("result") or {}
        h_score = result.get("home")
        a_score = result.get("away")
        if h_score is None or a_score is None:
            continue
        home = g.get("home") or ""
        away = g.get("away") or ""
        if not home or not away:
            continue
        for team, scored, allowed, is_home in (
            (home, h_score, a_score, True),
            (away, a_score, h_score, False),
        ):
            rec = accum.setdefault(team, {
                "name": team,
                "scored_w": 0.0,
                "allowed_w": 0.0,
                "wins_w": 0.0,
                "games_w": 0.0,
                "n_games": 0,
                "home_games": 0,
                "away_games": 0,
            })
            rec["scored_w"] += scored * weight
            rec["allowed_w"] += allowed * weight
            rec["games_w"] += weight
            rec["n_games"] += 1
            rec["wins_w"] += weight if scored > allowed else 0.0
            if is_home:
                rec["home_games"] += 1
            else:
                rec["away_games"] += 1

    ratings: dict[str, dict] = {}
    for team, rec in accum.items():
        if rec["n_games"] < MIN_TEAM_GAMES:
            continue
        gw = max(0.01, rec["games_w"])
        ppg = rec["scored_w"] / gw
        opp_ppg = rec["allowed_w"] / gw
        diff = ppg - opp_ppg
        # Clamp the rating.
        rating = max(-RATING_CLAMP, min(RATING_CLAMP, diff))
        ratings[team] = {
            "team": team,
            "rating": round(rating, 3),
            "ppg": round(ppg, 2),
            "opp_ppg": round(opp_ppg, 2),
            "win_rate": round(rec["wins_w"] / gw, 3),
            "n_games": rec["n_games"],
        }

    # League means for context.
    if ratings:
        league_ppg = sum(r["ppg"] for r in ratings.values()) / len(ratings)
    else:
        league_ppg = 22.0

    out = {
        "ratings": ratings,
        "league_ppg": round(league_ppg, 2),
        "n_teams": len(ratings),
        "computed_at": now,
    }
    _CACHE["ratings"] = out
    _CACHE["computed_at"] = now
    return out


async def predict_game(
    db, *, home: str, away: str, market: str = "ml",
    spread: Optional[float] = None, total: Optional[float] = None,
) -> dict:
    """Predict a single matchup's true probability.

    Args:
      market: "ml" | "spread" | "total"
      spread: Required for market="spread". HOME side spread (negative
              if home is favored, e.g. -3.5).
      total: Required for market="total".
    """
    ctx = await _team_ratings(db)
    ratings = ctx["ratings"]
    hr = ratings.get(home)
    ar = ratings.get(away)
    if not hr or not ar:
        missing = [t for t, r in (("home", hr), ("away", ar)) if not r]
        return {"reject": f"team_no_rating({','.join(missing)})",
                "n_teams_indexed": len(ratings)}

    expected_margin = hr["rating"] - ar["rating"] + HFA_POINTS
    expected_total = (hr["ppg"] + ar["opp_ppg"] + ar["ppg"] + hr["opp_ppg"]) / 2.0

    if market == "ml":
        p_home = _logistic_margin(expected_margin)
        p_away = 1.0 - p_home
        side = "home" if p_home >= p_away else "away"
        return {
            "matchup": f"{away} @ {home}",
            "market": "moneyline",
            "expected_margin": round(expected_margin, 2),
            "p_home": round(p_home, 4),
            "p_away": round(p_away, 4),
            "recommended_side": side,
            "true_probability": round(max(p_home, p_away), 4),
            "home_rating": hr,
            "away_rating": ar,
        }

    if market == "spread":
        if spread is None:
            return {"reject": "spread_required"}
        # P(home margin > -spread) — sportsbook convention: home spread of
        # -3.5 means home needs to win by >3.5.
        z = (expected_margin - (-spread)) / SIGMA_MARGIN
        p_home_covers = _norm_cdf(z)
        p_away_covers = 1.0 - p_home_covers
        side = "home" if p_home_covers >= p_away_covers else "away"
        return {
            "matchup": f"{away} @ {home}",
            "market": "spread",
            "spread": spread,
            "expected_margin": round(expected_margin, 2),
            "p_home_covers": round(p_home_covers, 4),
            "p_away_covers": round(p_away_covers, 4),
            "recommended_side": side,
            "true_probability": round(max(p_home_covers, p_away_covers), 4),
            "home_rating": hr,
            "away_rating": ar,
        }

    if market == "total":
        if total is None:
            return {"reject": "total_required"}
        z_over = (expected_total - total) / SIGMA_TOTAL
        p_over = _norm_cdf(z_over)
        p_under = 1.0 - p_over
        side = "over" if p_over >= p_under else "under"
        return {
            "matchup": f"{away} @ {home}",
            "market": "total",
            "total": total,
            "expected_total": round(expected_total, 2),
            "p_over": round(p_over, 4),
            "p_under": round(p_under, 4),
            "recommended_side": side,
            "true_probability": round(max(p_over, p_under), 4),
            "home_rating": hr,
            "away_rating": ar,
        }

    return {"reject": f"unknown_market({market})"}


async def safe_alt_locks(
    db,
    *,
    home: str,
    away: str,
    min_probability: float = MIN_PROBABILITY,
) -> dict:
    """Sweep the alt-spread + alt-total ladders for a matchup and return
    the highest-probability passing line per market.

    Useful for surfacing "safe locks" within a given matchup — e.g. a
    big favorite getting alt-spread of -1.5 with 95% probability."""
    ctx = await _team_ratings(db)
    ratings = ctx["ratings"]
    hr = ratings.get(home)
    ar = ratings.get(away)
    if not hr or not ar:
        return {"reject": "team_no_rating"}

    expected_margin = hr["rating"] - ar["rating"] + HFA_POINTS
    expected_total = (hr["ppg"] + ar["opp_ppg"] + ar["ppg"] + hr["opp_ppg"]) / 2.0

    # Determine favored side from expected_margin.
    fav_is_home = expected_margin >= 0
    fav_team = home if fav_is_home else away
    dog_team = away if fav_is_home else home
    abs_margin = abs(expected_margin)

    # Walk alt-spread ladder from STIFFEST to most generous. For a favored
    # team, the spread is negative (-1.5, -2.5, …). We're looking for the
    # stiffest line the favorite still covers with ≥ min_probability.
    spread_pick = None
    for s in sorted(ALT_SPREAD_LINES, reverse=True):  # closest to 0 first
        # Effective probability of favored team covering "−|s|".
        z = (abs_margin - abs(s)) / SIGMA_MARGIN
        p_cover = _norm_cdf(z)
        if p_cover >= min_probability:
            spread_pick = {
                "market": "spread",
                "team": fav_team,
                "spread": -abs(s),
                "true_probability": round(p_cover, 4),
            }
            break

    # Walk alt-total ladder: the most ASYMMETRIC line gives highest
    # probability — for high-scoring games take a low under-line that's
    # almost certain to be exceeded ("Over 33.5"). For defensive matchups
    # take a high over-line that's almost certain NOT to be exceeded.
    total_pick = None
    for t in ALT_TOTAL_LINES:
        z_over = (expected_total - t) / SIGMA_TOTAL
        p_over = _norm_cdf(z_over)
        p_under = 1.0 - p_over
        best_side_p = max(p_over, p_under)
        side = "Over" if p_over >= p_under else "Under"
        if best_side_p >= min_probability:
            # Keep the FIRST passing line — that's the safest extreme.
            if total_pick is None or best_side_p > total_pick["true_probability"]:
                total_pick = {
                    "market": "total",
                    "total": t,
                    "side": side,
                    "true_probability": round(best_side_p, 4),
                }

    # ML pick
    p_fav = _logistic_margin(abs_margin)
    ml_pick = None
    if p_fav >= min_probability:
        ml_pick = {
            "market": "moneyline",
            "team": fav_team,
            "opponent": dog_team,
            "true_probability": round(p_fav, 4),
        }

    return {
        "matchup": f"{away} @ {home}",
        "favored": fav_team,
        "expected_margin": round(expected_margin, 2),
        "expected_total": round(expected_total, 2),
        "ml_pick": ml_pick,
        "spread_pick": spread_pick,
        "total_pick": total_pick,
        "home_rating": hr,
        "away_rating": ar,
    }


async def team_strength_leaderboard(db, *, limit: int = 32) -> dict:
    """Rank every team in our data set by current rating."""
    ctx = await _team_ratings(db)
    rows = sorted(ctx["ratings"].values(), key=lambda r: r["rating"], reverse=True)
    return {
        "league_ppg": ctx["league_ppg"],
        "n_teams": ctx["n_teams"],
        "teams": rows[: max(1, int(limit))],
    }
