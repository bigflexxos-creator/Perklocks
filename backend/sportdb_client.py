"""SportDB.dev client — used to enrich PerksLocks picks with deeper team-form
and player-stat data than The Odds API exposes.

Strategy (trial budget is 1,000 lifetime requests):

  * Cache aggressively — standings TTL 24 h, persisted in MongoDB so server
    restarts don't waste credits.
  * Pre-fetch the top 10 football leagues + a couple of basketball/hockey
    leagues once per day (~10–15 requests/day). Picks pull from cache.
  * Public `lookup_team_form(team_name)` resolves a team across every cached
    league so a Soccer pick from any covered competition gets enrichment.

Returned form dict keys:
    rank, points, ppm, wins, draws, losses, goal_diff, goals_for, goals_against,
    form (list of 'W'/'D'/'L' for last N), form_score (-1.0..1.0), team_id,
    competition.
"""
from __future__ import annotations

import logging
import os
import asyncio
import re
import unicodedata
from datetime import datetime, timezone, timedelta
from typing import Optional, Any

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("lockscore.sportdb")

SPORTDB_KEY = os.getenv("SPORTDB_API_KEY")
SPORTDB_BASE = "https://api.sportdb.dev/api/flashscore"
_TIMEOUT = httpx.Timeout(15.0, connect=8.0)

# Daily budget guard. The trial gives 1,000 lifetime requests; we cap usage to
# 50/day so even a misbehaving refresh can't burn the wallet.
_DAILY_CAP = 60
_BUDGET = {"date": None, "used": 0}

# Top leagues to pre-cache (sport, country slug, country id, comp slug, comp id).
# Country & competition ids were resolved once via the /countries discovery
# endpoint and committed here so we never spend credits re-resolving them.
TOP_FOOTBALL_LEAGUES: list[tuple[str, str, str, str]] = [
    # (display name, country-slug:id, competition-slug:id, season)
    # European Big 5 use cross-year seasons.
    ("Premier League (England)",  "england:198",  "premier-league:dYlOSQOD",  "2025-2026"),
    ("La Liga (Spain)",           "spain:176",    "laliga:8tUjE6FL",          "2025-2026"),
    ("Bundesliga (Germany)",      "germany:81",   "bundesliga:lpwAFsK1",      "2025-2026"),
    ("Serie A (Italy)",           "italy:98",     "serie-a:dInOZ2Yo",         "2025-2026"),
    ("Ligue 1 (France)",          "france:77",    "ligue-1:Wt4ehJpS",         "2025-2026"),
    ("Eredivisie (Netherlands)",  "netherlands:139", "eredivisie:8H1huJyM",   "2025-2026"),
    ("Liga Portugal",             "portugal:155", "liga-portugal:KMabNT3K",   "2025-2026"),
    # Single-year season leagues (summer schedule). These are where the Odds
    # API has actual June volume so they matter the most for our trial.
    ("MLS (USA)",                 "usa:200",      "mls:CQv5qrFt",             "2026"),
    ("Brazil Série A",            "brazil:39",    "serie-a-betano:Yq4hUnzQ",  "2026"),
    ("Brazil Série B",            "brazil:39",    "serie-b:vRtLP6rs",         "2026"),
    ("Liga MX (Mexico)",          "mexico:128",   "liga-mx:bm2Vlsfl",         "2025-2026"),
    ("Eliteserien (Norway)",      "norway:145",   "eliteserien:GOvB22xg",     "2026"),
    ("Allsvenskan (Sweden)",      "sweden:181",   "allsvenskan:nXxWpLmT",     "2026"),
    ("Premier Division (Ireland)", "ireland:96",  "premier-division:naHiWdnt", "2026"),
    # European club competitions
    ("UEFA Champions League",     "europe:6",     "champions-league:xGrwqq16", "2025-2026"),
    ("UEFA Europa League",        "europe:6",     "europa-league:ClDjv3V5",   "2025-2026"),
]


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


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _budget_ok() -> bool:
    today = _today_str()
    if _BUDGET["date"] != today:
        _BUDGET["date"] = today
        _BUDGET["used"] = 0
    return _BUDGET["used"] < _DAILY_CAP


def _budget_inc():
    _BUDGET["used"] += 1


# ─────────────────────────── HTTP helper ───────────────────────────


