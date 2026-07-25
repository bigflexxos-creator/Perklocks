"""Team-strength engine — computes home/away attack & defense λ from
multi-season historical match results.

Feeds the GoalScorer Engine v3 with per-team Poisson rates used to
sample match totals and split attack between teams.

Data source: `soccer_matches` collection (populated by
`services.soccer.fallback.refresh_all_leagues` — currently 25k+ matches
from football-data.co.uk covering 2022-23 → 2024-25).

Weighting:
    Most recent season   → weight 1.00
    One season prior     → weight 0.65
    Two seasons prior    → weight 0.35   (shrinkage toward league mean)

Shrinkage:
    Bayesian pull toward the league mean using effective-sample-size
    (Dirichlet-Poisson conjugate). Prevents small-sample teams (newly
    promoted, cup entrants) from having wild λ estimates.
"""
from __future__ import annotations

import logging
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("lockscore.player_props.team_strength")


# ── Season weighting (user directive: use last 2 seasons since new season just started)
_SEASON_WEIGHTS: dict[str, float] = {
    # Understat / football-data.co.uk season-slug → weight
    "2024-25": 1.00,
    "2025":    1.00,    # Understat convention
    "2023-24": 0.65,
    "2024":    0.65,
    "2022-23": 0.35,
    "2023":    0.35,
    # current/live match season codes
    "2025-26": 1.00,
    "2026-27": 1.00,
}

# Season → Understat league label mapping (for cross-referencing).
# Fallback default weighting for unknown seasons.
_DEFAULT_WEIGHT = 0.15

# Bayesian shrinkage: matches-equivalent prior mass toward league mean.
_PRIOR_MATCHES = 12

# Sport-key / friendly-name → football-data.co.uk league code.
LEAGUE_CODE_MAP: dict[str, str] = {
    "soccer_epl":                    "EPL",
    "soccer_spain_la_liga":          "LaLiga",
    "soccer_italy_serie_a":          "SerieA",
    "soccer_germany_bundesliga":     "Bundesliga",
    "soccer_france_ligue_one":       "Ligue1",
    "soccer_uefa_champs_league":     "UCL",
    "soccer_usa_mls":                "MLS",
    "soccer_netherlands_eredivisie": "Eredivisie",
    "soccer_portugal_primeira_liga": "Primeira",
    "EPL":         "EPL",
    "La_liga":     "LaLiga",
    "LaLiga":      "LaLiga",
    "Serie_A":     "SerieA",
    "SerieA":      "SerieA",
    "Bundesliga":  "Bundesliga",
    "Ligue_1":     "Ligue1",
    "Ligue1":      "Ligue1",
    "UCL":         "UCL",
    "MLS":         "MLS",
}


def _norm_team(name: str) -> str:
    if not name:
        return ""
    nk = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in nk if not unicodedata.combining(c)).lower().strip()
    # Strip only trailing common suffixes; DO NOT strip city/united
    # because those are distinguishing tokens (e.g. Manchester United
    # vs Manchester City would collapse to "manchester").
    for suf in (" f.c.", " fc", " sc", " cf", " football club",
                " afc"):
        if s.endswith(suf):
            s = s[: -len(suf)]
    # Collapse multi-space to single.
    return " ".join(s.split()).strip()


@dataclass
class TeamStrength:
    """Aggregated team attack + defense expected goals per match."""
    league:     str
    team:       str
    matches:    int = 0
    # Empirical means across weighted seasons
    home_gf:    float = 0.0     # goals scored at home
    home_ga:    float = 0.0     # goals conceded at home
    away_gf:    float = 0.0
    away_ga:    float = 0.0
    home_matches: int = 0
    away_matches: int = 0
    # Shrunk λs (post-Bayesian pull toward league mean)
    lam_attack_home:  float = 0.0
    lam_defense_home: float = 0.0
    lam_attack_away:  float = 0.0
    lam_defense_away: float = 0.0
    # Provenance
    league_mean_goals: float = 1.30
    seasons_used: list[str] = field(default_factory=list)

    def attack(self, is_home: bool) -> float:
        return self.lam_attack_home if is_home else self.lam_attack_away

    def defense(self, is_home: bool) -> float:
        return self.lam_defense_home if is_home else self.lam_defense_away


@dataclass
class LeagueStrength:
    """League-wide baselines."""
    league:              str
    mean_home_goals:     float = 1.45
    mean_away_goals:     float = 1.15
    mean_total_goals:    float = 2.60
    home_advantage_mult: float = 1.26      # empirical home boost
    matches_used:        int   = 0
    seasons_used:        list[str] = field(default_factory=list)


