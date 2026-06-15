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
    await db.picks.insert_many(picks)
    logger.info("Stored %d picks for %s", len(picks), date_str)
    return len(picks)


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
    "moneyline":      r"moneyline|^win or draw|win$",
    "1x2":            r"moneyline|win or draw|draw",
    "double_chance":  r"win or draw|double chance",
    "btts":           r"both teams to score|btts",
    "goalscorer":     r"goal scorer|anytime|first goal",
    "spread":         r"spread|handicap",
    "run_line":       r"run line|spread",
    "totals":         r"total goals|game total|total points|total runs|total bases|over/under|\bover\b|\bunder\b",
    "player_points":  r"\bpoints\b",
    "player_rebounds": r"rebounds",
    "player_assists": r"assists",
    "player_props":   r"hits|total bases|points|rebounds|assists|passing yards|rushing yards|receiving yards|touchdowns|goal scorer",
    "batter_hits":    r"hits",
    "batter_total_bases": r"total bases",
    "passing_yards":  r"passing yards",
    "rushing_yards":  r"rushing yards",
    "receiving_yards": r"receiving yards",
    "match_winner":   r"moneyline|match winner|to win",
    "sets":           r"\bsets?\b|set winner|set total",
    "games_total":    r"games over|games under|total games",
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
                      sort: Optional[str] = None,
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
    floor = max(85.0, float(min_lock)) if min_lock is not None else 85.0
    q: dict = {"pick_date": _today_str(), "lock_score": {"$gte": floor},
               "is_under_lock": {"$ne": True}}
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
        if s == "time":
            picks.sort(key=lambda p: (_event_dt(p), -p.get("lock_score", 0)))
        elif s == "edge":
            picks.sort(key=lambda p: (_bucket(p), -p.get("edge_percent", 0), -p.get("lock_score", 0)))
        elif s == "implied":
            picks.sort(key=lambda p: (_bucket(p), -p.get("implied_probability", 0), -p.get("lock_score", 0)))
        else:  # "lock" (default)
            picks.sort(key=lambda p: (_bucket(p), -p.get("lock_score", 0)))
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
                           sort: Optional[str] = None,
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
    q: dict = {"pick_date": _today_str(), "is_under_lock": True}
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
    q: dict = {"pick_date": _today_str(), "lock_score": {"$gte": 90}}
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
    cursor = db.picks.find(q, {"_id": 0})
    picks = await cursor.to_list(length=500)
    # Soccer exclusion is the *default* — if the user explicitly filtered to
    # Soccer, respect that. Otherwise keep variance low by dropping soccer.
    if not sport or sport.lower() == "all":
        picks = [p for p in picks if (p.get("sport") or "").lower() != "soccer"]
    # Cap chalk at -400 (was -200). The Rollover tab is for "most likely to
    # hit" — capping too tight excludes legit 75-88% win-prob alt props
    # priced -300 to -400. -400 still rejects absurd -700+ super-chalk.
    picks = [p for p in picks if (p.get("book_odds") or -9999) >= -400]
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
                     market: str | None = None,
                     league: str | None = None):
    """Auto-build a parlay from today's picks.

    Modes:
      - standard: Elite Locks (>=95), fallback to Strong (>=90). Legs 2-8.
      - high_risk: Win-probability >= 66% (was lock_score >= 90 — the user
        explicitly asked for win-prob-based selection here). Legs 10/15/20.

    Optional `market` / `league` narrow the candidate pool further so users
    can build, e.g., a 4-leg MLB Hits parlay or a 3-leg EPL 1X2 parlay.
    """
    await _ensure_today_picks()
    is_high_risk = (mode or "").lower() == "high_risk"
    # Sport filter ("mix" / "all" / empty → no filter; otherwise exact match).
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
    if is_high_risk:
        legs = max(10, min(20, legs))
        # Switched from lock_score >= 90 to win_probability >= 66 per user
        # request. Win-prob is the better signal for "will this hit".
        cursor = db.picks.find(
            {"pick_date": _today_str(), "win_probability": {"$gte": 66},
             **sport_filter, **line_filter, **market_filter, **league_filter},
            {"_id": 0},
        ).sort("win_probability", -1).limit(300)
        pool = await cursor.to_list(length=300)
    else:
        legs = max(2, min(8, legs))
        cursor = db.picks.find(
            {"pick_date": _today_str(), "lock_score": {"$gte": 95},
             **sport_filter, **line_filter, **market_filter, **league_filter},
            {"_id": 0},
        ).sort("lock_score", -1).limit(50)
        pool = await cursor.to_list(length=50)
        if len(pool) < legs:
            extra_cursor = db.picks.find(
                {"pick_date": _today_str(), "lock_score": {"$gte": 90, "$lt": 95},
                 **sport_filter, **line_filter, **market_filter, **league_filter},
                {"_id": 0},
            ).sort("lock_score", -1).limit(50)
            pool.extend(await extra_cursor.to_list(length=50))

    # ─── Apply historical win-rate uplift to each candidate ───
    bucket_rates = await _historical_winrates()
    global_rate = bucket_rates.get("__global__", 0.55)

    def _bucket_key(p: dict) -> tuple:
        sport_ = (p.get("sport") or "").lower()
        market = (p.get("market") or "").lower()
        # Coarse market family — keep buckets stable enough to accumulate
        # statistically meaningful samples (we have 168 settled picks).
        if "anytime goal scorer" in market: family = "goal_scorer"
        elif "win or draw" in market or "double chance" in market: family = "win_or_draw"
        elif "moneyline" in market: family = "moneyline"
        elif "spread" in market: family = "spread"
        elif "over" in market and ("hits" in market or "total bases" in market): family = "batter_over"
        elif "over" in market or "under" in market: family = "total_over_under"
        elif "wins by" in market: family = "mma_method"
        else: family = "other"
        return (sport_, family)

    def _adjusted_score(p: dict) -> float:
        win_p = (p.get("win_probability") or 0) / 100.0
        key = _bucket_key(p)
        sample = bucket_rates.get(key)
        if not sample or sample["n"] < 8:
            # Not enough historical data for this bucket — fall back to
            # win_probability alone (no penalty, no boost).
            return win_p
        # Uplift = bucket historical winrate / global average winrate.
        # Clamped to [0.7, 1.3] so a single bad slice can't fully suppress.
        uplift = max(0.7, min(1.3, sample["winrate"] / max(global_rate, 0.3)))
        return win_p * uplift

    pool.sort(key=_adjusted_score, reverse=True)

    # One leg per event to avoid correlated bets.
    seen_events: set = set()
    selected: list = []
    for p in pool:
        ev_key = (p.get("sport"), p.get("event"))
        if ev_key in seen_events:
            continue
        seen_events.add(ev_key)
        selected.append(p)
        if len(selected) >= legs:
            break
    min_legs = 5 if is_high_risk else 2
    if len(selected) < min_legs:
        sport_hint = f" in {sport_q}" if sport_filter else ""
        threshold = "win-prob 66%+" if is_high_risk else "Lock 90+"
        return {
            "parlay": None,
            "reason": f"Need at least {min_legs} {threshold} picks today{sport_hint} (have {len(selected)})",
        }

    # Convert each leg's American odds → decimal, multiply, convert back.
    def american_to_decimal(american: int) -> float:
        return 1 + (american / 100 if american > 0 else 100 / abs(american))
    decimal_total = 1.0
    for leg in selected:
        decimal_total *= american_to_decimal(int(leg["book_odds"]))
    # Combined American odds.
    if decimal_total >= 2.0:
        combined_american = int(round((decimal_total - 1) * 100))
        combined_str = f"+{combined_american}"
    else:
        combined_american = int(round(-100 / (decimal_total - 1)))
        combined_str = str(combined_american)
    payout_100 = round(100 * decimal_total, 2)
    profit_100 = round(payout_100 - 100, 2)
    # Combined model win probability = product of individual model probs.
    combined_prob = 1.0
    for leg in selected:
        combined_prob *= leg["win_probability"] / 100.0
    return {
        "parlay": {
            "legs": selected,
            "leg_count": len(selected),
            "combined_decimal_odds": round(decimal_total, 3),
            "combined_american_odds": combined_str,
            "combined_win_probability": round(combined_prob * 100, 1),
            "payout_on_100": payout_100,
            "profit_on_100": profit_100,
        }
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
    if pick.get("lock_score", 0) >= 85:
        text, real = await explain_pick(pick)
    else:
        text, real = await bet_killer_warning(pick)
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
            "elite": {"$sum": {"$cond": [{"$gte": ["$lock_score", 95]}, 1, 0]}},
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
    elite = await db.picks.count_documents({"pick_date": today, "lock_score": {"$gte": 95}})
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
    """
    await asyncio.sleep(60)  # let startup settle
    while True:
        try:
            await settle_due_picks(db)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("Settlement loop error: %s", e)
        await asyncio.sleep(7200)  # 2 hours


@app.on_event("startup")
async def on_startup():
    await db.users.create_index("email", unique=True)
    await db.picks.create_index([("pick_date", 1), ("sport", 1)])
    await db.picks.create_index([("pick_date", 1), ("lock_score", -1)])
    await db.picks.create_index([("status", 1), ("settled_at", -1)])
    await db.picks.create_index("id", unique=True)
    asyncio.create_task(_daily_refresh_loop())
    asyncio.create_task(_settlement_loop())
    logger.info("PerksLocks AI started")


@app.on_event("shutdown")
async def on_shutdown():
    client.close()
