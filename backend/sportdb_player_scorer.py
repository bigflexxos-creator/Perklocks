"""SportDB-driven Universal Anytime Goal Scorer model.

Why this module exists:
    The Odds API only carries `player_goal_scorer_anytime` markets for the
    "big" leagues (top-5 Europe + WC + UCL/UEL). For everything else —
    Chinese Super League, J-League, K-League, MLS depth, Brasileirão, etc.
    — bookmakers return ZERO player props (verified in main agent's probe).

    SportDB.dev exposes:
      • Per-league standings + fixtures + results (we already use this)
      • Per-team full squad with player IDs
      • Per-player season stats (Goals Scored, Matches Played, Rating)

    Combining these, we can compute a synthetic Anytime Goal Scorer
    probability for every attacker on every roster across ~25 leagues
    where the bookmakers don't bother to publish player markets.

Model (Poisson rate-based):
    1. For each player on the active squad with position ∈ {Forward,
       Midfielder, Attacker}, fetch `Goals Scored` and `Matches Played`
       from their career stats for the current season.
    2. Compute base rate λ_base = goals / matches_played.
    3. Apply opponent-defence multiplier — derived from team standings
       cache (goals-against per match) — to get λ_event.
    4. Anytime-scorer probability = 1 - exp(-λ_event × 90/90 minutes).
    5. Keep picks where probability ≥ 28% (matches our existing
       `_SOCCER_PROP_MIN_IMPLIED = 0.22` floor with a small buffer for
       synthetic-only picks).

Caching:
    Player profile pages are STABLE — we cache for 7 days (career stats
    update once after each match). Squads update on transfer windows — 24h
    cache. League fixtures/standings — 24h. Total cost per pipeline run on
    a 16-team league = ~32 squad+player calls = 32 SportDB credits, but
    aggressive caching means the SECOND day's run is ~1-2 credits.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("lockscore.sportdb_scorer")

SPORTDB_KEY = os.getenv("SPORTDB_API_KEY")
SPORTDB_BASE = "https://api.sportdb.dev/api/flashscore"
_TIMEOUT = httpx.Timeout(12.0, connect=6.0)

# Player profile cache (7d) — career stats update slowly.
_PLAYER_TTL = timedelta(days=7)
# Team squad cache (24h) — roster can change at transfer windows.
_SQUAD_TTL = timedelta(hours=24)

# Concurrent SportDB requests cap (separate from sportdb_client's budget so
# we don't compete with the standings refresh). Capped at 2 to stay under
# the per-second rate limit (429 EXCEEDED_FREQ_LIMIT).
_SEM = asyncio.Semaphore(2)
# Per-request sleep so we honour the documented 1 req/sec ceiling.
_REQUEST_DELAY = 0.6

# Daily call counter so a misbehaving prop fetch can't burn all credits.
_DAILY_LIMIT = 200
_USAGE = {"date": None, "used": 0}


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _budget_ok() -> bool:
    today = _today_utc()
    if _USAGE["date"] != today:
        _USAGE["date"] = today
        _USAGE["used"] = 0
    return _USAGE["used"] < _DAILY_LIMIT


def _budget_inc():
    _USAGE["used"] += 1


# ─────────────── ODDS API key → SportDB league mapping ───────────────
# Maps the sport_key The Odds API returns (e.g. "soccer_china_superleague")
# to the SportDB path components we need: (country_slug_id, comp_slug_id,
# season). Cross-year leagues use "2025-2026"; calendar-year leagues use
# "2026" (currently active season).
LEAGUE_MAP: dict[str, tuple[str, str, str]] = {
    # ── Top 5 (already covered by Odds API too, used for cross-verification) ──
    "soccer_epl":                ("england:198",      "premier-league:dYlOSQOD",     "2025-2026"),
    "soccer_spain_la_liga":      ("spain:176",        "laliga:8tUjE6FL",             "2025-2026"),
    "soccer_germany_bundesliga": ("germany:81",       "bundesliga:lpwAFsK1",         "2025-2026"),
    "soccer_italy_serie_a":      ("italy:98",         "serie-a:dInOZ2Yo",            "2025-2026"),
    "soccer_france_ligue_one":   ("france:77",        "ligue-1:Wt4ehJpS",            "2025-2026"),
    # ── Mid-tier Europe (bookmakers MAY publish goal scorers; we backfill) ──
    "soccer_netherlands_eredivisie": ("netherlands:139", "eredivisie:8H1huJyM",      "2025-2026"),
    "soccer_portugal_primeira_liga": ("portugal:155",    "liga-portugal:KMabNT3K",   "2025-2026"),
    # ── PRIMARY TARGETS: bookmakers DON'T expose player markets here ──
    "soccer_china_superleague":      ("china:52",        "super-league:nc9yRmcn",    "2026"),
    "soccer_usa_mls":                ("usa:200",         "mls:CQv5qrFt",             "2026"),
    "soccer_brazil_campeonato":      ("brazil:39",       "serie-a-betano:Yq4hUnzQ",  "2026"),
    "soccer_brazil_serie_b":         ("brazil:39",       "serie-b:vRtLP6rs",         "2026"),
    "soccer_mexico_ligamx":          ("mexico:128",      "liga-mx:bm2Vlsfl",         "2025-2026"),
    "soccer_norway_eliteserien":     ("norway:145",      "eliteserien:GOvB22xg",     "2026"),
    "soccer_sweden_allsvenskan":     ("sweden:181",      "allsvenskan:nXxWpLmT",     "2026"),
    "soccer_finland_veikkausliiga":  ("finland:76",      "veikkausliiga:zTH7XBoF",   "2026"),
    "soccer_denmark_superliga":      ("denmark:63",      "superliga:O6W7GIaF",       "2025-2026"),
    "soccer_japan_j_league":         ("japan:105",       "j1-league:K0qPDOLA",       "2026"),
    "soccer_korea_kleague1":         ("south-korea:202", "k-league-1:rFcdLNRl",      "2026"),
    "soccer_australia_aleague":      ("australia:24",    "a-league:CIA9TpvC",        "2025-2026"),
    "soccer_saudi_prof":             ("saudi-arabia:172", "saudi-pro-league:p7CtIfx0", "2025-2026"),
    "soccer_argentina_primera_division": ("argentina:22", "liga-profesional:vTYz5w5l", "2026"),
    "soccer_uefa_champs_league":     ("europe:6",        "champions-league:xGrwqq16",  "2025-2026"),
    "soccer_uefa_europa_league":     ("europe:6",        "europa-league:ClDjv3V5",     "2025-2026"),
    "soccer_fifa_world_cup":         ("world:7",         "world-cup:eBHRoOnX",         "2026"),
    "soccer_fifa_club_world_cup":    ("world:7",         "club-world-cup:CGwEMb1u",    "2025"),
}

# Positions that can plausibly score. Goalkeepers/defenders excluded.
_SCORING_POSITIONS = {"forwards", "midfielders", "attackers"}

# Position priority for fetching order. Forwards > Attackers > Midfielders.
# We fetch the most likely scorers FIRST so the per-side cap doesn't waste
# credits on midfielders who rarely score (the squad list is alphabetical,
# not goal-likelihood-ordered).
_POSITION_PRIORITY = {
    "forwards":    0,
    "attackers":   0,
    "midfielders": 1,
}

# Probability floor for surfacing a synthetic anytime-scorer pick.
# 22% matches our existing `_SOCCER_PROP_MIN_IMPLIED` for real-bookmaker
# soccer goal-scorer markets. Lower means more picks but more longshots.
SYNTH_MIN_PROB = 0.22
# Cap — anything above 70% is suspicious for a synthetic pick (we have no
# real bookmaker line to validate it). Cap to prevent unrealistic locks.
SYNTH_MAX_PROB = 0.70

# ─────────────────────── HTTP layer ───────────────────────


async def _get(path: str, db=None) -> Optional[Any]:
    if not SPORTDB_KEY or not _budget_ok():
        return None
    url = path if path.startswith("http") else f"{SPORTDB_BASE}{path}"
    headers = {"X-API-Key": SPORTDB_KEY}
    async with _SEM:
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT, headers=headers) as cx:
                r = await cx.get(url)
                _budget_inc()
                if r.status_code == 429:
                    # Rate limited — back off and retry once.
                    logger.info("SportDB %s → 429, backing off 2s", path)
                    await asyncio.sleep(2.0)
                    r = await cx.get(url)
                    _budget_inc()
                if r.status_code != 200:
                    logger.warning("SportDB %s → %d", path, r.status_code)
                    return None
                # Pace subsequent calls so we don't get blocked.
                await asyncio.sleep(_REQUEST_DELAY)
                return r.json()
        except Exception as e:
            logger.warning("SportDB %s failed: %s", path, e)
            return None


# ─────────────────────── Cache helpers ───────────────────────


async def _cache_get(db, key: str, ttl: timedelta) -> Optional[Any]:
    if db is None:
        return None
    doc = await db.sportdb_scorer_cache.find_one({"_id": key})
    if not doc:
        return None
    try:
        ts = datetime.fromisoformat(doc.get("fetched_at", ""))
    except Exception:
        return None
    if datetime.now(timezone.utc) - ts > ttl:
        return None
    return doc.get("data")


async def _cache_get_stale_ok(db, key: str) -> Optional[Any]:
    """Return cached data IGNORING the TTL — used as a last-resort
    fallback when the live SportDB fetch fails (HTTP 402 / 429 / network
    error). Keeps lower-tier leagues like CSL on the board even when the
    upstream API throttles us; the data may be a few weeks old but a
    striker's career goal rate doesn't move materially in that window.
    Returns None if there's no cache entry at all.
    """
    if db is None:
        return None
    doc = await db.sportdb_scorer_cache.find_one({"_id": key})
    if not doc:
        return None
    return doc.get("data")


async def _cache_set(db, key: str, data: Any):
    if db is None or data is None:
        return
    await db.sportdb_scorer_cache.update_one(
        {"_id": key},
        {"$set": {"data": data, "fetched_at": datetime.now(timezone.utc).isoformat()}},
        upsert=True,
    )


# ─────────────────────── Squad + player helpers ───────────────────────


def _norm(name: str) -> str:
    if not name:
        return ""
    n = unicodedata.normalize("NFKD", name)
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = n.lower()
    n = re.sub(r"\b(fc|cf|sc|afc|cd|ac|sk|bk)\b", " ", n)
    n = re.sub(r"[^a-z0-9 ]+", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


# ── Legacy / rebranded team-name aliases ───────────────────────────────
# Odds API often keeps stale legacy club names ("Shandong Luneng", which
# rebranded to "Shandong Taishan" years ago). SportDB uses the modern
# name. Without this mapping the team resolver fails fuzzy-match because
# the extra word "Luneng" widens the edit distance past containment.
#
# Each entry is a normalised-input → normalised-canonical mapping. Add
# new aliases here when a CSL / Chinese / Saudi / Brazilian team name
# fails to resolve (most common rebrand cases).
_TEAM_NAME_ALIASES: dict[str, str] = {
    # China Super League rebrands
    "shandong luneng taishan":      "shandong taishan",
    "shandong luneng":              "shandong taishan",
    "shanghai sipg":                "shanghai port",
    "guangzhou evergrande":         "guangzhou",
    "guangzhou evergrande taobao":  "guangzhou",
    "jiangsu suning":               "jiangsu",
    "tianjin tianhai":              "tianjin",
    "tianjin teda":                 "tianjin jinmen tiger",
    "tianjin quanjian":             "tianjin tigers",
    "beijing renhe":                "beijing",
    "beijing guoan":                "beijing",
    "dalian yifang":                "dalian pro",
    "dalian aerbin":                "dalian pro",
    "guangzhou r f":                "guangzhou city",
    "wuhan zall":                   "wuhan three towns",
    # Saudi / others can be added here as they're encountered.
}


def _apply_team_alias(team_name: str) -> str:
    """Map a known legacy/rebranded team name to its modern canonical
    name. Returns the original string unchanged if no alias hits."""
    if not team_name:
        return team_name
    nq = _norm(team_name)
    canonical = _TEAM_NAME_ALIASES.get(nq)
    return canonical if canonical else team_name


async def _resolve_team_id(db, country_slug: str, comp_slug: str, season: str,
                            team_name: str) -> Optional[tuple[str, str]]:
    """Find (team_id, team_slug) by matching against the league's standings.

    SportDB standings rows include `teamId` + `teamName`. Standings are
    already cached by `sportdb_client.fetch_standings`. If not present, we
    fetch them here (1 credit, 24h cache).

    Returns None if no team matches — caller should fall back to no enrichment.
    """
    cache_key = f"standings:{country_slug}:{comp_slug}:{season}"
    cached = await _cache_get(db, cache_key, timedelta(hours=24))
    if not cached:
        path = f"/football/{country_slug}/{comp_slug}/{season}/standings"
        cached = await _get(path, db=db)
        if isinstance(cached, list):
            await _cache_set(db, cache_key, cached)
    if not isinstance(cached, list):
        return None
    # Apply rebranded-team alias BEFORE normalising — Odds API uses legacy
    # club names ("Shandong Luneng Taishan FC") while SportDB has the
    # modern names ("Shandong Taishan"). Without this the fuzzy match
    # fails and the team's entire forward roster gets skipped — meaning
    # players like Cryzan never get synthetic goalscorer picks.
    team_name = _apply_team_alias(team_name)
    nq = _norm(team_name)
    if not nq:
        return None
    best: Optional[tuple[str, str]] = None
    for row in cached:
        tn = row.get("teamName") or ""
        if not tn:
            continue
        n_team = _norm(tn)
        team_id = row.get("teamId") or ""
        team_slug = (row.get("teamSlug") or "").strip() or _norm(tn).replace(" ", "-")
        if n_team == nq:
            return (team_id, team_slug)
        # Loose match — keep the first containment hit as a fallback.
        if best is None and (nq in n_team or n_team in nq):
            best = (team_id, team_slug)
    return best


async def get_team_squad(db, team_slug: str, team_id: str) -> Optional[list[dict]]:
    """Fetch & cache a team's full squad. Returns the player list (or None).

    Each player dict has at minimum: id, slug, firstName, lastName, position,
    jerseyNumber, countryName, link.
    """
    if not team_id or not team_slug:
        return None
    key = f"squad:{team_slug}:{team_id}"
    cached = await _cache_get(db, key, _SQUAD_TTL)
    if cached is not None:
        return cached
    data = await _get(f"/team/{team_slug}/{team_id}", db=db)
    if not isinstance(data, dict):
        # Live fetch failed (likely 402/429). Fall back to STALE cache
        # so leagues like CSL stay on the board through API outages.
        # A squad doesn't change materially week-to-week; serving the
        # last known list is far better than dropping every CSL pick.
        stale = await _cache_get_stale_ok(db, key)
        if isinstance(stale, list) and stale:
            logger.info("SportDB squad fetch failed for %s — serving %d stale-cached players", team_slug, len(stale))
            return stale
        return None
    # Squad is grouped by tournament. We want the LEAGUE squad (not cup-only).
    squad_groups = data.get("squad") or []
    players: list[dict] = []
    seen_ids: set[str] = set()
    for grp in squad_groups:
        if (grp.get("tournamentType") or "").lower() != "league":
            continue
        for p in (grp.get("players") or []):
            pid = p.get("id") or ""
            if pid and pid not in seen_ids:
                seen_ids.add(pid)
                players.append(p)
    if not players and squad_groups:
        # Some leagues (CSL early-season) only have the cup squad. Fallback.
        for p in (squad_groups[0].get("players") or []):
            players.append(p)
    await _cache_set(db, key, players)
    return players


async def get_player_goal_rate(db, player_slug: str, player_id: str,
                                comp_slug_id: str, season: str) -> Optional[dict]:
    """Fetch a player's career stats and compute a TIME-WEIGHTED multi-season
    scoring rate.

    Per user feedback 2026-06-26: a player who scored 28 goals last season
    (Fabio Abreu: 28g/30m = 93% rate) but is at 5g/11m in the current early
    season should still be recognised as a top scorer. Only using current
    season buries proven stars — fixed by blending the last 4 seasons with
    a decaying weight, then taking a 70/30 blend of (weighted recent) vs
    (overall career average).

    Returns dict with:
      - rate_per_match: blended weighted+career rate (the value Poisson uses)
      - weighted_rate: 4-season decayed avg
      - career_rate: overall career goals/matches
      - career_goals, career_matches: totals across all league seasons
      - current_season_goals, current_season_matches: just this season
      - seasons_used: count of seasons included in weighting (max 4)
      - is_proven_star: career_goals >= 50 OR weighted_rate >= 0.40
      - last_5_seasons: list of per-season records for the insights UI

    None if no usable data.
    """
    if not player_id or not player_slug:
        return None
    key = f"player:{player_slug}:{player_id}"
    cached = await _cache_get(db, key, _PLAYER_TTL)
    if cached is None:
        data = await _get(f"/player/{player_slug}/{player_id}", db=db)
        if not isinstance(data, dict):
            # Live fetch failed (likely 402/429). Fall back to STALE
            # cache so synthetic-scorer leagues (CSL, MLS, J-League, …)
            # don't go dark just because SportDB throttled today. A
            # striker's career rate barely moves week-to-week, so a
            # 2-month-old snapshot is still a strong signal.
            stale = await _cache_get_stale_ok(db, key)
            if isinstance(stale, dict):
                logger.info("SportDB player fetch failed for %s — using stale cache", player_slug)
                cached = stale
            else:
                return None
        else:
            cached = data
            await _cache_set(db, key, cached)
    careers_root = cached.get("careers") or {}
    # MULTI-SOURCE career fetch:
    #   league          → domestic league (Premier League, La Liga, CSL...)
    #   nationalTeams   → World Cup, AFCON, Copa, Euros, Nations League, friendlies
    #   internationalCups → UCL, UEL, AFC Champions League, Conference League
    #   nationalCups    → FA Cup, Copa del Rey, DFB-Pokal etc.
    #
    # Per user 2026-06-26: "Salah on Egypt performs well in national league
    # games — app should know this" — and "Mbappé snaps for national team".
    # Was previously ignoring nationalTeams entirely. SportDB uses camelCase
    # PLURAL keys (`nationalTeams`, `internationalCups`) — verified against
    # Mbappé's profile which exposes 50+ France goals across Euros + WCs.
    league_careers = careers_root.get("league") or []
    national_careers = (
        careers_root.get("nationalTeams")        # SportDB.dev actual key
        or careers_root.get("nationalTeam")      # legacy
        or careers_root.get("national_team")     # snake_case legacy
        or []
    )
    intl_cup_careers = (
        careers_root.get("internationalCups")
        or careers_root.get("international_cups")
        or []
    )
    domestic_cup_careers = (
        careers_root.get("nationalCups")
        or careers_root.get("domesticCups")
        or careers_root.get("domestic_cups")
        or []
    )
    careers = (
        list(league_careers)
        + list(national_careers)
        + list(intl_cup_careers)
        + list(domestic_cup_careers)
    )
    if not careers:
        return None
    # Build season records — only LEAGUE entries with ≥3 matches played.
    seasons: list[dict] = []
    target_slug = comp_slug_id.split(":")[0]
    current_season_record: Optional[dict] = None
    for entry in careers:
        stats = {(s.get("name") or "").lower(): s.get("value") for s in (entry.get("stats") or [])}
        matches = _to_int(stats.get("matches played"))
        goals = _to_int(stats.get("goals scored"))
        if matches < 1:
            continue
        rec = {
            "season": entry.get("season") or "",
            "goals": goals,
            "matches": matches,
            "assists": _to_int(stats.get("assists")),
            "rating": _to_float(stats.get("rating")),
            "rate": goals / matches if matches > 0 else 0.0,
            "competition_slug": (entry.get("competitionSlug") or "").lower(),
            "team": entry.get("teamName") or "",
        }
        seasons.append(rec)
        # Identify the current-season record for this competition.
        if (rec["competition_slug"] == target_slug
                and rec["season"] == season
                and current_season_record is None):
            current_season_record = rec
    if not seasons:
        return None
    # Sort seasons newest → oldest (string sort works for "2025"/"2025-2026"
    # mostly correctly — close enough for ranking).
    seasons.sort(key=lambda x: x["season"], reverse=True)
    # Use the last 4 league seasons (mix of competitions OK — a 30-goal striker
    # in Portugal Primeira is still a 30-goal striker).
    recent_4 = [s for s in seasons if s["matches"] >= 5][:4]
    if not recent_4:
        # If no full seasons, fall back to ANY season with matches.
        recent_4 = seasons[:4]
    # Decayed weights: 40% current, 30% prev, 20% 2-ago, 10% 3-ago.
    weights = [0.40, 0.30, 0.20, 0.10]
    weighted_rate = 0.0
    total_weight = 0.0
    for i, s in enumerate(recent_4):
        w = weights[i] if i < len(weights) else 0.05
        weighted_rate += s["rate"] * w
        total_weight += w
    if total_weight > 0:
        weighted_rate /= total_weight
    # Career totals across ALL league seasons.
    career_goals = sum(s["goals"] for s in seasons)
    career_matches = sum(s["matches"] for s in seasons)
    career_rate = career_goals / career_matches if career_matches else 0.0
    # Final blend: 70% recent weighted + 30% overall career.
    # This rewards CURRENT form while not abandoning career-proven stars
    # who happen to be in a slow start.
    blended_rate = 0.7 * weighted_rate + 0.3 * career_rate
    # If we have a current-season record, use its actual goals/matches for
    # the "current season" tier check; otherwise fall back to most-recent.
    cs = current_season_record or recent_4[0]
    is_proven_star = (career_goals >= 50) or (weighted_rate >= 0.40)
    return {
        # Tracked for backward-compat with existing _prob_to_lock callers.
        "goals": cs["goals"],
        "matches": cs["matches"],
        "rate_per_match": blended_rate,
        "rating": cs["rating"],
        "assists": cs["assists"],
        # New multi-season fields used by the upgraded lock calibrator.
        "weighted_rate": weighted_rate,
        "career_rate": career_rate,
        "career_goals": career_goals,
        "career_matches": career_matches,
        "current_season_goals": cs["goals"],
        "current_season_matches": cs["matches"],
        "seasons_used": len(recent_4),
        "is_proven_star": is_proven_star,
        "last_5_seasons": recent_4[:5],
        "season": cs["season"],
        "first_name": cached.get("firstName"),
        "last_name": cached.get("lastName"),
        "position": (cached.get("position") or "").lower(),
        "market_value": cached.get("marketValue"),
    }


def _stats_to_rate(entry: dict, player_profile: dict) -> Optional[dict]:
    stats = {(s.get("name") or "").lower(): s.get("value") for s in (entry.get("stats") or [])}
    matches = _to_int(stats.get("matches played"))
    goals = _to_int(stats.get("goals scored"))
    if matches < 1:
        return None
    return {
        "goals": goals,
        "matches": matches,
        "rate_per_match": goals / matches,
        "rating": _to_float(stats.get("rating")),
        "assists": _to_int(stats.get("assists")),
        "first_name": player_profile.get("firstName"),
        "last_name": player_profile.get("lastName"),
        "position": (player_profile.get("position") or "").lower(),
        "market_value": player_profile.get("marketValue"),
        "season": entry.get("season"),
    }


def _to_int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _to_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# ─────────────────────── Synthetic pick generator ───────────────────────


async def compute_anytime_scorer_picks(
    db,
    sport_key: str,
    home_team: str,
    away_team: str,
    event_id: str,
    kickoff_iso: str,
    home_form: Optional[dict] = None,
    away_form: Optional[dict] = None,
    max_per_side: int = 4,
) -> list[dict]:
    """Build synthetic anytime-goal-scorer picks for ALL scoring-position
    players on both sides.

    Args:
      sport_key: The Odds API sport key (must be in LEAGUE_MAP).
      home_team, away_team: as reported by The Odds API.
      event_id: Odds API event id (used as pick_id seed for de-dupe).
      kickoff_iso: ISO 8601 commence_time.
      home_form, away_form: pre-resolved standings rows for opponent-defence
        adjustments (optional; falls back to neutral if missing).
      max_per_side: cap per team to keep slate manageable.

    Returns a list of pick dicts ready to merge into the main pipeline.
    """
    if sport_key not in LEAGUE_MAP:
        return []
    country, comp, season = LEAGUE_MAP[sport_key]

    home_resolved = await _resolve_team_id(db, country, comp, season, home_team)
    away_resolved = await _resolve_team_id(db, country, comp, season, away_team)

    picks: list[dict] = []
    if home_resolved:
        picks.extend(await _picks_for_side(
            db, country, comp, season, home_resolved, home_team,
            opp_form=away_form, side="home", event_id=event_id,
            home_team=home_team, away_team=away_team, kickoff_iso=kickoff_iso,
            max_count=max_per_side,
        ))
    if away_resolved:
        picks.extend(await _picks_for_side(
            db, country, comp, season, away_resolved, away_team,
            opp_form=home_form, side="away", event_id=event_id,
            home_team=home_team, away_team=away_team, kickoff_iso=kickoff_iso,
            max_count=max_per_side,
        ))

    # ── TheSportsDB fallback (2026-06-27) ──
    # When sportdb.dev produced zero picks (quota exhausted on their
    # free plan returns 402 → empty squad → empty picks), fall back to
    # the user-paid TheSportsDB v1 API for roster + team-strength data.
    # This keeps lower-tier leagues (CSL, MLS, J-League…) on the board
    # even when our primary data source is throttled. Top leagues
    # (EPL, La Liga, …) where sportdb.dev was producing picks before
    # are UNAFFECTED — this only kicks in when picks == 0.
    if not picks:
        try:
            import thesportsdb_scorer as _tsdb
            # Use the USER-FACING league label so picks land under the same
            # league name the frontend / SportFilterBar uses (e.g. "China
            # Super League" not "super-league:nc9yRmcn"). Falls back to
            # the internal `comp` slug if we don't have a label mapping.
            try:
                from sports_engine import LEAGUE_LABELS as _LL
                league_label = _LL.get(sport_key, comp)
            except Exception:
                league_label = comp
            tsdb_picks = await _tsdb.compute_anytime_scorer_picks(
                db,
                home_team=home_team, away_team=away_team,
                event_id=event_id, kickoff_iso=kickoff_iso,
                league=league_label, sport_key=sport_key,
                max_per_side=max_per_side,
            )
            if tsdb_picks:
                logger.info(
                    "TheSportsDB fallback produced %d goalscorer picks for %s @ %s",
                    len(tsdb_picks), away_team, home_team,
                )
                picks.extend(tsdb_picks)
        except Exception as e:
            logger.warning("TheSportsDB fallback failed for %s @ %s: %s",
                           away_team, home_team, e)
    return picks


async def _picks_for_side(
    db, country: str, comp: str, season: str,
    team_resolved: tuple[str, str], team_name: str,
    opp_form: Optional[dict],
    side: str, event_id: str, home_team: str, away_team: str,
    kickoff_iso: str, max_count: int,
) -> list[dict]:
    team_id, team_slug = team_resolved
    squad = await get_team_squad(db, team_slug, team_id)
    if not squad:
        return []
    # Filter to scoring positions
    candidates = []
    for p in squad:
        pos = (p.get("position") or "").lower()
        if pos in _SCORING_POSITIONS:
            candidates.append(p)
    if not candidates:
        return []
    # Sort by position priority (Forwards/Attackers first), then by jersey.
    # This ensures we fetch the most-likely-scorers FIRST so the credit cap
    # below doesn't waste calls on midfielders before forwards.
    def _sort_key(p: dict):
        pos = (p.get("position") or "").lower()
        pri = _POSITION_PRIORITY.get(pos, 2)
        try:
            jn = int(p.get("jerseyNumber") or 99)
        except (ValueError, TypeError):
            jn = 99
        return (pri, jn)
    candidates.sort(key=_sort_key)
    # Cap candidates we'll look up — top-N forwards, then midfielders if room.
    candidates = candidates[: max_count * 3]

    # Opponent defence adjustment.
    opp_concede_rate = 1.30  # neutral baseline (~1.3 goals/match avg)
    if opp_form and isinstance(opp_form, dict):
        gpm = opp_form.get("goals_against", 0) / max(opp_form.get("matches", 1), 1)
        if gpm > 0:
            opp_concede_rate = gpm
    # Boost rate: opponent concedes 2.0/g vs neutral 1.3 → 1.54× multiplier
    # capped to 1.6 so a bottom-tier defence doesn't blow up the pick math.
    defence_mult = min(opp_concede_rate / 1.30, 1.60)

    out: list[dict] = []
    # ── ESPN live active-player filter (user request 2026-06-27) ──
    # For CSL specifically, ESPN's `chn.1` endpoints give us the
    # authoritative active roster + leaders. Any candidate whose name
    # ESPN explicitly marks as INACTIVE (e.g. Guy Mbenza after a transfer)
    # is dropped here BEFORE we waste a SportDB lookup on them. Unknown
    # players (not in ESPN cache) pass through — legacy heuristics still
    # apply so we never accidentally nuke an entire match's pick board.
    csl_filter_active: Optional["callable"] = None  # type: ignore
    csl_get_live_form: Optional["callable"] = None  # type: ignore
    if (comp or "").lower().startswith("china:") or comp == "soccer_china_superleague":
        try:
            import csl_espn_live as _csl_live
            csl_filter_active = _csl_live.is_player_currently_active
            csl_get_live_form = _csl_live.get_live_form
        except Exception:
            csl_filter_active = None

    for player in candidates:
        # ── CSL retired-player block ──
        if csl_filter_active is not None:
            verdict = csl_filter_active(
                _format_player_name(player, None) or player.get("name") or "",
                team_hint=team_name,
            )
            if verdict is False:
                logger.debug(
                    "CSL ESPN: dropping inactive player %s (team=%s)",
                    player.get("name"), team_name,
                )
                continue  # ESPN says this player is not active → skip
        rate = await get_player_goal_rate(
            db, player.get("slug") or "", player.get("id") or "",
            comp, season,
        )
        # ── ESPN-derived form override ──
        # If ESPN has a current-season scoring rate for this player, prefer
        # it over SportDB / seed data — ESPN is the most authoritative
        # source for "what's happening THIS season".
        if csl_get_live_form is not None:
            live = csl_get_live_form(
                _format_player_name(player, rate) or player.get("name") or ""
            )
            if live and (live.get("matches") or 0) >= 3 and (live.get("rate_per_match") or 0) > 0:
                rate = {
                    "rate_per_match": live["rate_per_match"],
                    "goals": live["goals"],
                    "matches": live["matches"],
                    "source": live["source"],
                }
        if not rate:
            continue
        base = rate["rate_per_match"]
        if base <= 0.0:
            continue
        lam = base * defence_mult
        # Anytime scorer = P(N>=1) where N ~ Poisson(λ).
        prob = 1.0 - math.exp(-lam)
        prob = max(0.0, min(prob, SYNTH_MAX_PROB))
        if prob < SYNTH_MIN_PROB:
            continue
        player_full_name = _format_player_name(player, rate)
        # Synthesise a market-style line. We have no bookmaker, so emit a
        # "MODEL-ONLY" tag and store the implied price equivalent.
        implied_odds = _prob_to_american(prob)
        pick = {
            # Unique-ish: event + player; downstream dedupe handles collisions.
            "id": f"synth_csl_{event_id}_{player.get('id')}",
            "pick_id": f"synth_csl_{event_id}_{player.get('id')}",
            "sport": "Soccer",
            "league": _LEAGUE_LABELS.get(comp, "Soccer"),
            # `event` string mirrors the format used by the bookmaker-derived
            # soccer picks (`"Away @ Home"`) so the home board, dedupe, and
            # group-by-event UI treat these synthetic picks identically to
            # the real ones — no special-casing needed downstream.
            "event": f"{away_team} @ {home_team}",
            "market": f"{player_full_name} Anytime Goal Scorer",
            "selection": player_full_name,
            # `player_name` is what LockPickCard renders as the headline —
            # surface explicitly so the synthetic pick shows the player's
            # name instead of "?" in the home feed (iter-62 fix).
            "player_name": player_full_name,
            "home_team": home_team,
            "away_team": away_team,
            "player_team": team_name,
            "player_side": side,
            "event_time": kickoff_iso,
            "book_odds": implied_odds,
            "book_implied_prob": prob,
            # `implied_probability` is the field name the dedupe step 5
            # market-favourite check reads — surface it explicitly so the
            # downstream guard recognises high-confidence synth picks.
            "implied_probability": round(prob * 100, 1),
            "win_probability": round(prob * 100, 1),
            "win_probability_raw": round(prob * 100, 1),
            "sim_win_probability": round(prob * 100, 1),
            "edge_percent": 0.0,           # model-only — no market to gauge edge
            "lock_score": _prob_to_lock(prob, rate),
            "lock_score_v2": _prob_to_lock(prob, rate),
            "grade": _prob_to_grade(prob),
            "confidence": _prob_to_grade(prob),
            "status": "pending",
            "no_bet": False,
            "is_model_only": True,         # frontend renders MODEL badge
            "is_synthetic_scorer": True,   # specific tag for filtering
            "is_long_shot": True,           # treat like other goalscorer picks
            "source": "sportdb_scorer_v1",
            # Why this pick — surfaced verbatim in the UI.
            "key_insights": _build_insights(player, rate, defence_mult, opp_form, lam, prob),
            "sportdb_signal": (
                f"📊 SportDB: {player_full_name} has scored {rate['goals']} goal"
                f"{'s' if rate['goals'] != 1 else ''} in {rate['matches']} "
                f"{rate.get('season') or season} {_LEAGUE_LABELS.get(comp, 'league')} "
                f"appearances ({rate['rate_per_match']*100:.0f}% rate)."
            ),
        }
        out.append(pick)
        if len(out) >= max_count:
            break
    return out


# ─────────────────── Career enrichment for bookmaker picks ───────────────────


def _norm_name(name: str) -> str:
    """Normalise a player name for fuzzy matching across SportDB ↔ Odds API.

    Odds API: "Sadio Mané", SportDB: "Mané S." or "Sadio Mane" — normalise
    to lowercase ASCII, strip punctuation, collapse whitespace. Compare on
    both full normalised form AND last-name-token sets.
    """
    if not name:
        return ""
    n = unicodedata.normalize("NFKD", name)
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = n.lower()
    n = re.sub(r"[^a-z0-9 ]+", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def _name_match(query: str, candidates: list[str]) -> Optional[int]:
    """Find best candidate index for a player name. Returns None if no match."""
    nq = _norm_name(query)
    if not nq:
        return None
    nq_tokens = set(nq.split())
    nq_last = nq.split()[-1] if nq.split() else ""
    # First pass: exact full normalised match
    for i, c in enumerate(candidates):
        if _norm_name(c) == nq:
            return i
    # Second pass: same last-name token
    for i, c in enumerate(candidates):
        c_tokens = set(_norm_name(c).split())
        if nq_last and nq_last in c_tokens and len(nq_tokens & c_tokens) >= 1:
            return i
    return None


async def enrich_bookmaker_scorer_pick(
    db,
    pick: dict,
    sport_key: str,
) -> dict:
    """Enrich a bookmaker-derived goalscorer pick with SportDB career data.

    This is what gives Mané his lock floor of 88+ on Senegal-Iraq, even
    though the model originally scored him based on bookmaker implied prob
    alone. Pulls multi-source career (league + national team + intl cups)
    via `get_player_goal_rate`, then re-tiers the lock using the same
    classifier as the synthetic CSL picks.

    Rules:
      • ONLY boost — never downgrade. If career data suggests Mané is a
        Tier S player (career_goals ≥ 100) and his current pick has lock
        72, push it up to max(72, 92). If career data is weak/missing,
        the pick stays as-is.
      • Add a `sportdb_career_signal` field and a tier-badge insight so
        users SEE the data.
      • Best-effort — failures don't change the pick.

    Args:
      pick: bookmaker-derived pick (must have `selection` and `player_team`).
      sport_key: Odds API sport key — must be in LEAGUE_MAP.

    Returns the (possibly mutated) pick.
    """
    if sport_key not in LEAGUE_MAP:
        return pick
    selection = pick.get("selection") or ""
    player_team = (
        pick.get("player_team") or
        pick.get("home_team") or
        pick.get("away_team") or ""
    )
    if not selection or not player_team:
        return pick
    country, comp, season = LEAGUE_MAP[sport_key]
    # 1. Resolve team via standings
    team_resolved = await _resolve_team_id(db, country, comp, season, player_team)
    if not team_resolved:
        # Try the OTHER team (player_team may be reversed for home/away picks).
        alt = pick.get("away_team") if player_team == pick.get("home_team") else pick.get("home_team")
        if alt and alt != player_team:
            team_resolved = await _resolve_team_id(db, country, comp, season, alt)
        if not team_resolved:
            return pick
    team_id, team_slug = team_resolved
    # 2. Get squad — find this player by name match.
    squad = await get_team_squad(db, team_slug, team_id)
    if not squad:
        return pick
    candidates = [f"{p.get('firstName') or ''} {p.get('lastName') or ''}".strip()
                   for p in squad]
    # Also try last-name-only candidates
    last_candidates = [p.get("lastName") or "" for p in squad]
    idx = _name_match(selection, candidates) or _name_match(selection, last_candidates)
    if idx is None or idx >= len(squad):
        return pick
    matched_player = squad[idx]
    # 3. Fetch career rate (multi-source: league + national_team + intl_cups)
    rate = await get_player_goal_rate(
        db, matched_player.get("slug") or "", matched_player.get("id") or "",
        comp, season,
    )
    if not rate:
        return pick
    # 4. Compute the TIER-IMPLIED lock floor — never downgrade.
    # Use the pick's existing win_probability if present (bookmaker-derived).
    book_prob = float(pick.get("win_probability") or 0.0) / 100.0
    tier_lock = _prob_to_lock(book_prob, rate)
    # Only boost if the new lock is higher AND the player has substance.
    if rate.get("career_goals", 0) < 10 and rate.get("weighted_rate", 0) < 0.15:
        # Not enough evidence to override — skip.
        return pick
    current_lock = float(pick.get("lock_score") or 0.0)
    if tier_lock > current_lock:
        # Boost — but cap the swing at +12 lock points to avoid runaway shifts.
        delta = min(tier_lock - current_lock, 12.0)
        new_lock = current_lock + delta
        pick["lock_score"] = round(new_lock, 1)
        if isinstance(pick.get("lock_score_v2"), (int, float)):
            pick["lock_score_v2"] = round(float(pick["lock_score_v2"]) + delta, 1)
        pick["sportdb_career_boost"] = round(delta, 1)
    # 5. Attach insight + signal regardless of boost direction.
    career_goals = rate.get("career_goals", 0)
    career_matches = rate.get("career_matches", 0)
    weighted_rate = rate.get("weighted_rate", 0.0)
    current_g = rate.get("current_season_goals", 0)
    current_m = rate.get("current_season_matches", 0)
    pname = selection
    tier_badge = _tier_label(career_goals, weighted_rate)
    insight_lines: list[str] = []
    if tier_badge:
        insight_lines.append(tier_badge)
    insight_lines.append(
        f"📊 SportDB career: {pname} has {career_goals} goals in "
        f"{career_matches} career league/NT/intl-cup matches "
        f"({(career_goals/career_matches*100 if career_matches else 0):.0f}% career rate)."
    )
    if rate.get("seasons_used", 0) >= 2:
        insight_lines.append(
            f"📈 Last {rate.get('seasons_used')} seasons weighted: "
            f"{weighted_rate*100:.0f}% per-match scoring rate. "
            f"Current season: {current_g}g in {current_m}m "
            f"({(current_g/current_m*100 if current_m else 0):.0f}%)."
        )
    existing = pick.setdefault("key_insights", [])
    if not isinstance(existing, list):
        existing = []
        pick["key_insights"] = existing
    # Prepend so career context shows FIRST in the why-this-pick.
    pick["key_insights"] = insight_lines + existing
    pick["sportdb_career_signal"] = (
        f"{career_goals} career goals · {weighted_rate*100:.0f}% weighted "
        f"recent rate · {tier_badge.split(' ')[1] if tier_badge else 'Standard'} tier"
    )
    pick["sportdb_career_rate"] = rate.get("rate_per_match")
    return pick


def _tier_label(career_goals: int, weighted_rate: float) -> str:
    """Return the tier badge string for the Why This Pick insight."""
    if career_goals >= 200 or weighted_rate >= 0.65:
        return "🏆 ELITE STAR — 200+ career goals or 65%+ scoring rate."
    if career_goals >= 100 or weighted_rate >= 0.55:
        return "🥇 ALL-TIME GREAT — 100+ career goals, proven match-winner."
    if career_goals >= 50 or weighted_rate >= 0.40:
        return "⭐ PROVEN SCORER — 50+ career goals across leagues."
    if career_goals >= 25 or weighted_rate >= 0.25:
        return "🥈 REGULAR GOAL THREAT — 25+ career goals."
    if career_goals >= 10:
        return "📍 RECOGNISED SCORER — 10+ career goals."
    return ""


# ─────────────────── Opposition GK quality enrichment ───────────────────


async def get_team_top_goalkeeper(db, country: str, comp: str, season: str,
                                   team_name: str) -> Optional[dict]:
    """Find the OPPOSITION team's #1 goalkeeper and return their season rating.

    Returns {name, rating, matches, slug, id} or None if no GK data available.
    The "#1 GK" is whoever has the most matches played in the league — same
    way coaches actually pick their starter.

    Cached 7 days (the GK stats refresh slowly).
    """
    if not team_name:
        return None
    cache_key = f"top_gk:{country}:{comp}:{season}:{_norm(team_name)}"
    cached = await _cache_get(db, cache_key, _PLAYER_TTL)
    if cached is not None:
        return cached if cached else None
    team_resolved = await _resolve_team_id(db, country, comp, season, team_name)
    if not team_resolved:
        return None
    team_id, team_slug = team_resolved
    # Pull the full squad (cached separately for 24h)
    full_squad = await _get(f"/team/{team_slug}/{team_id}", db=db)
    if not isinstance(full_squad, dict):
        return None
    squad_groups = full_squad.get("squad") or []
    gks: list[dict] = []
    for grp in squad_groups:
        if (grp.get("tournamentType") or "").lower() != "league":
            continue
        for p in (grp.get("players") or []):
            if (p.get("position") or "").lower() == "goalkeepers":
                gks.append(p)
    if not gks:
        await _cache_set(db, cache_key, {})  # cache "no data" so we don't re-probe
        return None
    # For each GK, fetch their season rating. Cap at the top 3 by jersey
    # number (lower = likely starter). Most teams have 2-3 GKs total.
    candidates = []
    for gk in gks[:3]:
        rate = await get_player_goal_rate(
            db, gk.get("slug") or "", gk.get("id") or "", comp, season,
        )
        if rate and rate.get("current_season_matches", 0) >= 3:
            candidates.append({
                "name": f"{gk.get('firstName') or ''} {gk.get('lastName') or ''}".strip(),
                "rating": rate.get("rating", 6.5),
                "matches": rate.get("current_season_matches", 0),
                "slug": gk.get("slug"),
                "id": gk.get("id"),
            })
    if not candidates:
        await _cache_set(db, cache_key, {})
        return None
    # Top GK = most matches (starter), tie-break by rating
    candidates.sort(key=lambda x: (-x["matches"], -x["rating"]))
    result = candidates[0]
    await _cache_set(db, cache_key, result)
    return result


def _gk_lock_adjustment(gk_rating: float) -> tuple[float, str]:
    """Translate GK rating to a lock-score adjustment + descriptive label.

    Returns (delta, tier_label).
    Strong GK depresses opposing-scorer probability — lock penalty.
    Weak GK boosts opposing-scorer probability — lock boost.
    """
    if gk_rating >= 7.3:
        return (-3.0, f"🛡️ ELITE GK ({gk_rating:.1f}/10) — tough to score on")
    if gk_rating >= 7.0:
        return (-1.5, f"🛡️ Above-average GK ({gk_rating:.1f}/10)")
    if gk_rating >= 6.7:
        return (0.0, f"🟰 Average GK ({gk_rating:.1f}/10)")
    if gk_rating >= 6.4:
        return (+1.5, f"⚠️ Below-average GK ({gk_rating:.1f}/10) — exploitable")
    return (+3.0, f"🚪 Poor GK ({gk_rating:.1f}/10) — leaky net")


async def enrich_pick_with_gk_quality(
    db, pick: dict, sport_key: str,
) -> dict:
    """Adjust a goalscorer pick's lock_score based on the opposition GK rating.

    Player_team and opposition_team are derived from the pick's home/away
    fields. Best-effort — failures don't touch the pick.
    """
    if sport_key not in LEAGUE_MAP:
        return pick
    country, comp, season = LEAGUE_MAP[sport_key]
    # Determine which side the scorer is on, then opposition is the OTHER team.
    player_team = pick.get("player_team") or ""
    home_team = pick.get("home_team") or ""
    away_team = pick.get("away_team") or ""
    if not player_team or not home_team or not away_team:
        return pick
    if _norm(player_team) == _norm(home_team):
        opp_team = away_team
    elif _norm(player_team) == _norm(away_team):
        opp_team = home_team
    else:
        # Best guess — assume player's on the home side if we can't tell
        opp_team = away_team if _norm(player_team) in _norm(home_team) else home_team
    gk = await get_team_top_goalkeeper(db, country, comp, season, opp_team)
    if not gk:
        return pick
    delta, label = _gk_lock_adjustment(float(gk.get("rating", 6.5)))
    # Apply the adjustment (clamp to 55-99 range)
    for k in ("lock_score", "lock_score_v2"):
        v = pick.get(k)
        if isinstance(v, (int, float)):
            pick[k] = round(max(55.0, min(float(v) + delta, 99.0)), 1)
    # Surface the GK insight
    insight = f"{label} — {gk.get('name')} on {opp_team}. Lock {'+' if delta >= 0 else ''}{delta:.1f}."
    existing = pick.setdefault("key_insights", [])
    if isinstance(existing, list):
        # Insert AFTER the tier badge / career signal (which are first)
        # so the GK note follows the player-quality context.
        existing.insert(min(2, len(existing)), insight)
    pick["sportdb_gk_signal"] = (
        f"Opposing GK {gk.get('name')} rating {gk.get('rating'):.1f}/10 "
        f"({gk.get('matches')} matches) → lock {delta:+.1f}"
    )
    pick["sportdb_gk_rating"] = gk.get("rating")
    pick["sportdb_gk_adjustment"] = delta
    return pick


async def _self_test():
    """Manual smoke test — pick CSL match Beijing Guoan vs Meizhou Hakka."""
    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    from motor.motor_asyncio import AsyncIOMotorClient
    mc = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = mc[os.environ["DB_NAME"]]
    picks = await compute_anytime_scorer_picks(
        db,
        sport_key="soccer_china_superleague",
        home_team="Beijing Guoan",
        away_team="Meizhou Hakka",
        event_id="test-csl-1",
        kickoff_iso="2026-06-27T11:00:00Z",
    )
    print(f"Generated {len(picks)} picks:")
    for p in picks:
        print(f"  • {p['selection']} ({p['player_team']}) — "
              f"prob={p['win_probability']:.1f}% lock={p['lock_score']:.1f} grade={p['grade']}")
        print(f"      {p.get('sportdb_signal') or ''}")


if __name__ == "__main__":
    asyncio.run(_self_test())


_LEAGUE_LABELS = {
    "premier-league:dYlOSQOD":  "Premier League",
    "laliga:8tUjE6FL":          "La Liga",
    "bundesliga:lpwAFsK1":      "Bundesliga",
    "serie-a:dInOZ2Yo":         "Serie A",
    "ligue-1:Wt4ehJpS":         "Ligue 1",
    "eredivisie:8H1huJyM":      "Eredivisie",
    "liga-portugal:KMabNT3K":   "Liga Portugal",
    "super-league:nc9yRmcn":    "China Super League",
    "mls:CQv5qrFt":             "MLS",
    "serie-a-betano:Yq4hUnzQ":  "Brasileirão Série A",
    "serie-b:vRtLP6rs":         "Brasileirão Série B",
    "liga-mx:bm2Vlsfl":         "Liga MX",
    "eliteserien:GOvB22xg":     "Eliteserien",
    "allsvenskan:nXxWpLmT":     "Allsvenskan",
    "veikkausliiga:zTH7XBoF":   "Veikkausliiga",
    "superliga:O6W7GIaF":       "Danish Superliga",
    "j1-league:K0qPDOLA":       "J1 League",
    "k-league-1:rFcdLNRl":      "K League 1",
    "a-league:CIA9TpvC":        "A-League Men",
    "saudi-pro-league:p7CtIfx0": "Saudi Pro League",
    "liga-profesional:vTYz5w5l": "Liga Profesional Argentina",
    "champions-league:xGrwqq16": "UEFA Champions League",
    "europa-league:ClDjv3V5":   "UEFA Europa League",
    "world-cup:eBHRoOnX":       "FIFA World Cup",
    "club-world-cup:CGwEMb1u":  "FIFA Club World Cup",
}


def _format_player_name(player: dict, rate: dict) -> str:
    fn = player.get("firstName") or rate.get("first_name") or ""
    ln = player.get("lastName") or rate.get("last_name") or ""
    name = f"{fn} {ln}".strip()
    return name or (player.get("slug") or "Unknown").replace("-", " ").title()


def _prob_to_american(p: float) -> int:
    """Convert win probability to nearest American odds (informational, since
    there's no real book). E.g. 0.40 → +150, 0.60 → -150."""
    p = max(min(p, 0.95), 0.05)
    if p >= 0.5:
        return -int(round((p / (1 - p)) * 100))
    return int(round(((1 - p) / p) * 100))


def _prob_to_lock(prob: float, rate: dict) -> float:
    """Lock score for model-only picks — TIER-RELATIVE confidence scale.

    Per user 2026-06-26: lock_score is NOT a literal win-probability — it's
    a confidence ceiling where 99 = "best pick available in this market".
    A career-proven star (Leonardo: 21g, 21g, 19g over last 3 seasons;
    Fabio Abreu: 28g/30m last season; Salah: 50+ Egypt NT goals over a
    decade) should hit 95+ even if they're slow-starting the current
    season — they're STILL the best anytime-scorer pick in their match.

    ── PROBABILITY-DRIVEN OVERRIDE (CSL/MLS/J-League/lower-tier fix) ──
    For leagues where SportDB lacks rich career data (CSL, MLS, lower
    divisions etc.) the career_goals tier collapses to D (~58 lock) even
    when the model + simulation say the player has a 45-65% chance to
    score. Per user 2026-06-26: "with china super league I gave you their
    history we should be able to create 95-99 lock picks with history".
    Solution: when the model probability is strong, lift to the tier the
    probability earns — current-season goals + sim consensus IS history.

      prob ≥ 0.60  →  Tier S+ floor (95+)   "near-lock — top scorer-rank pick"
      prob ≥ 0.50  →  Tier S  floor (92+)   "premium model lock"
      prob ≥ 0.42  →  Tier A  floor (88+)   "strong evidence"
      prob ≥ 0.35  →  Tier B  floor (80+)   "above-average"

    Tier classification (uses both weighted multi-season rate AND career
    goal total — covers both "currently hot" and "lifetime star" cases):

      Tier  Triggers (ANY of)                           Lock anchor
      ────  ──────────────────────────────────────────  ───────────
      S+    career_goals ≥ 200  OR  weighted_rate ≥ 0.65   95-99
      S     career_goals ≥ 100  OR  weighted_rate ≥ 0.55   90-95
      A     career_goals ≥ 50   OR  weighted_rate ≥ 0.40   85-90
      B     career_goals ≥ 25   OR  weighted_rate ≥ 0.25   75-85
      C     career_goals ≥ 10   OR  weighted_rate ≥ 0.15   65-75
      D     anything else                                  55-65
    """
    career_goals = rate.get("career_goals", 0) or rate.get("goals", 0)
    weighted_rate = rate.get("weighted_rate") or rate.get("rate_per_match", 0.0)
    rating = rate.get("rating", 0.0)
    matches = rate.get("current_season_matches") or rate.get("matches", 0)

    # Tier base (use the HIGHER of career-goals tier and weighted-rate tier).
    def tier_from_career(g: int) -> tuple[str, float]:
        if g >= 200:  return ("S+", 96.0)
        if g >= 100:  return ("S",  92.0)
        if g >= 50:   return ("A",  88.0)
        if g >= 25:   return ("B",  78.0)
        if g >= 10:   return ("C",  68.0)
        return ("D", 58.0)

    def tier_from_rate(r: float) -> tuple[str, float]:
        if r >= 0.65: return ("S+", 96.0)
        if r >= 0.55: return ("S",  92.0)
        if r >= 0.40: return ("A",  88.0)
        if r >= 0.25: return ("B",  78.0)
        if r >= 0.15: return ("C",  68.0)
        return ("D", 58.0)

    # ── PROBABILITY-DRIVEN TIER FLOOR ─────────────────────────────────
    # Maps the model's win probability into the tier ladder so a
    # 60%-to-score player IS a Tier S+ pick even if SportDB has no
    # career data on him (CSL/MLS top scorer-rank case).
    def tier_from_prob(p: float) -> tuple[str, float]:
        if p >= 0.60: return ("S+", 96.0)
        if p >= 0.50: return ("S",  92.0)
        if p >= 0.42: return ("A",  88.0)
        if p >= 0.35: return ("B",  80.0)
        return ("D", 58.0)

    t1, b1 = tier_from_career(career_goals)
    t2, b2 = tier_from_rate(weighted_rate)
    t3, b3 = tier_from_prob(prob)
    base = max(b1, b2, b3)
    # Quality adjustments
    if rating >= 7.5:
        base += 2
    elif rating >= 7.0:
        base += 1
    if matches and matches < 5 and not (career_goals >= 25):
        # Small-sample penalty ONLY if no career anchor AND prob is weak
        # (would otherwise punish CSL top scorers with 3-game samples).
        if prob < 0.42:
            base -= 6
    # Matchup adjustment using probability (encodes opponent defence).
    if prob >= 0.55:
        base += 3
    elif prob >= 0.45:
        base += 1
    elif prob < 0.25:
        base -= 3
    return float(max(55.0, min(base, 99.0)))


def _prob_to_grade(prob: float) -> str:
    if prob >= 0.55:
        return "A"
    if prob >= 0.42:
        return "B+"
    if prob >= 0.35:
        return "B"
    return "C+"


def _build_insights(player: dict, rate: dict, defence_mult: float,
                     opp_form: Optional[dict], lam: float, prob: float) -> list[str]:
    out: list[str] = []
    pname = _format_player_name(player, rate)
    # ── Tier tag ── shown FIRST so users see the calibre at a glance.
    goals = rate.get("goals", 0)
    if goals >= 10:
        out.append(f"🏆 GOLDEN BOOT TIER ({goals} goals this season) — league-leading scorer, always playable.")
    elif goals >= 8:
        out.append(f"🥇 LEAGUE TOP-5 SCORER ({goals} goals this season).")
    elif goals >= 5:
        out.append(f"🥈 LEAGUE TOP-15 SCORER ({goals} goals this season).")
    out.append(
        f"📈 {pname}: {rate['goals']} goal{'s' if rate['goals'] != 1 else ''} "
        f"in {rate['matches']} matches → {rate['rate_per_match']*100:.0f}% per-game scoring rate."
    )
    if rate.get("rating", 0) >= 7.0:
        out.append(f"⭐ Rated {rate['rating']:.1f}/10 — a primary attacking option, not a depth piece.")
    if rate.get("assists", 0) >= rate.get("goals", 0):
        out.append(f"🎯 {pname} has {rate['assists']} assists — playmaker profile, may be on free-kick / corner duty.")
    if defence_mult >= 1.30:
        opp_name = (opp_form or {}).get("team_name") or "Opponent"
        gpm = (opp_form or {}).get("goals_against", 0) / max((opp_form or {}).get("matches", 1), 1)
        out.append(
            f"🛡️ {opp_name} concedes {gpm:.2f} goals/match — defence-quality multiplier of "
            f"{defence_mult:.2f}× boosts the scoring rate to ~{lam*100:.0f}%."
        )
    elif defence_mult < 1.0:
        opp_name = (opp_form or {}).get("team_name") or "Opponent"
        out.append(f"⚠️ {opp_name} runs a tight defence — multiplier {defence_mult:.2f}× drags scoring rate down.")
    out.append(
        f"🎲 Model: λ={lam:.2f} expected goals → P(anytime) = 1 - e^-λ = {prob*100:.1f}%. "
        f"No bookmaker line — model-only signal."
    )
    return out


# ─────────────────────── Quick self-test entry point ───────────────────────


async def _self_test():
    """Manual smoke test — pick CSL match Beijing Guoan vs Meizhou Hakka."""
    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    from motor.motor_asyncio import AsyncIOMotorClient
    mc = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = mc[os.environ["DB_NAME"]]
    picks = await compute_anytime_scorer_picks(
        db,
        sport_key="soccer_china_superleague",
        home_team="Beijing Guoan",
        away_team="Meizhou Hakka",
        event_id="test-csl-1",
        kickoff_iso="2026-06-27T11:00:00Z",
    )
    print(f"Generated {len(picks)} picks:")
    for p in picks:
        print(f"  • {p['selection']} ({p['player_team']}) — "
              f"prob={p['win_probability']:.1f}% lock={p['lock_score']:.1f} grade={p['grade']}")
        print(f"      {p['sportdb_signal']}")


if __name__ == "__main__":
    asyncio.run(_self_test())
