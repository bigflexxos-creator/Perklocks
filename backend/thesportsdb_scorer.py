"""TheSportsDB v1 goalscorer-rate engine.

Built 2026-06-27 to replace the now-quota-exhausted sportdb.dev/flashscore
calls in `sportdb_player_scorer.py` for the lower-tier soccer leagues
that depend on synthetic goalscorer generation (CSL, MLS, J-League, etc).

WHY a separate module instead of patching `sportdb_player_scorer`?
  - Different upstream API (thesportsdb.com vs sportdb.dev)
  - Different JSON shapes
  - We want the existing working leagues (Premier League, La Liga, …)
  to keep using sportdb.dev when its quota recovers — this module is
  a DROP-IN fallback consulted when sportdb.dev returns 402/null.

DATA STRATEGY
  1. Team lookup     → /searchteams.php?t=<name>           (cache 30d)
  2. Roster fetch    → /lookup_all_players.php?id=<team>   (cache 7d)
  3. Season fixtures → /eventsseason.php?id=<team>&s=...   (cache 12h)
  4. Goal timelines  → /lookuptimeline.php?id=<event>      (cache forever)
  5. Roll up: striker_rate = goals / matches_played

The KEY INSIGHT: TheSportsDB doesn't store per-player "season stats" the
way sportdb.dev did. We have to BUILD that ourselves by tallying goals
across every match's timeline. The cache strategy makes this affordable:
each match timeline costs ONE API call (cached forever — finished matches
don't change), and a CSL team plays ~30 matches/year. Full-league
warm-up: ~16 teams × 30 matches = 480 calls (one-time, then cached).

PUBLIC API
  • async get_team_id(db, team_name) → str | None
  • async get_roster(db, team_id)    → list[dict]
  • async get_player_goal_rate(db, team_id, player_id, *, season_tag)
                                     → {goals, matches, rate, last_5_goals}
  • async compute_csl_scorers(db, home_team, away_team, ...) →
                                       list[pick_dict]
"""
from __future__ import annotations
import os
import asyncio
import logging
import math
import re
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

import httpx
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

logger = logging.getLogger("lockscore.thesportsdb_scorer")

_KEY = os.environ.get("THESPORTSDB_KEY") or "0621047683"
_BASE_V1 = "https://www.thesportsdb.com/api/v1/json"
_TIMEOUT = httpx.Timeout(8.0, connect=4.0)
_SEM = asyncio.Semaphore(3)            # never more than 3 in-flight calls
_REQUEST_DELAY = 0.10                  # pace requests so we don't get rate-limited

# TTLs — finished matches never change so timelines cache forever; season
# fixtures get a 12h TTL so new fixtures appear; rosters refresh weekly.
_TIMELINE_TTL = timedelta(days=365)    # effectively forever for finished matches
_FIXTURE_TTL  = timedelta(hours=12)
_ROSTER_TTL   = timedelta(days=7)
_TEAM_TTL     = timedelta(days=30)

# ───────────────────────── HTTP / cache layer ─────────────────────────


async def _get(path: str) -> Optional[Any]:
    """Single GET against TheSportsDB v1. Returns parsed JSON or None
    on any non-200 / network error. Logs 4xx/5xx for observability."""
    url = f"{_BASE_V1}/{_KEY}/{path.lstrip('/')}"
    async with _SEM:
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as cx:
                r = await cx.get(url)
                if r.status_code == 429:
                    logger.info("TheSportsDB %s → 429, backing off 1.5s", path)
                    await asyncio.sleep(1.5)
                    r = await cx.get(url)
                if r.status_code != 200:
                    logger.warning("TheSportsDB %s → %d", path, r.status_code)
                    return None
                await asyncio.sleep(_REQUEST_DELAY)
                return r.json()
        except Exception as e:
            logger.warning("TheSportsDB %s failed: %s", path, e)
            return None


_COLLECTION = "thesportsdb_cache"


