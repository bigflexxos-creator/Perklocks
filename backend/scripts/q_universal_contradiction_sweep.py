"""Q-Universal-Settlement Contradiction Sweep — Root Closure 2026-06.

Scans EVERY settled pick in the last 30 days across ALL sports and
detects `SETTLEMENT_RESULT_ACTUAL_CONTRADICTION`: cases where
`pick.final_score` numerically implies a different result than the
canonical `status` (e.g. Over 1.5 · actual=4 · LOST).

Actions per contradiction (never mutates the append-only ledger):
    1. Log the contradiction row.
    2. Suppress the misleading `final_score` on the pick compat-mirror
       (`final_score_suppressed=True`) so consumers cannot paint the
       impossible pairing.  The canonical `status` stays authoritative.
    3. Flag `final_score_needs_reverify=True` so a future runtime
       verifier pass can pull the authoritative actual from the
       first-party source and restore the mirror.

Universal: works for MLB / NFL / NBA / CFB / Soccer / Tennis / NHL /
UFC because it uses only the pick's own market string + line + stored
final_score dict — no sport-specific logic.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)
load_dotenv(os.path.join(_BACKEND, ".env"))

_LINE_RE = re.compile(r"(over|under)\s*(-?\d+(?:\.\d+)?)", re.I)


def _parse_line(market: str):
    m = _LINE_RE.search(market or "")
    if not m:
        return None
    return (float(m.group(2)), m.group(1).lower())


def _first_numeric(fs: dict):
    if not isinstance(fs, dict):
        return None
    for k, v in fs.items():
        if k in ("Line", "line"):
            continue
        try:
            return float(v)
        except Exception:
            continue
    return None


def _grade(actual: float, line: float, side: str) -> str:
    if side == "over":
        if actual > line:  return "won"
        if actual < line:  return "lost"
        return "push"
    if actual < line:      return "won"
    if actual > line:      return "lost"
    return "push"


async def run() -> dict:
    c = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = c[os.environ["DB_NAME"]]
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=60)).isoformat()

    stats = {
        "scanned":              0,
        "contradictions":       0,
        "by_sport":             {},
        "suppressed":           0,
        "already_suppressed":   0,
        "started_at":           now.isoformat(),
    }

    q = {
        "status": {"$in": ["won", "lost", "push"]},
        "$or":  [{"settled_at": {"$gte": cutoff}}, {"event_time": {"$gte": cutoff}}],
        "final_score": {"$exists": True, "$ne": None, "$ne": {}},
        "market": {"$regex": "Over|Under", "$options": "i"},
    }
    total = await db.picks.count_documents(q)
    print(f"[Q-contradiction] scanning {total} settled Over/Under picks…")

    async for p in db.picks.find(q):
        stats["scanned"] += 1
        ls = _parse_line(p.get("market") or "")
        val = _first_numeric(p.get("final_score") or {})
        status = (p.get("status") or "").lower()
        if not ls or val is None or status not in ("won", "lost", "push"):
            continue
        implied = _grade(val, ls[0], ls[1])
        if implied != status:
            sport = p.get("sport") or "?"
            stats["contradictions"] += 1
            stats["by_sport"][sport] = stats["by_sport"].get(sport, 0) + 1
            if p.get("final_score_suppressed"):
                stats["already_suppressed"] += 1
                continue
            await db.picks.update_one(
                {"_id": p["_id"]},
                {"$set": {
                    "final_score_suppressed":      True,
                    "final_score_needs_reverify":  True,
                    "_actual_contradiction":       True,
                    "_actual_contradiction_reason": (
                        f"final_score={val} vs line={ls[0]} ({ls[1]}) "
                        f"implies {implied} but status={status}"),
                    "final_score_pre_suppress":    p.get("final_score"),
                    "contradiction_detected_at":   now.isoformat(),
                }},
            )
            stats["suppressed"] += 1

    stats["finished_at"] = datetime.now(timezone.utc).isoformat()
    return stats


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run()), indent=2, default=str))
