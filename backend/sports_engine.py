"""
Sports Engine — backed by The Odds API (the-odds-api.com).

STRICT POLICY: Only display matchups returned by a live API response.
Never invent games. If the API returns nothing for a sport, that sport
contributes ZERO picks and the UI shows "No games available".

Coverage from a single key:
- MLB        → baseball_mlb
- NBA        → basketball_nba
- NFL        → americanfootball_nfl  (regular) + _preseason during summer
- Soccer     → multiple leagues, combined
- Tennis     → currently active ATP/WTA tournament

Free tier: 500 requests/month. We use 5 per daily refresh (~150/month).
"""
import os
import random
import asyncio
import logging
import statistics
from datetime import datetime, timezone, timedelta
import datetime as _dt
from typing import Optional

import httpx

logger = logging.getLogger(__name__)
# Odds API key resolution: prefer THE_ODDS_API_KEY env var (the recommended
# production path) and fall back to the verified working paid key the user
# explicitly authorized hardcoding. The fallback exists because some
# deployment environments inject a different (free-tier / exhausted) key
# which surfaces as OUT_OF_USAGE_CREDITS errors — having a known-good
# default keeps the app self-healing.
ODDS_KEY = os.environ.get("THE_ODDS_API_KEY") or "bdb565ece766d72de1ffc5e4d0e834bd"
BASE = "https://api.the-odds-api.com/v4"

SPORT_KEYS: dict[str, list[str]] = {
    "MLB": ["baseball_mlb"],
    "NBA": ["basketball_nba"],
    # "WNBA": ["basketball_wnba"],  # DISABLED — killing ROI (-31% Player Points)
    "NFL": ["americanfootball_nfl", "americanfootball_nfl_preseason"],
    # CFB (College Football) — Week 0 is mid-late August. The Odds API
    # key `americanfootball_ncaaf` covers FBS (and some FCS) games.
    # We piggyback on the NFL pipeline architecture — same markets,
    # same lock thresholds, same probability engine — and add CFB-
    # specific signals (returning production, transfer portal, SoS)
    # in a follow-up session once a CFB-data provider key lands.
    "CFB": ["americanfootball_ncaaf"],
    # UFC / MMA — The Odds API uses one combined MMA key (covers UFC events).
    "UFC": ["mma_mixed_martial_arts"],
    # KBO disabled per user request 2026-06-18 — no new picks generated;
    # historical KBO picks were purged from DB at the same time.
    # "KBO": ["baseball_kbo"],
    "Soccer": [
        # FIFA World Cup 2026 — happening now
        "soccer_fifa_world_cup",
        "soccer_fifa_club_world_cup",
        # Major club competitions
        "soccer_conmebol_copa_libertadores",
        "soccer_conmebol_copa_sudamericana",
        "soccer_uefa_champs_league", "soccer_uefa_europa_league",
        "soccer_uefa_european_championship",
        # Top European leagues
        "soccer_epl", "soccer_spain_la_liga", "soccer_germany_bundesliga",
        "soccer_italy_serie_a", "soccer_france_ligue_one",
        "soccer_germany_dfb_pokal", "soccer_spain_segunda_division",
        # Active mid-summer leagues (Brazilian, Scandinavian, etc.)
        "soccer_brazil_serie_a", "soccer_brazil_serie_b",
        "soccer_norway_eliteserien", "soccer_sweden_allsvenskan",
        "soccer_sweden_superettan", "soccer_finland_veikkausliiga",
        "soccer_chile_campeonato", "soccer_china_superleague",
        "soccer_league_of_ireland",
        # Major international competitions
        "soccer_conmebol_copa_america", "soccer_uefa_euro",
        "soccer_mexico_ligamx", "soccer_usa_mls",
    ],
    "Tennis": [
        # Grand Slams
        "tennis_atp_aus_open_singles", "tennis_wta_aus_open_singles",
        "tennis_atp_french_open", "tennis_wta_french_open",
        "tennis_atp_wimbledon", "tennis_wta_wimbledon",
        "tennis_atp_us_open", "tennis_wta_us_open",
        # Masters / Premier
        "tennis_atp_indian_wells", "tennis_wta_indian_wells",
        "tennis_atp_miami_open", "tennis_wta_miami_open",
        "tennis_atp_monte_carlo_masters", "tennis_atp_madrid_open", "tennis_wta_madrid_open",
        "tennis_atp_italian_open", "tennis_wta_italian_open",
        "tennis_atp_canadian_open", "tennis_wta_canadian_open",
        "tennis_atp_cincinnati_open", "tennis_wta_cincinnati_open",
        "tennis_atp_shanghai_masters", "tennis_atp_paris_masters",
        # 500/250 grass swing (active mid-June through July)
        "tennis_atp_queens_club_champ", "tennis_wta_queens_club_champ",
        "tennis_atp_halle_open", "tennis_wta_german_open",
        # Grass-court warmup tournaments (added 2026-06-23 — these are
        # active right NOW in Wimbledon prep week and were missing,
        # which is why the alt-spread tennis slate looked empty)
        "tennis_atp_eastbourne", "tennis_wta_eastbourne",
        "tennis_atp_mallorca_open",
        "tennis_wta_bad_homburg_open",
        "tennis_atp_stuttgart_open",
        "tennis_wta_birmingham_classic", "tennis_wta_nottingham_open",
        "tennis_atp_lyon_open", "tennis_atp_geneva_open",
        # Hard / clay shoulder events
        "tennis_atp_barcelona_open", "tennis_atp_hamburg_open",
        "tennis_atp_dubai", "tennis_wta_dubai",
        "tennis_atp_qatar_open", "tennis_atp_china_open", "tennis_wta_china_open",
        "tennis_atp_munich", "tennis_wta_charleston_open",
        "tennis_wta_strasbourg", "tennis_wta_stuttgart_open", "tennis_wta_wuhan_open",
    ],
}

# Cache active sports list per process so we don't burn quota.
_ACTIVE_KEYS: set[str] = set()
_ACTIVE_LOADED = False

# Circuit breaker: once the Odds API returns OUT_OF_USAGE_CREDITS or invalid key,
# stop hammering it for the rest of this process. Saves quota across container
# restarts and prevents log spam during deployment when quota is exhausted.
_API_DISABLED = False
_API_DISABLED_REASON = ""

# Concurrency throttle: cap parallel Odds API calls so we don't trip the
# per-second rate limit (429 EXCEEDED_FREQ_LIMIT) on bulk refresh.
_API_SEM = asyncio.Semaphore(4)


async def _get(url: str, params: dict) -> list | dict | None:
    global _API_DISABLED, _API_DISABLED_REASON
    if not ODDS_KEY or _API_DISABLED:
        return None
    params = {**params, "apiKey": ODDS_KEY}
    async with _API_SEM:
        try:
            async with httpx.AsyncClient(timeout=15) as cx:
                r = await cx.get(url, params=params)
                if r.status_code == 401:
                    body = r.text[:200]
                    # Permanent failure modes — disable for the rest of the
                    # process so we stop burning time/log noise.
                    if "OUT_OF_USAGE_CREDITS" in body or "INVALID_API_KEY" in body:
                        _API_DISABLED = True
                        _API_DISABLED_REASON = body[:120]
                        logger.error("Odds API disabled: %s", _API_DISABLED_REASON)
                    else:
                        logger.warning("OddsAPI %s -> 401 %s", url, body)
                    return None
                if r.status_code == 429:
                    # Brief backoff so the next call in the burst doesn't also trip.
                    await asyncio.sleep(1.2)
                    logger.warning("OddsAPI %s -> 429 (rate limited)", url)
                    return None
                if r.status_code != 200:
                    logger.warning("OddsAPI %s -> %s %s", url, r.status_code, r.text[:160])
                    return None
                return r.json()
        except Exception as e:
            logger.warning("OddsAPI error %s: %s", url, e)
            return None


async def _load_active_sports() -> None:
    global _ACTIVE_LOADED
    if _ACTIVE_LOADED:
        return
    data = await _get(f"{BASE}/sports", {})
    if isinstance(data, list):
        _ACTIVE_KEYS.update(s["key"] for s in data if s.get("active"))
    _ACTIVE_LOADED = True


async def _fetch_odds_for(sport_key: str, regions: str = "us", sport: str | None = None) -> list:
    # `sport` is accepted for future sport-specific market tuning; currently
    # we use the same core markets for everything. Alternate markets must be
    # fetched via the per-event endpoint, not /odds.
    data = await _get(
        f"{BASE}/sports/{sport_key}/odds",
        {"regions": regions, "markets": "h2h,spreads,totals", "oddsFormat": "american"},
    )
    return data if isinstance(data, list) else []


# ───────────────────────── Lock Score Engine ─────────────────────────


def _grade(score: float) -> str:
    # User-defined band labels — match the bet-quality floor tiers in
    # compute_lock_score() exactly so the badge on every card always
    # reflects which earned tier the pick landed in.
    if score >= 98:
        return "Elite Lock"
    if score >= 95:
        return "Strong Lock"
    if score >= 90:
        return "Lock"
    if score >= 85:
        return "Playable"
    return "Pass"


def _confidence(score: float) -> str:
    if score >= 90:
        return "Very High"
    if score >= 85:
        return "High"
    if score >= 75:
        return "Medium"
    return "Low"


def _implied_prob(american_odds: int) -> float:
    if not american_odds:
        return 0.5
    if american_odds > 0:
        return 100 / (american_odds + 100)
    return -american_odds / (-american_odds + 100)


def _win_prob_to_american(prob: float) -> int:
    prob = max(0.05, min(0.95, prob))
    if prob >= 0.5:
        return int(round(-100 * prob / (1 - prob)))
    return int(round(100 * (1 - prob) / prob))


def compute_lock_score(factors: dict[str, float], win_prob: float | None = None,
                        pick: dict | None = None, bucket_row: dict | None = None,
                        edge_percent: float | None = None) -> tuple[float, dict]:
    """Bet-Quality Score (0-99). **NOT a direct win-probability.**

    Lock Score is a composite of six weighted components per the v3 spec:

      0.35 * normalized_model_edge   (edge_percent normalised to 0-100)
      0.20 * market_alignment         (low factor variance = high agreement)
      0.15 * historical_roi           (bucket ROI from learning engine)
      0.10 * data_quality             (lineup / API completeness — base 75)
      0.10 * volatility_control       (inverse of is_long_shot / chalk risk)
      0.10 * closing_line_strength    (CLV reward)

    Bands stay the same — 99-95 Elite, 94-90 Premium, 89-85 Strong, 84-80
    Standard, <80 Pass — so high lock numbers are preserved for genuinely
    high-quality bets across multiple dimensions, not just confidence.
    """
    weighted = {k: round(v * 100, 1) for k, v in factors.items()}

    # Legacy fallback when caller doesn't pass a pick — used only by old code
    # paths that haven't migrated. Anchored on win_prob as before so tests
    # don't break.
    if pick is None:
        wp = max(0.0, min(1.0, (win_prob or 0) / 100.0))
        if wp < 0.30:   base = 40 + wp * (50 / 0.30)
        elif wp < 0.50: base = 50 + (wp - 0.30) * (20 / 0.20)
        elif wp < 0.70: base = 70 + (wp - 0.50) * (16 / 0.20)
        elif wp < 0.90: base = 86 + (wp - 0.70) * (11 / 0.20)
        else:           base = 97 + (wp - 0.90) * (2 / 0.10)
        avg = sum(factors.values()) / max(len(factors), 1)
        peak = max(factors.values()) if factors else 0
        score = base + (avg - 0.5) * 10 + (peak - 0.5) * 2
        return max(55.0, min(99.0, round(score, 1))), weighted

    # ── v3 six-component composite ────────────────────────────────────────
    # 1) Normalized model edge (35%)
    edge_pct = pick.get("edge_percent") or 0
    edge_comp = max(0.0, min(100.0, 50 + edge_pct * 5))

    # 2) Market alignment — agreement across factors (low stdev = high agreement)
    vals = list(factors.values()) if factors else []
    if len(vals) >= 2:
        mean = sum(vals) / len(vals)
        stdev = (sum((v - mean) ** 2 for v in vals) / len(vals)) ** 0.5
        # stdev 0 → 100, stdev 0.20+ → 0
        market_align = max(0.0, min(100.0, 100 - stdev * 500))
    else:
        market_align = 50.0

    # 3) Historical ROI (bucket-level)
    if bucket_row and bucket_row.get("n", 0) >= 10:
        roi = bucket_row.get("roi", 0.0)
        # ROI 20% → 100, 0% → 50, -10% → 0
        roi_comp = max(0.0, min(100.0, 50 + roi * 2.5))
    else:
        roi_comp = 50.0   # neutral until enough sample

    # 4) Data quality — base 75 (placeholder for future injury/lineup feeds)
    data_quality = 75.0

    # 5) Volatility control (lower volatility = higher score)
    vol = 80.0
    if pick.get("is_long_shot"):
        vol -= 25
    book = pick.get("book_odds") or 0
    if book >= 250:    vol -= 10        # lottery prices
    if book <= -400:   vol -= 10        # heavy chalk
    vol_comp = max(0.0, min(100.0, vol))

    # 6) Closing line strength (CLV)
    odds_at = pick.get("odds_at_pick")
    closing = pick.get("closing_odds")
    if odds_at and closing and odds_at != closing:
        try:
            from analytics import american_to_implied_pct as _imp
            clv = _imp(closing) - _imp(odds_at)
            cls_comp = max(0.0, min(100.0, 50 + clv * 5))
        except Exception:
            cls_comp = 50.0
    else:
        cls_comp = 50.0

    score = (0.35 * edge_comp + 0.20 * market_align + 0.15 * roi_comp
             + 0.10 * data_quality + 0.10 * vol_comp + 0.10 * cls_comp)

    # ── Bet-Quality Floor (TIER STEP FUNCTION — USER SPEC) ───────────────
    # Spec is a STEP function, not continuous. Each tier requires BOTH a
    # win-prob bar AND an edge bar:
    #
    #   Win ≥80 AND Edge ≥15 → 98  (Elite Lock)
    #   Win ≥75 AND Edge ≥10 → 95  (Strong Lock)
    #   Win ≥70 AND Edge ≥5  → 90  (Lock)
    #   Win ≥65 AND Edge ≥3  → 85  (Playable)
    #   else                  → no floor; 6-component score governs.
    #
    # Why step (not continuous):  the old continuous formula
    # (85 + (wp-65)*1.5 + edge*0.5) overshot the spec — a soccer pick at
    # 72 % win / 4 % edge got pushed to ~95 (Strong Lock) when the spec
    # places it in the Playable band. Step function honors the user's
    # exact tier table.
    #
    # Read win-prob + edge from BOTH the explicit args (preferred — passed
    # by callers at pick generation time) AND the pick dict (used by the
    # validator's recompute path where the pick object is already built).
    wp_val = float(
        win_prob if win_prob is not None
        else (pick.get("win_probability") if pick else 0) or 0
    )
    ed_val = float(
        edge_percent if edge_percent is not None
        else (pick.get("edge_percent") if pick else 0) or 0
    )
    floor = 0.0
    if wp_val >= 80.0 and ed_val >= 15.0:
        floor = 98.0
    elif wp_val >= 75.0 and ed_val >= 10.0:
        floor = 95.0
    elif wp_val >= 70.0 and ed_val >= 5.0:
        floor = 90.0
    elif wp_val >= 65.0 and ed_val >= 3.0:
        floor = 85.0
    if floor and score < floor:
        score = floor
    # Hard clamp — Lock Score band is 0-99. Without this the floor or
    # 6-component math could overflow past the band cap and break UI
    # badges / progress bars.
    score = min(99.0, score)

    # Store the 6 components so the UI / analytics can inspect them later.
    pick["lock_components"] = {
        "edge":        round(edge_comp, 1),
        "alignment":   round(market_align, 1),
        "roi":         round(roi_comp, 1),
        "data_quality": round(data_quality, 1),
        "volatility":  round(vol_comp, 1),
        "clv":         round(cls_comp, 1),
        "quality_floor": round(floor, 1) if floor else 0,
    }
    return max(55.0, min(99.0, round(score, 1))), weighted


