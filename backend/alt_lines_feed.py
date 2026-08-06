"""Live alt-line feed (The Odds API) — replaces synthetic alt-line generation
with REAL sportsbook lines.

User mandate (2026-06-30):
  • Turn OFF synthetic alt-line generation entirely.
  • Display ONLY lines that exist on the live sportsbook board.
  • For every pick, validate (sportsbook, market_key, selection, line, price,
    last_seen) against the live feed.
  • If a book removes a line, the pick is auto-hidden via stale-detection.

Data model — collection `live_alt_lines`:
  {
    sport:            "soccer" | "mlb" | "nfl" | "tennis",
    odds_api_sport:   "soccer_fifa_world_cup",
    event_id:         "5a34afe2de99513ba48360b983cfe80c",  # Odds API event id
    event_name:       "Netherlands vs Morocco",
    home_team, away_team,
    commence_time:    ISO timestamp,
    sportsbook:       "draftkings" | "fanduel",
    market_key:       "player_goal_scorer_anytime" | "alternate_totals" | ...,
    selection:        "Lionel Messi" | "Over",
    selection_norm:   "lionel messi",  # for fuzzy match
    line:             0.5,             # the actual book line (null for binary)
    price:            -150,             # American odds
    last_seen:        ISO timestamp,    # when we last saw this from the API
    fetched_at:       ISO timestamp,
    market_id:        "evid:dk:player_goal_scorer_anytime:lionel-messi",
    selection_id:     ...same composite key...
  }

Markets fetched (Phase 1):
  Soccer:   player_goal_scorer_anytime, player_first_goal_scorer,
            player_to_score_or_assist, alternate_totals
  MLB:      batter_hits_alternate, batter_total_bases_alternate,
            pitcher_strikeouts_alternate, alternate_totals
  NFL:      player_pass_yds_alternate, player_rush_yds_alternate,
            player_anytime_td
  Tennis:   alternate_totals_games, alternate_spreads_games

Refresh cadence:
  • 10 min during pregame
  • 5 min within 1h of kickoff
  • TTL index on last_seen — rows older than 30 min auto-evict, so any
    pick referencing an evicted line will fail the validation gate and
    surface error_code = stale_odds.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger("lockscore.alt_lines_feed")

ODDS_API_KEY = os.getenv("THE_ODDS_API_KEY")
ODDS_API_BASE = "https://api.the-odds-api.com/v4"

# Phase 1 — DraftKings + FanDuel only (lowest quota cost per event).
BOOKMAKERS = "draftkings,fanduel"

# Sports → (odds_api_sport_key, [markets])
# NOTE: for TENNIS we don't hard-code tournaments here. The `SPORT_CONFIG`
# dict is expanded at runtime by `_discover_active_tennis_tournaments`
# (called from `refresh_alt_lines`) which queries The Odds API's
# `/v4/sports` catalog and injects any `active=True` ATP/WTA event key.
# This way we automatically pick up whichever Slam / ATP-Masters / WTA-1000
# is running without a manual code change. (2026-07-01 fix)
SPORT_CONFIG: dict[str, tuple[str, list[str]]] = {
    "soccer_world_cup": (
        "soccer_fifa_world_cup",
        ["player_goal_scorer_anytime", "player_first_goal_scorer",
         "player_to_score_or_assist", "alternate_totals"],
    ),
    "soccer_epl": (
        "soccer_epl",
        ["player_goal_scorer_anytime", "player_first_goal_scorer",
         "player_to_score_or_assist", "alternate_totals"],
    ),
    "soccer_uefa_champs": (
        "soccer_uefa_champs_league",
        ["player_goal_scorer_anytime", "player_first_goal_scorer",
         "player_to_score_or_assist", "alternate_totals"],
    ),
    "mlb": (
        "baseball_mlb",
        ["batter_hits_alternate", "batter_total_bases_alternate",
         "pitcher_strikeouts_alternate", "alternate_totals",
         "alternate_runs_lines"],
    ),
    "nfl": (
        "americanfootball_nfl",
        ["player_pass_yds_alternate", "player_rush_yds_alternate",
         "player_anytime_td", "player_reception_alternate"],
    ),
    # Tennis entries are inserted dynamically — see comment above.
}

# The set of markets we want for every ACTIVE tennis tournament.
# `h2h` is the moneyline (match winner) — the winningest tennis market
# in our history at 66.7%. Alt totals and alt spreads capture the
# "26 games over/under" and "+3.5 games handicap" style bets.
TENNIS_MARKETS = ["h2h", "alternate_totals_games", "alternate_spreads_games"]


def _norm(name: str) -> str:
    n = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower())
    return re.sub(r"\s+", " ", n).strip()


def _composite_key(event_id: str, book: str, market: str, sel: str,
                   line: Optional[float]) -> str:
    line_s = "" if line is None else f"@{line}"
    return f"{event_id}:{book}:{market}:{_norm(sel)}{line_s}"


async def _fetch_events(cx: httpx.AsyncClient, sport_key: str) -> list[dict]:
    from services.odds_cache import cached_httpx_get
    data = await cached_httpx_get(
        f"{ODDS_API_BASE}/sports/{sport_key}/events",
        {},
        api_key=ODDS_API_KEY,
        endpoint_type="events_list",
        caller="alt_lines_feed._fetch_events",
        sport_key=sport_key,
        skip_completed=True,
    )
    return data or []


async def _fetch_event_odds(cx: httpx.AsyncClient, sport_key: str,
                             event_id: str, markets: list[str],
                             db: Optional[AsyncIOMotorDatabase] = None,
                             ) -> Optional[dict]:
    """Fetch alt-line markets for one event.

    Phase A (2026-08) — burn-reduction changes:
      1. Consult the bad-market registry before fetching.  Any (sport,
         market) tuple that returned 422 in the last 24 h is skipped.
      2. If the batch returns None (422 / upstream error) we mark the
         *entire* market set as bad — no more per-market fallback that
         used to double our call volume.
    """
    from services.odds_cache import cached_httpx_get
    from services.bad_market_registry import filter_markets, mark_bad

    # (1) Drop any markets we already know are unsupported for this sport.
    if db is not None:
        markets = await filter_markets(
            db, sport_key=sport_key, markets=markets)
    if not markets:
        return None

    data = await cached_httpx_get(
        f"{ODDS_API_BASE}/sports/{sport_key}/events/{event_id}/odds",
        {"regions": "us", "bookmakers": BOOKMAKERS,
          "markets": ",".join(markets), "oddsFormat": "american"},
        api_key=ODDS_API_KEY,
        endpoint_type="event_alt_lines",
        caller="alt_lines_feed._fetch_event_odds",
        sport_key=sport_key,
        markets=",".join(markets),
    )
    # (2) Bulk failed → mark the whole set as bad so future cycles skip
    # them.  We do NOT retry per-market anymore; that was burning 4k+
    # credits/day.  If a market later becomes valid the 24 h TTL will
    # let us rediscover it on the next day's snapshot.
    if data is None and db is not None:
        await mark_bad(db, sport_key=sport_key, markets=markets,
                        reason="batch_422_or_error")
    return data


async def _discover_active_tennis_tournaments(cx: httpx.AsyncClient) -> list[tuple[str, str]]:
    """Query The Odds API `/v4/sports` catalog and return every tennis
    tournament (both ATP and WTA) that either is currently `active` OR
    has at least one upcoming event. Returns `(cfg_key, sport_key)`
    tuples suitable for injection into SPORT_CONFIG at runtime.

    We DELIBERATELY include `active: false` tournaments too because
    The Odds API often flips the `active` flag off between rounds even
    when events are still scheduled (e.g., transition day between a
    tournament's semi-finals and final). The downstream event-fetch
    already handles empty event lists gracefully.

    Coverage caveat (2026-07-13 user question: "Why alt lines not
    generating for tennis matches?"):
      The Odds API tennis catalog only covers Grand Slams, Masters
      1000s, WTA 1000s, and a handful of 500-level events. The ATP/
      WTA 250 tour (Umag, Bastad, Gstaad, Iasi WTA, Athens WTA,
      Kitzbühel WTA, Hamburg, etc.) is NOT in the catalog at all —
      no amount of discovery tuning surfaces those tournaments here.
      For 250-level events our tennis picks come from the TennisExplorer
      scrape (`source=tennis_extra`) which is moneyline-only.
    """
    return await _discover_tennis_from_catalog(cx, include_inactive=True)


async def _discover_tennis_from_catalog(
    cx: httpx.AsyncClient, include_inactive: bool = True,
) -> list[tuple[str, str]]:
    if not ODDS_API_KEY:
        return []
    try:
        r = await cx.get(
            f"{ODDS_API_BASE}/sports",
            params={"apiKey": ODDS_API_KEY, "all": "true"},
            timeout=10,
        )
        if r.status_code != 200:
            return []
        catalog = r.json() or []
    except Exception as e:
        logger.warning("tennis catalog fetch failed: %s", e)
        return []
    out: list[tuple[str, str]] = []
    active_ct = 0
    inactive_ct = 0
    for s in catalog:
        key = s.get("key") or ""
        if not key.startswith("tennis_"):
            continue
        if s.get("active"):
            active_ct += 1
            out.append((key, key))
        elif include_inactive:
            inactive_ct += 1
            out.append((key, key))
    logger.info(
        "tennis catalog: %d entries (%d active, %d inactive included) "
        "— note: 250-tier tournaments never appear in this catalog and "
        "have no alt-line coverage",
        len(out), active_ct, inactive_ct,
    )
    return out


async def _discover_active_soccer_leagues(cx: httpx.AsyncClient) -> list[tuple[str, str]]:
    """Same as tennis auto-discovery but for soccer. Handles the fact
    that our static SPORT_CONFIG only covers World Cup / EPL / UCL —
    when those are out of season we miss MLS, Copa America,
    Bundesliga, Serie A, Ligue 1, Liga MX, Champ League, etc. entirely.

    2026-07-01 addition per user Task E."""
    return await _discover_active_sports_by_prefix(cx, "soccer_")


async def _discover_active_sports_by_prefix(cx: httpx.AsyncClient, prefix: str) -> list[tuple[str, str]]:
    if not ODDS_API_KEY:
        return []
    try:
        r = await cx.get(
            f"{ODDS_API_BASE}/sports",
            params={"apiKey": ODDS_API_KEY, "all": "true"},
            timeout=10,
        )
        if r.status_code != 200:
            return []
        catalog = r.json() or []
    except Exception as e:
        logger.warning("sports auto-discovery (%s) failed: %s", prefix, e)
        return []
    out: list[tuple[str, str]] = []
    for s in catalog:
        key = s.get("key") or ""
        if not key.startswith(prefix):
            continue
        if not s.get("active"):
            continue
        out.append((key, key))
    if out:
        logger.info("%s auto-discovery: %d active (%s)",
                    prefix.rstrip("_"), len(out), ", ".join(k for _, k in out))
    return out


# The set of markets we want for every ACTIVE soccer league auto-
# discovered at runtime. Same three markets we hardcoded for the static
# WC/EPL/UCL entries. Anytime GS is the marquee; To-Score-or-Assist
# doubles the coverage; alternate_totals gives us the O/U 2.5 goals.
SOCCER_MARKETS = [
    "player_goal_scorer_anytime",
    "player_to_score_or_assist",
    "alternate_totals",
]


# Canonical `sport` string → Odds API sport_key fallback.  Used when
# picks lack a `sport_key` field (e.g. MLB NRFI picks).  Multi-league
# sports (Soccer, Tennis) are excluded — they must carry an explicit
# `sport_key` because we can't guess which league.
_SPORT_TO_ODDS_KEY: dict[str, str] = {
    "mlb":  "baseball_mlb",
    "nfl":  "americanfootball_nfl",
    "nba":  "basketball_nba",
    "nhl":  "icehockey_nhl",
    "ncaaf": "americanfootball_ncaaf",
    "ncaab": "basketball_ncaab",
    "cfb":  "americanfootball_ncaaf",
}


async def _todays_pick_scope(db: AsyncIOMotorDatabase) -> dict:
    """Return the set of sports + team-pairs that appear in TODAY's
    picks board.  Used by `refresh_alt_lines` to restrict alt-line
    fetching to games that actually have picks — avoiding the "poll
    every discovered soccer league" credit-burn pattern.

    Because picks are stored WITHOUT `event_id` today, we match on
    normalized team-pair tuples `(norm(home), norm(away))` — which is
    stable across data sources.

    Returns:
        {
          "sport_keys":     set[str],                 # sports with picks
          "team_pairs":     set[tuple[str, str]],      # (norm_home, norm_away)
          "by_sport_key":   dict[str, set[tuple[str, str]]],
        }
    """
    from datetime import date
    empty = {"sport_keys": set(), "team_pairs": set(),
             "by_sport_key": {}}
    if db is None:
        return empty
    today = date.today().isoformat()
    yday = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    try:
        cursor = db.picks.find(
            {"pick_date": {"$in": [today, yday]}},
            {"odds_api_sport_key": 1, "sport_key": 1,
             "home_team": 1, "away_team": 1, "sport": 1},
        )
        sports: set[str] = set()
        pairs: set[tuple[str, str]] = set()
        by_sport: dict[str, set[tuple[str, str]]] = {}
        async for p in cursor:
            sk = (p.get("odds_api_sport_key") or p.get("sport_key")
                   or "").strip()
            # Fallback: derive sport_key from `sport` string for
            # single-league sports.  Multi-league sports (Soccer,
            # Tennis) MUST carry an explicit sport_key — we skip if
            # missing rather than guess.
            if not sk:
                sport_lc = (p.get("sport") or "").strip().lower()
                sk = _SPORT_TO_ODDS_KEY.get(sport_lc, "")
            if sk:
                sports.add(sk)
            home = _norm(p.get("home_team") or "")
            away = _norm(p.get("away_team") or "")
            if home and away:
                # store both orderings so we match regardless of
                # home/away designation across data sources
                pair_ab = (home, away)
                pair_ba = (away, home)
                pairs.add(pair_ab)
                pairs.add(pair_ba)
                if sk:
                    by_sport.setdefault(sk, set()).update([pair_ab, pair_ba])
        return {"sport_keys": sports, "team_pairs": pairs,
                "by_sport_key": by_sport}
    except Exception as e:
        logger.warning("_todays_pick_scope err: %s", e)
        return empty


async def refresh_alt_lines(
    db: AsyncIOMotorDatabase,
    *,
    picks_scope: bool = True,
    max_events_per_sport: int = 30,
    event_window_hours: int = 36,
) -> dict:
    """Pull alt-line markets for all active events across configured sports.

    Phase A (2026-08) — burn-reduction changes:
      • `picks_scope=True` restricts fetching to sports/events that
        appear in today's `picks` collection.  Sports that don't have
        picks yet still get a *shortlist* fetch (events list only) so
        the pick-generation snapshot can find candidates, but we skip
        per-event alt-line pulls for un-picked events.
      • Event window narrowed from +4 d → +36 h (rarely posted earlier).
      • `_fetch_event_odds` now consults the bad-market registry and
        no longer falls back to per-market retries on 422.
    """
    from services.bad_market_registry import ensure_indices as _ensure_bmr
    await _ensure_bmr(db)

    if not ODDS_API_KEY:
        return {"ok": False, "reason": "no_api_key"}

    stats = {"sports": 0, "events": 0, "rows": 0, "errors": 0,
             "tennis_tournaments": 0, "picks_scope": picks_scope,
             "skipped_no_picks": 0, "skipped_out_of_window": 0}
    now = datetime.now(timezone.utc)

    scope = await _todays_pick_scope(db) if picks_scope else \
        {"sport_keys": set(), "team_pairs": set(), "by_sport_key": {}}
    stats["scope_sports"] = len(scope["sport_keys"])
    stats["scope_team_pairs"] = len(scope["team_pairs"]) // 2  # dedupe ordering

    async with httpx.AsyncClient(headers={"User-Agent": "PerkLocks/1.0"}) as cx:
        # Build the effective config on each cycle: static entries +
        # dynamically-discovered active tennis + soccer tournaments
        # (2026-07-01 auto-discovery).
        effective_config = dict(SPORT_CONFIG)
        for cfg_key, sport_key in await _discover_active_tennis_tournaments(cx):
            effective_config[cfg_key] = (sport_key, TENNIS_MARKETS)
            stats["tennis_tournaments"] += 1
        # Soccer auto-discovery — only add leagues that either have
        # picks today OR are in the static safety-net list.  This is
        # the key change that stops us polling Argentina Primera / J-
        # League / Superettan / etc. when we don't have picks in them.
        discovered_soccer = 0
        for cfg_key, sport_key in await _discover_active_soccer_leagues(cx):
            already_covered = any(
                sk == sport_key for _, (sk, _) in effective_config.items()
            )
            if already_covered:
                continue
            # picks-scope filter: only add if we have picks in this sport
            if picks_scope and sport_key not in scope["sport_keys"]:
                continue
            effective_config[cfg_key] = (sport_key, SOCCER_MARKETS)
            discovered_soccer += 1
        stats["soccer_leagues_discovered"] = discovered_soccer

        for cfg_key, (sport_key, markets) in effective_config.items():
            events = await _fetch_events(cx, sport_key)
            if not events:
                continue
            stats["sports"] += 1
            # Restrict events to those that have picks today (if we're
            # in picks-scope AND this sport has any picks at all).
            scoped_pairs = scope["by_sport_key"].get(sport_key, set())
            for ev in events[:max_events_per_sport]:
                ev_id = ev.get("id")
                if not ev_id:
                    continue
                stats["events"] += 1

                # picks-scope filter: skip events where neither team
                # pair appears in today's picks
                if picks_scope and scoped_pairs:
                    home_n = _norm(ev.get("home_team") or "")
                    away_n = _norm(ev.get("away_team") or "")
                    pair = (home_n, away_n)
                    if not home_n or not away_n or pair not in scoped_pairs:
                        stats["skipped_no_picks"] += 1
                        continue

                try:
                    commence = datetime.fromisoformat(
                        (ev.get("commence_time") or "").replace("Z", "+00:00")
                    )
                    if commence < now - timedelta(hours=2):
                        continue  # already over
                    if commence > now + timedelta(hours=event_window_hours):
                        stats["skipped_out_of_window"] += 1
                        continue  # too far out
                except Exception:
                    pass

                odds = await _fetch_event_odds(cx, sport_key, ev_id,
                                                markets, db=db)
                if not odds:
                    continue
                rows = _flatten_odds(odds, cfg_key, sport_key, now)
                if not rows:
                    continue
                # Upsert by composite key.
                for row in rows:
                    await db.live_alt_lines.update_one(
                        {"market_id": row["market_id"]},
                        {"$set": row},
                        upsert=True,
                    )
                stats["rows"] += len(rows)
                await asyncio.sleep(0.08)  # be nice to the API

    logger.info("alt_lines refresh: %s", stats)
    return {"ok": True, **stats, "refreshed_at": now.isoformat()}


def _sport_label(cfg_key: str) -> str:
    if cfg_key.startswith("soccer"):
        return "soccer"
    if cfg_key == "mlb":
        return "mlb"
    if cfg_key == "nfl":
        return "nfl"
    if cfg_key.startswith("tennis"):
        return "tennis"
    return cfg_key


def _flatten_odds(odds: dict, cfg_key: str, sport_key: str,
                   now: datetime) -> list[dict]:
    """Turn an Odds API event payload into per-(book, market, line, sel) rows."""
    event_id = odds.get("id")
    home = odds.get("home_team")
    away = odds.get("away_team")
    commence = odds.get("commence_time")
    event_name = f"{away} @ {home}" if away and home else (
        odds.get("sport_title") or "?"
    )
    sport = _sport_label(cfg_key)
    out: list[dict] = []
    for bm in odds.get("bookmakers") or []:
        book = bm.get("key")
        if not book:
            continue
        for mk in bm.get("markets") or []:
            mkey = mk.get("key")
            if not mkey:
                continue
            for o in mk.get("outcomes") or []:
                sel = o.get("description") or o.get("name") or ""
                if not sel:
                    continue
                line = o.get("point")
                try:
                    line = float(line) if line is not None else None
                except Exception:
                    line = None
                price = o.get("price")
                try:
                    price = int(price) if price is not None else None
                except Exception:
                    price = None
                composite = _composite_key(event_id, book, mkey, sel, line)
                out.append({
                    "sport": sport,
                    "odds_api_sport": sport_key,
                    "event_id": event_id,
                    "event_name": event_name,
                    "home_team": home,
                    "away_team": away,
                    "commence_time": commence,
                    "sportsbook": book,
                    "market_key": mkey,
                    "selection": sel,
                    "selection_norm": _norm(sel),
                    "line": line,
                    "price": price,
                    "market_id": composite,
                    "selection_id": composite,
                    "last_seen": now,
                    "fetched_at": now,
                })
    return out


async def ensure_indices(db: AsyncIOMotorDatabase) -> None:
    """TTL on last_seen (30 min) + lookup indices."""
    await db.live_alt_lines.create_index("market_id", unique=True)
    await db.live_alt_lines.create_index(
        [("sport", 1), ("event_name", 1), ("market_key", 1)]
    )
    await db.live_alt_lines.create_index(
        [("sport", 1), ("selection_norm", 1), ("market_key", 1)]
    )
    # TTL: any row not refreshed in 30 minutes auto-deletes.
    await db.live_alt_lines.create_index(
        "last_seen", expireAfterSeconds=1800
    )


async def lookup_alt_line(
    db: AsyncIOMotorDatabase, *, sport: str, player_or_selection: str,
    market_key: str, line: Optional[float] = None,
    sportsbook: Optional[str] = None, max_stale_minutes: int = 15,
) -> Optional[dict]:
    """Validate that an alt-line pick has a matching LIVE row.

    Returns the matching row, or None if not found (caller decides which
    error code to surface: line_not_found, market_removed, stale_odds).
    """
    q: dict = {
        "sport": sport,
        "selection_norm": _norm(player_or_selection),
        "market_key": market_key,
    }
    if sportsbook:
        q["sportsbook"] = sportsbook
    if line is not None:
        q["line"] = float(line)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_stale_minutes)
    q["last_seen"] = {"$gte": cutoff}
    return await db.live_alt_lines.find_one(q)


async def find_nearest_line(
    db: AsyncIOMotorDatabase, *, sport: str, event_name_substr: str,
    market_key: str, target_line: float, sportsbook: Optional[str] = None,
    max_stale_minutes: int = 15,
) -> Optional[dict]:
    """Snap a model prediction to the nearest live book line.

    Used by Tennis: model says "total games 22.4 over" → look up which
    line the book actually offers (22.5 / 23.5 / 21.5) and snap.
    """
    q = {
        "sport": sport,
        "event_name": {"$regex": event_name_substr, "$options": "i"},
        "market_key": market_key,
        "last_seen": {
            "$gte": datetime.now(timezone.utc) -
                    timedelta(minutes=max_stale_minutes),
        },
    }
    if sportsbook:
        q["sportsbook"] = sportsbook
    candidates = await db.live_alt_lines.find(q).to_list(50)
    if not candidates:
        return None
    candidates.sort(
        key=lambda r: abs((r.get("line") or 0) - target_line)
    )
    return candidates[0]
