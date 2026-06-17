"""LockScore AI — Sports Betting Intelligence backend."""
import os
import logging
import uuid
import asyncio
from datetime import datetime, timezone, timedelta, time as dtime
from pathlib import Path
from typing import Annotated, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, Field
from starlette.middleware.cors import CORSMiddleware

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

from auth import (  # noqa: E402
    UserCreate, UserLogin, UserPublic, Token,
    hash_password, verify_password, create_access_token,
    get_current_user_from_db, oauth2_scheme,
)
from sports_engine import generate_all_picks  # noqa: E402
from ai_engine import explain_pick, bet_killer_warning, analyze_loss  # noqa: E402
from settlement_engine import settle_due_picks  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("lockscore")

# Production-safe env loading with sane fallbacks so deployment doesn't crash
# if env vars aren't set on the production environment.
mongo_url = os.environ.get("MONGO_URL") or "mongodb://localhost:27017"
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ.get("DB_NAME") or "perkslocks_production"]

app = FastAPI(title="PerksLocks AI")
api = APIRouter(prefix="/api")


# ────────────────────── Auth ──────────────────────

async def current_user(
    token: Annotated[Optional[str], Depends(oauth2_scheme)],
) -> UserPublic:
    return await get_current_user_from_db(db, token)


@api.post("/auth/register", response_model=Token, status_code=201)
async def register(payload: UserCreate):
    existing = await db.users.find_one({"email": payload.email.lower()})
    if existing:
        raise HTTPException(status_code=409, detail="Email already registered")
    user_id = str(uuid.uuid4())
    doc = {
        "id": user_id,
        "email": payload.email.lower(),
        "name": payload.name or payload.email.split("@")[0],
        "hashed_password": hash_password(payload.password),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.users.insert_one(doc)
    public = UserPublic(id=user_id, email=doc["email"], name=doc["name"],
                        created_at=doc["created_at"])
    return Token(access_token=create_access_token(user_id), user=public)


@api.post("/auth/login", response_model=Token)
async def login(payload: UserLogin):
    doc = await db.users.find_one({"email": payload.email.lower()})
    if not doc or not verify_password(payload.password, doc["hashed_password"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    public = UserPublic(id=doc["id"], email=doc["email"], name=doc.get("name"),
                        created_at=doc.get("created_at"))
    return Token(access_token=create_access_token(doc["id"]), user=public)


@api.get("/auth/me", response_model=UserPublic)
async def me(user: Annotated[UserPublic, Depends(current_user)]):
    return user


# ────────────────────── Picks ──────────────────────


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def _picks_for_date(date_str: str) -> list[dict]:
    cursor = db.picks.find({"pick_date": date_str}, {"_id": 0}).sort("lock_score", -1)
    return await cursor.to_list(length=500)


_WINRATE_CACHE: dict = {"data": {}, "expires_at": 0.0}
_WINRATE_TTL = 1800  # 30 min refresh window for the learning loop


async def _historical_winrates() -> dict:
    """Aggregate settled picks into (sport, market_family) win-rate buckets.

    Used by the parlay builder as the "study from past picks" learning loop —
    buckets that historically over-perform get a leg-selection boost, ones
    that under-perform get penalized. Cached for 30 min to avoid hammering
    Mongo on every parlay request. Key '__global__' carries the overall
    settled win-rate for normalization.
    """
    import time as _t
    now = _t.time()
    if _WINRATE_CACHE["data"] and _WINRATE_CACHE["expires_at"] > now:
        return _WINRATE_CACHE["data"]
    cursor = db.picks.find(
        {"status": {"$in": ["won", "lost"]}},
        {"_id": 0, "sport": 1, "market": 1, "status": 1},
    )
    docs = await cursor.to_list(length=5000)
    if not docs:
        out = {"__global__": 0.55}
        _WINRATE_CACHE["data"] = out
        _WINRATE_CACHE["expires_at"] = now + _WINRATE_TTL
        return out
    def _family(market: str) -> str:
        m = (market or "").lower()
        if "anytime goal scorer" in m: return "goal_scorer"
        if "win or draw" in m or "double chance" in m: return "win_or_draw"
        if "moneyline" in m: return "moneyline"
        if "spread" in m: return "spread"
        if "over" in m and ("hits" in m or "total bases" in m): return "batter_over"
        if "over" in m or "under" in m: return "total_over_under"
        if "wins by" in m: return "mma_method"
        return "other"
    buckets: dict = {}
    g_won = g_total = 0
    for d in docs:
        won = d["status"] == "won"
        key = ((d.get("sport") or "").lower(), _family(d.get("market") or ""))
        b = buckets.setdefault(key, {"n": 0, "w": 0})
        b["n"] += 1
        if won: b["w"] += 1
        g_total += 1
        if won: g_won += 1
    result: dict = {
        k: {"n": v["n"], "winrate": v["w"] / max(v["n"], 1)}
        for k, v in buckets.items()
    }
    result["__global__"] = g_won / max(g_total, 1)
    _WINRATE_CACHE["data"] = result
    _WINRATE_CACHE["expires_at"] = now + _WINRATE_TTL
    logger.info("Win-rate buckets refreshed: %d buckets, global=%.3f", len(buckets), result["__global__"])
    return result



async def _refresh_picks(date_str: str) -> int:
    """Generate today's picks, replace any existing rows for that date.

    Critical: only delete existing picks AFTER we've successfully generated
    new ones. Otherwise, if the upstream API is down/rate-limited, we'd
    end up with an empty board instead of last-known-good picks.

    Pick IDs are deterministic (UUID5 derived from external_id) so cached
    references in user slips and the frontend remain valid across refreshes
    instead of pointing to a brand-new UUID that 404s.
    """
    logger.info("Refreshing picks for %s", date_str)
    picks = await generate_all_picks(date_str)
    if not picks:
        logger.warning(
            "Refresh produced 0 picks for %s — keeping existing rows intact "
            "instead of wiping the board.", date_str,
        )
        return 0
    namespace = uuid.UUID("00000000-0000-0000-0000-000000000001")
    for p in picks:
        ext = str(p.get("external_id") or "")
        p["id"] = str(uuid.uuid5(namespace, ext)) if ext else str(uuid.uuid4())

    # ── SportDB enrichment: pull live team-form into Soccer picks. Cached
    # league standings (24h TTL) keep the daily request count to ~10. The
    # enrichment is best-effort — if SportDB is down or budget hit we still
    # save the un-enriched pick and move on.
    try:
        from sportdb_client import refresh_top_leagues, enrich_pick
        await refresh_top_leagues(db)
        enriched = 0
        for p in picks:
            if p.get("sport") == "Soccer":
                before = p.get("win_probability")
                await enrich_pick(db, p)
                if p.get("enriched_by") == "sportdb" and p.get("win_probability") != before:
                    enriched += 1
        if enriched:
            logger.info("SportDB enriched %d Soccer picks", enriched)
    except Exception as e:
        logger.warning("SportDB enrichment skipped: %s", e)

    # ── Self-tuning learning layer: bias predictions based on historical
    # ROI / hit-rate vs expected. Applied AFTER all other enrichment so it
    # sits on top of model + SportDB + Odds-API edge.
    try:
        from learning_engine import apply_learning
        adjusted = 0
        for p in picks:
            before = p.get("win_probability")
            await apply_learning(db, p)
            if p.get("learning") and p.get("win_probability") != before:
                adjusted += 1
        if adjusted:
            logger.info("Learning engine adjusted %d picks", adjusted)
    except Exception as e:
        logger.warning("Learning engine skipped: %s", e)

    # ── Elite Player Boost: world-class players (Mbappé, Haaland, Messi,
    # Kane, Judge, Sinner, Jokic, Wilson, etc.) get a +10 lock_score bump
    # so they auto-qualify for Lock tier — books price them tightly but
    # they're still the safest hit candidates by reputation.
    try:
        from elite_players import apply_elite_boost
        before_elite = sum(1 for p in picks if p.get("elite_player"))
        picks = apply_elite_boost(picks)
        after_elite = sum(1 for p in picks if p.get("elite_player"))
        logger.info("Elite Player Boost applied: %d picks tagged (was %d)",
                    after_elite, before_elite)
    except Exception as e:
        logger.warning("Elite Player Boost skipped: %s", e)

    # ── Sportsbook deep-link enrichment: attach home_team / away_team / pick
    # / fanduel_event_id / draftkings_event_id / etc. to every pick. These
    # power the "Add to Bet Slip" deep links from the parlay & detail screens
    # so users land on the correct game page in FanDuel / DraftKings instead
    # of the sportsbook homepage.
    try:
        from event_matcher import enrich_picks_with_event_ids
        enrich_picks_with_event_ids(picks)
        sample = next((p for p in picks if p.get("fanduel_event_id")), None)
        logger.info(
            "Event-ID enrichment applied to %d picks (sample: %s)",
            len(picks),
            sample.get("fanduel_event_id") if sample else "NONE",
        )
    except Exception as e:
        logger.warning("Event-ID enrichment skipped: %s", e)

    # ── Sportsbook Mapping Engine: build a sportsbook-INDEPENDENT
    # ``selection_v2`` per pick + per-book deep-link bundles (best_link /
    # best_depth). The frontend consumes ``sportsbook_mapping[<Book>].best_link``
    # so users land as close to the actual bet as we can manage without a
    # partner API key. UI is unchanged — same buttons, deeper destinations.
    try:
        from sportsbook_mapper import enrich_picks_with_mapping, SUPPORTED_BOOKS
        enrich_picks_with_mapping(picks)
        depth_counts: dict[str, int] = {}
        for p in picks:
            depths = {b: ((p.get("sportsbook_mapping") or {}).get(b) or {}).get("best_depth")
                      for b in SUPPORTED_BOOKS}
            for d in depths.values():
                depth_counts[d or "none"] = depth_counts.get(d or "none", 0) + 1
        logger.info("Sportsbook Mapping: %d picks enriched across %d books · depth=%s",
                    len(picks), len(SUPPORTED_BOOKS), depth_counts)
    except Exception as e:
        logger.warning("Sportsbook mapping enrichment skipped: %s", e)

    # ── Tennis Edge Engine v2: per-pick component scoring + NO_BET filter,
    # 99-LOCK gating, and max-3-per-day cap. Pure post-processing; no extra
    # API calls. Non-tennis picks pass through unchanged.
    try:
        from tennis_engine import apply_tennis_engine, build_tennis_insights
        before_tennis = sum(1 for p in picks if (p.get("sport") or "").lower() == "tennis")
        picks = apply_tennis_engine(picks)
        after_tennis = sum(1 for p in picks if (p.get("sport") or "").lower() == "tennis")
        # Attach tennis-specific insights to surviving tennis picks so the
        # Deep Dive UI gets the surface/serve/matchup bullets.
        for p in picks:
            if (p.get("sport") or "").lower() == "tennis":
                tennis_insights = build_tennis_insights(p)
                if tennis_insights:
                    existing = p.get("key_insights") or []
                    p["key_insights"] = tennis_insights + existing
        logger.info("Tennis Edge v2: tennis picks %d → %d (filtered + capped)",
                    before_tennis, after_tennis)
    except Exception as e:
        logger.warning("Tennis Edge v2 skipped: %s", e)

    # ── Bet-Type Classification & Weighted Unit Tagging
    # Per spec: odds ≥ -300 → STRAIGHT (1.0u), ≥ -500 → REDUCED (0.5u),
    # < -500 → PARLAY (0.25u). Real betting behavior — heavy chalk gets
    # smaller stake so ROI math isn't distorted by -500+ lines.
    try:
        from bet_type import classify_bet_type, unit_weight
        for p in picks:
            odds = p.get("book_odds")
            p["bet_type"] = classify_bet_type(odds)
            p["unit_weight"] = unit_weight(odds)
    except Exception as e:
        logger.warning("Bet-type tagging skipped: %s", e)

    # ── Learning System v2: apply ROI/CLV/Calibration/Volume weights +
    # 99-Lock gates + calibration band raises to the freshly-built slate.
    try:
        from learning_system_v2 import apply_v2_to_picks
        picks = await apply_v2_to_picks(picks, db)
    except Exception as e:
        logger.warning("Learning v2 apply skipped: %s", e)

    # ── Deep Dive Mode: attach edge/confidence/risk scores, top-3 reasons,
    # and NO-BET flag for low-confidence picks. Internal only; UI unchanged.
    try:
        from deep_dive import deep_dive, NO_BET_THRESHOLD
        no_bet_count = 0
        for p in picks:
            # Tag every fresh pick with the current formula version so the
            # learning engine can isolate clean calibration samples from
            # legacy data.
            p["formula_v"] = 2
            await deep_dive(db, p)
            if p.get("no_bet"):
                no_bet_count += 1
        logger.info("Deep Dive: %d picks analysed, %d flagged NO-BET (conf < %d)",
                    len(picks), no_bet_count, NO_BET_THRESHOLD)
    except Exception as e:
        logger.warning("Deep Dive skipped: %s", e)

    # ── Brain Pipeline v1 — Prediction Memory + Candidate Ranker + hidden
    # Monte Carlo simulator + Decision Filter (PASS verdict) + Confidence
    # Calibration. All seven layers run ON TOP of existing scoring; PASS
    # picks set the existing `no_bet=True` flag so feed endpoints silently
    # drop them with zero UI change. See /app/backend/brain/ for the
    # individual modules.
    try:
        from brain import process_brain
        brain_summary = await process_brain(picks, db)
        logger.info("Brain v%s done in %sms: %s",
                    brain_summary.get("version"),
                    brain_summary.get("elapsed_ms"),
                    brain_summary.get("steps", {}).get("filter"))
    except Exception as e:
        logger.warning("Brain pipeline skipped: %s", e)

    # Deduplicate picks within this batch by `id` — UUID5 hashes can collide
    # if two markets produce identical external_ids (saw this with Anytime
    # Goal Scorer picks generated twice in the same refresh). Keep the first.
    seen_ids: set = set()
    dedup_picks = []
    for p in picks:
        pid = p.get("id")
        if pid in seen_ids:
            continue
        seen_ids.add(pid)
        dedup_picks.append(p)
    picks = dedup_picks

    # Preserve original `odds_at_pick` and `units_risked` across refreshes so
    # CLV can be measured later (closing_odds is updated by settle). The
    # latest book_odds becomes the running "closing line" snapshot.
    if seen_ids:
        existing = db.picks.find(
            {"id": {"$in": list(seen_ids)}},
            {"_id": 0, "id": 1, "odds_at_pick": 1, "units_risked": 1, "first_seen_at": 1},
        )
        prior: dict[str, dict] = {}
        async for doc in existing:
            prior[doc["id"]] = doc
        from datetime import datetime as _dt, timezone as _tz
        now_iso = _dt.now(_tz.utc).isoformat()
        for p in picks:
            pid = p.get("id")
            book = p.get("book_odds")
            prev = prior.get(pid)
            if prev and prev.get("odds_at_pick"):
                p["odds_at_pick"] = prev["odds_at_pick"]
                p["first_seen_at"] = prev.get("first_seen_at", now_iso)
            else:
                p["odds_at_pick"] = book
                p["first_seen_at"] = now_iso
            # closing_odds will be the latest book_odds we saw at refresh time
            # — re-snapshotted on settle below.
            p["closing_odds"] = book
            p["units_risked"] = (prev.get("units_risked") if prev else None) or 1.0

    # Delete previous entries for this date AND any leftover picks with the
    # same UUID5 from a prior day, then insert fresh.
    await db.picks.delete_many({"pick_date": date_str})
    await db.picks.delete_many({"id": {"$in": list(seen_ids)}})
    # Defensive write: drop malformed pick docs (missing required fields)
    # so a single broken doc never aborts the entire batch insert. Required
    # fields: id, sport, event_time, market, book_odds.
    REQUIRED = ("id", "sport", "event_time", "market", "book_odds")
    safe_picks = []
    dropped = 0
    for p in picks:
        if not isinstance(p, dict):
            dropped += 1
            continue
        missing = [k for k in REQUIRED if not p.get(k)]
        if missing:
            logger.warning("Dropping malformed pick (missing %s): event=%s market=%s",
                         missing, p.get("event"), p.get("market"))
            dropped += 1
            continue
        safe_picks.append(p)
    if dropped:
        logger.warning("Skipped %d malformed picks before insert", dropped)
    if safe_picks:
        await db.picks.insert_many(safe_picks, ordered=False)
    logger.info("Stored %d picks for %s", len(safe_picks), date_str)
    return len(safe_picks)


async def _ensure_today_picks() -> None:
    today = _today_str()
    count = await db.picks.count_documents({"pick_date": today})
    if count == 0:
        await _refresh_picks(today)


# ── Market filter taxonomy ────────────────────────────────────────────────
# Tokens are the SAME across sports where it makes sense (moneyline, totals,
# spread). Sport-specific tokens (btts, goalscorer, player_points, etc.) only
# match their own sport. The regex is matched (case-insensitive) against the
# pick's stored `market` string.
_MARKET_REGEX = {
    # ── Soccer-specific families ──────────────────────────────────────────
    "1x2":           r"\bmoneyline\b|\bwin or draw\b",
    "btts":          r"both teams to score|\bbtts\b",
    "goalscorer":    r"anytime goal scorer|first goal scorer|last goal scorer|to score or assist",

    # ── Generic team markets ──────────────────────────────────────────────
    "moneyline":     r"\bmoneyline\b",
    "double_chance": r"\bwin or draw\b|double chance",
    "spread":        r"[+\-]\d+(\.\d+)?\s+spread\b|\bspread\b",
    "run_line":      r"\brun line\b|[+\-]\d+(\.\d+)?\s+spread\b",  # MLB run line ≡ spread

    # ── Game totals ONLY (must START with "Total <stat>") ─────────────────
    # This intentionally does NOT match "Total Bases", "Player … Total …",
    # or alt-prop Over/Under player markets. Game totals only.
    "totals":        r"^total (goals|points|runs|sets|games|corners)\b|\bgame total\b",

    # ── Player props (mutually exclusive, anchored) ───────────────────────
    # Each prop is keyed off the STAT NAME and excludes neighbouring stats.
    # MongoDB supports PCRE lookaheads so we use them to disambiguate.
    "batter_hits":          r"\bhits\b(?!\s*allowed)",
    "batter_total_bases":   r"\btotal bases\b",
    "player_points":        r"\bpoints\b(?!\s*total)",
    "player_rebounds":      r"\brebounds\b",
    "player_assists":       r"\bassists\b",

    # ── NFL props ─────────────────────────────────────────────────────────
    "passing_yards":   r"passing yards",
    "rushing_yards":   r"rushing yards",
    "receiving_yards": r"receiving yards",

    # ── Tennis ────────────────────────────────────────────────────────────
    "match_winner":  r"\bmoneyline\b|match winner|to win match",
    "sets":          r"\btotal sets\b|\bset winner\b|\bset score\b",
    "games_total":   r"\bgames over\b|\bgames under\b|\btotal games\b",

    # ── Broad catch-all (still used by analytics market-label grouping) ──
    "player_props":  r"hits|total bases|points|rebounds|assists|passing yards|rushing yards|receiving yards|touchdowns|goal scorer",
}


def _market_regex(token: str) -> str | None:
    return _MARKET_REGEX.get(token.lower().strip())


# Sport → available market filter tokens. Drives the UI MarketSelector pills.
SPORT_MARKETS = {
    "Soccer": [
        {"token": "1x2",         "label": "1X2"},
        {"token": "totals",      "label": "Over/Under"},
        {"token": "btts",        "label": "BTTS"},
        {"token": "goalscorer",  "label": "Goalscorer"},
    ],
    "NBA": [
        {"token": "moneyline",   "label": "Moneyline"},
        {"token": "spread",      "label": "Spread"},
        {"token": "totals",      "label": "Totals"},
        {"token": "player_points",   "label": "Points"},
        {"token": "player_rebounds", "label": "Rebounds"},
        {"token": "player_assists",  "label": "Assists"},
    ],
    "NFL": [
        {"token": "moneyline",   "label": "Moneyline"},
        {"token": "spread",      "label": "Spread"},
        {"token": "totals",      "label": "Totals"},
        {"token": "passing_yards",   "label": "Passing Yds"},
        {"token": "rushing_yards",   "label": "Rushing Yds"},
        {"token": "receiving_yards", "label": "Receiving Yds"},
    ],
    "MLB": [
        {"token": "moneyline",   "label": "Moneyline"},
        {"token": "run_line",    "label": "Run Line"},
        {"token": "totals",      "label": "Totals"},
        {"token": "batter_hits",        "label": "Hits"},
        {"token": "batter_total_bases", "label": "Total Bases"},
    ],
    "Tennis": [
        {"token": "match_winner", "label": "Match Winner"},
        {"token": "sets",         "label": "Sets"},
        {"token": "games_total",  "label": "Games O/U"},
    ],
}


@api.get("/picks/markets/{sport}")
async def markets_for_sport(
    user: Annotated[UserPublic, Depends(current_user)],
    sport: str,
):
    """Return the dynamic market list + active leagues for a given sport.
    Used by the Locks tab to populate the MarketSelector + League pills."""
    markets = SPORT_MARKETS.get(sport, [])
    # Active leagues: distinct league names from today's picks for this sport.
    leagues_cursor = db.picks.aggregate([
        {"$match": {"sport": sport, "pick_date": _today_str()}},
        {"$group": {"_id": "$league", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
    ])
    leagues = []
    async for row in leagues_cursor:
        if row.get("_id"):
            leagues.append({"name": row["_id"], "count": row["count"]})
    return {"sport": sport, "markets": markets, "leagues": leagues}


@api.get("/picks/today")
async def picks_today(user: Annotated[UserPublic, Depends(current_user)],
                      sport: Optional[str] = None,
                      grade: Optional[str] = None,
                      day_offset: Optional[int] = None,
                      line_type: Optional[str] = None,
                      sort: Optional[str] = "time",
                      min_lock: Optional[float] = None,
                      min_implied: Optional[float] = None,
                      max_implied: Optional[float] = None,
                      market: Optional[str] = None,
                      league: Optional[str] = None):
    """Top picks from today's 72-hour window (lock score >= 85).
    Filters:
      - `min_lock`: only show picks with lock_score >= this value.
      - `min_implied` / `max_implied`: only show picks whose implied
        probability falls in [min, max] (units: %, e.g. 60 → 90).
      - `market`: filter by market family token (see /picks/markets/{sport}).
      - `league`: substring-match against pick.league.
    Sort options:
      - "lock" / None (default): highest lock_score first
      - "time": soonest kickoff first
      - "edge": biggest model edge first
      - "implied": highest implied probability first (safest first)
    """
    await _ensure_today_picks()
    # When the user explicitly filters by a single market, relax the default
    # 85+ lock floor — they're narrowing the pool themselves and want to see
    # everything that matches their selection.
    default_floor = 75.0 if market else 85.0
    floor = max(default_floor, float(min_lock)) if min_lock is not None else default_floor
    # Two-bucket query:
    #  • Standard picks: must pass lock floor + edge >= 0 + not no_bet + not under_lock
    #  • Elite-player anchors (Mbappé, Haaland, Messi, Kane, Ronaldo synth FGS
    #    etc.): bypass lock floor + edge filter — they're reputation-locked
    #    Elite tier even when raw math is borderline. Still must not be NO-BET
    #    or under-lock.
    standard_q = {
        "lock_score": {"$gte": floor},
        "is_under_lock": {"$ne": True},
        "no_bet": {"$ne": True},
        # Hide negative-edge picks from the main feed entirely.
        # Picks where model_WP < book_implied are by definition bad
        # bets (book is sharper than us). The Locks tab is for
        # actionable +EV picks only.
        "edge_percent": {"$gte": 0},
    }
    elite_q = {
        "elite_player": True,
        "is_under_lock": {"$ne": True},
        "no_bet": {"$ne": True},
    }
    q: dict = {
        "pick_date": _today_str(),
        "$or": [standard_q, elite_q],
    }
    if sport and sport.lower() != "all":
        q["sport"] = sport
    if grade:
        q["grade"] = grade
    lt = (line_type or "").lower()
    if lt == "main":
        q["is_alt"] = {"$ne": True}
    elif lt == "alt":
        q["is_alt"] = True
    if min_implied is not None or max_implied is not None:
        imp_q: dict = {}
        if min_implied is not None:
            imp_q["$gte"] = float(min_implied)
        if max_implied is not None:
            imp_q["$lte"] = float(max_implied)
        q["implied_probability"] = imp_q
    # Market family filter — uses the same labelling we use in analytics so
    # the same token works on every sport (e.g. "moneyline", "spread",
    # "game_total", "btts", "1x2", "goalscorer", "player_points", etc.).
    if market:
        regex = _market_regex(market)
        if regex:
            q["market"] = {"$regex": regex, "$options": "i"}
    if league:
        # League names come from The Odds API e.g. "Premier League", "MLS",
        # "MLB". Loose substring match keeps the UI simple.
        q["league"] = {"$regex": str(league).replace("\\", ""), "$options": "i"}
    cursor = db.picks.find(q, {"_id": 0}).sort("lock_score", -1).limit(200)
    picks = await cursor.to_list(length=200)
    if day_offset is not None:
        target_day = (datetime.now(timezone.utc).date() + timedelta(days=day_offset)).isoformat()
        picks = [p for p in picks if (p.get("event_time") or "").startswith(target_day)]
    else:
        # Default ordering: today's games first (kickoff within 24h), then later
        # games — within each bucket, sorted by lock_score desc. Keeps the
        # "best bet for the day" front-and-center even if a 2-day-out game
        # has a higher base lock_score.
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(hours=24)

        def _bucket(p: dict) -> int:
            et = p.get("event_time") or ""
            try:
                dt = datetime.strptime(et, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                return 0 if now <= dt <= cutoff else 1
            except Exception:
                return 1

        # Apply the user's sort preference. Default "lock": highest lock_score
        # first (today first). "time": soonest kickoff first. "edge": biggest
        # model edge first.
        s = (sort or "lock").lower()
        def _event_dt(p: dict) -> datetime:
            try:
                return datetime.strptime(
                    p.get("event_time") or "", "%Y-%m-%dT%H:%M:%SZ",
                ).replace(tzinfo=timezone.utc)
            except Exception:
                return datetime.max.replace(tzinfo=timezone.utc)
        # Elite-player anchor: always float elite picks to the top within
        # their bucket regardless of sort key. Mbappé/Haaland/Messi/Kane etc.
        # are the headline picks of the slate.
        def _elite_rank(p: dict) -> int:
            return 0 if p.get("elite_player") else 1
        if s == "time":
            # Pure chronological — earliest kickoff first, regardless of
            # elite/lock status. User explicitly wants time order.
            picks.sort(key=lambda p: (_event_dt(p), -p.get("lock_score", 0)))
        elif s == "edge":
            picks.sort(key=lambda p: (_elite_rank(p), _bucket(p), -p.get("edge_percent", 0), -p.get("lock_score", 0)))
        elif s == "implied":
            picks.sort(key=lambda p: (_elite_rank(p), _bucket(p), -p.get("implied_probability", 0), -p.get("lock_score", 0)))
        else:  # "lock" (default)
            picks.sort(key=lambda p: (_elite_rank(p), _bucket(p), -p.get("lock_score", 0)))
    return {"picks": picks}


@api.get("/picks/all")
async def picks_all(user: Annotated[UserPublic, Depends(current_user)],
                    sport: Optional[str] = None):
    await _ensure_today_picks()
    q: dict = {"pick_date": _today_str()}
    if sport and sport.lower() != "all":
        q["sport"] = sport
    cursor = db.picks.find(q, {"_id": 0}).sort("lock_score", -1).limit(200)
    return {"picks": await cursor.to_list(length=200)}


@api.get("/picks/bet-killer")
async def picks_bet_killer(user: Annotated[UserPublic, Depends(current_user)],
                           sport: Optional[str] = None):
    """Legacy bet-killer endpoint (deprecated) — kept for backwards compat."""
    await _ensure_today_picks()
    q: dict = {"pick_date": _today_str(), "lock_score": {"$lt": 85}}
    if sport and sport.lower() != "all":
        q["sport"] = sport
    cursor = db.picks.find(q, {"_id": 0}).sort("lock_score", 1).limit(50)
    return {"picks": await cursor.to_list(length=50)}


@api.get("/picks/under-of-the-day")
async def under_of_the_day(user: Annotated[UserPublic, Depends(current_user)],
                           line_type: Optional[str] = None,
                           sort: Optional[str] = "time",
                           sport: Optional[str] = None,
                           market: Optional[str] = None,
                           league: Optional[str] = None):
    """The single safest Under lock across all sports.

    `line_type`:
      - "main": main-line totals only
      - "alt":  alt-prop Unders only
      - "both" / None: unrestricted (default)
    `sort`: "lock" (default), "time", or "edge"
    `sport` / `market` / `league`: same semantics as /picks/today.
    """
    await _ensure_today_picks()
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=24)
    q: dict = {"pick_date": _today_str(), "is_under_lock": True,
               "no_bet": {"$ne": True}}
    lt = (line_type or "").lower()
    if lt == "main":
        q["is_alt"] = {"$ne": True}
    elif lt == "alt":
        q["is_alt"] = True
    if sport and sport.lower() != "all":
        q["sport"] = sport
    if market:
        regex = _market_regex(market)
        if regex:
            q["market"] = {"$regex": regex, "$options": "i"}
    if league:
        q["league"] = {"$regex": str(league).replace("\\", ""), "$options": "i"}
    s = (sort or "lock").lower()
    if s == "time":
        cursor = db.picks.find(q, {"_id": 0}).sort("event_time", 1).limit(50)
    elif s == "edge":
        cursor = db.picks.find(q, {"_id": 0}).sort("edge_percent", -1).limit(50)
    else:
        cursor = db.picks.find(q, {"_id": 0}).sort("lock_score", -1).limit(50)
    picks = await cursor.to_list(length=50)

    def starts_today(p: dict) -> bool:
        et = p.get("event_time") or ""
        try:
            dt = datetime.strptime(et, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            return now <= dt <= cutoff
        except Exception:
            return False

    today_picks = [p for p in picks if starts_today(p)]
    pool = today_picks if today_picks else picks
    if not pool:
        return {"pick": None, "alternates": [], "total_evaluated": 0}

    # Rank by win probability (the higher, the safer the Under)
    pool.sort(key=lambda p: (p.get("win_probability", 0), p.get("lock_score", 0)), reverse=True)
    return {
        "pick": pool[0],
        "alternates": pool[1:6],  # 5 backup alt-Under locks
        "total_evaluated": len(pool),
        "scoped_to_today": bool(today_picks),
    }


@api.get("/picks/rollover")
async def pick_rollover(user: Annotated[UserPublic, Depends(current_user)],
                        line_type: Optional[str] = None,
                        sport: Optional[str] = None,
                        market: Optional[str] = None,
                        league: Optional[str] = None):
    """Top 3 safest bets of the day — the user picks which one to roll.

    Rules:
      - Today's slate only (kickoff within 24h)
      - Lock score >= 90
      - NO Soccer by default (small leagues, high variance) — but if the user
        explicitly picks `sport=Soccer` we honour their choice.
      - Prefers player props over team moneylines (lower variance)
      - Ranks by win_probability first, then lock_score — "most likely to hit"
      - Diversifies: at most one pick per game so the trio isn't 3 of the same matchup
      - `line_type`: "main" / "alt" / "both" (default) — same semantics as /picks/today.
      - `sport` / `market` / `league`: optional narrowing filters.
    """
    await _ensure_today_picks()
    base_q: dict = {"pick_date": _today_str(), "no_bet": {"$ne": True}}
    lt = (line_type or "").lower()
    if lt == "main":
        base_q["is_alt"] = {"$ne": True}
    elif lt == "alt":
        base_q["is_alt"] = True
    sport_filter_active = bool(sport and sport.lower() != "all")
    if sport_filter_active:
        base_q["sport"] = sport
    if market:
        regex = _market_regex(market)
        if regex:
            base_q["market"] = {"$regex": regex, "$options": "i"}
    if league:
        base_q["league"] = {"$regex": str(league).replace("\\", ""), "$options": "i"}

    # Rollover = "most likely to hit" → require POSITIVE expected value. A
    # negative-edge pick (model WP < book implied) is a bad bet — must never
    # appear here regardless of lock_score.
    base_q["edge_percent"] = {"$gte": 0}

    # Always exclude Soccer goalscorer markets from Rollover — they're
    # high-variance lottery tickets (often 20-35% implied). Other Soccer
    # markets (Moneyline / Win-or-Draw / Totals) are now ALLOWED in rollover.
    existing_market_q = base_q.pop("market", None)
    goalscorer_block = {"market": {"$not": {"$regex": r"goal scorer|to score or assist", "$options": "i"}}}
    if existing_market_q:
        base_q["$and"] = [{"market": existing_market_q}, goalscorer_block]
    else:
        base_q["market"] = goalscorer_block["market"]

    # Progressive widening — Rollover needs at least 3 options. We try each
    # safety floor in order until we have ≥3 distinct candidate games, but we
    # NEVER widen the user-chosen sport/market/league filters.
    floors = [90, 85, 80, 75, 70]
    chalk_cap = -400
    picks: list = []
    floor_used: int = 90
    for f in floors:
        q = {**base_q, "lock_score": {"$gte": f}}
        cursor = db.picks.find(q, {"_id": 0})
        picks = await cursor.to_list(length=500)
        picks = [p for p in picks if (p.get("book_odds") or -9999) >= chalk_cap]
        if len({p.get("event") for p in picks}) >= 3:
            floor_used = f
            break
        floor_used = f
    # Final safety net — if still nothing matched, broaden the chalk cap.
    if len(picks) < 3:
        q = {**base_q, "lock_score": {"$gte": 70}}
        picks = await db.picks.find(q, {"_id": 0}).to_list(length=500)
        picks = [p for p in picks if (p.get("book_odds") or -9999) >= -700]
    # Restrict Rollover to today's games only (start time within next 24h),
    # with graceful fallback to the broader pool if nothing starts today.
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=24)
    def starts_today(p: dict) -> bool:
        et = p.get("event_time") or ""
        try:
            dt = datetime.strptime(et, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            return now <= dt <= cutoff
        except Exception:
            return False
    today_picks = [p for p in picks if starts_today(p)]
    pool = today_picks if today_picks else picks
    if not pool:
        return {"picks": [], "pick": None, "total_evaluated": 0}

    # User explicit ranking: win_probability first (highest chance to hit),
    # lock_score as tie-breaker, edge_percent as third tie-breaker. No
    # composite weighting — the strongest signal of "will it hit" should win.
    ranked = sorted(
        pool,
        key=lambda p: (
            p.get("win_probability", 0) or 0,
            p.get("lock_score", 0) or 0,
            p.get("edge_percent", 0) or 0,
        ),
        reverse=True,
    )
    # Diversify: one pick per game so the user gets 3 distinct options.
    seen_events: set = set()
    top: list = []
    for p in ranked:
        ev = p.get("event")
        if ev in seen_events:
            continue
        seen_events.add(ev)
        top.append({
            **p,
            "composite_rank": round(p.get("win_probability", 0) or 0, 1),
        })
        if len(top) >= 3:
            break
    return {
        "picks": top,
        "pick": top[0] if top else None,  # back-compat for older clients
        "composite_rank": top[0]["composite_rank"] if top else None,
        "total_evaluated": len(pool),
        "scoped_to_today": bool(today_picks),
    }


@api.get("/picks/parlay")
async def pick_parlay(user: Annotated[UserPublic, Depends(current_user)],
                     legs: int = 3,
                     mode: str = "standard",
                     sport: str | None = None,
                     line_type: str | None = None,
                     exclude_sports: str | None = None,
                     include_sports: str | None = None,
                     sport_mode: str = "auto",
                     window_hours: int = 24,
                     market: str | None = None,
                     league: str | None = None,
                     rank: int = 1,
                     locked_ids: str | None = None):
    """Parlay Optimizer V1.1 — highest-probability parlay builder.

    Mode (`mode`):
      - standard: Lock≥88, Edge≥+3%, ROI non-negative. Target 2-5 legs.
      - high_risk: Lock≥75, Edge≥+1%. Target 10-20 legs.

    Sport selection (`sport_mode`):
      - auto: use everything (default).
      - custom: limit pool to `include_sports` (comma-separated list).
      - single: limit pool to one `sport` value AND bypass same-sport
        diversification (so 100% same sport is allowed).

    Time window (`window_hours`): only consider events with commence_time
    inside the next N hours. Defaults to 24h.

    Refresh: pass `rank=2,3,4…` to cycle through next-best candidates.
    Pin legs: pass `locked_ids` (comma-separated pick IDs).
    """
    from parlay_optimizer import (
        build_top_parlays, parlay_to_payload,
    )
    await _ensure_today_picks()
    is_high_risk = (mode or "").lower() == "high_risk"
    mode_lower = (sport_mode or "auto").lower()
    is_single_sport = mode_lower == "single"

    # ─── Sport filter ───
    sport_filter: dict = {}
    sport_q = (sport or "").strip()
    if mode_lower == "single" and sport_q and sport_q.lower() not in ("mix", "all"):
        sport_filter = {"sport": sport_q}
    elif mode_lower == "custom" and include_sports:
        wanted = [s.strip() for s in include_sports.split(",") if s.strip()]
        if wanted:
            sport_filter = {"sport": {"$in": wanted}}
    else:
        # AUTO mode (or fallback): honour legacy exclude_sports if provided.
        if exclude_sports:
            excluded = [s.strip() for s in exclude_sports.split(",") if s.strip()]
            if excluded:
                sport_filter = {"sport": {"$nin": excluded}}

    lt = (line_type or "").lower()
    line_filter: dict = {}
    if lt == "main":
        line_filter = {"is_alt": {"$ne": True}}
    elif lt == "alt":
        line_filter = {"is_alt": True}
    market_filter: dict = {}
    if market:
        regex = _market_regex(market)
        if regex:
            market_filter = {"market": {"$regex": regex, "$options": "i"}}
    league_filter: dict = {}
    if league:
        league_filter = {"league": {"$regex": str(league).replace("\\", ""), "$options": "i"}}

    target_legs = max(10, min(20, max(1, int(legs or 10)))) if is_high_risk else max(2, min(8, max(1, int(legs or 3))))
    rank = max(1, min(20, int(rank or 1)))  # clamp refresh cursor to 1-20

    # ─── Time window filter ───
    # `commence_time` is stored as ISO-8601 string (UTC, e.g.
    # "2026-06-19T19:00:00Z"). Build a window cap and filter the DB query.
    window_hours = max(1, min(720, int(window_hours)))  # 1h .. 30d
    now_utc = datetime.now(timezone.utc)
    window_cap_iso = (now_utc + timedelta(hours=window_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    window_floor_iso = (now_utc - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    time_filter = {"event_time": {"$gte": window_floor_iso, "$lte": window_cap_iso}}

    # ─── Fetch candidate pool ───
    base_q = {
        "pick_date": _today_str(),
        "no_bet": {"$ne": True},
        "is_under_lock": {"$ne": True},
        **sport_filter, **line_filter, **market_filter, **league_filter,
        **time_filter,
    }
    base_q["lock_score"] = {"$gte": 70 if is_high_risk else 85}
    pool = await db.picks.find(base_q, {"_id": 0}).sort("lock_score", -1).limit(400).to_list(length=400)

    # ─── Bucket-map ROI ───
    raw_buckets = await _historical_winrates()
    bucket_map: dict = {}
    for k, v in raw_buckets.items():
        if k == "__global__":
            continue
        winrate = v.get("winrate", 0.0)
        n = v.get("n", 0)
        proxy_roi = (winrate - 0.524) / 0.524 if winrate > 0 else 0.0
        bucket_map[k] = {"roi": proxy_roi, "n": n}

    # ─── Locked picks ───
    locked_picks: list[dict] = []
    if locked_ids:
        wanted_ids = [s.strip() for s in locked_ids.split(",") if s.strip()]
        if wanted_ids:
            locked_picks = await db.picks.find(
                {"id": {"$in": wanted_ids}, "pick_date": _today_str()},
                {"_id": 0},
            ).to_list(length=len(wanted_ids))

    # ─── Build ───
    top = build_top_parlays(
        pool, target_legs=target_legs, high_risk=is_high_risk,
        bucket_map=bucket_map, rank=max(1, rank),
        locked_picks=locked_picks if locked_picks else None,
        single_sport_mode=is_single_sport,
    )

    if not top:
        hints = []
        if mode_lower == "single" and sport_q:
            hints.append(f"in {sport_q}")
        elif mode_lower == "custom" and include_sports:
            hints.append(f"in {include_sports}")
        if window_hours != 24:
            hints.append(f"within {window_hours}h")
        hint_str = (" " + " ".join(hints)) if hints else ""
        return {
            "parlay": None,
            "parlays": [],
            "reason": (
                f"Not enough qualifying picks today{hint_str} to build a "
                f"{target_legs}-leg parlay (need Lock>=88, Edge>=+3%, "
                f"positive ROI)."
            ),
            "rank": rank,
            "locked_ids": [p.get("id") for p in locked_picks],
            "window_hours": window_hours,
            "sport_mode": mode_lower,
        }

    payloads = [parlay_to_payload(p, bucket_map) for p in top]
    legacy = payloads[1] if len(payloads) > 1 else payloads[0]
    return {
        "parlay": {
            "legs": legacy["legs"],
            "leg_count": legacy["leg_count"],
            "combined_decimal_odds": legacy["combined_decimal_odds"],
            "combined_american_odds": legacy["combined_american_odds"],
            "combined_win_probability": legacy["survival_pct"],
            "payout_on_100": legacy["payout_on_100"],
            "profit_on_100": legacy["profit_on_100"],
        },
        "parlays": payloads,
        "rank": rank,
        "locked_ids": [p.get("id") for p in locked_picks],
        "window_hours": window_hours,
        "sport_mode": mode_lower,
    }
    """Parlay Optimizer V1 — highest-probability parlay builder.

    Modes:
      - standard: Lock>=88, Edge>=+3%, ROI non-negative. Legs 2-5 (target).
      - high_risk: Lock>=75, Edge>=+1%. Legs 10-20 (target).

    Returns TOP 3 parlays labelled SAFE / BALANCED / AGGRESSIVE in one
    response. The builder enforces survival-damage control, no-filler-legs,
    diversification (max 40% same sport, max 2 same game), correlation
    penalties, and anti-hero detection.

    Refresh: pass `rank=2`, `rank=3`, ... to cycle to next-best candidates
    in each survival band. `locked_ids` is a comma-separated list of pick
    IDs that MUST be included in every returned parlay (pin-leg feature).
    """
    from parlay_optimizer import (
        build_top_parlays, parlay_to_payload,
    )
    await _ensure_today_picks()
    is_high_risk = (mode or "").lower() == "high_risk"

    # ─── Build pool with the same filters as before ───
    sport_q = (sport or "").strip()
    sport_filter: dict = {}
    if sport_q and sport_q.lower() not in ("mix", "all", ""):
        sport_filter = {"sport": sport_q}
    else:
        if exclude_sports:
            excluded = [s.strip() for s in exclude_sports.split(",") if s.strip()]
            if excluded:
                sport_filter = {"sport": {"$nin": excluded}}
    lt = (line_type or "").lower()
    line_filter: dict = {}
    if lt == "main":
        line_filter = {"is_alt": {"$ne": True}}
    elif lt == "alt":
        line_filter = {"is_alt": True}
    market_filter: dict = {}
    if market:
        regex = _market_regex(market)
        if regex:
            market_filter = {"market": {"$regex": regex, "$options": "i"}}
    league_filter: dict = {}
    if league:
        league_filter = {"league": {"$regex": str(league).replace("\\", ""), "$options": "i"}}

    target_legs = max(10, min(20, max(1, int(legs or 10)))) if is_high_risk else max(2, min(8, max(1, int(legs or 3))))
    rank = max(1, min(20, int(rank or 1)))  # clamp refresh cursor to 1-20

    # ─── Fetch candidate pool ───
    # Broad pool — optimizer applies hard eligibility filters internally.
    base_q = {
        "pick_date": _today_str(),
        "no_bet": {"$ne": True},
        "is_under_lock": {"$ne": True},
        **sport_filter, **line_filter, **market_filter, **league_filter,
    }
    # In standard mode, use lock>=85 floor so we have headroom above the
    # optimizer's 88 hard cut.  High-risk uses lock>=70 to cast wider net.
    base_q["lock_score"] = {"$gte": 70 if is_high_risk else 85}
    pool = await db.picks.find(base_q, {"_id": 0}).sort("lock_score", -1).limit(300).to_list(length=300)

    # ─── Build bucket-map for ROI scoring ───
    # Convert _historical_winrates → optimizer-expected shape {(sport, family): {roi, n}}.
    # We use winrate-vs-global as a proxy ROI when no learning-v2 row exists.
    raw_buckets = await _historical_winrates()
    global_rate = raw_buckets.get("__global__", 0.55)
    bucket_map: dict = {}
    for k, v in raw_buckets.items():
        if k == "__global__":
            continue
        winrate = v.get("winrate", 0.0)
        n = v.get("n", 0)
        # Proxy ROI: (winrate - 0.524) / 0.524 ≈ ROI assuming -110 vig book.
        # 52.4% win-rate = break-even. 60% → +14% ROI.
        proxy_roi = (winrate - 0.524) / 0.524 if winrate > 0 else 0.0
        bucket_map[k] = {"roi": proxy_roi, "n": n}

    # ─── Resolve locked picks ───
    locked_picks: list[dict] = []
    if locked_ids:
        wanted = [s.strip() for s in locked_ids.split(",") if s.strip()]
        if wanted:
            locked_picks = await db.picks.find(
                {"id": {"$in": wanted}, "pick_date": _today_str()},
                {"_id": 0},
            ).to_list(length=len(wanted))

    # ─── Run the optimizer ───
    top = build_top_parlays(
        pool, target_legs=target_legs, high_risk=is_high_risk,
        bucket_map=bucket_map, rank=max(1, rank),
        locked_picks=locked_picks if locked_picks else None,
    )

    if not top:
        if sport_filter and sport_q:
            sport_hint = f" in {sport_q}"
        elif exclude_sports:
            sport_hint = f" (excluding {exclude_sports})"
        else:
            sport_hint = ""
        return {
            "parlay": None,
            "parlays": [],
            "reason": (
                f"Not enough qualifying picks today{sport_hint} to build a "
                f"{target_legs}-leg parlay (need Lock>=88, Edge>=+3%, "
                f"positive ROI)."
            ),
        }

    payloads = [parlay_to_payload(p, bucket_map) for p in top]
    # Legacy field for backward compatibility — return the BALANCED card
    # (middle of survival) as `parlay`. Frontend should prefer `parlays`.
    legacy = payloads[1] if len(payloads) > 1 else payloads[0]
    return {
        "parlay": {
            # Legacy-shaped fields the old UI consumed
            "legs": legacy["legs"],
            "leg_count": legacy["leg_count"],
            "combined_decimal_odds": legacy["combined_decimal_odds"],
            "combined_american_odds": legacy["combined_american_odds"],
            "combined_win_probability": legacy["survival_pct"],
            "payout_on_100": legacy["payout_on_100"],
            "profit_on_100": legacy["profit_on_100"],
        },
        "parlays": payloads,
        "rank": rank,
        "locked_ids": [p.get("id") for p in locked_picks],
    }


# Static routes MUST be declared BEFORE the parameterized /picks/{pick_id}
# route, otherwise FastAPI's routing would match them as a pick_id.

@api.post("/picks/settle")
async def trigger_settle(user: Annotated[UserPublic, Depends(current_user)]):
    """Manually trigger settlement (also runs every 30 min in background)."""
    result = await settle_due_picks(db)
    return result


@api.get("/picks/history")
async def picks_history(user: Annotated[UserPublic, Depends(current_user)],
                        days: int = 30,
                        rollover_only: bool = False):
    """Settled picks from the last N days, newest first.

    Applies the same correlated-pick dedup as the live picks endpoint so the
    History tab doesn't show "Player Over 0.5 Hits" AND "Player Over 0.5
    Total Bases" as two separate losses — they're one logical bet.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    q: dict = {"settled_at": {"$gte": cutoff}}
    cursor = db.picks.find(q, {"_id": 0}).sort("settled_at", -1).limit(2000)
    picks = await cursor.to_list(length=2000)

    # ─── Dedupe correlated historical picks ───
    # Same logic as sports_engine.generate_all_picks. Group by
    # (sport, event, selection, line_threshold) and keep the preferred one:
    #   1) Market family — Hits > anything > Total Bases
    #   2) Settled status outcome consistency (prefer won > lost > push > pending)
    #      so the user sees the strongest historical signal for that bet.
    #   3) Higher lock_score, then better odds.
    import re as _re
    def _key(p: dict) -> tuple:
        market = p.get("market") or ""
        m = _re.search(r"(-?\d+\.\d+)", market)
        return (
            p.get("sport"), p.get("event"), p.get("selection") or "",
            m.group(1) if m else "",
        )
    def _market_priority(market: str) -> int:
        m = (market or "").lower()
        if "hits" in m: return 0
        if "win or draw" in m or "double chance" in m: return 0
        if "moneyline" in m: return 2
        if "total bases" in m: return 2
        return 1
    _STATUS_RANK = {"won": 0, "lost": 1, "push": 2, "pending": 3}

    best: dict = {}
    for p in picks:
        k = _key(p)
        ex = best.get(k)
        if ex is None:
            best[k] = p
            continue
        new_pri = _market_priority(p.get("market"))
        old_pri = _market_priority(ex.get("market"))
        if new_pri != old_pri:
            if new_pri < old_pri:
                best[k] = p
            continue
        new_stat = _STATUS_RANK.get(p.get("status") or "pending", 4)
        old_stat = _STATUS_RANK.get(ex.get("status") or "pending", 4)
        if new_stat != old_stat:
            if new_stat < old_stat:
                best[k] = p
            continue
        if (p.get("lock_score") or 0) > (ex.get("lock_score") or 0):
            best[k] = p
        elif (p.get("lock_score") or 0) == (ex.get("lock_score") or 0):
            if (p.get("book_odds") or -9999) > (ex.get("book_odds") or -9999):
                best[k] = p
    picks = sorted(best.values(), key=lambda p: p.get("settled_at") or "", reverse=True)

    settled = [p for p in picks if p.get("status") in ("won", "lost", "push")]
    won = sum(1 for p in settled if p.get("status") == "won")
    lost = sum(1 for p in settled if p.get("status") == "lost")
    push = sum(1 for p in settled if p.get("status") == "push")
    decided = won + lost
    hit_rate = round(won / decided * 100, 1) if decided else 0.0
    rollover_picks = [p for p in settled if (p.get("lock_score") or 0) >= 90]
    ro_won = sum(1 for p in rollover_picks if p.get("status") == "won")
    ro_lost = sum(1 for p in rollover_picks if p.get("status") == "lost")
    ro_push = sum(1 for p in rollover_picks if p.get("status") == "push")
    ro_decided = ro_won + ro_lost
    ro_hit_rate = round(ro_won / ro_decided * 100, 1) if ro_decided else 0.0
    if rollover_only:
        # Stats must reflect the SAME scope as the returned picks list.
        # Previously we returned rollover_picks but kept the broader stats —
        # showed e.g. "6 picks · 77 won" which was nonsense.
        settled = rollover_picks
        return {
            "picks": settled,
            "stats": {
                "total": len(settled),
                "won": ro_won,
                "lost": ro_lost,
                "push": ro_push,
                "hit_rate": ro_hit_rate,
                "rollover_hit_rate": ro_hit_rate,
                "rollover_decided": ro_decided,
            },
        }
    return {
        "picks": settled,
        "stats": {
            "total": len(settled),
            "won": won,
            "lost": lost,
            "push": push,
            "hit_rate": hit_rate,
            "rollover_hit_rate": ro_hit_rate,
            "rollover_decided": ro_decided,
        },
    }


@api.get("/picks/{pick_id}")
async def pick_detail(pick_id: str,
                      user: Annotated[UserPublic, Depends(current_user)]):
    pick = await db.picks.find_one({"id": pick_id}, {"_id": 0})
    if not pick:
        raise HTTPException(status_code=404, detail="Pick not found")
    if not pick.get("explanation"):
        from ai_engine import _fallback_explanation, _fallback_killer
        if pick.get("lock_score", 0) >= 85:
            pick["explanation"] = _fallback_explanation(pick)
        else:
            pick["explanation"] = _fallback_killer(pick)
        pick["ai_pending"] = True
    else:
        pick["ai_pending"] = False
    return pick


@api.post("/picks/{pick_id}/ai-explain")
async def pick_ai_explain(pick_id: str,
                          user: Annotated[UserPublic, Depends(current_user)]):
    """Generate (or fetch cached) Claude Sonnet 4.5 explanation for a pick.
    Frontend calls this after the initial pick_detail render so the spinner
    stays scoped to the AI box only."""
    pick = await db.picks.find_one({"id": pick_id}, {"_id": 0})
    if not pick:
        raise HTTPException(status_code=404, detail="Pick not found")
    # If we already cached a real AI explanation, return it.
    cached = pick.get("explanation_ai")
    if cached:
        return {"explanation": cached, "source": "cached"}
    # All picks reaching the UI are recommended picks (NO_BET filter removed
    # the bad ones). Always generate the "why to BET" explanation, never the
    # legacy bet-killer warning.
    text, real = await explain_pick(pick)
    if real:
        await db.picks.update_one(
            {"id": pick_id},
            {"$set": {"explanation": text, "explanation_ai": text}},
        )
    return {"explanation": text, "source": "live" if real else "fallback"}


@api.post("/picks/refresh")
async def force_refresh(user: Annotated[UserPublic, Depends(current_user)]):
    """Manually refresh today's picks. Rate-limited to 1× per hour per user
    to prevent button-mashing that burns The Odds API credits
    (each refresh costs ~250-400 credits)."""
    now = datetime.now(timezone.utc)
    # Check last refresh time for this user (stored in user doc).
    user_doc = await db.users.find_one({"id": user.id}, {"_id": 0, "last_refresh_at": 1})
    last_iso = (user_doc or {}).get("last_refresh_at")
    if last_iso:
        try:
            last_dt = datetime.fromisoformat(last_iso)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            elapsed = (now - last_dt).total_seconds()
            cooldown = 3600  # 1 hour
            if elapsed < cooldown:
                remaining_min = int((cooldown - elapsed) // 60) + 1
                # Return current pick count without burning a refresh.
                existing = await db.picks.count_documents({"pick_date": _today_str()})
                return {
                    "refreshed": False,
                    "rate_limited": True,
                    "retry_after_minutes": remaining_min,
                    "count": existing,
                    "date": _today_str(),
                    "message": f"Picks were refreshed recently. Try again in {remaining_min} min — saves API credits.",
                }
        except Exception:
            pass
    count = await _refresh_picks(_today_str())
    await db.users.update_one(
        {"id": user.id},
        {"$set": {"last_refresh_at": now.isoformat()}},
    )
    return {"refreshed": True, "count": count, "date": _today_str()}


# ───────────────────────── Loss Analysis ─────────────────────────

@api.post("/picks/{pick_id}/loss-analysis")
async def pick_loss_analysis(pick_id: str,
                             user: Annotated[UserPublic, Depends(current_user)]):
    """AI 'Why It Lost' breakdown for a losing pick."""
    pick = await db.picks.find_one({"id": pick_id}, {"_id": 0})
    if not pick:
        raise HTTPException(status_code=404, detail="Pick not found")
    if pick.get("status") != "lost":
        return {"analysis": "This pick wasn't recorded as a loss. No analysis available.",
                "source": "skip"}
    if pick.get("loss_analysis"):
        return {"analysis": pick["loss_analysis"], "source": "cached"}
    text, real = await analyze_loss(pick)
    if real:
        await db.picks.update_one(
            {"id": pick_id},
            {"$set": {"loss_analysis": text, "loss_analysis_ai": text}},
        )
    return {"analysis": text, "source": "live" if real else "fallback"}


@api.get("/stats/summary")
async def stats_summary(user: Annotated[UserPublic, Depends(current_user)]):
    await _ensure_today_picks()
    today = _today_str()
    pipeline = [
        {"$match": {"pick_date": today}},
        {"$group": {
            "_id": "$sport",
            "count": {"$sum": 1},
            "avg_lock": {"$avg": "$lock_score"},
            "avg_edge": {"$avg": "$edge_percent"},
            # Elite = explicitly anchored to a star player (Mbappé/Haaland/
            # Messi/Kane/Ronaldo/Sinner/Jokic/Judge/etc.), OR very high lock.
            # The `elite_player` flag is the canonical signal — the lock-score
            # threshold is only a fallback for sports without anchor coverage.
            "elite": {"$sum": {"$cond": [
                {"$or": [
                    {"$eq": ["$elite_player", True]},
                    {"$gte": ["$lock_score", 95]},
                ]}, 1, 0,
            ]}},
        }},
    ]
    by_sport = [
        {"sport": r["_id"], "count": r["count"],
         "avg_lock": round(r["avg_lock"], 1) if r["avg_lock"] else 0,
         "avg_edge": round(r["avg_edge"], 2) if r["avg_edge"] else 0,
         "elite_count": r["elite"]}
        async for r in db.picks.aggregate(pipeline)
    ]
    total = await db.picks.count_documents({"pick_date": today})
    elite = await db.picks.count_documents({
        "pick_date": today,
        "$or": [
            {"elite_player": True},
            {"lock_score": {"$gte": 95}},
        ],
    })
    avg_edge_agg = await db.picks.aggregate([
        {"$match": {"pick_date": today, "lock_score": {"$gte": 85}}},
        {"$group": {"_id": None, "avg": {"$avg": "$edge_percent"}}},
    ]).to_list(1)
    avg_edge = round(avg_edge_agg[0]["avg"], 2) if avg_edge_agg else 0
    return {"date": today, "total_picks": total, "elite_count": elite,
            "avg_edge_percent": avg_edge, "by_sport": by_sport}


@api.get("/analytics/model-performance")
async def model_performance(
    user: Annotated[UserPublic, Depends(current_user)],
    days: int = 30,
    backfill: bool = True,
):
    """Auto-tracked model performance: ROI, CLV, Edge, calibration. Does NOT
    require the user to log any bets — every generated pick is simulated as
    a 1u flat stake."""
    from analytics import backfill_metrics, compute_model_performance
    if backfill:
        await backfill_metrics(db)
    return await compute_model_performance(db, days=days)


@api.get("/analytics/learned-weights")
async def learned_weights(user: Annotated[UserPublic, Depends(current_user)]):
    """What the self-tuning engine has learned from past picks. Used by the
    Analytics screen to surface every active bucket weight + calibration
    correction the engine is currently applying to new picks."""
    doc = await db.learned_weights.find_one({"_id": "current"}, {"_id": 0})
    if not doc:
        return {"buckets": [], "calibration": [], "updated_at": None, "sample_size": 0}
    return doc


@api.get("/analytics/v2")
async def analytics_v2(user: Annotated[UserPublic, Depends(current_user)]):
    """Learning System v2 dashboard payload.

    Returns: market performance rows, band calibration, market weights,
    learning changes log (last 30), and high-level totals. Used by the new
    Analytics dashboard sections."""
    state = await db.learning_state.find_one({"_id": "learning_v2_state"},
                                             {"_id": 0}) or {}
    # Last 30 audit log entries (most recent first).
    log = await db.learning_log.find({}, {"_id": 0}).sort(
        "ts", -1
    ).to_list(length=30)
    state["changes_log"] = log
    # Sport-level profit summary.
    sport_rows: dict[str, dict] = {}
    async for p in db.picks.aggregate([
        {"$match": {"status": {"$in": ["won", "lost", "push"]}}},
        {"$group": {
            "_id": "$sport",
            "n": {"$sum": 1},
            "won": {"$sum": {"$cond": [{"$eq": ["$status", "won"]}, 1, 0]}},
            "lost": {"$sum": {"$cond": [{"$eq": ["$status", "lost"]}, 1, 0]}},
            "units_risked": {"$sum": {"$ifNull": ["$units_risked", 0]}},
            "units_profit": {"$sum": {"$ifNull": ["$units_profit", 0]}},
            "clv_avg": {"$avg": "$clv_value"},
        }},
    ]):
        s = p["_id"]
        risked = p.get("units_risked") or 0
        profit = p.get("units_profit") or 0
        sport_rows[s] = {
            "sport": s,
            "n": p["n"], "won": p["won"], "lost": p["lost"],
            "units_risked": round(risked, 2),
            "units_profit": round(profit, 2),
            "roi_pct": round((profit / risked * 100.0) if risked else 0, 2),
            "hit_rate_pct": round((p["won"] / (p["won"] + p["lost"]) * 100.0)
                                  if (p["won"] + p["lost"]) else 0, 2),
            "clv_avg": round(p.get("clv_avg") or 0, 2),
        }
    state["profit_by_sport"] = list(sport_rows.values())

    # Bet-Type breakdown — STRAIGHT (1.0u) / REDUCED (0.5u) / PARLAY (0.25u)
    # ROI is now weighted per spec so heavy chalk doesn't distort the metric.
    bt_rows: dict[str, dict] = {}
    async for p in db.picks.aggregate([
        {"$match": {"status": {"$in": ["won", "lost", "push"]}}},
        {"$group": {
            "_id": {"$ifNull": ["$bet_type", "STRAIGHT"]},
            "n": {"$sum": 1},
            "won": {"$sum": {"$cond": [{"$eq": ["$status", "won"]}, 1, 0]}},
            "lost": {"$sum": {"$cond": [{"$eq": ["$status", "lost"]}, 1, 0]}},
            "units_risked": {"$sum": {"$ifNull": ["$units_risked", 0]}},
            "units_profit": {"$sum": {"$ifNull": ["$units_profit", 0]}},
        }},
    ]):
        bt = p["_id"]
        risked = p.get("units_risked") or 0
        profit = p.get("units_profit") or 0
        bt_rows[bt] = {
            "bet_type": bt,
            "n": p["n"], "won": p["won"], "lost": p["lost"],
            "units_risked": round(risked, 2),
            "units_profit": round(profit, 2),
            "roi_pct": round((profit / risked * 100.0) if risked else 0, 2),
            "hit_rate_pct": round((p["won"] / (p["won"] + p["lost"]) * 100.0)
                                  if (p["won"] + p["lost"]) else 0, 2),
        }
    state["profit_by_bet_type"] = list(bt_rows.values())
    return state


@api.post("/analytics/v2/recompute")
async def analytics_v2_recompute(user: Annotated[UserPublic, Depends(current_user)]):
    """Force re-run of the v2 learning aggregation (market perf, calibration,
    band gates, market weights, audit log). Returns the new state summary."""
    from learning_system_v2 import recompute_and_persist
    return await recompute_and_persist(db)


# ──────────────────────────────────────────────────────────────────────────
# Isolated learning buckets (sport × market_type × prop_type)
# ANALYTICS-ONLY: see /app/backend/learning_buckets.py — does NOT influence
# predictions, lock scores, or confidence outputs. Pure dashboard.
# ──────────────────────────────────────────────────────────────────────────

@api.get("/analytics/buckets")
async def analytics_buckets(user: Annotated[UserPublic, Depends(current_user)]):
    """Return per-sport, per-market-type, per-prop-type bucket performance.

    NEVER influences live predictions. Pure analytics for monitoring how
    each isolated learning group is performing.
    """
    from learning_buckets import get_buckets
    return await get_buckets(db)


@api.post("/analytics/buckets/recompute")
async def analytics_buckets_recompute(user: Annotated[UserPublic, Depends(current_user)]):
    """Force re-scan settled picks and rebuild all isolated learning buckets.

    Snapshots the previous state for rollback (keeps last 5). Analytics-only.
    """
    from learning_buckets import recompute_buckets
    return await recompute_buckets(db)


@api.post("/analytics/buckets/rollback")
async def analytics_buckets_rollback(
    user: Annotated[UserPublic, Depends(current_user)],
    snapshot_index: int = 1,
):
    """Restore the Nth-most-recent bucket snapshot. snapshot_index=1 = the
    previous version, =2 = two versions ago. Analytics-only."""
    from learning_buckets import rollback_buckets
    return await rollback_buckets(db, snapshot_index=max(1, min(5, int(snapshot_index or 1))))


@api.post("/analytics/learn")
async def learn_now(user: Annotated[UserPublic, Depends(current_user)]):
    """Force a recompute of learned weights and re-apply to today's picks."""
    from learning_engine import recompute_learned_weights, apply_learning
    weights = await recompute_learned_weights(db)
    # Apply to all picks generated for today that haven't been settled.
    cursor = db.picks.find(
        {"pick_date": _today_str(), "status": {"$in": [None, "pending"]}},
        {"_id": 0},
    )
    adjusted = 0
    async for p in cursor:
        before = p.get("win_probability")
        await apply_learning(db, p)
        if p.get("learning") and p.get("win_probability") != before:
            adjusted += 1
            await db.picks.update_one(
                {"id": p["id"]},
                {"$set": {
                    "win_probability": p["win_probability"],
                    "lock_score": p.get("lock_score"),
                    "edge_percent": p.get("edge_percent"),
                    "implied_probability": p.get("implied_probability"),
                    "learning": p.get("learning"),
                }},
            )
    return {"active_buckets": sum(1 for b in weights.get("buckets", []) if b.get("active")),
            "picks_adjusted": adjusted,
            "sample_size": weights.get("sample_size", 0)}


@api.get("/")
async def root():
    return {"ok": True, "service": "PerksLocks AI", "date": _today_str()}


# ────────────────────── App wiring ──────────────────────

app.include_router(api)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ────────────────────── Reliability layer ──────────────────────
# Surgical hardening that does NOT change behavior — only catches errors,
# logs them, and returns friendly JSON instead of HTML 500 pages.
import time as _time_mod
import traceback as _traceback

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware


class _ReliabilityMiddleware(BaseHTTPMiddleware):
    """Catches every uncaught exception, logs with full traceback + request
    context, and returns a friendly JSON error. Also tracks per-route
    response times for monitoring.
    """
    async def dispatch(self, request: Request, call_next):
        rid = uuid.uuid4().hex[:12]
        request.state.request_id = rid
        start = _time_mod.perf_counter()
        try:
            response = await call_next(request)
            elapsed_ms = (_time_mod.perf_counter() - start) * 1000.0
            # Slow-request log (>1500 ms — likely a perf regression)
            if elapsed_ms > 1500:
                logger.warning(
                    "SLOW %s %s → %d in %.0fms (rid=%s)",
                    request.method, request.url.path, response.status_code,
                    elapsed_ms, rid,
                )
            # Always set a request-id header so the frontend can echo it
            # back in bug reports.
            response.headers["X-Request-ID"] = rid
            return response
        except Exception as exc:
            tb = _traceback.format_exc()
            logger.error(
                "UNHANDLED %s %s rid=%s\n%s\n%s",
                request.method, request.url.path, rid, exc, tb,
            )
            return JSONResponse(
                status_code=500,
                content={
                    "error": "Something went wrong — please retry.",
                    "request_id": rid,
                },
                headers={"X-Request-ID": rid},
            )


app.add_middleware(_ReliabilityMiddleware)


async def _daily_refresh_loop():
    """Refresh picks once on startup, then daily at 06:00 UTC."""
    try:
        await _ensure_today_picks()
    except Exception as e:
        logger.warning("Startup picks seed failed: %s", e)
    while True:
        try:
            now = datetime.now(timezone.utc)
            tomorrow_6 = datetime.combine(
                (now + timedelta(days=1)).date(), dtime(6, 0), tzinfo=timezone.utc)
            sleep_for = (tomorrow_6 - now).total_seconds()
            await asyncio.sleep(max(sleep_for, 60))
            await _refresh_picks(_today_str())
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("Daily refresh failed: %s", e)
            await asyncio.sleep(3600)


async def _settlement_loop():
    """Run settlement every 2 hours to mark completed picks Won/Lost.

    Was 30 min — burned ~480 Odds API credits/day on score polls. Most games
    take 2-4hrs to complete so 30-min polling was wasted spend; 2-hour cycle
    drops credit usage by 75% while still settling everything within a few
    hours of game-end.

    After each settlement run, also recompute Learning v2 state so the next
    pick refresh picks up updated market weights + band-gate raises.
    """
    await asyncio.sleep(60)  # let startup settle
    while True:
        try:
            await settle_due_picks(db)
            # Recompute Learning v2 immediately so new settlements feed
            # forward into the next pick generation cycle.
            try:
                from learning_system_v2 import recompute_and_persist
                v2_res = await recompute_and_persist(db)
                if not v2_res.get("gated"):
                    logger.info("Learning v2 recomputed: %d rows, %d weight overrides, %d log entries",
                                v2_res.get("rows", 0),
                                len(v2_res.get("market_weights") or {}),
                                v2_res.get("changes_log_count", 0))
            except Exception as e:
                logger.warning("Learning v2 recompute error: %s", e)
            # Brain memory cache-bust so the next pick refresh picks up
            # the freshly-settled samples (calibration / ROI / market perf).
            try:
                from brain import process_brain  # noqa: F401 — for module import side effect
                from brain.pipeline import on_settlement
                await on_settlement(db)
            except Exception as e:
                logger.warning("Brain cache-bust error: %s", e)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("Settlement loop error: %s", e)
        await asyncio.sleep(7200)  # 2 hours


async def _weekly_model_tuning_loop():
    """Once a week, recompute learned weights from the FULL settled-pick
    dataset and reseed the model. This is the user-requested weekly model
    adjustment — the per-settlement recompute already updates incrementally,
    but the weekly run also re-applies fresh weights to today's open picks.
    """
    await asyncio.sleep(180)  # let startup settle
    SEVEN_DAYS = 7 * 24 * 3600
    while True:
        try:
            from learning_engine import recompute_learned_weights, apply_learning
            weights = await recompute_learned_weights(db)
            # Re-apply to all open picks for the next 7 days.
            cursor = db.picks.find(
                {"status": {"$in": [None, "pending"]}},
                {"_id": 0},
            )
            adjusted = 0
            async for p in cursor:
                before = p.get("win_probability")
                await apply_learning(db, p)
                if p.get("learning") and p.get("win_probability") != before:
                    adjusted += 1
                    await db.picks.update_one(
                        {"id": p["id"]},
                        {"$set": {"win_probability": p["win_probability"],
                                   "lock_score": p.get("lock_score"),
                                   "edge_percent": p.get("edge_percent"),
                                   "implied_probability": p.get("implied_probability"),
                                   "learning": p.get("learning")}},
                    )
            active = sum(1 for b in weights.get("buckets", []) if b.get("active"))
            logger.info("Weekly model tuning: %d active buckets, %d picks re-weighted",
                        active, adjusted)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("Weekly tuning failed: %s", e)
        await asyncio.sleep(SEVEN_DAYS)


@app.on_event("startup")
async def on_startup():
    await db.users.create_index("email", unique=True)
    await db.picks.create_index([("pick_date", 1), ("sport", 1)])
    await db.picks.create_index([("pick_date", 1), ("lock_score", -1)])
    await db.picks.create_index([("status", 1), ("settled_at", -1)])
    await db.picks.create_index("id", unique=True)
    asyncio.create_task(_daily_refresh_loop())
    asyncio.create_task(_settlement_loop())
    asyncio.create_task(_weekly_model_tuning_loop())
    logger.info("PerksLocks AI started")


@app.on_event("shutdown")
async def on_shutdown():
    client.close()
