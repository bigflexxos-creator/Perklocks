"""Perklocks — Historical Freshness Contract (universal, sport-neutral).

ONE canonical module every Historical Intelligence adapter calls to
normalize:

    * ``season``          deterministic, derived from the event date
                          when the row stored null
    * ``season_type``     passthrough / normalized
    * ``event_date``      ISO ``YYYY-MM-DD`` from event_time/date
    * ``is_current_season``  truthful current-season flag
    * ``identity_candidates`` every canonical_player_id to probe so a
                              dual-pipeline split cannot hide rows

Contract invariants
-------------------

1. **Deterministic**.  Given the same (sport, event_time) the derived
   season is always the same.  Never random, never network.

2. **Data-only**.  We ONLY compute from fields already present on the
   row (or on the ``now`` clock for ``current_season``).  No row
   gets a fabricated season.  If the event date is itself missing
   we return ``None`` — HONEST ZERO.

3. **Universal**.  Every supported sport uses the same entry points.
   Sport-specific calendars live in a single table in this module;
   no sport-specific hacks allowed elsewhere.

4. **Identity bridge**.  The resolver returns the full set of
   ``canonical_player_id`` values that have ever written rows under
   the pick's player name — closes the dual-pipeline split
   (NFL gsis + ESPN, Tennis provider swaps, MLB rebranding).

5. **Soft-fail**.  Every helper traps exceptions and either returns
   an honest ``None`` / empty set, or falls back to the caller's
   input unchanged.  Freshness repair must never turn a working
   adapter into a silent zero.
"""
from __future__ import annotations

import datetime as _dt
import logging as _logging
from typing import Any, Iterable, Optional

__all__ = [
    "derive_season",
    "current_season",
    "normalize_context",
    "identity_candidates",
    "sort_observations_newest_first",
    "SEASON_CALENDARS",
]

logger = _logging.getLogger("hi.freshness")


# ────────────────────────────────────────────────────────────────────
# Season calendars — one source of truth, per sport
# ────────────────────────────────────────────────────────────────────

# For each sport: ``start_month`` is the first calendar-month of the
# season Y.  Any event at or after ``start_month`` of year Y and
# before ``start_month`` of year Y+1 belongs to season Y.  A sport
# whose season fits inside a single calendar year uses the trivial
# calendar (``start_month = 1``).
SEASON_CALENDARS: dict[str, dict[str, Any]] = {
    "nfl":    {"start_month":  8},   # Aug Y → Feb Y+1
    "cfb":    {"start_month":  8},   # Aug Y → Jan Y+1
    "nhl":    {"start_month": 10},   # Oct Y → Jun Y+1
    "nba":    {"start_month": 10},   # Oct Y → Jun Y+1
    "mlb":    {"start_month":  3},   # Mar Y → Nov Y   (fits inside Y)
    "soccer": {"start_month":  8},   # Aug Y → May Y+1 (European default)
    "tennis": {"start_month":  1},   # calendar year
    "ufc":    {"start_month":  1},
    "mma":    {"start_month":  1},
}

# Allowed sport aliases that normalize to a calendar key.
_ALIAS: dict[str, str] = {
    # canonical lowercase
    "nfl": "nfl", "nhl": "nhl", "nba": "nba", "mlb": "mlb", "cfb": "cfb",
    "soccer": "soccer", "tennis": "tennis", "ufc": "ufc", "mma": "mma",
    # friendly / UI
    "NFL": "nfl", "NHL": "nhl", "NBA": "nba", "MLB": "mlb", "CFB": "cfb",
    "Soccer": "soccer", "Tennis": "tennis", "UFC": "ufc", "MMA": "mma",
    # provider / abbreviations
    "hockey": "nhl", "ice_hockey": "nhl",
    "football": "nfl", "american_football": "nfl",
    "college_football": "cfb", "ncaaf": "cfb",
    "basketball": "nba", "ncaab": "nba",
    "baseball": "mlb",
    "football_soccer": "soccer", "futbol": "soccer",
}


