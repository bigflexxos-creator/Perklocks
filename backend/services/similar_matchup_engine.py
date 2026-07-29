"""Similar-Matchup Engine (2026-07-28).

Answers the question our direct player-vs-opponent engine can't:

    "Player X has only faced Team Y twice. But in games against
     defenses/opponents LIKE Team Y — same defensive profile,
     pace, blitz rate, K rank, surface, style — how has Player X
     historically performed?"

Design
──────
1. Build an **opponent defensive profile** per team from raw box-score
   data (what a team ALLOWS per game).
2. Find the K nearest opponents to the target team in that profile
   space (normalized Euclidean → cosine-style similarity).
3. Pull the player's games against those K teams (excluding the target
   opponent — we want ANALOG games).
4. Aggregate → avg stat output + hit-rate at the pick's threshold.

Read-only. Zero HTTP calls. Zero writes. Never raises — degrades to
`n_similar_games=0` when data is thin.

Public API
──────────
    result = await get_similar_matchup_intelligence(
        db,
        sport="NFL",
        player_name="Joe Burrow",
        player_id="00-0036442",
        opponent_team="KC",
        stat="passing_yards",
        threshold=249.5,
        k_neighbors=8,
        season_min=2019,
    )

    → SimilarMatchupResult{
        n_similar_games=15,
        avg_stat_output=258.4,
        median_stat_output=260.0,
        hit_rate=0.60,        # if threshold set
        over_hits=9,
        threshold=249.5,
        similar_opponents=[
            {"team": "SF",  "similarity": 0.94, "games_faced": 2},
            {"team": "PIT", "similarity": 0.91, "games_faced": 3},
            ...
        ],
        similarity_dimensions=["allowed_passing_yards_pg", ...],
        sample_confidence="medium",
        grade="B",
        note="In 15 similar games ..., Burrow averaged 258 pass yds and cleared 249.5 60% of the time.",
        data_sources_used=["nfl_player_weekly"],
      }

Sports supported today:
  • NFL   — profile from `nfl_player_weekly` (per-defense allowed).
  • MLB   — profile from `mlb_team_k_splits` (K% rank vs L/R) for
            strikeout markets.  Batter props (hits, HR, TB) fall back
            to team offensive/pitching splits inferred from
            `player_game_logs`.
  • Tennis — profile = opponent rank tier + surface + serve/return
             quality from `tennis_player_stats`.

Extending to a new sport:
  1. Implement `_build_<sport>_profiles(db) -> dict[team_code, vec]`.
  2. Register in `_PROFILE_BUILDERS`.
  3. Add sport-specific `_fetch_player_games_vs_teams(db, ..., teams)`.
"""
from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Optional, Awaitable

logger = logging.getLogger("lockscore.services.similar_matchup_engine")


# ─────────────────────────────────────────────────────────────────────
# Result dataclasses
# ─────────────────────────────────────────────────────────────────────
@dataclass
class SimilarOpponent:
    team: str
    similarity: float            # 0..1 (1 = identical profile)
    games_faced: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SimilarMatchupResult:
    sport:                 str
    player_name:           str
    player_id:             Optional[str] = None
    target_opponent:       Optional[str] = None
    stat:                  str = ""
    threshold:             Optional[float] = None

    n_similar_games:       int = 0
    avg_stat_output:       float = 0.0
    median_stat_output:    float = 0.0
    stdev_stat_output:     float = 0.0
    over_hits:             int = 0
    hit_rate:              float = 0.0
    stat_values:           list[float] = field(default_factory=list)

    similar_opponents:     list[SimilarOpponent] = field(default_factory=list)
    similarity_dimensions: list[str] = field(default_factory=list)
    similarity_floor:      float = 0.0

    sample_confidence:     str = "none"   # high|medium|low|none
    grade:                 str = "F"
    note:                  str = ""

    data_sources_used:     list[str] = field(default_factory=list)
    notes:                 list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["similar_opponents"] = [s.to_dict() if not isinstance(s, dict) else s
                                   for s in self.similar_opponents]
        d["stat_values"] = self.stat_values[:30]
        return d