async def _cache_get(db, key: str, ttl: timedelta) -> Optional[Any]:
    if db is None:
        return None
    doc = await db[_COLLECTION].find_one({"_id": key})
    if not doc:
        return None
    try:
        ts = datetime.fromisoformat(doc["fetched_at"])
    except Exception:
        return None
    if datetime.now(timezone.utc) - ts > ttl:
        return None
    return doc.get("data")


async def _cache_get_stale_ok(db, key: str) -> Optional[Any]:
    if db is None:
        return None
    doc = await db[_COLLECTION].find_one({"_id": key})
    return doc.get("data") if doc else None


async def _cache_set(db, key: str, data: Any) -> None:
    if db is None:
        return
    await db[_COLLECTION].update_one(
        {"_id": key},
        {"$set": {"data": data,
                  "fetched_at": datetime.now(timezone.utc).isoformat()}},
        upsert=True,
    )


# ───────────────────────── Team / roster lookup ─────────────────────────


# A handful of common team-name aliases between our system (which uses
# the Odds-API canonical names) and TheSportsDB. Expand as new leagues
# come online. Keys are NORMALIZED (lowercase, no punctuation).
_TEAM_ALIASES: dict[str, str] = {
    # CSL — Odds-API often uses long historical names
    "shandong luneng taishan fc":   "Shandong Taishan",
    "shandong luneng taishan":      "Shandong Taishan",
    "shanghai shenhua fc":          "Shanghai Shenhua",
    "shanghai port fc":             "Shanghai Port",
    "shanghai sipg fc":             "Shanghai Port",
    "beijing guoan fc":             "Beijing Guoan",
    "guangzhou evergrande taobao":  "Guangzhou",
    "henan jianye":                 "Henan",
    "dalian yifang":                "Dalian Pro",
    "tianjin teda":                 "Tianjin Jinmen Tiger",
    "wuhan three towns":            "Wuhan Three Towns",
    "chengdu rongcheng":            "Chengdu Rongcheng",
    "zhejiang professional":        "Zhejiang Professional",
    "qingdao hainiu":               "Qingdao Hainiu",
    "qingdao west coast":           "Qingdao West Coast",
    "yunnan yukun":                 "Yunnan Yukun",
    "changchun yatai":              "Changchun Yatai",
    "meizhou hakka":                "Meizhou Hakka",
    "shenzhen peng cheng":          "Shenzhen Peng City",
    "liaoning tieren fc":           "Liaoning Tieren",
    # MLS — Odds-API often uses full club name including "FC"
    "lafc":                         "Los Angeles FC",
    "la galaxy":                    "LA Galaxy",
}


def _norm(s: str) -> str:
    return re.sub(r"[^\w\s]", "", (s or "").lower()).strip()


def _resolve_team_name(team: str) -> str:
    n = _norm(team)
    if n in _TEAM_ALIASES:
        return _TEAM_ALIASES[n]
    # Strip common suffixes that aren't in TheSportsDB names
    stripped = re.sub(r"\b(fc|cf|sc|ac|afc|fk)\b", "", n).strip()
    if stripped in _TEAM_ALIASES:
        return _TEAM_ALIASES[stripped]
    return team


async def get_team_id(db, team_name: str) -> Optional[str]:
    """Resolve a team name to TheSportsDB idTeam. Cached 30 days."""
    canonical = _resolve_team_name(team_name)
    cache_key = f"team:{_norm(canonical)}"
    cached = await _cache_get(db, cache_key, _TEAM_TTL)
    if cached:
        return cached
    # URL-encode spaces as underscores per TheSportsDB convention
    q = canonical.replace(" ", "_")
    data = await _get(f"searchteams.php?t={q}")
    teams = (data or {}).get("teams") or [] if isinstance(data, dict) else []
    # Pick the first SOCCER team that matches the requested name (search
    # returns women's/youth/B-teams which we want to skip).
    pick = None
    for t in teams:
        if (t.get("strSport") or "").lower() != "soccer":
            continue
        name_n = _norm(t.get("strTeam") or "")
        if name_n == _norm(canonical):
            pick = t
            break
    if pick is None and teams:
        # Best-effort fallback to first soccer team
        for t in teams:
            if (t.get("strSport") or "").lower() == "soccer":
                pick = t
                break
    team_id = (pick or {}).get("idTeam")
    if team_id:
        await _cache_set(db, cache_key, team_id)
    return team_id


