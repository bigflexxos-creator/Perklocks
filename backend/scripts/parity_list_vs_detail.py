"""Parity harness: board list (/picks/today?lite=true) vs detail (/picks/{id}) vs canonical DB.

Usage: python scripts/parity_list_vs_detail.py [N]
Reports any pick where win_probability / lock_score / edge / grade differ between surfaces.
"""
import asyncio, os, sys, json, random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
import httpx

CMP = ["win_probability", "lock_score", "edge_percent", "grade", "book_odds", "line", "selection", "market", "publication_version", "truth_fingerprint"]


def _r(v):
    if isinstance(v, float):
        return round(v, 2)
    return v


async def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    db = AsyncIOMotorClient(os.environ["MONGO_URL"])[os.environ.get("DB_NAME", "lockscore_db")]
    base = "http://localhost:8001/api"
    async with httpx.AsyncClient(timeout=180) as c:
        r = await c.post(f"{base}/auth/login", json={"email": "demo@lockscore.ai", "password": "demo123"})
        tok = r.json().get("access_token") or r.json().get("token")
        h = {"Authorization": f"Bearer {tok}"} if tok else {}
        r1 = await c.get(f"{base}/picks/today", params={"lite": "true"}, headers=h)
        body = r1.json()
        rows = body.get("picks", body) if isinstance(body, dict) else body
        print(f"board rows={len(rows)} X-Board-Version={r1.headers.get('x-board-version')}")
        if isinstance(body, dict):
            print("board manifest keys:", [k for k in body.keys() if k != "picks"])
        random.seed(7)
        # prioritise high lock scores (the 89/85 regression class) + random sample
        hi = [p for p in rows if (p.get("lock_score") or 0) >= 85]
        sample = hi[:n // 2] + random.sample(rows, min(len(rows), n - min(len(hi), n // 2)))
        seen = set(); diffs = 0; checked = 0
        for row in sample:
            pid = row.get("pick_id") or row.get("id")
            if not pid or pid in seen:
                continue
            seen.add(pid)
            r2 = await c.get(f"{base}/picks/{pid}", headers=h)
            if r2.status_code != 200:
                print("DETAIL_FAIL", pid, r2.status_code); continue
            det = r2.json()
            doc = await db.picks.find_one({"pick_id": pid}) or {}
            checked += 1
            d = {}
            for k in CMP:
                a, b = _r(row.get(k)), _r(det.get(k))
                if a != b:
                    d[k] = {"list": a, "detail": b, "db": _r(doc.get(k)), "db_published": _r(doc.get({"win_probability": "published_probability", "lock_score": "published_lock_score", "edge_percent": "published_edge", "grade": "published_grade"}.get(k, "__none__")))}
            if d:
                diffs += 1
                print("DIFF", pid, row.get("away_team"), "@", row.get("home_team"), row.get("market"), json.dumps(d, default=str))
        print(f"checked={checked} diffs={diffs}")

asyncio.run(main())