async def _get(path: str) -> Optional[Any]:
    if not SPORTDB_KEY:
        return None
    if not _budget_ok():
        logger.warning("SportDB daily budget hit — skipping %s", path)
        return None
    url = path if path.startswith("http") else f"{SPORTDB_BASE}{path}"
    # `path` may already include `/api/flashscore` (when echoing the API's own
    # "link" hrefs) — strip duplication.
    url = url.replace("/api/flashscoreapi/flashscore", "/api/flashscore")
    if not url.startswith("http"):
        url = f"https://api.sportdb.dev{url}"
    headers = {"X-API-Key": SPORTDB_KEY}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=headers) as cx:
            r = await cx.get(url)
            _budget_inc()
            if r.status_code != 200:
                logger.warning("SportDB %s → %d", url, r.status_code)
                return None
            return r.json()
    except Exception as e:
        logger.warning("SportDB %s failed: %s", url, e)
        return None


# ─────────────────────────── Cache ───────────────────────────


_CACHE_TTL = timedelta(hours=24)


async def _cache_get(db, key: str) -> Optional[dict]:
    if db is None:
        return None
    doc = await db.sportdb_cache.find_one({"_id": key})
    if not doc:
        return None
    fetched_at = doc.get("fetched_at")
    if not fetched_at:
        return None
    try:
        ts = datetime.fromisoformat(fetched_at)
    except Exception:
        return None
    if datetime.now(timezone.utc) - ts > _CACHE_TTL:
        return None
    return doc.get("data")


async def _cache_set(db, key: str, data: Any):
    if db is None or data is None:
        return
    await db.sportdb_cache.update_one(
        {"_id": key},
        {"$set": {"data": data, "fetched_at": datetime.now(timezone.utc).isoformat()}},
        upsert=True,
    )


# ─────────────────────────── Domain ───────────────────────────


def _parse_team_form(team_row: dict) -> dict:
    """Convert a SportDB standings row into a normalised form dict."""
    goals = (team_row.get("goals") or "0:0").split(":")
    try:
        gf, ga = int(goals[0]), int(goals[1])
    except (ValueError, IndexError):
        gf, ga = 0, 0
    events = team_row.get("events") or []
    form_letters: list[str] = []
    for ev in events:
        et = ev.get("eventType")  # 'w' win, 'l' loss, 'd' draw
        if et == "w":
            form_letters.append("W")
        elif et == "l":
            form_letters.append("L")
        elif et == "d":
            form_letters.append("D")
    # Simple form score: W=+1, D=0, L=-1, averaged over last 5.
    score = 0.0
    if form_letters:
        score = sum(1 if x == "W" else -1 if x == "L" else 0 for x in form_letters[:5]) / max(len(form_letters[:5]), 1)
    return {
        "team_name": team_row.get("teamName"),
        "team_id": team_row.get("teamId"),
        "rank": _int(team_row.get("rank")),
        "points": _int(team_row.get("points")),
        "ppm": _float(team_row.get("pointsPerMatchesPlayed")),
        "wins": _int(team_row.get("wins")),
        "draws": _int(team_row.get("draws")),
        "losses": _int(team_row.get("lossesRegular")),
        "matches": _int(team_row.get("matches")),
        "goals_for": gf,
        "goals_against": ga,
        "goal_diff": gf - ga,
        "form": form_letters[:5],
        "form_score": round(score, 3),
    }


def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


async def _resolve_competition_link(country_slug: str, comp_slug: str) -> Optional[str]:
    """If we don't have the comp id yet, discover it via the /football/{country}
    endpoint. Costs 1 request per league we don't know. Should be a one-time
    cost since IDs are stable."""
    data = await _get(f"/football/{country_slug}")
    if not isinstance(data, list):
        return None
    target = comp_slug.lower()
    for comp in data:
        if (comp.get("slug") or "").lower() == target:
            return comp.get("link")  # e.g. /api/flashscore/football/england:198/premier-league:dYlOSQOD
    return None


async def fetch_standings(db, country_slug_id: str, comp_slug_id: str, season: str = "2025-2026") -> Optional[list[dict]]:
    """Fetch & cache a league's standings. country_slug_id is 'england:198'."""
    cache_key = f"standings:{country_slug_id}:{comp_slug_id}:{season}"
    cached = await _cache_get(db, cache_key)
    if cached is not None:
        return cached
    path = f"/football/{country_slug_id}/{comp_slug_id}/{season}/standings"
    data = await _get(path)
    if data is None:
        return None
    if not isinstance(data, list):
        return None
    await _cache_set(db, cache_key, data)
    return data


