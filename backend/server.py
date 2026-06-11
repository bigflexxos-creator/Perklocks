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
from ai_engine import explain_pick, bet_killer_warning  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("lockscore")

mongo_url = os.environ["MONGO_URL"]
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ["DB_NAME"]]

app = FastAPI(title="LockScore AI")
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
    """Generate today's picks, replace any existing rows for that date."""
    logger.info("Refreshing picks for %s", date_str)
    picks = await generate_all_picks(date_str)
    for p in picks:
        p["id"] = str(uuid.uuid4())
    await db.picks.delete_many({"pick_date": date_str})
    if picks:
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
                      grade: Optional[str] = None):
    await _ensure_today_picks()
    q: dict = {"pick_date": _today_str(), "lock_score": {"$gte": 85}}
    if sport and sport.lower() != "all":
        q["sport"] = sport
    if grade:
        q["grade"] = grade
    cursor = db.picks.find(q, {"_id": 0}).sort("lock_score", -1).limit(50)
    return {"picks": await cursor.to_list(length=50)}


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
    await _ensure_today_picks()
    q: dict = {"pick_date": _today_str(), "lock_score": {"$lt": 85}}
    if sport and sport.lower() != "all":
        q["sport"] = sport
    cursor = db.picks.find(q, {"_id": 0}).sort("lock_score", 1).limit(50)
    return {"picks": await cursor.to_list(length=50)}


@api.get("/picks/rollover")
async def pick_rollover(user: Annotated[UserPublic, Depends(current_user)]):
    """Single best bet of the day — highest combined score across the board."""
    await _ensure_today_picks()
    cursor = db.picks.find({"pick_date": _today_str(), "lock_score": {"$gte": 85}},
                           {"_id": 0})
    picks = await cursor.to_list(length=500)
    if not picks:
        return {"pick": None}
    # Rank by composite: lock_score (70%) + edge_percent scaled (30%)
    def composite(p: dict) -> float:
        # Lock confidence (full weight) + edge bonus (bettor's expected value).
        return p.get("lock_score", 0) * 1.0 + max(0, p.get("edge_percent", 0)) * 1.5
    best = max(picks, key=composite)
    return {"pick": best, "composite_rank": round(composite(best), 2),
            "total_evaluated": len(picks)}


@api.get("/picks/{pick_id}")
async def pick_detail(pick_id: str,
                      user: Annotated[UserPublic, Depends(current_user)]):
    pick = await db.picks.find_one({"id": pick_id}, {"_id": 0})
    if not pick:
        raise HTTPException(status_code=404, detail="Pick not found")
    # Lazy-cache AI explanations.
    if not pick.get("explanation"):
        if pick.get("lock_score", 0) >= 85:
            text = await explain_pick(pick)
        else:
            text = await bet_killer_warning(pick)
        pick["explanation"] = text
        await db.picks.update_one({"id": pick_id}, {"$set": {"explanation": text}})
    return pick


@api.post("/picks/refresh")
async def force_refresh(user: Annotated[UserPublic, Depends(current_user)]):
    count = await _refresh_picks(_today_str())
    return {"refreshed": True, "count": count, "date": _today_str()}


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
    return {"ok": True, "service": "LockScore AI", "date": _today_str()}


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


@app.on_event("startup")
async def on_startup():
    await db.users.create_index("email", unique=True)
    await db.picks.create_index([("pick_date", 1), ("sport", 1)])
    await db.picks.create_index([("pick_date", 1), ("lock_score", -1)])
    await db.picks.create_index("id", unique=True)
    asyncio.create_task(_daily_refresh_loop())
    logger.info("LockScore AI started")


@app.on_event("shutdown")
async def on_shutdown():
    client.close()
