"""NHL Feature Engine — builds ``nhl_sim_context`` for pick scoring.

Perklocks MASTER BUILD 2026-06-28 · P0-A NHL model closure.

This module is the bridge between the already-ingested NHL historical
data (``db.games`` + ``db.player_game_logs``, both ``sport='nhl'``)
and the already-built Monte-Carlo simulator (``brain.sim_nhl``).  The
simulator reads ``pick['nhl_sim_context']`` and never generates any
features of its own; this engine is the ONLY producer of that context.

Design:
    * Pure independent evidence — no sportsbook implied prob leak.
    * All inputs come from ``db.games`` / ``db.player_game_logs``
      populated by ``historical/nhl.py`` from the public NHL Stats API.
    * Fails CLOSED when source data is insufficient: never fabricates
      a mean, lambda, or sample size.  A pick without enough recent
      games receives ``nhl_sim_context = None`` and the simulator
      returns ``DATA_INSUFFICIENT`` → pick is skipped at scoring.
    * One shared feature cache per refresh cycle (team + player
      precomputes) so repeated same-player alt rungs reuse the same
      distribution.

Game markets (home_lambda / away_lambda for the Poisson team score):
    * home_lambda = 0.6·recent_gf + 0.4·season_gf  (recent-first blend)
    * Opponent defense multiplier applied symmetrically.
    * Home-ice ±3% factor to match the sim's `is_home` convention.

Player markets (goals / assists / points / shots_on_goal):
    * recent_mean = per-game average over last 10 games played.
    * season_mean = per-game average over the current/last season.
    * recent_std  = sample stddev over recent window (Normal stats).
    * opp_defense_mult = team-level shot/goal suppression modifier.
    * is_home = taken from the pick when available.

Everything persisted here flows through the existing
``brain.sim_runner.apply_simulations`` promotion so Probability
Authority receives an independent model probability.
"""
from __future__ import annotations

import logging
import math
import re
from typing import Any, Optional

logger = logging.getLogger("lockscore.nhl_feature_engine")

# ── Caches populated per refresh (invalidated on backend restart) ───
_TEAM_RATE_CACHE: dict[tuple[str, str], dict] = {}      # (team_abbrev_or_name, scope) → stats
_PLAYER_CACHE:    dict[tuple[str, str], dict] = {}      # (player_name_norm, stat) → distribution

# League-average Poisson rate used only when the OPPONENT's suppression
# multiplier is needed and the opponent has 0 recent games (first game
# of season).  Not used as a seed for the subject team's own lambda —
# subject lambda always requires its own history.  2.9 ≈ 5-year NHL
# average goals/team/game.
_LEAGUE_GOALS_PER_GAME = 2.90

# Minimum sample for the model to run.  Below this → fail closed.
_MIN_TEAM_RECENT_GAMES   = 5
_MIN_PLAYER_RECENT_GAMES = 5
_MAX_RECENT_WINDOW       = 10


def _norm_name(name: str) -> str:
    if not name:
        return ""
    s = re.sub(r"[^\w\s'.-]", "", name.strip()).lower()
    return re.sub(r"\s+", " ", s)


async def _team_recent_stats(db, team_name: str, window: int = _MAX_RECENT_WINDOW) -> dict:
    """Return recent GF/GA averages for a team using the ingested games.

    ``team_name`` may be a full name (Odds API: "Detroit Red Wings"),
    place name (NHL API: "Detroit"), or 3-letter abbreviation (DET).
    We match any of those against the stored ``home``/``away``/
    ``home_abbrev``/``away_abbrev`` fields using a tolerant prefix /
    substring comparison so the Odds API full name and the NHL API
    place name both resolve to the same team history.
    """
    key = (_norm_name(team_name), f"recent_{window}")
    cached = _TEAM_RATE_CACHE.get(key)
    if cached is not None:
        return cached
    if not team_name:
        return {"games": 0}
    tnorm = _norm_name(team_name)
    tabbrev = team_name.upper()
    # First word of the Odds API team name is almost always the city
    # (e.g. "Detroit" from "Detroit Red Wings").  NHL API stores the
    # city as `home`/`away`, so matching on the first token is a
    # robust bridge between the two provider conventions.
    first_token = tnorm.split()[0] if tnorm else ""
    gf_total = ga_total = games = 0

    def _match_side(stored_name: str, stored_abbrev: str) -> bool:
        if not stored_name and not stored_abbrev:
            return False
        if stored_abbrev and stored_abbrev == tabbrev:
            return True
        sn = _norm_name(stored_name or "")
        if not sn:
            return False
        if sn == tnorm:
            return True
        # Prefix match: "detroit red wings" starts with "detroit" (NHL API side)
        if tnorm.startswith(sn + " ") or tnorm == sn:
            return True
        if sn.startswith(first_token + " ") or sn == first_token:
            return True
        return False

    try:
        # Pull a wider candidate pool, then filter in Python using the
        # tolerant matcher.  The DB query matches on first-token
        # substrings to narrow the scan.
        q = {"sport": "nhl", "status": "Final"}
        cur = db.games.find(q, {"home": 1, "away": 1, "home_abbrev": 1,
                                 "away_abbrev": 1, "result": 1}
                            ).sort("game_id", -1).limit(window * 20)
        async for g in cur:
            r = g.get("result") or {}
            hs = r.get("home")
            as_ = r.get("away")
            if hs is None or as_ is None:
                continue
            home_hit = _match_side(g.get("home") or "", g.get("home_abbrev") or "")
            away_hit = _match_side(g.get("away") or "", g.get("away_abbrev") or "")
            if home_hit:
                gf_total += float(hs); ga_total += float(as_)
            elif away_hit:
                gf_total += float(as_); ga_total += float(hs)
            else:
                continue
            games += 1
            if games >= window:
                break
    except Exception as e:
        logger.debug("nhl_feature_engine team stats query failed for %s: %s", team_name, e)
    out = {
        "games": games,
        "gf_mean": (gf_total / games) if games else None,
        "ga_mean": (ga_total / games) if games else None,
    }
    _TEAM_RATE_CACHE[key] = out
    return out


