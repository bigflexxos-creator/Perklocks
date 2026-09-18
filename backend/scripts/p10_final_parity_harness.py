"""P10 — FINAL PARITY HARNESS.

Compares canonical DB ↔ /picks/today (lite) ↔ /picks/{id} ↔ rollover legs ↔ ATD
slate for: all 90+ picks on the board, per-sport samples, named fixtures
(Dortmund/Stuttgart BTTS, Kane, Sørloth, Endrick, Wheeler), ATD Top 5 and
≥5 ATD games.  Fields: id, publication_version, market, selection, line,
odds, probability, edge, Lock Score, grade, model_version, fingerprint.

Usage: python scripts/p10_final_parity_harness.py [BASE_URL]
Default BASE_URL = http://localhost:8001/api.  Pass a Preview/Production
origin to compare a second surface: the script prints per-surface
fingerprints so two runs can be diffed.
"""
from __future__ import annotations
import asyncio, os, sys, json
sys.path.insert(0, "/app/backend")
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")
from motor.motor_asyncio import AsyncIOMotorClient
import httpx

CMP = ["publication_version", "market", "selection", "line", "book_odds", "win_probability",
       "edge_percent", "lock_score", "grade", "model_version", "truth_fingerprint"]


def _r(v):
    return round(v, 2) if isinstance(v, float) else v


def _db_view(doc: dict) -> dict:
    from services.published_prediction_reader import hydrate
    from services.truth_manifest import truth_fingerprint
    h = hydrate(doc)
    if h.get("publication_version") is None and h.get("snapshot_version") is not None:
        h["publication_version"] = h["snapshot_version"]
    h["truth_fingerprint"] = truth_fingerprint(h)
    return h


async def main():
    base = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8001/api"
    db = AsyncIOMotorClient(os.environ["MONGO_URL"])[os.environ.get("DB_NAME", "lockscore_db")]
    report = {"base": base, "checked": 0, "diffs": [], "fixtures": {}, "atd": {}, "rollover": {}}
    async with httpx.AsyncClient(timeout=200) as c:
        tok = (await c.post(f"{base}/auth/login", json={"email": "demo@lockscore.ai", "password": "demo123"})).json()["access_token"]
        h = {"Authorization": f"Bearer {tok}"}
        board = (await c.get(f"{base}/picks/today", params={"lite": "true"}, headers=h)).json()
        rows = board.get("picks", [])
        report["board_version"] = board.get("board_version")
        report["truth_manifest"] = board.get("truth_manifest")
        # sample: all >=90, plus up to 25 per sport
        sample = [p for p in rows if (p.get("lock_score") or 0) >= 90]
        per_sport: dict[str, int] = {}
        for p in rows:
            s = p.get("sport") or "?"
            if per_sport.get(s, 0) < 25 and p not in sample:
                sample.append(p); per_sport[s] = per_sport.get(s, 0) + 1
        report["sample_by_sport"] = per_sport
        for row in sample:
            pid = row.get("id")
            det = (await c.get(f"{base}/picks/{pid}", headers=h)).json()
            doc = await db.picks.find_one({"id": pid}, {"_id": 0})
            dbv = _db_view(doc) if doc else {}
            report["checked"] += 1
            d = {}
            for k in CMP:
                vals = {"list": _r(row.get(k)), "detail": _r(det.get(k)), "db": _r(dbv.get(k))}
                if len({json.dumps(v, default=str) for v in vals.values()}) > 1:
                    d[k] = vals
            if d:
                report["diffs"].append({"id": pid, "event": row.get("event"), "market": row.get("market"), "diff": d})
        # named fixtures (DB → detail)
        fixtures = {
            "dortmund_stuttgart_btts": {"event": {"$regex": "Dortmund @ VfB Stuttgart"}, "market": "BTTS Yes"},
            "kane": {"selection": "Harry Kane", "status": "pending"},
            "sorloth": {"selection": {"$regex": "S[oø]rloth"}, "status": "pending"},
            "endrick": {"selection": {"$regex": "^Endrick"}, "status": "pending"},
            "wheeler_outs": {"selection": {"$regex": "Wheeler"}, "market": {"$regex": "Outs", "$options": "i"}},
        }
        for name, q in fixtures.items():
            doc = await db.picks.find_one(q, {"_id": 0}, sort=[("event_time", -1)])
            if not doc:
                report["fixtures"][name] = {"status": "NOT_IN_DB"}
                continue
            det = (await c.get(f"{base}/picks/{doc['id']}", headers=h)).json()
            dbv = _db_view(doc)
            diff = {k: {"db": _r(dbv.get(k)), "api": _r(det.get(k))} for k in CMP
                    if json.dumps(_r(dbv.get(k)), default=str) != json.dumps(_r(det.get(k)), default=str)}
            report["fixtures"][name] = {
                "id": doc["id"], "event": doc.get("event"), "market": doc.get("market"),
                "lock_score": det.get("lock_score"), "win_probability": det.get("win_probability"),
                "edge": det.get("edge_percent"), "grade": det.get("grade"),
                "publication_version": det.get("publication_version"),
                "fingerprint": det.get("truth_fingerprint"), "status": "PARITY" if not diff else "DIFF", "diff": diff,
            }
        # ATD slate vs leaderboard vs by-game
        slate = (await c.get(f"{base}/nfl/atd/slate")).json()
        lb = (await c.get(f"{base}/nfl/atd/leaderboard")).json()
        bg = (await c.get(f"{base}/nfl/atd/by-game")).json()
        top5 = [(p.get("player_id"), p.get("td_probability"), p.get("book_odds")) for p in slate.get("top5", [])]
        lb5 = [(p.get("player_id"), p.get("td_probability"), p.get("book_odds")) for p in lb.get("picks", [])[:5]]
        slate_games = {g["canonical_event_id"]: [c_["player_id"] for c_ in g["candidates"]] for g in slate.get("games", [])}
        bg_games = {g["canonical_event_id"]: [c_["player_id"] for c_ in g["picks"]] for g in bg.get("games", [])}
        report["atd"] = {
            "board_version": slate.get("board_version"), "universe_count": slate.get("universe_count"),
            "games": len(slate_games), "top5": top5, "top5_matches_leaderboard": top5 == lb5,
            "by_game_prefix_matches": all(bg_games.get(k, [])[:3] == v[:3] for k, v in slate_games.items()),
        }
        ro = (await c.get(f"{base}/picks/rollover", headers=h)).json()
        report["rollover"] = {"version": ro.get("rollover_version"), "slate": ro.get("slate"),
                              "legs": [(p.get("id"), p.get("event"), p.get("market"), p.get("lock_score")) for p in ro.get("picks", [])]}
        # rollover legs must equal board/detail truth
        for p in ro.get("picks", []):
            det = (await c.get(f"{base}/picks/{p['id']}", headers=h)).json()
            if _r(det.get("lock_score")) != _r(p.get("lock_score")) or _r(det.get("win_probability")) != _r(p.get("win_probability")):
                report["diffs"].append({"id": p["id"], "surface": "rollover_vs_detail",
                                        "diff": {"lock": (p.get("lock_score"), det.get("lock_score"))}})
    report["unexplained_differences"] = len(report["diffs"])
    print(json.dumps(report, indent=1, default=str))

asyncio.run(main())
