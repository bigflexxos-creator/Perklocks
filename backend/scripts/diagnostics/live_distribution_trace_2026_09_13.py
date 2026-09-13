"""Live distribution + BQ authority handoff diagnostic.

Reads the CURRENT ``picks`` collection (candidates + published) and returns:
  * Per-sport histogram in the tiers the user demanded (85-89 / 90-92 /
    93-95 / 96-97 / 98 / 99 / 100).
  * Top-N candidates per sport with WP, v4 composite, per-factor breakdown,
    BQ raw + authority, final LS, and the exact gate that limited the pick.
  * NFL game-market family split (moneyline / spread / total) with counts +
    top-5 rows.
  * Burrow / Dak passing-yard reachability trace (provider → candidate →
    published) using the current DB state.
  * Soccer control comparison against MLB/CFB/NFL top rows.

READ-ONLY. No mutations.  Prints structured JSON so the caller (main agent)
can post-process without re-parsing free-form output.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from collections import Counter, defaultdict
from statistics import mean, pstdev
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

# Ensure /app/backend is on sys.path so `services.*` imports resolve when
# this script is executed from /app/backend/scripts/diagnostics.
import sys, pathlib
_BACKEND_DIR = pathlib.Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

load_dotenv()

TIER_EDGES = [
    ("<85",   lambda s: s < 85),
    ("85-89", lambda s: 85 <= s < 90),
    ("90-92", lambda s: 90 <= s < 93),
    ("93-95", lambda s: 93 <= s < 96),
    ("96-97", lambda s: 96 <= s < 98),
    ("98",    lambda s: 98 <= s < 99),
    ("99",    lambda s: 99 <= s < 100),
    ("100",   lambda s: s >= 100),
]

QB_TARGETS = {"joe burrow", "dak prescott"}
PASS_YDS_HINTS = ("pass yds", "passing yards", "player_pass_yds",
                  "player pass yds", "passing yds")


def _tier_of(score: float) -> str:
    for label, pred in TIER_EDGES:
        if pred(score):
            return label
    return "other"


def _norm(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


def _limiting_gate(pick: dict) -> str:
    lc = pick.get("lock_components") or {}
    bq = pick.get("bet_quality_authority") or {}
    bqc = bq.get("ceiling")
    ls = float(pick.get("lock_score") or 0.0)
    # Ordered by prior authority chain.
    if pick.get("nfl_prop_authority_applied") and not pick.get(
        "nfl_prop_authority_bq_supersedes"
    ):
        return f"nfl_prop_authority_ceiling={pick.get('nfl_prop_authority_ceiling')}"
    if isinstance(bqc, (int, float)) and abs(ls - bqc) < 0.15 and ls >= 92:
        return f"bq_authority_ceiling={bqc}"
    if ls >= 98.9:
        return "hit_99_cap"
    if ls < 85:
        return "below_locks_floor"
    return f"composite_natural={ls}"


def _factor_summary(pick: dict) -> dict:
    """Return the 7 composite components the user asked for in one dict."""
    lc = pick.get("lock_components") or {}
    return {
        "P_confidence":   lc.get("confidence"),
        "R_roi":          lc.get("roi"),
        "H_history":      (pick.get("bet_quality_authority") or {}).get(
                             "components", {}
                          ).get("history"),
        "M_market_align": lc.get("alignment"),
        "C_clv":          lc.get("clv"),
        "S_stability":    lc.get("volatility"),
        "D_data_quality": lc.get("data_quality"),
    }


async def _mongo():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    return client[os.environ.get("DB_NAME", "lockscore_db")]


async def per_sport_distribution(db, pick_date: str | None = None) -> dict:
    out = {}
    match = {"lock_score": {"$type": "number"}}
    if pick_date:
        match["pick_date"] = pick_date
    async for row in db.picks.aggregate([
        {"$match": match},
        {"$group": {"_id": "$sport", "lock_scores": {"$push": "$lock_score"}}},
    ]):
        scores = [float(x or 0.0) for x in row["lock_scores"]]
        tiers = Counter(_tier_of(s) for s in scores)
        out[row["_id"] or "UNKNOWN"] = {
            "n":       len(scores),
            "max":     round(max(scores), 2) if scores else None,
            "mean":    round(mean(scores), 2) if scores else None,
            "pstdev":  round(pstdev(scores), 2) if len(scores) > 1 else None,
            "tiers":   dict(tiers),
        }
    return out


async def top_n_per_sport(db, sport: str, n: int = 20,
                          pick_date: str | None = None) -> list[dict]:
    q = {"sport": sport, "lock_score": {"$type": "number"}}
    if pick_date:
        q["pick_date"] = pick_date
    rows = await db.picks.find(q).sort("lock_score", -1).limit(n).to_list(None)
    return [
        {
            "canonical_pick_id": p.get("canonical_pick_id") or p.get("_id"),
            "event":  p.get("event_name") or p.get("event") or p.get("matchup"),
            "market": p.get("market"),
            "selection": p.get("selection") or p.get("pick"),
            "line":   p.get("line") or p.get("threshold"),
            "book":   p.get("book") or p.get("sportsbook"),
            "odds":   p.get("odds") or p.get("american_odds"),
            "wp":     p.get("win_probability") or p.get("win_prob") or
                      p.get("calibrated_win_probability"),
            "v4_composite": (p.get("lock_components") or {}).get("confidence"),
            "factors": _factor_summary(p),
            "bq_raw":  (p.get("bet_quality_authority") or {}).get("ceiling"),
            "bq_components": (p.get("bet_quality_authority") or {}).get("components"),
            "final_ls": p.get("lock_score"),
            "gate":    _limiting_gate(p),
            "prov":    p.get("probability_provenance"),
            "n_factors": len(p.get("factors") or {}),
        }
        for p in rows
    ]


async def nfl_game_family_split(db, pick_date: str | None = None) -> dict:
    out = {"MONEYLINE": [], "SPREAD": [], "TOTAL": []}
    q = {"sport": "NFL"}
    if pick_date:
        q["pick_date"] = pick_date
    async for p in db.picks.find(q):
        m = _norm(p.get("market"))
        if any(t in m for t in ("moneyline", "match winner", "h2h")):
            fam = "MONEYLINE"
        elif "spread" in m or "handicap" in m or "run line" in m:
            fam = "SPREAD"
        elif "total" in m and not any(
            k in m for k in PASS_YDS_HINTS
        ) and "player" not in m and "team total" not in m:
            fam = "TOTAL"
        else:
            continue
        out[fam].append(p)
    summary = {}
    for fam, rows in out.items():
        scores = [float(r.get("lock_score") or 0) for r in rows]
        top5 = sorted(rows, key=lambda r: r.get("lock_score") or 0, reverse=True)[:5]
        summary[fam] = {
            "candidates": len(rows),
            "n_85+": sum(1 for s in scores if s >= 85),
            "n_90+": sum(1 for s in scores if s >= 90),
            "n_published": sum(
                1 for r in rows if r.get("published_at") or r.get("is_locks")
            ),
            "top5": [
                {
                    "canonical_pick_id": r.get("canonical_pick_id") or r.get("_id"),
                    "event": r.get("event_name") or r.get("event"),
                    "selection": r.get("selection") or r.get("pick"),
                    "line": r.get("line") or r.get("threshold"),
                    "book": r.get("book") or r.get("sportsbook"),
                    "odds": r.get("odds") or r.get("american_odds"),
                    "wp":   r.get("win_probability") or r.get("calibrated_win_probability"),
                    "bq":   (r.get("bet_quality_authority") or {}).get("ceiling"),
                    "lock_score": r.get("lock_score"),
                    "market": r.get("market"),
                }
                for r in top5
            ],
        }
    return summary


async def qb_passing_yards_trace(db, qb_names: set[str]) -> dict:
    """Trace passing yard reachability for the target QBs across the DB."""
    trace = {}
    for qb in qb_names:
        qb_norm = _norm(qb)
        # Any pick tied to this player in candidates + published + rejected.
        rows = await db.picks.find({
            "$or": [
                {"player": {"$regex": qb, "$options": "i"}},
                {"player_name": {"$regex": qb, "$options": "i"}},
                {"selection": {"$regex": qb, "$options": "i"}},
                {"event_name": {"$regex": qb, "$options": "i"}},
                {"event": {"$regex": qb, "$options": "i"}},
                {"description": {"$regex": qb, "$options": "i"}},
            ]
        }).to_list(length=None)
        markets = Counter(_norm(r.get("market")) for r in rows)
        pass_yds_rows = [r for r in rows if any(
            k in _norm(r.get("market")) for k in PASS_YDS_HINTS
        )]
        # Also check the alt-lines feed cache if it stashes provider payload.
        prov_cache = await db.get_collection("odds_snapshots").find({
            "$or": [
                {"players.name": {"$regex": qb, "$options": "i"}},
                {"raw": {"$regex": qb, "$options": "i"}},
            ]
        }).to_list(length=25) if "odds_snapshots" in await db.list_collection_names() else []
        trace[qb] = {
            "total_rows": len(rows),
            "markets_seen": dict(markets),
            "pass_yds_rows": len(pass_yds_rows),
            "pass_yds_thresholds": sorted({
                float(r.get("line") or r.get("threshold") or 0)
                for r in pass_yds_rows
                if isinstance(r.get("line") or r.get("threshold"), (int, float))
            }),
            "pass_yds_sample": [
                {
                    "market": r.get("market"),
                    "line":   r.get("line") or r.get("threshold"),
                    "book":   r.get("book") or r.get("sportsbook"),
                    "odds":   r.get("odds") or r.get("american_odds"),
                    "wp":     r.get("win_probability") or r.get("calibrated_win_probability"),
                    "ls":     r.get("lock_score"),
                    "published": bool(r.get("published_at") or r.get("is_locks")),
                }
                for r in pass_yds_rows[:10]
            ],
            "provider_snapshots_seen": len(prov_cache),
        }
    return trace


async def collection_survey(db) -> dict:
    names = await db.list_collection_names()
    out = {}
    for n in names:
        try:
            out[n] = await db[n].estimated_document_count()
        except Exception as e:
            out[n] = f"err:{e}"
    return out


async def main():
    db = await _mongo()
    from services.perklocks_day import current_slate_day
    today = current_slate_day()
    report = {
        "db_name": db.name,
        "slate_date": today,
        "per_sport_distribution_TODAY": await per_sport_distribution(db, today),
        "per_sport_distribution_ALL_TIME": await per_sport_distribution(db),
        "top20_TODAY": {
            sport: await top_n_per_sport(db, sport, 20, pick_date=today)
            for sport in ("MLB", "CFB", "NFL", "SOCCER")
        },
        "nfl_game_family_split_TODAY": await nfl_game_family_split(db, today),
        "qb_passing_yards_trace_TODAY": await qb_passing_yards_trace(db, QB_TARGETS),
    }
    print(json.dumps(report, default=str, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