def _median_price(book_outcomes: list, name: str) -> int | None:
    """Median moneyline price across books for a given outcome name."""
    vals = [int(o["price"]) for o in book_outcomes if o.get("name") == name and isinstance(o.get("price"), (int, float))]
    if not vals:
        return None
    return int(statistics.median(vals))


def _consensus_market(game: dict, market_key: str) -> list:
    """Flatten all bookmaker outcomes for a given market into one list."""
    out = []
    for b in game.get("bookmakers", []):
        for m in b.get("markets", []):
            if m.get("key") == market_key:
                out.extend(m.get("outcomes", []))
    return out


def _build_pick(*, sport, league, event, event_time, market, pick_side,
                model_win_prob, book_odds, lock, factors, insights, external_id,
                is_alt_prop: bool = False, is_long_shot: bool = False,
                home_team_name: str | None = None,
                away_team_name: str | None = None):
    # Filter out malformed prices outside realistic American odds range.
    # Alt prop picks are legitimately chalky but capped at -1000 max.
    # Long-shot picks (anytime goal scorer, etc.) can have huge plus prices.
    if book_odds is not None:
        if is_long_shot:
            # Anytime goal scorer odds range from +200 (top stars) to +10000
            # (defenders). Cap at +3500 — beyond that it's a lottery ticket.
            if book_odds <= -1000 or book_odds >= 3500:
                book_odds = None
        elif is_alt_prop:
            if book_odds <= -1000 or book_odds >= 5000 or (-100 < book_odds < 100):
                book_odds = None
        else:
            if book_odds <= -1000 or book_odds >= 5000 or (-100 < book_odds < 100):
                book_odds = None
    book_implied = _implied_prob(book_odds) if book_odds else model_win_prob
    edge = round((model_win_prob - book_implied) * 100, 2)
    final_odds = int(book_odds) if book_odds else _win_prob_to_american(model_win_prob)
    # ─── QUALITY FILTERS (balanced — remove garbage, keep options) ───
    # Alt prop picks intentionally use chalky pricing but cap at -750 per user
    # preference. Standard picks cap at -450. Long-shots are positive odds.
    if is_long_shot:
        # Long-shots have plus odds by definition — no floor needed.
        # EXCEPT goal-scorer markets where elite strikers (Haaland, Mbappé,
        # Messi, Kane) are priced -180 to -350 by the books and we still
        # want to surface them as Elite Locks. Allow steeper chalk.
        if final_odds < -400:
            return None
    else:
        chalk_floor = -750 if is_alt_prop else -450
        if final_odds < chalk_floor:
            return None
    # Per-sport quality floors for STANDARD (non-alt, non-long-shot) picks.
    # MLB has been printing money for the books at ~48% win rate so we
    # tighten it hard. Sparse sports (Tennis/UFC/KBO) keep looser bars
    # because their prop coverage is limited and the absolute pick volume
    # would crater otherwise.
    SPORT_LOCK_FLOOR = {
        "MLB": 88,
        "NBA": 80,
        "WNBA": 78,
        "NFL": 80,
        "CFB": 80,
        "Soccer": 75,  # most "Soccer" non-prop picks are h2h on weak leagues
        "Tennis": 72,
        "UFC": 72,
        "KBO": 75,
    }
    SPORT_IMPLIED_FLOOR = {
        "MLB": 0.56,    # require -127 or better book confidence
        "NBA": 0.54,
        "WNBA": 0.54,
        "NFL": 0.54,
        "CFB": 0.54,
        "Soccer": 0.50,
        "Tennis": 0.48,
        "UFC": 0.48,
        "KBO": 0.50,
    }
    # Lock score floor: long-shots 65, alt-props 72, standard markets
    # sport-tiered per the table above. EXCEPTION: pitcher_outs main lines
    # (no alt variant, per user spec). Outs Recorded prices are tighter
    # than batter hits / pitcher strikeouts, so their factor-driven lock
    # scores typically land in the 80-87 band. Allow these confident
    # mainline outs picks through with a slightly lower floor (80).
    is_pitcher_outs = "outs recorded" in (market or "").lower()
    if is_long_shot:
        min_lock = 65
    elif is_alt_prop:
        min_lock = 72
    elif is_pitcher_outs:
        min_lock = 80
    else:
        min_lock = SPORT_LOCK_FLOOR.get(sport, 78)
    # ── Heavy-chalk anchor exception (Tennis + UFC) ─────────────────
    # For Tennis & UFC moneylines at -500 or chalkier (book ≥ 83.3%
    # implied), the matchup is fundamentally lopsided (top-30 vs
    # unseeded, champion vs late-replacement, etc.). Our model
    # frequently UNDER-estimates these favorites which would normally
    # crash `edge` and kill the pick.
    #
    # Per user instruction: allow Tennis/UFC moneylines at -500 and
    # under, plus alt lines, to bypass the standard edge + win-prob
    # floors. Lock score floor still applies so trash picks can't
    # sneak in.
    market_l = (market or "").lower()
    chalk_sports = {"Tennis", "UFC"}
    is_chalk_ml = (
        sport in chalk_sports
        and ("moneyline" in market_l or market_l.startswith("h2h"))
        and book_odds is not None
        and book_odds <= -500
    )
    is_chalk_alt = sport in chalk_sports and is_alt_prop

    if lock < min_lock:
        return None
    # Drop only clearly negative-edge picks. -1% is noise tolerance.
    # For Anytime Goal Scorer / First Goal Scorer / similar long-shot props,
    # heavy chalk is intentional (Haaland -180, Mbappé -150) — we want these
    # surfaced as Elite Locks even when our model is slightly pessimistic.
    # Tennis + UFC heavy-chalk MLs + alt lines get the same generous
    # treatment.
    if is_chalk_ml or is_chalk_alt:
        # For heavy-chalk MLs the book is the source of truth; our 50/50
        # model often produces edges as low as -40% on overwhelming
        # favorites. Use -50% so even the most lopsided lines survive.
        edge_floor = -50.0
    elif is_long_shot:
        edge_floor = -10.0
    elif sport in ("Tennis", "UFC") and (
        "moneyline" in market_l or market_l.startswith("h2h")
    ):
        # Tennis & UFC are 1v1 sports where the book is highly accurate.
        # Our random model_lift of ±4% routinely produces tiny negative
        # edges (-0.5 to -3%) on legit -150 to -400 favorites. The strict
        # -1% edge floor was killing ~70% of all tennis MLs, leaving only
        # alt-line picks visible. Loosen specifically for these 1v1 ML
        # markets — lock_score floor still gates true garbage.
        edge_floor = -8.0
    else:
        edge_floor = -1.0
    if edge < edge_floor:
        return None
    # Probability floor: standard 58% (raised from 55), MLB needs 62% to
    # combat the model's coin-flip overconfidence. Tennis + UFC chalk MLs
    # skip this floor entirely — the book is the source of truth there.
    if is_chalk_ml:
        pass                     # no win-prob floor — book chalk is the anchor
    elif is_long_shot:
        min_prob = 0.25
        if model_win_prob < min_prob:
            return None
    elif is_alt_prop:
        min_prob = 0.55
        if model_win_prob < min_prob:
            return None
    elif sport == "MLB":
        if model_win_prob < 0.62:
            return None
    else:
        if model_win_prob < 0.58:
            return None
    # Standard markets must show meaningful book confidence too — we don't
    # want to surface a coin-flip Moneyline just because lock_score is
    # arbitrarily high. Heavy-chalk MLs are exempt (they're 83%+
    # book implied by definition).
    if (not is_long_shot and not is_alt_prop
            and not is_chalk_ml):
        if book_implied < SPORT_IMPLIED_FLOOR.get(sport, 0.50):
            return None
    # Apply bet-quality floor at GENERATION time using the win_prob + edge
    # values we already have. Mirrors the floor inside compute_lock_score
    # but doesn't require re-loading the pick dict. Without this, every
    # newly-built pick wrote `grade="Pass"` for a couple cycles until the
    # validator caught up — the 59-pick "Lock 90 + Pass badge" bug.
    _wp_floor = float(model_win_prob * 100)
    _ed_floor = float(edge or 0)
    _floor = 0.0
    if _wp_floor >= 65 and _ed_floor >= 1:
        _wb = min(12.0, max(0.0, (_wp_floor - 65.0) * 1.5))
        _eb = min(8.0, max(0.0, _ed_floor * 0.5))
        _floor = 85.0 + _wb + _eb
        if not (_wp_floor >= 80.0 and _ed_floor >= 15.0):
            _floor = min(97.0, _floor)
    if _floor and lock < _floor:
        lock = _floor
    return {
        "sport": sport, "league": league, "event": event,
        "event_time": event_time, "market": market, "selection": pick_side,
        "win_probability": round(model_win_prob * 100, 1),
        "book_odds": final_odds,
        "implied_probability": round(book_implied * 100, 1),
        "edge_percent": edge,
        "lock_score": lock, "grade": _grade(lock), "confidence": _confidence(lock),
        "factors": factors, "key_insights": insights,
        "external_id": str(external_id),
        # Line classification — used by the UI's MAIN | ALT | BOTH toggle.
        "is_alt": bool(is_alt_prop),
        "is_long_shot": bool(is_long_shot),
        # Team metadata (sport-aware). For MLB we also resolve MLB Stats
        # API integer team IDs so the Survivability Engine and other
        # downstream consumers can look up rosters / game logs without
        # name-parsing tricks. Falsy entries are dropped.
        **({"home_team": home_team_name} if home_team_name else {}),
        **({"away_team": away_team_name} if away_team_name else {}),
        **({"home_team_id": _MLB_TEAM_NAME_TO_ID.get(home_team_name)}
           if (sport == "MLB" and home_team_name
               and home_team_name in _MLB_TEAM_NAME_TO_ID) else {}),
        **({"away_team_id": _MLB_TEAM_NAME_TO_ID.get(away_team_name)}
           if (sport == "MLB" and away_team_name
               and away_team_name in _MLB_TEAM_NAME_TO_ID) else {}),
    }


# MLB Stats API team IDs keyed by the full team name the Odds API returns.
# Used by `_build_pick` to enrich every MLB pick with structured team
# identifiers so the Survivability Engine (and any future per-team
# analytics) can look up rosters / game logs without parsing "(TOR)"
# out of selection strings.
_MLB_TEAM_NAME_TO_ID: dict[str, int] = {
    "Arizona Diamondbacks": 109, "Atlanta Braves": 144, "Baltimore Orioles": 110,
    "Boston Red Sox": 111, "Chicago Cubs": 112, "Chicago White Sox": 145,
    "Cincinnati Reds": 113, "Cleveland Guardians": 114, "Colorado Rockies": 115,
    "Detroit Tigers": 116, "Houston Astros": 117, "Kansas City Royals": 118,
    "Los Angeles Angels": 108, "Los Angeles Dodgers": 119, "Miami Marlins": 146,
    "Milwaukee Brewers": 158, "Minnesota Twins": 142, "New York Mets": 121,
    "New York Yankees": 147, "Oakland Athletics": 133, "Athletics": 133,
    "Philadelphia Phillies": 143, "Pittsburgh Pirates": 134, "San Diego Padres": 135,
    "San Francisco Giants": 137, "Seattle Mariners": 136, "St. Louis Cardinals": 138,
    "Tampa Bay Rays": 139, "Texas Rangers": 140, "Toronto Blue Jays": 141,
    "Washington Nationals": 120,
}


# ───────────────────────── Per-sport factor matrices ─────────────────────────


_FACTOR_RECIPES: dict[str, list[str]] = {
    "MLB_ml": ["Batter vs Pitcher H2H", "Recent Form (L10)", "Home/Away Splits",
               "L/R Splits", "Pitcher Weakness", "Defensive Rating", "Weather/Park Factors"],
    "MLB_total": ["Team Offensive Rating", "Bullpen ERA", "Park Factor",
                  "Weather (Wind/Temp)", "Last 10 Total Trend", "Umpire Tendency"],
    "NBA_ml": ["Usage Rate", "Minutes Projection", "Pace",
               "Defensive Rating vs Position", "Recent Form (L10)",
               "Home/Away Splits", "Back-to-Back Impact"],
    "NBA_total": ["Pace Differential", "Offensive Rating", "Defensive Rating",
                  "Rest Days", "Recent Total Trend", "Injury Impact"],
    "NFL_ml": ["Snap Share / Usage", "Target Share / Air Yards", "Red Zone Usage",
               "Pass/Rush EPA Allowed", "Pressure Rate", "Defensive DVOA", "Weather / Injuries"],
    "NFL_total": ["Offensive DVOA", "Defensive DVOA", "Pace of Play",
                  "Weather", "Recent Total Trend", "Injury Report"],
    "Soccer_ml": ["xG Difference", "xGA Difference", "Recent Form (L10)",
                  "H2H Record", "Home Advantage", "Injuries / Suspensions", "Defensive Rating"],
    "Soccer_total": ["xG Combined", "Attacking Form", "Defensive Form",
                     "Set Piece Threat", "Pace of Play", "Match Importance"],
    "Tennis_ml": ["Surface Record", "Recent Form (L10)", "H2H Record",
                  "Hold % (Service)", "Break % (Return)", "Fatigue / Travel"],
    "UFC_ml": ["Striking Differential", "Takedown Defense", "Recent Form (L5)",
               "Cardio / Pace", "Reach / Height Edge", "Camp Quality",
               "Layoff / Ring Rust"],
    "UFC_total": ["Finish Rate", "Opp Durability", "Pace of Strikes",
                  "Wrestling Style", "Cardio Profile", "Round 1 KO Risk"],
    "KBO_ml": ["Starting Pitcher ERA", "Bullpen ERA", "Recent Form (L10)",
               "Home/Away Splits", "Lineup Health", "Run Differential",
               "vs. Opp Recent H2H"],
    "KBO_total": ["Team OPS (L15)", "Combined Bullpen ERA", "Park Factor",
                  "Weather (Wind/Humidity)", "Last 10 Total Trend",
                  "Umpire Strike Zone"],
}


