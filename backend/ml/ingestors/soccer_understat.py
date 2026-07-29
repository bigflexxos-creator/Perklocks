"""Soccer per-match player-log ingestor (Understat-backed) — Phase 7 Part 4.

Populates the `soccer_player_game_logs` collection with one row per
(player, match) using Understat's public AJAX endpoints:

  • GET /main/getLeagueData/{LEAGUE}/{SEASON}
      → list of 380 matches for a league-season with metadata
        (match_id, date, home/away team IDs, goals, xG, forecast).

  • GET /main/getMatchData/{MATCH_ID}
      → per-match `rosters` (per-player stats) + `shots` (shot events).

Per-player fields captured
  goals, assists, own_goals, shots, key_passes, xG, xA, xGChain,
  xGBuildup, yellow_card, red_card, roster_in, roster_out (minutes),
  starts, position, positionOrder, team_id, h_a (home/away),
  shots_on_target (derived from `shots.result` ∈ {Goal, SavedShot}).

Per-match context
  match_id, match_date, season, league, is_home, opponent_team_id,
  opponent_team_name, home_goals, away_goals, home_xg, away_xg,
  team_goals_scored, team_goals_conceded, team_xg, opponent_xg.

Rate-limit / politeness
  • MIN_INTERVAL_SEC = 3.0 seconds between requests (well within the
    5s courteous-scraping default used in `soccer_player_form.py`).
  • Exponential backoff on 429/5xx.
  • Session cookies preserved via a shared `httpx.AsyncClient`.

Idempotency
  • The collection has a unique index on (match_id, player_id).
  • Existing (match_id, player_id) rows are UPDATED, not duplicated.
  • Ingest checkpoints on (league, season) so a resumed run only
    fetches matches missing from the DB.

NEVER raises — all errors are logged and yield an "errored" counter.
"""
from __future__ import annotations

import asyncio
import logging
import time as _time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

logger = logging.getLogger("lockscore.ml.ingestors.soccer_understat")

UNDERSTAT_BASE = "https://understat.com"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 "
    "LockScoreSoccerIngestor/1.0"
)

# Understat league slugs — Big 5 European leagues.
BIG5_LEAGUES: tuple[str, ...] = (
    "EPL",
    "La_liga",
    "Bundesliga",
    "Serie_A",
    "Ligue_1",
)

# Politeness / retry knobs — 3s min interval keeps us safely within
# what the season-aggregate scraper (`soccer_player_form.py`) uses.
MIN_INTERVAL_SEC = 3.0
REQUEST_TIMEOUT = 30.0
MAX_RETRIES = 4                       # 4 back-off retries per request
BACKOFF_BASE = 2.0                    # 2, 4, 8, 16 seconds

COLLECTION_NAME = "soccer_player_game_logs"


# ─────────────────────────────────────────────────────────────────────
# Rate limiter (module-level so nested calls share throttle)
# ─────────────────────────────────────────────────────────────────────
_last_request_t: float = 0.0
_rate_lock = asyncio.Lock()


async def _polite_get(
    client: httpx.AsyncClient,
    url: str,
    *,
    referer: str,
) -> Optional[dict]:
    """GET with min-spacing throttle and exponential-backoff retries.

    Returns parsed JSON on success, None on any hard failure.
    """
    global _last_request_t
    async with _rate_lock:
        now = _time.monotonic()
        delta = now - _last_request_t
        if delta < MIN_INTERVAL_SEC:
            await asyncio.sleep(MIN_INTERVAL_SEC - delta)
        _last_request_t = _time.monotonic()

    headers = {
        "User-Agent":       USER_AGENT,
        "X-Requested-With": "XMLHttpRequest",
        "Referer":          referer,
        "Accept":           "application/json,text/javascript,*/*;q=0.01",
    }
    last_err: Any = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            if attempt > 0:
                await asyncio.sleep(BACKOFF_BASE ** attempt)
            resp = await client.get(url, headers=headers,
                                     timeout=REQUEST_TIMEOUT,
                                     follow_redirects=True)
            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError:
                    last_err = "invalid JSON body"
                    continue
            if resp.status_code in (429,) or resp.status_code >= 500:
                last_err = f"HTTP {resp.status_code}"
                continue
            logger.warning("understat GET %s → HTTP %s", url,
                            resp.status_code)
            return None
        except httpx.HTTPError as e:
            last_err = e
    logger.warning("understat GET %s failed after %s retries: %s",
                    url, MAX_RETRIES, last_err)
    return None


