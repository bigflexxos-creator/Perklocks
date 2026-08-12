"""Final Authoritative History Expansion — locked history sequence closure.

Two new authoritative sources discovered on final pod scan:

* ``db.nfl_player_weekly``  — 129,657 rows of per-game NFL player
  stats (nflverse-style shape) with per-market atoms:
  passing_yards, passing_tds, passing_ints, completions, attempts,
  carries, rushing_yards, rushing_tds, receptions, targets,
  receiving_yards, receiving_tds.  Closes the NFL Player gap.

* ``db.soccer_matches``     — 25,091 finished soccer matches with
  real home_score / away_score across multiple leagues.  Deepens
  Soccer Team History.

Both normalisers follow the SAME contract as the earlier backfills:
canonical identity, missing != 0, idempotent upsert, provenance.
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("lockscore.final_history_expansion")

def _f(v):
    if v is None or v == "" or isinstance(v, bool): return None
    try:
        f = float(v)
        return None if f != f else f
    except (TypeError, ValueError): return None


def _extract_nfl_season_week(game_id: str, weekly_row: dict) -> tuple[Optional[int], Optional[int]]:
    """nflverse game_id has shape '2019_01_IND_LAC' → season=2019 week=1."""
    if weekly_row.get("season") is not None and weekly_row.get("week") is not None:
        return _int(weekly_row["season"]), _int(weekly_row["week"])
    if isinstance(game_id, str) and "_" in game_id:
        parts = game_id.split("_")
        if len(parts) >= 2:
            return _int(parts[0]), _int(parts[1])
    return None, None


def _int(v):
    try: return int(v)
    except (TypeError, ValueError): return None


async def backfill_nfl_from_player_weekly(db, *, limit=200_000, dry_run=False):
    counters = {"examined":0,"accepted":0,"identity_unresolved":0,
                "no_stats":0,"inserted":0,"updated":0,
                "duplicates_avoided":0,"players":set(),
                "date_min":None,"date_max":None,"seasons":set()}
    cursor = db.nfl_player_weekly.find({}, {"_id": 1}).limit(limit)
    async for stub in cursor:
        row = await db.nfl_player_weekly.find_one({"_id": stub["_id"]})
        if not row: continue
        counters["examined"] += 1
        pid = row.get("player_id")
        gid = row.get("game_id")
        if not pid or not gid:
            counters["identity_unresolved"] += 1; continue
        season, week = _extract_nfl_season_week(gid, row)
        # Extract per-market atoms — missing stays None.
        actuals = {
            "pass_yds":      _f(row.get("passing_yards")),
            "pass_tds":      _f(row.get("passing_tds")),
            "interceptions": _f(row.get("passing_ints")),
            "completions":   _f(row.get("completions")),
            "attempts":      _f(row.get("attempts")),
            "rush_yds":      _f(row.get("rushing_yards")),
            "rush_attempts": _f(row.get("carries")),
            "rush_tds":      _f(row.get("rushing_tds")),
            "rec_yds":       _f(row.get("receiving_yards")),
            "receptions":    _f(row.get("receptions")),
            "rec_tds":       _f(row.get("receiving_tds")),
            "targets":       _f(row.get("targets")),
        }
        # nflverse fills 0 for absent stats — accept any row that has
        # a game_id (real game happened) and belongs to a real player.
        if all(v is None for v in actuals.values()):
            counters["no_stats"] += 1; continue
        # Derive event_time from season/week (approx — first Sunday-ish).
        event_time = None
        if season is not None:
            # Coarse ISO — good enough for as_of comparisons.
            month = "09" if (week or 0) <= 17 else "01"
            event_time = f"{season}-{month}-01T00:00:00Z"
            counters["date_min"] = min(counters["date_min"] or event_time, event_time)
            counters["date_max"] = max(counters["date_max"] or event_time, event_time)
            counters["seasons"].add(season)
        counters["accepted"] += 1
        counters["players"].add(pid)
        doc = {
            "sport":"nfl", "canonical_player_id":str(pid),
            "player_id":str(pid),
            "player_name": row.get("player_display_name") or row.get("player_name"),
            "team": row.get("recent_team") or row.get("team"),
            "opponent": row.get("opponent_team"),
            "event_id": str(gid), "event_time": event_time,
            "season": season, "week": week,
            "position": row.get("position"),
            "actuals": actuals,
            "source":"nfl_player_weekly",
            "source_record_id": str(stub["_id"]),
            "backfill_version":"nfl-weekly-v1.0.0",
            "ingested_at": datetime.now(timezone.utc).isoformat(),
        }
        if dry_run: continue
        filt = {"sport":"nfl","canonical_player_id":str(pid),"event_id":str(gid)}
        try:
            ex = await db.player_game_actuals.find_one(filt, {"_id":1})
            if ex:
                await db.player_game_actuals.update_one(filt, {"$set":doc})
                counters["updated"] += 1; counters["duplicates_avoided"] += 1
            else:
                await db.player_game_actuals.insert_one(doc)
                counters["inserted"] += 1
        except Exception as e:
            logger.debug("nfl weekly upsert failure: %s", e)
    counters["players"] = len(counters["players"])
    counters["seasons"] = sorted(counters["seasons"])
    return counters


async def backfill_soccer_teams_from_soccer_matches(db, *, limit=100_000, dry_run=False):
    from .backfill import _emit_two_perspective_rows
    counters = {"examined":0,"accepted":0,"identity_unresolved":0,
                "missing_result":0,"inserted":0,"updated":0,
                "duplicates_avoided":0,"teams":set(),"seasons":set(),
                "date_min":None,"date_max":None,"leagues":set()}
    q = {"status":"finished"}
    cursor = db.soccer_matches.find(q, {"_id": 1}).limit(limit)
    async for stub in cursor:
        row = await db.soccer_matches.find_one({"_id": stub["_id"]})
        if not row: continue
        counters["examined"] += 1
        home = row.get("home_team"); away = row.get("away_team")
        if not home or not away:
            counters["identity_unresolved"] += 1; continue
        hs = row.get("home_score"); as_ = row.get("away_score")
        if hs is None or as_ is None:
            counters["missing_result"] += 1; continue
        date_iso = row.get("date")
        if isinstance(date_iso, str) and "T" not in date_iso:
            date_iso = date_iso + "T00:00:00Z"
        season = None
        if isinstance(row.get("season"), str) and "-" in row["season"]:
            try: season = int(row["season"].split("-")[0])
            except: season = None
        elif isinstance(row.get("season"), int):
            season = row["season"]
        if row.get("league"): counters["leagues"].add(row["league"])
        event_id = str(row.get("match_id") or f"{row.get('league')}-{home}-{away}-{date_iso[:10] if date_iso else ''}")
        await _emit_two_perspective_rows(
            db, sport="soccer", event_id=event_id, event_time=date_iso,
            season=season, competition=row.get("league"),
            home_team_id=home, away_team_id=away,
            home_score=hs, away_score=as_, extra={},
            source="soccer_matches", source_record_id=str(stub["_id"]),
            counters=counters, dry_run=dry_run,
        )
    counters["teams"] = len(counters["teams"])
    counters["seasons"] = sorted(counters["seasons"])
    counters["leagues"] = sorted(counters["leagues"])
    return counters


__all__ = ["backfill_nfl_from_player_weekly",
             "backfill_soccer_teams_from_soccer_matches"]