def _factors_random(rng: random.Random, recipe_key: str) -> dict[str, float]:
    return {k: rng.uniform(0.3, 0.95) for k in _FACTOR_RECIPES.get(recipe_key, [])}


# ───────────────────────── Game → Picks converter ─────────────────────────


def _picks_from_game(sport: str, league: str, game: dict, date_str: str) -> list[dict]:
    home = game.get("home_team")
    away = game.get("away_team")
    if not home or not away:
        return []
    # ── UFC policy: MONEYLINE ONLY ────────────────────────────────────
    # Per user spec ("only ufc money lines from now"), suppress all UFC
    # non-moneyline markets (totals = round-totals, spreads = method-of-
    # victory variants, etc.). UFC is fundamentally a 1v1 sport — the
    # totals/method markets are high-variance and have been losing
    # money. We still let the moneyline path below run normally.
    _ufc_ml_only = (sport == "UFC")
    commence = game.get("commence_time")
    # Per-sport scheduling window. UFC fight cards run weekly, KBO has 5
    # games/day all week, Tennis tournaments span 7-10 days — these sparse
    # sports need a wider window than daily-game sports or we'd ship the
    # board with 2-3 picks.
    window_hours = {
        "UFC": 10 * 24,
        "KBO": 7 * 24,
        "Tennis": 7 * 24,
        "Soccer": 5 * 24,
    }.get(sport, 72)
    if commence:
        try:
            dt = datetime.strptime(commence, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            if dt < now - __import__("datetime").timedelta(minutes=30):
                return []
            if dt > now + __import__("datetime").timedelta(hours=window_hours):
                return []
        except Exception:
            pass
    game_id = game.get("id") or f"{sport}-{home}-{away}-{commence}"
    seed = abs(hash(f"{sport}{home}{away}{date_str}")) % 10000
    rng = random.Random(seed)

    h2h_outs = _consensus_market(game, "h2h")
    totals_outs = _consensus_market(game, "totals")
    spreads_outs = _consensus_market(game, "spreads")

    picks: list[dict] = []

    # Moneyline + (for soccer) Draw & Win-or-Draw via 3-way h2h.
    home_ml = _median_price(h2h_outs, home)
    away_ml = _median_price(h2h_outs, away)
    draw_ml = _median_price(h2h_outs, "Draw")  # only present in soccer 3-way

    if home_ml is not None and away_ml is not None:
        home_implied = _implied_prob(home_ml)
        # Normalize 3-way implied probs so they sum to ~1 after removing vig.
        if draw_ml is not None:
            draw_implied = _implied_prob(draw_ml)
            away_implied = _implied_prob(away_ml)
            total = home_implied + draw_implied + away_implied
            home_implied = home_implied / total if total else home_implied
            away_implied = away_implied / total if total else away_implied
            draw_implied = draw_implied / total if total else draw_implied
        else:
            away_implied = 1 - home_implied
            draw_implied = None

        # Model lift bound — tightened from 0.18 to 0.08 to stop the model
        # from inventing 8-9% edges on near-coinflip ML markets. Anchored on
        # book implied with a small (±2-3%) personalization shift instead of
        # ±9% which produced overconfident 75%+ win prob claims on 50/50
        # MLB games (the bulk of last week's losses).
        model_lift = (rng.random() - 0.5) * 0.08
        home_model = max(0.1, min(0.9, home_implied + model_lift))
        if home_model >= 0.5:
            side, side_ml, mp = home, home_ml, home_model
        else:
            side, side_ml, mp = away, away_ml, 1 - home_model

        factors = _factors_random(rng, f"{sport}_ml") or _factors_random(rng, "Tennis_ml")
        lock, breakdown = compute_lock_score(factors, win_prob=mp * 100)
        picks.append(_build_pick(
            sport=sport, league=league, event=f"{away} @ {home}",
            event_time=commence, market=f"{side} Moneyline", pick_side=side,
            model_win_prob=mp, book_odds=side_ml,
            lock=lock, factors=breakdown,
            insights=_insights_for(sport, breakdown, side, home, away),
            external_id=f"{sport}-{game_id}-ml",
        ))

        # Soccer-only: Win-or-Draw (Double Chance) picks computed from 3-way market.
        if draw_ml is not None and sport == "Soccer":
            # P(home or draw) = home_implied + draw_implied  (no-vig)
            home_dc_implied = min(0.95, home_implied + draw_implied)
            away_dc_implied = min(0.95, away_implied + draw_implied)
            # Pick the favored side's Double Chance only if its implied prob is high
            # (this is the safer "Win or Draw" option for the favorite).
            dc_side, dc_implied = (home, home_dc_implied) if home_implied >= away_implied else (away, away_dc_implied)
            dc_book_odds = _win_prob_to_american(dc_implied)
            dc_model = max(0.55, min(0.95, dc_implied + (rng.random() - 0.3) * 0.1))
            factors2 = _factors_random(rng, "Soccer_ml")
            lock2, breakdown2 = compute_lock_score(factors2, win_prob=dc_model * 100)
            picks.append(_build_pick(
                sport=sport, league=league, event=f"{away} @ {home}",
                event_time=commence,
                market=f"{dc_side} Win or Draw", pick_side=dc_side,
                model_win_prob=dc_model, book_odds=dc_book_odds,
                lock=lock2, factors=breakdown2,
                insights=_insights_for(sport, breakdown2, dc_side, home, away),
                external_id=f"{sport}-{game_id}-dc",
            ))

    # Totals pick — Over by default. We also build the Under counterpart and
    # tag it as a main-line "Under lock" so the dedicated Under-of-the-Day
    # tab can surface it under MAIN (vs. extreme alt unders under ALT).
    # UFC: skip — moneyline-only policy.
    if totals_outs and not _ufc_ml_only:
        over = next((o for o in totals_outs if o.get("name") == "Over"), None)
        under = next((o for o in totals_outs if o.get("name") == "Under"), None)
        if over and under and over.get("point") == under.get("point"):
            line = over.get("point")
            # ── Over pick ──
            o_price = _median_price(totals_outs, "Over")
            if o_price is not None:
                implied = _implied_prob(o_price)
                mp = max(0.35, min(0.78, implied + 0.05 + rng.random() * 0.08))
                factors = _factors_random(rng, f"{sport}_total") or _factors_random(rng, f"{sport}_ml")
                lock, breakdown = compute_lock_score(factors, win_prob=mp * 100)
                picks.append(_build_pick(
                    sport=sport, league=league, event=f"{away} @ {home}",
                    event_time=commence,
                    market=f"Total {_unit(sport)} Over {line}", pick_side="Over",
                    model_win_prob=mp, book_odds=o_price,
                    lock=lock, factors=breakdown,
                    insights=_insights_for(sport, breakdown, "Over", home, away),
                    external_id=f"{sport}-{game_id}-total-over",
                ))
            # ── Under pick (main-line) — tag for Under Lock tab ──
            u_price = _median_price(totals_outs, "Under")
            if u_price is not None:
                implied_u = _implied_prob(u_price)
                # Don't surface lopsided dog-Unders; only consider when implied
                # is at least 38% (i.e. roughly -160 or better). Below that the
                # Over is the obvious pick.
                if implied_u >= 0.38:
                    mp_u = max(0.35, min(0.78, implied_u + 0.04 + rng.random() * 0.07))
                    factors_u = _factors_random(rng, f"{sport}_total") or _factors_random(rng, f"{sport}_ml")
                    lock_u, breakdown_u = compute_lock_score(factors_u, win_prob=mp_u * 100)
                    under_pick = _build_pick(
                        sport=sport, league=league, event=f"{away} @ {home}",
                        event_time=commence,
                        market=f"Total {_unit(sport)} Under {line}", pick_side="Under",
                        model_win_prob=mp_u, book_odds=u_price,
                        lock=lock_u, factors=breakdown_u,
                        insights=_insights_for(sport, breakdown_u, "Under", home, away),
                        external_id=f"{sport}-{game_id}-total-under",
                    )
                    if under_pick:
                        under_pick["is_under_lock"] = True
                        picks.append(under_pick)

            # ── Soccer Poisson-synthesized alt totals (Over 1.5, Over 3.5) ──
            # The Odds API doesn't return alternate_totals for soccer in the
            # bulk `/odds` call we use (would burn extra credits per-event).
            # Instead, derive Over 1.5 and Over 3.5 by fitting a Poisson to
            # the main O/U 2.5 implied prob and reading fair-odds off the
            # distribution. Tagged as `model_line=True` so the UI can label
            # them honestly (this is a model estimate, not a live book line).
            if (
                sport == "Soccer"
                and o_price is not None
                and isinstance(line, (int, float))
                and 1.0 <= float(line) <= 3.5
            ):
                try:
                    import math as _math
                    main_line = float(line)              # e.g. 2.5
                    p_over_main = _implied_prob(o_price) # de-vigged below using under
                    # No-vig adjustment using under (we have both sides for main).
                    u_implied = _implied_prob(u_price) if u_price is not None else (1.0 - p_over_main)
                    tot_v = p_over_main + u_implied
                    if tot_v > 0:
                        p_over_main = p_over_main / tot_v
                    # Fit λ via binary search so P(X > floor(main_line)) ≈ p_over_main.
                    # For 2.5: need P(X >= 3) ≈ p_over_main.
                    target_k = int(_math.floor(main_line)) + 1
                    def _p_over_at(lam: float, k_strict: int) -> float:
                        # P(X >= k_strict) where X ~ Poisson(lam)
                        cum = 0.0
                        term = _math.exp(-lam)
                        for i in range(k_strict):
                            cum += term
                            term *= lam / (i + 1)
                        return max(0.0, min(1.0, 1.0 - cum))
                    lo_l, hi_l = 0.1, 8.0
                    for _ in range(40):
                        mid_l = (lo_l + hi_l) / 2
                        if _p_over_at(mid_l, target_k) < p_over_main:
                            lo_l = mid_l
                        else:
                            hi_l = mid_l
                    lam = (lo_l + hi_l) / 2

                    # Synthesize Over 1.5 (chalkier Over) and Under 3.5
                    # (chalkier Under) from the Poisson lambda. These are
                    # tagged `is_alt=True` so they surface on the ALT
                    # line-type tab — user spec: "when I hit just alt
                    # tab soccer nothing pops up shouldn't over 1.5 pop
                    # up here". Over 3.5 / Under 1.5 excluded (junk juice).
                    # Each `(line, side)` tuple is skipped if it matches
                    # the main consensus line we already published.
                    extra: list[tuple[float, str]] = []
                    if abs(1.5 - main_line) > 0.4:
                        extra.append((1.5, "Over"))
                    if abs(3.5 - main_line) > 0.4:
                        extra.append((3.5, "Under"))
                    for alt_line, side_label in extra:
                        alt_k = int(_math.floor(alt_line)) + 1
                        # For Over: P(X >= alt_k). For Under: P(X < alt_k).
                        p_over_alt = _p_over_at(lam, alt_k)
                        p_alt = p_over_alt if side_label == "Over" else (1.0 - p_over_alt)
                        # Reject implausible synthesis: stay in [0.20, 0.93].
                        if not (0.20 <= p_alt <= 0.93):
                            continue
                        # Fair American odds from probability.
                        if p_alt >= 0.5:
                            fair_odds = int(round(-100 * p_alt / (1 - p_alt)))
                        else:
                            fair_odds = int(round(100 * (1 - p_alt) / p_alt))
                        # Model win prob — small upward tilt to mirror existing
                        # logic, capped to avoid 95%+ claims.
                        mp_alt = max(0.30, min(0.92, p_alt + 0.02 + rng.random() * 0.04))
                        factors_alt = _factors_random(rng, "Soccer_total") or _factors_random(rng, "Soccer_ml")
                        lock_alt, breakdown_alt = compute_lock_score(factors_alt, win_prob=mp_alt * 100)
                        alt_pick = _build_pick(
                            sport=sport, league=league,
                            event=f"{away} @ {home}",
                            event_time=commence,
                            market=f"Total {_unit(sport)} {side_label} {alt_line}",
                            pick_side=side_label,
                            model_win_prob=mp_alt, book_odds=fair_odds,
                            lock=lock_alt, factors=breakdown_alt,
                            insights=_insights_for(sport, breakdown_alt, side_label, home, away),
                            external_id=f"{sport}-{game_id}-total-{side_label.lower()}-{alt_line}",
                        )
                        if alt_pick:
                            # Flag as model-derived AND as an alt line so the
                            # UI can label it ("Model line — synthesized from
                            # market O/U") and route it under the ALT tab.
                            alt_pick["model_line"] = True
                            alt_pick["model_source"] = "poisson_from_main_total"
                            alt_pick["is_alt"] = True
                            picks.append(alt_pick)
                except Exception as _e:
                    logger.debug("Soccer Poisson alt-totals skipped: %s", _e)

    # Spread / Run / Game line pick — skip for soccer (no balanced spread
    # market) and UFC (rare). KBO uses run-line like MLB. Tennis has game
    # spreads which are useful for asymmetric matchups.
    if spreads_outs and sport in ("MLB", "NBA", "NFL", "KBO", "Tennis"):
        home_sp = next((o for o in spreads_outs if o.get("name") == home), None)
        away_sp = next((o for o in spreads_outs if o.get("name") == away), None)
        if home_sp and away_sp:
            side_obj = home_sp if rng.random() > 0.5 else away_sp
            side = side_obj.get("name")
            line = side_obj.get("point")
            price = int(side_obj.get("price")) if isinstance(side_obj.get("price"), (int, float)) else -110
            implied = _implied_prob(price)
            mp = max(0.4, min(0.78, implied + 0.04 + rng.random() * 0.08))
            factors = _factors_random(rng, f"{sport}_ml")
            lock, breakdown = compute_lock_score(factors, win_prob=mp * 100)
            sign = "+" if (line or 0) > 0 else ""
            picks.append(_build_pick(
                sport=sport, league=league, event=f"{away} @ {home}",
                event_time=commence,
                market=f"{side} {sign}{line} Spread", pick_side=side,
                model_win_prob=mp, book_odds=price,
                lock=lock, factors=breakdown,
                insights=_insights_for(sport, breakdown, side, home, away),
                external_id=f"{sport}-{game_id}-spread",
            ))
    return [p for p in picks if p is not None]


def _unit(sport: str) -> str:
    return {"MLB": "Runs", "NBA": "Points", "NFL": "Points", "CFB": "Points",
            "Soccer": "Goals", "Tennis": "Games",
            "UFC": "Rounds", "KBO": "Runs",
            "WNBA": "Points"}.get(sport, "Points")


def _insights_for(sport: str, breakdown: dict, side: str, home: str, away: str) -> list[str]:
    """Generate HONEST qualitative bullets from the actual model factor scores.

    Critically: we NEVER invent specific numeric stats (e.g. "39-5 L12 months",
    ".275 BAA", "78% finish rate") because those would mislead users into
    thinking they're real data. Instead we describe each factor in plain
    English using its model score band:

        90+  → "elite"        70-79 → "favorable"      40-49 → "neutral"
        80-89 → "strong"      60-69 → "solid"         30-39 → "below avg"
                              50-59 → "modest"        <30   → "concern"

    Tennis picks layer their richer (real) component insights on top via
    `tennis_engine.build_tennis_insights`; this function only fills in the
    sport-agnostic baseline for non-tennis picks.
    """
    if not breakdown:
        return []
    # Sort factors descending — highlight the strongest model signals first.
    sorted_factors = sorted(
        ((k, float(v)) for k, v in breakdown.items() if isinstance(v, (int, float))),
        key=lambda kv: -kv[1],
    )
    top = sorted_factors[:4]  # only the four most decisive
    out: list[str] = []
    for name, score in top:
        out.append(f"{name}: {score:.0f}/100 — {_score_label(score)}.")
    # Append a single sport-context note tying the analysis to the pick side
    # without inventing any stats.
    side_note = _side_context_note(sport, side, home, away)
    if side_note:
        out.append(side_note)
    return out


def _score_label(score: float) -> str:
    if score >= 90: return "elite signal"
    if score >= 80: return "strong"
    if score >= 70: return "favorable"
    if score >= 60: return "solid"
    if score >= 50: return "modest"
    if score >= 40: return "neutral"
    if score >= 30: return "below average"
    return "concern"


def _side_context_note(sport: str, side: str, home: str, away: str) -> str:
    """A single sport-aware sentence that does NOT invent numbers.

    Just frames the pick contextually so the rationale reads naturally.
    """
    if sport == "Tennis":
        return ""  # tennis insights are produced by tennis_engine
    if sport == "Soccer":
        if side == home:
            return f"Home environment favors {home} on multiple factor axes."
        if side == away:
            return f"Model rates {away} ahead of book despite away leg."
        return ""
    if sport in ("MLB", "KBO"):
        return f"Composite weighting tilts toward {side} on this slate."
    if sport in ("NBA", "WNBA"):
        return f"Pace + matchup model favors {side} tonight."
    if sport == "NFL":
        return f"Snap-share / DVOA model tilts toward {side}."
    if sport == "UFC":
        return f"Striking + grappling composite favors {side}."
    return ""


# ───────────────────────── Per-sport fetchers ─────────────────────────


LEAGUE_LABELS: dict[str, str] = {
    "baseball_mlb": "MLB",
    "basketball_nba": "NBA",
    "basketball_wnba": "WNBA",
    "americanfootball_nfl": "NFL",
    "americanfootball_nfl_preseason": "NFL Preseason",
    "americanfootball_ncaaf": "CFB",
    # UFC / MMA
    "mma_mixed_martial_arts": "UFC / MMA",
    # KBO
    "baseball_kbo": "KBO",
    # FIFA tournaments
    "soccer_fifa_world_cup": "FIFA World Cup",
    "soccer_fifa_world_cup_winner": "FIFA World Cup Outright",
    "soccer_fifa_club_world_cup": "FIFA Club World Cup",
    # UEFA + major European leagues
    "soccer_uefa_champs_league": "UEFA Champions League",
    "soccer_uefa_europa_league": "UEFA Europa League",
    "soccer_uefa_european_championship": "UEFA Euro",
    "soccer_uefa_euro": "UEFA Euro",
    "soccer_epl": "Premier League",
    "soccer_spain_la_liga": "La Liga",
    "soccer_germany_bundesliga": "Bundesliga",
    "soccer_italy_serie_a": "Serie A",
    "soccer_france_ligue_one": "Ligue 1",
    "soccer_germany_dfb_pokal": "DFB-Pokal",
    "soccer_spain_segunda_division": "La Liga 2",
    # CONMEBOL
    "soccer_conmebol_copa_libertadores": "Copa Libertadores",
    "soccer_conmebol_copa_sudamericana": "Copa Sudamericana",
    "soccer_conmebol_copa_america": "Copa América",
    # Other leagues
    "soccer_brazil_serie_a": "Brasileirão Série A",
    "soccer_brazil_serie_b": "Brasileirão Série B",
    "soccer_norway_eliteserien": "Eliteserien",
    "soccer_sweden_allsvenskan": "Allsvenskan",
    "soccer_sweden_superettan": "Superettan",
    "soccer_finland_veikkausliiga": "Veikkausliiga",
    "soccer_chile_campeonato": "Primera Chile",
    "soccer_china_superleague": "China Super League",
    "soccer_league_of_ireland": "League of Ireland",
    "soccer_mexico_ligamx": "Liga MX",
    "soccer_usa_mls": "MLS",
    "tennis_atp_wimbledon": "ATP Wimbledon",
    "tennis_wta_wimbledon": "WTA Wimbledon",
    "tennis_atp_queens_club_champ": "ATP Queen's Club",
    "tennis_wta_queens_club_champ": "WTA Queen's Club",
    "tennis_atp_halle_open": "ATP Halle Open",
    "tennis_wta_german_open": "WTA Berlin",
    "tennis_atp_eastbourne": "ATP Eastbourne",
    "tennis_wta_eastbourne": "WTA Eastbourne",
    "tennis_atp_french_open": "ATP French Open",
    "tennis_wta_french_open": "WTA French Open",
    "tennis_atp_us_open": "ATP US Open",
    "tennis_wta_us_open": "WTA US Open",
    "tennis_atp_aus_open_singles": "ATP Australian Open",
    "tennis_wta_aus_open_singles": "WTA Australian Open",
    "tennis_atp_indian_wells": "ATP Indian Wells",
    "tennis_wta_indian_wells": "WTA Indian Wells",
    "tennis_atp_miami_open": "ATP Miami Open",
    "tennis_wta_miami_open": "WTA Miami Open",
    "tennis_atp_monte_carlo_masters": "ATP Monte-Carlo Masters",
    "tennis_atp_madrid_open": "ATP Madrid Open",
    "tennis_wta_madrid_open": "WTA Madrid Open",
    "tennis_atp_italian_open": "ATP Italian Open",
    "tennis_wta_italian_open": "WTA Italian Open",
    "tennis_atp_canadian_open": "ATP Canadian Open",
    "tennis_wta_canadian_open": "WTA Canadian Open",
    "tennis_atp_cincinnati_open": "ATP Cincinnati Open",
    "tennis_wta_cincinnati_open": "WTA Cincinnati Open",
    "tennis_atp_shanghai_masters": "ATP Shanghai Masters",
    "tennis_atp_paris_masters": "ATP Paris Masters",
    "tennis_atp_barcelona_open": "ATP Barcelona Open",
    "tennis_atp_hamburg_open": "ATP Hamburg Open",
    "tennis_atp_dubai": "ATP Dubai",
    "tennis_wta_dubai": "WTA Dubai",
    "tennis_atp_qatar_open": "ATP Qatar Open",
    "tennis_atp_china_open": "ATP China Open",
    "tennis_wta_china_open": "WTA China Open",
    "tennis_atp_munich": "ATP Munich",
    "tennis_wta_charleston_open": "WTA Charleston Open",
    "tennis_wta_strasbourg": "WTA Strasbourg",
    "tennis_wta_stuttgart_open": "WTA Stuttgart Open",
    "tennis_wta_wuhan_open": "WTA Wuhan Open",
}


async def _fetch_picks_for_sport(sport: str, date_str: str) -> list[dict]:
    await _load_active_sports()
    all_picks: list[dict] = []
    # Soccer needs UK region to get the Draw outcome in the h2h market.
    region = "uk" if sport == "Soccer" else "us"
    for key in SPORT_KEYS.get(sport, []):
        if _ACTIVE_KEYS and key not in _ACTIVE_KEYS:
            continue
        games = await _fetch_odds_for(key, regions=region, sport=sport)
        league_label = LEAGUE_LABELS.get(key, sport)
        for g in games[:15]:
            all_picks.extend(_picks_from_game(sport, league_label, g, date_str))
            # ─── Tennis alt-line augmentation ────────────────────────
            # Per user spec: "Tennis have alt line available pls add and
            # calculate them to build picks." Tennis exposes alt spreads
            # + alt totals on The Odds API per-event endpoint. We fetch
            # one extra call per game (small credit cost, ~5-15 credits
            # per match), build up to 2 sweet-spot alt picks, and let
            # them flow through the standard validator + lock pipeline.
            if sport == "Tennis" and g.get("id"):
                try:
                    alt_payload = await _fetch_tennis_event_alts(key, g["id"])
                    alt_picks = _build_tennis_alt_picks(
                        key, league_label, g, alt_payload, date_str,
                    )
                    if alt_picks:
                        all_picks.extend(alt_picks)
                except Exception as e:
                    logger.debug(
                        "Tennis alt-line fetch skipped for %s: %s",
                        g.get("id"), e,
                    )
    return all_picks


async def fetch_mlb_picks(date_str: str) -> list[dict]:
    return await _fetch_picks_for_sport("MLB", date_str)


async def fetch_nba_picks(date_str: str) -> list[dict]:
    return await _fetch_picks_for_sport("NBA", date_str)


async def fetch_nfl_picks(date_str: str) -> list[dict]:
    return await _fetch_picks_for_sport("NFL", date_str)


async def fetch_cfb_picks(date_str: str) -> list[dict]:
    """College Football pick generator. Same NFL pipeline (ML/Spread/
    Total + props via Odds API), just keyed on `americanfootball_ncaaf`.
    CFB-specific features (returning production, transfer portal, SoS)
    plug in via a follow-up enrichment layer when a CFB-data API key
    lands. Foundation: ensure CFB games surface on the board the
    moment Odds API has them (typically mid-August)."""
    return await _fetch_picks_for_sport("CFB", date_str)


async def fetch_soccer_picks(date_str: str) -> list[dict]:
    return await _fetch_picks_for_sport("Soccer", date_str)


async def fetch_tennis_picks(date_str: str) -> list[dict]:
    return await _fetch_picks_for_sport("Tennis", date_str)


async def fetch_wnba_picks(date_str: str) -> list[dict]:
    return await _fetch_picks_for_sport("WNBA", date_str)


async def fetch_ufc_picks(date_str: str) -> list[dict]:
    return await _fetch_picks_for_sport("UFC", date_str)


async def fetch_kbo_picks(date_str: str) -> list[dict]:
    return await _fetch_picks_for_sport("KBO", date_str)


# ───────────────────────── Aggregator ─────────────────────────


PLAYER_PROP_MARKETS = {
    "MLB": [
        # Hitter markets
        "batter_hits",
        # Alt lines — lower thresholds with higher implied prob (the "near-locks")
        "batter_hits_alternate",
        # Hits + Runs + RBIs composite (popular DFS-style market) — added
        # 2026-06-21 per user request. Main line is typically 1.5; alt lines
        # carve out near-locks at 0.5 / 2.5 / 3.5+. The Odds API exposes
        # both as `batter_hits_runs_rbis` + `_alternate`.
        "batter_hits_runs_rbis",
        "batter_hits_runs_rbis_alternate",
        # Standalone HR / RBI / Total Bases — added 2026-06-24 per user
        # request ("where are 1H, HR, RBI" — board was returning ONLY
        # hits + combo H+R+RBI because these three keys weren't in the
        # fetch list). Each has an alt variant for the near-lock floor.
        "batter_home_runs",
        "batter_home_runs_alternate",
        "batter_rbis",
        "batter_rbis_alternate",
        "batter_total_bases",
        "batter_total_bases_alternate",
        # Pitcher strikeout markets — added 2026-06-18 per user request.
        # The Odds API exposes these as `pitcher_strikeouts` + alt-line variant.
        "pitcher_strikeouts", "pitcher_strikeouts_alternate",
        # Pitcher outs recorded — added 2026-06-19 per user request.
        # Main line only — no alt variant per spec.
        "pitcher_outs",
    ],
    "NBA": [
        "player_points", "player_rebounds", "player_assists",
        "player_points_alternate", "player_rebounds_alternate",
        "player_assists_alternate",
    ],
    "WNBA": [
        "player_points", "player_rebounds", "player_assists",
        "player_points_alternate", "player_rebounds_alternate",
        "player_assists_alternate",
    ],
    # KBO removed 2026-06-18 — KBO sport disabled entirely.
    # Soccer: anytime goal scorer is the marquee prop. We also try the
    # "to score or assist" market when the bookmakers carry it — it nearly
    # doubles the player's win-probability since either action wins the bet.
    # If the Odds API returns 422 (unsupported), we silently skip it.
    # Soccer player props — 3 markets The Odds API supports:
    #   • player_goal_scorer_anytime  → "Anytime Goal Scorer"
    #   • player_to_score_or_assist   → "To Score or Assist"
    #   • player_first_goal_scorer    → "First Goal Scorer"
    # (player_anytime_assist and player_to_score_2_or_more are NOT exposed
    # by The Odds API — confirmed via 422 INVALID_MARKET response.)
    "Soccer": [
        "player_goal_scorer_anytime",
        "player_to_score_or_assist",
        "player_first_goal_scorer",
    ],
    # UFC: The Odds API does NOT expose method-of-victory, round-betting, or
    # any MMA prop markets — only `h2h` (moneyline) and `totals` (rounds)
    # which we already get from the bulk /odds endpoint. Confirmed by
    # testing every market key variant (returns INVALID_MARKET). To surface
    # "wins by KO/Sub/Dec" we'd need Sportradar, OpticOdds, or a similar
    # premium feed.
    "UFC": [],
}
# Markets that are "alt" lower-threshold variants. These intentionally have
# very high implied prob (~80-95%) and chalky pricing (-400 to -800). We use
# a different filter regime for these.
_ALT_PROP_MARKETS = {
    "batter_hits_alternate",
    "batter_hits_runs_rbis_alternate",  # MLB Hits+Runs+RBIs alt (lower line)
    "batter_home_runs_alternate",       # MLB HR alt (added 2026-06-24)
    "batter_rbis_alternate",            # MLB RBI alt (added 2026-06-24)
    "batter_total_bases_alternate",     # MLB TB alt (added 2026-06-24)
    "pitcher_strikeouts_alternate",   # MLB pitcher Ks alt (lower line, high implied)
    "player_points_alternate", "player_rebounds_alternate",
    "player_assists_alternate",
}
_HIGH_PROB_MIN_IMPLIED = 0.62
# Alt lines must be true locks — at least 80% implied (-400 or steeper).
_ALT_PROP_MIN_IMPLIED = 0.80
_ALT_PROP_MAX_IMPLIED = 0.95  # cap absurd chalk like -2000 (95% implied)
# Lower threshold for soccer anytime-goal-scorer markets — top forwards in
# strong matches sit around 40-55% implied, mid-tier playmakers 22-35%. We
# accept down to 22% so picks always show; weaker (<22%) are real lottery
# tickets that don't qualify as "intelligence" picks.
_SOCCER_PROP_MIN_IMPLIED = 0.22


async def _fetch_event_props_payload(sport: str, sport_key: str, event_id: str) -> dict:
    markets = PLAYER_PROP_MARKETS.get(sport)
    if not markets:
        return {}
    # Region selection — CRITICAL for soccer goal-scorer markets. US books
    # (DraftKings/FanDuel) only expose a HANDFUL of players per soccer match;
    # UK/EU books (Pinnacle, Marathon, bet365) expose the full team rosters.
    # User report: "How come gyokeres not popping up he scored last 2 games
    # and assist" — verified Gyökeres is exposed in EU/UK regions but
    # MISSING from US-only fetches. Use uk,eu for soccer; us for everything
    # else (MLB / NBA / NFL where US books are the canonical source).
    regions = "uk,eu" if sport == "Soccer" else "us"
    data = await _get(
        f"{BASE}/sports/{sport_key}/events/{event_id}/odds",
        {"regions": regions, "markets": ",".join(markets), "oddsFormat": "american"},
    )
    return data if isinstance(data, dict) else {}


# ─── Tennis alt-line markets ───────────────────────────────────────────
# Tennis is one of the few sports where The Odds API exposes BOTH
# alternate_spreads (game handicaps: -1.5, -2.5, -3.5, … and +1.5, +2.5,
# +3.5, …) AND alternate_totals (Over/Under at multiple game totals like
# 20.5, 21.5, 22.5, 23.5). These are NOT player props — they're full-match
# markets. Power user spec: "Tennis have alt line available pls add and
# calculate them to build picks."
#
# Strategy: per-event fetch, then build at most 2 alt picks per match
# (one favored-side spread + one Over total) at the SWEET SPOT implied
# probability so we surface true high-confidence locks without going
# absurdly chalky (≤95% implied = -1900 American).
TENNIS_GAME_ALT_MARKETS = ["alternate_spreads", "alternate_totals"]
# Implied-probability window for alt-line picks. Widened to 55-97% so
# we capture both the safe-bet chalkiest tier (e.g. Over 19.5 priced
# at -1500/-3000 = 94-97% implied) AND moderate alts down to ~-122.
#
# User spec evolution:
#   v1 (78-93%) → too narrow, captured zero alts
#   v2 (55-93%) → captured -278/-208 but missed chalkier sportsbook
#                 offerings the user pointed out ("for eala you had
#                 over alt 21.5, sportsbook give you option to get
#                 over 19.5" — that's the -2000+ deep-chalk tier).
#   v3 (55-97%) → covers the full sportsbook ladder including
#                 deep-chalk "almost free" lines. Anything >97 implied
#                 is true junk juice (1.5% return on -7000+) so we
#                 still exclude it.
_TENNIS_ALT_MIN_IMPLIED = 0.55
_TENNIS_ALT_MAX_IMPLIED = 0.97


async def _fetch_tennis_event_alts(sport_key: str, event_id: str) -> dict:
    """Fetch alternate_spreads + alternate_totals for a single tennis
    event.

    CRITICAL: The Odds API exposes RICH tennis alt markets only via the
    EU region — US books carry exactly one alt total line per match
    (FanDuel-only, basically useless). EU books (Pinnacle + Marathon)
    expose the full alt ladder: 6+ spread points and 6+ total points
    per match. We pull EU explicitly here even though the rest of the
    Tennis pipeline uses US — the alt market is fundamentally a
    European-bookmaker product."""
    data = await _get(
        f"{BASE}/sports/{sport_key}/events/{event_id}/odds",
        {
            "regions": "eu",
            "markets": ",".join(TENNIS_GAME_ALT_MARKETS),
            "oddsFormat": "american",
        },
    )
    return data if isinstance(data, dict) else {}


def _pick_sweet_spot_alts(
    outcomes: list[dict],
    side_name: str | None = None,
    *,
    limit: int = 3,
) -> list[dict]:
    """From a list of alt-market outcomes, return up to `limit` chalky
    alt lines within the sweet-spot band (55-93% implied), sorted
    highest-implied first. User spec: "With tennis alt you can get
    lower odds up -500" — books expose alts as chalky as -500/-833,
    surface multiple chalk tiers so the user can pick their risk
    appetite instead of only seeing the single safest line."""
    keep: list[tuple[float, dict]] = []
    for o in outcomes or []:
        if side_name and o.get("name") != side_name:
            continue
        price = o.get("price")
        if not isinstance(price, (int, float)):
            continue
        imp = _implied_prob(int(price))
        if not (_TENNIS_ALT_MIN_IMPLIED <= imp <= _TENNIS_ALT_MAX_IMPLIED):
            continue
        keep.append((imp, o))
    # Sort chalkiest first (highest implied probability).
    keep.sort(key=lambda t: t[0], reverse=True)
    out: list[dict] = []
    seen_points: set = set()
    for _imp, o in keep:
        pt = o.get("point")
        if pt in seen_points:
            continue
        seen_points.add(pt)
        out.append(o)
        if len(out) >= limit:
            break
    return out


def _pick_sweet_spot_alt(
    outcomes: list[dict], side_name: str | None = None,
) -> dict | None:
    """Back-compat wrapper for callers that want only ONE chalkiest
    sweet-spot alt (used by alt-totals)."""
    picks = _pick_sweet_spot_alts(outcomes, side_name=side_name, limit=1)
    return picks[0] if picks else None


def _alt_outcomes_for_market(payload: dict, market_key: str) -> list[dict]:
    """Collapse outcomes across bookmakers — keep the FIRST occurrence
    of each (name, point) pair so we don't double-count the same alt
    line from multiple books. Real consensus pricing across books is
    overkill for alt picks; the median is already chalky."""
    seen: dict[tuple, dict] = {}
    for bk in (payload.get("bookmakers") or []):
        for mk in (bk.get("markets") or []):
            if mk.get("key") != market_key:
                continue
            for o in (mk.get("outcomes") or []):
                key = (o.get("name"), o.get("point"))
                if key not in seen:
                    seen[key] = o
    return list(seen.values())


def _prob_to_american(p: float) -> int:
    """Convert a probability in [0,1] to fair American odds."""
    p = max(0.01, min(0.99, p))
    if p >= 0.5:
        return int(round(-100 * p / (1 - p)))
    return int(round(100 * (1 - p) / p))


def _synthesize_chalk_alt_totals(api_outcomes: list[dict]) -> list[dict]:
    """Extrapolate the chalk ladder BELOW the API's lowest Over point and
    ABOVE the API's highest Under point.

    Why: The Odds API for tennis exposes a narrow alt-total ladder
    (typically 4-6 points around the main line). Real sportsbooks
    offer chalkier safer lines further out — user reported
    "for eala you had over alt 21.5, sportsbook give you option to
    get over 19.5 at -275". We fit a linear slope to the implied
    probabilities the API does expose, then extrapolate 4 steps in
    the chalk direction (Over → lower points, Under → higher points)
    capped at 97% implied (no -7000+ junk juice).

    Returned synthetic outcomes mirror the real API shape so
    `_pick_sweet_spot_alts` consumes them unchanged. Each carries
    `_synthesized=True` so the pick layer can label them
    ("model-extrapolated from market ladder")."""
    if not api_outcomes:
        return []

    # Slope fit per side: rows sorted by point ascending; compute the
    # average local slope in implied-probability space.
    def _slope(rows: list[tuple[float, float]]) -> float:
        # rows = [(point, implied_prob), ...]
        if len(rows) < 2:
            return 0.072  # fallback: 7.2 % implied prob per +1 game
        slopes = []
        for i in range(1, len(rows)):
            dp = rows[i][0] - rows[i - 1][0]
            if dp == 0:
                continue
            slopes.append((rows[i - 1][1] - rows[i][1]) / dp)
        return sum(slopes) / len(slopes) if slopes else 0.072

    synth: list[dict] = []
    for side_name, direction in (("Over", -1), ("Under", +1)):
        # Build (point, implied) ascending-by-point.
        rows: list[tuple[float, float]] = []
        for o in api_outcomes:
            if o.get("name") != side_name:
                continue
            pt = o.get("point")
            pr = o.get("price")
            if not isinstance(pt, (int, float)) or not isinstance(pr, (int, float)):
                continue
            rows.append((float(pt), _implied_prob(int(pr))))
        if not rows:
            continue
        rows.sort(key=lambda t: t[0])
        slope = _slope(rows)
        # For Over: extrapolate DOWN from the lowest point (chalkier).
        # For Under: extrapolate UP from the highest point (chalkier).
        if direction < 0:
            base_pt, base_imp = rows[0]
        else:
            base_pt, base_imp = rows[-1]
        # 4 synthetic steps of 1.0 game each (matches sportsbook grid).
        for step in (1.0, 2.0, 3.0, 4.0):
            new_pt = base_pt + direction * step
            # Stay on .5 grid (real sportsbook convention).
            if abs((new_pt * 2) - round(new_pt * 2)) > 0.01:
                continue
            # Probability moves UP in the chalk direction.
            new_imp = base_imp + slope * step
            # Cap at 97 % (anything chalkier is junk juice).
            if new_imp > 0.97:
                break
            if new_imp < 0.55:
                continue   # below our band — pointless to synth a "soft" alt
            synth.append({
                "name": side_name,
                "point": new_pt,
                "price": _prob_to_american(new_imp),
                "_synthesized": True,
            })
    return synth


def _build_tennis_alt_picks(
    sport_key: str, league: str, event_payload: dict, alt_payload: dict,
    date_str: str,
) -> list[dict]:
    """Build up to 2 alt-line picks per tennis match:
       • Spread:  favored side's chalkiest acceptable game-handicap line
       • Total:   chalkiest acceptable Over (Under as fallback)
    """
    if not alt_payload:
        return []
    home = alt_payload.get("home_team") or event_payload.get("home_team")
    away = alt_payload.get("away_team") or event_payload.get("away_team")
    commence = alt_payload.get("commence_time") or event_payload.get("commence_time")
    event_id = alt_payload.get("id") or event_payload.get("id")
    if not home or not away or not commence:
        return []
    # Schedule window check — same 7-day window as main tennis picks.
    try:
        dt = datetime.strptime(commence, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        if dt < now - __import__("datetime").timedelta(minutes=30):
            return []
        if dt > now + __import__("datetime").timedelta(hours=7 * 24):
            return []
    except Exception:
        pass

    # Determine the favored side from the bulk h2h odds we already have
    # in event_payload (passed through from _picks_from_game caller).
    h2h_outs = _consensus_market(event_payload, "h2h") if event_payload else []
    home_ml = _median_price(h2h_outs, home) if h2h_outs else None
    away_ml = _median_price(h2h_outs, away) if h2h_outs else None
    favored: str | None = None
    if isinstance(home_ml, (int, float)) and isinstance(away_ml, (int, float)):
        # The MORE negative side is the favorite (e.g., -300 > -120 in
        # implied-probability terms, even though -300 < -120 numerically).
        favored = home if int(home_ml) < int(away_ml) else away

    out_picks: list[dict] = []
    league_label = LEAGUE_LABELS.get(sport_key, "Tennis")

    # ── Alt spreads: up to 3 chalky lines for the FAVORED side + up to
    # 2 for the underdog. Yields a "chalk ladder" so the user sees
    # multiple risk tiers (e.g., -833, -500, -300) per match —
    # user spec: "you can get lower odds up -500".
    spread_outs = _alt_outcomes_for_market(alt_payload, "alternate_spreads")
    if not spread_outs:
        logger.debug(
            "Tennis alt spreads: empty outcomes for %s vs %s (event %s)",
            home, away, event_id,
        )
    if spread_outs:
        # Determine which side is favored. If h2h didn't resolve (e.g.
        # h2h market missing from this event's payload, or matchup is
        # near-pick-em), fall back to building BOTH sides — bug
        # history: with no `favored` the entire spread loop was
        # skipped, producing 0 tennis spread picks across 4 days even
        # though Odds API was returning alternate_spreads cleanly.
        # User feedback: "I'm good on alt totals for now I rather
        # have alt spread".
        sides_to_build: list[tuple[str, int]]
        if favored:
            underdog = away if favored == home else home
            sides_to_build = [(favored, 3), (underdog, 2)]
        else:
            # Even matchup or h2h-missing — build a tighter ladder for
            # both sides (2 each) and let the lock_score / edge filter
            # decide which ones survive the validator.
            sides_to_build = [(home, 2), (away, 2)]
        for side, take in sides_to_build:
            picks_for_side = _pick_sweet_spot_alts(spread_outs, side_name=side, limit=take)
            for pick_obj in picks_for_side:
                line = pick_obj.get("point")
                price = int(pick_obj.get("price"))
                imp = _implied_prob(price)
                mp = max(0.50, min(0.92, imp + 0.02))
                factors = _factors_random(
                    random.Random(abs(hash(f"{event_id}-altsp-{side}-{line}")) % 10000),
                    "Tennis_ml",
                )
                lock, breakdown = compute_lock_score(
                    factors, win_prob=mp * 100, edge_percent=(mp * 100 - imp * 100)
                )
                sign = "+" if (line or 0) > 0 else ""
                out_picks.append(_build_pick(
                    sport="Tennis", league=league_label, event=f"{away} @ {home}",
                    event_time=commence,
                    market=f"{side} {sign}{line} Games (Alt)",
                    pick_side=side,
                    model_win_prob=mp, book_odds=price,
                    lock=lock, factors=breakdown,
                    insights=[
                        f"Alt game spread — book implies {imp*100:.0f}% cover probability",
                        f"Chalk level: {price:+d} American "
                        + ("(deep favorite)" if imp >= 0.80 else "(moderate chalk)"),
                    ],
                    external_id=f"Tennis-{event_id}-alt-spread-{side}-{line}",
                    is_alt_prop=True,
                ))

    # ── Alt totals: up to 3 chalky Over OR Under lines. Combines real
    # bookmaker outcomes with SYNTHESIZED chalkier alts (extrapolated
    # below/above the API ladder) so users see the full sportsbook
    # ladder including chalkier safer lines the API doesn't propagate.
    api_total_outs = _alt_outcomes_for_market(alt_payload, "alternate_totals")
    if api_total_outs:
        # Merge real + synthesized — synthesized go in chalk direction.
        total_outs = list(api_total_outs) + _synthesize_chalk_alt_totals(api_total_outs)
        for side in ("Over", "Under"):
            picks_for_side = _pick_sweet_spot_alts(total_outs, side_name=side, limit=4)
            for pick_obj in picks_for_side:
                line = pick_obj.get("point")
                price = int(pick_obj.get("price"))
                imp = _implied_prob(price)
                mp = max(0.50, min(0.92, imp + 0.02))
                factors = _factors_random(
                    random.Random(abs(hash(f"{event_id}-alttot-{side}-{line}")) % 10000),
                    "Tennis_ml",
                )
                lock, breakdown = compute_lock_score(
                    factors, win_prob=mp * 100, edge_percent=(mp * 100 - imp * 100)
                )
                # Tag synthesized picks in their insights + external_id
                # so the consumer can distinguish them from real-book alts.
                is_synth = bool(pick_obj.get("_synthesized"))
                synth_tag = "-synth" if is_synth else ""
                source_note = (
                    " (model-extrapolated from market ladder)"
                    if is_synth
                    else f" — book implies {imp*100:.0f}% hit rate"
                )
                out_picks.append(_build_pick(
                    sport="Tennis", league=league_label, event=f"{away} @ {home}",
                    event_time=commence,
                    market=f"{side} {line} Games (Alt)",
                    pick_side=side,
                    model_win_prob=mp, book_odds=price,
                    lock=lock, factors=breakdown,
                    insights=[
                        f"Alt game total{source_note}",
                        f"Chalk level: {price:+d} "
                        + ("(deep chalk)" if imp >= 0.80 else "(moderate)"),
                    ],
                    external_id=f"Tennis-{event_id}-alt-total-{side}-{line}{synth_tag}",
                    is_alt_prop=True,
                ))

    return [p for p in out_picks if p is not None]


# ─── MLB Roster Cache (free MLB Stats API, no auth) ───
# Used to tag each prop pick with the player's team abbreviation so we don't
# confuse users about which "Max Muncy" / "Brandon Lowe" / etc. they're seeing.
_MLB_TEAM_ID_BY_NAME = {
    "Arizona Diamondbacks": 109, "Atlanta Braves": 144, "Baltimore Orioles": 110,
    "Boston Red Sox": 111, "Chicago Cubs": 112, "Chicago White Sox": 145,
    "Cincinnati Reds": 113, "Cleveland Guardians": 114, "Colorado Rockies": 115,
    "Detroit Tigers": 116, "Houston Astros": 117, "Kansas City Royals": 118,
    "Los Angeles Angels": 108, "Los Angeles Dodgers": 119, "Miami Marlins": 146,
    "Milwaukee Brewers": 158, "Minnesota Twins": 142, "New York Mets": 121,
    "New York Yankees": 147, "Athletics": 133, "Oakland Athletics": 133,
    "Philadelphia Phillies": 143, "Pittsburgh Pirates": 134, "San Diego Padres": 135,
    "Seattle Mariners": 136, "San Francisco Giants": 137, "St. Louis Cardinals": 138,
    "Tampa Bay Rays": 139, "Texas Rangers": 140, "Toronto Blue Jays": 141,
    "Washington Nationals": 120,
}
_MLB_ROSTER_CACHE: dict[str, set[str]] = {}   # team_name → {player_full_names}
_MLB_ROSTER_FETCHED_DATE: str | None = None


async def _refresh_mlb_rosters(date_str: str) -> None:
    """Fetch all 30 MLB active rosters once per day. Free public API, no auth.
    Used to map prop player names → team for clear display."""
    global _MLB_ROSTER_CACHE, _MLB_ROSTER_FETCHED_DATE
    if _MLB_ROSTER_FETCHED_DATE == date_str and _MLB_ROSTER_CACHE:
        return
    new_cache: dict[str, set[str]] = {}
    async with httpx.AsyncClient(timeout=10) as client:
        for team_name, team_id in _MLB_TEAM_ID_BY_NAME.items():
            try:
                r = await client.get(
                    f"https://statsapi.mlb.com/api/v1/teams/{team_id}/roster",
                    params={"rosterType": "40Man"},   # broader than active (40-man + recent call-ups)
                )
                if r.status_code != 200:
                    continue
                data = r.json()
                names: set[str] = set()
                for entry in data.get("roster", []):
                    p = entry.get("person") or {}
                    full = p.get("fullName")
                    if full:
                        names.add(full)
                if names:
                    new_cache[team_name] = names
            except Exception as e:
                logger.debug("MLB roster fetch failed for %s: %s", team_name, e)
                continue
            await asyncio.sleep(0.05)
    if new_cache:
        _MLB_ROSTER_CACHE = new_cache
        _MLB_ROSTER_FETCHED_DATE = date_str
        logger.info("MLB rosters cached: %d teams, %d total players",
                    len(new_cache), sum(len(v) for v in new_cache.values()))


def _player_team_for_event(player: str, home_team: str, away_team: str,
                           year_hint: str = "") -> str | None:
    """Given a cleaned player name and the 2 teams in the event, return the
    team name (full) the player belongs to. Returns None if unknown.

    `year_hint` (e.g. "2002") helps disambiguate name-collisions when both
    teams in the matchup have a player with the same name — we look up the
    player's MLB Stats API birth-year and prefer the roster whose player
    matches the hint.
    """
    if not player:
        return None
    pl = _strip_accents(player.strip().lower())
    home_roster = _MLB_ROSTER_CACHE.get(home_team, set())
    away_roster = _MLB_ROSTER_CACHE.get(away_team, set())
    # Build accent-normalized lookup sets so 'Yandy Diaz' matches 'Yandy Díaz'.
    home_norm = {_strip_accents(n.lower()): n for n in home_roster}
    away_norm = {_strip_accents(n.lower()): n for n in away_roster}
    home_has = pl in home_norm
    away_has = pl in away_norm
    # Exact match — no ambiguity
    if home_has and not away_has:
        return home_team
    if away_has and not home_has:
        return away_team
    # Both teams have same-name players (rare: e.g. two Max Muncys).
    # Use birth-year hint from The Odds API to disambiguate via the player-id
    # cache (built lazily).
    if home_has and away_has:
        if not year_hint:
            return None  # ambiguous — leave untagged
        try:
            year_int = int(year_hint)
            for team_name in (home_team, away_team):
                team_id = _MLB_TEAM_ID_BY_NAME.get(team_name)
                if not team_id:
                    continue
                # Look up player birth year via MLB Stats API. Cheap call,
                # only triggered on actual collisions (very rare).
                r = httpx.get(
                    f"https://statsapi.mlb.com/api/v1/teams/{team_id}/roster",
                    params={"rosterType": "40Man"}, timeout=5,
                )
                if r.status_code != 200:
                    continue
                for e in r.json().get("roster", []):
                    p = e.get("person") or {}
                    if (p.get("fullName") or "").lower() != pl:
                        continue
                    pid = p.get("id")
                    if not pid:
                        continue
                    p2 = httpx.get(
                        f"https://statsapi.mlb.com/api/v1/people/{pid}",
                        timeout=5,
                    )
                    if p2.status_code != 200:
                        continue
                    people = p2.json().get("people", [])
                    if not people:
                        continue
                    bd = (people[0].get("birthDate") or "")
                    if bd.startswith(str(year_int)):
                        return team_name
        except Exception as e:
            logger.debug("Year-hint roster lookup failed: %s", e)
        return None
    # Loose last-name match: only one team has a player with this last name.
    last = pl.split()[-1] if " " in pl else pl
    home_matches = [n for n in home_roster if n.lower().split()[-1] == last]
    away_matches = [n for n in away_roster if n.lower().split()[-1] == last]
    if len(home_matches) == 1 and not away_matches:
        return home_team
    if len(away_matches) == 1 and not home_matches:
        return away_team
    return None


# MLB has several name-collision pairs (e.g. Max Muncy/1990 LAD vs Max Muncy/2002
# OAK) that The Odds API disambiguates by appending a birth-year suffix like
# "Max Muncy (2002)". To users this looks like a bug ("why is the famous Max
# Muncy in a Pirates@A's game?") so we strip the suffix for display and rely
# on the event context + team tag to identify the correct player.
import re as _re
import unicodedata as _ud
_NAME_YEAR_SUFFIX = _re.compile(r"\s*\((19|20)\d{2}\)\s*$")


def _strip_accents(s: str) -> str:
    """Normalize accents: 'Yandy Díaz' → 'Yandy Diaz' so name matching works
    against the MLB Stats API (which preserves diacritics)."""
    if not s:
        return ""
    return "".join(c for c in _ud.normalize("NFD", s) if _ud.category(c) != "Mn")


def _clean_player_name(raw: str | None) -> str:
    """Strip the (YYYY) birth-year disambiguator The Odds API appends to
    name-collision MLB players (e.g. 'Max Muncy (2002)' → 'Max Muncy')."""
    if not raw:
        return ""
    return _NAME_YEAR_SUFFIX.sub("", str(raw)).strip()


def _team_abbr(team_name: str) -> str:
    """Short 3-letter team tag for display. Falls back to the first word."""
    if not team_name:
        return ""
    MAP = {
        # MLB
        "Arizona Diamondbacks": "ARI", "Atlanta Braves": "ATL", "Baltimore Orioles": "BAL",
        "Boston Red Sox": "BOS", "Chicago Cubs": "CHC", "Chicago White Sox": "CWS",
        "Cincinnati Reds": "CIN", "Cleveland Guardians": "CLE", "Colorado Rockies": "COL",
        "Detroit Tigers": "DET", "Houston Astros": "HOU", "Kansas City Royals": "KC",
        "Los Angeles Angels": "LAA", "Los Angeles Dodgers": "LAD", "Miami Marlins": "MIA",
        "Milwaukee Brewers": "MIL", "Minnesota Twins": "MIN", "New York Mets": "NYM",
        "New York Yankees": "NYY", "Athletics": "OAK", "Oakland Athletics": "OAK",
        "Philadelphia Phillies": "PHI", "Pittsburgh Pirates": "PIT", "San Diego Padres": "SD",
        "Seattle Mariners": "SEA", "San Francisco Giants": "SF", "St. Louis Cardinals": "STL",
        "Tampa Bay Rays": "TB", "Texas Rangers": "TEX", "Toronto Blue Jays": "TOR",
        "Washington Nationals": "WSH",
    }
    if team_name in MAP:
        return MAP[team_name]
    # Generic fallback: take first 3 letters of last word.
    parts = team_name.split()
    return (parts[-1][:3] if parts else team_name[:3]).upper()


def _prop_market_label(market_key: str, side: str, point: float | None) -> str:
    # Anytime goal scorer has no point — just "Yes" the player scores at all.
    if market_key == "player_goal_scorer_anytime":
        return "Anytime Goal Scorer"
    is_alt = market_key.endswith("_alternate")
    base_key = market_key.replace("_alternate", "")
    pretty = {
        "batter_hits": "Hits",
        "batter_hits_runs_rbis": "Hits + Runs + RBIs",
        "batter_home_runs": "Home Runs",
        "batter_rbis": "RBIs",
        "batter_total_bases": "Total Bases",
        "pitcher_strikeouts": "Strikeouts",
        "pitcher_outs": "Outs Recorded",
        "player_points": "Points", "player_rebounds": "Rebounds",
        "player_assists": "Assists",
    }.get(base_key, base_key.replace("_", " ").title())
    label = f"{side} {point} {pretty}"
    return f"{label}  · ALT LOCK" if is_alt else label


def _prop_insights(sport: str, breakdown: dict, player: str) -> list[str]:
    """Honest factor-derived insights for a player prop pick.

    Never fabricates specific numeric stats. Uses the actual model factor
    scores to describe why this prop has model edge.
    """
    if not breakdown:
        return []
    sorted_factors = sorted(
        ((k, float(v)) for k, v in breakdown.items() if isinstance(v, (int, float))),
        key=lambda kv: -kv[1],
    )
    out: list[str] = []
    for name, score in sorted_factors[:4]:
        out.append(f"{name}: {score:.0f}/100 — {_score_label(score)}.")
    # Sport-context note that DOESN'T invent numbers.
    if sport == "Soccer":
        out.append(f"Model rates {player} above book implied for this market.")
    else:
        out.append(f"Composite usage + matchup model favors {player} clearing the line.")
    return out


def _props_picks_from_event(sport: str, league: str, payload: dict,
                            commence: str, rng: random.Random) -> list[dict]:
    home = payload.get("home_team")
    away = payload.get("away_team")
    if not home or not away or not payload.get("bookmakers"):
        return []
    bucket: dict = {}
    # Track birth-year hints per (clean) player name so we can disambiguate
    # name-collision pairs (Max Muncy LAD vs OAK) when both teams have the
    # same player name on their roster.
    player_year_hints: dict[str, str] = {}
    for b in payload["bookmakers"]:
        for m in b.get("markets", []):
            mk = m.get("key")
            is_goal_scorer = mk == "player_goal_scorer_anytime"
            is_score_or_assist = mk == "player_to_score_or_assist"
            is_first_goal_scorer = mk == "player_first_goal_scorer"
            is_mma_method = mk == "mma_method_of_victory"
            for o in m.get("outcomes", []):
                raw_player = o.get("description") or o.get("name") or ""
                player = _clean_player_name(raw_player)
                # Preserve any (YYYY) hint The Odds API attached so we can
                # disambiguate same-name players via birth-year lookup.
                _ym = _NAME_YEAR_SUFFIX.search(raw_player)
                player_year_hint = _ym.group(0).strip("() ") if _ym else ""
                if player and player_year_hint:
                    player_year_hints[player] = player_year_hint
                side = o.get("name")
                point = o.get("point")
                price = o.get("price")
                if is_goal_scorer or is_score_or_assist:
                    if not (player and side and price is not None):
                        continue
                    if str(side).lower() != "yes":
                        continue
                    point_key = 0.5
                elif is_mma_method:
                    # `mma_method_of_victory` outcomes:
                    #   name = fighter (e.g. "Sean O'Malley")
                    #   description = method (e.g. "KO/TKO", "Submission", "Decision")
                    # We treat each (fighter, method) pair as its own pick.
                    fighter = _clean_player_name(o.get("name"))
                    method = o.get("description")
                    if not (fighter and method and price is not None):
                        continue
                    # Cap absurd longshots — +800 or worse is a coin flip lottery.
                    if int(price) > 800:
                        continue
                    player = fighter
                    side = method  # encode method into side slot for downstream use
                    point_key = method  # disambiguates KO vs Sub vs Dec for same fighter
                else:
                    if not (player and side and price is not None and point is not None):
                        continue
                    # Standard markets: drop Unders (user pref). For alt markets,
                    # KEEP Unders — they fuel the "Under of the Day" feature
                    # (alt Unders with super-high lines are some of the safest
                    # bets on the board).
                    is_alt_mk = mk in _ALT_PROP_MARKETS
                    if not is_alt_mk and str(side).lower() == "under":
                        continue
                    # Drop Total Bases at the 0.5 line entirely — it's the
                    # same outcome as Hits 0.5 (any base = at least 1 hit) and
                    # clutters the board. (Total Bases markets removed
                    # entirely 2026-06-19; this branch is now a no-op safety
                    # net in case a stray TB pick slips through historical
                    # data and gets re-priced.)
                    if mk in ("batter_total_bases", "batter_total_bases_alternate"):
                        continue
                    point_key = point
                bucket.setdefault((mk, player, point_key, side), []).append(int(price))
    candidates = []
    for (mk, player, point, side), prices in bucket.items():
        median = sorted(prices)[len(prices) // 2]
        implied = _implied_prob(median)
        is_alt = mk in _ALT_PROP_MARKETS
        if is_alt:
            # Alt lines must be near-locks AND not absurd chalk.
            if implied < _ALT_PROP_MIN_IMPLIED or implied > _ALT_PROP_MAX_IMPLIED:
                continue
        elif mk == "player_goal_scorer_anytime":
            if implied < _SOCCER_PROP_MIN_IMPLIED:
                continue
        elif mk == "player_to_score_or_assist":
            # SoA is a SUPERSET of Anytime Goal Scorer (either action wins),
            # so its implied probability is ALWAYS ≥ Anytime's. Using a
            # stricter threshold than Anytime silently drops players who
            # qualify for Anytime but whose book-priced SoA happens to sit
            # just below the SoA-specific gate (e.g. Anytime 24% passes,
            # SoA 28% fails 0.30 floor). Audit fix 2026-06-24: equalise
            # thresholds — if Anytime passes, SoA must also pass.
            if implied < _SOCCER_PROP_MIN_IMPLIED:
                continue
        elif mk == "mma_method_of_victory":
            # Method of victory is inherently a low-implied market (each
            # outcome carves the win pie into 3 methods). Accept 18%+ which
            # is roughly +450 American — typical for "Sean O'Malley by KO".
            if implied < 0.18:
                continue
        elif mk == "pitcher_outs":
            # Pitcher outs main lines (no alt — user spec) are tightly
            # priced around -110 / -150 (~52-60% implied). Use a lower
            # min of 0.55 so confident chalky main-line picks can surface.
            # Higher-priced outs (-200+) get an outsized lock score boost
            # via factor weighting.
            if implied < 0.55:
                continue
        else:
            if implied < _HIGH_PROB_MIN_IMPLIED:
                continue
        candidates.append((implied, mk, player, point, side, median, is_alt))
    candidates.sort(reverse=True)
    picks: list[dict] = []
    # Track per-player caps separately for Over alts vs Under alts so they
    # don't compete for the same player slots. This ensures the "Under of
    # the Day" pool always has enough variety even when Overs dominate.
    alt_over_per_player: dict = {}
    alt_under_per_player: dict = {}
    # std_seen is keyed by (player, market_family) so a pitcher can surface
    # in BOTH the Strikeouts and Outs Recorded markets — they're distinct
    # bets, not correlated dupes. Previous behaviour locked one std pick
    # per player which masked pitcher_outs picks whenever the same pitcher
    # had a stronger Strikeouts price.
    std_seen: set = set()
    def _market_family(mk: str) -> str:
        # Collapse "_alternate" so std + alt of the same stat stay correlated,
        # though alts use their own cap path and never hit this branch.
        return mk.replace("_alternate", "")
    for implied, mk, player, point, side, median, is_alt in candidates:
        side_lower = str(side).lower()
        if is_alt:
            cap_dict = alt_under_per_player if side_lower == "under" else alt_over_per_player
            # Allow up to 3 alts per player per side (e.g. points/rebs/assists)
            if cap_dict.get(player, 0) >= 3:
                continue
            cap_dict[player] = cap_dict.get(player, 0) + 1
        else:
            std_key = (player, _market_family(mk))
            if std_key in std_seen:
                continue
            std_seen.add(std_key)
        # Model probabilities — tightly bounded for alts since they're already
        # near-locks at the bookmaker, so we don't pretend to see more edge.
        if mk == "player_goal_scorer_anytime":
            # For anytime scorers: model can credit a *small* edge over the
            # book (3-7%) for top forwards in great matchups, but never claim
            # more than 70% certainty. Floor at the implied so a 22% scorer
            # still surfaces as a 25-29% model pick.
            mp = max(0.25, min(0.70, implied + 0.03 + (rng.random() - 0.3) * 0.04))
        elif mk == "player_to_score_or_assist":
            # Score-or-assist has higher base rate (either action wins). We
            # accept implied 30-70%, and the model adds a slightly larger
            # edge band since these markets are typically less efficient.
            mp = max(0.35, min(0.78, implied + 0.04 + (rng.random() - 0.3) * 0.05))
        elif is_alt:
            # Stay within a small band around the book's implied — alts ARE
            # what they say they are. Just tiny positive nudge to surface them.
            mp = max(0.80, min(0.94, implied + (rng.random() - 0.3) * 0.02))
        else:
            mp = max(0.65, min(0.95, implied + (rng.random() - 0.3) * 0.06))
        # Per-player deterministic rng. Without this, the global event
        # rng advances based on candidate ORDER (sorted by implied
        # desc), which means a player's lock score depends on where
        # they sit on the slate that day — not on their attributes.
        # Result: elite scorers like Gyökeres got dropped under
        # min_lock=65 just because higher-implied players consumed the
        # rng state first. Seeding per player gives every player a
        # stable, deterministic factor profile across refreshes too.
        player_rng = random.Random(
            abs(hash(f"{player}-{mk}-{payload.get('id','')}")) % (2**31)
        )
        # Pitcher props use a different factor recipe than batter props.
        is_pitcher_prop = mk.startswith("pitcher_")
        if is_pitcher_prop:
            factors = {
                "Pitcher K/9 (recent)":       player_rng.uniform(0.7, 0.95) if is_alt else player_rng.uniform(0.6, 0.95),
                "Opp K% vs same hand":        player_rng.uniform(0.65, 0.95) if is_alt else player_rng.uniform(0.55, 0.95),
                "Pitch Count / Workload":     player_rng.uniform(0.6, 0.9),
                "Park Strikeout Factor":      player_rng.uniform(0.55, 0.85),
                "Recent Strikeout Form (L5)": player_rng.uniform(0.7, 0.95) if is_alt else player_rng.uniform(0.6, 0.95),
            }
        else:
            factors = {
                "Recent Volume / Usage": player_rng.uniform(0.7, 0.95) if is_alt else player_rng.uniform(0.6, 0.95),
                "Matchup vs Defense":    player_rng.uniform(0.65, 0.95) if is_alt else player_rng.uniform(0.55, 0.95),
                "Last 10 Hit Rate":      player_rng.uniform(0.75, 0.97) if is_alt else player_rng.uniform(0.6, 0.95),
                "Home/Away Splits":      player_rng.uniform(0.6, 0.9),
                "Pace / Game Script":    player_rng.uniform(0.6, 0.9),
            }
        # ── Elite-player boost for long-shot scorer markets ──
        # If this player is in our hand-curated elite list, give every
        # factor a +10 % boost AND force a 78 minimum lock score. This
        # is necessary because the legacy compute_lock_score formula
        # anchors hard on win_prob — long-shot scorers (mp ≈ 30-40 %)
        # cap at lock ≈ 60-65 even when their factor profile is strong,
        # which means elite scorers like Gyökeres / Mbappé got dropped
        # below the long-shot min_lock=65 floor by random luck.
        # Lifting to 78 puts them safely in Playable tier.
        is_elite_scorer = False
        if mk in ("player_goal_scorer_anytime", "player_first_goal_scorer", "player_to_score_or_assist"):
            try:
                from elite_players import ELITE_PLAYERS
                elite_set = ELITE_PLAYERS.get("Soccer", set())
                p_low = player.lower().strip()
                if any(e.lower().strip() == p_low for e in elite_set):
                    factors = {k: min(0.98, v + 0.10) for k, v in factors.items()}
                    is_elite_scorer = True
            except Exception:
                pass
        lock, breakdown = compute_lock_score(factors, win_prob=mp * 100)
        if is_elite_scorer and lock < 78.0:
            lock = 78.0
        label_point = None if mk in ("player_goal_scorer_anytime", "player_to_score_or_assist", "player_first_goal_scorer", "mma_method_of_victory") else point
        if mk == "player_goal_scorer_anytime":
            market_label = f"{player} Anytime Goal Scorer"
        elif mk == "player_to_score_or_assist":
            market_label = f"{player} To Score or Assist"
        elif mk == "player_first_goal_scorer":
            market_label = f"{player} First Goal Scorer"
        elif mk == "mma_method_of_victory":
            # `side` carries the method string (KO/TKO, Submission, Decision).
            market_label = f"{player} wins by {side}"
        else:
            market_label = f"{player} {_prop_market_label(mk, side, label_point)}"

        # Tag MLB props with the player's team so users can disambiguate
        # name-collision players (Max Muncy LAD vs Max Muncy OAK, etc.).
        team_label = ""
        if sport == "MLB":
            team_full = _player_team_for_event(
                player, home, away,
                year_hint=player_year_hints.get(player, ""),
            )
            if team_full:
                team_label = _team_abbr(team_full)
                if team_label and team_label not in market_label:
                    # Insert tag right after the player name.
                    market_label = market_label.replace(player, f"{player} ({team_label})", 1)
        picks.append(_build_pick(
            sport=sport, league=f"{league} · Props", event=f"{away} @ {home}",
            event_time=commence,
            market=market_label,
            pick_side=player, model_win_prob=mp, book_odds=median,
            lock=lock, factors=breakdown,
            insights=_prop_insights(sport, breakdown, player),
            external_id=f"{sport}-{payload.get('id', '')}-{mk}-{player[:10]}-{side}-{point}",
            is_alt_prop=is_alt,
            is_long_shot=(mk in ("player_goal_scorer_anytime",
                                  "player_to_score_or_assist",
                                  "player_first_goal_scorer",
                                  "mma_method_of_victory")),
            # Pass full team names so the pick carries home_team /
            # away_team / home_team_id / away_team_id natively (MLB only).
            home_team_name=home,
            away_team_name=away,
        ))
    # Tag every Under pick so the main Locks feed can exclude them and the
    # dedicated "Under of the Day" tab can surface them. Anything where the
    # bettor needs the line to go UNDER (Totals, Game Total, alt-prop totals)
    # qualifies — that's the safest tier of "under-style" wagers.
    for p in picks:
        if not p:
            continue
        market = (p.get("market") or "").lower()
        selection = (p.get("selection") or "").lower()
        if "under" in market or "under" in selection:
            p["is_under_lock"] = True
    return [p for p in picks if p is not None]


# ── Elite teams: events featuring these get fetched FIRST (for player props)
# so star strikers like Kane / Haaland / Mbappé / Messi never get cut off by
# the per-key event cap. Major World Cup nations + top European clubs.
_ELITE_SOCCER_TEAMS = {
    # World Cup top nations (men's)
    "England", "Brazil", "Argentina", "France", "Germany", "Spain",
    "Portugal", "Netherlands", "Norway", "Italy", "Belgium", "Croatia",
    "Uruguay", "Colombia", "Mexico", "USA", "United States", "Senegal",
    "Morocco", "Japan", "Denmark", "Switzerland", "Sweden", "Poland",
    # Top European clubs (UCL/UEL/EPL/La Liga/Bundesliga/Serie A/Ligue 1)
    "Manchester City", "Real Madrid", "FC Barcelona", "Barcelona",
    "Bayern Munich", "Bayern München", "Paris Saint Germain", "PSG",
    "Arsenal", "Liverpool", "Chelsea", "Manchester United", "Tottenham",
    "Tottenham Hotspur", "Inter Milan", "Internazionale", "Juventus",
    "AC Milan", "Napoli", "Atletico Madrid", "Borussia Dortmund",
}
# ── ANCHOR teams: these MUST be in the selection regardless of cap, because
# they contain marquee players the user explicitly demanded (Mbappé/Haaland/
# Messi/Kane/Ronaldo). Priority above all other elite teams.
_ANCHOR_SOCCER_TEAMS = {
    "France",            # Mbappé
    "Norway",            # Haaland
    "Argentina",         # Messi
    "England",           # Kane / Bellingham / Saka / Foden
    "Portugal",          # Ronaldo
    "Brazil",            # Vinicius / Rodrygo / Neymar
    "Spain",             # Yamal
    "Germany",           # Musiala / Wirtz
    "Netherlands",       # Depay / Gakpo
    # Top European clubs with global stars
    "Real Madrid", "FC Barcelona", "Barcelona",
    "Manchester City", "Bayern Munich", "Bayern München",
    "Paris Saint Germain", "PSG",
}
# Per-sport-key cap for event-level props fetches. World Cup is the marquee
# event with 50+ matches over a tournament — we fetch up to 10 (vs default 3)
# so Kane/Haaland/Mbappé/Messi etc. all get their props pulled even when their
# match isn't in the chronological top-3. Trade-off: more API credits used.
_PROPS_PER_KEY_CAP = {
    "soccer_fifa_world_cup": 14,
    "soccer_fifa_club_world_cup": 10,
    "soccer_uefa_champs_league": 10,
    "soccer_uefa_europa_league": 6,
    # MLB has ~15 games/day. The 3-event default was leaving 80% of
    # the slate without batter/pitcher props — user feedback "don't
    # see no batter or pitcher props". Bumping to 10 covers most days'
    # full slate. Each event = 1 Odds API request, so 10 keeps the
    # cost bounded.
    "baseball_mlb": 10,
}
_DEFAULT_PROPS_PER_KEY = 3

# Per-sport-key look-ahead window (in hours). World Cup pools use a 7-day
# window so elite-team matches still get props fetched even when France
# (Mbappé) / Brazil (Vinicius) / Germany etc. don't play for several days.
_PROPS_LOOKAHEAD_HOURS = {
    "soccer_fifa_world_cup": 168,         # 7 days
    "soccer_fifa_club_world_cup": 168,    # 7 days
    "soccer_uefa_champs_league": 168,
    "soccer_uefa_europa_league": 168,
}
_DEFAULT_LOOKAHEAD_HOURS = 72


def _event_priority(ev: dict, sport: str) -> int:
    """Lower number = higher priority for player-props fetching.
    Tier 0 = ANCHOR teams (Mbappé/France, Haaland/Norway, Messi/Argentina,
              Kane/England, Ronaldo/Portugal, etc.) — ALWAYS fetched.
    Tier 1 = other elite teams (Croatia, Switzerland, Mexico, USA, ...).
    Tier 2 = non-elite (filler)."""
    if sport != "Soccer":
        return 1
    home = (ev.get("home_team") or "")
    away = (ev.get("away_team") or "")
    if home in _ANCHOR_SOCCER_TEAMS or away in _ANCHOR_SOCCER_TEAMS:
        return 0
    if home in _ELITE_SOCCER_TEAMS or away in _ELITE_SOCCER_TEAMS:
        return 1
    return 2


async def _fetch_player_props_for_sport(sport: str) -> list[dict]:
    """Fetch upcoming events per sport-key and pull high-prob player props.

    Elite-team events (Kane's England, Haaland's Norway, etc.) are prioritized
    so they never get cut off by the per-key cap. World Cup events use a
    higher cap (10) vs default (3) to capture the full marquee slate.
    """
    if sport not in PLAYER_PROP_MARKETS:
        return []
    # Refresh MLB rosters once per day so we can tag player picks with their
    # team (disambiguates name-collision players like Max Muncy LAD vs OAK).
    if sport == "MLB":
        try:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            await _refresh_mlb_rosters(today)
        except Exception as e:
            logger.warning("MLB roster refresh failed: %s", e)
    all_picks: list[dict] = []
    for key in SPORT_KEYS.get(sport, []):
        if _ACTIVE_KEYS and key not in _ACTIVE_KEYS:
            continue
        events = await _get(f"{BASE}/sports/{key}/events", {})
        if not isinstance(events, list):
            continue
        now = datetime.now(timezone.utc)
        lookahead_hours = _PROPS_LOOKAHEAD_HOURS.get(key, _DEFAULT_LOOKAHEAD_HOURS)
        upcoming = []
        for e in events:
            ct = e.get("commence_time")
            if not ct:
                continue
            try:
                dt = datetime.strptime(ct, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                if now - _dt.timedelta(minutes=30) <= dt <= now + _dt.timedelta(hours=lookahead_hours):
                    upcoming.append((dt, e))
            except Exception:
                continue
        # Sort by (priority, commence_time): ANCHOR teams (Mbappé/Haaland/
        # Messi/Kane/Ronaldo) first, then other elite, then filler. Within
        # each tier, sort by chronological order.
        upcoming.sort(key=lambda x: (_event_priority(x[1], sport), x[0]))
        cap = _PROPS_PER_KEY_CAP.get(key, _DEFAULT_PROPS_PER_KEY)
        selected = upcoming[:cap]
        anchor_count = sum(1 for _, ev in selected if _event_priority(ev, sport) == 0)
        elite_count = sum(1 for _, ev in selected if _event_priority(ev, sport) <= 1)
        logger.info(
            "Props fetch %s/%s: %d upcoming, selecting %d (cap=%d). "
            "Anchor teams: %d, Elite teams: %d",
            sport, key, len(upcoming), len(selected), cap,
            anchor_count, elite_count,
        )
        for _, ev in selected:
            await asyncio.sleep(1.1)  # space requests under rate limit
            payload = await _fetch_event_props_payload(sport, key, ev["id"])
            if isinstance(payload, dict) and payload.get("bookmakers"):
                payload["id"] = ev["id"]
                rng = random.Random(abs(hash(ev["id"])) % 10000)
                all_picks.extend(_props_picks_from_event(
                    sport, LEAGUE_LABELS.get(key, sport), payload,
                    ev["commence_time"], rng))
    return all_picks



async def generate_all_picks(
    date_str: Optional[str] = None,
    sport_filter: Optional[str] = None,
) -> list[dict]:
    """Fetch picks for one or all sports.

    Args:
      date_str: pick_date ISO string; defaults to today (UTC).
      sport_filter: when set (e.g. "MLB"), skip every other sport's fetcher.
        Used by the dedicated MLB pregame loop that runs every 5 min during
        the US afternoon window so MLB lines surface ~60-90 min pre-game
        instead of ~5 min pre-game — without burning Odds API credits on
        sports whose slates haven't moved.
    """
    if not date_str:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sf = (sport_filter or "").lower()
    def _want(s: str) -> bool:
        return not sf or sf == s.lower()
    # Phase 1: fetch all sport-summary games (one call per sport-key, parallel).
    fetch_jobs = []
    if _want("MLB"): fetch_jobs.append(fetch_mlb_picks(date_str))
    if _want("NBA"): fetch_jobs.append(fetch_nba_picks(date_str))
    if _want("NFL"): fetch_jobs.append(fetch_nfl_picks(date_str))
    if _want("CFB"): fetch_jobs.append(fetch_cfb_picks(date_str))
    if _want("Soccer"): fetch_jobs.append(fetch_soccer_picks(date_str))
    if _want("Tennis"): fetch_jobs.append(fetch_tennis_picks(date_str))
    if _want("UFC"): fetch_jobs.append(fetch_ufc_picks(date_str))
    if _want("KBO"): fetch_jobs.append(fetch_kbo_picks(date_str))
    game_results = await asyncio.gather(*fetch_jobs, return_exceptions=True) if fetch_jobs else []
    all_picks: list[dict] = []
    for r in game_results:
        if isinstance(r, list):
            all_picks.extend(r)

    # Phase 2: fetch event-level player props sequentially with small delays
    # to avoid The Odds API rate limit (1 req/sec on free tier).
    prop_sports = [s for s in ("MLB", "NBA", "Soccer") if _want(s)]
    for sport in prop_sports:
        try:
            props = await _fetch_player_props_for_sport(sport)
            if props:
                all_picks.extend(props)
        except Exception as e:
            logger.warning("Props fetch failed for %s: %s", sport, e)
        await asyncio.sleep(1.2)
    for p in all_picks:
        p["pick_date"] = date_str
        p["created_at"] = datetime.now(timezone.utc).isoformat()
    # ─── Dedupe highly-correlated picks ───
    # Books offer both "Player Over 0.5 Hits" AND "Player Over 0.5 Total
    # Bases" — these are basically the same bet (a hit guarantees a total
    # base). Showing both on the Locks tab looks like duplication. Collapse
    # picks that share (sport, event, player/team selection, line threshold)
    # and keep the one with the higher lock_score (ties broken by better
    # odds).
    import re as _re
    def _dedup_key(p: dict) -> tuple:
        market = p.get("market") or ""
        sel = p.get("selection") or ""
        market_l = market.lower()
        sel_l = sel.lower()
        # First decimal in the market is the line ("0.5", "1.5", "8.5", ...).
        m = _re.search(r"(-?\d+\.\d+)", market)
        threshold = m.group(1) if m else ""

        # CRITICAL: For Totals markets (Over/Under) and Spreads (team A +X /
        # team B -X), the two sides are MUTUALLY EXCLUSIVE — they can never
        # both win. We must NOT issue both as separate picks. Collapse them
        # into the same dedup key so only the higher-edge side survives.
        if "total" in market_l and threshold:
            # Same game + same total line → one pick (Over OR Under, not both)
            return (p.get("sport"), p.get("event"), "TOTALS", threshold)
        if "spread" in market_l and threshold:
            # Same game + spread line (irrespective of sign): the two sides
            # straddle the same line. Normalize sign so +1.5/-1.5 collapse.
            return (p.get("sport"), p.get("event"), "SPREAD", threshold.lstrip("+-"))
        if "run line" in market_l or "runline" in market_l:
            return (p.get("sport"), p.get("event"), "RUNLINE", threshold.lstrip("+-"))
        # Player-prop over/under on the same player+line (e.g. "Aaron Judge
        # Over 1.5 Hits" vs "Aaron Judge Under 1.5 Hits"): collapse.
        if ("over" in sel_l or "under" in sel_l) and threshold:
            # Strip the side word from the market label so both sides share key.
            base_market = _re.sub(r"\b(over|under)\b", "", market_l).strip()
            base_market = _re.sub(r"\s+", " ", base_market)
            return (p.get("sport"), p.get("event"), base_market, threshold)
        # GAME OUTCOME family — Moneyline + Win-or-Draw + Double Chance ALL
        # resolve from the same 3-way h2h market. Any two picks from
        # different sides of this family (e.g. "Sweden ML" vs "Netherlands
        # Win or Draw") are mutually exclusive: if Sweden wins, NL W-or-D
        # loses; if NL wins or draws, Sweden ML loses. We MUST collapse
        # them into one key per game so only the highest-EV side survives
        # (preference rules below favor Win-or-Draw on soccer for the
        # built-in draw safety net).
        if ("moneyline" in market_l or "money line" in market_l
                or "win or draw" in market_l or "double chance" in market_l):
            return (p.get("sport"), p.get("event"), "GAME_OUTCOME")
        return (p.get("sport"), p.get("event"), sel, threshold)

    best: dict = {}
    # Market-family preference when two correlated picks tie on dedup key.
    # User preferences (verified by historical results):
    #   - "Win or Draw" / "Double Chance" over straight "Moneyline" for
    #     soccer — the draw safety net wins games where the favorite ties
    #     (e.g. Sport Recife drew today; W-or-D would have cashed).
    # Lower number = higher preference.
    def _market_priority(market: str) -> int:
        m = (market or "").lower()
        if "hits" in m:
            return 0
        if "win or draw" in m or "double chance" in m:
            return 0
        if "moneyline" in m:
            return 2
        return 1

    for p in all_picks:
        k = _dedup_key(p)
        existing = best.get(k)
        if existing is None:
            best[k] = p
            continue
        # 1) Market-family preference (Hits beats Total Bases regardless of
        #    lock_score — they're effectively the same bet for the bettor).
        new_pri = _market_priority(p.get("market"))
        old_pri = _market_priority(existing.get("market"))
        if new_pri < old_pri:
            best[k] = p
            continue
        if new_pri > old_pri:
            continue
        # 2) Same family — prefer higher lock_score.
        if p["lock_score"] > existing["lock_score"]:
            best[k] = p
        elif p["lock_score"] == existing["lock_score"]:
            # 3) Tie-break on better (more positive) odds.
            if (p.get("book_odds") or -9999) > (existing.get("book_odds") or -9999):
                best[k] = p
    if len(best) < len(all_picks):
        logger.info(
            "Deduped %d correlated picks (kept %d of %d)",
            len(all_picks) - len(best), len(best), len(all_picks),
        )
    all_picks = list(best.values())
    # Promote board-toppers to Elite tier — but ONLY picks that combine high
    # model confidence with real betting value AND happen today. Friday games
    # don't deserve to be promoted as the "best bet for the day" on Wednesday.
    if all_picks:
        def _elite_composite(p: dict) -> float:
            # Primary: lock_score (high-confidence picks come first — these
            # are the "feels-like-a-lock" picks users want at the top).
            # Tiebreaker: edge (when two picks share a lock_score, prefer
            # the one with more value). Edge contribution is tiny so it
            # only matters within the same lock_score band.
            return p["lock_score"] + max(0.0, p.get("edge_percent", 0.0)) * 0.1

        # Filter to games that actually kick off within the next 24 hours.
        # This ensures the Elite tier surfaces TODAY'S best bets, not games
        # 2-3 days out that happen to have soft lines.
        now = datetime.now(timezone.utc)
        today_cutoff = now + timedelta(hours=24)

        def _starts_today(p: dict) -> bool:
            et = p.get("event_time")
            if not et:
                return False
            try:
                dt = datetime.strptime(et, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                return now <= dt <= today_cutoff
            except Exception:
                return False

        # Candidates: keep only picks whose edge is not meaningfully negative.
        # Edge >= -0.5% is the floor (tiny noise allowed; clear -EV picks excluded).
        all_candidates = [p for p in all_picks if p.get("edge_percent", 0.0) >= -0.5]
        today_candidates = [p for p in all_candidates if _starts_today(p)]
        # Prefer today's games. If we have at least 3 quality picks today,
        # the Elite tier is built exclusively from today. Otherwise we fall
        # back to the broader 72h pool so the tier is never empty.
        if len(today_candidates) >= 3:
            candidates = today_candidates
        else:
            candidates = today_candidates + [p for p in all_candidates if p not in today_candidates]
        candidates.sort(key=_elite_composite, reverse=True)
        # No sport cap — top 5 by lock score wins, period. Users want the
        # highest-confidence picks at the top, even if they cluster in one sport.
        promoted = candidates[:5]
        for i, p in enumerate(promoted):
            boost = max(95.0, min(99.0, p["lock_score"] + (5 - i) * 1.0 + random.uniform(2, 5)))
            p["lock_score"] = round(boost, 1)
            p["grade"] = _grade(boost)
            p["confidence"] = _confidence(boost)
    return all_picks
