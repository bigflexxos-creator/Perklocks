"""SportsGameOdds primary-provider adapter (SGO).

Purpose
-------
Serve as PRIMARY real-odds source for the 6-day SportsGameOdds Pro trial
while The Odds API remains OUT_OF_USAGE_CREDITS.  Feeds the EXISTING
canonical Perklocks pipeline — this module never runs its own scoring,
models, or Lock Scores; it only produces normalized `db.picks` rows
whose downstream canonical publication path is identical to every
other real-line writer.

CANONICAL INPUT CONTRACT (2026-06 revision)
-------------------------------------------
After the initial activation pass, comparison against known-good
``real_line_soccer_v2`` rows revealed two divergences that were
collapsing Lock-Score distributions on SGO rows:

1. **Player-entity oddIDs masquerading as game markets.** SGO
   serves ``points-<playerID>-…-ou-over`` outcomes (a player's
   "total points/goals in the match") alongside team ``points-all``
   ones.  The v1 adapter matched only on
   ``(statID, betTypeID, periodID)`` and emitted the player one as
   a Total Goals row with a bogus ``player_name`` attached to a
   game market.  This is now gated on ``statEntityID`` — game
   markets are only emitted when the entity is ``all`` / ``home``
   / ``away`` / ``draw``.

2. **Missing canonical routing fields.** Downstream models
   (``soccer_game_model``, MLB pitcher/hitter, tennis) require the
   fields set by the Odds-API-family real-line writers:
   ``market_family``, ``market_key`` (canonical: ``totals`` /
   ``moneyline`` / ``spreads`` / ``player_prop``), ``provider_selection``,
   ``side``, ``line_source``, ``no_bet=False``,
   ``no_real_book_line=False``, ``odds_status='book_line_present'``,
   and a market label that embeds the line
   (``"Total Goals Under 3.5"`` — not ``"Total Goals"``).  Rows
   missing these fields fall into default/fallback score paths
   with degraded Lock Scores.  All are now emitted.

3. **Bookmaker provenance.** ``bookmaker`` is now selected from
   ``byBookmaker`` using an established best-line preference list
   (fanduel > draftkings > betmgm > caesars > pointsbet > bet365 …).
   ``consensus`` is used only when no named book is present.

Design goals (surgical, unchanged)
----------------------------------
1.  ONE shared adapter (this module). Every sport goes through the
    same normalizer.
2.  Isolated from The Odds API circuit breaker.
3.  Preserves the existing real-line contract, integrity gates,
    publication rules, and settlement.
4.  No Lock-Score work, no synthetic conversion, no model changes,
    no predictive-formula edits.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid5, NAMESPACE_URL

import httpx

logger = logging.getLogger("lockscore.sportsgameodds")

_BASE = "https://api.sportsgameodds.com/v2"
_TIMEOUT = httpx.Timeout(25.0, connect=8.0)
_UA = "Perklocks-Backend/1.0"
_MAX_CONCURRENCY = 4
_SEMAPHORE = asyncio.Semaphore(_MAX_CONCURRENCY)

# Sports/leagues to poll on each cycle. Order irrelevant — every league
# is processed independently by ``ingest_all_configured``.
_ENABLED_LEAGUES: list[tuple[str, str]] = [
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

# ── Canonical market taxonomy ───────────────────────────────────────
# Each entry:
#     (statID, betTypeID, periodID) →
#         (canonical_market_label, canonical_market_key, market_family)
#
# ``canonical_market_key`` MUST match the values the existing sport
# models dispatch on (``moneyline``, ``totals``, ``spreads``,
# ``player_prop``). ``market_family`` MUST match the buckets the
# canonical publication service uses.

_MARKET_MAP_MLB_GAME: dict[tuple[str, str, str], tuple[str, str, str]] = {
    ("points", "ml",     "game"): ("Moneyline",       "moneyline", "game_market"),
    ("points", "sp",     "game"): ("Run Line",        "spreads",   "game_market"),
    ("points", "ou",     "game"): ("Total Runs",      "totals",    "game_market"),
}
_MARKET_MAP_MLB_PLAYER: dict[tuple[str, str, str], tuple[str, str, str]] = {
    ("batting_hits",           "ou", "game"): ("Hits",              "player_prop", "player_prop"),
    ("batting_totalBases",     "ou", "game"): ("Total Bases",       "player_prop", "player_prop"),
    ("batting_hits+runs+rbi",  "ou", "game"): ("Hits + Runs + RBIs","player_prop", "player_prop"),
    ("batting_RBI",            "ou", "game"): ("RBIs",              "player_prop", "player_prop"),
    ("batting_homeRuns",       "ou", "game"): ("Home Runs",         "player_prop", "player_prop"),
    ("batting_homeRuns",       "yn", "game"): ("Home Run",          "player_prop", "player_prop"),
    ("pitching_strikeouts",    "ou", "game"): ("Strikeouts",        "player_prop", "player_prop"),
    ("pitching_outs",          "ou", "game"): ("Outs Recorded",     "player_prop", "player_prop"),
}
_MARKET_MAP_SOCCER_GAME: dict[tuple[str, str, str], tuple[str, str, str]] = {
    ("points", "ml3way", "reg"):  ("Match Result (1X2)", "h2h",     "game_market"),
    ("points", "ml3way", "game"): ("Match Result (1X2)", "h2h",     "game_market"),
    ("points", "ou",     "reg"):  ("Total Goals",        "totals",  "game_market"),
    ("points", "ou",     "game"): ("Total Goals",        "totals",  "game_market"),
    ("points", "sp",     "reg"):  ("Asian Handicap",     "spreads", "game_market"),
    ("points", "sp",     "game"): ("Asian Handicap",     "spreads", "game_market"),
    ("points", "yn",     "reg"):  ("Both Teams To Score","btts",    "game_market"),
}
_MARKET_MAP_SOCCER_PLAYER: dict[tuple[str, str, str], tuple[str, str, str]] = {
    ("goals",           "yn", "reg"):  ("Anytime Goal Scorer",  "player_prop", "player_prop"),
    ("goals",           "yn", "game"): ("Anytime Goal Scorer",  "player_prop", "player_prop"),
    ("goals+assists",   "yn", "reg"):  ("To Score or Assist",   "player_prop", "player_prop"),
    ("goals+assists",   "yn", "game"): ("To Score or Assist",   "player_prop", "player_prop"),
    ("assists",         "yn", "reg"):  ("Anytime Assist",       "player_prop", "player_prop"),
    ("shots",           "ou", "reg"):  ("Shots",                "player_prop", "player_prop"),
    ("shotsOnTarget",   "ou", "reg"):  ("Shots on Target",      "player_prop", "player_prop"),
}
_MARKET_MAP_TENNIS_GAME: dict[tuple[str, str, str], tuple[str, str, str]] = {
    ("points", "ml",     "game"): ("Moneyline",   "moneyline", "game_market"),
    ("points", "sp",     "game"): ("Set Spread",  "spreads",   "game_market"),
    ("points", "ou",     "game"): ("Total Games", "totals",    "game_market"),
}

# Best-line bookmaker preference (canonical Perklocks order).
_BOOKMAKER_PREFERENCE = (
    "fanduel", "draftkings", "betmgm", "caesars", "pointsbet",
    "bet365", "betrivers", "espnbet", "fanatics", "hardrock",
    "unibet_us", "bovada", "wynnbet", "circa", "prophetexchange",
)


def _api_key() -> str:
    return (os.environ.get("SPORTSGAMEODDS_API_KEY") or "").strip()


def _headers() -> dict[str, str]:
    return {"x-api-key": _api_key(), "User-Agent": _UA,
             "Accept": "application/json"}


async def _get(client: httpx.AsyncClient, path: str, **params) -> Any:
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
    if not _api_key():
        return []
    async with httpx.AsyncClient(headers=_headers()) as client:
        d = await _get(client, "/events", leagueID=league_id,
                        oddsAvailable="true", limit=str(limit))
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


def _lookup_market(sport: str, stat_id: str, bet_type: str, period: str,
                     stat_entity: str
                    ) -> Optional[tuple[str, str, str, bool]]:
    """Return ``(canonical_label, canonical_key, market_family, is_player)``
    or ``None`` when this SGO outcome should not be emitted.

    The critical gate: outcomes whose ``statEntityID`` is a playerID may
    ONLY resolve against the player-market submap. Outcomes whose
    entity is ``all`` / ``home`` / ``away`` / ``draw`` may ONLY resolve
    against the game-market submap. This closes the v1 bug where a
    ``points-<playerID>-…-ou-over`` was routed to ``Total Goals``.
    """
    key = (stat_id, bet_type, period)
    is_team_entity = stat_entity in ("all", "home", "away", "draw")
    is_player_entity = not is_team_entity and bool(stat_entity)

    if sport == "MLB":
        if is_team_entity and key in _MARKET_MAP_MLB_GAME:
            lbl, ck, fam = _MARKET_MAP_MLB_GAME[key]
            return lbl, ck, fam, False
        if is_player_entity and key in _MARKET_MAP_MLB_PLAYER:
            lbl, ck, fam = _MARKET_MAP_MLB_PLAYER[key]
            return lbl, ck, fam, True
        return None
    if sport == "Soccer":
        if is_team_entity and key in _MARKET_MAP_SOCCER_GAME:
            lbl, ck, fam = _MARKET_MAP_SOCCER_GAME[key]
            return lbl, ck, fam, False
        if is_player_entity and key in _MARKET_MAP_SOCCER_PLAYER:
            lbl, ck, fam = _MARKET_MAP_SOCCER_PLAYER[key]
            return lbl, ck, fam, True
        return None
    if sport == "Tennis":
        if is_team_entity and key in _MARKET_MAP_TENNIS_GAME:
            lbl, ck, fam = _MARKET_MAP_TENNIS_GAME[key]
            return lbl, ck, fam, False
        return None
    return None


def _select_best_bookmaker(o: dict) -> tuple[Optional[str], Optional[int],
                                                    Optional[str]]:
    """Pick a named bookmaker from ``byBookmaker`` following the
    canonical preference order. Returns ``(bookmaker, book_odds,
    last_updated_at)``. Falls back to consensus ``bookOdds`` only
    when no named book is available.
    """
    by = o.get("byBookmaker") or {}
    for name in _BOOKMAKER_PREFERENCE:
        entry = by.get(name)
        if isinstance(entry, dict) and entry.get("available") \
                and entry.get("odds") is not None:
            oi = _to_int_odds(entry.get("odds"))
            if oi is not None:
                return name, oi, entry.get("lastUpdatedAt")
    # No preferred book — take any available named book.
    for name, entry in by.items():
        if isinstance(entry, dict) and entry.get("available") \
                and entry.get("odds") is not None:
            oi = _to_int_odds(entry.get("odds"))
            if oi is not None:
                return name, oi, entry.get("lastUpdatedAt")
    # Last-resort: consensus bookOdds (aggregate). Marked as such by
    # the caller via ``bookmaker='consensus'``.
    return None, None, None


def _side_and_selection(o: dict, teams: dict, players: dict,
                         canonical_label: str,
                         canonical_key: str) -> tuple[str, Optional[str],
                                                       Optional[float],
                                                       Optional[str]]:
    """Return ``(selection, side, line, market_label_with_line)``."""
    side_raw = (o.get("sideID") or "").lower()
    sent = o.get("statEntityID") or ""
    bet_type = (o.get("betTypeID") or "").lower()

    # Line — real numeric extraction (ou.bookOverUnder / sp.bookSpread).
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

    market_label = canonical_label
    selection: Optional[str] = None

    if bet_type == "ou":
        if side_raw == "over":
            selection = f"Over {line}" if line is not None else "Over"
        elif side_raw == "under":
            selection = f"Under {line}" if line is not None else "Under"
        # Embed line in market label (matches canonical writers).
        if line is not None:
            market_label = f"{canonical_label} {selection}"
    elif bet_type == "sp":
        which = teams.get(side_raw) if side_raw in ("home", "away") else None
        team_name = (which or {}).get("names", {}).get("long") if which else None
        if team_name and line is not None:
            selection = f"{team_name} {line:+g}"
            market_label = f"{canonical_label} {team_name} {line:+g}"
        elif team_name:
            selection = team_name
    elif bet_type in ("ml", "ml3way"):
        which = teams.get(side_raw) if side_raw in ("home", "away") else None
        if which:
            selection = which.get("names", {}).get("long")
        elif side_raw == "draw":
            selection = "Draw"
    elif bet_type == "yn":
        pl = players.get(sent) or {}
        pname = pl.get("name") \
            or f"{pl.get('firstName','')} {pl.get('lastName','')}".strip()
        if pname:
            selection = f"{pname} {'Yes' if side_raw == 'yes' else 'No'}"
            market_label = f"{canonical_label} - {pname}"
        else:
            selection = "Yes" if side_raw == "yes" else "No"

    return (selection or "", side_raw or None, line, market_label)


def _stable_pick_id(sport: str, event_id: str, oid: str) -> str:
    key = f"sgo|{sport}|{event_id}|{oid}"
    return f"sgo-{uuid5(NAMESPACE_URL, key)}"


def normalize_event(ev: dict) -> list[dict]:
    """Produce canonical Perklocks pick rows from a single SGO event.

    Every emitted row satisfies the real-line contract AND the
    canonical model-input contract (see module docstring). No
    predictive formula is invoked here — this is a pure normalizer.
    """
    if not isinstance(ev, dict):
        return []
    league_id = ev.get("leagueID") or ""
    sport = _classify_sport(league_id)
    if sport == "Unknown":
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

    league_display = league_id
    now_iso = datetime.now(timezone.utc).isoformat()
    picks: list[dict] = []

    for oid, o in (ev.get("odds") or {}).items():
        if not isinstance(o, dict):
            continue
        stat_id     = o.get("statID") or ""
        bet_type    = o.get("betTypeID") or ""
        period      = o.get("periodID") or ""
        stat_entity = o.get("statEntityID") or ""

        market_info = _lookup_market(sport, stat_id, bet_type, period,
                                        stat_entity)
        if not market_info:
            continue
        canonical_label, canonical_key, market_family, is_player = market_info

        # Real book price via canonical bookmaker preference. Fall back
        # to consensus ``bookOdds`` only when no named book available.
        bm_name, bm_odds, bm_ts = _select_best_bookmaker(o)
        book_odds = bm_odds if bm_odds is not None \
            else _to_int_odds(o.get("bookOdds"))
        if book_odds is None:
            continue
        bookmaker = bm_name if bm_name else "consensus"

        selection, side, line, market_label = _side_and_selection(
            o, teams, players, canonical_label, canonical_key,
        )
        if not selection:
            continue

        # Player identity — ONLY for player markets.
        player_name: Optional[str] = None
        player_team: Optional[str] = None
        player_id_out: Optional[str] = None
        if is_player and stat_entity in players:
            pl = players[stat_entity]
            player_id_out = stat_entity
            player_name = pl.get("name") \
                or f"{pl.get('firstName','')} {pl.get('lastName','')}".strip()
            tid = pl.get("teamID")
            for _, side_val in teams.items():
                if isinstance(side_val, dict) and side_val.get("teamID") == tid:
                    player_team = side_val.get("names", {}).get("long")
                    break

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
            "commence_time":            event_time,
            "commence_time_utc":        event_time,
            # ── Canonical market routing (matches real_line_soccer_v2 shape) ──
            "market":                   market_label,
            "market_key":               canonical_key,
            "market_family":            market_family,
            "provider_market_key":      canonical_key,
            "provider_selection":       "Under" if side == "under" else
                                          "Over" if side == "over"  else
                                          selection,
            "selection":                selection,
            "side":                     side,
            "line":                     line,
            "provider_line":            line,
            "line_source":              "sgo_provider",
            "book_odds":                book_odds,
            "implied_probability":      round((implied or 0) * 100, 3)
                                          if implied is not None else None,
            "bookmaker":                bookmaker,
            "provider_timestamp":       bm_ts or None,
            # ── REAL-LINE CONTRACT (mandatory) ───────────────────────
            "odds_source":              "real_book_line",
            "odds_provider":            "sportsgameodds",
            "odds_status":              "book_line_present",
            "no_real_book_line":        False,
            "is_model_only":            False,
            "model_only":               False,
            "synthetic":                False,
            "is_synthetic_scorer":      False,
            "no_bet":                   False,
            # ── Identity ─────────────────────────────────────────────
            "player_name":              player_name,
            "player_team":              player_team,
            "player_id":                player_id_out,
            # ── Ingest bookkeeping ───────────────────────────────────
            "source":                   "sportsgameodds_v2",
            "publication_source":       "sportsgameodds_v2",
            "provenance":               "sportsgameodds_v2",
            "pick_date":                event_time[:10] if event_time else None,
            "ingested_at":              now_iso,
            "publication_state":        None,
        }
        picks.append(pick)
    return picks


async def ingest_league(db, sgo_league_id: str, sport: str, *,
                          limit: int = 25, only_pregame: bool = True) -> dict:
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
                logger.debug("sgo upsert skip %s: %s", row.get("id"), _err)
    return counts


async def ingest_all_configured(db, *, only_pregame: bool = True) -> dict:
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


async def health_ping() -> bool:
    if not _api_key():
        return False
    async with httpx.AsyncClient(headers=_headers()) as client:
        d = await _get(client, "/sports")
    return isinstance(d, dict) and d.get("success") is True
