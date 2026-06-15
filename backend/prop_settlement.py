"""Player prop settlement engine.

The Odds API doesn't grade player props (Hits, Rebounds, Points, Anytime Goal
Scorer, etc.). This module fills that gap using free official stat sources:

  * MLB:    statsapi.mlb.com         — hits, total bases, home runs, RBIs,
                                       strikeouts (batter & pitcher), walks
  * NBA/WNBA/NHL/NFL:  ESPN site API — points, rebounds, assists, goals
  * Soccer:           ESPN site API — anytime goal scorer (any goal)

Public, key-free endpoints. We intentionally cache scoreboards per (sport,
date) and batch-fetch all boxscores once, so settling 100 props for a single
game day stays well under 20 outbound HTTP calls.

Markets we know how to grade today (selection is the player name, market
string contains the line e.g. "Aaron Judge Over 0.5 Hits"):

    MLB    : Hits, Total Bases, Home Runs, Strikeouts, RBIs, Runs, Walks
    NBA    : Points, Rebounds, Assists, Threes, Steals, Blocks
    WNBA   : Points, Rebounds, Assists
    NHL    : Points (G+A), Goals, Assists, Shots on Goal
    NFL    : Passing Yards, Rushing Yards, Receiving Yards, TDs
    Soccer : Anytime Goal Scorer, First Goal Scorer
    Tennis : (not auto-graded — match-result picks already handled by main engine)

Anything else falls back to None and stays pending. Names are normalised
(accent-strip + lowercase) before comparison; we also accept last-name only
matches when a full-name lookup misses.
"""
from __future__ import annotations

import logging
import re
import unicodedata
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional, Iterable

import httpx

logger = logging.getLogger("lockscore.props")

_MLB_BASE = "https://statsapi.mlb.com/api/v1"
_ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"

_TIMEOUT = httpx.Timeout(15.0, connect=8.0)


# ─────────────────────────── helpers ───────────────────────────