async def get_roster(db, team_id: str) -> list[dict]:
    """Return a team's roster. Each entry has idPlayer, strPlayer,
    strPosition, strNationality, strStatus. Cached 7 days."""
    if not team_id:
        return []
    cache_key = f"roster:{team_id}"
    cached = await _cache_get(db, cache_key, _ROSTER_TTL)
    if cached is not None:
        return cached
    data = await _get(f"lookup_all_players.php?id={team_id}")
    players = (data or {}).get("player") or [] if isinstance(data, dict) else []
    if not isinstance(players, list):
        players = []
    # Stash trimmed fields only — we don't need bio data here.
    slim = [
        {
            "idPlayer":   p.get("idPlayer"),
            "strPlayer":  p.get("strPlayer"),
            "strPosition": p.get("strPosition"),
            "strNationality": p.get("strNationality"),
            "strStatus":  p.get("strStatus"),
            "strNumber":  p.get("strNumber"),
        }
        for p in players
        if p.get("idPlayer") and p.get("strPlayer")
    ]
    await _cache_set(db, cache_key, slim)
    return slim


# ───────────────────────── Goal-rate computation ─────────────────────────


async def _fetch_team_recent_events(db, team_id: str, n_events: int = 15) -> list[dict]:
    """Pull last N finished events for a team. TheSportsDB caps at 5
    via eventslast.php — so we also try eventsseason.php for the
    current season to get more sample. Cached 12h."""
    if not team_id:
        return []
    cache_key = f"events_recent:{team_id}:{n_events}"
    cached = await _cache_get(db, cache_key, _FIXTURE_TTL)
    if cached is not None:
        return cached

    events: list[dict] = []

    # Strategy A: eventslast.php gives us the most recent 5 finished
    data = await _get(f"eventslast.php?id={team_id}")
    if isinstance(data, dict) and isinstance(data.get("results"), list):
        events.extend(data["results"])

    # Strategy B: pad to n_events with the current season's earlier fixtures
    if len(events) < n_events:
        now = datetime.now(timezone.utc)
        # CSL season runs Mar-Nov; MLS Feb-Dec; pick reasonable season tag
        for season in (f"{now.year}", f"{now.year - 1}-{now.year}", f"{now.year}-{now.year + 1}"):
            data2 = await _get(f"eventsseason.php?id={team_id}&s={season}")
            season_evs = (data2 or {}).get("events") or [] if isinstance(data2, dict) else []
            if isinstance(season_evs, list):
                # Only finished matches (score is non-null)
                for e in season_evs:
                    if e.get("intHomeScore") not in (None, "", "null") and e.get("idEvent"):
                        # Avoid dupes from strategy A
                        if not any(ev.get("idEvent") == e["idEvent"] for ev in events):
                            events.append(e)
            if len(events) >= n_events:
                break

    # Sort newest first, keep top N
    def _parse(d):
        try:
            return datetime.strptime(d.get("dateEvent") or "", "%Y-%m-%d")
        except Exception:
            return datetime.min
    events.sort(key=_parse, reverse=True)
    events = events[:n_events]
    await _cache_set(db, cache_key, events)
    return events


