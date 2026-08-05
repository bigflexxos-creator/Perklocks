"""Post-fix Odds API projection.

Runs the same aggregation as `odds_usage_audit.py` but restricts the
window to *since* a specified ISO timestamp — used to project the new
credit burn rate right after the Phase A/B changes go live, before we
have a true 24h post-fix sample.
"""
from __future__ import annotations

import argparse
import asyncio
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

load_dotenv("/app/backend/.env")

MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "lockscore_db")


def _estimate_credits(doc: dict) -> int:
    if not doc.get("upstream_called"):
        return 0
    ep = (doc.get("endpoint_path") or "").lower()
    markets = doc.get("markets") or ""
    if ep.endswith("/sports") or ep.endswith("/sports/"):
        return 1
    if "/events" in ep and "/odds" not in ep and "player_" not in markets:
        return 1
    n_markets = len([m for m in markets.split(",") if m.strip()]) or 1
    return n_markets


async def main(since_iso: str, baseline_daily: int = 16114,
                baseline_monthly: int = 483420) -> None:
    client = AsyncIOMotorClient(MONGO_URL)
    db = client[DB_NAME]
    coll = db.odds_api_request_log

    since_dt = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    window_secs = max(1.0, (now - since_dt).total_seconds())
    window_hours = window_secs / 3600.0

    total = await coll.count_documents({"ts": {"$gte": since_iso}})
    up = await coll.count_documents(
        {"ts": {"$gte": since_iso}, "upstream_called": True})
    hits = total - up
    hit_rate = round(100 * hits / max(1, total), 1)

    total_credits = 0
    per_caller = defaultdict(lambda: {"n": 0, "up": 0, "credits": 0})
    async for d in coll.find({"ts": {"$gte": since_iso}}):
        c = _estimate_credits(d)
        total_credits += c
        caller = d.get("caller") or "unknown"
        per_caller[caller]["n"] += 1
        per_caller[caller]["up"] += 1 if d.get("upstream_called") else 0
        per_caller[caller]["credits"] += c

    hours_in_day = 24.0
    hours_in_month = 24 * 30.0
    proj_daily = int(total_credits * (hours_in_day / max(0.01, window_hours)))
    proj_monthly = int(total_credits *
                        (hours_in_month / max(0.01, window_hours)))

    saved_daily_pct = round(100 * (baseline_daily - proj_daily) /
                              max(1, baseline_daily), 1)
    saved_monthly_pct = round(100 * (baseline_monthly - proj_monthly) /
                                max(1, baseline_monthly), 1)

    print("=" * 78)
    print(f" ODDS API POST-FIX PROJECTION")
    print(f" Window: {since_iso}  →  {now.isoformat()}  ({window_hours:.2f} h)")
    print("=" * 78)
    print(f" Requests in window     : {total:,}")
    print(f" Upstream in window     : {up:,}")
    print(f" Cache hit rate         : {hit_rate}%")
    print(f" Credits burned         : {total_credits:,}")
    print(f" Projected daily        : {proj_daily:,}   "
          f"(baseline {baseline_daily:,}, saved {saved_daily_pct}%)")
    print(f" Projected monthly      : {proj_monthly:,}   "
          f"(baseline {baseline_monthly:,}, saved {saved_monthly_pct}%)")
    print("=" * 78)

    print("\n▸ BY CALLER (projected daily)")
    print(f" {'caller':<55} {'n':>6} {'up':>6} {'crd':>6} {'daily_crd':>10}")
    for k, v in sorted(per_caller.items(),
                       key=lambda kv: kv[1]["credits"], reverse=True)[:15]:
        daily = int(v["credits"] * (hours_in_day / max(0.01, window_hours)))
        print(f" {k[:55]:<55} {v['n']:>6,} {v['up']:>6,} "
              f"{v['credits']:>6,} {daily:>10,}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--since", required=True,
                   help="ISO timestamp of when the fix went live")
    p.add_argument("--baseline-daily", type=int, default=16114)
    p.add_argument("--baseline-monthly", type=int, default=483420)
    args = p.parse_args()
    asyncio.run(main(args.since, args.baseline_daily, args.baseline_monthly))
