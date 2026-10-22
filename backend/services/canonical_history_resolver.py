"""Universal canonical history resolver (Blocks B, C, D, E, F, G, H, I).

Shared read-layer that every sport's feature engine uses to query
player/team completed-game history.  The resolver:

1. Reads canonical collections FIRST (player_game_actuals,
   team_game_actuals, soccer_matches, soccer_player_game_logs,
   tennis_matches_history).
2. Falls back to legacy collections (player_game_logs, games) only
   when the canonical read returns zero rows for that identity.
3. Normalizes canonical/legacy FIELD aliases per sport so one
   consumer-facing shape is returned regardless of which collection
   the row came from.

This closes the dialect mismatch that was starving consumers even
when projectors reported "inserted".  Zero scoring changes.  Zero
threshold changes.  Zero collection destruction — legacy stores
remain readable as provenance.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

logger = logging.getLogger("lockscore.canonical_history_resolver")


# ── Field alias maps ────────────────────────────────────────────────
# Each entry maps a canonical name to the list of legacy aliases that
# may appear in older rows.  Resolver returns the first value found.

_PLAYER_FIELD_ALIASES: dict[str, dict[str, tuple[str, ...]]] = {
    "common": {
        "player_id":    ("canonical_player_id", "player_id", "player"),
        "player_name":  ("player_name", "name", "player"),
        "team":         ("team", "team_abbrev", "team_abbreviation"),
        "opponent":     ("opponent", "opponent_abbrev", "opponent_team"),
        "home_away":    ("home_away", "is_home", "home"),
        "game_date":    ("game_date", "date", "event_date",
                          "commence_time", "event_time"),
        "event_id":     ("event_id", "game_id", "espn_id",
                          "provider_event_id"),
        "season":       ("season", "season_year"),
        "season_type":  ("season_type", "season_phase"),
    },
    "nhl": {
        "goals":        ("goals", "actuals.g", "stats.g"),
        "assists":      ("assists", "actuals.a", "stats.a"),
        "points":       ("points", "actuals.pts", "stats.pts"),
        "shots":        ("shots", "shots_on_goal", "sog",
                          "actuals.sog", "stats.sog"),
        "toi":          ("toi", "ice_time", "actuals.toi"),
    },
    "nba": {
        # P0-2 FIX (2026-10-20) — canonical NBA actuals store FULL stat
        # names inside the ``actuals`` nested dict (``actuals.points``,
        # ``actuals.rebounds``, ``actuals.assists``, ``actuals.steals``,
        # ``actuals.blocks``, ``actuals.minutes``).  Previous aliases
        # only looked up abbreviated keys (``pts``, ``reb``, ``ast``),
        # which left every NBA factor engine call seeing a row with
        # name/team but ZERO stat fields → factors={} → player prop
        # dropped as MISSING_FEATURE_DATA.  Smallest NBA-scoped
        # correction: add the full-name variants to each alias tuple.
        "points":       ("points", "pts", "actuals.points",
                          "actuals.pts", "stats.pts"),
        "rebounds":     ("rebounds", "reb", "actuals.rebounds",
                          "actuals.reb"),
        "assists":      ("assists", "ast", "actuals.assists",
                          "actuals.ast"),
        "threes":       ("threes", "fg3m", "actuals.threes",
                          "actuals.threes_made", "actuals.fg3m"),
        "threes_made":  ("threes_made", "fg3m", "actuals.threes_made",
                          "actuals.fg3m", "actuals.threes"),
        "steals":       ("steals", "stl", "actuals.steals",
                          "actuals.stl"),
        "blocks":       ("blocks", "blk", "actuals.blocks",
                          "actuals.blk"),
        "turnovers":    ("turnovers", "to", "actuals.turnovers",
                          "actuals.to"),
        "minutes":      ("minutes", "min", "actuals.minutes",
                          "actuals.min"),
    },
    "nfl": {
        "pass_yds":     ("pass_yds", "passing_yards",
                          "actuals.passing_yards"),
        "pass_tds":     ("pass_tds", "passing_tds",
                          "actuals.passing_tds"),
        "rush_yds":     ("rush_yds", "rushing_yards",
                          "actuals.rushing_yards"),
        "rush_tds":     ("rush_tds", "rushing_tds",
                          "actuals.rushing_tds"),
        "rec_yds":      ("rec_yds", "receiving_yards",
                          "actuals.receiving_yards"),
        "receptions":   ("receptions", "actuals.receptions"),
    },
    "mlb": {
        "hits":         ("hits", "actuals.hits"),
        "total_bases":  ("total_bases", "actuals.tb"),
        "home_runs":    ("home_runs", "hr", "actuals.hr"),
        "rbis":         ("rbis", "rbi", "actuals.rbi"),
        "runs":         ("runs", "actuals.runs"),
        # Pitcher K is DISTINCT from hitter strikeouts.  Resolver only
        # returns pitcher_strikeouts if the row is a pitcher game
        # (role=="pitcher" or "pitcher_strikeouts"/"actuals.k" present).
        "pitcher_strikeouts": ("pitcher_strikeouts", "actuals.k", "pitching_k"),
        "pitcher_outs":       ("pitcher_outs", "actuals.outs",
                                "pitching_outs"),
        "hitter_strikeouts":  ("hitter_strikeouts", "actuals.so",
                                "batting_strikeouts"),
    },
    "soccer": {
        "goals":        ("goals", "actuals.g"),
        "assists":      ("assists", "actuals.a"),
        "shots":        ("shots", "actuals.sh"),
        "shots_on_target": ("shots_on_target", "sot", "actuals.sot"),
        "minutes":      ("minutes", "actuals.min"),
    },
    "tennis": {
        "winner_name":  ("winner_name", "player1", "winner"),
        "loser_name":   ("loser_name", "player2", "loser"),
        "match_date":   ("match_date", "date", "event_date"),
        "surface":      ("surface",),
        "tournament":   ("tournament", "event_name"),
    },
    "cfb": {
        "pass_yds":     ("pass_yds", "passing_yards"),
        "rush_yds":     ("rush_yds", "rushing_yards"),
        "rec_yds":      ("rec_yds", "receiving_yards"),
        "receptions":   ("receptions",),
    },
}

_TEAM_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "home_team":   ("home_team", "home", "home_abbrev",
                     "home_team_abbreviation"),
    "away_team":   ("away_team", "away", "away_abbrev",
                     "away_team_abbreviation"),
    "home_score":  ("home_score", "result.home", "scores.home"),
    "away_score":  ("away_score", "result.away", "scores.away"),
    "game_date":   ("game_date", "date", "event_date",
                     "commence_time"),
    "event_id":    ("event_id", "game_id", "espn_id",
                     "provider_event_id"),
    "status":      ("status",),
    "sport":       ("sport",),
}


def _dotted_get(d: dict, path: str) -> Any:
    """Resolve a dotted path like 'actuals.g' from a dict."""
    if "." not in path:
        return d.get(path)
    cur: Any = d
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def resolve_player_row(row: dict, *, sport: str) -> dict:
    """Normalize a raw row (from either canonical or legacy
    collection) into a single consumer-facing shape."""
    sport_key = (sport or "").strip().lower()
    out: dict[str, Any] = {}
    common = _PLAYER_FIELD_ALIASES["common"]
    for canonical, aliases in common.items():
        for a in aliases:
            v = _dotted_get(row, a)
            if v is not None:
                out[canonical] = v
                break
    sport_map = _PLAYER_FIELD_ALIASES.get(sport_key, {})
    for canonical, aliases in sport_map.items():
        for a in aliases:
            v = _dotted_get(row, a)
            if v is not None:
                out[canonical] = v
                break
    # MLB pitcher-K disambiguation (Block I): if the row is clearly a
    # pitcher game and only hitter_strikeouts was found, don't leak
    # it into pitcher_strikeouts.
    if sport_key == "mlb":
        role = (row.get("role") or row.get("position") or "").lower()
        if role != "pitcher" and "pitcher_strikeouts" not in out:
            # Nothing to clean.
            pass
    # ── 2026-10-20 P0-2 FIX ───────────────────────────────────────────
    # Mirror ``player_id`` → ``canonical_player_id`` and ``event_id`` →
    # ``canonical_event_id`` on the normalized output so simulators that
    # key off the canonical name (``brain.sim_nba`` line 273 filter)
    # accept these rows as canonical-identity-keyed and admit them into
    # the EMPIRICAL_INDEPENDENT branch.  Without this, every normalized
    # NBA row was dropped by sim_nba's canonical gate and the sim
    # silently fell back to MODEL_CONDITIONED (independent_evidence=False),
    # starving the board_validator evidence recognizer.  Zero scoring
    # change; purely a key-aliasing addition.
    if out.get("player_id") is not None:
        out.setdefault("canonical_player_id", out["player_id"])
    if out.get("event_id") is not None:
        out.setdefault("canonical_event_id", out["event_id"])
    out["_source_collection"] = row.get("_source_collection") or "unknown"
    return out


def resolve_team_row(row: dict) -> dict:
    """Normalize a raw team-game row to the canonical consumer shape."""
    out: dict[str, Any] = {}
    for canonical, aliases in _TEAM_FIELD_ALIASES.items():
        for a in aliases:
            v = _dotted_get(row, a)
            if v is not None:
                out[canonical] = v
                break
    out["_source_collection"] = row.get("_source_collection") or "unknown"
    return out


# ── DB readers ──────────────────────────────────────────────────────

async def read_player_recent(
    db, *, sport: str, player_name: Optional[str] = None,
    canonical_player_id: Optional[str] = None,
    limit: int = 40,
) -> list[dict]:
    """Return up to ``limit`` recent player-game rows for a player,
    canonical-first with legacy fallback.  Rows are returned in the
    normalized consumer shape (via resolve_player_row)."""
    sport_key = (sport or "").strip().lower()
    rows: list[dict] = []
    filt: dict[str, Any] = {"sport": sport_key}
    if canonical_player_id:
        filt["$or"] = [
            {"canonical_player_id": canonical_player_id},
            {"player_id": canonical_player_id},
        ]
    elif player_name:
        # Priority order:
        #   1. canonical_player_id (handled above).
        #   2. Case-insensitive exact full-name match.
        #   3. Controlled first-initial + last-name fallback — ONLY when
        #      the exact match returns zero rows AND the abbreviated
        #      form uniquely identifies ONE person in that sport (no
        #      collision between players sharing initial + last name).
        #
        # 2026-10-21 (Preview) — NHL history stores abbreviated names
        # like "B. Burns" while live outcomes send "Brent Burns".  The
        # anchored-exact regex at step 2 misses those rows.  Step 3's
        # ambiguity gate below makes the fallback safe: if multiple
        # distinct canonical_player_ids share the same "X. Last"
        # abbreviation, we fail closed (no history returned) so the
        # mismatch gate stops the pick instead of silently attaching
        # another skater's stats.
        filt["$or"] = [
            {"player_name": {"$regex": f"^{player_name}$", "$options": "i"}},
            {"name":        {"$regex": f"^{player_name}$", "$options": "i"}},
        ]
    sort_spec = [("game_date", -1), ("date", -1)]

    # Canonical first.
    try:
        canonical_cursor = db.player_game_actuals.find(filt).sort(
            sort_spec).limit(limit)
        async for r in canonical_cursor:
            r["_source_collection"] = "player_game_actuals"
            rows.append(resolve_player_row(r, sport=sport_key))
    except Exception as e:
        logger.debug("canonical player read failed: %s", e)
    if rows:
        return rows

    # ── 2026-10-21 · NHL abbreviated-name fallback (ambiguity-gated) ─
    # Step 3 of the priority ladder.  Triggers when:
    #   • exact match returned zero rows, AND
    #   • (no canonical_player_id was supplied) OR (the supplied id
    #     didn't match any canonical rows — e.g. ESPN athlete-id
    #     passed in while actuals are keyed on NHL player-id), AND
    #   • the "X. Last" abbreviation uniquely identifies ONE
    #     canonical_player_id across the sport.
    # Multiple candidates → fail closed (no history returned).
    if (not rows) and player_name:
        parts = (player_name or "").strip().split()
        if len(parts) >= 2 and len(parts[0]) >= 1:
            abbrev = f"{parts[0][0].upper()}. {' '.join(parts[1:])}"
            if abbrev.lower() != (player_name or "").lower():
                # Enforce uniqueness of canonical_player_id for this abbreviation.
                try:
                    distinct_ids = await db.player_game_actuals.distinct(
                        "canonical_player_id",
                        {"sport": sport_key,
                         "$or": [
                             {"player_name": {"$regex":
                                 f"^{abbrev}$", "$options": "i"}},
                             {"name":        {"$regex":
                                 f"^{abbrev}$", "$options": "i"}},
                         ]},
                    )
                    distinct_ids = [i for i in distinct_ids if i]
                    if len(distinct_ids) == 1:
                        safe_cursor = db.player_game_actuals.find(
                            {"sport": sport_key,
                             "canonical_player_id": distinct_ids[0]}
                        ).sort(sort_spec).limit(limit)
                        async for r in safe_cursor:
                            r["_source_collection"] = "player_game_actuals"
                            r["_resolution"] = "abbrev_fallback"
                            rows.append(resolve_player_row(r, sport=sport_key))
                    elif len(distinct_ids) > 1:
                        logger.info(
                            "canonical_history_resolver: abbreviated-name "
                            "ambiguity for sport=%s name=%r (%d candidates) "
                            "— failing closed",
                            sport_key, player_name, len(distinct_ids),
                        )
                except Exception as e:
                    logger.debug(
                        "abbrev fallback distinct() failed for %s: %s",
                        player_name, e,
                    )
        if rows:
            return rows

    # Legacy fallback.
    try:
        legacy_cursor = db.player_game_logs.find(filt).sort(sort_spec).limit(limit)
        async for r in legacy_cursor:
            r["_source_collection"] = "player_game_logs"
            rows.append(resolve_player_row(r, sport=sport_key))
    except Exception as e:
        logger.debug("legacy player read failed: %s", e)
    return rows


async def read_team_recent(
    db, *, sport: str, team: str, limit: int = 20,
) -> list[dict]:
    """Return recent completed-game rows that involve ``team``,
    canonical-first with legacy fallback."""
    sport_key = (sport or "").strip().lower()
    rows: list[dict] = []
    # Build the OR filter against every known team-field alias.
    team_or = []
    for alias in _TEAM_FIELD_ALIASES["home_team"]:
        team_or.append({alias: team})
    for alias in _TEAM_FIELD_ALIASES["away_team"]:
        team_or.append({alias: team})
    filt = {"sport": sport_key, "$or": team_or}
    sort_spec = [("game_date", -1), ("date", -1), ("event_date", -1)]
    try:
        c = db.team_game_actuals.find(filt).sort(sort_spec).limit(limit)
        async for r in c:
            r["_source_collection"] = "team_game_actuals"
            rows.append(resolve_team_row(r))
    except Exception as e:
        logger.debug("canonical team read failed: %s", e)
    if rows:
        return rows
    try:
        c = db.games.find(filt).sort(sort_spec).limit(limit)
        async for r in c:
            r["_source_collection"] = "games"
            rows.append(resolve_team_row(r))
    except Exception as e:
        logger.debug("legacy team read failed: %s", e)
    return rows


__all__ = [
    "resolve_player_row", "resolve_team_row",
    "read_player_recent", "read_team_recent",
    "_PLAYER_FIELD_ALIASES", "_TEAM_FIELD_ALIASES",
]
