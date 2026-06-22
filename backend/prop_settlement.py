"""Player prop settlement engine.

The Odds API doesn't grade player props (Hits, Rebounds, Points, Anytime Goal
Scorer, etc.). This module fills that gap using free official stat sources:

  * MLB:    statsapi.mlb.com         — hits, home runs, RBIs, runs,
                                       strikeouts (batter & pitcher), walks,
                                       pitcher outs recorded
  * NBA/WNBA/NHL/NFL:  ESPN site API — points, rebounds, assists, goals
  * Soccer:           ESPN site API — anytime goal scorer (any goal)

Public, key-free endpoints. We intentionally cache scoreboards per (sport,
date) and batch-fetch all boxscores once, so settling 100 props for a single
game day stays well under 20 outbound HTTP calls.

Markets we know how to grade today (selection is the player name, market
string contains the line e.g. "Aaron Judge Over 0.5 Hits"):

    MLB    : Hits, Home Runs, Strikeouts, RBIs, Runs, Walks, Outs Recorded
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
    forgiving (e.g. 'José Ramírez' == 'jose ramirez', 'A.J. Brown' == 'aj brown').

    Also strips parenthetical disambiguators that The Odds API attaches to
    duplicate player names — e.g. 'Max Muncy (2002)' (year of birth) and
    'Aaron Judge (NYY)' (team tag) — so they match the stats feed which
    only stores the plain name."""
    if not name:
        return ""
    # Strip parenthetical content FIRST so "Max Muncy (2002)" → "Max Muncy"
    # and "Aaron Judge (NYY)" → "Aaron Judge".
    name = re.sub(r"\([^)]*\)", " ", name)
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
    ("outs recorded", "mlb.outs"),
    ("pitcher outs",  "mlb.outs"),
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
    ("to score or assist",  "soccer.scoreOrAssist"),
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
    """stat_key is like 'mlb.hits'.

    Returns the player's stat value. Critical edge case fixed here:
    when the player IS on the roster but did NOT play (empty batting/pitching
    block — happens with bench scratches, late-arrival rosters, and games
    where a bullpen pitcher prop never came in), the upstream MLB Stats API
    response still includes the player under teams.{side}.players but the
    `batting` and `pitching` blocks are empty dicts.

    Previously we returned None in that case, which made the settlement engine
    SKIP the pick (status stayed pending forever — the Friday→Saturday "still
    on Friday" bug the user reported). Sportsbooks resolve a DNP / no-at-bat
    Over prop as a LOSS under standard "Action" rules (you bet on a stat that
    didn't materialise → you lose). We now return 0.0 in that case so the
    grader can settle it. If we ever want PUSH-on-DNP semantics, change the
    sentinel here and teach `_grade` to honour it.
    """
    field = stat_key.split(".", 1)[1]
    if not box:
        return None
    found_player = False
    for side in ("away", "home"):
        players = ((box.get("teams") or {}).get(side, {}).get("players") or {})
        for _pid, pdata in players.items():
            person = pdata.get("person") or {}
            full = person.get("fullName") or ""
            if not _names_match(player_name, full):
                continue
            found_player = True
            stats = pdata.get("stats") or {}
            # Try canonical blocks first (batting, pitching). The MLB API uses
            # identical keys ('strikeOuts', 'hits', etc.) in both.
            for block in ("batting", "pitching"):
                section = stats.get(block) or {}
                if field in section:
                    try:
                        return float(section[field])
                    except (TypeError, ValueError):
                        return None
            # Field-level fallback (some stats live under non-canonical keys).
            for _block_name, block in stats.items():
                if isinstance(block, dict) and field in block:
                    try:
                        return float(block[field])
                    except (TypeError, ValueError):
                        continue
            # Player matched but no usable stat block — DNP / scratch.
            # Treat as 0 so the prop grades cleanly instead of hanging in
            # "pending" until the heat death of the universe.
            return 0.0
    # Player not on either roster at all — unknown game, bail.
    return None if not found_player else 0.0


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
    """For Anytime/First Goal Scorer markets in Soccer/NHL.

    ESPN moved the play feed: modern soccer summaries put goal events under
    `keyEvents` (each event has `type.text == "Goal"` and a text field like
    "Goal! Team 1, Team 0. Player Name (Team) ..."). Older endpoints used
    `scoringPlays`/`plays`. We check all three so we work against whichever
    shape the API returns for a given match.
    """
    if not summary:
        return None
    plays = list(summary.get("scoringPlays") or summary.get("plays") or [])
    # Also pull Goal-type rows out of keyEvents (modern shape).
    for ev in summary.get("keyEvents") or []:
        if (ev.get("type") or {}).get("text", "").lower() == "goal":
            plays.append(ev)
    if not plays:
        return None
    found_first: Optional[str] = None
    found_any = False
    for p in plays:
        ath_blocks = (p.get("athletes") or p.get("participants") or
                      p.get("athletesInvolved") or [])
        scorer_name = None
        for block in ath_blocks:
            ath = block.get("athlete") or block
            name = ath.get("displayName") or ath.get("name") or ""
            if name:
                scorer_name = name
                break
        if not scorer_name:
            text = (p.get("text") or "") + " " + (p.get("name") or "")
            # "Goal! Saudi Arabia 1, Uruguay 0. Abdulelah Al Amri (Saudi Arabia) ..."
            m = re.search(r"\.\s+([A-ZÀ-ÿ][\w'.-]+(?:\s+[A-ZÀ-ÿ][\w'.-]+)+)\s*\(", text)
            if not m:
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
    return (found_any, found_first)