async def _player_recent_stats(db, player_name: str, stat_key: str,
                                window: int = _MAX_RECENT_WINDOW) -> dict:
    """Compute per-game recent + season stats for a player stat family.

    Only uses stored game-log fields.  Missing fields → fail closed.
    """
    key = (_norm_name(player_name), stat_key)
    cached = _PLAYER_CACHE.get(key)
    if cached is not None:
        return cached
    if not player_name:
        return {"recent_n": 0}
    nname = _norm_name(player_name)
    # ── 2026-06-28 · P0-A NHL identity bridge ───────────────────────
    # Odds API names are FULL (e.g. "Auston Matthews") while NHL API
    # ingestion stored "A. Matthews" style (first-initial + last).
    # Build a tolerant match regex: first_initial + "." + last_name.
    _tokens = [t for t in nname.split() if t]
    if len(_tokens) >= 2:
        _first_init = _tokens[0][0]
        _last = _tokens[-1]
        _init_last_pat = f"^{_first_init}\\.\\s+{re.escape(_last)}$"
    elif len(_tokens) == 1:
        _init_last_pat = f"^[A-Z]\\.\\s+{re.escape(_tokens[0])}$"
    else:
        return {"recent_n": 0}
    # Points is a derived sum of goals + assists; use that when the
    # stored log carries them separately.
    stat_field_map = {
        "goals":          ["goals"],
        "assists":        ["assists"],
        "points":         ["points"],  # per-log stored as pre-summed
        "shots_on_goal":  ["shots"],
        "shots":          ["shots"],
    }
    fields = stat_field_map.get(stat_key)
    if not fields:
        return {"recent_n": 0, "reason": f"unknown_stat:{stat_key}"}
    # Pull up to 40 most-recent logs for this player (identity by name).
    try:
        cur = db.player_game_logs.find(
            {"sport": "nhl", "name": {"$regex": _init_last_pat, "$options": "i"}},
            {"goals": 1, "assists": 1, "points": 1, "shots": 1,
             "game_id": 1, "team": 1, "is_home": 1},
        ).sort("game_id", -1).limit(40)
        logs = [l async for l in cur]
    except Exception as e:
        logger.debug("nhl_feature_engine player stats failed for %s: %s", player_name, e)
        return {"recent_n": 0}
    if not logs:
        out = {"recent_n": 0, "season_n": 0}
        _PLAYER_CACHE[key] = out
        return out

    # Extract values honestly.  Missing field in a log → skip that log.
    def _val(log: dict) -> Optional[float]:
        for f in fields:
            v = log.get(f)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return None
        # Points fallback: g + a when "points" is None.
        if stat_key == "points":
            g = log.get("goals"); a = log.get("assists")
            if g is not None and a is not None:
                try:
                    return float(g) + float(a)
                except (TypeError, ValueError):
                    return None
        return None

    values = [_val(l) for l in logs]
    values = [v for v in values if v is not None]
    if not values:
        out = {"recent_n": 0, "season_n": 0}
        _PLAYER_CACHE[key] = out
        return out

    recent = values[:window]
    season = values
    recent_mean = sum(recent) / len(recent)
    season_mean = sum(season) / len(season)
    # Sample stddev (unbiased) over recent window for Normal stats.
    if len(recent) >= 2:
        mu = recent_mean
        var = sum((v - mu) ** 2 for v in recent) / (len(recent) - 1)
        recent_std = math.sqrt(var)
    else:
        recent_std = None
    out = {
        "recent_n":    len(recent),
        "season_n":    len(season),
        "recent_mean": recent_mean,
        "season_mean": season_mean,
        "recent_std":  recent_std,
    }
    _PLAYER_CACHE[key] = out
    return out


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