async def _fetch_matches(db, league: str) -> list[dict]:
    """Return all historical matches (last 3 seasons) for a league."""
    q = {"league": league,
         "status": "finished",
         "home_score": {"$ne": None},
         "away_score": {"$ne": None}}
    cursor = db.soccer_matches.find(q, {
        "_id": 0,
        "home_team": 1, "away_team": 1,
        "home_score": 1, "away_score": 1,
        "season": 1,
    }).limit(5000)
    return await cursor.to_list(length=5000)


def _weight(season: str) -> float:
    return _SEASON_WEIGHTS.get(season, _DEFAULT_WEIGHT)


async def _fetch_standings(db, league: str) -> list[dict]:
    """Fallback: use `soccer_standings` (GF/GA/played) when
    match-level data is unavailable (MLS, Norway, etc)."""
    q = {"league": league}
    cursor = db.soccer_standings.find(q, {
        "_id": 0, "team": 1, "played": 1, "goals_for": 1,
        "goals_against": 1, "season": 1,
    }).limit(200)
    return await cursor.to_list(length=200)


async def _fetch_mls_team_strength(db) -> list[dict]:
    """Last-resort MLS fallback: derive team attack from
    `espn_mls_stats` (sum player goals per team).

    ESPN publishes only top-scorer leaderboards (~3 per team), so raw
    sums undercount team goals. We compute a per-team goal-share of
    the observed distribution then rescale to MLS league mean
    (~1.45 GF/team/match). No GA available → assumes league mean.
    """
    pipeline = [
        {"$group": {
            "_id": "$team",
            "goals":  {"$sum": "$goals"},
            "games":  {"$max": "$games"},
            "player_count": {"$sum": 1},
        }},
    ]
    rows = []
    async for row in db.espn_mls_stats.aggregate(pipeline):
        rows.append(row)
    if not rows:
        return []

    # League mean per-team goals per match (MLS 2024: ~1.45).
    MLS_LEAGUE_MEAN_GF = 1.45

    # First pass: raw per-team GF/game as ranked shares.
    per_team = []
    for r in rows:
        team = r.get("_id") or ""
        raw_g = float(r.get("goals") or 0)
        games = max(1.0, float(r.get("games") or 15))
        per_team.append({"team": team, "raw_gpg": raw_g / games,
                         "games": games})

    # Compute rank-scaled GF where mean(gpg) = MLS_LEAGUE_MEAN_GF.
    n_teams = len(per_team)
    total_gpg = sum(t["raw_gpg"] for t in per_team)
    if total_gpg > 0:
        scale = (MLS_LEAGUE_MEAN_GF * n_teams) / total_gpg
    else:
        scale = 1.0

    out = []
    for t in per_team:
        scaled_gpg = min(2.5, t["raw_gpg"] * scale)   # cap wild outliers
        # Blend heavily toward league mean since sample is sparse.
        blended = 0.4 * scaled_gpg + 0.6 * MLS_LEAGUE_MEAN_GF
        gf = blended * t["games"]
        out.append({"team": t["team"],
                    "played": t["games"],
                    "goals_for":     gf,
                    "goals_against": t["games"] * MLS_LEAGUE_MEAN_GF,
                    "season": "2025"})
    return out


async def build_league_strength(db, league: str) -> tuple[LeagueStrength, dict[str, TeamStrength]]:
    """Return (league baselines, per-team strengths keyed by norm name)."""
    league_code = LEAGUE_CODE_MAP.get(league, league)
    matches = await _fetch_matches(db, league_code)

    lg = LeagueStrength(league=league_code)
    if matches:
        return await _build_from_matches(matches, lg, league_code)

    # Standings-only fallback (e.g. MLS if standings exist).
    standings = await _fetch_standings(db, league_code)
    if standings:
        return _build_from_standings(standings, lg, league_code)

    # MLS-specific: derive from espn_mls_stats aggregation.
    if league_code == "MLS":
        mls_rows = await _fetch_mls_team_strength(db)
        if mls_rows:
            return _build_from_standings(mls_rows, lg, league_code)

    logger.info("team_strength: no data for %s — returning defaults", league_code)
    return lg, {}


