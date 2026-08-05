"""One-off Odds API usage audit — read-only.

Answers "what is burning our credits?" by aggregating `odds_api_request_log`.

Produces a report grouped by:
  • caller (which code path)
  • endpoint_path (URL path — reveals bulk vs event-odds vs alt-lines)
  • sport_key
  • hour-of-day (UTC)
  • cache_status breakdown

Includes an estimated-credits column (The Odds API bills per market on odds
endpoints; 1 credit for /sports listings). We compute credits ONLY for calls
that hit upstream (`upstream_called=True`).

Usage:
    cd /app/backend && python -m scripts.odds_usage_audit --hours 24
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
    """Estimate credits for a single upstream call."""
    if not doc.get("upstream_called"):
        return 0
    ep = (doc.get("endpoint_path") or "").lower()
    markets = doc.get("markets") or ""
    # /sports listing → 1 credit
    if ep.endswith("/sports") or ep.endswith("/sports/"):
        return 1
    # events listing (no odds) → 1 credit
    if "/events" in ep and "/odds" not in ep and "player_" not in markets:
        # events discovery endpoint; still bills at 1
        return 1
    n_markets = len([m for m in markets.split(",") if m.strip()]) or 1
    # regions default = us; each region multiplies credits, but our log
    # doesn't always capture regions. Assume 1 region for conservative est.
    return n_markets


def _short(s: str, n: int = 70) -> str:
    if not s:
        return "-"
    return s if len(s) <= n else s[: n - 1] + "…"


async def main(hours: int, top_n: int) -> None:
    client = AsyncIOMotorClient(MONGO_URL)
    db = client[DB_NAME]
    coll = db.odds_api_request_log

    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

    total = await coll.count_documents({"ts": {"$gte": since}})
    upstream = await coll.count_documents(
        {"ts": {"$gte": since}, "upstream_called": True}
    )
    hits = total - upstream
    hit_rate = round((hits / total) * 100.0, 2) if total else 0.0

    # Aggregate credits per caller / endpoint / sport / hour
    per_caller = defaultdict(lambda: {"n": 0, "credits": 0, "up": 0})
    per_endpoint = defaultdict(lambda: {"n": 0, "credits": 0, "up": 0})
    per_sport = defaultdict(lambda: {"n": 0, "credits": 0, "up": 0})
    per_hour = defaultdict(lambda: {"n": 0, "credits": 0, "up": 0})
    per_cache_status = defaultdict(lambda: {"n": 0, "credits": 0})
    # Deep: caller × endpoint × markets — most granular
    per_deep = defaultdict(lambda: {"n": 0, "credits": 0, "up": 0})

    total_credits = 0
    async for d in coll.find({"ts": {"$gte": since}}):
        c = _estimate_credits(d)
        total_credits += c

        caller = d.get("caller") or "unknown"
        ep = d.get("endpoint_path") or "-"
        sport = d.get("sport_key") or "-"
        markets = d.get("markets") or "-"
        cache_status = d.get("cache_status") or "-"
        ts = d.get("ts", "")
        try:
            hr = ts[11:13]
        except Exception:
            hr = "-"
        up = 1 if d.get("upstream_called") else 0

        per_caller[caller]["n"] += 1
        per_caller[caller]["credits"] += c
        per_caller[caller]["up"] += up

        per_endpoint[ep]["n"] += 1
        per_endpoint[ep]["credits"] += c
        per_endpoint[ep]["up"] += up

        per_sport[sport]["n"] += 1
        per_sport[sport]["credits"] += c
        per_sport[sport]["up"] += up

        per_hour[hr]["n"] += 1
        per_hour[hr]["credits"] += c
        per_hour[hr]["up"] += up

        per_cache_status[cache_status]["n"] += 1
        per_cache_status[cache_status]["credits"] += c

        key = f"{caller} | {_short(ep, 40)} | markets={_short(markets, 40)}"
        per_deep[key]["n"] += 1
        per_deep[key]["credits"] += c
        per_deep[key]["up"] += up

    # Projections
    hours_in_month = 24 * 30
    projected_monthly = int(total_credits * (hours_in_month / max(1, hours)))
    daily = int(total_credits * (24 / max(1, hours)))

    # ─────────────────────────────────────────────────────
    # Print report
    # ─────────────────────────────────────────────────────
    print("=" * 78)
    print(f" ODDS API USAGE AUDIT — window: last {hours}h")
    print("=" * 78)
    print(f" Total logged requests   : {total:,}")
    print(f" Upstream (hit API)      : {upstream:,}")
    print(f" Served from cache       : {hits:,}")
    print(f" Cache hit rate          : {hit_rate}%")
    print(f" ESTIMATED credits used  : {total_credits:,}   ({daily:,}/day extrapolated)")
    print(f" PROJECTED monthly       : {projected_monthly:,} credits")
    print("=" * 78)

    def _print_table(title: str, data: dict, key_label: str, sort_by="credits"):
        rows = sorted(
            data.items(), key=lambda kv: kv[1].get(sort_by, 0), reverse=True
        )[:top_n]
        if not rows:
            return
        print(f"\n▸ {title}")
        print(f" {key_label:<52} {'reqs':>8} {'upstr':>8} {'credits':>10}")
        print(" " + "-" * 80)
        for k, v in rows:
            print(
                f" {_short(str(k), 52):<52} "
                f"{v.get('n', 0):>8,} "
                f"{v.get('up', 0):>8,} "
                f"{v.get('credits', 0):>10,}"
            )

    _print_table("BY CALLER (code path)      — sorted by credits", per_caller, "caller")
    _print_table("BY ENDPOINT PATH           — sorted by credits", per_endpoint, "endpoint")
    _print_table("BY SPORT                   — sorted by credits", per_sport, "sport_key")

    print("\n▸ BY HOUR-OF-DAY (UTC) — reveals scheduled-job spikes")
    print(f" {'hour':<6} {'reqs':>8} {'upstr':>8} {'credits':>10}")
    print(" " + "-" * 40)
    for h in sorted(per_hour.keys()):
        v = per_hour[h]
        bar = "▮" * min(40, v["credits"] // max(1, total_credits // 40 or 1))
        print(f" {h:<6} {v['n']:>8,} {v['up']:>8,} {v['credits']:>10,}  {bar}")

    print("\n▸ CACHE STATUS distribution")
    print(f" {'status':<20} {'reqs':>8} {'credits':>10}")
    print(" " + "-" * 40)
    for k, v in sorted(per_cache_status.items(), key=lambda kv: kv[1]["n"], reverse=True):
        print(f" {k:<20} {v['n']:>8,} {v['credits']:>10,}")

    _print_table(
        "DEEP: caller × endpoint × markets  — top burners",
        per_deep, "caller|endpoint|markets", sort_by="credits"
    )

    print("\n" + "=" * 78)
    print(" DONE.")
    print("=" * 78)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--hours", type=int, default=24)
    p.add_argument("--top", type=int, default=20)
    args = p.parse_args()
    asyncio.run(main(args.hours, args.top))