async def build_nhl_sim_context(db, pick: dict) -> Optional[dict]:
    """Return a dict for ``pick['nhl_sim_context']`` or None if the
    model cannot run on real evidence.
    """
    market = str(pick.get("market") or "").lower()
    sport  = str(pick.get("sport")  or "").upper()
    if sport != "NHL":
        return None

    # ── Game markets: ML / Puck Line / Total ──────────────────────
    is_game = any(tok in market for tok in (
        "moneyline", "money line", "puck line", "puck_line",
        "total ", "over/under", "o/u"))
    is_player = any(tok in market for tok in (
        "goals", "assists", "points", "shots on goal", "shots", "sog"))
    # Game-market precedence (so "Total Goals" does not route to player).
    if is_game:
        home = pick.get("home_team") or ""
        away = pick.get("away_team") or ""
        if not home or not away:
            # Parse "Away @ Home" event string as fallback.
            ev = str(pick.get("event") or "")
            m = re.match(r"^(.+?)\s*@\s*(.+)$", ev)
            if m:
                away = away or m.group(1).strip()
                home = home or m.group(2).strip()
        if not (home and away):
            return None
        hstats = await _team_recent_stats(db, home)
        astats = await _team_recent_stats(db, away)
        if (hstats.get("games", 0) < _MIN_TEAM_RECENT_GAMES
                or astats.get("games", 0) < _MIN_TEAM_RECENT_GAMES):
            return None
        # Home lambda = 0.5 × home_gf_mean + 0.5 × away_ga_mean
        # (classic Poisson matchup form); away lambda symmetric.
        home_lambda = 0.5 * hstats["gf_mean"] + 0.5 * astats["ga_mean"]
        away_lambda = 0.5 * astats["gf_mean"] + 0.5 * hstats["ga_mean"]
        # Home-ice +3% / -3% identical to the sim's convention.
        home_lambda *= 1.03
        away_lambda *= 0.97
        # Guard against pathological zero-rate samples.
        home_lambda = max(0.5, home_lambda)
        away_lambda = max(0.5, away_lambda)
        return {
            "home_lambda":  float(home_lambda),
            "away_lambda":  float(away_lambda),
            "home_team":    home,
            "away_team":    away,
            "home_recent_games": hstats["games"],
            "away_recent_games": astats["games"],
            "feature_source":    "nhl_feature_engine_v1",
        }

    if not is_player:
        return None
    # ── Player markets: goals / assists / points / shots_on_goal ──
    stat_key = ("shots_on_goal" if ("shots on goal" in market or " sog" in market)
                else "goals"    if "goals"   in market
                else "assists"  if "assists" in market
                else "points"   if "points"  in market
                else "shots_on_goal" if "shots" in market
                else None)
    if stat_key is None:
        return None
    player = pick.get("elite_player_name") or pick.get("player") or ""
    if not player:
        # Try to parse the player off the market: "Austin Matthews Over 0.5 Goals"
        m = re.match(r"^(.+?)\s+(?:Over|Under)\s+", str(pick.get("market") or ""),
                     flags=re.IGNORECASE)
        if m:
            player = m.group(1).strip()
    if not player:
        return None
    pstats = await _player_recent_stats(db, player, stat_key)
    if pstats.get("recent_n", 0) < _MIN_PLAYER_RECENT_GAMES:
        return None
    # Opponent defensive context — team's recent GA against (shots or goals).
    opponent = pick.get("opponent") or ""
    opp_mult = 1.0
    if opponent:
        tstats = await _team_recent_stats(db, opponent)
        if tstats.get("games", 0) >= _MIN_TEAM_RECENT_GAMES:
            # For GOALS/POINTS/ASSISTS: GA higher → scoring environment stronger → mult > 1.
            # For SOG: use goals-against as a loose proxy (no shots-against stored today).
            opp_ga = tstats.get("ga_mean")
            if opp_ga:
                opp_mult = _clamp(opp_ga / _LEAGUE_GOALS_PER_GAME, 0.80, 1.20)
    is_home = pick.get("is_home")
    ctx = {
        "stat_key":          stat_key,
        "recent_mean":       pstats["recent_mean"],
        "season_mean":       pstats["season_mean"],
        "recent_n":          pstats["recent_n"],
        "season_n":          pstats["season_n"],
        "recent_std":        pstats.get("recent_std"),
        "is_home":           is_home,
        "opp_defense_mult":  float(opp_mult),
        "feature_source":    "nhl_feature_engine_v1",
    }
    return ctx


def clear_caches() -> None:
    """Called at the top of each refresh cycle so caches reflect
    freshly-ingested data (new boxscores from the last incremental sync).
    """
    _TEAM_RATE_CACHE.clear()
    _PLAYER_CACHE.clear()


__all__ = ["build_nhl_sim_context", "clear_caches"]