async def refresh_top_leagues(db) -> dict:
    """Refresh standings for the curated top leagues. Idempotent — cached calls
    are free. Returns counts."""
    counts = {"hit": 0, "miss": 0, "errors": 0}
    for entry in TOP_FOOTBALL_LEAGUES:
        # Backwards-compatible: support both 3- and 4-tuple entries.
        if len(entry) == 4:
            name, country, comp, season = entry
        else:
            name, country, comp = entry
            season = "2025-2026"
        try:
            cache_key = f"standings:{country}:{comp}:{season}"
            cached = await _cache_get(db, cache_key)
            if cached:
                counts["hit"] += 1
                continue
            data = await fetch_standings(db, country, comp, season=season)
            if data:
                counts["miss"] += 1
            else:
                counts["errors"] += 1
        except Exception as e:
            logger.warning("refresh %s failed: %s", name, e)
            counts["errors"] += 1
        await asyncio.sleep(0.3)
    logger.info("SportDB league refresh: %s", counts)
    return counts


async def lookup_team_form(db, team_name: str) -> Optional[dict]:
    """Find a team across every cached league. Returns the form dict + league
    label, or None if not found."""
    if not team_name:
        return None
    nq = _norm(team_name)
    if not nq:
        return None
    # ── Phase 2A.5B RC1 CLOSURE (2026-08) ────────────────────────────
    # Canonical alias resolution BEFORE the DB sweep — this makes
    # provider ↔ DB naming differences resolvable without unsafe
    # broad fuzzy substring matching.
    try:
        from services.soccer_team_identity import canonical_team_key
        nq_canon = canonical_team_key(team_name) or nq
    except Exception:
        nq_canon = nq
    # Sweep the cache; this is a single DB query.
    cursor = db.sportdb_cache.find({"_id": {"$regex": "^standings:"}})
    async for doc in cursor:
        data = doc.get("data") or []
        if not isinstance(data, list):
            continue
        for row in data:
            tn = row.get("teamName") or ""
            if not tn:
                continue
            n_team = _norm(tn)
            # Canonical alias-resolved team name for equality.
            try:
                from services.soccer_team_identity import canonical_team_key as _canon
                n_team_canon = _canon(tn) or n_team
            except Exception:
                n_team_canon = n_team
            # PRIMARY: canonical equality (safe).
            if n_team_canon and (n_team_canon == nq_canon or n_team == nq):
                parsed = _parse_team_form(row)
                _id = doc.get("_id", "")
                parts = _id.split(":")
                comp = parts[3] if len(parts) >= 4 else _id
                parsed["competition"] = comp
                parsed["identity_match"] = "canonical"
                return parsed
            # SECONDARY: constrained substring — allow ONLY when the
            # shorter side is ≥ 4 characters AND ≥ 55 % of the longer's
            # length.  This blocks the historic "Madrid" ↔ "Real Madrid"
            # / "Real Madrid Castilla" collision class while still
            # tolerating "Vancouver Whitecaps" ↔ "Vancouver Whitecaps FC"
            # after the FC/CF/SC noise strip.
            short, long_ = (nq, n_team) if len(nq) <= len(n_team) else (n_team, nq)
            if (len(short) >= 4
                    and len(short) / max(1, len(long_)) >= 0.55
                    and (short in long_)):
                parsed = _parse_team_form(row)
                _id = doc.get("_id", "")
                parts = _id.split(":")
                comp = parts[3] if len(parts) >= 4 else _id
                parsed["competition"] = comp
                parsed["identity_match"] = "safe_substring"
                return parsed
    return None


# ─────────────────────────── Prediction enrichment ───────────────────────────


def form_label(form_score: float) -> str:
    if form_score >= 0.6:
        return "Hot"
    if form_score >= 0.2:
        return "Trending Up"
    if form_score <= -0.6:
        return "Cold"
    if form_score <= -0.2:
        return "Trending Down"
    return "Mixed"


def build_form_insights(home: dict, away: dict) -> list[str]:
    """Human-readable insights for the pick's key_insights array."""
    out: list[str] = []
    if home:
        gpg = round(home["goals_for"] / max(home["matches"], 1), 2)
        cgpg = round(home["goals_against"] / max(home["matches"], 1), 2)
        out.append(f"{home['team_name']}: rank {home['rank']} • {home['ppm']} ppm • {gpg} G/{cgpg} GA per match")
        out.append(f"{home['team_name']} L5: {'-'.join(home['form']) or 'n/a'} ({form_label(home['form_score'])})")
    if away:
        gpg = round(away["goals_for"] / max(away["matches"], 1), 2)
        cgpg = round(away["goals_against"] / max(away["matches"], 1), 2)
        out.append(f"{away['team_name']}: rank {away['rank']} • {away['ppm']} ppm • {gpg} G/{cgpg} GA per match")
        out.append(f"{away['team_name']} L5: {'-'.join(away['form']) or 'n/a'} ({form_label(away['form_score'])})")
    return out