def _espn_did_score_or_assist(summary: dict, player_name: str) -> Optional[bool]:
    """For Soccer "To Score or Assist" markets.

    Returns True if `player_name` either scored OR was credited with an assist.
    False if neither. None if we couldn't read the play feed at all.

    See `_espn_did_score_goal` for the modern ESPN summary shape — we look
    at `keyEvents`, `scoringPlays`, and `plays` in that order.
    """
    if not summary:
        return None
    plays = list(summary.get("scoringPlays") or summary.get("plays") or [])
    for ev in summary.get("keyEvents") or []:
        if (ev.get("type") or {}).get("text", "").lower() == "goal":
            plays.append(ev)
    if not plays:
        return None
    for p in plays:
        names: list[str] = []
        for block in (p.get("athletes") or p.get("participants") or
                      p.get("athletesInvolved") or []):
            ath = block.get("athlete") or block
            nm = ath.get("displayName") or ath.get("name") or ""
            if nm:
                names.append(nm)
        for k in ("assist", "assistedBy", "assists"):
            v = p.get(k)
            if isinstance(v, dict):
                nm = v.get("displayName") or v.get("name")
                if nm:
                    names.append(nm)
            elif isinstance(v, list):
                for entry in v:
                    if isinstance(entry, dict):
                        nm = entry.get("displayName") or entry.get("name")
                        if nm:
                            names.append(nm)
        # Text-field fallback covers "Goal! ... Player Name (Team) ... Assisted by Other"
        text = (p.get("text") or "") + " " + (p.get("name") or "")
        for m in re.finditer(
            r"(?:by|assist(?:ed)? by|from)\s+([A-ZÀ-ÿ][\w'.-]+(?:\s+[A-ZÀ-ÿ][\w'.-]+)+)",
            text, re.IGNORECASE,
        ):
            names.append(m.group(1))
        # Also catch "Goal! ... Player Name (Team)" pattern
        m = re.search(r"\.\s+([A-ZÀ-ÿ][\w'.-]+(?:\s+[A-ZÀ-ÿ][\w'.-]+)+)\s*\(", text)
        if m:
            names.append(m.group(1))
        for nm in names:
            if _names_match(player_name, nm):
                return True
    return False


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
        # ESPN soccer needs a league code, which varies. We try all the
        # leagues The Odds API + our soccer pipeline can possibly surface,
        # including FIFA World Cup, World Cup qualifiers, EU domestic leagues,
        # CONMEBOL competitions, and the rest. Adding a league here costs
        # one extra scoreboard call per settle group — cheap, public API.
        soccer_leagues = [
            # FIFA / international
            "soccer/fifa.world",
            "soccer/fifa.worldq.uefa",
            "soccer/fifa.worldq.conmebol",
            "soccer/fifa.worldq.concacaf",
            "soccer/fifa.worldq.afc",
            "soccer/fifa.worldq.caf",
            "soccer/fifa.worldq.ofc",
            "soccer/fifa.confederations",
            "soccer/fifa.cwc",
            # UEFA
            "soccer/uefa.champions",
            "soccer/uefa.europa",
            "soccer/uefa.europa.conf",
            "soccer/uefa.nations",
            "soccer/uefa.euro",
            "soccer/uefa.euroq",
            # CONMEBOL
            "soccer/conmebol.libertadores",
            "soccer/conmebol.sudamericana",
            "soccer/conmebol.america",
            # Domestic top flights
            "soccer/eng.1", "soccer/esp.1", "soccer/ger.1", "soccer/ita.1",
            "soccer/fra.1", "soccer/por.1", "soccer/ned.1",
            "soccer/mex.1", "soccer/usa.1", "soccer/bra.1", "soccer/arg.1",
            # Second tiers (where our backfill found events)
            "soccer/eng.2", "soccer/esp.2", "soccer/ita.2", "soccer/ger.2",
            "soccer/bra.2", "soccer/swe.1", "soccer/nor.1", "soccer/fin.1",
            "soccer/irl.1", "soccer/chn.1", "soccer/jpn.1", "soccer/kor.1",
        ]
        events: list[dict] = []
        event_league_map: dict[str, str] = {}  # event_id → league code
        for sl in soccer_leagues:
            sport_path, league = sl.split("/", 1)
            try:
                ev = await _espn_scoreboard(cx, sport_path, league, date_str)
                if ev:
                    for e in ev:
                        eid = str(e.get("id") or "")
                        if eid and eid not in event_league_map:
                            event_league_map[eid] = league
                    events.extend(ev)
            except Exception:
                pass
            await asyncio.sleep(0.05)  # 33 leagues @ 0.05s = 1.6s overhead, ESPN tolerates this
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
                # Use the league we discovered at scoreboard time (mapped per
                # event ID). Previously we tried to parse the league out of
                # the event's link URL but ESPN URLs look like
                # `.../soccer/match/_/gameId/<id>` — the regex matched
                # `match` instead of the real league code, so the summary
                # fetch 404'd and the prop never settled.
                sport_path = "soccer"
                league = event_league_map.get(ev_id, "eng.1")
                summary = await _espn_summary(cx, sport_path, league, ev_id) or {}
                summaries[ev_id] = summary
                await asyncio.sleep(0.2)
            # Determine the player. selection often holds the actual name
            # (e.g. "Vinicius Jr"); for picks where selection is just "Yes"
            # (older Odds API payloads), pull the name out of the market label.
            raw_sel = (p.get("selection") or "").strip()
            player = raw_sel if raw_sel and raw_sel.lower() not in ("yes", "no") else _player_from_market(p["market"])
            if not player:
                counts["skipped"] += 1
                continue
            # ─── Per-user feedback 2026-06-22: "Don't drop the goalscorer
            # I just want them to grade in history" ────────────────────────
            # Previously the engine VOIDED any goalscorer pick whose player
            # wasn't in the elite/auto_elite top-3 roster (`not-in-top-3-
            # scorers`). That dropped legit graded entries from history.
            # Now we grade EVERY goalscorer pick — ESPN tells us who scored,
            # so settlement is deterministic regardless of whether the pick
            # was on a top-3 striker or an obscure midfielder.
            if stat_key == "soccer.scoreOrAssist":
                got = _espn_did_score_or_assist(summary, player)
                if got is None:
                    counts["skipped"] += 1
                    continue
                outcome = "won" if got else "lost"
                await _record(db, p, outcome,
                              {"player": player, "stat": "scoreOrAssist", "value": 1 if got else 0, "line": 0.5},
                              counts)
                continue
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
    """Best-effort fallback to pull the player name out of the market label.

    Examples that must work:
      • "Aaron Judge Over 0.5 Hits"                → "Aaron Judge"
      • "Jamal Musiala Anytime Goal Scorer"        → "Jamal Musiala"
      • "Bukayo Saka First Goal Scorer"            → "Bukayo Saka"
      • "Vinicius Jr To Score or Assist"           → "Vinicius Jr"
      • "Pitcher Strikeouts Over 5.5 (Sandy Alcantara)" → "Sandy Alcantara"

    Returns "" when no name can be extracted — callers MUST treat that as
    "skip / leave pending", never grade against an empty name (that's the bug
    that was marking every soccer goalscorer pick as a loss when selection
    was just "Yes").
    """
    raw = (market or "").strip()
    if not raw:
        return ""
    # 1. Hits/Over/Under style: "<Name> Over 1.5 Hits" or "(<Name>)" trailing.
    paren = re.search(r"\(([A-ZÀ-ÿ][\w'.\- ]+?)\)\s*$", raw)
    if paren:
        return paren.group(1).strip()
    m = re.match(r"^(.*?)\s+(Over|Under)\s+", raw, re.IGNORECASE)
    if m and m.group(1).strip():
        return m.group(1).strip()
    # 2. Goal-scorer style: "<Name> Anytime Goal Scorer" / "First Goal Scorer".
    for tag in [
        r"Anytime Goal Scorer",
        r"First Goal Scorer",
        r"Last Goal Scorer",
        r"To Score or Assist",
        r"To Score \d+\+\s*Goals?",
        r"To Score Hat-?trick",
        r"To Score Brace",
    ]:
        m = re.match(rf"^(.+?)\s+{tag}\b", raw, re.IGNORECASE)
        if m and m.group(1).strip():
            return m.group(1).strip()
    # 3. Last-ditch heuristic: leading two capitalised tokens.
    m = re.match(r"^([A-ZÀ-ÿ][\w'.\-]+(?:\s+[A-ZÀ-ÿ][\w'.\-]+){1,3})\b", raw)
    if m:
        return m.group(1).strip()
    return ""


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
    # Build a `final_score` payload so the History UI can render a stat line
    # for player props (was previously empty → "score unavailable" text + the
    # user complaint "history don't show accurate data"). For player props the
    # most relevant "score" is the player's actual stat (e.g. {"Aaron Judge
    # Hits": 2}). For game-level props we'd already have score from settlement_engine.
    final_score_payload: dict = {}
    try:
        player = (detail or {}).get("player") or ""
        stat = (detail or {}).get("stat") or ""
        value = (detail or {}).get("value")
        line = (detail or {}).get("line")
        if player and stat and value is not None:
            label = f"{player} {stat.replace('_', ' ').title()}"
            final_score_payload[label] = value
            if line is not None:
                final_score_payload["Line"] = line
        elif (detail or {}).get("scorers") is not None:
            # Goal-scorer pick — show goal scorers + own player's goal count
            final_score_payload["Goals Scored"] = (detail or {}).get("scorers") or []
            if player:
                final_score_payload[player] = (detail or {}).get("player_goals", 0)
    except Exception:
        pass

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
            **({"final_score": final_score_payload} if final_score_payload else {}),
            **({"confidence_bucket": conf} if conf else {}),
        }},
    )
    counts[outcome] = counts.get(outcome, 0) + 1
    counts["settled"] += 1