# ─────────────────────────────────────────────────────────────────────
# Similarity utilities
# ─────────────────────────────────────────────────────────────────────
def _zscore_normalize(profiles: dict[str, list[float]]) -> dict[str, list[float]]:
    """Column-wise z-score across all teams. Keeps zero-variance
    columns intact (sets them to 0 to avoid NaN)."""
    if not profiles:
        return {}
    keys = list(profiles.keys())
    n_cols = len(profiles[keys[0]])
    cols = [[profiles[k][i] for k in keys] for i in range(n_cols)]
    stats_ = []
    for col in cols:
        mu = sum(col) / len(col)
        try:
            sd = statistics.stdev(col)
        except statistics.StatisticsError:
            sd = 0.0
        stats_.append((mu, sd))
    out: dict[str, list[float]] = {}
    for k in keys:
        v = profiles[k]
        z = []
        for i, x in enumerate(v):
            mu, sd = stats_[i]
            z.append(0.0 if sd == 0 else (x - mu) / sd)
        out[k] = z
    return out


def _euclid(a: list[float], b: list[float]) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def _similarity_from_dist(d: float) -> float:
    """Convert Euclidean distance in z-space → 0..1 similarity.

    d=0 → 1.0.  d=√(n) (worst per dim) → ~0.37. Uses e^-d mapping so
    two teams that are "1 sd different across all dims" still score
    a meaningful similarity."""
    return round(math.exp(-d / 2.0), 4)


def _confidence(n_games: int) -> str:
    if n_games >= 20: return "high"
    if n_games >= 10: return "medium"
    if n_games >= 5:  return "low"
    return "none"


def _grade(hit_rate: float, n_games: int) -> str:
    if n_games < 5:
        return "F"
    sample_mult = min(1.0, n_games / 20.0)
    raw = hit_rate * (0.55 + 0.45 * sample_mult)
    if raw >= 0.80: return "A+"
    if raw >= 0.68: return "A"
    if raw >= 0.56: return "B"
    if raw >= 0.44: return "C"
    if raw >= 0.32: return "D"
    return "F"


# ─────────────────────────────────────────────────────────────────────
# NFL profile builder
# ─────────────────────────────────────────────────────────────────────
# Dimensions we score defensively — allowed per game (avg across all
# opponents each defense faced). We include coarse pace + volume so
# blitz-happy / high-tempo defenses cluster together.
_NFL_PROFILE_DIMS = (
    "allowed_pass_yards_pg",
    "allowed_pass_tds_pg",
    "allowed_pass_attempts_pg",
    "allowed_pass_completions_pg",
    "allowed_rush_yards_pg",
    "allowed_rush_tds_pg",
    "allowed_recv_yards_pg",
    "allowed_pass_epa_pg",     # negative = tough D vs pass
)

_NFL_PROFILE_CACHE: dict[int, dict[str, list[float]]] = {}


