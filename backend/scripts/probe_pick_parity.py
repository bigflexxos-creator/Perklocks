"""Probe: find a pick by team regex + market and compare DB vs /today?lite=true vs /picks/{id}.

Usage: python scripts/probe_pick_parity.py "Dortmund" "btts"
"""
import asyncio, os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
import httpx

FIELDS = ["pick_id", "sport", "away_team", "home_team", "market", "selection", "line", "odds", "book",
          "win_probability", "lock_score", "edge_percent", "grade", "confidence",
          "published_probability", "published_lock_score", "published_edge", "published_grade",
          "status", "game_time", "signal_score", "evidence_score", "model_version"]


async def main():
    team = sys.argv[1] if len(sys.argv) > 1 else "Dortmund"
    market = sys.argv[2] if len(sys.argv) > 2 else "btts"
    db = AsyncIOMotorClient(os.environ["MONGO_URL"])[os.environ.get("DB_NAME", "lockscore_db")]
    q = {"$and": [{"$or": [{"away_team": {"$regex": team, "$options": "i"}}, {"home_team": {"$regex": team, "$options": "i"}}]},
                  {"market": {"$regex": market, "$options": "i"}}]}
    docs = [d async for d in db.picks.find(q).sort("created_at", -1).limit(6)]
    print(f"DB matches: {len(docs)}")
    for d in docs:
        print("DB  ", json.dumps({k: d.get(k) for k in FIELDS}, default=str))
    if not docs:
        return
    base = "http://localhost:8001/api"
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(f"{base}/auth/login", json={"email": "demo@lockscore.ai", "password": "demo123"})
        tok = r.json().get("access_token") or r.json().get("token")
        h = {"Authorization": f"Bearer {tok}"} if tok else {}
        for d in docs[:3]:
            pid = d["pick_id"]
            r1 = await c.get(f"{base}/picks/today", params={"lite": "true"}, headers=h)
            rows = r1.json()
            rows = rows.get("picks", rows) if isinstance(rows, dict) else rows
            row = next((p for p in rows if p.get("pick_id") == pid), None)
            print("BOARD-VERSION", r1.headers.get("x-board-version"))
            print("LIST", json.dumps({k: row.get(k) for k in FIELDS} if row else None, default=str))
            r2 = await c.get(f"{base}/picks/{pid}", headers=h)
            det = r2.json()
            print("DETL", json.dumps({k: det.get(k) for k in FIELDS}, default=str))
            print("-" * 80)

asyncio.run(main())