def _canon_sport(sport: Any) -> Optional[str]:
    if sport is None:
        return None
    s = str(sport).strip()
    if not s:
        return None
    if s in _ALIAS:
        return _ALIAS[s]
    sl = s.lower()
    if sl in _ALIAS:
        return _ALIAS[sl]
    return sl if sl in SEASON_CALENDARS else None


# ────────────────────────────────────────────────────────────────────
# Date parsing (defensive — ISO strings, naive, with/without tz)
# ────────────────────────────────────────────────────────────────────

def _parse_date(value: Any) -> Optional[_dt.date]:
    """Return a ``datetime.date`` for any supported shape; else None.
    Accepts ``datetime``, ``date``, ISO-8601 strings with/without
    timezone, and ``YYYY-MM-DD`` prefixes."""
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    s = str(value).strip()
    if not s:
        return None
    # Fast path — plain YYYY-MM-DD or ISO prefix
    if len(s) >= 10 and s[4] == "-" and s[7] == "-" and \
       s[0:4].isdigit() and s[5:7].isdigit() and s[8:10].isdigit():
        try:
            return _dt.date(int(s[0:4]), int(s[5:7]), int(s[8:10]))
        except Exception:
            pass
    # Fallback — full ISO parse
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return _dt.datetime.fromisoformat(s).date()
    except Exception:
        return None


# ────────────────────────────────────────────────────────────────────
# Core helpers
# ────────────────────────────────────────────────────────────────────

def derive_season(sport: Any,
                   event_date: Any,
                   stored_season: Any = None) -> Optional[int]:
    """Return the authoritative season year for an event.

    Priority:

      1. Trust the stored season if it is a sane integer.
      2. Else compute from the event date using the sport's calendar.
      3. Else ``None`` (HONEST ZERO — do not guess).
    """
    # Trust an explicit stored value (int or int-coercible)
    if stored_season is not None:
        try:
            iv = int(stored_season)
            if 1900 <= iv <= 2200:
                return iv
        except Exception:
            pass
    sport_key = _canon_sport(sport)
    if sport_key is None:
        return None
    d = _parse_date(event_date)
    if d is None:
        return None
    start_month = SEASON_CALENDARS[sport_key]["start_month"]
    if start_month == 1:
        return d.year
    return d.year if d.month >= start_month else d.year - 1


def current_season(sport: Any,
                    now_utc: Optional[_dt.datetime] = None) -> Optional[int]:
    """Active season year for the sport right now.  Pure function of
    the clock + the calendar table — no DB, no network."""
    sport_key = _canon_sport(sport)
    if sport_key is None:
        return None
    now_utc = now_utc or _dt.datetime.utcnow()
    start_month = SEASON_CALENDARS[sport_key]["start_month"]
    if start_month == 1:
        return now_utc.year
    return now_utc.year if now_utc.month >= start_month else now_utc.year - 1


def normalize_context(sport: Any, raw: dict,
                       *,
                       now_utc: Optional[_dt.datetime] = None) -> dict:
    """Return a standard context dict every HI observation emits.

    Keys:
      ``season``             int | None (truthful, HONEST ZERO)
      ``season_type``        str | None (passthrough)
      ``event_date``         "YYYY-MM-DD" | None
      ``is_current_season``  bool
      ``source``             str | None (provenance passthrough)
    """
    if not isinstance(raw, dict):
        raw = {}
    sport_key = _canon_sport(sport)
    # Pick best event-date-ish field (adapters pass different shapes).
    date_val = (raw.get("event_time")
                or raw.get("event_date")
                or raw.get("game_date")
                or raw.get("match_date")
                or raw.get("date"))
    parsed = _parse_date(date_val)
    season = derive_season(sport_key, parsed, raw.get("season"))
    cur = current_season(sport_key, now_utc=now_utc)
    is_current = bool(season is not None and cur is not None and season == cur)
    return {
        "season":            season,
        "season_type":       raw.get("season_type"),
        "event_date":        parsed.isoformat() if parsed else None,
        "is_current_season": is_current,
        "source":            raw.get("source") or raw.get("provenance"),
    }


