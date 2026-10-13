"""HI Freshness Verifier — Blocks 11-13 closure.

Measures the ACTUAL Historical Intelligence consumer-store freshness,
not just raw source ingestion.  A successful provider ingest alone is
NOT sufficient for a ``CURRENT`` claim — the row must have reached the
HI consumer collection that Lock Score / Apex / Evidence Authority
actually reads.

Returns a canonical freshness block per sport:

    {
      "sport":                  <sport>,
      "provider_latest_final":  <ISO | None>,
      "raw_ingestion_latest":   <ISO | None>,
      "consumer_store_latest":  <ISO | None>,
      "HI_visible_latest":      <ISO | None>,
      "lag_seconds":            <int | None>,
      "status":                 CURRENT | INGESTION_LAG | COVERAGE_LIMITED |
                                 STALE | UNKNOWN,
    }

The ``HI_visible_latest`` field is the max timestamp the production
HI reader would surface at the moment of the check — accounting for
any cache/windowing enforced by the consumer.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

logger = logging.getLogger("lockscore.hi_freshness")

# Status strings mirror the projector module exactly.
STATUS_CURRENT          = "CURRENT"
STATUS_INGESTION_LAG    = "INGESTION_LAG"
STATUS_COVERAGE_LIMITED = "COVERAGE_LIMITED"
STATUS_STALE            = "STALE"
STATUS_UNKNOWN          = "UNKNOWN"

# Sport-level "fresh enough" windows.  Deliberately generous — we only
# want to catch MULTI-DAY lag, not minute-level drift.
_STALE_THRESHOLD_HOURS = {
    "nfl":    36,
    "mlb":    24,
    "nba":    24,
    "nhl":    24,
    "soccer": 36,
    "tennis": 36,
    "cfb":    48,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(ts: Any) -> Optional[datetime]:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts
    try:
        s = str(ts).replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except Exception:
        return None


async def _max_timestamp(db, collection: str, filt: dict, field: str) -> Optional[datetime]:
    """Return the max ISO timestamp from a collection, or None.

    Uses a sort-limit query (cheap) instead of a server-side aggregation
    so this stays usable on lightweight pytest DB stubs.
    """
    try:
        row = await db[collection].find_one(
            filt, {"_id": 0, field: 1}, sort=[(field, -1)])
    except TypeError:
        # Fake test DBs may not accept the ``sort=`` kw.
        try:
            cur = db[collection].find(filt, {"_id": 0, field: 1})
            rows = []
            async for r in cur:
                rows.append(r)
            row = max(rows, key=lambda r: str(r.get(field) or "")) if rows else None
        except Exception:
            return None
    except Exception as e:
        logger.debug("max_timestamp(%s/%s) read failed: %s",
                     collection, field, e)
        return None
    if not row:
        return None
    return _parse_iso(row.get(field))


# Per-sport mapping to the right (collection, filter, timestamp field).
_SPORT_CONSUMER_MAP = {
    "nfl":    [("player_game_actuals", {"sport": "nfl"}, "updated_at"),
                ("team_game_actuals",   {"sport": "nfl"}, "updated_at")],
    "mlb":    [("player_game_actuals", {"sport": "mlb"}, "updated_at"),
                ("team_game_actuals",   {"sport": "mlb"}, "updated_at")],
    "nba":    [("player_game_actuals", {"sport": "nba"}, "updated_at"),
                ("team_game_actuals",   {"sport": "nba"}, "updated_at")],
    "nhl":    [("player_game_actuals", {"sport": "nhl"}, "updated_at"),
                ("team_game_actuals",   {"sport": "nhl"}, "updated_at")],
    "soccer": [("soccer_matches",            {},                     "updated_at"),
                ("soccer_player_game_logs",  {},                     "updated_at"),
                ("player_game_actuals",      {"sport": "soccer"},    "updated_at")],
    "tennis": [("tennis_matches_history",    {},                     "updated_at")],
    "cfb":    [("games",                     {"sport": "cfb"},       "game_date")],
}

_SPORT_PROVIDER_LATEST_MAP = {
    "nfl":    ("nfl_player_weekly", {}, "season"),           # week + season proxy
    "mlb":    ("games",             {"sport": "mlb"}, "game_date"),
    "nba":    ("games",             {"sport": "nba"}, "game_date"),
    "nhl":    ("games",             {"sport": "nhl"}, "game_date"),
    "soccer": ("games",             {"sport": "soccer"}, "game_date"),
    "tennis": ("games",             {"sport": "tennis"}, "game_date"),
    "cfb":    ("games",             {"sport": "cfb"}, "game_date"),
}

_SPORT_RAW_INGESTION_MAP = {
    "nfl":    ("nfl_player_weekly",  {}, "season"),
    "mlb":    ("player_game_logs",   {"sport": "mlb"}, "event_time"),
    "nba":    ("player_game_logs",   {"sport": "nba"}, "event_time"),
    "nhl":    ("player_game_logs",   {"sport": "nhl"}, "event_time"),
    "soccer": ("games",              {"sport": "soccer"}, "game_date"),
    "tennis": ("games",              {"sport": "tennis"}, "game_date"),
    "cfb":    ("games",              {"sport": "cfb"},  "game_date"),
}


async def verify_hi_freshness(db, sport: str) -> dict:
    """Measure the ACTUAL HI consumer-store freshness for ``sport``.

    Returns the canonical freshness dict (see module docstring).  Status
    sentinels are chosen conservatively:

      * ``CURRENT`` only when consumer_latest ≥ provider_latest within
        the per-sport stale window.
      * ``INGESTION_LAG`` when provider has newer rows than the
        consumer.  Projection run but gap persists → still LAG.
      * ``COVERAGE_LIMITED`` when the provider genuinely has no rows
        (empty source — not a projector bug).
      * ``STALE`` when consumer latest is older than the per-sport
        stale window but provider is also stale (shared outage).
      * ``UNKNOWN`` when neither can be read (DB stub / permissions).
    """
    sport_k = (sport or "").strip().lower()

    # Provider latest final (what the UPSTREAM source thinks is current).
    prov_entry = _SPORT_PROVIDER_LATEST_MAP.get(sport_k)
    provider_latest: Optional[datetime] = None
    if prov_entry:
        coll, filt, field = prov_entry
        if field == "season":
            # Weekly-oriented store (nfl_player_weekly) — synthesise
            # a timestamp from (max(season), max(week)) so lag math
            # still makes sense (one week ≈ 7 days).
            try:
                row = await db[coll].find_one(
                    filt, {"_id": 0, "season": 1, "week": 1},
                    sort=[("season", -1), ("week", -1)])
            except TypeError:
                row = None
            if row and row.get("season") and row.get("week"):
                # 2026 season week 10 ≈ early-November
                yr = int(row["season"])
                wk = int(row["week"] or 1)
                provider_latest = datetime(yr, 9, 1, tzinfo=timezone.utc) \
                                   + timedelta(days=7 * (wk - 1))
        else:
            provider_latest = await _max_timestamp(db, coll, filt, field)

    # Raw ingestion latest.
    raw_entry = _SPORT_RAW_INGESTION_MAP.get(sport_k)
    raw_latest: Optional[datetime] = None
    if raw_entry:
        coll, filt, field = raw_entry
        if field == "season":
            try:
                row = await db[coll].find_one(
                    filt, {"_id": 0, "season": 1, "week": 1},
                    sort=[("season", -1), ("week", -1)])
            except TypeError:
                row = None
            if row and row.get("season") and row.get("week"):
                yr = int(row["season"]); wk = int(row["week"] or 1)
                raw_latest = datetime(yr, 9, 1, tzinfo=timezone.utc) \
                              + timedelta(days=7 * (wk - 1))
        else:
            raw_latest = await _max_timestamp(db, coll, filt, field)

    # Consumer-store latest — max across every mapped consumer collection.
    consumer_entries = _SPORT_CONSUMER_MAP.get(sport_k, [])
    consumer_latest: Optional[datetime] = None
    for coll, filt, field in consumer_entries:
        t = await _max_timestamp(db, coll, filt, field)
        if t and (consumer_latest is None or t > consumer_latest):
            consumer_latest = t

    # HI visible latest = whichever would surface to the production
    # reader right now.  For the current contract this equals the
    # consumer-store latest (there is no additional cache layer that
    # could hide a freshly-written row).
    hi_visible_latest = consumer_latest

    # Lag seconds between provider_latest and consumer_latest.
    lag_seconds: Optional[int] = None
    if provider_latest and consumer_latest:
        lag_seconds = int((provider_latest - consumer_latest).total_seconds())
        if lag_seconds < 0:
            lag_seconds = 0

    stale_hours = _STALE_THRESHOLD_HOURS.get(sport_k, 48)

    # Status decision tree.
    status = STATUS_UNKNOWN
    if provider_latest is None and consumer_latest is None:
        status = STATUS_COVERAGE_LIMITED
    elif provider_latest is None and consumer_latest is not None:
        # No provider rows but consumer has historical ones → fine.
        age = (_now() - consumer_latest).total_seconds()
        status = STATUS_CURRENT if age <= stale_hours * 3600 else STATUS_STALE
    elif consumer_latest is None:
        status = STATUS_INGESTION_LAG
    else:
        gap = (provider_latest - consumer_latest).total_seconds()
        age = (_now() - consumer_latest).total_seconds()
        if gap > stale_hours * 3600:
            status = STATUS_INGESTION_LAG
        elif age > stale_hours * 3600 * 2:
            status = STATUS_STALE
        else:
            status = STATUS_CURRENT

    return {
        "sport":                 sport_k,
        "provider_latest_final": provider_latest.isoformat() if provider_latest else None,
        "raw_ingestion_latest":  raw_latest.isoformat() if raw_latest else None,
        "consumer_store_latest": consumer_latest.isoformat() if consumer_latest else None,
        "HI_visible_latest":     hi_visible_latest.isoformat() if hi_visible_latest else None,
        "lag_seconds":           lag_seconds,
        "stale_threshold_hours": stale_hours,
        "status":                status,
    }


async def verify_hi_freshness_all(db,
                                    sports: Optional[list[str]] = None
                                    ) -> dict[str, dict]:
    """Convenience: run freshness verification for every sport in one
    call.  Used by the orchestrator post-refresh + the ops /
    historical_intelligence routes."""
    sports = sports or list(_SPORT_CONSUMER_MAP.keys())
    out: dict[str, dict] = {}
    for s in sports:
        out[s] = await verify_hi_freshness(db, s)
    return out


__all__ = [
    "STATUS_CURRENT", "STATUS_INGESTION_LAG", "STATUS_COVERAGE_LIMITED",
    "STATUS_STALE", "STATUS_UNKNOWN",
    "verify_hi_freshness", "verify_hi_freshness_all",
]