async def _fetch_event_goals(db, event_id: str) -> list[dict]:
    """Return list of goal entries for an event. Each entry has
    idPlayer + strPlayer + idTeam. Cached forever — finished matches
    don't change their goal scorers."""
    if not event_id:
        return []
    cache_key = f"goals:{event_id}"
    cached = await _cache_get(db, cache_key, _TIMELINE_TTL)
    if cached is not None:
        return cached
    data = await _get(f"lookuptimeline.php?id={event_id}")
    timeline = (data or {}).get("timeline") or [] if isinstance(data, dict) else []
    goals = []
    if isinstance(timeline, list):
        for t in timeline:
            if (t.get("strTimeline") or "").lower() == "goal":
                detail = (t.get("strTimelineDetail") or "").lower()
                # Skip own-goals — the SCORING player is the opponent so
                # they're recorded as their OWN team's goal in the
                # `idTeam` field, but it's not THEIR finishing skill
                # we want to credit.
                if "own goal" in detail:
                    continue
                goals.append({
                    "idPlayer": t.get("idPlayer"),
                    "strPlayer": t.get("strPlayer"),
                    "idTeam":   t.get("idTeam"),
                })
    await _cache_set(db, cache_key, goals)
    return goals


async def _fetch_team_avg_goals(db, team_id: str, n: int = 10) -> float:
    """Team's average goals scored per match over the last N matches.
    Used as a fallback rate-anchor when TheSportsDB has no per-goal
    timelines (lower-tier leagues like CSL, MLS-NEXT, etc.) — we
    still know how many goals each team usually scores from the
    final score line.
    """
    events = await _fetch_team_recent_events(db, team_id, n_events=n)
    if not events:
        return 0.0
    total_goals = 0
    matches_counted = 0
    for e in events:
        # idHomeTeam / idAwayTeam available — match the team_id to side
        home = str(e.get("idHomeTeam") or "")
        away = str(e.get("idAwayTeam") or "")
        try:
            home_score = int(e.get("intHomeScore") or 0)
            away_score = int(e.get("intAwayScore") or 0)
        except (TypeError, ValueError):
            continue
        if str(team_id) == home:
            total_goals += home_score
            matches_counted += 1
        elif str(team_id) == away:
            total_goals += away_score
            matches_counted += 1
    return (total_goals / matches_counted) if matches_counted else 0.0


# Per-position share-of-team-goals priors. A striker on a team that
# averages 1.5 g/match historically nets ~0.40 of those goals → 0.60
# expected goals → Poisson P(>=1) ≈ 0.45. A backup CF on jersey #18
# might net 0.18 of those goals → 0.27 xG → ≈ 24%.
_GOAL_SHARE_BY_POSITION = {
    "centre-forward":     0.34,
    "centre forward":     0.34,
    "striker":            0.34,
    "second striker":     0.22,
    "left winger":        0.18,
    "right winger":       0.18,
    "wing":               0.18,
    "attacking midfield": 0.14,
}


def _fallback_xg(team_avg_goals: float, position: str, jersey_number: str | None) -> float:
    """Compute expected-goals for a player when we don't have per-goal
    timeline data. Combines team scoring rate × position-based share
    × jersey-number boost (#9 / #10 / #11 are typically the starters
    so they get a slight bump over backup strikers wearing #20+).
    """
    pos = (position or "").lower().strip()
    base_share = _GOAL_SHARE_BY_POSITION.get(pos, 0.0)
    if base_share <= 0:
        return 0.0
    # Jersey-number heuristic — #9/#10/#11 = first-choice forwards
    jersey_boost = 1.0
    try:
        n = int((jersey_number or "0").strip())
        if n in (9, 10, 11, 7):
            jersey_boost = 1.18
        elif n <= 17:
            jersey_boost = 1.00
        else:
            jersey_boost = 0.78    # likely a rotation / backup
    except (TypeError, ValueError):
        jersey_boost = 1.0
    return team_avg_goals * base_share * jersey_boost


