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


async def _refresh_picks(date_str: str) -> int:
    """Generate today's picks, replace any existing rows for that date.

    Critical: only delete existing picks AFTER we've successfully generated
    new ones. Otherwise, if the upstream API is down/rate-limited, we'd
    end up with an empty board instead of last-known-good picks.
    """
    logger.info("Refreshing picks for %s", date_str)
    picks = await generate_all_picks(date_str)
    if not picks:
        logger.warning(
            "Refresh produced 0 picks for %s — keeping existing rows intact "
            "instead of wiping the board.", date_str,
        )
        return 0
    for p in picks:
        p["id"] = str(uuid.uuid4())
    await db.picks.delete_many({"pick_date": date_str})
    await db.picks.insert_many(picks)
    logger.info("Stored %d picks for %s", len(picks), date_str)
    return len(picks)


async def _ensure_today_picks() -> None:
    today = _today_str()
    count = await db.picks.count_documents({"pick_date": today})
    if count == 0:
        await _refresh_picks(today)


@api.get("/picks/today")
async def picks_today(user: Annotated[UserPublic, Depends(current_user)],
                      sport: Optional[str] = None,
                      grade: Optional[str] = None,
                      day_offset: Optional[int] = None,
                      line_type: Optional[str] = None,
                      sort: Optional[str] = None):
    """Top picks from today's 72-hour window (lock score >= 85).
    Optional `day_offset` filters picks to a specific calendar day relative
    to today (0=today, 1=tomorrow, 2=day after).
    `line_type`:
      - "main": standard markets only (no alt-prop lines)
      - "alt":  alternate-line picks only (e.g. Over 0.5 Hits, Anytime Goal Scorer)
      - "both" / None (default): unrestricted
    `sort`:
      - "lock" / None (default): highest lock_score first
      - "time": soonest kickoff first
      - "edge": biggest model edge first
    """
    await _ensure_today_picks()
    q: dict = {"pick_date": _today_str(), "lock_score": {"$gte": 85},
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
                           sort: Optional[str] = None):
    """The single safest Under lock across all sports.

    `line_type`:
      - "main": main-line totals only
      - "alt":  alt-prop Unders only
      - "both" / None: unrestricted (default)
    `sort`: "lock" (default), "time", or "edge"
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
                        line_type: Optional[str] = None):
    """Top 3 safest bets of the day — the user picks which one to roll.

    Rules:
      - Today's slate only (kickoff within 24h)
      - Lock score >= 90
      - NO Soccer (small leagues, high variance, too volatile)
      - Prefers player props over team moneylines (lower variance)
      - Ranks by win_probability first, then lock_score — "most likely to hit"
      - Diversifies: at most one pick per game so the trio isn't 3 of the same matchup
      - `line_type`: "main" / "alt" / "both" (default) — same semantics as /picks/today.
    """
    await _ensure_today_picks()
    q: dict = {"pick_date": _today_str(), "lock_score": {"$gte": 90}}
    lt = (line_type or "").lower()
    if lt == "main":
        q["is_alt"] = {"$ne": True}
    elif lt == "alt":
        q["is_alt"] = True
    cursor = db.picks.find(q, {"_id": 0})
    picks = await cursor.to_list(length=500)
    # Restrict Rollover to today's games only (start time within next 24h).
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
    # Exclude Soccer — user feedback: small-league soccer rollovers are too volatile.
    pool = [p for p in pool if (p.get("sport") or "").lower() != "soccer"]
    # Cap chalk at -200 so risk:reward is reasonable. -200 means $200 risked
    # to win $100 — a daily roll should pay decently, not all be chalk.
    pool = [p for p in pool if (p.get("book_odds") or -9999) >= -200]
    if not pool:
        return {"picks": [], "pick": None, "total_evaluated": 0}

    def composite(p: dict) -> float:
        win_prob = p.get("win_probability", 0) or 0
        lock = p.get("lock_score", 0) or 0
        edge = max(0, p.get("edge_percent", 0) or 0)
        league = (p.get("league") or "").lower()
        prop_boost = 2.0 if "props" in league else 0.0
        return (win_prob * 2.0) + (lock * 0.5) + (edge * 0.3) + prop_boost

    ranked = sorted(pool, key=composite, reverse=True)
    # Diversify: one pick per game so the user gets 3 distinct options.
    seen_events: set = set()
    top: list = []
    for p in ranked:
        ev = p.get("event")
        if ev in seen_events:
            continue
        seen_events.add(ev)
        top.append({**p, "composite_rank": round(composite(p), 2)})
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
                     line_type: str | None = None):
    """Auto-build a parlay from today's picks.

    Modes:
      - standard: Elite Locks (>=95), fallback to Strong (>=90). Legs 2-8.
      - high_risk: Lock 90+ across the entire board. Legs 10/15/20 — designed
        as a longshot/lottery ticket where if everything hits, payout is huge.

    Sport filter:
      - None or "mix": cross-sport parlay (default — best variance distribution).
      - Specific sport (e.g. "MLB", "NBA", "Soccer"): single-sport parlay.

    Line type:
      - "main": standard markets only (no alt-prop lines).
      - "alt":  alternate-line picks only.
      - "both" / None: unrestricted (default).
    """
    await _ensure_today_picks()
    is_high_risk = (mode or "").lower() == "high_risk"
    # Sport filter ("mix" / "all" / empty → no filter; otherwise exact match).
    sport_q = (sport or "").strip()
    sport_filter: dict = {}
    if sport_q and sport_q.lower() not in ("mix", "all", ""):
        sport_filter = {"sport": sport_q}
    # Line type filter — same semantics as picks_today.
    lt = (line_type or "").lower()
    line_filter: dict = {}
    if lt == "main":
        line_filter = {"is_alt": {"$ne": True}}
    elif lt == "alt":
        line_filter = {"is_alt": True}
    if is_high_risk:
        legs = max(10, min(20, legs))
        cursor = db.picks.find(
            {"pick_date": _today_str(), "lock_score": {"$gte": 90}, **sport_filter, **line_filter},
            {"_id": 0},
        ).sort("lock_score", -1).limit(200)
        pool = await cursor.to_list(length=200)
    else:
        legs = max(2, min(8, legs))
        cursor = db.picks.find(
            {"pick_date": _today_str(), "lock_score": {"$gte": 95}, **sport_filter, **line_filter},
            {"_id": 0},
        ).sort("lock_score", -1).limit(50)
        pool = await cursor.to_list(length=50)
        if len(pool) < legs:
            extra_cursor = db.picks.find(
                {"pick_date": _today_str(), "lock_score": {"$gte": 90, "$lt": 95}, **sport_filter, **line_filter},
                {"_id": 0},
            ).sort("lock_score", -1).limit(50)
            pool.extend(await extra_cursor.to_list(length=50))
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
        return {
            "parlay": None,
            "reason": f"Need at least {min_legs} Lock 90+ picks today{sport_hint} (have {len(selected)})",
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
    """Settled picks from the last N days, newest first."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    q: dict = {"settled_at": {"$gte": cutoff}}
    cursor = db.picks.find(q, {"_id": 0}).sort("settled_at", -1).limit(500)
    picks = await cursor.to_list(length=500)
    settled = [p for p in picks if p.get("status") in ("won", "lost", "push")]
    won = sum(1 for p in settled if p.get("status") == "won")
    lost = sum(1 for p in settled if p.get("status") == "lost")
    push = sum(1 for p in settled if p.get("status") == "push")
    decided = won + lost
    hit_rate = round(won / decided * 100, 1) if decided else 0.0
    rollover_picks = [p for p in settled if (p.get("lock_score") or 0) >= 90]
    ro_won = sum(1 for p in rollover_picks if p.get("status") == "won")
    ro_lost = sum(1 for p in rollover_picks if p.get("status") == "lost")
    ro_decided = ro_won + ro_lost
    ro_hit_rate = round(ro_won / ro_decided * 100, 1) if ro_decided else 0.0
    if rollover_only:
        settled = rollover_picks
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
    count = await _refresh_picks(_today_str())
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
    """Run settlement every 30 minutes to mark completed picks Won/Lost."""
    await asyncio.sleep(60)  # let startup settle
    while True:
        try:
            await settle_due_picks(db)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("Settlement loop error: %s", e)
        await asyncio.sleep(1800)  # 30 min


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