def _norm(name: str) -> str:
    """lowercase + strip diacritics + collapse punctuation to make name matches
    forgiving (e.g. 'José Ramírez' == 'jose ramirez', 'A.J. Brown' == 'aj brown')."""
    if not name:
        return ""
    n = unicodedata.normalize("NFKD", name)
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = n.lower()
    n = re.sub(r"[^a-z0-9 ]+", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def _last_name(name: str) -> str:
    parts = _norm(name).split()
    if not parts:
        return ""
    # Drop common suffixes
    if parts[-1] in ("jr", "sr", "ii", "iii", "iv"):
        parts = parts[:-1]
    return parts[-1] if parts else ""


def _names_match(query: str, candidate: str) -> bool:
    qn, cn = _norm(query), _norm(candidate)
    if not qn or not cn:
        return False
    if qn == cn:
        return True
    # Full last name + first initial fallback (handles "Jose Ramirez" vs
    # "J. Ramirez" stats display).
    ql, cl = _last_name(query), _last_name(candidate)
    if ql and cl and ql == cl:
        # Require first-initial agreement to avoid colliding two players who
        # share a last name on the same team.
        q_first = (qn.split()[0] if qn.split() else "")
        c_first = (cn.split()[0] if cn.split() else "")
        if q_first and c_first and q_first[0] == c_first[0]:
            return True
    return False


def _grade(actual: float, line: float, side: str) -> str:
    """Over/Under grader with push handling on whole-number lines."""
    if side == "over":
        if actual > line:
            return "won"
        if actual == line:
            return "push"
        return "lost"
    # under
    if actual < line:
        return "won"
    if actual == line:
        return "push"
    return "lost"


_LINE_RE = re.compile(r"(?:Over|Under)\s+(-?\d+(?:\.\d+)?)", re.IGNORECASE)


def _extract_line(market: str) -> Optional[tuple[float, str]]:
    """('Aaron Judge Over 0.5 Hits') -> (0.5, 'over')."""
    if not market:
        return None
    m = _LINE_RE.search(market)
    if not m:
        return None
    side = "over" if "over" in market.lower() else "under"
    try:
        return (float(m.group(1)), side)
    except ValueError:
        return None


# Map the human market text we render in picks to a (stat-key, source) pair.
# Keys are matched in priority order against the LOWERCASED market text.
_MARKET_STATS: list[tuple[str, str]] = [
    # MLB
    ("total bases", "mlb.totalBases"),
    ("home runs",   "mlb.homeRuns"),
    ("strikeouts",  "mlb.strikeOuts"),
    ("walks",       "mlb.baseOnBalls"),
    ("rbi",         "mlb.rbi"),
    ("runs",        "mlb.runs"),
    ("hits",        "mlb.hits"),
    # Basketball / hockey shared
    ("rebounds",        "espn.rebounds"),
    ("assists",         "espn.assists"),
    ("three",           "espn.threes"),
    ("steals",          "espn.steals"),
    ("blocks",          "espn.blocks"),
    ("shots on goal",   "espn.shots"),
    ("goals",           "espn.goals"),
    ("points",          "espn.points"),
    # Football
    ("passing yards",   "espn.passYds"),
    ("rushing yards",   "espn.rushYds"),
    ("receiving yards", "espn.recYds"),
    ("touchdowns",      "espn.tds"),
    # Soccer special-case
    ("anytime goal scorer", "soccer.anytime"),
    ("first goal scorer",   "soccer.first"),
]


def _stat_key_for_market(market: str) -> Optional[str]:
    m = (market or "").lower()
    for needle, key in _MARKET_STATS:
        if needle in m:
            return key
    return None


def _parse_event_teams(event_str: str) -> tuple[Optional[str], Optional[str]]:
    if not event_str or "@" not in event_str:
        return (None, None)
    a, h = event_str.split("@", 1)
    return (a.strip(), h.strip())


# ─────────────────────────── MLB ───────────────────────────


async def _mlb_games_on(cx: httpx.AsyncClient, date_str: str) -> list[dict]:
    """date_str is YYYY-MM-DD in US/Eastern game-day terms. We just pass it
    straight through — MLB Stats API treats dates loosely enough for our use."""
    url = f"{_MLB_BASE}/schedule"
    r = await cx.get(url, params={"sportId": 1, "date": date_str})
    if r.status_code != 200:
        return []
    games: list[dict] = []
    for d in (r.json().get("dates") or []):
        for g in (d.get("games") or []):
            games.append(g)
    return games


async def _mlb_boxscore(cx: httpx.AsyncClient, game_pk: int) -> Optional[dict]:
    url = f"{_MLB_BASE}/game/{game_pk}/boxscore"
    r = await cx.get(url)
    if r.status_code != 200:
        return None
    return r.json()


def _mlb_find_game(games: list[dict], away: str, home: str) -> Optional[dict]:
    an, hn = _norm(away), _norm(home)
    for g in games:
        teams = g.get("teams") or {}
        away_team = (teams.get("away") or {}).get("team", {}).get("name") or ""
        home_team = (teams.get("home") or {}).get("team", {}).get("name") or ""
        if _norm(away_team) == an and _norm(home_team) == hn:
            return g
        # Loose match (e.g. "St. Louis Cardinals" vs "St Louis Cardinals")
        if hn in _norm(home_team) and an in _norm(away_team):
            return g
    return None


def _mlb_stat_for_player(box: dict, player_name: str, stat_key: str) -> Optional[float]:
    """stat_key is like 'mlb.hits'."""
    field = stat_key.split(".", 1)[1]
    if not box:
        return None
    # box["teams"]["away"]["players"]["ID{ID}"]["stats"]["batting"|"pitching"]
    for side in ("away", "home"):
        players = ((box.get("teams") or {}).get(side, {}).get("players") or {})
        for _pid, pdata in players.items():
            person = pdata.get("person") or {}
            full = person.get("fullName") or ""
            if not _names_match(player_name, full):
                continue
            stats = pdata.get("stats") or {}
            # Strikeouts can come from batting or pitching depending on context;
            # for prop markets we always treat the player's own stat line. The
            # MLB API uses identical keys ('strikeOuts') in both blocks.
            for block in ("batting", "pitching"):
                section = stats.get(block) or {}
                if field in section:
                    try:
                        return float(section[field])
                    except (TypeError, ValueError):
                        return None
            # Field-level fallback (e.g. "hits" might also live elsewhere)
            for block_name, block in stats.items():
                if isinstance(block, dict) and field in block:
                    try:
                        return float(block[field])
                    except (TypeError, ValueError):
                        continue
    return None


# ─────────────────────────── ESPN (NBA/WNBA/NHL/NFL/Soccer) ───────────────────────────


_ESPN_SPORTS: dict[str, tuple[str, str]] = {
    "NBA":     ("basketball", "nba"),
    "WNBA":    ("basketball", "wnba"),
    "NHL":     ("hockey",     "nhl"),
    "NFL":     ("football",   "nfl"),
    "MLB":     ("baseball",   "mlb"),  # fallback
}


async def _espn_scoreboard(cx: httpx.AsyncClient, sport: str, league: str, date_str: str) -> list[dict]:
    url = f"{_ESPN_BASE}/{sport}/{league}/scoreboard"
    r = await cx.get(url, params={"dates": date_str.replace("-", "")})
    if r.status_code != 200:
        return []
    return r.json().get("events") or []


async def _espn_summary(cx: httpx.AsyncClient, sport: str, league: str, event_id: str) -> Optional[dict]:
    url = f"{_ESPN_BASE}/{sport}/{league}/summary"
    r = await cx.get(url, params={"event": event_id})
    if r.status_code != 200:
        return None
    return r.json()


def _espn_find_event(events: list[dict], away: str, home: str) -> Optional[dict]:
    an, hn = _norm(away), _norm(home)
    for ev in events:
        comps = (ev.get("competitions") or [{}])[0].get("competitors") or []
        home_team = ""
        away_team = ""
        for c in comps:
            name = (c.get("team") or {}).get("displayName") or ""
            short = (c.get("team") or {}).get("shortDisplayName") or ""
            full = name or short
            if c.get("homeAway") == "home":
                home_team = full
            elif c.get("homeAway") == "away":
                away_team = full
        ah, hh = _norm(away_team), _norm(home_team)
        if ah == an and hh == hn:
            return ev
        if an and hn and an in ah and hn in hh:
            return ev
    return None


# ESPN boxscore stat label → our normalized stat name lookup.
# Labels appear in boxscore.players[*].statistics[*].labels (array of stat
# header keys) and the parallel "stats" array (per-athlete row of strings).
_ESPN_LABEL_MAP = {
    "espn.points":   ("PTS", "POINTS"),
    "espn.rebounds": ("REB", "TOT", "REBOUNDS"),
    "espn.assists":  ("AST", "A"),
    "espn.threes":   ("3PT", "3PM", "3P"),
    "espn.steals":   ("STL",),
    "espn.blocks":   ("BLK",),
    "espn.shots":    ("SOG", "S"),
    "espn.goals":    ("G", "GOALS"),
    "espn.passYds":  ("YDS",),   # contextually from passing block
    "espn.rushYds":  ("YDS",),
    "espn.recYds":   ("YDS",),
    "espn.tds":      ("TD",),
}


# For NFL & similar multi-block sports we look at specific named groups.
_ESPN_GROUP_HINT = {
    "espn.passYds": ("passing",),
    "espn.rushYds": ("rushing",),
    "espn.recYds":  ("receiving",),
    "espn.tds":     ("passing", "rushing", "receiving"),
}


def _espn_player_stat(summary: dict, player_name: str, stat_key: str) -> Optional[float]:
    if not summary:
        return None
    labels_wanted = _ESPN_LABEL_MAP.get(stat_key, ())
    group_hint = _ESPN_GROUP_HINT.get(stat_key)
    box = summary.get("boxscore") or {}
    teams = box.get("players") or []
    for team in teams:
        stats_blocks = team.get("statistics") or []
        for block in stats_blocks:
            block_name = (block.get("name") or "").lower()
            if group_hint and block_name not in group_hint:
                continue
            labels = block.get("labels") or block.get("keys") or []
            # Find the column index for the stat label we want.
            idx = None
            for i, lab in enumerate(labels):
                if lab.upper() in labels_wanted:
                    idx = i
                    break
            if idx is None:
                continue
            athletes = block.get("athletes") or []
            for ath in athletes:
                person = ath.get("athlete") or {}
                full = person.get("displayName") or person.get("shortName") or ""
                if not _names_match(player_name, full):
                    continue
                values = ath.get("stats") or []
                if idx < len(values):
                    raw = str(values[idx])
                    # Strip non-numeric (handles "12-25" type splits → take first)
                    m = re.match(r"-?\d+(?:\.\d+)?", raw)
                    if m:
                        try:
                            return float(m.group(0))
                        except ValueError:
                            continue
    return None


def _espn_did_score_goal(summary: dict, player_name: str) -> Optional[bool]:
    """For Anytime/First Goal Scorer markets in Soccer/NHL."""
    if not summary:
        return None
    # Soccer: scoringPlays[*].team / participants / athletes
    plays = summary.get("scoringPlays") or summary.get("plays") or []
    found_first: Optional[str] = None
    found_any = False
    for p in plays:
        # ESPN format varies; scan athletes / participants
        ath_blocks = p.get("athletes") or p.get("participants") or []
        scorer_name = None
        for block in ath_blocks:
            ath = block.get("athlete") or block
            name = ath.get("displayName") or ath.get("name") or ""
            if name:
                scorer_name = name
                break
        if not scorer_name:
            # Sometimes the text field has "Goal by NAME"
            text = (p.get("text") or "") + " " + (p.get("name") or "")
            m = re.search(r"by\s+([A-ZÀ-ÿ][\w'.-]+(?:\s+[A-ZÀ-ÿ][\w'.-]+)+)", text)
            if m:
                scorer_name = m.group(1)
        if not scorer_name:
            continue
        if _names_match(player_name, scorer_name):
            found_any = True
            if found_first is None:
                found_first = scorer_name
        if found_first is None:
            found_first = scorer_name
    return None if not plays else (found_any, found_first)


# ─────────────────────────── orchestrator ───────────────────────────


def _date_str_for_pick(pick: dict) -> Optional[str]:
    """Get a YYYY-MM-DD we can hand to schedule endpoints."""
    et = pick.get("event_time") or pick.get("commence_time") or ""
    if not et:
        return None
    try:
        dt = datetime.fromisoformat(et.replace("Z", "+00:00"))
        # MLB / ESPN expect local-ish dates; using UTC date is close enough for
        # day-game props but for late games we also try the previous day.
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return None


async def settle_player_props(db, max_picks: int = 800) -> dict:
    """Find pending player-prop picks whose games have concluded and grade them.
    Returns counts: settled / won / lost / push / skipped / errors.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
    cursor = db.picks.find(
        {"status": {"$in": [None, "pending"]}},
        {"_id": 0},
    ).limit(max_picks)
    picks = await cursor.to_list(length=max_picks)
    counts = {"settled": 0, "won": 0, "lost": 0, "push": 0, "skipped": 0, "errors": 0, "scanned": 0}
    if not picks:
        return counts

    # Only consider markets that are recognisably player props.
    prop_picks: list[dict] = []
    for p in picks:
        stat_key = _stat_key_for_market(p.get("market") or "")
        if not stat_key:
            continue
        # Skip totals/teams ("Total Points Over 200" without a player name)
        sel = (p.get("selection") or "").strip()
        if not sel or sel.lower() in ("over", "under", "yes", "no"):
            # Soccer anytime goal scorer stores player in selection too — leave it
            if stat_key not in ("soccer.anytime", "soccer.first"):
                continue
        prop_picks.append(p)
    counts["scanned"] = len(prop_picks)
    if not prop_picks:
        return counts

    # Group by (sport, date) for batched fetching.
    groups: dict[tuple[str, str], list[dict]] = {}
    for p in prop_picks:
        et = p.get("event_time") or p.get("commence_time") or ""
        try:
            dt = datetime.fromisoformat(et.replace("Z", "+00:00"))
        except Exception:
            counts["skipped"] += 1
            continue
        if dt > cutoff:
            counts["skipped"] += 1
            continue
        groups.setdefault((p.get("sport") or "", _date_str_for_pick(p) or ""), []).append(p)

    async with httpx.AsyncClient(timeout=_TIMEOUT, headers={"User-Agent": "PerksLocks/1.0"}) as cx:
        for (sport, date_str), batch in groups.items():
            try:
                await _settle_group(cx, db, sport, date_str, batch, counts)
            except Exception as e:
                logger.warning("group settle failed %s/%s: %s", sport, date_str, e)
                counts["errors"] += len(batch)
            # Friendly to the public APIs.
            await asyncio.sleep(0.4)

    logger.info("Prop settlement: %s", counts)
    return counts


async def _settle_group(cx, db, sport: str, date_str: str, batch: list[dict], counts: dict):
    if not date_str:
        counts["skipped"] += len(batch)
        return

    # Resolve game lookups once.
    if sport == "MLB":
        games = await _mlb_games_on(cx, date_str)
        # also try the day before, since some games end after midnight UTC
        if not games:
            prev = (datetime.fromisoformat(date_str) - timedelta(days=1)).strftime("%Y-%m-%d")
            games = await _mlb_games_on(cx, prev)
        boxscores: dict[int, dict] = {}
        for p in batch:
            stat_key = _stat_key_for_market(p["market"] or "")
            line = _extract_line(p["market"] or "")
            if not stat_key or not line:
                counts["skipped"] += 1
                continue
            away, home = _parse_event_teams(p.get("event") or "")
            if not away or not home:
                counts["skipped"] += 1
                continue
            game = _mlb_find_game(games, away, home)
            if not game:
                counts["skipped"] += 1
                continue
            status = ((game.get("status") or {}).get("abstractGameState") or "").lower()
            if status != "final":
                counts["skipped"] += 1
                continue
            game_pk = game.get("gamePk")
            if not game_pk:
                counts["skipped"] += 1
                continue
            box = boxscores.get(game_pk)
            if box is None:
                box = await _mlb_boxscore(cx, game_pk) or {}
                boxscores[game_pk] = box
                await asyncio.sleep(0.25)
            player = (p.get("selection") or "").strip() or _player_from_market(p["market"])
            value = _mlb_stat_for_player(box, player, stat_key)
            if value is None:
                counts["skipped"] += 1
                continue
            outcome = _grade(value, line[0], line[1])
            await _record(db, p, outcome, {"player": player, "stat": stat_key.split(".")[-1], "value": value, "line": line[0]}, counts)
        return

    # ESPN sports (NBA, WNBA, NHL, NFL, Soccer)
    if sport == "Soccer":
        # ESPN soccer needs a league code, which varies. We try a couple of
        # the highest-volume leagues The Odds API surfaces.
        soccer_leagues = ["soccer/eng.1", "soccer/esp.1", "soccer/ger.1", "soccer/ita.1",
                          "soccer/fra.1", "soccer/mex.1", "soccer/usa.1", "soccer/uefa.champions",
                          "soccer/uefa.europa", "soccer/uefa.nations", "soccer/conmebol.libertadores"]
        events: list[dict] = []
        for sl in soccer_leagues:
            sport_path, league = sl.split("/", 1)
            try:
                ev = await _espn_scoreboard(cx, sport_path, league, date_str)
                if ev:
                    events.extend(ev)
            except Exception:
                pass
            await asyncio.sleep(0.2)
        summaries: dict[str, dict] = {}
        for p in batch:
            stat_key = _stat_key_for_market(p["market"] or "")
            away, home = _parse_event_teams(p.get("event") or "")
            if not away or not home:
                counts["skipped"] += 1
                continue
            ev = _espn_find_event(events, away, home)
            if not ev:
                counts["skipped"] += 1
                continue
            ev_id = str(ev.get("id"))
            status = (((ev.get("status") or {}).get("type") or {}).get("state") or "").lower()
            if status not in ("post", "final"):
                counts["skipped"] += 1
                continue
            summary = summaries.get(ev_id)
            if summary is None:
                # Need to infer sport/league from event link
                link = ev.get("links", [{}])[0].get("href") or ""
                # Reuse the same league guesser by matching the date scoreboard's league
                # (we'd already know but it's not stored on the event). Default to eng.1.
                sport_path, league = "soccer", "eng.1"
                m = re.search(r"/soccer/([a-z0-9.]+)/", link, re.IGNORECASE)
                if m:
                    league = m.group(1)
                summary = await _espn_summary(cx, sport_path, league, ev_id) or {}
                summaries[ev_id] = summary
                await asyncio.sleep(0.2)
            player = (p.get("selection") or "").strip() or _player_from_market(p["market"])
            result = _espn_did_score_goal(summary, player)
            if not result:
                counts["skipped"] += 1
                continue
            scored_any, first_name = result if isinstance(result, tuple) else (result, None)
            if stat_key == "soccer.anytime":
                outcome = "won" if scored_any else "lost"
            else:  # first
                outcome = "won" if first_name and _names_match(player, first_name) else "lost"
            await _record(db, p, outcome,
                          {"player": player, "stat": "goals", "value": 1 if scored_any else 0, "line": 0.5},
                          counts)
        return

    # NBA / WNBA / NHL / NFL
    sport_path, league = _ESPN_SPORTS.get(sport, (None, None))  # type: ignore[assignment]
    if not sport_path:
        counts["skipped"] += len(batch)
        return
    events = await _espn_scoreboard(cx, sport_path, league, date_str)
    if not events:
        # Try previous day for late-night games (UTC drift).
        prev = (datetime.fromisoformat(date_str) - timedelta(days=1)).strftime("%Y-%m-%d")
        events = await _espn_scoreboard(cx, sport_path, league, prev)
    summaries: dict[str, dict] = {}
    for p in batch:
        stat_key = _stat_key_for_market(p["market"] or "")
        line = _extract_line(p["market"] or "")
        if not stat_key or not line:
            counts["skipped"] += 1
            continue
        away, home = _parse_event_teams(p.get("event") or "")
        if not away or not home:
            counts["skipped"] += 1
            continue
        ev = _espn_find_event(events, away, home)
        if not ev:
            counts["skipped"] += 1
            continue
        ev_id = str(ev.get("id"))
        status = (((ev.get("status") or {}).get("type") or {}).get("state") or "").lower()
        if status not in ("post", "final"):
            counts["skipped"] += 1
            continue
        summary = summaries.get(ev_id)
        if summary is None:
            summary = await _espn_summary(cx, sport_path, league, ev_id) or {}
            summaries[ev_id] = summary
            await asyncio.sleep(0.25)
        player = (p.get("selection") or "").strip() or _player_from_market(p["market"])
        value = _espn_player_stat(summary, player, stat_key)
        if value is None:
            counts["skipped"] += 1
            continue
        outcome = _grade(value, line[0], line[1])
        await _record(db, p, outcome, {"player": player, "stat": stat_key.split(".")[-1], "value": value, "line": line[0]}, counts)


def _player_from_market(market: str) -> str:
    """Best-effort fallback: 'Aaron Judge Over 0.5 Hits' → 'Aaron Judge'."""
    m = re.match(r"^(.*?)\s+(Over|Under)\s+", market or "", re.IGNORECASE)
    return m.group(1).strip() if m else ""


async def _record(db, pick: dict, outcome: str, detail: dict, counts: dict):
    # Compute analytics-side fields the same way settlement_engine does so
    # the model-performance dashboard treats every settled pick uniformly.
    try:
        from analytics import (american_profit_per_unit, clv_units,
                                confidence_bucket)
        odds_used = pick.get("closing_odds") or pick.get("book_odds") or 0
        units_profit = american_profit_per_unit(odds_used, outcome)
        clv = clv_units(pick.get("odds_at_pick"), pick.get("closing_odds") or pick.get("book_odds"))
        conf = confidence_bucket(pick.get("lock_score"))
    except Exception:
        units_profit = -1.0 if outcome == "lost" else (0.0 if outcome == "push" else 0.91)
        clv = 0.0
        conf = None
    await db.picks.update_one(
        {"id": pick["id"]},
        {"$set": {
            "status": outcome,
            "settled_at": datetime.now(timezone.utc).isoformat(),
            "settlement_detail": detail,
            "settled_via": "prop_engine",
            "units_risked": 1.0 if outcome != "push" else 0.0,
            "units_profit": units_profit,
            "clv_value": clv,
            **({"confidence_bucket": conf} if conf else {}),
        }},
    )
    counts[outcome] = counts.get(outcome, 0) + 1
    counts["settled"] += 1