async def compute_player_goal_rate(
    db, team_id: str, player_id: str, *, n_events: int = 15,
    player_position: str | None = None,
    jersey_number: str | None = None,
) -> dict:
    """Compute a player's goals-per-match rate.

    PRIMARY path: tally per-goal timeline entries from the last N
    team matches (works for top leagues — EPL, La Liga, UCL, MLS, …).

    FALLBACK path: when timelines are empty (CSL, lower-tier leagues
    where TheSportsDB doesn't track per-goal data), estimate xG from
    team_avg_goals × position_share × jersey_boost. The fallback
    rate is FLAGGED as `from_fallback: True` so downstream knows it
    isn't ground-truth.

    Returns:
      {
        "matches":         int,
        "goals":           int,
        "rate_per_match":  float,
        "last_5_goals":    list[int],
        "from_fallback":   bool,
      }
    """
    events = await _fetch_team_recent_events(db, team_id, n_events=n_events)
    if not events:
        return {"matches": 0, "goals": 0, "rate_per_match": 0.0,
                "last_5_goals": [], "from_fallback": False}

    goals_per_match: list[int] = []
    total_timeline_entries = 0
    for e in events:
        eid = e.get("idEvent")
        if not eid:
            continue
        ev_goals = await _fetch_event_goals(db, eid)
        total_timeline_entries += len(ev_goals)
        n = sum(1 for g in ev_goals if str(g.get("idPlayer") or "") == str(player_id))
        goals_per_match.append(n)

    matches = len(goals_per_match)
    goals = sum(goals_per_match)

    # If we got ZERO timeline entries across all N matches, the league
    # doesn't have per-goal data on TheSportsDB. Use the team-avg
    # fallback so we still surface strikers from that league.
    if total_timeline_entries == 0 and player_position is not None:
        team_avg = await _fetch_team_avg_goals(db, team_id, n=10)
        xg = _fallback_xg(team_avg, player_position, jersey_number)
        return {
            "matches": matches,
            "goals":   0,
            "rate_per_match": round(xg, 3),
            "last_5_goals": [0] * min(5, matches),
            "from_fallback": True,
            "team_avg_goals": round(team_avg, 2),
        }

    # Standard Bayesian shrinkage toward 0.20 g/m striker prior
    PRIOR_RATE = 0.20
    PRIOR_WEIGHT = 4.0
    rate = (goals + PRIOR_RATE * PRIOR_WEIGHT) / (matches + PRIOR_WEIGHT) if matches else 0.0
    return {
        "matches": matches,
        "goals":   goals,
        "rate_per_match": round(rate, 3),
        "last_5_goals": goals_per_match[:5],
        "from_fallback": False,
    }


# ───────────────────────── Position-priority filter ─────────────────────────

# Which roster positions are eligible for anytime-goalscorer synthesis.
# Mirrors the sportdb.dev mapping. Goalkeepers and defenders never get
# synthetic goalscorer picks — too rare.
_ELIGIBLE_POSITIONS = {
    "centre-forward":       (0, 0.32),
    "centre forward":       (0, 0.32),
    "striker":              (0, 0.32),
    "left winger":          (1, 0.24),
    "right winger":         (1, 0.24),
    "wing":                 (1, 0.24),
    "second striker":       (1, 0.26),
    "attacking midfield":   (2, 0.18),
    "midfielder":           (3, 0.10),
    "central midfield":     (3, 0.10),
    "defensive midfield":   (4, 0.06),  # very low — usually skipped
}


def _position_prior(position: str) -> tuple[Optional[int], float]:
    """Map a roster position string to (priority, base_prob_floor).
    Returns (None, 0.0) if the position isn't eligible."""
    pos = (position or "").lower().strip()
    return _ELIGIBLE_POSITIONS.get(pos, (None, 0.0))


# ───────────────────────── Pick synthesis ─────────────────────────


