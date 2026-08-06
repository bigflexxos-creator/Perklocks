"""tournament_registry — Phase 2γ Odds API sport-key registry.

The Phase 2α audit showed the tennis + soccer discovery paths were
fanning out to /sports and per-event endpoints for every inactive
tournament on every snapshot.  This registry tracks the health of
each ``sport_key`` and lets discovery skip inactive keys.

Suppression policy
──────────────────
- Keys represented in current published picks always remain eligible
  (regardless of upstream signals).
- Active keys (recent successful catalog/event fetch) use normal cadence.
- Recently active keys use reduced cadence (min_interval doubles).
- Inactive keys with no recent events are suppressed for 24-72 hours.
- Repeatedly empty keys get progressively longer suppression (capped
  at 7 days).
- Newly active keys are immediately unsuppressed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger("lockscore.tournament_registry")

TOURNAMENT_COLL = "odds_tournament_registry"

# Suppression windows tuned to the Odds API "sports" catalog cadence.
MIN_SUPPRESSION_SECONDS = 24 * 3600           # 24h floor
INITIAL_SUPPRESSION_SECONDS = 24 * 3600       # first suppression
MAX_SUPPRESSION_SECONDS = 7 * 24 * 3600       # 7-day cap
CONSECUTIVE_EMPTY_THRESHOLD = 3               # empties before suppressing


def _now() -> datetime:
    return datetime.now(timezone.utc)


class TournamentRegistry:

    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self.db = db

    async def ensure_indices(self) -> None:
        try:
            await self.db[TOURNAMENT_COLL].create_index(
                "sport_key", name="sport_key_uniq", unique=True)
            await self.db[TOURNAMENT_COLL].create_index(
                "suppress_until", name="suppress_until_idx")
            await self.db[TOURNAMENT_COLL].create_index(
                "sport_group", name="sport_group_idx")
            await self.db[TOURNAMENT_COLL].create_index(
                "updated_at", name="updated_at_idx")
        except Exception as e:  # pragma: no cover
            logger.debug("tournament_registry index create: %s", e)

    # ── Read helpers ────────────────────────────────────────────────
    async def is_eligible(self, sport_key: str, *,
                           bypass_if_in_current_picks: bool = True) -> bool:
        doc = await self.db[TOURNAMENT_COLL].find_one(
            {"sport_key": sport_key}, {"_id": 0})
        if not doc:
            return True
        if bypass_if_in_current_picks and doc.get("present_in_current_picks"):
            return True
        su = doc.get("suppress_until")
        if isinstance(su, datetime):
            if su.tzinfo is None:
                su = su.replace(tzinfo=timezone.utc)
            if su > _now():
                return False
        return True

    async def filter_eligible(self, sport_keys: list[str]) -> list[str]:
        """Return the subset of ``sport_keys`` currently eligible."""
        if not sport_keys:
            return []
        out: list[str] = []
        docs = await self.db[TOURNAMENT_COLL].find(
            {"sport_key": {"$in": list(sport_keys)}},
            {"_id": 0, "sport_key": 1,
             "suppress_until": 1, "present_in_current_picks": 1},
        ).to_list(len(sport_keys) * 2)
        by_key = {d["sport_key"]: d for d in docs}
        now = _now()
        for k in sport_keys:
            d = by_key.get(k)
            if not d:
                out.append(k)
                continue
            if d.get("present_in_current_picks"):
                out.append(k)
                continue
            su = d.get("suppress_until")
            if isinstance(su, datetime):
                if su.tzinfo is None:
                    su = su.replace(tzinfo=timezone.utc)
                if su > now:
                    continue
            out.append(k)
        return out

    async def all(self) -> list[dict]:
        return await self.db[TOURNAMENT_COLL].find(
            {}, {"_id": 0}).to_list(2000)

    # ── Signal ingestion (called from OddsApiGateway callbacks) ────
    async def mark_catalog_seen(self, sport_key: str, *,
                                  sport_group: Optional[str] = None,
                                  title: Optional[str] = None,
                                  active: Optional[bool] = None) -> None:
        now = _now()
        set_: dict[str, Any] = {
            "sport_key":            sport_key,
            "last_catalog_seen_at": now,
            "updated_at":           now,
        }
        if sport_group is not None:
            set_["sport_group"] = sport_group
        if title is not None:
            set_["title"] = title
        if active is not None:
            set_["active"] = bool(active)
        await self.db[TOURNAMENT_COLL].update_one(
            {"sport_key": sport_key},
            {"$set": set_,
             "$setOnInsert": {
                 "created_at": now, "consecutive_empty_checks": 0,
             }},
            upsert=True,
        )

    async def mark_events_seen(self, sport_key: str, *, count: int) -> None:
        now = _now()
        set_: dict[str, Any] = {
            "sport_key": sport_key,
            "last_successful_check_at": now,
            "updated_at": now,
            "consecutive_empty_checks": 0,
            # An event-seen signal unsuppresses immediately.
            "suppress_until": None,
        }
        if count > 0:
            set_["last_event_seen_at"] = now
        await self.db[TOURNAMENT_COLL].update_one(
            {"sport_key": sport_key},
            {"$set": set_,
             "$setOnInsert": {"created_at": now}},
            upsert=True,
        )

    async def mark_empty(self, sport_key: str, *,
                          failure_reason: str = "empty_response") -> None:
        now = _now()
        cur = await self.db[TOURNAMENT_COLL].find_one(
            {"sport_key": sport_key},
            {"consecutive_empty_checks": 1, "suppress_until": 1},
        )
        consec = int((cur or {}).get("consecutive_empty_checks", 0)) + 1
        suppress_seconds = 0
        if consec >= CONSECUTIVE_EMPTY_THRESHOLD:
            # exponential-ish back-off with a cap
            base = INITIAL_SUPPRESSION_SECONDS
            factor = 2 ** max(0, consec - CONSECUTIVE_EMPTY_THRESHOLD)
            suppress_seconds = min(base * factor, MAX_SUPPRESSION_SECONDS)
        set_: dict[str, Any] = {
            "sport_key":              sport_key,
            "last_empty_check_at":    now,
            "consecutive_empty_checks": consec,
            "failure_reason":         failure_reason,
            "updated_at":             now,
        }
        if suppress_seconds:
            set_["suppress_until"] = now + timedelta(seconds=suppress_seconds)
        await self.db[TOURNAMENT_COLL].update_one(
            {"sport_key": sport_key},
            {"$set": set_,
             "$setOnInsert": {"created_at": now}},
            upsert=True,
        )
        if suppress_seconds:
            logger.info(
                "tournament_registry suppress %s for %ds (empties=%d)",
                sport_key, suppress_seconds, consec,
            )

    async def refresh_current_pick_presence(self, db_picks_collection: str = "picks") -> int:
        """Mark ``present_in_current_picks`` on every registry entry
        whose ``sport_key`` currently appears in today's picks.
        Called periodically so the eligibility bypass tracks reality.
        Returns count of keys marked present."""
        now = _now()
        today = now.strftime("%Y-%m-%d")
        # Distinct sport_key values from today's slate.
        keys_today = await self.db[db_picks_collection].distinct(
            "sport_key", {"pick_date": today})
        keys_today = [k for k in keys_today if k]
        # Reset flag first (bulk), then set on the current set.
        await self.db[TOURNAMENT_COLL].update_many(
            {"present_in_current_picks": True},
            {"$set": {"present_in_current_picks": False,
                       "updated_at": now}},
        )
        if not keys_today:
            return 0
        res = await self.db[TOURNAMENT_COLL].update_many(
            {"sport_key": {"$in": keys_today}},
            {"$set": {"present_in_current_picks": True,
                       "updated_at": now}},
        )
        return int(res.modified_count)


__all__ = [
    "TournamentRegistry", "TOURNAMENT_COLL",
    "MIN_SUPPRESSION_SECONDS", "INITIAL_SUPPRESSION_SECONDS",
    "MAX_SUPPRESSION_SECONDS", "CONSECUTIVE_EMPTY_THRESHOLD",
]