async def _build_from_matches(matches: list[dict], lg: LeagueStrength,
                              league_code: str) -> tuple[LeagueStrength, dict[str, TeamStrength]]:
    per_team: dict[str, dict] = {}
    total_w = 0.0
    total_home_g = 0.0
    total_away_g = 0.0
    seasons_seen: set[str] = set()

    for m in matches:
        season = str(m.get("season") or "")
        w = _weight(season)
        if w <= 0:
            continue
        seasons_seen.add(season)
        h = _norm_team(m.get("home_team") or "")
        a = _norm_team(m.get("away_team") or "")
        hs = int(m.get("home_score") or 0)
        as_ = int(m.get("away_score") or 0)

        total_w += w
        total_home_g += hs * w
        total_away_g += as_ * w

        for team, is_home, gf, ga in (
            (h, True, hs, as_),
            (a, False, as_, hs),
        ):
            if not team:
                continue
            d = per_team.setdefault(team, {
                "home_gf": 0.0, "home_ga": 0.0, "home_w": 0.0,
                "away_gf": 0.0, "away_ga": 0.0, "away_w": 0.0,
                "raw_name": m.get("home_team") if is_home else m.get("away_team"),
            })
            if is_home:
                d["home_gf"] += gf * w
                d["home_ga"] += ga * w
                d["home_w"]  += w
            else:
                d["away_gf"] += gf * w
                d["away_ga"] += ga * w
                d["away_w"]  += w

    if total_w > 0:
        lg.mean_home_goals  = total_home_g / total_w
        lg.mean_away_goals  = total_away_g / total_w
        lg.mean_total_goals = lg.mean_home_goals + lg.mean_away_goals
        if lg.mean_away_goals > 0:
            lg.home_advantage_mult = lg.mean_home_goals / max(0.5, lg.mean_away_goals)
    lg.matches_used = len(matches)
    lg.seasons_used = sorted(seasons_seen)

    out: dict[str, TeamStrength] = {}
    for tnorm, d in per_team.items():
        hw = d["home_w"] or 0.001
        aw = d["away_w"] or 0.001
        raw_home_gf = d["home_gf"] / hw
        raw_home_ga = d["home_ga"] / hw
        raw_away_gf = d["away_gf"] / aw
        raw_away_ga = d["away_ga"] / aw

        # Bayesian shrinkage toward league mean
        def _shrink(raw: float, mean: float, w_matches: float) -> float:
            n = max(0.001, w_matches)
            return (n * raw + _PRIOR_MATCHES * mean) / (n + _PRIOR_MATCHES)

        lam_ah = _shrink(raw_home_gf, lg.mean_home_goals, hw)
        lam_dh = _shrink(raw_home_ga, lg.mean_away_goals, hw)
        lam_aa = _shrink(raw_away_gf, lg.mean_away_goals, aw)
        lam_da = _shrink(raw_away_ga, lg.mean_home_goals, aw)

        out[tnorm] = TeamStrength(
            league=league_code,
            team=d["raw_name"] or tnorm.title(),
            matches=int(hw + aw),
            home_matches=int(hw),
            away_matches=int(aw),
            home_gf=round(raw_home_gf, 3),
            home_ga=round(raw_home_ga, 3),
            away_gf=round(raw_away_gf, 3),
            away_ga=round(raw_away_ga, 3),
            lam_attack_home=round(lam_ah, 3),
            lam_defense_home=round(lam_dh, 3),
            lam_attack_away=round(lam_aa, 3),
            lam_defense_away=round(lam_da, 3),
            league_mean_goals=round(lg.mean_total_goals, 3),
            seasons_used=lg.seasons_used,
        )

    logger.info(
        "team_strength[%s]: %d matches, %d teams, seasons=%s, "
        "μ_home=%.2f μ_away=%.2f HA=%.2f",
        league_code, len(matches), len(out), lg.seasons_used,
        lg.mean_home_goals, lg.mean_away_goals, lg.home_advantage_mult,
    )
    return lg, out


def _build_from_standings(standings: list[dict], lg: LeagueStrength,
                          league_code: str) -> tuple[LeagueStrength, dict[str, TeamStrength]]:
    """Derive team strength from a season's standings table.

    Standings give us team-level (played, goals_for, goals_against) —
    no home/away split, so we assume the league-average split ratio.
    """
    total_gf = 0.0
    total_gp = 0.0
    seasons_seen: set[str] = set()
    for s in standings:
        gp = float(s.get("played") or 0)
        gf = float(s.get("goals_for") or 0)
        total_gf += gf
        total_gp += gp
        seasons_seen.add(str(s.get("season") or ""))
    league_gpg = (total_gf / total_gp) if total_gp else 1.30

    # Empirical soccer split: home teams score ~55% of goals.
    lg.mean_home_goals = round(league_gpg * 1.10, 3)
    lg.mean_away_goals = round(league_gpg * 0.90, 3)
    lg.mean_total_goals = lg.mean_home_goals + lg.mean_away_goals
    if lg.mean_away_goals > 0:
        lg.home_advantage_mult = lg.mean_home_goals / lg.mean_away_goals
    lg.matches_used = int(total_gp / 2)
    lg.seasons_used = sorted([s for s in seasons_seen if s])

    out: dict[str, TeamStrength] = {}
    for s in standings:
        team = s.get("team") or ""
        gp = float(s.get("played") or 0) or 0.001
        gf = float(s.get("goals_for") or 0)
        ga = float(s.get("goals_against") or 0)
        gpg = gf / gp
        gapg = ga / gp

        # Bayesian shrinkage.
        def _shrink(raw: float, mean: float) -> float:
            return (gp * raw + _PRIOR_MATCHES * mean) / (gp + _PRIOR_MATCHES)

        lam_atk = _shrink(gpg, league_gpg)
        lam_def = _shrink(gapg, league_gpg)

        tnorm = _norm_team(team)
        out[tnorm] = TeamStrength(
            league=league_code,
            team=team,
            matches=int(gp),
            home_matches=int(gp / 2),
            away_matches=int(gp / 2),
            home_gf=round(gpg * 1.10, 3),   # symmetric split
            home_ga=round(gapg * 0.90, 3),
            away_gf=round(gpg * 0.90, 3),
            away_ga=round(gapg * 1.10, 3),
            lam_attack_home=round(lam_atk * 1.10, 3),
            lam_defense_home=round(lam_def * 0.90, 3),
            lam_attack_away=round(lam_atk * 0.90, 3),
            lam_defense_away=round(lam_def * 1.10, 3),
            league_mean_goals=round(lg.mean_total_goals, 3),
            seasons_used=lg.seasons_used,
        )

    logger.info(
        "team_strength[%s] (standings-only): %d teams, seasons=%s, "
        "μ_gpg=%.2f",
        league_code, len(out), lg.seasons_used, league_gpg,
    )
    return lg, out