async def compute_anytime_scorer_picks(
    db, *,
    home_team: str, away_team: str,
    event_id: str, kickoff_iso: str,
    league: str,
    sport_key: str = "soccer",
    max_per_side: int = 3,
    min_prob: float = 0.22,
    max_prob: float = 0.65,
) -> list[dict]:
    """Generate synthetic anytime-goalscorer picks for a match using
    TheSportsDB v1 as the data source.

    Steps:
      1. Resolve both teams' TheSportsDB idTeam.
      2. Fetch each team's roster.
      3. For every eligible position (CF, Striker, Wingers, Attacking Mid),
         compute the player's goals-per-match rate from the last 15
         finished team matches via timelines.
      4. Convert rate → P(>=1 goal) via Poisson.
      5. Cap probabilities at SYNTH_MAX_PROB. Filter < SYNTH_MIN_PROB.
      6. Return top `max_per_side` picks per team (descending probability).

    Each returned pick dict matches the legacy sportdb_player_scorer
    schema so downstream code (the picks pipeline, lock-score scorer,
    canonicalizer) can consume it without changes.
    """
    out: list[dict] = []
    for team_name, side in ((home_team, "home"), (away_team, "away")):
        team_id = await get_team_id(db, team_name)
        if not team_id:
            logger.info("TheSportsDB: no team_id for %s", team_name)
            continue
        roster = await get_roster(db, team_id)
        if not roster:
            logger.info("TheSportsDB: empty roster for %s (%s)", team_name, team_id)
            continue

        candidates: list[tuple[float, dict, dict]] = []  # (prob, player, stats)
        for p in roster:
            prio, _ = _position_prior(p.get("strPosition") or "")
            if prio is None or prio > 2:  # skip pure midfielders for now
                continue
            stats = await compute_player_goal_rate(
                db, team_id, p["idPlayer"],
                player_position=p.get("strPosition"),
                jersey_number=p.get("strNumber"),
            )
            if stats["matches"] < 3:
                # Not enough history to make a confident pick
                continue
            rate = stats["rate_per_match"]
            # P(>=1 goal) given Poisson(rate)
            prob = 1.0 - math.exp(-rate)
            # Cap into the sane band
            prob = max(0.0, min(prob, max_prob))
            if prob < min_prob:
                continue
            candidates.append((prob, p, stats))

        candidates.sort(key=lambda t: t[0], reverse=True)
        for prob, p, stats in candidates[:max_per_side]:
            # Mirror the lock_score mapping used by sportdb_player_scorer
            # so downstream dedup / ranking / canonicalization treat
            # these picks identically. The curve is intentionally less
            # aggressive than win_probability — a 45% anytime-goal-scorer
            # bet is a STRONG lock relative to the market floor of ~30%.
            #   prob 0.65 → lock 92    (max)
            #   prob 0.50 → lock 86
            #   prob 0.40 → lock 81
            #   prob 0.30 → lock 73
            #   prob 0.22 → lock 65    (min surfaced)
            lock_score = round(min(92.0, 55.0 + prob * 60.0), 1)
            out.append({
                "id": f"thesportsdb_scorer:{event_id}:{p['idPlayer']}",
                "market": f"{p['strPlayer']} - Anytime Goal Scorer",
                "selection": "Yes",
                "sport": "Soccer",
                "league": league,
                "event": f"{away_team} @ {home_team}",
                "event_id": event_id,
                "event_time": kickoff_iso,
                "team": team_name,
                "side": side,
                "player_id": p["idPlayer"],
                "player_name": p["strPlayer"],
                "win_probability": round(prob * 100, 2),
                "implied_probability": round(prob * 100, 2),
                "edge_percent": 0.0,           # synthetic — no book line to compare
                "lock_score":    lock_score,
                "lock_score_v2": lock_score,
                "raw_lock_score": lock_score,
                "peak_lock_score": lock_score,
                "grade": "A" if lock_score >= 88 else ("B" if lock_score >= 80 else "C"),
                "confidence": "A" if lock_score >= 88 else ("B" if lock_score >= 80 else "C"),
                "book_odds": int(round(-100 * prob / (1 - prob))) if prob < 0.5
                              else int(round(100 * (1 - prob) / prob)),
                "no_bet": False,
                "synthetic": True,
                "synthetic_source": "thesportsdb",
                "samples": {
                    "matches": stats["matches"],
                    "goals":   stats["goals"],
                    "rate":    stats["rate_per_match"],
                    "last_5":  stats["last_5_goals"],
                    "from_fallback": stats.get("from_fallback", False),
                },
            })
    return out
