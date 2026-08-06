"""phase2gamma_24h_report — Phase 2γ post-cutover measurement.

Aggregates a rolling window of ``odds_api_request_log`` +
``provider_request_intents`` and compares against the Phase 2α
baseline documented in ``/app/PHASE2_BASELINE_REPORT.md``.

Usage
─────
    cd /app/backend
    python scripts/phase2gamma_24h_report.py \
        --hours 24 --out /app/reports/phase2gamma_24h_final.txt

Exit code:
    0 — success target met (credits/day ≤ 3,000)
    2 — over the daily ceiling
    3 — Mongo unavailable / no data
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from motor.motor_asyncio import AsyncIOMotorClient


BASELINE = {
    "upstream_requests_per_day":       1988,
    "credits_per_day":                 3270,
    "projected_monthly_credits":       98100,
    "cache_hit_rate_pct":              55.4,
    "duplicate_calls_within_1min":     617,
}


async def _run(hours: int) -> dict:
    db = AsyncIOMotorClient(os.environ["MONGO_URL"])[
        os.environ.get("DB_NAME", "lockscore_db")
    ]
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    since_iso = since.isoformat()

    # Aggregate from odds_api_request_log
    total = await db.odds_api_request_log.count_documents(
        {"ts": {"$gte": since_iso}})
    upstream = await db.odds_api_request_log.count_documents(
        {"ts": {"$gte": since_iso}, "upstream_called": True})
    hits = total - upstream

    # Committed credits (post-Phase 2γ)
    committed = await db.provider_request_intents.aggregate([
        {"$match": {
            "provider": "odds_api",
            "status": "committed",
            "committed_at": {"$gte": since},
        }},
        {"$group": {"_id": None,
                     "credits": {"$sum": "$actual_credits"},
                     "count":   {"$sum": 1}}},
    ]).to_list(1)
    committed_credits = int((committed or [{}])[0].get("credits", 0))
    committed_count   = int((committed or [{}])[0].get("count", 0))

    # Duplicate suppression counters
    suppressed = await db.odds_api_request_log.count_documents(
        {"ts": {"$gte": since_iso},
         "duplicate_suppressed": True})

    # Budget snapshot
    day_key = now.strftime("%Y-%m-%d")
    month_key = now.strftime("%Y-%m")
    st = await db.provider_budget_state.find_one(
        {"provider": "odds_api", "month_key": month_key}) or {}
    days = (st.get("days") or {}).get(day_key) or {}
    month = st.get("month") or {}

    return {
        "window_hours":               hours,
        "since":                      since_iso,
        "now":                        now.isoformat(),
        "total_requests":             total,
        "upstream_requests":          upstream,
        "cache_hits":                 hits,
        "cache_hit_rate_pct":         round(100.0 * hits / max(1, total), 2),
        "committed_credits":          committed_credits,
        "committed_intents":          committed_count,
        "duplicate_suppressed_calls": suppressed,
        "current_day_used":           int(days.get("used", 0)),
        "current_day_reserved":       int(days.get("reserved", 0)),
        "current_month_used":         int(month.get("used", 0)),
        "current_month_reserved":     int(month.get("reserved", 0)),
        "emergency_used":             int(month.get("emergency_used", 0)),
    }


def _format(report: dict) -> str:
    lines: list[str] = []
    lines.append(
        f"Phase 2γ measurement — {report['window_hours']} h window"
    )
    lines.append("=" * 60)
    lines.append(f"Since:  {report['since']}")
    lines.append(f"Now:    {report['now']}")
    lines.append("")
    lines.append("Baseline (Phase 2α)")
    lines.append(f"  upstream_requests_per_day: {BASELINE['upstream_requests_per_day']}")
    lines.append(f"  credits_per_day:           {BASELINE['credits_per_day']}")
    lines.append(f"  cache_hit_rate_pct:        {BASELINE['cache_hit_rate_pct']}")
    lines.append(f"  duplicate_calls_within_1m: {BASELINE['duplicate_calls_within_1min']}")
    lines.append("")
    lines.append("Observed (window)")
    lines.append(f"  total_requests:            {report['total_requests']}")
    lines.append(f"  upstream_requests:         {report['upstream_requests']}")
    lines.append(f"  cache_hits:                {report['cache_hits']}")
    lines.append(f"  cache_hit_rate_pct:        {report['cache_hit_rate_pct']}")
    lines.append(f"  committed_credits:         {report['committed_credits']}")
    lines.append(f"  committed_intents:         {report['committed_intents']}")
    lines.append(f"  duplicate_suppressed_calls:{report['duplicate_suppressed_calls']}")
    lines.append("")
    lines.append("Budget snapshot (today, month)")
    lines.append(f"  day_used:      {report['current_day_used']}")
    lines.append(f"  day_reserved:  {report['current_day_reserved']}")
    lines.append(f"  month_used:    {report['current_month_used']}")
    lines.append(f"  month_reserved:{report['current_month_reserved']}")
    lines.append(f"  emergency_used:{report['emergency_used']}")
    lines.append("")
    lines.append("Verdict")
    hours = report["window_hours"]
    if hours >= 24:
        cpd = report["current_day_used"]
    else:
        cpd = int(report["committed_credits"] * 24 / max(1, hours))
    lines.append(f"  extrapolated credits/day: {cpd}")
    lines.append(f"  daily_ceiling (3,000):    {'PASS' if cpd <= 3000 else 'FAIL'}")
    lines.append(f"  preferred (~1,300):       {'PASS' if cpd <= 1500 else 'CHECK'}")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--out", type=str,
                     default="/app/reports/phase2gamma_24h_final.txt")
    args = ap.parse_args()
    try:
        report = asyncio.run(_run(args.hours))
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 3
    text = _format(report)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(text)
    print(text)
    if report["current_day_used"] > 3000:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