def form_win_prob_delta(pick: dict, home: Optional[dict], away: Optional[dict]) -> float:
    """Return a small adjustment to model_win_prob (in [-0.06, +0.06]) based on
    the form gap between the two teams and which side the pick is on. We
    intentionally cap this so the AI's existing factors stay dominant.
    """
    if not home or not away:
        return 0.0
    market = (pick.get("market") or "").lower()
    selection = pick.get("selection") or ""
    # Difference in form score: positive favours home.
    diff_form = (home["form_score"] - away["form_score"])
    # Difference in points-per-match (normalised by 3): positive favours home.
    diff_ppm = (home["ppm"] - away["ppm"]) / 3.0
    # Composite signal — equally weighted.
    signal = (diff_form + diff_ppm) / 2.0  # in roughly [-1, +1]
    # Map signal magnitude to a probability bump capped at ±6%.
    bump = max(-0.06, min(0.06, signal * 0.06))
    # Direction: positive bump if the pick sides with home (or "win or draw"
    # of home), negative if it sides with away. For Over/Under totals we
    # convert the form into an "expected goals" boost.
    if "moneyline" in market or "win or draw" in market or "double chance" in market:
        if selection == home["team_name"] or _norm(selection) == _norm(home["team_name"]):
            return bump
        if selection == away["team_name"] or _norm(selection) == _norm(away["team_name"]):
            return -bump
        return 0.0
    if "over" in market or "under" in market or "total" in market:
        # Hot offences → Over bump / Under penalty.
        attack = (home["goals_for"] / max(home["matches"], 1)) + (away["goals_for"] / max(away["matches"], 1))
        defence = (home["goals_against"] / max(home["matches"], 1)) + (away["goals_against"] / max(away["matches"], 1))
        # >3.5 G/game in form → +4%; <2 → -4%.
        bias = max(-0.04, min(0.04, (attack + defence - 3.0) * 0.02))
        sel_l = (selection or "").lower()
        market_l = market.lower()
        # If selection is "Over", positive bias when teams score a lot; vice
        # versa for "Under". Some markets store side in market text not sel.
        if "under" in sel_l or "under" in market_l:
            return -bias
        return bias
    return 0.0


async def enrich_pick(db, pick: dict) -> dict:
    """Attach SportDB form context to a pick. Adjusts win_probability,
    implied probability delta, and key_insights in-place; returns the pick."""
    if pick.get("sport") != "Soccer":
        return pick

    event = pick.get("event") or ""
    home: Optional[dict] = None
    away: Optional[dict] = None
    home_team = ""
    away_team = ""
    if "@" in event:
        away_team, home_team = [x.strip() for x in event.split("@", 1)]
        home = await lookup_team_form(db, home_team)
        away = await lookup_team_form(db, away_team)

    insights = pick.get("key_insights") or []
    market_l = (pick.get("market") or "").lower()
    is_goalscorer = "goal scorer" in market_l or "to score or assist" in market_l
    have_form = bool(home or away)

    # Form-based insights (only if we have cached standings — i.e. club games).
    if have_form:
        insights.extend(build_form_insights(home, away))

    # Goal-scorer deep-dive — works even WITHOUT SportDB form by leaning on
    # the market price + model factors. So national-team World Cup props still
    # get a "why he should score" breakdown.
    if is_goalscorer:
        gs_insights = _goalscorer_deep_dive(pick, home, away, home_team, away_team)
        if gs_insights:
            insights.extend(gs_insights)

    pick["key_insights"] = insights

    # Probability delta only meaningful when team form is available.
    if have_form:
        delta = form_win_prob_delta(pick, home, away)
        if abs(delta) > 0.005:
            old_wp = pick.get("win_probability") or 0
            new_wp = max(1.0, min(99.0, old_wp + delta * 100))
            pick["win_probability"] = round(new_wp, 1)
            book_odds = pick.get("book_odds") or 0
            if book_odds:
                book_implied = _implied_prob_pct(book_odds)
                pick["implied_probability"] = round(book_implied, 1)
                pick["edge_percent"] = round((new_wp - book_implied), 2)
            factors = pick.get("factors") or {}
            factors["Live Form (SportDB)"] = round(50 + delta * 500, 1)
            pick["factors"] = factors

    # Mark as enriched if we contributed anything (form OR goalscorer deep dive).
    if have_form or is_goalscorer:
        pick["enriched_by"] = "sportdb"
    return pick