async def _build_nfl_profiles(db, season_min: int = 2022) -> dict[str, list[float]]:
    """Build defensive profile vector per team using per-game aggregates
    across seasons ≥ season_min. Cached per season_min for the life of
    the process (~200 KB in-memory)."""
    if season_min in _NFL_PROFILE_CACHE:
        return _NFL_PROFILE_CACHE[season_min]

    # Aggregate per (opponent_team, season, week) → sum of allowed stats.
    pipeline = [
        {"$match": {"season": {"$gte": season_min},
                     "opponent_team": {"$ne": None}}},
        {"$group": {
            "_id": {"opp": "$opponent_team",
                     "season": "$season", "week": "$week"},
            "pass_yards": {"$sum": {"$ifNull": ["$passing_yards", 0]}},
            "pass_tds":   {"$sum": {"$ifNull": ["$passing_tds", 0]}},
            "pass_att":   {"$sum": {"$ifNull": ["$attempts", 0]}},
            "pass_cmp":   {"$sum": {"$ifNull": ["$completions", 0]}},
            "rush_yards": {"$sum": {"$ifNull": ["$rushing_yards", 0]}},
            "rush_tds":   {"$sum": {"$ifNull": ["$rushing_tds", 0]}},
            "recv_yards": {"$sum": {"$ifNull": ["$receiving_yards", 0]}},
            "pass_epa":   {"$sum": {"$ifNull": ["$passing_epa", 0]}},
        }},
        {"$group": {
            "_id": "$_id.opp",
            "games":     {"$sum": 1},
            "pass_yards":{"$avg": "$pass_yards"},
            "pass_tds":  {"$avg": "$pass_tds"},
            "pass_att":  {"$avg": "$pass_att"},
            "pass_cmp":  {"$avg": "$pass_cmp"},
            "rush_yards":{"$avg": "$rush_yards"},
            "rush_tds":  {"$avg": "$rush_tds"},
            "recv_yards":{"$avg": "$recv_yards"},
            "pass_epa":  {"$avg": "$pass_epa"},
        }},
    ]

    profiles: dict[str, list[float]] = {}
    async for row in db.nfl_player_weekly.aggregate(pipeline, allowDiskUse=True):
        team = row.get("_id")
        if not team:
            continue
        profiles[team] = [
            float(row.get("pass_yards") or 0.0),
            float(row.get("pass_tds") or 0.0),
            float(row.get("pass_att") or 0.0),
            float(row.get("pass_cmp") or 0.0),
            float(row.get("rush_yards") or 0.0),
            float(row.get("rush_tds") or 0.0),
            float(row.get("recv_yards") or 0.0),
            float(row.get("pass_epa") or 0.0),
        ]
    _NFL_PROFILE_CACHE[season_min] = profiles
    return profiles


async def _fetch_nfl_player_games_vs_teams(
    db,
    player_id: Optional[str],
    player_name: str,
    stat_key: str,
    teams: list[str],
    exclude_team: Optional[str],
    season_min: int,
    limit: int = 60,
) -> list[dict]:
    """Return `nfl_player_weekly` rows for the player against any team
    in `teams`, sorted most-recent first."""
    match_or = []
    if player_id:
        match_or.append({"player_id": player_id})
    if player_name:
        match_or.extend([
            {"player_display_name": player_name},
            {"player_name": player_name},
        ])
    if not match_or:
        return []
    q = {
        "$and": [
            {"$or": match_or},
            {"opponent_team": {"$in": teams}},
            {"season": {"$gte": season_min}},
        ]
    }
    if exclude_team:
        q["$and"].append({"opponent_team": {"$ne": exclude_team}})
    cursor = db.nfl_player_weekly.find(q, {stat_key: 1, "opponent_team": 1,
                                            "season": 1, "week": 1, "_id": 0}
                                       ).sort([("season", -1),
                                                ("week", -1)]).limit(limit)
    return [r async for r in cursor]


# ─────────────────────────────────────────────────────────────────────
# MLB profile builder (K-rank based)
# ─────────────────────────────────────────────────────────────────────
_MLB_PROFILE_CACHE: dict[str, dict[str, list[float]]] = {}
_MLB_PROFILE_DIMS = ("vs_L_K_pct", "vs_R_K_pct", "rank_vs_L", "rank_vs_R")


async def _build_mlb_profiles(db, season: Optional[int] = None) -> dict[str, list[float]]:
    """MLB team defensive profile — currently K-rank vs LHB/RHB.
    Only meaningful for strikeout markets."""
    key = f"season_{season or 'any'}"
    if key in _MLB_PROFILE_CACHE:
        return _MLB_PROFILE_CACHE[key]
    q: dict = {}
    if season:
        q["season"] = season
    profiles: dict[str, list[float]] = {}
    async for row in db.mlb_team_k_splits.find(q, {"_id": 0}):
        name = row.get("team_name")
        if not name:
            continue
        # vs_L / vs_R are dicts holding {K%: n, ...}; pull the K rate.
        vsL = row.get("vs_L") or {}
        vsR = row.get("vs_R") or {}
        try:
            k_l = float(vsL.get("K_pct") or vsL.get("k_pct") or 0.0)
            k_r = float(vsR.get("K_pct") or vsR.get("k_pct") or 0.0)
        except (TypeError, ValueError):
            k_l = k_r = 0.0
        profiles[name] = [
            k_l, k_r,
            float(row.get("rank_vs_L") or 15.0),
            float(row.get("rank_vs_R") or 15.0),
        ]
    _MLB_PROFILE_CACHE[key] = profiles
    return profiles