def lookup_team(strengths: dict[str, TeamStrength], team_name: str) -> Optional[TeamStrength]:
    """Fuzzy lookup — tries norm, then substring, then abbreviation aliases."""
    if not team_name:
        return None
    n = _norm_team(team_name)
    if not n:
        return None
    if n in strengths:
        return strengths[n]

    # Common abbreviation aliases from Understat / TheOddsAPI / ESPN.
    _ALIASES: dict[str, list[str]] = {
        "man utd":        ["man united", "manchester united"],
        "man united":     ["man utd", "manchester united"],
        "manchester utd": ["man united", "manchester united"],
        "manchester united": ["man utd", "man united"],
        "man city":       ["manchester city"],
        "manchester city": ["man city"],
        "spurs":          ["tottenham", "tottenham hotspur"],
        "tottenham":      ["spurs", "tottenham hotspur"],
        "wolves":         ["wolverhampton", "wolverhampton wanderers"],
        "wolverhampton":  ["wolves"],
        "leeds":          ["leeds united"],
        "leeds united":   ["leeds"],
        "west ham":       ["west ham united"],
        "west ham united":["west ham"],
        "newcastle":      ["newcastle united"],
        "newcastle united":["newcastle"],
        "brighton":       ["brighton and hove albion", "brighton & hove albion"],
        "nott m forest":  ["nottingham forest"],
        "nottingham forest": ["nott m forest", "forest"],
        "sheffield utd":  ["sheffield united"],
        "sheffield united":["sheffield utd"],
        "atletico madrid":["atl madrid", "atletico"],
        "atl madrid":     ["atletico madrid"],
        "athletic bilbao":["athletic club", "ath bilbao"],
        "real madrid":    ["r madrid"],
        "barcelona":      ["fc barcelona"],
        "bayern munich":  ["bayern"],
        "borussia dortmund": ["dortmund", "b dortmund"],
        "psg":            ["paris saint-germain", "paris sg"],
        "paris saint-germain": ["psg"],
        "inter":          ["internazionale", "inter milan"],
        "inter milan":    ["inter", "internazionale"],
    }
    for alt in _ALIASES.get(n, []):
        if alt in strengths:
            return strengths[alt]

    # Partial match.
    for k, v in strengths.items():
        if k in n or n in k:
            return v
    return None


# Simple in-memory cache — refreshed on demand or by admin endpoint.
_CACHE: dict[str, tuple[LeagueStrength, dict[str, TeamStrength]]] = {}
_CACHE_META: dict[str, str] = {}


async def get_league_strength(db, league: str,
                              *, force_refresh: bool = False) -> tuple[LeagueStrength, dict[str, TeamStrength]]:
    """Cached wrapper — first call warms, subsequent hits are O(1)."""
    key = LEAGUE_CODE_MAP.get(league, league)
    if not force_refresh and key in _CACHE:
        return _CACHE[key]
    lg, teams = await build_league_strength(db, key)
    _CACHE[key] = (lg, teams)
    return lg, teams


def clear_cache() -> None:
    _CACHE.clear()
    _CACHE_META.clear()


__all__ = [
    "TeamStrength", "LeagueStrength",
    "build_league_strength", "get_league_strength",
    "lookup_team", "clear_cache",
    "LEAGUE_CODE_MAP",
]