def _goalscorer_deep_dive(pick: dict, home: Optional[dict], away: Optional[dict],
                           home_team: str, away_team: str) -> list[str]:
    """Generate sport-aware reasoning for an Anytime Goal Scorer pick.

    We can't fetch every player's last-N matches without burning the trial
    budget, so we lean on data we already have cached: opposing defence,
    home/away offence, and the player's market price (which IS the market's
    estimate of his scoring rate).

    Returns 3-5 plain-language bullets the UI renders in `key_insights`.
    """
    out: list[str] = []
    player = (pick.get("selection") or pick.get("market") or "").strip()
    # Strip trailing "Anytime Goal Scorer" wording when selection is the full market.
    player_name = player.replace("Anytime Goal Scorer", "").replace("First Goal Scorer", "").strip()
    if not player_name:
        return out

    # Identify which side the player is on by checking the model's stored
    # `home_or_away` hint if available; otherwise infer from defensive form.
    player_side = pick.get("player_side")  # "home" | "away" if set upstream
    player_team = pick.get("player_team")
    if not player_team:
        # Best-effort guess: use the team with the stronger attack as a hint
        # (forwards on the stronger attack are more likely to be the bookmaker's
        # named goal scorer). UI tone is "we think" not "we know".
        player_team = (home["team_name"] if (home and away and
                       home["goals_for"] >= away["goals_for"]) else
                       (away["team_name"] if away else home_team))
        player_side = "home" if player_team == (home or {}).get("team_name") else "away"

    own = home if player_side == "home" else away
    opp = away if player_side == "home" else home

    # 1) Implied scoring rate from the price.
    book_odds = pick.get("book_odds")
    if book_odds is not None:
        implied = _implied_prob_pct(book_odds)
        out.append(
            f"Market prices {player_name} to score at {implied:.0f}% — "
            f"sportsbooks see him as a {'primary' if implied >= 55 else 'secondary' if implied >= 40 else 'depth'} threat."
        )

    # 2) Own team's attacking output.
    if own:
        gpm = own["goals_for"] / max(own["matches"], 1)
        out.append(
            f"{own['team_name']} are averaging {gpm:.2f} goals/match this season "
            f"({own['goals_for']} in {own['matches']}). Players in this attack convert {form_label(own['form_score']).lower()} form into goals."
        )

    # 3) Opponent's defensive frailty.
    if opp:
        cgpm = opp["goals_against"] / max(opp["matches"], 1)
        out.append(
            f"{opp['team_name']} concede {cgpm:.2f} goals/match — "
            f"{'bottom-tier defence (chance for a clean strike)' if cgpm >= 1.5 else 'mid-tier defence (occasional gaps)' if cgpm >= 1.0 else 'tight defence (toughest matchup)'}."
        )

    # 4) Form interaction — hot striker vs leaky defence is the dream.
    if own and opp:
        offence = own["goals_for"] / max(own["matches"], 1)
        defence_weakness = opp["goals_against"] / max(opp["matches"], 1)
        score = offence + defence_weakness
        if score >= 3.5:
            out.append(f"🔥 Style matchup favours scorers: combined offence + defensive frailty rates {score:.2f} goals/g.")
        elif score >= 2.5:
            out.append(f"Neutral matchup: combined goal-environment is {score:.2f}/match.")
        else:
            out.append(f"⚠️ Low-scoring matchup expected: combined goal-environment is {score:.2f}/match — fade if you need a sure thing.")

    # 5) Win-or-Draw / Assist note: if the player's team also has a strong
    # Win-or-Draw price, the goalscorer pick is doubly supported. We can't
    # cross-reference picks without an extra query, so we just nudge the user.
    if own and own["form_score"] >= 0.4:
        out.append(f"Bonus angle: {own['team_name']} are also trending up — a goal or assist contribution is the upside scenario.")

    return out



def _implied_prob_pct(american: int) -> float:
    if american >= 0:
        return 100.0 / (american + 100) * 100
    return (-american) / ((-american) + 100) * 100
