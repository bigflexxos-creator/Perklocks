"""NFL Player-vs-Opponent Matchup Intelligence (2026-07-28).

Built on top of `services.player_matchup_intelligence`. Adds NFL-specific
position-aware breakdowns for QB / RB / WR / TE against a given
opponent team.

Data source: `nfl_player_weekly` collection (~129 K rows) which stores
one document per (player, season, week) with `opponent_team` and every
box-score stat we care about.

Purely historical performance intelligence. **No sportsbook odds, no
betting lines** — only what the player actually did against this
opponent (and rest-of-league for context).

──────────────────────────────────────────────────────────────────────
Public API
──────────────────────────────────────────────────────────────────────
    result = await get_nfl_matchup_intelligence(
        db,
        player_name="Joe Burrow",
        opponent_team="KC",             # NFL 2/3-letter code preferred
        season_min=2019,                # optional, default: last 6 seasons
    )
    # → NFLPlayerMatchup(
    #     player_name="Joe Burrow",
    #     position="QB",
    #     opponent_team="KC",
    #     games_played=5,
    #     last_meeting={ season, week, passing_yards, ... },
    #     sample_confidence="high",
    #     stat_lines={
    #       "passing_yards": StatBreakdown(
    #         avg, median, min, max, values, thresholds={
    #           150: {hits: 5, hit_rate: 1.0},
    #           200: {hits: 4, hit_rate: 0.8},
    #           250: {hits: 3, hit_rate: 0.6},
    #           300: {hits: 2, hit_rate: 0.4},
    #         }),
    #       "passing_tds":  StatBreakdown(...),
    #       ...
    #     },
    #     data_sources_used=["nfl_player_weekly"],
    #     notes=[...],
    #   )

Rendering example (mirrors the user-provided spec):

    Joe Burrow vs Kansas City Chiefs
    Games: 5
    Passing yards:
      Avg: 278
      Median: 285
      Min/Max: 199 / 341
      150+ yards: 5/5   (100%)
      200+ yards: 4/5   (80%)
      250+ yards: 3/5   (60%)
      300+ yards: 2/5   (40%)
    Confidence: high
    Last meeting: 2023 Week 17 — 341 pass yards, 3 TD

Format-agnostic — callers decide the UI rendering.
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

from .player_matchup_intelligence import _confidence  # reuse tiers

logger = logging.getLogger("lockscore.services.nfl_matchup_intelligence")

# ─────────────────────────────────────────────────────────────────────
# Position → stat set → hit-rate thresholds
# ─────────────────────────────────────────────────────────────────────
# Every line here is a HISTORICAL performance threshold — never a
# sportsbook line and never used as a betting line. They exist so we
# can answer "how many times did the player exceed 200 pass yards" —
# a factual historical question.
_POSITION_STAT_LINES: dict[str, dict[str, list[float]]] = {
    "QB": {
        "passing_yards":   [150, 200, 250, 300],
        "passing_tds":     [0.5, 1.5, 2.5, 3.5],
        "attempts":        [24.5, 29.5, 34.5, 39.5],
        "completions":     [15.5, 19.5, 24.5],
        "passing_ints":    [0.5, 1.5],
        "rushing_yards":   [10, 25, 50],   # dual-threat QBs
    },
    "RB": {
        "rushing_yards":   [25, 50, 75, 100],
        "carries":         [9.5, 14.5, 19.5],
        "rushing_tds":     [0.5, 1.5],
        "receptions":      [1.5, 3.5, 5.5],
        "receiving_yards": [10, 25, 50],
    },
    "WR": {
        "receiving_yards": [25, 50, 75, 100],
        "receptions":      [2.5, 4.5, 6.5, 8.5],
        "targets":         [4.5, 6.5, 8.5, 10.5],
        "receiving_tds":   [0.5, 1.5],
    },
    "TE": {
        "receiving_yards": [15, 30, 50, 75],
        "receptions":      [1.5, 3.5, 5.5],
        "targets":         [3.5, 5.5, 7.5],
        "receiving_tds":   [0.5, 1.5],
    },
}


# ─────────────────────────────────────────────────────────────────────
# Result dataclasses
# ─────────────────────────────────────────────────────────────────────
@dataclass
class ThresholdHit:
    """Historical hit count + rate for a specific stat threshold."""
    threshold: float
    hits: int = 0
    games: int = 0
    hit_rate: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class StatBreakdown:
    """Per-stat historical breakdown vs opponent."""
    stat_key:   str
    games:      int = 0
    avg:        float = 0.0
    median:     float = 0.0
    minimum:    float = 0.0
    maximum:    float = 0.0
    values:     list[float] = field(default_factory=list)
    thresholds: dict[float, ThresholdHit] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["thresholds"] = {
            str(k): v if isinstance(v, dict) else v.to_dict()
            for k, v in self.thresholds.items()
        }
        return d


@dataclass
class NFLPlayerMatchup:
    """Complete NFL player-vs-opponent matchup intelligence."""
    player_name:       str
    player_id:         Optional[str] = None
    position:          Optional[str] = None
    opponent_team:     Optional[str] = None
    games_played:      int = 0
    last_meeting:      Optional[dict] = None
    sample_confidence: str = "none"
    stat_lines:        dict[str, StatBreakdown] = field(default_factory=dict)
    data_sources_used: list[str] = field(default_factory=list)
    notes:             list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["stat_lines"] = {
            k: v.to_dict() for k, v in self.stat_lines.items()
        }
        return d


# ─────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────
def _build_stat_breakdown(stat_key: str,
                          values: list[float],
                          thresholds: list[float]) -> StatBreakdown:
    if not values:
        return StatBreakdown(stat_key=stat_key)
    br = StatBreakdown(
        stat_key=stat_key,
        games=len(values),
        avg=round(sum(values) / len(values), 2),
        median=round(statistics.median(values), 2),
        minimum=round(min(values), 2),
        maximum=round(max(values), 2),
        values=[float(v) for v in values],
    )
    for t in thresholds:
        hits = sum(1 for v in values if v >= t)
        br.thresholds[float(t)] = ThresholdHit(
            threshold=float(t),
            hits=hits,
            games=len(values),
            hit_rate=round(hits / len(values), 4),
        )
    return br


def _fetch_stat_column(doc: dict, key: str) -> Optional[float]:
    v = doc.get(key)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ─────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────
async def get_nfl_matchup_intelligence(
    db,
    *,
    player_name: str,
    opponent_team: str,
    season_min: int = 2019,
    max_games: int = 30,
) -> NFLPlayerMatchup:
    """Build NFLPlayerMatchup for one (player, opponent) pair.

    Reads ONLY from `nfl_player_weekly`. Zero writes. Zero HTTP calls.

    Args:
      db:            Motor async DB handle.
      player_name:   Full name or NFL-abbreviated (matches
                     `player_display_name` OR `player_name` in the
                     collection).
      opponent_team: 2-3 letter code (KC, CIN, TB, ...) or full name.
      season_min:    Earliest season to include (default 2019).
      max_games:     Cap on returned games (default 30 — 4 seasons).

    Returns an NFLPlayerMatchup with all populated slices. Empty
    matchup (games_played=0, confidence="none") is returned when
    no rows match — never raises.
    """
    result = NFLPlayerMatchup(
        player_name=player_name,
        opponent_team=opponent_team,
    )

    # Player match: try both name fields (nfl_player_weekly stores both
    # `player_name` — like "J.Burrow" — and `player_display_name` —
    # like "Joe Burrow").
    name_or = [
        {"player_display_name": player_name},
        {"player_name": player_name},
    ]
    # Opponent match: `opponent_team` in the collection is always the
    # 2-3 letter abbreviation. Accept both abbreviation input AND full
    # team name via a small alias table.
    opp_code = _resolve_opponent_code(opponent_team)
    opp_query = {"opponent_team": opp_code}

    query = {
        "$and": [
            {"$or": name_or},
            opp_query,
            {"season": {"$gte": season_min}},
        ]
    }

    cursor = db.nfl_player_weekly.find(query).sort(
        [("season", -1), ("week", -1)]
    ).limit(max_games)

    rows: list[dict] = []
    async for d in cursor:
        rows.append(d)

    if not rows:
        result.notes.append(
            f"no nfl_player_weekly rows for {player_name} vs "
            f"opponent_team={opp_code} since season {season_min}"
        )
        return result

    result.data_sources_used.append("nfl_player_weekly")
    result.player_id = rows[0].get("player_id")
    result.position = rows[0].get("position")
    result.games_played = len(rows)
    result.sample_confidence = _confidence(len(rows))

    # Last meeting → most recent row (rows are sorted DESC).
    last = rows[0]
    result.last_meeting = {
        "season": last.get("season"),
        "week": last.get("week"),
        "team": last.get("team"),
        "passing_yards": last.get("passing_yards"),
        "passing_tds": last.get("passing_tds"),
        "attempts": last.get("attempts"),
        "completions": last.get("completions"),
        "rushing_yards": last.get("rushing_yards"),
        "carries": last.get("carries"),
        "receiving_yards": last.get("receiving_yards"),
        "receptions": last.get("receptions"),
        "targets": last.get("targets"),
        "rushing_tds": last.get("rushing_tds"),
        "receiving_tds": last.get("receiving_tds"),
    }
    # Strip None keys for a cleaner UI payload.
    result.last_meeting = {
        k: v for k, v in result.last_meeting.items() if v is not None
    }

    # Position-driven stat set — fall back to QB if unknown position.
    stat_set = _POSITION_STAT_LINES.get(
        result.position or "",
        _POSITION_STAT_LINES["QB"],
    )
    if result.position not in _POSITION_STAT_LINES:
        result.notes.append(
            f"unknown position {result.position!r} — using QB stat set as fallback"
        )

    for stat_key, thresholds in stat_set.items():
        values = [
            v for v in (
                _fetch_stat_column(r, stat_key) for r in rows
            ) if v is not None
        ]
        if not values:
            continue
        result.stat_lines[stat_key] = _build_stat_breakdown(
            stat_key, values, thresholds,
        )

    if not result.stat_lines:
        result.notes.append(
            f"no non-null stat columns for position={result.position}"
        )
    return result


# ─────────────────────────────────────────────────────────────────────
# Opponent-code resolver
# ─────────────────────────────────────────────────────────────────────
# Minimal full-name → nflverse code map so callers can pass either
# "KC" or "Kansas City Chiefs". Unknown values pass through as-is.
_TEAM_ABBR = {
    "arizona cardinals": "ARI", "atlanta falcons": "ATL",
    "baltimore ravens": "BAL", "buffalo bills": "BUF",
    "carolina panthers": "CAR", "chicago bears": "CHI",
    "cincinnati bengals": "CIN", "cleveland browns": "CLE",
    "dallas cowboys": "DAL", "denver broncos": "DEN",
    "detroit lions": "DET", "green bay packers": "GB",
    "houston texans": "HOU", "indianapolis colts": "IND",
    "jacksonville jaguars": "JAX", "kansas city chiefs": "KC",
    "las vegas raiders": "LV", "los angeles chargers": "LAC",
    "los angeles rams": "LA", "miami dolphins": "MIA",
    "minnesota vikings": "MIN", "new england patriots": "NE",
    "new orleans saints": "NO", "new york giants": "NYG",
    "new york jets": "NYJ", "philadelphia eagles": "PHI",
    "pittsburgh steelers": "PIT", "san francisco 49ers": "SF",
    "seattle seahawks": "SEA", "tampa bay buccaneers": "TB",
    "tennessee titans": "TEN", "washington commanders": "WAS",
}


def _resolve_opponent_code(opponent: str) -> str:
    if not opponent:
        return ""
    if len(opponent) <= 3:
        return opponent.upper()
    return _TEAM_ABBR.get(opponent.lower().strip(), opponent)


__all__ = [
    "get_nfl_matchup_intelligence",
    "NFLPlayerMatchup",
    "StatBreakdown",
    "ThresholdHit",
]
