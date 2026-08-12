"""History Source-Gap Closures — locked history/backfill sequence.

Additive-only closures for the documented gaps:

* **MLB team season derivation** — the `db.games` MLB rows lack an
  explicit `season` field but the season is deterministic from the
  ISO date (MLB season is contained within one calendar year).
* **Tennis richer authoritative source** — `db.tennis_matches_history`
  carries per-match winner/loser stats.  Normalise both perspectives
  into `db.player_game_actuals` (adds surface, aces, DFs, break points).
* **Soccer team results from settlement_events** — real final scores
  live in `db.settlement_events.actual_result.final_score`; normalise
  finished-fixture-with-score rows into `db.team_game_actuals`.

No fabrication.  Missing values stay `None`.  Every insert is
idempotent by canonical key.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from .backfill import BACKFILL_VERSION as PLAYER_BACKFILL_VERSION
# NOTE: tennis normalises into the *player* actuals store, not team.
from ..player_history.backfill import TARGET_COLLECTION as PLAYER_ACTUALS_COLL

logger = logging.getLogger("lockscore.gap_closure")


def _f(v):
    if v is None or v == "" or isinstance(v, bool):
        return None
    try:
        f = float(v)
        return None if f != f else f
    except (TypeError, ValueError):
        return None


def _derive_mlb_season(date_iso: Optional[str]) -> Optional[int]:
    """MLB seasons are calendar-year contained (Feb spring training
    → Oct/Nov World Series).  Deterministic year → season."""
    if not date_iso or not isinstance(date_iso, str):
        return None
    try:
        return int(date_iso[:4])
    except (TypeError, ValueError):
        return None


async def apply_mlb_season_backfill(db) -> dict:
    """Populate `season` on existing MLB `team_game_actuals` rows
    where it is currently missing.  Deterministic — the ISO date
    fully determines the MLB season."""
    counters = {"updated": 0, "skipped": 0, "no_date": 0,
                  "seasons": set()}
    q = {"sport": "mlb", "season": None}
    cursor = db.team_game_actuals.find(q, {"_id": 0, "event_id": 1,
                                             "canonical_team_id": 1,
                                             "event_time": 1})
    async for row in cursor:
        s = _derive_mlb_season(row.get("event_time"))
        if s is None:
            counters["no_date"] += 1
            continue
        counters["seasons"].add(s)
        try:
            await db.team_game_actuals.update_one(
                {"sport": "mlb", "event_id": row["event_id"],
                  "canonical_team_id": row["canonical_team_id"]},
                {"$set": {"season": s}},
            )
            counters["updated"] += 1
        except Exception as e:                       # pragma: no cover
            logger.debug("season backfill failure: %s", e)
    counters["seasons"] = sorted(counters["seasons"])
    return counters


# ═══════════════════════════════════════════════════════════════════
# Tennis richer player history from tennis_matches_history
# ═══════════════════════════════════════════════════════════════════
def _tennis_row_actuals(row: dict, side: str) -> dict:
    """Build actuals for one side of a tennis match.

    ``side`` is ``"w"`` (winner) or ``"l"`` (loser).  Legacy field
    prefix mirrors the source: ``w_ace``, ``l_ace``, ``w_df``,
    ``l_df``, etc.  A field that is `None` remains `None`.
    """
    p = side
    return {
        "aces":            _f(row.get(f"{p}_ace")),
        "double_faults":   _f(row.get(f"{p}_df")),
        "serve_points":    _f(row.get(f"{p}_svpt")),
        "first_in":        _f(row.get(f"{p}_1stIn")),
        "first_won":       _f(row.get(f"{p}_1stWon")),
        "second_won":      _f(row.get(f"{p}_2ndWon")),
        "service_games":   _f(row.get(f"{p}_SvGms")),
        "break_points_saved": _f(row.get(f"{p}_bpSaved")),
        "break_points_faced": _f(row.get(f"{p}_bpFaced")),
    }


async def backfill_tennis_from_matches_history(
    db,
    *,
    limit: int = 200_000,
    dry_run: bool = False,
) -> dict:
    counters = {
        "examined":            0,
        "accepted":            0,
        "identity_unresolved": 0,
        "inserted":            0,
        "updated":             0,
        "duplicates_avoided":  0,
        "players":             set(),
        "date_min":            None,
        "date_max":            None,
    }
    cursor = db.tennis_matches_history.find({}, {"_id": 0}).limit(limit)
    async for row in cursor:
        counters["examined"] += 1
        winner_id = row.get("winner_id")
        loser_id  = row.get("loser_id")
        if not winner_id or not loser_id:
            counters["identity_unresolved"] += 1
            continue
        date_iso = row.get("date")
        if isinstance(date_iso, str) and "T" not in date_iso:
            date_iso = date_iso + "T00:00:00Z"
        tourney_id = row.get("tourney_id") or ""
        # Deterministic match id.
        match_id = row.get("match_id") or f"{tourney_id}:{winner_id}:{loser_id}:{date_iso[:10]}"
        surface = (row.get("surface") or "").lower() or None
        season = None
        try:
            season = int(date_iso[:4]) if date_iso else None
        except (TypeError, ValueError):
            season = None
        if date_iso:
            counters["date_min"] = min(counters["date_min"] or date_iso,
                                         date_iso)
            counters["date_max"] = max(counters["date_max"] or date_iso,
                                         date_iso)
        for side, cpid, opp_id, result in (
            ("w", winner_id, loser_id, "WIN"),
            ("l", loser_id,  winner_id, "LOSS"),
        ):
            counters["players"].add(str(cpid))
            counters["accepted"] += 1
            doc = {
                "sport":               "tennis",
                "canonical_player_id": str(cpid),
                "player_id":           str(cpid),
                "player_name":         (row.get("winner_name")
                                          if side == "w"
                                          else row.get("loser_name")),
                "opponent":            str(opp_id),
                "event_id":            f"tennis-{tourney_id}-{match_id}",
                "event_time":          date_iso,
                "season":              season,
                "surface":             surface,
                "tournament":          row.get("tourney_name") or tourney_id,
                "round":               row.get("round"),
                "result":              result,
                "actuals":             _tennis_row_actuals(row, side),
                "source":              "tennis_matches_history",
                "source_record_id":    f"{tourney_id}:{match_id}",
                "backfill_version":    "tennis-gap-v1.0.0",
                "ingested_at":         datetime.now(timezone.utc).isoformat(),
            }
            if dry_run:
                continue
            filt = {"sport": "tennis",
                     "canonical_player_id": str(cpid),
                     "event_id": doc["event_id"]}
            try:
                existing = await db[PLAYER_ACTUALS_COLL].find_one(
                    filt, {"_id": 1})
                if existing:
                    await db[PLAYER_ACTUALS_COLL].update_one(
                        filt, {"$set": doc})
                    counters["updated"] += 1
                    counters["duplicates_avoided"] += 1
                else:
                    await db[PLAYER_ACTUALS_COLL].insert_one(doc)
                    counters["inserted"] += 1
            except Exception as e:                   # pragma: no cover
                logger.debug("tennis gap upsert failure: %s", e)
    counters["players"] = len(counters["players"])
    return counters


# ═══════════════════════════════════════════════════════════════════
# Soccer team results from settlement_events
# ═══════════════════════════════════════════════════════════════════
async def backfill_soccer_teams_from_settlement_events(
    db,
    *,
    limit: int = 50_000,
    dry_run: bool = False,
) -> dict:
    from .backfill import _emit_two_perspective_rows
    counters = {
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
    q = {"result": "won", "actual_result.final_score": {"$exists": True}}
    cursor = db.settlement_events.find(q, {"_id": 0}).limit(limit)
    async for row in cursor:
        counters["examined"] += 1
        ar = row.get("actual_result") or {}
        fs = ar.get("final_score") or {}
        # final_score is {"Team A": "score", "Team B": "score"}
        if not isinstance(fs, dict) or len(fs) < 2:
            counters["missing_result"] += 1
            continue
        teams = list(fs.keys())
        home_name, away_name = teams[0], teams[1]
        try:
            home_s = int(fs[home_name])
            away_s = int(fs[away_name])
        except (TypeError, ValueError):
            counters["missing_result"] += 1
            continue
        event_time = row.get("settled_at")
        event_id = row.get("event_id") or row.get("prediction_id")
        if not event_id:
            counters["identity_unresolved"] += 1
            continue
        season = None
        if isinstance(event_time, str):
            try:
                season = int(event_time[:4])
            except (TypeError, ValueError):
                season = None
        await _emit_two_perspective_rows(
            db,
            sport="soccer",
            event_id=str(event_id),
            event_time=event_time,
            season=season,
            competition=None,
            home_team_id=home_name,
            away_team_id=away_name,
            home_score=home_s,
            away_score=away_s,
            extra={},
            source="settlement_events",
            source_record_id=str(event_id),
            counters=counters,
            dry_run=dry_run,
        )
    counters["seasons"] = sorted(counters["seasons"])
    counters["teams"] = len(counters["teams"])
    return counters


__all__ = [
    "apply_mlb_season_backfill",
    "backfill_tennis_from_matches_history",
    "backfill_soccer_teams_from_settlement_events",
    "_derive_mlb_season",
]
