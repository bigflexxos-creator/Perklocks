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
        ["player_goal_scorer_anytime",
         "player_to_score_or_assist", "alternate_totals", "btts",
         "double_chance"],
    ),
    "soccer_epl": (
        "soccer_epl",
        ["player_goal_scorer_anytime",
         "player_to_score_or_assist", "alternate_totals", "btts",
         "double_chance"],
    ),
    "soccer_uefa_champs": (
        "soccer_uefa_champs_league",
        ["player_goal_scorer_anytime",
         "player_to_score_or_assist", "alternate_totals", "btts",
         "double_chance"],
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


# Phase 4E follow-up (2026-08-06) — some leagues have team-name
# transliteration drift between the Odds API and our internal
# picks/DB source (e.g. CSL: "Beijing Guoan" (DB) vs "Beijing FC"
# (Odds API), "Shenzhen Xinpengcheng" (DB) vs "Shenzhen Peng City"
# (Odds API)).  The picks-scope filter at ``refresh_alt_lines``
# compared normalised team pairs literally, so scorer-market requests
# for CSL never fired even when events existed.
#
# Fix: canonicalise both sides through the existing per-league alias
# tables BEFORE forming the pair tuple.  This does NOT loosen scorer
# quality gates, does NOT synthesize odds, and does NOT change any
# grading logic — it only closes the alias gap on the pair-equality
# check.
def _team_key(sport_key: str, raw_name: str) -> str:
    """Return the canonical, alias-resolved, normalised team key for
    the given sport.  For sports without an alias table this is just
    the plain normalised name."""
    n = _norm(raw_name)
    if not n:
        return ""
    try:
        if sport_key == "soccer_china_superleague":
            from csl_form_seed import _TEAM_ALIASES as _CSL
            return _CSL.get(n, n)
        # (Other leagues can be wired here as needed — MLS uses a
        # different multi-alias structure that maps to lists, not a
        # 1-1 canonical, so we skip it here.  Reserved for later.)
    except Exception:
        pass
    return n


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

    SOCCER_MARKET_COMPETITION_RUNTIME (2026-09) — bundle-failure
    recovery.  Previously a single unsupported market in the batch
    (e.g. `player_to_score_or_assist` on a league that doesn't carry
    it) marked ALL requested markets as bad, so valid siblings like
    BTTS / alternate_totals / anytime_scorer disappeared with it.

    New behavior:
      1. Consult the bad-market registry at (sport_key, event_id)
         scope BEFORE fetching — event-specific failures already
         cached are honored so we don't retry the same broken market
         on this event.
      2. Attempt the bundled request once.  If it succeeds, use it.
      3. If the bundle fails (422 / upstream error), retry each
         market family individually.  Preserve successful sibling
         responses; only the failing family is cached at
         (sport_key, event_id) scope.
      4. If EVERY family individually fails for this event, cache
         the union under the event-scoped registry so the next
         refresh does not repeat the fan-out.
    """
    from services.odds_cache import cached_httpx_get
    from services.bad_market_registry import filter_markets, mark_bad

    # (1) Drop any markets we already know are unsupported for this
    #     (sport_key, event_id) or globally for this sport.
    if db is not None:
        markets = await filter_markets(
            db, sport_key=sport_key, markets=markets, event_id=event_id)
    if not markets:
        return None

    async def _fetch_bundle(mkts: list[str]) -> Optional[dict]:
        return await cached_httpx_get(
            f"{ODDS_API_BASE}/sports/{sport_key}/events/{event_id}/odds",
            {"regions": "us", "bookmakers": BOOKMAKERS,
              "markets": ",".join(mkts), "oddsFormat": "american"},
            api_key=ODDS_API_KEY,
            endpoint_type="event_alt_lines",
            caller="alt_lines_feed._fetch_event_odds",
            sport_key=sport_key,
            markets=",".join(mkts),
        )

    # (2) Bundle attempt.
    data = await _fetch_bundle(markets)
    if data is not None:
        return data

    # (3) Bundle failed — retry each market individually so one bad
    #     market doesn't kill the entire event.  Merge successes.
    #     Cost cap: only fan out when there are 2+ markets AND we
    #     have a DB to persist the per-event bad marker; otherwise
    #     accept the single failure.
    if db is None or len(markets) <= 1:
        # Single-market failure or no DB: nothing to salvage.
        if db is not None:
            await mark_bad(db, sport_key=sport_key, markets=markets,
                            event_id=event_id, scope="event",
                            reason="single_market_422_or_error")
        return None

    merged: Optional[dict] = None
    failed_markets: list[str] = []
    for m in markets:
        one = await _fetch_bundle([m])
        if one is None:
            failed_markets.append(m)
            continue
        if merged is None:
            # First success — seed with event metadata + this market's
            # bookmakers.
            merged = {k: v for k, v in one.items() if k != "bookmakers"}
            merged["bookmakers"] = one.get("bookmakers") or []
        else:
            # Merge additional market rows into the same bookmaker
            # entries (Odds API returns one bookmaker block per
            # bookmaker with a `markets` list inside — we append the
            # new markets to matching bookmakers).
            existing_bm = {b.get("key"): b for b in merged["bookmakers"]}
            for new_bm in one.get("bookmakers") or []:
                bk = new_bm.get("key")
                if bk in existing_bm:
                    existing_bm[bk].setdefault("markets", []).extend(
                        new_bm.get("markets") or []
                    )
                else:
                    merged["bookmakers"].append(new_bm)

    # (4) Cache the actually-failed markets at event scope so we
    #     don't retry them next cycle.  If EVERYTHING failed, still
    #     mark them; if we salvaged even one, only mark the losers.
    if failed_markets:
        await mark_bad(db, sport_key=sport_key, markets=failed_markets,
                        event_id=event_id, scope="event",
                        reason="individual_market_422_or_error")

    return merged


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
    # Player-scorer markets (2A.5 universal — already available in
    # live_alt_lines for MLS + La Liga + other auto-discovered active
    # soccer leagues).  SOCCER_MARKET_COMPETITION_RUNTIME (2026-09):
    # `player_first_goal_scorer` REMOVED — do not waste provider
    # budget on first-goalscorer markets in this repair.
    "player_goal_scorer_anytime",
    "player_to_score_or_assist",
    # Game markets.  `alternate_totals` provides the multi-line
    # Over/Under surface (1.5 / 2.0 / 2.5 / 3.0 / ...).  `btts` is the
    # Both Teams to Score market.  `double_chance` is the real
    # sportsbook Home-or-Draw / Draw-or-Away / Home-or-Away market —
    # supported downstream but previously missing from acquisition
    # (SOCCER_MARKET_COMPETITION_RUNTIME 2026-09 fix §2).  Per-market
    # 422s are cached at (sport_key, event_id) scope so an unsupported
    # market on one event never suppresses valid siblings.
    "alternate_totals",
    "btts",
    "double_chance",
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
            # Phase 4E follow-up — use the per-sport canonical team
            # key so aliases (e.g. CSL "Beijing Guoan" ≡ "Beijing FC")
            # collapse to a single scope-pair entry.
            home = _team_key(sk, p.get("home_team") or "")
            away = _team_key(sk, p.get("away_team") or "")
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
        #
        # SOCCER_MARKET_COMPETITION_RUNTIME (2026-09) §4 — the strict
        # picks_scope gate created a CIRCULAR DEPENDENCY: an event
        # needed a published pick before we would fetch its alt
        # markets, but the pick was often waiting on alt markets to
        # be modeled.  We now include any league that either (a) has
        # picks today OR (b) has upcoming events in the standard
        # window as returned by ``_fetch_events``.  Cost stays low
        # because ``max_events_per_sport=30`` and per-event alt
        # requests are already bounded.
        discovered_soccer = 0
        for cfg_key, sport_key in await _discover_active_soccer_leagues(cx):
            already_covered = any(
                sk == sport_key for _, (sk, _) in effective_config.items()
            )
            if already_covered:
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
                # pair appears in today's picks.
                #
                # SOCCER_MARKET_COMPETITION_RUNTIME (2026-09) §4 —
                # soccer is EXEMPT from this gate.  An active soccer
                # fixture is eligible for alt-market discovery even
                # when we have not yet published a pick for it —
                # otherwise we cannot bootstrap BTTS / Double Chance
                # / anytime scorer markets for new fixtures.  Other
                # sports still respect picks_scope to control burn.
                is_soccer = cfg_key.startswith("soccer") or sport_key.startswith("soccer")
                if picks_scope and scoped_pairs and not is_soccer:
                    # Phase 4E follow-up — canonicalise via alias map
                    # so CSL etc. resolve across name variants.
                    home_n = _team_key(sport_key, ev.get("home_team") or "")
                    away_n = _team_key(sport_key, ev.get("away_team") or "")
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
    """Phase 3C — delegate to central registry (live_alt_lines
    indexes including the 30-minute TTL on last_seen)."""
    try:
        from services import index_registry as _ir
        await _ir.ensure_collection(db, "live_alt_lines")
    except Exception as e:  # pragma: no cover
        # Match old behaviour: preserve legacy log tone.
        import logging
        logging.getLogger("lockscore.alt_lines_feed").debug(
            "alt_lines_feed ensure_indices via registry: %s", e)


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