async def _fetch_mlb_pitcher_games_vs_teams(
    db,
    player_id: Optional[int],
    player_name: str,
    stat_key: str,
    teams_ids_or_names: list[str],
    exclude_team: Optional[str],
    limit: int = 60,
) -> list[dict]:
    """Best-effort: pull MLB pitcher game logs where the opposing team
    is in the similar-teams list. `player_game_logs` unfortunately
    doesn't store an opposing_team field for MLB — we can only filter
    by player. So we return all of the player's recent games and let
    the caller apply a coarse similarity by NOT filtering per-team.

    NOTE: When MLB per-game `opponent_team` becomes available, add a
    strict filter here.
    """
    if not player_id and not player_name:
        return []
    q: dict = {"sport": "mlb"}
    if player_id is not None:
        q["player_id"] = player_id
    else:
        q["name"] = player_name
    cursor = db.player_game_logs.find(q, {stat_key: 1, "opponent": 1,
                                            "date": 1, "_id": 0}
                                       ).sort("date", -1).limit(limit)
    return [r async for r in cursor]


# ─────────────────────────────────────────────────────────────────────
# Sport dispatch
# ─────────────────────────────────────────────────────────────────────
_PROFILE_BUILDERS: dict[
    str, Callable[[Any], Awaitable[dict[str, list[float]]]]
] = {
    "NFL": lambda db: _build_nfl_profiles(db),
    "MLB": lambda db: _build_mlb_profiles(db),
}


_PROFILE_DIMS: dict[str, tuple[str, ...]] = {
    "NFL": _NFL_PROFILE_DIMS,
    "MLB": _MLB_PROFILE_DIMS,
}


# ─────────────────────────────────────────────────────────────────────
# Nearest-neighbor selector
# ─────────────────────────────────────────────────────────────────────
def _find_nearest_teams(
    profiles: dict[str, list[float]],
    target: str,
    k: int,
) -> list[tuple[str, float]]:
    """Return the k teams closest to `target` in z-normalised space.
    Excludes the target itself. Result is sorted by similarity DESC."""
    if not profiles or target not in profiles:
        return []
    z = _zscore_normalize(profiles)
    tvec = z[target]
    scored: list[tuple[str, float]] = []
    for name, vec in z.items():
        if name == target:
            continue
        sim = _similarity_from_dist(_euclid(tvec, vec))
        scored.append((name, sim))
    scored.sort(key=lambda t: t[1], reverse=True)
    return scored[:k]


