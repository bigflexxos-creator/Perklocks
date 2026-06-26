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
    """Fetch a player's career stats and extract goals/matches for the given
    competition + season.

    Returns dict {goals, matches, rate_per_match, position, last_name,
    market_value} or None if no data.
    """
    if not player_id or not player_slug:
        return None
    key = f"player:{player_slug}:{player_id}"
    cached = await _cache_get(db, key, _PLAYER_TTL)
    if cached is None:
        data = await _get(f"/player/{player_slug}/{player_id}", db=db)
        if not isinstance(data, dict):
            return None
        cached = data
        await _cache_set(db, key, cached)
    careers = (cached.get("careers") or {}).get("league") or []
    # Extract competition slug from "super-league:nc9yRmcn" → "super-league"
    target_slug = comp_slug_id.split(":")[0]
    rate: Optional[dict] = None
    # Try the active season first; if no data, fall back to most recent season.
    for entry in careers:
        if (entry.get("competitionSlug") or "").lower() != target_slug:
            continue
        if (entry.get("season") or "") != season:
            continue
        rate = _stats_to_rate(entry, cached)
        if rate and rate.get("matches", 0) > 0:
            break
    if not rate:
        # Fallback: most recent season for the same competition.
        for entry in careers:
            if (entry.get("competitionSlug") or "").lower() != target_slug:
                continue
            cand = _stats_to_rate(entry, cached)
            if cand and cand.get("matches", 0) >= 3:
                rate = cand
                rate["fallback_season"] = entry.get("season")
                break
    return rate


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
    if not home_resolved and not away_resolved:
        return []

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
    for player in candidates:
        rate = await get_player_goal_rate(
            db, player.get("slug") or "", player.get("id") or "",
            comp, season,
        )
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


_LEAGUE_LABELS = {
    "premier-league:dYlOSQOD":  "Premier League",
    "laliga:8tUjE6FL":          "La Liga",
    "bundesliga:lpwAFsK1":      "Bundesliga",
    "serie-a:dInOZ2Yo":         "Serie A",
    "ligue-1:Wt4ehJpS":         "Ligue 1",
    "eredivisie:8H1huJyM":      "Eredivisie",
    "liga-portugal:KMabNT3K":   "Liga Portugal",
    "super-league:nc9yRmcn":    "Chinese Super League",
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
    the user's confidence ceiling where 99 = "best pick available in this
    market". So a league-leading scorer (Cadiz J / Taty Maritu / Cryzan at
    12g) should hit 95+ even though anytime-scorer prob is mathematically
    bounded by Poisson at ~70%. They earn the high lock because they're
    the BEST anytime-scorer pick the model can find in their league.

    Calibration (tier-relative, not absolute):
      ≥12 goals (Golden Boot leader)         → 95-99
      10-11 goals (top-3)                    → 90-95
      8-9 goals  (top-5)                     → 85-90
      5-7 goals  (top-15)                    → 75-85
      3-4 goals  (regular starter)           → 65-75
      <3 goals   (depth / variance)          → 55-65

    The PROBABILITY itself still flows through as `win_probability` on the
    pick so users see the underlying scoring rate, but the lock ranks the
    PICK QUALITY (calibre-of-player × match-fit × evidence) rather than
    coin-flip odds.
    """
    goals = rate.get("goals", 0)
    matches = rate.get("matches", 0)
    rating = rate.get("rating", 0.0)
    # Tier-driven base lock from absolute goal count this season.
    if goals >= 12:
        # Golden Boot leader — top of the league. Anchor at 96.
        base = 96.0
    elif goals >= 10:
        # Top-3 scorer. Anchor at 92.
        base = 92.0
    elif goals >= 8:
        # Top-5 scorer. Anchor at 88.
        base = 88.0
    elif goals >= 5:
        # Top-15 scorer. Scale 75 → 85 based on rate.
        base = 75.0 + (rate.get("rate_per_match", 0.0) - 0.4) * 50.0
        base = max(75.0, min(base, 85.0))
    elif goals >= 3:
        base = 65.0 + (rate.get("rate_per_match", 0.0) - 0.2) * 50.0
        base = max(65.0, min(base, 75.0))
    else:
        # Depth — prob-driven only.
        base = 55.0 + prob * 25.0
        base = max(55.0, min(base, 65.0))
    # Quality adjustments
    if matches < 5:
        base -= 6   # small-sample penalty
    if rating >= 7.5:
        base += 2   # heavy bonus for elite ratings
    elif rating >= 7.0:
        base += 1
    # Opponent quality also factors in — captured in `prob` (which embeds
    # the defence multiplier), so a tough opponent already dampens the
    # tier base via probability-scaled bands above.
    if prob >= 0.50:
        base += 2   # +2 for matchups where prob crosses 50%
    elif prob < 0.25:
        base -= 3   # bad matchup drags even a top scorer down
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
