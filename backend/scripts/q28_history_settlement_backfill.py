"""Q28 — Historical Settlement Backfill (2026-06)
=================================================

CONTRACT (per user Root Closure spec):
  - Process historical pending picks (status='pending' AND event_time in past)
  - If an AUTHORITATIVE actual is available → canonical settlement via
    SettlementService.settle_from_pick() (append-only ledger).
  - If NOT available → mark `status='unresolved'` and
    `settlement_status='UNRESOLVED'` with a machine reason.
  - NEVER fabricate actuals. NEVER guess results. NEVER recompute using
    today's models.

Authoritative actual sources considered (in strict priority order):
  1. Existing `settlement_events` row (prediction_id) — already canonically
     settled by the live pipeline; sync the picks mirror if lagging.
  2. Pick document's own `final_score` dict (populated by real feeds at
     event close).  Grader logic computes result from side/line/market.
  3. Otherwise → UNRESOLVED (no fabrication).

Idempotent and chunked; safe to re-run.  Emits a full JSON summary.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

# Guarantee we can import from /app/backend regardless of caller CWD.
_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from services.settlement_service import (  # noqa: E402
    SettlementService, NEW_SETTLEMENT, ALREADY_SETTLED_IDENTICAL, CORRECTION_APPLIED,
)

load_dotenv(os.path.join(_BACKEND, ".env"))

# ── Config ───────────────────────────────────────────────────────────
CHUNK = 500
FINAL_BARRIER_HOURS = 6   # event must be ≥6h old to be "historical"
LOG_EVERY = 5000
UNRESOLVED_REASON_NO_ACTUAL = "no_authoritative_actual_available"
UNRESOLVED_REASON_NO_MARKET = "no_gradeable_market_authority"


# ── Deterministic graders (no ML, no fabrication) ────────────────────
def _to_int(x: Any) -> Optional[int]:
    try:
        return int(x)
    except Exception:
        return None


def _grade_from_final_score(pick: dict) -> Optional[str]:
    """Return 'won'/'lost'/'push' if the final_score dict provides an
    authoritative outcome for this side-aware wager; else None.

    Only handles the two markets that are unambiguously scored purely
    from `final_score` totals: MONELINE (team wins) and TOTAL (over/under
    of combined score).  Everything else (spread/props) requires more
    context and is left for the live pipeline to canonically settle.
    """
    fs = pick.get("final_score")
    if not isinstance(fs, dict) or not fs:
        return None

    home = pick.get("home_team_name")
    away = pick.get("away_team_name")
    if not (home and away):
        return None

    h_pts = _to_int(fs.get(home))
    a_pts = _to_int(fs.get(away))
    if h_pts is None or a_pts is None:
        return None

    market = (pick.get("market") or "").lower()
    selection = (pick.get("selection") or "").strip()
    side = (pick.get("side") or "").strip().lower()
    line = pick.get("line")

    # ── MONELINE (team win) ──
    ml_hints = ("moneyline", "ml", "match winner", "to win")
    if any(h in market for h in ml_hints) or (selection in (home, away) and not line):
        if h_pts == a_pts:
            return "push"     # rare — real books void, but we log push
        winner = home if h_pts > a_pts else away
        return "won" if selection == winner else "lost"

    # ── TOTAL (over/under of combined final) ──
    if line is not None and (side in ("over", "under") or "total" in market):
        try:
            l = float(line)
        except Exception:
            return None
        combined = h_pts + a_pts
        if abs(combined - l) < 1e-9:
            return "push"
        is_over = combined > l
        want_over = side == "over" or "over" in selection.lower()
        return "won" if (is_over == want_over) else "lost"

    return None


# ── Time coercion helper ─────────────────────────────────────────────
def _event_time_is_historical(pick: dict, cutoff: datetime) -> bool:
    et = pick.get("event_time")
    if et is None:
        return False
    if isinstance(et, datetime):
        return et < cutoff
    if isinstance(et, str):
        try:
            dt = datetime.fromisoformat(et.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt < cutoff
        except Exception:
            return False
    return False


async def q28_run() -> dict:
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = client[os.environ["DB_NAME"]]
    svc = SettlementService(db)
    await svc.ensure_indices()

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=FINAL_BARRIER_HOURS)
    cutoff_iso = cutoff.isoformat().replace("+00:00", "Z")

    query = {
        "status": "pending",
        "$or": [
            {"event_time": {"$lt": cutoff_iso}},
            {"event_time": {"$lt": cutoff}},
        ],
    }

    total_matched = await db.picks.count_documents(query)
    print(f"[Q28] historical pending matched: {total_matched}")

    stats = {
        "scanned": 0,
        "settled_from_ledger":  0,
        "settled_from_final":   0,
        "unresolved_no_actual": 0,
        "unresolved_no_market": 0,
        "errors":               0,
        "cutoff_utc":           cutoff_iso,
        "started_at":           now.isoformat(),
        "matched":              total_matched,
    }

    last_id = None
    while True:
        chunk_q = dict(query)
        if last_id is not None:
            chunk_q["_id"] = {"$gt": last_id}
        cursor = db.picks.find(chunk_q).sort("_id", 1).limit(CHUNK)
        chunk = [p async for p in cursor]
        if not chunk:
            break
        last_id = chunk[-1]["_id"]

        for pick in chunk:
            stats["scanned"] += 1
            pid = pick.get("id") or str(pick.get("_id"))

            # 1) Ledger authority
            existing = await db.settlement_events.find_one(
                {"prediction_id": pid, "is_active": True},
                {"result": 1, "actual_result": 1},
            )
            if existing and existing.get("result") in ("won", "lost", "push", "void"):
                # Sync picks mirror if it lags the canonical ledger.
                await db.picks.update_one(
                    {"_id": pick["_id"]},
                    {"$set": {
                        "status": existing["result"],
                        "settlement_status": "SETTLED_FROM_LEDGER",
                        "q28_backfilled_at": now,
                    }},
                )
                stats["settled_from_ledger"] += 1
                continue

            # 2) Deterministic grade from final_score (if available)
            graded = _grade_from_final_score(pick)
            if graded is not None:
                try:
                    resp = await svc.settle_from_pick(
                        pick,
                        result=graded,
                        source="q28_final_score_backfill",
                        actual_result={
                            "home_score": pick.get("final_score", {}).get(pick.get("home_team_name")),
                            "away_score": pick.get("final_score", {}).get(pick.get("away_team_name")),
                            "line": pick.get("line"),
                            "provenance": "authoritative_final_score_attached",
                        },
                        authoritative_event_final=True,
                    )
                    if resp.get("status") in (NEW_SETTLEMENT, ALREADY_SETTLED_IDENTICAL, CORRECTION_APPLIED):
                        await db.picks.update_one(
                            {"_id": pick["_id"]},
                            {"$set": {"settlement_status": "SETTLED_FROM_FINAL_SCORE",
                                       "q28_backfilled_at": now}},
                        )
                        stats["settled_from_final"] += 1
                        continue
                except Exception as e:
                    stats["errors"] += 1
                    print(f"[Q28] settle error pid={pid}: {e}")

            # 3) UNRESOLVED — reason branch
            reason = (UNRESOLVED_REASON_NO_MARKET
                      if (not pick.get("market") and not pick.get("selection"))
                      else UNRESOLVED_REASON_NO_ACTUAL)
            await db.picks.update_one(
                {"_id": pick["_id"]},
                {"$set": {
                    "status": "unresolved",
                    "settlement_status": "UNRESOLVED",
                    "unresolved_reason": reason,
                    "q28_backfilled_at": now,
                }},
            )
            if reason == UNRESOLVED_REASON_NO_MARKET:
                stats["unresolved_no_market"] += 1
            else:
                stats["unresolved_no_actual"] += 1

        if stats["scanned"] % LOG_EVERY < CHUNK:
            print(f"[Q28] progress: scanned={stats['scanned']}/{total_matched}  "
                  f"ledger={stats['settled_from_ledger']}  "
                  f"final={stats['settled_from_final']}  "
                  f"unresolved={stats['unresolved_no_actual']+stats['unresolved_no_market']}  "
                  f"err={stats['errors']}")

    stats["finished_at"] = datetime.now(timezone.utc).isoformat()
    return stats


if __name__ == "__main__":
    out = asyncio.run(q28_run())
    print()
    print("── Q28 SUMMARY ──────────────────────────────────────────")
    print(json.dumps(out, indent=2, default=str))
