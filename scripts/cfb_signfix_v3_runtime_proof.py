"""Final CFB Signfix v3 Runtime Acceptance Proof
(Produces /tmp/cfb_signfix_v3_runtime_proof.json)

For each of the three physical fixtures, produces:
  - DB canonical row (source of truth)
  - /api/picks/today rows (if visible)
  - /api/picks/{id} detail row
  - Parity check: DB ↔ list ↔ detail (line/odds/wp/ls)
"""
from __future__ import annotations
import asyncio, os, sys, json
sys.path.insert(0, "/app/backend")
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

import httpx
from deps import db

BASE = "http://localhost:8001"
EMAIL, PWD = "demo@lockscore.ai", "demo123"

TARGETS = [
    {"label": "Northwestern @ Indiana / Over 47.5",
     "event_regex": "Northwestern.*Indiana Hoosiers",
     "market_contains": "Total Points Over"},
    {"label": "South Alabama @ Kentucky / Under 54.5",
     "event_regex": "South Alabama.*Kentucky Wildcats",
     "market_contains": "Total Points Under"},
    {"label": "Boise State @ Oregon / current Total",
     "event_regex": "Boise.*Oregon Ducks",
     "market_contains": "Total Points"},
]


async def _db_row(t):
    q = {"sport": "CFB",
         "event": {"$regex": t["event_regex"]},
         "market": {"$regex": t["market_contains"]}}
    rows = await db.picks.find(q).to_list(length=10)
    return [
        {"id": r.get("id"),
         "event": r.get("event"),
         "market": r.get("market"),
         "selection": r.get("selection"),
         "line": r.get("line"),
         "book_odds": r.get("book_odds"),
         "win_probability": r.get("win_probability"),
         "implied_probability": r.get("implied_probability"),
         "edge_percent": r.get("edge_percent"),
         "lock_score": r.get("lock_score"),
         "published_lock_score": r.get("published_lock_score"),
         "cfb_engine_version": r.get("cfb_engine_version"),
         "off_board": bool(r.get("off_board")),
         "publication_state": r.get("publication_state"),
         "probability_provenance": r.get("probability_provenance"),
         "cfb_game_sim": {
             "expected_total": (r.get("cfb_game_sim") or {}).get("expected_total"),
             "expected_margin": (r.get("cfb_game_sim") or {}).get("expected_margin"),
             "total_sigma": (r.get("cfb_game_sim") or {}).get("total_sigma"),
             "signfix_rescored_at": (r.get("cfb_game_sim") or {}).get("signfix_rescored_at"),
         }}
        for r in rows
    ]


async def main():
    out = {"fixtures": []}
    async with httpx.AsyncClient() as c:
        auth = await c.post(f"{BASE}/api/auth/login",
                            json={"email": EMAIL, "password": PWD})
        token = auth.json()["access_token"]

        # /api/picks/today CFB list
        r = await c.get(f"{BASE}/api/picks/today",
                        params={"sport": "CFB", "lite": "true"},
                        headers={"Authorization": f"Bearer {token}"})
        list_picks = r.json().get("picks") or []
        list_by_id = {p.get("id"): p for p in list_picks}

        for t in TARGETS:
            fx = {"target": t["label"], "db_rows": [], "list_rows": [],
                  "detail_parity": []}
            db_rows = await _db_row(t)
            fx["db_rows"] = db_rows
            for r in db_rows:
                pid = r["id"]
                list_row = list_by_id.get(pid)
                if list_row:
                    fx["list_rows"].append({
                        "id": pid,
                        "line": list_row.get("line"),
                        "book_odds": list_row.get("book_odds"),
                        "win_probability": list_row.get("win_probability"),
                        "lock_score": list_row.get("lock_score"),
                    })
                # Detail
                d = await c.get(f"{BASE}/api/picks/{pid}",
                                headers={"Authorization": f"Bearer {token}"})
                if d.status_code == 200:
                    dj = d.json()
                    parity = {
                        "id": pid,
                        "line_match":  dj.get("line") == r["line"],
                        "odds_match":  dj.get("book_odds") == r["book_odds"],
                        "wp_match": abs((dj.get("win_probability") or 0)
                                        - (r["win_probability"] or 0)) < 0.02,
                        "ls_match": abs((dj.get("lock_score") or 0)
                                        - (r["lock_score"] or 0)) < 0.51,
                        "cfb_engine_version": dj.get("cfb_engine_version"),
                        "detail_wp": dj.get("win_probability"),
                        "detail_ls": dj.get("lock_score"),
                    }
                    fx["detail_parity"].append(parity)
                else:
                    fx["detail_parity"].append({"id": pid, "http_status": d.status_code})
            out["fixtures"].append(fx)

    with open("/tmp/cfb_signfix_v3_runtime_proof.json", "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
