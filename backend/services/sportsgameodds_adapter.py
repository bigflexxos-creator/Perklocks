"""SportsGameOdds primary-provider adapter (SGO).

Purpose
-------
Serve as PRIMARY real-odds source for the 6-day SportsGameOdds Pro trial
while The Odds API remains OUT_OF_USAGE_CREDITS.  Feeds the EXISTING
canonical Perklocks pipeline — this module never runs its own scoring,
models, or Lock Scores; it only produces normalized `db.picks` rows
whose downstream canonical publication path is identical to every
other real-line writer.

Design goals (surgical)
-----------------------
1.  ONE shared adapter (this module). Every sport goes through the same
    normalizer.
2.  Isolated from The Odds API circuit breaker — a 401
    OUT_OF_USAGE_CREDITS on the primary provider must not disable SGO.
3.  Preserves the existing real-line contract (odds_source=real_book_line
    equivalent), integrity gates, publication rules, and settlement.
4.  No Lock-Score work, no synthetic conversion, no model changes.

Contract
--------
- Access the key via ``os.environ.get("SPORTSGAMEODDS_API_KEY")`` — never
  logged, never printed, never mirrored to the frontend.
- All outbound requests carry ``x-api-key`` + a real ``User-Agent``
  (Cloudflare in front of SGO rejects header-less requests with 1010).
- Every persisted row carries::

      odds_source          = "real_book_line"
      odds_provider        = "sportsgameodds"
      no_real_book_line    = False
      is_model_only        = False
      synthetic            = False

  so ``services.board_visibility.compute_off_board`` and the main-board
  eligibility gate treat SGO rows exactly like any other real line.

- Provider-specific caps present in The Odds API path (odds_api_gateway
  budget throttle, top-1 scorer cap, request-budget breaker) are NOT
  invoked here — SGO calls do not touch that gateway.

Public API
----------
- ``fetch_events(league_id: str, limit: int = 100) -> list[dict]``
- ``fetch_event_with_odds(event_id: str) -> dict | None``
- ``normalize_event(ev: dict) -> list[dict]``   # canonical picks
- ``ingest_league(db, league_id, sport, limit=25, only_pregame=True)``
- ``ingest_all_configured(db, only_pregame=True)``
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Optional
from uuid import uuid5, NAMESPACE_URL

import httpx

logger = logging.getLogger("lockscore.sportsgameodds")

_BASE = "https://api.sportsgameodds.com/v2"
_TIMEOUT = httpx.Timeout(25.0, connect=8.0)
_UA = "Perklocks-Backend/1.0"
_MAX_CONCURRENCY = 4
_SEMAPHORE = asyncio.Semaphore(_MAX_CONCURRENCY)

# Perklocks league configuration.  Only leagues Perklocks already models
# are enabled here; the underlying canonical pipeline decides what to
# publish downstream.
_ENABLED_LEAGUES: list[tuple[str, str]] = [
    # (SGO leagueID, Perklocks canonical sport label)
    ("MLB",           "MLB"),
    ("EPL",           "Soccer"),
    ("LALIGA",        "Soccer"),
    ("MLS",           "Soccer"),
    ("SERIEA",        "Soccer"),
    ("BUNDESLIGA",    "Soccer"),
    ("LIGUE1",        "Soccer"),
    ("ATP",           "Tennis"),
    ("WTA",           "Tennis"),
]

# Market taxonomy — maps SGO (statID, betTypeID, periodID) triples to a
# canonical Perklocks market label. ONLY markets the existing pipeline
# already understands. Anything not in this table is silently ignored
# (never fabricated, never emitted as synthetic).
_MARKET_MAP_MLB: dict[tuple[str, str, str], str] = {
    ("points", "ml",     "game"): "Moneyline",
    ("points", "sp",     "game"): "Run Line",
    ("points", "ou",     "game"): "Total Runs",
    # MLB player props (statEntityID = <playerID>).
    ("batting_hits",           "ou", "game"): "Hits",
    ("batting_totalBases",     "ou", "game"): "Total Bases",
    ("batting_hits+runs+rbi",  "ou", "game"): "Hits + Runs + RBIs",
    ("batting_RBI",            "ou", "game"): "RBIs",
    ("batting_homeRuns",       "ou", "game"): "Home Runs",
    ("batting_homeRuns",       "yn", "game"): "Home Run",
    ("pitching_strikeouts",    "ou", "game"): "Strikeouts",
    ("pitching_outs",          "ou", "game"): "Outs Recorded",
}
_MARKET_MAP_SOCCER: dict[tuple[str, str, str], str] = {
    ("points", "ml3way", "reg"):  "Match Result (1X2)",
    ("points", "ml3way", "game"): "Match Result (1X2)",
    ("points", "ou",     "reg"):  "Total Goals",
    ("points", "ou",     "game"): "Total Goals",
    ("points", "sp",     "reg"):  "Asian Handicap",
    ("points", "sp",     "game"): "Asian Handicap",
    ("points", "yn",     "reg"):  "Both Teams To Score",
    # Player props
    ("goals",           "yn", "reg"):  "Anytime Goal Scorer",
    ("goals",           "yn", "game"): "Anytime Goal Scorer",
    ("goals+assists",   "yn", "reg"):  "To Score or Assist",
    ("goals+assists",   "yn", "game"): "To Score or Assist",
    ("assists",         "yn", "reg"):  "Anytime Assist",
    ("shots",           "ou", "reg"):  "Shots",
    ("shotsOnTarget",   "ou", "reg"):  "Shots on Target",
}
_MARKET_MAP_TENNIS: dict[tuple[str, str, str], str] = {
    ("points", "ml",     "game"): "Moneyline",
    ("points", "sp",     "game"): "Set Spread",
    ("points", "ou",     "game"): "Total Games",
    # Alternate lines share the same betTypeID here (SGO doesn't
    # separate them) — downstream tennis carve-outs already treat
    # additional totals/spreads as ALT lines.
}


def _api_key() -> str:
    k = os.environ.get("SPORTSGAMEODDS_API_KEY") or ""
    return k.strip()


def _headers() -> dict[str, str]:
    return {
        "x-api-key":   _api_key(),
        "User-Agent":  _UA,
        "Accept":      "application/json",
    }


async def _get(client: httpx.AsyncClient, path: str, **params) -> Any:
    """Single-flight-safe GET.  Never logs the key or the raw header."""
    async with _SEMAPHORE:
        r = await client.get(f"{_BASE}{path}", params=params, timeout=_TIMEOUT)
    if r.status_code == 429:
        retry = int(r.headers.get("Retry-After") or "5")
        logger.warning("sportsgameodds 429; sleeping %ss", retry)
        await asyncio.sleep(min(retry, 30))
        return None
    if r.status_code >= 400:
        logger.warning("sportsgameodds %s %s → %s", path, params, r.status_code)
        return None
    return r.json()


async def fetch_events(league_id: str, *, limit: int = 100,
                          only_pregame: bool = True) -> list[dict]:
    """Fetch events for ``league_id``.  When ``only_pregame`` is True
    we filter out started/completed/cancelled events after retrieval
    (SGO does not currently expose a native pregame filter)."""
    if not _api_key():
        return []
    async with httpx.AsyncClient(headers=_headers()) as client:
        d = await _get(client, "/events",
                        leagueID=league_id,
                        oddsAvailable="true",
                        limit=str(limit))
    if not d or not isinstance(d, dict):
        return []
    events = d.get("data") or []
    if only_pregame:
        events = [e for e in events if _is_pregame(e)]
    return events


def _is_pregame(ev: dict) -> bool:
    st = ev.get("status") or {}
    return (not st.get("started")) and (not st.get("completed")) \
        and (not st.get("cancelled")) and (not st.get("ended"))


def _to_int_odds(s: Any) -> Optional[int]:
    """SGO returns American odds as strings like ``"+205"`` / ``"-138"``."""
    if s is None:
        return None
    try:
        return int(str(s).replace("+", "").strip())
    except (TypeError, ValueError):
        return None


def _implied_from_american(odds: int) -> Optional[float]:
    if odds is None:
        return None
    if odds < 0:
        return (-odds) / ((-odds) + 100.0)
    return 100.0 / (odds + 100.0)


def _classify_sport(league_id: str) -> str:
    for lg, sp in _ENABLED_LEAGUES:
        if lg == league_id:
            return sp
    return "Unknown"


def _market_map(sport: str) -> dict[tuple[str, str, str], str]:
    if sport == "MLB":     return _MARKET_MAP_MLB
    if sport == "Soccer":  return _MARKET_MAP_SOCCER
    if sport == "Tennis":  return _MARKET_MAP_TENNIS
    return {}


def _side_label(o: dict, teams: dict, players: dict) -> Optional[str]:
    """Return the human-readable selection label for the outcome."""
    side = (o.get("sideID") or "").lower()
    sent = o.get("statEntityID") or ""
    bet_type = (o.get("betTypeID") or "").lower()

    if bet_type in ("ou",):
        line = o.get("bookOverUnder") or o.get("fairOverUnder")
        if side == "over":
            return f"Over {line}" if line is not None else "Over"
        if side == "under":
            return f"Under {line}" if line is not None else "Under"
    if bet_type == "sp":
        line = o.get("bookSpread") or o.get("fairSpread")
        which = teams.get(side, {}) if side in ("home", "away") else None
        team_name = (which or {}).get("names", {}).get("long") if which else None
        if team_name and line is not None:
            return f"{team_name} {line}"
        if team_name:
            return team_name
    if bet_type in ("ml", "ml3way"):
        which = teams.get(side, {}) if side in ("home", "away") else None
        if which:
            return which.get("names", {}).get("long")
        if side == "draw":
            return "Draw"
    if bet_type == "yn":
        # Player YN prop — selection is the player + Yes/No.
        pl = players.get(sent) or {}
        pname = pl.get("name") or f"{pl.get('firstName','')} {pl.get('lastName','')}".strip()
        if pname:
            return f"{pname} {'Yes' if side == 'yes' else 'No'}"
        return "Yes" if side == "yes" else "No"
    return None


def _stable_pick_id(sport: str, event_id: str, oid: str) -> str:
    key = f"sgo|{sport}|{event_id}|{oid}"
    return f"sgo-{uuid5(NAMESPACE_URL, key)}"


def normalize_event(ev: dict) -> list[dict]:
    """Produce canonical Perklocks pick rows from a single SGO event.

    Only outcomes whose ``(statID, betTypeID, periodID)`` is present in
    the enabled market map are emitted. Every row satisfies the
    real-line contract (real numeric ``book_odds``, valid identity,
    ``odds_source='real_book_line'``, ``odds_provider='sportsgameodds'``).
    """
    if not isinstance(ev, dict):
        return []
    league_id = ev.get("leagueID") or ""
    sport = _classify_sport(league_id)
    mm = _market_map(sport)
    if not mm:
        return []

    teams   = ev.get("teams")   or {}
    players = ev.get("players") or {}
    event_id = ev.get("eventID") or ""
    status = ev.get("status") or {}
    event_time = status.get("startsAt") or ev.get("commenceTime")
    if not event_id or not event_time:
        return []

    home = (teams.get("home") or {}).get("names", {}).get("long")
    away = (teams.get("away") or {}).get("names", {}).get("long")
    if not home or not away:
        return []

    league_display = ev.get("leagueID") or league_id
    now_iso = datetime.now(timezone.utc).isoformat()
    picks: list[dict] = []

    for oid, o in (ev.get("odds") or {}).items():
        if not isinstance(o, dict):
            continue
        # ── Filter for enabled markets ──────────────────────────
        stat_id  = o.get("statID") or ""
        bet_type = o.get("betTypeID") or ""
        period   = o.get("periodID") or ""
        canonical_market = mm.get((stat_id, bet_type, period))
        if not canonical_market:
            continue
        # Must have real book odds — SGO's `bookOdds` is authoritative.
        book_odds = _to_int_odds(o.get("bookOdds"))
        if book_odds is None:
            continue

        # Line (for OU / spread) — carry both possible fields.
        line: Optional[float] = None
        for k in ("bookOverUnder", "fairOverUnder",
                  "bookSpread", "fairSpread"):
            v = o.get(k)
            if v is not None:
                try:
                    line = float(v)
                    break
                except (TypeError, ValueError):
                    pass

        # Selection label.
        selection = _side_label(o, teams, players)
        if not selection:
            continue

        # Player identity (for player props only).
        sent = o.get("statEntityID") or ""
        player_name: Optional[str] = None
        player_team: Optional[str] = None
        if sent in players:
            pl = players[sent]
            player_name = pl.get("name") \
                or f"{pl.get('firstName','')} {pl.get('lastName','')}".strip()
            tid = pl.get("teamID")
            # Resolve player_team to canonical team name via the event's teams block.
            for side_key, side_val in teams.items():
                if not isinstance(side_val, dict):
                    continue
                if side_val.get("teamID") == tid:
                    player_team = side_val.get("names", {}).get("long")
                    break
        # Best available bookmaker for provenance.
        bms = list((o.get("byBookmaker") or {}).keys())
        bookmaker = bms[0] if bms else "consensus"
        provider_ts = None
        try:
            provider_ts = (o.get("byBookmaker", {}).get(bookmaker) or {}
                          ).get("lastUpdatedAt")
        except Exception:
            provider_ts = None

        implied = _implied_from_american(book_odds)
        pick_id = _stable_pick_id(sport, event_id, oid)

        pick = {
            "id":                       pick_id,
            "sport":                    sport,
            "league":                   league_display,
            "sport_key":                league_id,
            "event":                    f"{away} @ {home}",
            "event_id":                 event_id,
            "provider_event_id":        event_id,
            "home_team":                home,
            "away_team":                away,
            "event_time":               event_time,
            "market":                   canonical_market,
            "market_key":               f"{stat_id}|{bet_type}|{period}",
            "selection":                selection,
            "line":                     line,
            "book_odds":                book_odds,
            "implied_probability":      round(implied, 4) if implied else None,
            "bookmaker":                bookmaker,
            "provider_timestamp":       provider_ts,
            # ── REAL-LINE CONTRACT (mandatory) ───────────────
            "odds_source":              "real_book_line",
            "odds_provider":            "sportsgameodds",
            "no_real_book_line":        False,
            "is_model_only":            False,
            "model_only":               False,
            "synthetic":                False,
            "is_synthetic_scorer":      False,
            # ── Identity (populated when applicable) ─────────
            "player_name":              player_name,
            "player_team":              player_team,
            "player_id":                sent if sent in players else None,
            # ── Ingest bookkeeping ───────────────────────────
            "source":                   "sportsgameodds_v2",
            "pick_date":                event_time[:10] if event_time else None,
            "ingested_at":              now_iso,
            "publication_state":        None,  # canonical publisher owns this
        }
        picks.append(pick)
    return picks


async def ingest_league(db, sgo_league_id: str, sport: str, *,
                          limit: int = 25, only_pregame: bool = True) -> dict:
    """Fetch → normalize → upsert.  Idempotent by stable ``id``.

    Returns counts: ``{"events": …, "picks_seen": …, "picks_upserted": …}``.
    Never lowers Lock Scores, never publishes; canonical publisher runs
    on its normal cadence.
    """
    events = await fetch_events(sgo_league_id, limit=limit,
                                  only_pregame=only_pregame)
    counts = {"events": len(events), "picks_seen": 0, "picks_upserted": 0}
    if not events:
        return counts
    for ev in events:
        rows = normalize_event(ev)
        counts["picks_seen"] += len(rows)
        for row in rows:
            try:
                res = await db.picks.update_one(
                    {"id": row["id"]},
                    {"$set": row,
                     "$setOnInsert": {"created_at": row["ingested_at"]}},
                    upsert=True,
                )
                if res.upserted_id is not None or res.modified_count > 0:
                    counts["picks_upserted"] += 1
            except Exception as _err:
                # Never break the batch on a single-row DB hiccup.
                logger.debug("sgo upsert skip %s: %s", row.get("id"), _err)
    return counts


async def ingest_all_configured(db, *, only_pregame: bool = True) -> dict:
    """Cycle every enabled league. Independent of The Odds API state."""
    totals = {"events": 0, "picks_seen": 0, "picks_upserted": 0,
              "by_league": {}}
    for lg, sport in _ENABLED_LEAGUES:
        try:
            c = await ingest_league(db, lg, sport,
                                       limit=25,
                                       only_pregame=only_pregame)
        except Exception as _err:
            logger.warning("sgo league %s failed: %s", lg, _err)
            c = {"events": 0, "picks_seen": 0, "picks_upserted": 0,
                 "error": str(_err)[:200]}
        totals["by_league"][lg] = c
        totals["events"]         += c.get("events", 0)
        totals["picks_seen"]     += c.get("picks_seen", 0)
        totals["picks_upserted"] += c.get("picks_upserted", 0)
    return totals


# ── Small helper for admin-triggered on-demand fetch ────────────────
async def health_ping() -> bool:
    """Cheap authenticated probe used by /api/admin/provider-health."""
    if not _api_key():
        return False
    async with httpx.AsyncClient(headers=_headers()) as client:
        d = await _get(client, "/sports")
    return isinstance(d, dict) and d.get("success") is True