# ─────────────────────────────────────────────────────────────────────
# High-level fetchers
# ─────────────────────────────────────────────────────────────────────
async def fetch_league_matches(
    client: httpx.AsyncClient,
    league: str,
    season: int,
) -> Optional[list[dict]]:
    """Return the list of match dicts for a (league, season)."""
    url = f"{UNDERSTAT_BASE}/main/getLeagueData/{league}/{season}"
    ref = f"{UNDERSTAT_BASE}/league/{league}/{season}"
    payload = await _polite_get(client, url, referer=ref)
    if not payload:
        return None
    return payload.get("dates") or []


async def fetch_match_data(
    client: httpx.AsyncClient,
    match_id: str,
) -> Optional[dict]:
    """Return the `{rosters, shots, tmpl}` payload for one match."""
    url = f"{UNDERSTAT_BASE}/main/getMatchData/{match_id}"
    ref = f"{UNDERSTAT_BASE}/match/{match_id}"
    return await _polite_get(client, url, referer=ref)


# ─────────────────────────────────────────────────────────────────────
# Row normalisation
# ─────────────────────────────────────────────────────────────────────
_ON_TARGET_RESULTS = {"Goal", "SavedShot"}


def _canonicalize_name(name: str) -> str:
    """Lower-case + strip diacritics for fuzzy lookup keys."""
    import re
    import unicodedata
    if not name:
        return ""
    nfkd = unicodedata.normalize("NFKD", name)
    without_diacritics = "".join(c for c in nfkd
                                  if not unicodedata.combining(c))
    without_punct = re.sub(r"[\.\-'\"\u2019]", "", without_diacritics)
    return re.sub(r"\s+", " ", without_punct.strip().lower())


