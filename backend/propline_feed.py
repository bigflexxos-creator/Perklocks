"""Live alt-line feed — Player Props API (api.prop-line.com).

SECOND alt-line source, complementing `alt_lines_feed.py` (The Odds API).
The Odds API has a $30 quota and only covers a subset of player-prop
markets per event (mostly base markets + a few alt ladders for DK and
FanDuel). prop-line.com is a FREE coverage-rich API that exposes:

  ▸ 17 bookmakers (DraftKings, FanDuel, BetMGM, BetRivers, Bovada,
    Pinnacle, plus DFS apps and exchanges)
  ▸ 200k+ refreshed-within-60s player-prop markets
  ▸ Per-event alt-line ladders for batter hits / RBIs / HRs / total
    bases, pitcher strikeouts, NFL rushing/passing/receiving yards,
    NBA points/rebounds/assists, tennis games totals, etc.
  ▸ Free endpoints we may wire later for hit-rates, EV calcs, player
    trends.

Bookmaker scope (this pass):
  We restrict ingestion to US RETAIL books users can actually bet at:
    draftkings, fanduel, betmgm, betrivers, bovada
  Sharper / exchange / DFS books (pinnacle, novig, smarkets,
  matchbook, prizepicks, underdog, sleeper, polymarket) are skipped
  so the validator stamps prices users can actually shop.

Refresh cadence:
  • 8 min, slightly tighter than the Odds API loop (10 min) — prop-line
    has no documented free-tier quota and most rows are <1 min stale.
  • TTL on `last_seen` (30 min) so stale rows auto-evict, matching
    `live_alt_lines`.

Storage: `propline_alt_lines` collection — schema mirrors
`live_alt_lines` so `quality_gate.validate_against_live_alt_lines`
can union both at query time. The match query runs against
`live_alt_lines` first (preferred — DK/FD canonical) and falls back to
`propline_alt_lines` when no match. Conflicts break to whichever side
quotes the higher PRICE (best-of-book for the user).
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

logger = logging.getLogger("lockscore.propline_feed")

PROPLINE_API_KEY = os.getenv("PROPLINE_API_KEY")
PROPLINE_BASE = "https://api.prop-line.com/v1"

# US retail books — pick PRICES users can actually take.
US_RETAIL_BOOKS = frozenset({
    "draftkings", "fanduel", "betmgm", "betrivers", "bovada",
})

# DFS prop products — included for LINE-EXISTENCE validation but
# NEVER quoted for prices (PrizePicks uses flat ±0/-0 payouts that
# aren't comparable to American odds; including their prices would
# corrupt the user-facing price field). We need their rows because
# they're the only source of multi-rung alt ladders for sports like
# Tennis (player_games_won, total_games at 16.5/17.5/18.5/...).
DFS_PROP_BOOKS = frozenset({
    "prizepicks", "underdog", "sleeper",
})

# Combined ingest set — what the feed actually fetches.
INGEST_BOOKS = US_RETAIL_BOOKS | DFS_PROP_BOOKS

# prop-line.com sport_key → our internal sport label
SPORT_KEYS: dict[str, str] = {
    "baseball_mlb":    "mlb",
    "basketball_nba":  "nba",
    "basketball_wnba": "wnba",
    "football_nfl":    "nfl",
    "hockey_nhl":      "nhl",
    "basketball_ncaab": "ncaab",
    "football_ncaaf":  "ncaaf",
    "tennis":          "tennis",
    "golf":            "golf",
    "mma_ufc":         "mma",
    "boxing":          "boxing",
    # Soccer leagues (30+) — all map to internal "soccer" label so the
    # validator's sport check matches our pick documents.
    "soccer_epl":                    "soccer",
    "soccer_la_liga":                "soccer",
    "soccer_serie_a":                "soccer",
    "soccer_bundesliga":             "soccer",
    "soccer_ligue_1":                "soccer",
    "soccer_mls":                    "soccer",
    "soccer_uefa_champions_league":  "soccer",
    "soccer_uefa_europa_league":     "soccer",
    "soccer_uefa_conference_league": "soccer",
    "soccer_fifa_world_cup":         "soccer",
    "soccer_saudi_pro":              "soccer",
    "soccer_championship":           "soccer",
    "soccer_eredivisie":             "soccer",
    "soccer_liga_mx":                "soccer",
    "soccer_primeira_liga":          "soccer",
    "soccer_brasileirao":            "soccer",
    "soccer_argentina_primera":      "soccer",
    "soccer_scottish_premiership":   "soccer",
    "soccer_eliteserien":            "soccer",
    "soccer_japan_j_league":         "soccer",
    "soccer_turkey_super_lig":       "soccer",
    "soccer_belgium_pro_league":     "soccer",
    "soccer_sweden_allsvenskan":     "soccer",
    "soccer_a_league":               "soccer",
}

# Per-sport market keys we care about. None means "fetch everything"
# (prop-line lets us omit the markets filter to get all alt ladders).
SPORT_MARKETS: dict[str, Optional[list[str]]] = {
    "baseball_mlb": [
        # Base + alt ladders covering most of our MLB picks.
        "batter_hits", "batter_home_runs", "batter_total_bases",
        "batter_rbis", "batter_runs_scored", "batter_strikeouts",
        "batter_1plus_hits", "batter_2plus_hits", "batter_3plus_hits",
        "batter_1plus_rbis", "batter_2plus_rbis", "batter_3plus_rbis",
        "batter_2plus_home_runs", "batter_total_bases_alternate",
        "pitcher_strikeouts", "pitcher_strikeouts_alternate",
        "pitcher_outs", "pitcher_hits_allowed",
        "h2h", "spreads", "totals",
    ],
    "basketball_nba": [
        "h2h", "spreads", "totals",
        "player_points", "player_rebounds", "player_assists",
        "player_threes", "player_points_alternate",
        "player_rebounds_alternate", "player_assists_alternate",
        "player_points_rebounds_assists",
    ],
    "football_nfl":   [
        "h2h", "spreads", "totals",
        "player_pass_yds", "player_rush_yds", "player_reception_yds",
        "player_pass_tds", "player_anytime_td", "player_pass_yds_alternate",
        "player_rush_yds_alternate", "player_reception_alternate",
        "player_receptions", "player_pass_completions",
    ],
    "hockey_nhl":     [
        "h2h", "spreads", "totals",
        "player_points", "player_assists", "player_goal_scorer_anytime",
        "player_shots_on_goal",
    ],
    "basketball_ncaab": ["h2h", "spreads", "totals"],
    "football_ncaaf": ["h2h", "spreads", "totals"],
    # Tennis: the chalk-Over fix lives here. `total_games` + `totals`
    # are full-match totals (e.g. 36.5 games); `player_games_won` is
    # per-player. Without these the synthetic-chalk-line detector
    # can't compare picks against real book lines.
    "tennis":         [
        "h2h", "spreads", "totals", "total_games",
        "player_games_won", "player_aces", "player_double_faults",
        "total_sets", "total_tiebreaks",
    ],
    "golf":           ["h2h", "outrights"],
    # Soccer leagues — all use the same prop set (anytime scorer, S+A,
    # FGS, totals, BTTS). prop-line covers 30+ leagues we never had
    # access to via The Odds API.
    "soccer_epl":             None,  # fetch everything (varies per league)
    "soccer_la_liga":         None,
    "soccer_serie_a":         None,
    "soccer_bundesliga":      None,
    "soccer_ligue_1":         None,
    "soccer_mls":             None,
    "soccer_uefa_champions_league": None,
    "soccer_uefa_europa_league":    None,
    "soccer_uefa_conference_league": None,
    "soccer_fifa_world_cup":  None,
    "soccer_saudi_pro":       None,
    "soccer_championship":    None,
    "soccer_eredivisie":      None,
    "soccer_liga_mx":         None,
    "soccer_primeira_liga":   None,
    "soccer_brasileirao":     None,
    "soccer_argentina_primera": None,
    "soccer_scottish_premiership": None,
    "soccer_eliteserien":     None,
    "soccer_japan_j_league":  None,
    "soccer_turkey_super_lig": None,
    "soccer_belgium_pro_league": None,
    "soccer_sweden_allsvenskan": None,
    "soccer_a_league":        None,
}


def _norm(name: str) -> str:
    n = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower())
    return re.sub(r"\s+", " ", n).strip()


def _composite_key(event_id: str, book: str, market: str, sel: str,
                   line: Optional[float], side: str) -> str:
    line_s = "" if line is None else f"@{line}"
    side_s = f"|{side}" if side else ""
    return f"pl:{event_id}:{book}:{market}:{_norm(sel)}{line_s}{side_s}"


async def _request(cx: httpx.AsyncClient, path: str,
                   params: Optional[dict] = None) -> Optional[object]:
    headers = {"X-API-Key": PROPLINE_API_KEY, "Accept": "application/json"}
    try:
        r = await cx.get(f"{PROPLINE_BASE}{path}", params=params,
                         headers=headers, timeout=20)
        if r.status_code == 429:
            logger.warning("prop-line rate-limited on %s, sleeping 30s", path)
            await asyncio.sleep(30)
            return None
        if r.status_code != 200:
            logger.warning("prop-line %s status=%s body=%s",
                           path, r.status_code, r.text[:200])
            return None
        return r.json()
    except Exception as e:
        logger.warning("prop-line %s error: %s", path, e)
        return None


async def _fetch_events(cx: httpx.AsyncClient, sport_key: str) -> list[dict]:
    res = await _request(cx, f"/sports/{sport_key}/events")
    return res if isinstance(res, list) else []


async def _fetch_event_odds(cx: httpx.AsyncClient, sport_key: str,
                             event_id: str,
                             markets: Optional[list[str]]) -> Optional[dict]:
    params: dict = {}
    if markets:
        params["markets"] = ",".join(markets)
    res = await _request(
        cx, f"/sports/{sport_key}/events/{event_id}/odds", params,
    )
    return res if isinstance(res, dict) else None


def _flatten_event(odds: dict, sport_label: str, sport_key: str,
                   now: datetime) -> list[dict]:
    """prop-line odds payload → flat per-(book, market, line, sel) rows."""
    event_id = str(odds.get("id") or "")
    home = odds.get("home_team")
    away = odds.get("away_team")
    commence = odds.get("commence_time")
    event_name = f"{away} @ {home}" if away and home else "?"
    out: list[dict] = []
    for bm in odds.get("bookmakers") or []:
        book = (bm.get("key") or "").lower()
        if book not in INGEST_BOOKS:
            continue
        is_dfs = book in DFS_PROP_BOOKS
        for mk in bm.get("markets") or []:
            mkey = mk.get("key")
            if not mkey:
                continue
            for o in mk.get("outcomes") or []:
                # prop-line stores player in `description`, side in
                # `name` (Over/Under/Yes/No) or in `name` directly for
                # binary markets like batter_2plus_home_runs.
                desc = o.get("description") or ""
                side = (o.get("name") or "").strip()
                # Selection text: player name if present, else side.
                sel = desc.strip() if desc else side
                if not sel:
                    continue
                try:
                    line = float(o["point"]) if o.get("point") is not None else None
                except Exception:
                    line = None
                try:
                    price = int(o["price"]) if o.get("price") is not None else None
                except Exception:
                    price = None
                if price is None and not is_dfs:
                    # Skip retail rows with no price (corrupt feed
                    # entry); DFS rows are allowed to have null/100
                    # placeholder prices.
                    continue
                composite = _composite_key(
                    event_id, book, mkey, sel, line,
                    side.lower() if desc else "",
                )
                out.append({
                    "source": "propline",
                    "sport": sport_label,
                    "odds_api_sport": sport_key,
                    "event_id": event_id,
                    "event_name": event_name,
                    "home_team": home,
                    "away_team": away,
                    "commence_time": commence,
                    "sportsbook": book,
                    "is_dfs_only": is_dfs,
                    "market_key": mkey,
                    "selection": sel,
                    "selection_norm": _norm(sel),
                    "side": side,           # Over / Under / Yes / No / ""
                    "line": line,
                    "price": price,
                    "market_id": composite,
                    "selection_id": composite,
                    "last_seen": now,
                    "fetched_at": now,
                })
    return out


async def refresh_propline_alt_lines(db: AsyncIOMotorDatabase) -> dict:
    """Pull alt-line markets for all configured sports/events."""
    if not PROPLINE_API_KEY:
        return {"ok": False, "reason": "no_api_key"}

    stats = {"sports": 0, "events": 0, "rows": 0, "books": set()}
    now = datetime.now(timezone.utc)

    async with httpx.AsyncClient(headers={"User-Agent": "PerkLocks/1.0"}) as cx:
        for sport_key, sport_label in SPORT_KEYS.items():
            events = await _fetch_events(cx, sport_key)
            if not events:
                continue
            stats["sports"] += 1
            markets = SPORT_MARKETS.get(sport_key)
            # Filter to upcoming-in-window events FIRST, then cap. The
            # raw /events endpoint returns 528+ tennis events covering
            # weeks of slates — naively slicing first 30 missed all
            # the marquee WTA/ATP matches our app picks for. Window:
            # -3h to +4 days.
            window_events: list[dict] = []
            for ev in events:
                try:
                    ct = ev.get("commence_time") or ""
                    commence = datetime.fromisoformat(ct.replace("Z", "+00:00"))
                    if commence < now - timedelta(hours=3):
                        continue
                    if commence > now + timedelta(days=4):
                        continue
                except Exception:
                    continue
                window_events.append((commence, ev))
            window_events.sort(key=lambda t: t[0])
            # Cap per sport — higher caps for high-event sports.
            max_events = {
                "tennis": 120,           # full WTA + ATP slates
                "soccer_": 30,           # per soccer league
                "baseball_mlb": 60,
                "football_nfl": 30,
                "basketball_nba": 30,
                "basketball_wnba": 20,
                "hockey_nhl": 20,
            }
            cap = 30
            for prefix, n in max_events.items():
                if sport_key.startswith(prefix.rstrip("_")):
                    cap = n
                    break
            for _, ev in window_events[:cap]:
                ev_id = str(ev.get("id") or "")
                if not ev_id:
                    continue
                stats["events"] += 1
                odds = await _fetch_event_odds(cx, sport_key, ev_id, markets)
                if not odds:
                    continue
                rows = _flatten_event(odds, sport_label, sport_key, now)
                if not rows:
                    continue
                # Bulk upsert.
                for row in rows:
                    stats["books"].add(row["sportsbook"])
                    try:
                        await db.propline_alt_lines.update_one(
                            {"market_id": row["market_id"]},
                            {"$set": row},
                            upsert=True,
                        )
                        stats["rows"] += 1
                    except Exception as ue:
                        logger.debug("upsert err %s: %s", row["market_id"], ue)
                # Small throttle to be polite (free API).
                await asyncio.sleep(0.1)

    stats["books"] = sorted(stats["books"])
    logger.info("propline refresh: %s", stats)
    return {"ok": True, **stats, "refreshed_at": now.isoformat()}


async def ensure_propline_indices(db: AsyncIOMotorDatabase) -> None:
    """TTL + lookup indices for the propline collection."""
    await db.propline_alt_lines.create_index("market_id", unique=True)
    await db.propline_alt_lines.create_index(
        [("sport", 1), ("event_name", 1), ("market_key", 1)]
    )
    await db.propline_alt_lines.create_index(
        [("sport", 1), ("selection_norm", 1), ("market_key", 1), ("line", 1)]
    )
    # 30-min TTL on last_seen so stale rows auto-evict.
    await db.propline_alt_lines.create_index(
        "last_seen", expireAfterSeconds=1800
    )