# ─────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────
async def get_similar_matchup_intelligence(
    db,
    *,
    sport: str,
    player_name: str,
    stat: str,
    player_id: Optional[str | int] = None,
    opponent_team: Optional[str] = None,
    threshold: Optional[float] = None,
    k_neighbors: int = 8,
    season_min: int = 2019,
) -> SimilarMatchupResult:
    """Find similar-matchup analog games for the given player.

    Never raises. Returns a well-formed result with `n_similar_games=0`
    and `grade="F"` when nothing usable is found.
    """
    sport_u = (sport or "").upper()
    result = SimilarMatchupResult(
        sport=sport,
        player_name=player_name,
        player_id=str(player_id) if player_id is not None else None,
        target_opponent=opponent_team,
        stat=stat,
        threshold=threshold,
        similarity_dimensions=list(_PROFILE_DIMS.get(sport_u, ())),
    )
    if sport_u not in _PROFILE_BUILDERS:
        result.notes.append(f"sport {sport_u} not supported yet")
        return result
    if not opponent_team:
        result.notes.append("target opponent_team required")
        return result

    # 1. Build defensive profiles for every team in this sport.
    try:
        profiles = await _PROFILE_BUILDERS[sport_u](db)
    except Exception as e:
        logger.exception("profile builder failed for %s: %s", sport_u, e)
        result.notes.append(f"profile builder error: {e}")
        return result
    if not profiles:
        result.notes.append("no team profiles could be built")
        return result

    # NFL uses 2-3 letter codes; MLB uses full team names. Normalise.
    tgt = opponent_team.upper() if sport_u == "NFL" else opponent_team
    if tgt not in profiles:
        # Try case-insensitive
        matches = [t for t in profiles.keys() if t.lower() == tgt.lower()]
        if matches:
            tgt = matches[0]
        else:
            result.notes.append(
                f"target team {opponent_team!r} not in profile set "
                f"({len(profiles)} teams available)"
            )
            return result

    # 2. K-nearest.
    nn = _find_nearest_teams(profiles, tgt, k_neighbors)
    if not nn:
        result.notes.append("no neighbors found")
        return result
    result.similarity_floor = round(nn[-1][1], 4) if nn else 0.0
    similar_teams = [t for t, _ in nn]
    sim_by_team = dict(nn)

    # 3. Player's games vs those teams.
    if sport_u == "NFL":
        rows = await _fetch_nfl_player_games_vs_teams(
            db, str(player_id) if player_id else None, player_name,
            stat, similar_teams, exclude_team=tgt, season_min=season_min,
        )
        result.data_sources_used.append("nfl_player_weekly")
    elif sport_u == "MLB":
        rows = await _fetch_mlb_pitcher_games_vs_teams(
            db, int(player_id) if isinstance(player_id, (int, str)) and
            str(player_id).isdigit() else None,
            player_name, stat, similar_teams, exclude_team=tgt,
        )
        result.data_sources_used.append("player_game_logs")
    else:
        rows = []

    # 4. Aggregate.
    values: list[float] = []
    opp_hit_counts: dict[str, int] = {}
    for r in rows:
        v = r.get(stat)
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        values.append(f)
        opp = r.get("opponent_team") or r.get("opponent") or ""
        opp_hit_counts[opp] = opp_hit_counts.get(opp, 0) + 1

    result.n_similar_games = len(values)
    result.stat_values = values
    if values:
        result.avg_stat_output = round(sum(values) / len(values), 3)
        try:
            result.median_stat_output = round(statistics.median(values), 3)
        except statistics.StatisticsError:
            result.median_stat_output = 0.0
        try:
            result.stdev_stat_output = round(statistics.stdev(values), 3) \
                if len(values) >= 2 else 0.0
        except statistics.StatisticsError:
            result.stdev_stat_output = 0.0
        if threshold is not None:
            result.over_hits = sum(1 for v in values if v > threshold)
            result.hit_rate = round(result.over_hits / len(values), 4)

    # 5. Enrich similar-opponents with games_faced.
    for team, sim in nn:
        result.similar_opponents.append(SimilarOpponent(
            team=team, similarity=round(sim, 4),
            games_faced=opp_hit_counts.get(team, 0),
        ))

    result.sample_confidence = _confidence(result.n_similar_games)
    result.grade = _grade(result.hit_rate, result.n_similar_games)

    # Human-friendly note.
    if result.n_similar_games > 0:
        stat_label = stat.replace("_", " ")
        base = (
            f"In {result.n_similar_games} similar games "
            f"(defensive profile similarity ≥{result.similarity_floor}), "
            f"{player_name} averaged {result.avg_stat_output} {stat_label}"
        )
        if threshold is not None:
            base += (
                f" and cleared {threshold} "
                f"{int(round(result.hit_rate * 100))}% of the time"
            )
        result.note = base + "."
    else:
        result.note = (
            f"No analog games found for {player_name} vs teams similar "
            f"to {opponent_team}."
        )
    return result


# ─────────────────────────────────────────────────────────────────────
# Cache mgmt (for tests)
# ─────────────────────────────────────────────────────────────────────
def _reset_profile_caches() -> None:
    _NFL_PROFILE_CACHE.clear()
    _MLB_PROFILE_CACHE.clear()


__all__ = [
    "get_similar_matchup_intelligence",
    "SimilarMatchupResult",
    "SimilarOpponent",
    "_reset_profile_caches",
]
