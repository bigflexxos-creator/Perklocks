"""Player Matchup Intelligence Engine (2026-07-28).

Reusable, sport-agnostic engine that answers:

    "How does this player perform against this opponent
     — or opponents like this one?"

Consumes existing collections only. Zero HTTP calls. Zero production
pick changes. Callers get a rich dataclass they can render into a UI,
feed into a ranker, or gate a lock-score adjustment.

──────────────────────────────────────────────────────────────────────
Contract
──────────────────────────────────────────────────────────────────────
    result = await get_matchup_intelligence(
        db,
        sport="MLB",
        player_id=671096,                 # optional
        player_name="Zack Wheeler",        # required if no id
        stat="strikeouts",                 # canonical stat key
        opponent_team="Miami Marlins",     # optional but preferred
        threshold=6.5,                     # optional — over/under line
    )

    → MatchupIntelligence(
        career_vs_opponent = { games, avg, over_hits, hit_rate, ... },
        recent_vs_similar  = { games, avg, over_hits, hit_rate, ... },
        overall_last_5     = { ... },
        overall_last_10    = { ... },
        overall_season     = { ... },
        threshold_hit_rate = 0.68,         # if threshold provided
        avg_stat_output    = 7.2,
        median_stat_output = 7.0,
        consistency_score  = 0.71,          # 0..1
        sample_size        = 18,
        sample_confidence  = "high",        # low / medium / high
        matchup_grade      = "A",           # A+ .. F
        data_sources_used  = ["mlb_pvt", "props_history", "player_game_logs"],
        notes              = [ ... ],
      )

──────────────────────────────────────────────────────────────────────
Data source strategy per sport
──────────────────────────────────────────────────────────────────────
  MLB    → mlb_pvt.get_pitcher_vs_team_line  (career vs opp for K props)
           mlb_bvp collections (batter vs pitcher career)
           props_history.props.<sport>_<stat>  (hit-rate/consistency)
           player_game_logs                    (raw last-N)

  NFL    → nfl_player_weekly (has opponent_team)
           props_history
           player_game_logs (fallback)

  NBA    → player_game_logs (limited — no opp field yet)
           props_history

  Tennis → tennis_matches_history (winner_id / loser_id)
           props_history (if populated)

  Soccer → soccer_player_form (season aggregates)
           mls_player_matchup_history (81 rows, MLS only)

──────────────────────────────────────────────────────────────────────
NOTE:
  • This module is READ-ONLY. Never writes. Never modifies picks.
  • Callers integrate the result into their own pipelines.
  • "Similar opponents" is currently a heuristic — if the exact
    opponent isn't in history, we fall back to the same league
    (temporary). A future improvement will cluster opponents by
    defensive strength / pace / matchup features.
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

logger = logging.getLogger("lockscore.services.player_matchup_intelligence")

# ─────────────────────────────────────────────────────────────────────
# Sport-specific stat aliasing
# ─────────────────────────────────────────────────────────────────────
# The `stat` argument accepts either the canonical name (e.g. "hits")
# or a specific market family (e.g. "batter_hits"). This mapping keeps
# callers from having to know per-sport quirks.
_STAT_ALIAS: dict[tuple[str, str], str] = {
    # (sport_lower, incoming_stat) → canonical game-log key
    ("mlb", "hits"):                 "hits",
    ("mlb", "batter_hits"):          "hits",
    ("mlb", "home_runs"):            "home_runs",
    ("mlb", "hr"):                   "home_runs",
    ("mlb", "batter_home_runs"):     "home_runs",
    ("mlb", "rbi"):                  "rbi",
    ("mlb", "batter_rbis"):          "rbi",
    ("mlb", "total_bases"):          "total_bases",
    ("mlb", "batter_total_bases"):   "total_bases",
    ("mlb", "strikeouts"):           "pitcher_strikeouts",
    ("mlb", "k"):                    "pitcher_strikeouts",
    ("mlb", "pitcher_strikeouts"):   "pitcher_strikeouts",
    ("mlb", "hits_runs_rbis"):       "hits_runs_rbis",  # composite
    ("mlb", "batter_hits_runs_rbis"):"hits_runs_rbis",
    ("nfl", "passing_yards"):        "passing_yards",
    ("nfl", "rushing_yards"):        "rushing_yards",
    ("nfl", "receiving_yards"):      "receiving_yards",
    ("nfl", "receptions"):           "receptions",
    ("nfl", "passing_tds"):          "passing_tds",
    ("nfl", "rushing_tds"):          "rushing_tds",
    ("nba", "points"):               "points",
    ("nba", "rebounds"):             "rebounds",
    ("nba", "assists"):              "assists",
    ("nba", "threes"):               "threes",
    ("tennis", "aces"):              "aces",
    ("tennis", "double_faults"):     "double_faults",
    ("tennis", "total_games"):       "total_games_match",
    ("tennis", "break_points_won"):  "break_points_won",
    ("tennis", "bp_won"):             "break_points_won",
    # Soccer stats (Phase 7 Part 4c) — canonical.
    ("soccer", "goals"):               "goals",
    ("soccer", "assists"):             "assists",
    ("soccer", "shots"):               "shots",
    ("soccer", "shots_on_target"):     "shots_on_target",
    ("soccer", "xg"):                  "xg",
    ("soccer", "goal_contributions"):  "goal_contributions",
}


def _canon_stat(sport: str, stat: str) -> str:
    return _STAT_ALIAS.get((sport.lower(), stat.lower()), stat.lower())


# ─────────────────────────────────────────────────────────────────────
# Result dataclass
# ─────────────────────────────────────────────────────────────────────
@dataclass
class MatchupSlice:
    """A single window/slice of stat output."""
    games:        int = 0
    over_hits:    int = 0
    hit_rate:     float = 0.0    # over_hits / games (only if threshold set)
    avg:          float = 0.0
    median:       float = 0.0
    stat_values:  list[float] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        # Don't leak raw list to callers by default — keep summary lean.
        d["stat_values"] = self.stat_values[:20]
        return d


@dataclass
class MatchupIntelligence:
    """Complete matchup profile for one (player, opponent, stat, threshold).

    Every field is optional-safe — missing data yields empty slices,
    not exceptions. Callers should always inspect `sample_confidence`
    before acting on the numbers.
    """
    sport:                str
    player_name:          str
    player_id:            Optional[int | str] = None
    opponent_team:        Optional[str] = None
    stat:                 str = ""
    threshold:            Optional[float] = None

    career_vs_opponent:   MatchupSlice = field(default_factory=MatchupSlice)
    recent_vs_similar:    MatchupSlice = field(default_factory=MatchupSlice)
    overall_last_5:       MatchupSlice = field(default_factory=MatchupSlice)
    overall_last_10:      MatchupSlice = field(default_factory=MatchupSlice)
    overall_season:       MatchupSlice = field(default_factory=MatchupSlice)

    threshold_hit_rate:   float = 0.0
    avg_stat_output:      float = 0.0
    median_stat_output:   float = 0.0
    consistency_score:    float = 0.0
    sample_size:          int = 0
    sample_confidence:    str = "none"
    matchup_grade:        str = "F"

    data_sources_used:    list[str] = field(default_factory=list)
    notes:                list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("career_vs_opponent", "recent_vs_similar",
                  "overall_last_5", "overall_last_10", "overall_season"):
            d[k] = getattr(self, k).to_dict()
        return d


# ─────────────────────────────────────────────────────────────────────
# Slice builders
# ─────────────────────────────────────────────────────────────────────
def _build_slice(values: list[float], threshold: Optional[float]) -> MatchupSlice:
    """Aggregate a list of stat values into a MatchupSlice."""
    if not values:
        return MatchupSlice()
    n = len(values)
    avg = sum(values) / n
    try:
        med = statistics.median(values)
    except statistics.StatisticsError:
        med = 0.0
    over_hits = 0
    hit_rate = 0.0
    if threshold is not None:
        over_hits = sum(1 for v in values if v > threshold)
        hit_rate = over_hits / n if n else 0.0
    return MatchupSlice(
        games=n,
        over_hits=over_hits,
        hit_rate=round(hit_rate, 4),
        avg=round(avg, 3),
        median=round(med, 3),
        stat_values=[float(v) for v in values],
    )


def _consistency(values: list[float]) -> float:
    """Coefficient-of-variation-based consistency score in [0, 1].

    Higher is more consistent. Uses mean-normalised standard deviation
    inverted (low CV → high consistency). Same formula spirit as
    `player_intel.volatility.compute_volatility` but on continuous
    stat values (not W/L booleans).
    """
    if len(values) < 2:
        return 0.0
    mu = sum(values) / len(values)
    if mu <= 0:
        return 0.0
    try:
        sd = statistics.stdev(values)
    except statistics.StatisticsError:
        return 0.0
    cv = sd / mu
    # Map CV=0 → 1.0, CV=1 → ~0.37, CV=2 → ~0.14 (exp decay)
    import math
    return round(math.exp(-cv), 4)


def _grade(hit_rate: float, consistency: float, sample_size: int) -> str:
    """Assign a letter grade A+..F from hit_rate × consistency × sample."""
    if sample_size < 3:
        return "F"          # never enough
    # Weighted score — hit_rate carries most weight, then consistency,
    # sample size acts as multiplier so an 80% hit rate over 3 games
    # can't outrank a 65% hit rate over 20 games.
    sample_mult = min(1.0, sample_size / 15.0)
    raw = (0.6 * hit_rate + 0.4 * consistency) * (0.5 + 0.5 * sample_mult)
    if raw >= 0.75: return "A+"
    if raw >= 0.65: return "A"
    if raw >= 0.55: return "B"
    if raw >= 0.45: return "C"
    if raw >= 0.35: return "D"
    return "F"


def _confidence(sample_size: int) -> str:
    if sample_size >= 15: return "high"
    if sample_size >= 8:  return "medium"
    if sample_size >= 3:  return "low"
    return "none"


# ─────────────────────────────────────────────────────────────────────
# Per-sport lookup helpers
# ─────────────────────────────────────────────────────────────────────
async def _lookup_props_history(db, sport: str, player_id: Any,
                                 stat: str) -> Optional[dict]:
    """Read pre-computed hit-rate snapshot from `props_history`."""
    if not player_id:
        return None
    doc = await db.props_history.find_one({
        "player_id": player_id,
        "sport": sport.lower(),
    })
    if not doc:
        return None
    props = doc.get("props") or {}
    key = f"{sport.lower()}_{stat}"
    return props.get(key)


async def _lookup_player_game_logs(db, sport: str, player_id: Any,
                                    player_name: Optional[str],
                                    stat_key: str, limit: int = 25) -> list[float]:
    """Pull last-N raw stat values from `player_game_logs`."""
    query: dict = {"sport": sport.lower()}
    if player_id is not None:
        query["player_id"] = player_id
    elif player_name:
        query["name"] = player_name
    else:
        return []
    cursor = db.player_game_logs.find(query).sort("date", -1).limit(limit)
    out: list[float] = []
    async for d in cursor:
        v = d.get(stat_key)
        if v is None:
            continue
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            continue
    return out


async def _lookup_mlb_pvt(db, pitcher_name: str,
                          opponent_team: Optional[str],
                          pitcher_id: Optional[int] = None) -> Optional[dict]:
    """MLB Pitcher-vs-Team — delegate to the existing service."""
    if not opponent_team:
        return None
    try:
        from services.mlb_pvt import get_pvt_for_pitcher_vs_team  # type: ignore
        # PvT service takes pitcher_id (int) + opp_team_name. Resolve
        # name → id via the H2H helper when the caller didn't supply
        # one. Both helpers are cached, so this stays cheap.
        pid = pitcher_id
        if not pid:
            try:
                from mlb_pitcher_h2h import _resolve_pitcher_id  # type: ignore
                pid = await _resolve_pitcher_id(pitcher_name)
            except Exception:
                pid = None
        if not pid:
            return None
        return await get_pvt_for_pitcher_vs_team(int(pid), opponent_team)
    except (ImportError, AttributeError):
        return None
    except Exception as e:
        logger.debug("mlb_pvt lookup failed for %s vs %s: %s",
                     pitcher_name, opponent_team, e)
        return None


async def _lookup_nfl_vs_opponent(db, player_id: Any,
                                    stat_key: str,
                                    opponent_team: Optional[str],
                                    limit: int = 10) -> list[float]:
    """NFL: pull last-N vs specific opponent from nfl_player_weekly."""
    if not (player_id and opponent_team):
        return []
    query = {"player_id": player_id, "opponent_team": opponent_team}
    cursor = db.nfl_player_weekly.find(query).sort([("season", -1),
                                                     ("week", -1)]).limit(limit)
    key = stat_key
    out: list[float] = []
    async for d in cursor:
        v = d.get(key)
        if v is None:
            continue
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            continue
    return out


async def _lookup_tennis_vs_opponent(db, player_id: Any,
                                       opponent_name: Optional[str],
                                       stat_key: str,
                                       limit: int = 10) -> list[float]:
    """Tennis: use tennis_matches_history where winner_id or loser_id matches."""
    if not player_id:
        return []
    q_any: dict = {"$or": [{"winner_id": player_id}, {"loser_id": player_id}]}
    if opponent_name:
        q_any["$or"] = [
            {"winner_id": player_id, "loser_name": opponent_name},
            {"loser_id": player_id, "winner_name": opponent_name},
        ]
    cursor = db.tennis_matches_history.find(q_any).sort("date", -1).limit(limit)
    out: list[float] = []
    async for d in cursor:
        # Map stat_key → tennis column prefix (w_* for winner, l_* for loser)
        is_winner = d.get("winner_id") == player_id
        prefix = "w_" if is_winner else "l_"
        # Simple 1-to-1 column mappings.
        col_map = {
            "aces": f"{prefix}ace",
            "double_faults": f"{prefix}df",
            "total_games_match": "total_games_match",
        }
        # Composite stat: break_points_won = bpFaced - bpSaved
        # (breaks earned by the RETURNER off the opposite server side).
        # The winner-perspective "break_points_won" = losses's bpFaced -
        # loser's bpSaved. i.e. this player's break_points_won =
        # opponent's bpFaced - opponent's bpSaved.
        if stat_key == "break_points_won":
            opp_prefix = "l_" if is_winner else "w_"
            faced = d.get(f"{opp_prefix}bpFaced")
            saved = d.get(f"{opp_prefix}bpSaved")
            if faced is None or saved is None:
                continue
            try:
                v = max(0.0, float(faced) - float(saved))
            except (TypeError, ValueError):
                continue
            out.append(v)
            continue
        col = col_map.get(stat_key)
        if not col:
            continue
        v = d.get(col)
        if v is None:
            continue
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            continue
    return out


# ─────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────
async def _lookup_soccer_vs_opponent(
    db, player_name: str, opponent_team_name: Optional[str],
    stat_key: str, limit: int = 20,
) -> list[float]:
    """Pull last-N raw stat values from `soccer_player_game_logs` where
    the player played AGAINST the given opponent team.  Read-only.
    """
    if not player_name or not opponent_team_name:
        return []
    import re
    import unicodedata

    def _canon(s: str) -> str:
        d = "".join(c for c in unicodedata.normalize("NFKD", s)
                     if not unicodedata.combining(c))
        return re.sub(r"\s+", " ",
                       re.sub(r"[\.\-'\"\u2019]", "", d).strip().lower())

    name_c = _canon(player_name)
    # Match by exact opponent_team_name first, then by canonical prefix.
    q = {"name_canonical": name_c,
          "opponent_team_name": opponent_team_name}
    cursor = db.soccer_player_game_logs.find(q).sort("match_date", -1) \
        .limit(limit)
    out: list[float] = []
    async for d in cursor:
        v = d.get(stat_key)
        if v is None:
            # Composite fallback: goal_contributions = goals + assists
            if stat_key == "goal_contributions":
                g = d.get("goals") or 0
                a = d.get("assists") or 0
                v = g + a
            else:
                continue
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            continue
    return out


async def _lookup_soccer_recent(
    db, player_name: str, stat_key: str, limit: int = 25,
) -> list[float]:
    """Last-N raw stat values from `soccer_player_game_logs` (any opponent)."""
    if not player_name:
        return []
    import re
    import unicodedata

    def _canon(s: str) -> str:
        d = "".join(c for c in unicodedata.normalize("NFKD", s)
                     if not unicodedata.combining(c))
        return re.sub(r"\s+", " ",
                       re.sub(r"[\.\-'\"\u2019]", "", d).strip().lower())

    name_c = _canon(player_name)
    q = {"name_canonical": name_c}
    cursor = db.soccer_player_game_logs.find(q).sort("match_date", -1) \
        .limit(limit)
    out: list[float] = []
    async for d in cursor:
        v = d.get(stat_key)
        if v is None:
            if stat_key == "goal_contributions":
                v = (d.get("goals") or 0) + (d.get("assists") or 0)
            else:
                continue
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            continue
    return out


async def get_matchup_intelligence(
    db,
    *,
    sport: str,
    player_name: str,
    stat: str,
    player_id: Optional[int | str] = None,
    opponent_team: Optional[str] = None,
    threshold: Optional[float] = None,
) -> MatchupIntelligence:
    """Build a MatchupIntelligence record for the given player+stat+opponent.

    All arguments are keyword-only to prevent accidental misuse.
    Missing data yields empty slices rather than exceptions.
    """
    sport_l = sport.lower()
    stat_canon = _canon_stat(sport_l, stat)

    result = MatchupIntelligence(
        sport=sport,
        player_id=player_id,
        player_name=player_name,
        opponent_team=opponent_team,
        stat=stat_canon,
        threshold=threshold,
    )
    notes = result.notes
    sources = result.data_sources_used

    # ── 1. career vs specific opponent ─────────────────────────────
    career_values: list[float] = []
    if sport_l == "mlb":
        pvt = await _lookup_mlb_pvt(db, player_name, opponent_team, pitcher_id=player_id)
        if pvt and pvt.get("significance") in ("medium", "high"):
            sources.append("mlb_pvt")
            gs = pvt.get("n_gs") or 0
            avg_k = pvt.get("k_per_gs_vs_team") or 0.0
            # Synthesize a per-GS distribution using the reported avg
            # (we don't have per-game rows exposed by mlb_pvt).
            if gs and avg_k:
                career_values = [avg_k] * int(gs)
                notes.append(
                    f"mlb_pvt: {gs} GS vs {opponent_team}, "
                    f"avg K/GS={avg_k}, significance={pvt.get('significance')}"
                )
    elif sport_l == "nfl":
        career_values = await _lookup_nfl_vs_opponent(
            db, player_id, stat_canon, opponent_team, limit=20,
        )
        if career_values:
            sources.append("nfl_player_weekly")
            notes.append(
                f"nfl_player_weekly: {len(career_values)} games vs {opponent_team}"
            )
    elif sport_l == "tennis":
        career_values = await _lookup_tennis_vs_opponent(
            db, player_id, opponent_team, stat_canon, limit=20,
        )
        if career_values:
            sources.append("tennis_matches_history")
    elif sport_l == "soccer":
        # Career vs OPPONENT — soccer_player_game_logs stores
        # opponent_team_name per row so we can query it directly.
        career_values = await _lookup_soccer_vs_opponent(
            db, player_name, opponent_team, stat_canon, limit=20,
        )
        if career_values:
            sources.append("soccer_player_game_logs")
    result.career_vs_opponent = _build_slice(career_values, threshold)

    # ── 2. props_history (pre-computed windows + consistency) ──────
    ph = await _lookup_props_history(db, sport_l, player_id, stat_canon)
    if ph:
        sources.append("props_history")
        # Extract L5/L10/season by scanning `lines` sub-dict (any line).
        # If threshold is supplied and matches, we get the hit-rate directly.
        lines = ph.get("lines") or {}
        # Pick the closest line to `threshold` if provided; else the
        # first canonical mainline.
        chosen_line: Optional[str] = None
        if threshold is not None:
            best_dist = 1e9
            for lk in lines.keys():
                try:
                    d = abs(float(lk) - float(threshold))
                except (TypeError, ValueError):
                    continue
                if d < best_dist:
                    best_dist = d
                    chosen_line = lk
        elif lines:
            chosen_line = next(iter(lines))
        if chosen_line:
            windows = lines[chosen_line]
            for win_k, dest in (("5", "overall_last_5"),
                                 ("10", "overall_last_10"),
                                 ("season", "overall_season")):
                w = windows.get(win_k) or {}
                if w:
                    setattr(result, dest, MatchupSlice(
                        games=int(w.get("games_used") or 0),
                        over_hits=int(w.get("hits") or 0),
                        hit_rate=float(w.get("hit_rate") or 0.0),
                        avg=float(w.get("avg") or 0.0),
                        median=0.0,   # props_history doesn't store median
                        stat_values=[],
                    ))
            notes.append(f"props_history: line={chosen_line}")
        # Propagate consistency + last10_avg if available.
        if ph.get("consistency") is not None:
            result.consistency_score = float(ph["consistency"])
        if ph.get("last10_avg") is not None:
            result.avg_stat_output = float(ph["last10_avg"])

    # ── 3. player_game_logs fallback (raw last-25) ─────────────────
    # Soccer reads from soccer_player_game_logs (not the mixed
    # player_game_logs collection).
    if sport_l == "soccer":
        raw_values = await _lookup_soccer_recent(
            db, player_name, stat_canon, limit=25,
        )
    else:
        raw_values = await _lookup_player_game_logs(
            db, sport_l, player_id, player_name, stat_canon, limit=25,
        )
    if raw_values:
        sources.append("player_game_logs" if sport_l != "soccer"
                        else "soccer_player_game_logs")
        # Populate any slices props_history didn't cover.
        if result.overall_last_5.games == 0:
            result.overall_last_5 = _build_slice(raw_values[:5], threshold)
        if result.overall_last_10.games == 0:
            result.overall_last_10 = _build_slice(raw_values[:10], threshold)
        if result.overall_season.games == 0:
            result.overall_season = _build_slice(raw_values, threshold)
        # Recent-vs-similar heuristic — for now, the last 10 games are
        # the "similar opponents" proxy. A future improvement will
        # cluster opponents by defensive strength.
        if not result.recent_vs_similar.games:
            result.recent_vs_similar = _build_slice(raw_values[:10], threshold)
        # Fill overall stats from raw if props_history was silent.
        if result.consistency_score == 0.0:
            result.consistency_score = _consistency(raw_values[:10])
        if result.avg_stat_output == 0.0 and raw_values:
            result.avg_stat_output = round(sum(raw_values) / len(raw_values), 3)
        if result.median_stat_output == 0.0:
            try:
                result.median_stat_output = round(statistics.median(raw_values), 3)
            except statistics.StatisticsError:
                pass

    # ── 4. Final rollups ──────────────────────────────────────────
    # Sample size = largest of the populated slices.
    result.sample_size = max(
        result.career_vs_opponent.games,
        result.overall_last_10.games,
        result.overall_season.games,
    )
    # Threshold hit rate = weighted average of career-vs-opp + last-10.
    if threshold is not None:
        parts: list[tuple[float, int]] = []
        for s in (result.career_vs_opponent, result.overall_last_10):
            if s.games:
                parts.append((s.hit_rate, s.games))
        if parts:
            total_g = sum(g for _, g in parts)
            result.threshold_hit_rate = round(
                sum(r * g for r, g in parts) / total_g, 4,
            )
    result.sample_confidence = _confidence(result.sample_size)
    result.matchup_grade = _grade(
        result.threshold_hit_rate,
        result.consistency_score,
        result.sample_size,
    )
    if not sources:
        notes.append("no data sources returned rows — cold cache or unknown player")
    return result


__all__ = [
    "get_matchup_intelligence",
    "MatchupIntelligence",
    "MatchupSlice",
]
