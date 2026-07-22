"""Unified stats aggregator — pulls PlayerStats from any available
source across MongoDB collections and merges them into a canonical
`PlayerStats` object usable by the archetype engine + models.

Sources (in priority order):
   1. `soccer_player_form` — Understat-derived Big-5 European leagues.
      Full per-90 stats: goals_per_90, shots_per_90, key_passes, npxg,
      form_score, position. Most detailed.
   2. `espn_mls_stats` — ESPN MLS season leaders. Goals/assists totals
      per team, no per-90 (we derive from games).
   3. `wiki_top_scorers` — Wikipedia scraped leaderboards for smaller
      leagues (Allsvenskan, Eliteserien, Liga MX etc.). Goals only.
   4. `mls_player_matchup_history` — per-opponent MLS aggregates.

If a player appears in multiple sources, we prefer the one with the
richer feature set (Understat > ESPN > Wiki).
"""
from __future__ import annotations

import logging
import unicodedata
from typing import Optional

from .models import PlayerStats, MatchupSplit

logger = logging.getLogger("lockscore.player_props.stats")


def _norm(name: str) -> str:
    if not name:
        return ""
    nk = unicodedata.normalize("NFKD", name)
    return "".join(c for c in nk if not unicodedata.combining(c)).lower().strip()


# ─────────────────────────────────────────────────────────────────────
# Source-specific loaders
# ─────────────────────────────────────────────────────────────────────
async def _load_understat(name_norm: str) -> Optional[PlayerStats]:
    """Load a player from `soccer_player_form` (Understat per-90 stats)."""
    from deps import db
    doc = await db.soccer_player_form.find_one({"name_canonical": name_norm})
    if not doc:
        # Some docs use lowercased 'player_name' too.
        doc = await db.soccer_player_form.find_one(
            {"player_name": {"$regex": f"^{name_norm}$", "$options": "i"}}
        )
    if not doc:
        return None

    minutes = int(doc.get("minutes") or 0)
    games = int(doc.get("games") or 0) or (minutes // 90 if minutes else 0)
    assists = int(doc.get("assists") or 0)
    kp = int(doc.get("key_passes") or 0)
    assists_per_90 = (assists * 90.0 / minutes) if minutes else 0.0
    kp_per_90 = (kp * 90.0 / minutes) if minutes else 0.0

    return PlayerStats(
        player_name=doc.get("player_name") or name_norm.title(),
        name_norm=name_norm,
        team=doc.get("team", ""),
        league=doc.get("league", ""),
        season=int(doc.get("season") or 0),
        games=games,
        minutes=minutes,
        goals=int(doc.get("goals") or 0),
        assists=assists,
        goals_per_90=float(doc.get("goals_per_90") or 0.0),
        assists_per_90=round(assists_per_90, 3),
        shots_per_90=float(doc.get("shots_per_90") or 0.0),
        key_passes_per_90=round(kp_per_90, 3),
        npxg_per_90=float(doc.get("npxg_per_90") or 0.0),
        form_score=float(doc.get("form_score") or 50.0),
        form_label=doc.get("form_label", "NEUTRAL"),
        position=doc.get("position", ""),
        source="understat",
        data_ok=True,
    )


async def _load_espn_mls(name_norm: str) -> Optional[PlayerStats]:
    """Load a player from `espn_mls_stats`."""
    from deps import db
    doc = await db.espn_mls_stats.find_one({"name_norm": name_norm})
    if not doc:
        return None
    games = int(doc.get("games") or 0)
    goals = int(doc.get("goals") or 0)
    assists = int(doc.get("assists") or 0)
    # ESPN doesn't give minutes. Approximate: 80 min avg for starters.
    minutes_est = games * 80
    # Derive per-90 from goals/games (assumes near-full matches).
    gp90 = (goals / games) if games else 0.0
    ap90 = (assists / games) if games else 0.0

    return PlayerStats(
        player_name=doc.get("name") or name_norm.title(),
        name_norm=name_norm,
        team=doc.get("team", ""),
        league="MLS",
        season=int(doc.get("season") or 0),
        games=games,
        minutes=minutes_est,
        goals=goals,
        assists=assists,
        goals_per_90=round(gp90, 3),
        assists_per_90=round(ap90, 3),
        shots_per_90=0.0,       # unknown — ESPN doesn't publish
        key_passes_per_90=0.0,  # unknown
        npxg_per_90=0.0,        # unknown
        form_score=50.0,        # unknown → neutral
        form_label="NEUTRAL",
        position="",            # unknown
        source="espn_mls",
        data_ok=True,
    )


async def _load_wiki(name_norm: str) -> Optional[PlayerStats]:
    """Load a player from Wikipedia scorer leaderboards (goals only)."""
    from deps import db
    docs = await db.wiki_top_scorers.find({}).to_list(length=100)
    for lg_doc in docs:
        for row in lg_doc.get("scorers", []):
            if _norm(row.get("name", "")) == name_norm:
                goals = int(row.get("goals") or 0)
                # No games info — assume 20 for a league leader.
                games = int(row.get("games") or 20)
                return PlayerStats(
                    player_name=row.get("name") or name_norm.title(),
                    name_norm=name_norm,
                    team=row.get("club", ""),
                    league=row.get("league", ""),
                    season=int(row.get("season") or 0),
                    games=games,
                    minutes=games * 80,
                    goals=goals,
                    assists=0,
                    goals_per_90=round(goals / games, 3) if games else 0.0,
                    assists_per_90=0.0,
                    shots_per_90=0.0,
                    key_passes_per_90=0.0,
                    npxg_per_90=0.0,
                    form_score=50.0,
                    position="F",   # leaderboard = forward assumption
                    source="wiki",
                    data_ok=True,
                )
    return None


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────
async def get_player_stats(player_name: str,
                            league_hint: Optional[str] = None
                            ) -> Optional[PlayerStats]:
    """Return unified `PlayerStats` for a player, or None if not found.

    Merge policy:
      • Understat (Big-5) is preferred where available (richest features).
      • ESPN MLS is the primary source for MLS.
      • Wiki is a last-resort fallback for smaller leagues.
      • If found in multiple, we return Understat > ESPN > Wiki.
    """
    n = _norm(player_name)
    if not n:
        return None

    # Fast path: if we know the league, hit that source first.
    if league_hint and league_hint.upper() == "MLS":
        r = await _load_espn_mls(n)
        if r:
            return r

    r = await _load_understat(n)
    if r:
        return r
    r = await _load_espn_mls(n)
    if r:
        return r
    r = await _load_wiki(n)
    if r:
        return r
    return None


async def get_matchup_split(player_name: str,
                             opponent_team: str
                             ) -> Optional[MatchupSplit]:
    """Return per-opponent aggregated split (currently MLS only)."""
    from deps import db
    pname_n = _norm(player_name)
    doc = await db.mls_player_matchup_history.find_one(
        {"player_name_norm": pname_n},
    )
    if not doc:
        return None
    opp_l = (opponent_team or "").lower().strip()
    for rec in doc.get("by_opponent", []):
        opp_name = (rec.get("opponent_name") or "").lower().strip()
        if not opp_name:
            continue
        if opp_name == opp_l or opp_l in opp_name or opp_name in opp_l:
            return MatchupSplit(
                opponent=rec.get("opponent_name") or opponent_team,
                matches=int(rec.get("matches", 0)),
                goals=int(rec.get("goals", 0)),
                assists=int(rec.get("assists", 0)),
                scored_matches=int(rec.get("scored_matches", 0)),
                assist_matches=int(rec.get("assist_matches", 0)),
                shots=int(rec.get("shots", 0)),
            )
    return None