async def identity_candidates(db,
                                sport: Any,
                                entity_id: Any,
                                entity_name: Any,
                                *,
                                include_name_scan: bool = True
                                ) -> set[str]:
    """Return the full set of ``canonical_player_id`` values the HI
    query must probe to UNION every pipeline that writes under this
    player.

    Universal strategy, applied identically across sports:

      1. Seed with ``entity_id`` (pick's own stamped id).
      2. UNION every ``provider_ids.*`` value and ``canonical_player_id``
         on every matching ``player_identities`` doc (name scan).
      3. UNION with distinct ``canonical_player_id`` values already
         stored in ``player_game_actuals`` under the EXACT player
         name scoped to this sport.  Bounded — a scoped exact-name
         distinct is a tiny indexed query on a per-sport subset and
         no sport allows two concurrently-active players under one
         identical full name in a single roster.

    Soft-fail: on any exception returns the seed set so callers can
    always proceed with their original single-id query.
    """
    ids: set[str] = set()
    try:
        if entity_id is not None:
            s = str(entity_id).strip()
            if s:
                ids.add(s)
        sport_key = _canon_sport(sport)
        if not include_name_scan or not entity_name:
            return ids
        norm = str(entity_name).strip().lower().replace(".", "")
        if not norm:
            return ids
        # 2. player_identities (every doc that matches the name_norm)
        try:
            async for ident in db.player_identities.find(
                {"sport": _identity_sport_token(sport_key),
                 "name_norm": norm},
                {"provider_ids": 1, "canonical_player_id": 1},
            ):
                cp = ident.get("canonical_player_id")
                if cp:
                    ids.add(str(cp))
                for v in (ident.get("provider_ids") or {}).values():
                    if v:
                        ids.add(str(v))
        except Exception as e:
            logger.debug("freshness.identity: identities scan soft-fail (%s)", e)
        # 3. UNION with distinct canonical_player_id in actuals under
        #    the exact display name (bridges dual-pipeline splits).
        if sport_key and entity_name:
            try:
                distinct = await db.player_game_actuals.distinct(
                    "canonical_player_id",
                    {"sport": sport_key, "player_name": entity_name},
                )
                for d in distinct or ():
                    if d:
                        ids.add(str(d))
            except Exception as e:
                logger.debug("freshness.identity: distinct scan soft-fail (%s)", e)
    except Exception as e:  # pragma: no cover
        logger.debug("freshness.identity: outer soft-fail (%s)", e)
    return ids


def _identity_sport_token(sport_key: Optional[str]) -> str:
    """The ``player_identities`` collection stores the UI-cased sport
    label (``"NFL"``, ``"NHL"``, …).  Map the canonical lowercase
    sport key to that convention."""
    if not sport_key:
        return ""
    if sport_key == "soccer":
        return "Soccer"
    if sport_key == "tennis":
        return "Tennis"
    if sport_key == "ufc":
        return "UFC"
    if sport_key == "mma":
        return "MMA"
    return sport_key.upper()


def sort_observations_newest_first(docs: Iterable[dict]) -> list[dict]:
    """Deterministic newest-first ordering.  Prefers ``event_time``
    then ``event_date`` / ``date`` / ``match_date`` / ``game_date``.
    Rows with no usable date sink to the end (never silently drop)."""
    def _k(d: dict) -> str:
        return str(
            d.get("event_time")
            or d.get("event_date")
            or d.get("date")
            or d.get("match_date")
            or d.get("game_date")
            or ""
        )
    return sorted(list(docs), key=_k, reverse=True)