def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _int(v: Any, default: int = 0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _derive_sot_from_shots(shots_side: list[dict],
                            player_id: str) -> int:
    """Count shots-on-target for a player from the match shot list."""
    if not shots_side or not player_id:
        return 0
    return sum(1 for s in shots_side
                if str(s.get("player_id") or "") == str(player_id)
                and s.get("result") in _ON_TARGET_RESULTS)


def _build_player_row(
    *,
    match_id: str,
    match_date: str,
    league: str,
    season: int,
    home_team_id: str,
    home_team_name: str,
    away_team_id: str,
    away_team_name: str,
    home_goals: int,
    away_goals: int,
    home_xg: float,
    away_xg: float,
    player_id: str,
    player_stats: dict,
    shots_h: list[dict],
    shots_a: list[dict],
) -> dict:
    """Convert an Understat roster entry into a soccer_player_game_logs
    document with all context enrichment."""
    h_a = (player_stats.get("h_a") or "").lower()
    is_home = (h_a == "h")
    team_id = str(player_stats.get("team_id") or "")

    # Opponent + team goals / xG mapping.
    if is_home:
        opponent_team_id = away_team_id
        opponent_team_name = away_team_name
        team_goals_scored = home_goals
        team_goals_conceded = away_goals
        team_xg = home_xg
        opponent_xg = away_xg
    else:
        opponent_team_id = home_team_id
        opponent_team_name = home_team_name
        team_goals_scored = away_goals
        team_goals_conceded = home_goals
        team_xg = away_xg
        opponent_xg = home_xg

    # Shots-on-target (from same-side shots array).
    shots_side = shots_h if is_home else shots_a
    sot = _derive_sot_from_shots(shots_side, player_id)

    player_name = player_stats.get("player") or ""

    # Minutes played + start flag.
    # In Understat, `position="Sub"` marks a bench player who came on.
    # `roster_in` / `roster_out` are roster-row IDs (not minute markers),
    # so we base `starts` on position rather than roster_in==0.
    roster_in = _int(player_stats.get("roster_in"), 0)
    roster_out = _int(player_stats.get("roster_out"), 0)
    minutes = _int(player_stats.get("time"), 0)
    position = (player_stats.get("position") or "").strip()
    is_sub = position.lower() == "sub"
    starts = 0 if is_sub else (1 if minutes > 0 else 0)

    return {
        # Identity + context ─────────────────────────────
        "match_id":              str(match_id),
        "match_date":            match_date,
        "season":                int(season),
        "league":                league,
        "player_id":             str(player_id),
        "player_name":           player_name,
        "name_canonical":        _canonicalize_name(player_name),
        "team_id":               team_id,
        "team_name":             (home_team_name if is_home
                                    else away_team_name),
        "is_home":               is_home,
        "opponent_team_id":      opponent_team_id,
        "opponent_team_name":    opponent_team_name,
        "position":              position,
        "position_order":        _int(player_stats.get("positionOrder"), 0),

        # Match totals (context) ────────────────────────
        "team_goals_scored":     team_goals_scored,
        "team_goals_conceded":   team_goals_conceded,
        "team_xg":               team_xg,
        "opponent_xg":           opponent_xg,
        "home_goals":            home_goals,
        "away_goals":            away_goals,

        # Player performance ─────────────────────────────
        "minutes":               minutes,
        "starts":                starts,
        "roster_in":             roster_in,
        "roster_out":            roster_out,
        "goals":                 _int(player_stats.get("goals")),
        "assists":               _int(player_stats.get("assists")),
        "own_goals":             _int(player_stats.get("own_goals")),
        "shots":                 _int(player_stats.get("shots")),
        "shots_on_target":       sot,
        "key_passes":            _int(player_stats.get("key_passes")),
        "xg":                    _num(player_stats.get("xG")),
        "xa":                    _num(player_stats.get("xA")),
        "xg_chain":              _num(player_stats.get("xGChain")),
        "xg_buildup":            _num(player_stats.get("xGBuildup")),
        "yellow_card":           _int(player_stats.get("yellow_card")),
        "red_card":              _int(player_stats.get("red_card")),

        # Bookkeeping ────────────────────────────────────
        "source":                "understat",
        "ingested_at":           datetime.now(timezone.utc).isoformat(),
    }


# ─────────────────────────────────────────────────────────────────────
# Index creation (idempotent)
# ─────────────────────────────────────────────────────────────────────
async def ensure_indexes(db) -> None:
    """Create the indexes needed for both ingestion and feature builds."""
    coll = db[COLLECTION_NAME]
    await coll.create_index(
        [("match_id", 1), ("player_id", 1)],
        unique=True, name="uniq_match_player",
    )
    await coll.create_index(
        [("name_canonical", 1), ("match_date", -1)],
        name="name_date_desc",
    )
    await coll.create_index(
        [("player_id", 1), ("match_date", -1)],
        name="pid_date_desc",
    )
    await coll.create_index(
        [("league", 1), ("season", 1)],
        name="league_season",
    )


# ─────────────────────────────────────────────────────────────────────
# Public ingest entry points
# ─────────────────────────────────────────────────────────────────────
async def ingest_league_season(
    db,
    league: str,
    season: int,
    *,
    max_matches: Optional[int] = None,
    skip_existing: bool = True,
    log_every: int = 25,
) -> dict:
    """Ingest one (league, season).

    Args
    ─────
      max_matches   — cap for POC / smoke runs. None = no cap.
      skip_existing — when True (default), matches already fully
                       ingested (any player row present) are skipped.
      log_every     — progress log every N matches.

    Returns counters dict:
      {league, season, matches_seen, matches_fetched, matches_skipped,
       matches_errored, players_upserted, elapsed_sec}
    """
    counters = {
        "league":            league,
        "season":            season,
        "matches_seen":      0,
        "matches_fetched":   0,
        "matches_skipped":   0,
        "matches_errored":   0,
        "players_upserted":  0,
        "started_at":        datetime.now(timezone.utc).isoformat(),
        "elapsed_sec":       0.0,
    }
    start = _time.monotonic()

    await ensure_indexes(db)
    coll = db[COLLECTION_NAME]

    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}) as client:
        matches = await fetch_league_matches(client, league, season)
        if matches is None:
            counters["error"] = "league listing fetch failed"
            counters["elapsed_sec"] = round(_time.monotonic() - start, 2)
            return counters

        # Filter to *completed* matches only — future fixtures have no
        # per-player data yet.
        completed = [m for m in matches if m.get("isResult")]
        counters["matches_seen"] = len(completed)
        if max_matches:
            completed = completed[: int(max_matches)]

        for i, m in enumerate(completed, 1):
            match_id = str(m.get("id"))
            if not match_id:
                counters["matches_errored"] += 1
                continue

            if skip_existing:
                exists = await coll.find_one(
                    {"match_id": match_id}, {"_id": 1},
                )
                if exists:
                    counters["matches_skipped"] += 1
                    if i % log_every == 0:
                        logger.info(
                            "[%s/%s] soccer ingest progress: %s",
                            league, season, counters,
                        )
                    continue

            data = await fetch_match_data(client, match_id)
            if not data or "rosters" not in data:
                counters["matches_errored"] += 1
                continue

            rosters = data["rosters"] or {}
            shots = data.get("shots") or {}
            shots_h = shots.get("h") or []
            shots_a = shots.get("a") or []

            home_meta = m.get("h") or {}
            away_meta = m.get("a") or {}
            goals = m.get("goals") or {}
            xg = m.get("xG") or {}
            match_date = m.get("datetime") or ""

            # Build one player row per roster entry (both sides).
            rows: list[dict] = []
            for side_key in ("h", "a"):
                side = rosters.get(side_key) or {}
                if not isinstance(side, dict):
                    continue
                for _roster_id, pstats in side.items():
                    if not isinstance(pstats, dict):
                        continue
                    # The outer dict key is the per-match ROSTER row id.
                    # The stable player_id lives in `pstats["player_id"]`
                    # and matches the `shots[].player_id` join key.
                    canonical_pid = str(
                        pstats.get("player_id") or _roster_id or ""
                    )
                    if not canonical_pid:
                        continue
                    row = _build_player_row(
                        match_id=match_id,
                        match_date=match_date,
                        league=league,
                        season=season,
                        home_team_id=str(home_meta.get("id") or ""),
                        home_team_name=home_meta.get("title") or "",
                        away_team_id=str(away_meta.get("id") or ""),
                        away_team_name=away_meta.get("title") or "",
                        home_goals=_int(goals.get("h")),
                        away_goals=_int(goals.get("a")),
                        home_xg=_num(xg.get("h")),
                        away_xg=_num(xg.get("a")),
                        player_id=canonical_pid,
                        player_stats=pstats,
                        shots_h=shots_h,
                        shots_a=shots_a,
                    )
                    rows.append(row)

            if not rows:
                counters["matches_errored"] += 1
                continue

            try:
                from pymongo import UpdateOne
                ops = [
                    UpdateOne(
                        {"match_id": r["match_id"],
                          "player_id": r["player_id"]},
                        {"$set": r}, upsert=True,
                    )
                    for r in rows
                ]
                res = await coll.bulk_write(ops, ordered=False)
                counters["players_upserted"] += (
                    (res.upserted_count or 0) + (res.modified_count or 0)
                )
                counters["matches_fetched"] += 1
            except Exception as e:
                logger.exception(
                    "soccer_upsert failed for match %s: %s", match_id, e,
                )
                counters["matches_errored"] += 1

            if i % log_every == 0:
                elapsed = round(_time.monotonic() - start, 1)
                logger.info(
                    "[%s/%s] %s/%s matches · players_upserted=%s · %.1fs",
                    league, season, i, len(completed),
                    counters["players_upserted"], elapsed,
                )

    counters["elapsed_sec"] = round(_time.monotonic() - start, 2)
    counters["finished_at"] = datetime.now(timezone.utc).isoformat()
    logger.info("soccer ingest %s/%s DONE: %s", league, season, counters)
    return counters


async def ingest_big5_seasons(
    db,
    seasons: tuple[int, ...] = (2023, 2024, 2025),
    leagues: tuple[str, ...] = BIG5_LEAGUES,
    *,
    max_matches_per_league: Optional[int] = None,
    skip_existing: bool = True,
) -> dict:
    """Sequential ingest across all Big-5 leagues × seasons.

    Sequential (not parallel) so we NEVER exceed the polite request
    interval to understat.com.
    """
    results: dict[str, dict] = {}
    for league in leagues:
        for season in seasons:
            key = f"{league}_{season}"
            results[key] = await ingest_league_season(
                db, league, season,
                max_matches=max_matches_per_league,
                skip_existing=skip_existing,
            )
    return results


__all__ = [
    "BIG5_LEAGUES",
    "COLLECTION_NAME",
    "ensure_indexes",
    "fetch_league_matches",
    "fetch_match_data",
    "ingest_league_season",
    "ingest_big5_seasons",
]
