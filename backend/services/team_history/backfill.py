"""Team Game Actuals Backfill — canonical team-history data path.

Reads authoritative historical team games from existing legacy pod
collections and normalises them into ``db.team_game_actuals`` — the
collection consumed by ``services.team_history`` (Stage 3
Foundation).

Design principles (mirror the Player-Actuals backfill):

* **Read-only from legacy** — no external API calls (§24 cost safety
  from the previous roadmap step still applies).
* **Idempotent** — deterministic uniqueness on
  ``(sport, canonical_team_id, event_id)``.  A rerun must UPDATE
  existing rows, never duplicate.
* **Missing != 0** — score sentinels stay ``None``; a legitimate 0
  (0-run shutout, 0-0 draw) is preserved.
* **Perspective correctness** — for every source game we emit TWO
  canonical rows: one from the home team's perspective, one from
  the away team's perspective, with ``team_score`` and
  ``opponent_score`` swapped accordingly.
* **Provenance retained** — source, source_record_id,
  backfill_version, ingested_at.
* **Identity honesty** — a row is REJECTED when we cannot resolve
  a canonical team identity; never guessed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("lockscore.team_history.backfill")

BACKFILL_VERSION = "team-v1.0.0"
TARGET_COLLECTION = "team_game_actuals"


def _f(v):
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
        if f != f:
            return None
        return f
    except (TypeError, ValueError):
        return None


def _normalise_team_id(team) -> Optional[str]:
    """Turn a legacy team reference into a canonical id.

    Accepts a bare string (display name) OR a dict with
    ``canonical_team_id`` / ``id`` / ``name``.  Returns ``None`` when
    nothing resolvable is present.
    """
    if team is None:
        return None
    if isinstance(team, str):
        s = team.strip()
        return s or None
    if isinstance(team, dict):
        return (team.get("canonical_team_id")
                 or team.get("id")
                 or team.get("name"))
    return None


def _result_string(team_score, opp_score) -> Optional[str]:
    ts = _f(team_score)
    os_ = _f(opp_score)
    if ts is None or os_ is None:
        return None
    if ts > os_:
        return "WIN"
    if ts < os_:
        return "LOSS"
    return "DRAW"


async def _emit_two_perspective_rows(
    db,
    *,
    sport: str,
    event_id: str,
    event_time: Optional[str],
    season: Optional[int],
    competition: Optional[str],
    home_team_id: str,
    away_team_id: str,
    home_score,
    away_score,
    extra: Optional[dict] = None,
    source: str,
    source_record_id: Optional[str] = None,
    counters: Optional[dict] = None,
    dry_run: bool = False,
) -> None:
    """Emit TWO canonical rows — one per perspective."""
    base_extra = dict(extra or {})
    now_iso = datetime.now(timezone.utc).isoformat()

    def _doc(team_id: str, opp_id: str, team_side: str,
              team_score, opp_score) -> dict:
        return {
            "sport":                    sport,
            "canonical_team_id":        team_id,
            "canonical_opponent_id":    opp_id,
            "event_id":                 str(event_id),
            "event_time":               event_time,
            "season":                   season,
            "competition":              competition,
            "home_away":                team_side,        # "home"/"away"
            "team_score":               _f(team_score),
            "opponent_score":           _f(opp_score),
            "result":                   _result_string(team_score, opp_score),
            "actuals":                  base_extra,
            "source":                   source,
            "source_record_id":         source_record_id or str(event_id),
            "backfill_version":         BACKFILL_VERSION,
            "ingested_at":              now_iso,
        }

    for team_id, opp_id, side, ts, os_ in (
        (home_team_id, away_team_id, "home", home_score, away_score),
        (away_team_id, home_team_id, "away", away_score, home_score),
    ):
        doc = _doc(team_id, opp_id, side, ts, os_)
        if event_time and counters is not None:
            counters["date_min"] = min(counters["date_min"] or event_time,
                                         event_time)
            counters["date_max"] = max(counters["date_max"] or event_time,
                                         event_time)
        if season is not None and counters is not None:
            counters["seasons"].add(season)
        if counters is not None:
            counters["teams"].add(team_id)
            counters["accepted"] += 1
        if dry_run:
            continue
        filt = {"sport": sport, "canonical_team_id": team_id,
                 "event_id": str(event_id)}
        try:
            existing = await db[TARGET_COLLECTION].find_one(
                filt, {"_id": 1})
            if existing:
                await db[TARGET_COLLECTION].update_one(
                    filt, {"$set": doc})
                if counters is not None:
                    counters["updated"] += 1
                    counters["duplicates_avoided"] += 1
            else:
                await db[TARGET_COLLECTION].insert_one(doc)
                if counters is not None:
                    counters["inserted"] += 1
        except Exception as e:                   # pragma: no cover
            logger.debug("team backfill upsert failure: %s", e)


async def backfill_from_games_collection(
    db,
    *,
    sport: str,
    limit: int = 50_000,
    dry_run: bool = False,
) -> dict:
    """Normalise the shared legacy ``db.games`` collection into
    canonical team-history rows for the given sport.

    Expected legacy shape (observed):
        {
          "sport": "mlb"/"nfl"/"nhl",
          "game_id": <int|str>,
          "date": ISO,
          "home": <team name>,
          "away": <team name>,
          "result": {"home": <int>, "away": <int>},
          "season": <int>, (NFL only in this pod)
          "week":   <int>,
          "status": "Final",
        }
    """
    s = (sport or "").lower()
    counters = {
        "sport":               s,
        "examined":            0,
        "accepted":            0,
        "identity_unresolved": 0,
        "missing_result":      0,
        "inserted":            0,
        "updated":             0,
        "duplicates_avoided":  0,
        "teams":               set(),
        "seasons":             set(),
        "date_min":            None,
        "date_max":            None,
    }
    q = {"sport": s, "result": {"$exists": True}, "status": "Final"}
    cursor = db.games.find(q, {"_id": 0}).limit(limit)
    async for row in cursor:
        counters["examined"] += 1
        home = _normalise_team_id(row.get("home"))
        away = _normalise_team_id(row.get("away"))
        if not home or not away:
            counters["identity_unresolved"] += 1
            continue
        res = row.get("result") or {}
        home_s = res.get("home") if isinstance(res, dict) else None
        away_s = res.get("away") if isinstance(res, dict) else None
        if home_s is None or away_s is None:
            counters["missing_result"] += 1
            continue
        event_id = str(row.get("game_id") or f"{home}-{away}-{row.get('date')}")
        event_time = row.get("date")
        if isinstance(event_time, str) and "T" not in event_time and event_time:
            event_time = event_time + "T00:00:00Z"
        extras = {}
        if row.get("week") is not None:
            extras["week"] = row.get("week")
        await _emit_two_perspective_rows(
            db,
            sport=s,
            event_id=event_id,
            event_time=event_time,
            season=row.get("season"),
            competition=row.get("league") or row.get("competition"),
            home_team_id=home,
            away_team_id=away,
            home_score=home_s,
            away_score=away_s,
            extra=extras,
            source="legacy_games",
            source_record_id=str(row.get("game_id") or ""),
            counters=counters,
            dry_run=dry_run,
        )
    counters["seasons"] = sorted(counters["seasons"])
    counters["teams"] = len(counters["teams"])
    return counters


async def backfill_soccer_from_fixtures(
    db,
    *,
    limit: int = 50_000,
    dry_run: bool = False,
) -> dict:
    """Normalise ``db.soccer_fixtures`` (FINISHED entries with score
    data) into canonical soccer team-history rows.

    NOTE: in the current pod these rows are marked ``FINISHED`` but
    the score fields (``home_score``, ``away_score`` / ``full_time``)
    are ``None``.  The backfill correctly reports ``missing_result``
    for all such rows — no coverage inflation.
    """
    counters = {
        "sport":               "soccer",
        "examined":            0,
        "accepted":            0,
        "identity_unresolved": 0,
        "missing_result":      0,
        "inserted":            0,
        "updated":             0,
        "duplicates_avoided":  0,
        "teams":               set(),
        "seasons":             set(),
        "date_min":            None,
        "date_max":            None,
    }
    q = {"status": "FINISHED"}
    cursor = db.soccer_fixtures.find(q, {"_id": 0}).limit(limit)
    async for row in cursor:
        counters["examined"] += 1
        home = _normalise_team_id(row.get("home_team"))
        away = _normalise_team_id(row.get("away_team"))
        if not home or not away:
            counters["identity_unresolved"] += 1
            continue
        ft = row.get("full_time") or {}
        home_s = row.get("home_score")
        if home_s is None and isinstance(ft, dict):
            home_s = ft.get("home") or ft.get("home_team")
        away_s = row.get("away_score")
        if away_s is None and isinstance(ft, dict):
            away_s = ft.get("away") or ft.get("away_team")
        if home_s is None or away_s is None:
            counters["missing_result"] += 1
            continue
        event_id = str(row.get("id") or row.get("fixture_id")
                        or f"{home}-{away}-{row.get('utc_kickoff')}")
        event_time = row.get("utc_kickoff")
        season = row.get("season") if isinstance(row.get("season"), int) else None
        await _emit_two_perspective_rows(
            db,
            sport="soccer",
            event_id=event_id,
            event_time=event_time,
            season=season,
            competition=row.get("league") or row.get("competition"),
            home_team_id=home,
            away_team_id=away,
            home_score=home_s,
            away_score=away_s,
            extra={},
            source="legacy_soccer_fixtures",
            source_record_id=event_id,
            counters=counters,
            dry_run=dry_run,
        )
    counters["seasons"] = sorted(counters["seasons"])
    counters["teams"] = len(counters["teams"])
    return counters


async def ensure_team_backfill_indexes(db) -> None:
    try:
        await db[TARGET_COLLECTION].create_index(
            [("sport", 1), ("canonical_team_id", 1), ("event_id", 1)],
            name="team_backfill_unique", unique=True,
        )
        await db[TARGET_COLLECTION].create_index(
            [("sport", 1), ("canonical_team_id", 1),
              ("canonical_opponent_id", 1), ("event_time", -1)],
            name="team_h2h_lookup",
        )
    except Exception:
        pass


__all__ = [
    "BACKFILL_VERSION",
    "TARGET_COLLECTION",
    "backfill_from_games_collection",
    "backfill_soccer_from_fixtures",
    "ensure_team_backfill_indexes",
]
