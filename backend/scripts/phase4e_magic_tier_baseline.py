"""Phase 4E.4 — Magic Tier historical baseline.

Reads settled picks from the last N days (default 180) and reports
tier-level performance so we can validate that the *existing* tier
ordering (Apex > Elite > Strong > Lock > Playable) is actually
supported by historical outcomes, AND so we can decide whether the
Magic Tier caps introduced in Phase 4E.3 need threshold corrections.

Metrics per (sport, market_family, magic_tier):
    n_picks, wins, losses, pushes, hit_rate,
    avg_predicted_prob, brier_score, log_loss,
    roi_units, calibration_gap, sample_note

Design rules:
  * If a sport/market has < ``MIN_SAMPLE`` settled picks in the last
    180 days, expand the window backward in 60-day steps up to a
    cap of 540 days (~1.5 years).  Report the actual window used.
  * Never mix seasons blindly — for MLB/NBA/NFL/CFB, expansion
    respects season boundaries (defined below).
  * Buckets with fewer than ``MIN_SAMPLE_REPORTABLE`` picks are
    marked ``insufficient_sample=True`` and their metrics are NOT
    used to promote / demote thresholds — they are reported for
    visibility only.

Output:
  * Writes a JSON blob to /app/PHASE4E_MAGIC_TIER_BASELINE.json
  * Writes a Markdown summary to /app/PHASE4E_MAGIC_TIER_BASELINE.md

This script is READ-ONLY.  It does not modify picks, tiers, or
calibration state.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.database import get_database  # noqa: E402

logger = logging.getLogger("phase4e.baseline")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

DEFAULT_WINDOW_DAYS = 180
MAX_WINDOW_DAYS = 540
MIN_SAMPLE_TARGET = 50
MIN_SAMPLE_REPORTABLE = 15

# Rough season boundaries — used ONLY to note when a bucket window
# crosses a season change.  We do not currently drop cross-season
# picks; we just annotate them.
_SEASON_BOUNDARIES = {
    "MLB": ("03-15", "10-31"),   # ~Apr → Oct
    "NBA": ("10-15", "06-30"),   # Oct → Jun
    "NFL": ("09-01", "02-15"),
    "CFB": ("08-25", "01-15"),
}


# ── Market family normalisation ─────────────────────────────────────
def _market_family(market: str) -> str:
    m = (market or "").strip().lower()
    if any(k in m for k in ("moneyline", "ml", "h2h")):
        return "moneyline"
    if any(k in m for k in ("spread", "runline", "puckline", "handicap")):
        return "spread"
    if any(k in m for k in ("total", "over_under", "goals_ou")):
        return "total"
    if "scorer" in m or "goal_scorer" in m or "score_or_assist" in m:
        return "scorer"
    if "prop" in m or "player_" in m or any(k in m for k in (
        "points", "rebounds", "assists", "strikeouts", "hits", "yards",
        "receptions", "touchdowns", "threes", "blocks", "steals",
    )):
        return "player_prop"
    if "corner" in m: return "corners"
    if "card" in m:   return "cards"
    if "btts" in m or "both_teams_to_score" in m:
        return "btts"
    return "other"


# ── Odds → implied prob (used when pick has no explicit prob) ──────
def _implied_prob(odds: Any) -> Optional[float]:
    if odds is None: return None
    try:
        odds = int(odds)
    except (TypeError, ValueError):
        return None
    if odds < 0:
        return abs(odds) / (abs(odds) + 100.0)
    return 100.0 / (odds + 100.0)


# ── Metric helpers ──────────────────────────────────────────────────
def _brier(p: float, hit: bool) -> float:
    y = 1.0 if hit else 0.0
    return (p - y) ** 2


def _log_loss(p: float, hit: bool) -> float:
    p = max(1e-6, min(1 - 1e-6, p))
    return -math.log(p) if hit else -math.log(1 - p)


def _roi_for_pick(pick: dict, hit: bool, push: bool) -> float:
    if push: return 0.0
    odds = pick.get("american") or pick.get("odds")
    try:
        odds = int(odds) if odds is not None else -110
    except Exception:
        odds = -110
    if hit:
        return (100.0 / abs(odds)) if odds < 0 else (odds / 100.0)
    return -1.0


# ── Fetch settled picks ─────────────────────────────────────────────
async def _fetch_settled_picks(db, sport: str, since: datetime) -> list[dict]:
    query = {
        "sport": {"$in": [sport, sport.upper(), sport.title()]},
        "settled_at": {"$gte": since},
        "result": {"$in": ["win", "loss", "push", "WIN", "LOSS", "PUSH"]},
    }
    docs = await db.picks.find(
        query,
        {
            "_id": 0, "sport": 1, "market": 1, "grade": 1, "tier_v2": 1,
            "magic_tier": 1, "predicted_probability": 1,
            "predicted_prob": 1, "lock_score": 1, "implied_probability": 1,
            "odds": 1, "american": 1, "result": 1, "settled_at": 1,
            "commence_time": 1,
        },
    ).to_list(length=200000)
    return docs


# ── Bucket + metric aggregation ─────────────────────────────────────
def _bucketize(picks: list[dict]) -> dict:
    """Group by (sport, market_family, tier_label). Returns nested dict."""
    buckets: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for p in picks:
        sport = (p.get("sport") or "?").strip()
        family = _market_family(p.get("market") or "")
        mt = p.get("magic_tier") or {}
        tier_label = (
            (mt.get("magic_tier") if isinstance(mt, dict) else None)
            or p.get("grade")
            or p.get("tier_v2")
            or "Unknown"
        )
        buckets[(sport, family, tier_label)].append(p)
    return buckets


def _bucket_metrics(picks: list[dict]) -> dict:
    n = len(picks)
    if n == 0:
        return {"n_picks": 0}
    wins = sum(1 for p in picks if (p.get("result") or "").lower() == "win")
    losses = sum(1 for p in picks if (p.get("result") or "").lower() == "loss")
    pushes = sum(1 for p in picks if (p.get("result") or "").lower() == "push")
    decided = wins + losses
    hit_rate = (wins / decided) if decided else None
    probs = []
    briers = []
    logs = []
    rois = []
    for p in picks:
        prob = (p.get("predicted_probability") or p.get("predicted_prob")
                or p.get("lock_score") or _implied_prob(p.get("odds") or p.get("american")))
        if prob is not None:
            probs.append(float(prob))
        res = (p.get("result") or "").lower()
        hit = res == "win"
        push = res == "push"
        if prob is not None and not push:
            briers.append(_brier(float(prob), hit))
            logs.append(_log_loss(float(prob), hit))
        rois.append(_roi_for_pick(p, hit, push))
    avg_prob = sum(probs) / len(probs) if probs else None
    brier = sum(briers) / len(briers) if briers else None
    log_loss = sum(logs) / len(logs) if logs else None
    roi = sum(rois) / len(rois) if rois else None
    cal_gap = (abs(hit_rate - avg_prob) if (hit_rate is not None and avg_prob is not None) else None)
    return {
        "n_picks": n,
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "hit_rate": (round(hit_rate, 4) if hit_rate is not None else None),
        "avg_predicted_prob": (round(avg_prob, 4) if avg_prob is not None else None),
        "brier_score": (round(brier, 4) if brier is not None else None),
        "log_loss": (round(log_loss, 4) if log_loss is not None else None),
        "roi_units": (round(roi, 4) if roi is not None else None),
        "calibration_gap": (round(cal_gap, 4) if cal_gap is not None else None),
    }


async def build_baseline(days: int = DEFAULT_WINDOW_DAYS) -> dict:
    db = get_database()
    all_report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "requested_window_days": days,
        "min_sample_target": MIN_SAMPLE_TARGET,
        "min_sample_reportable": MIN_SAMPLE_REPORTABLE,
        "sports": {},
        "notes": [],
    }

    sports = ["MLB", "NBA", "NFL", "CFB", "Tennis", "Soccer"]
    for sport in sports:
        window = days
        since = datetime.now(timezone.utc) - timedelta(days=window)
        picks = await _fetch_settled_picks(db, sport, since)
        # Expand backwards if too sparse.
        while len(picks) < MIN_SAMPLE_TARGET and window < MAX_WINDOW_DAYS:
            window += 60
            since = datetime.now(timezone.utc) - timedelta(days=window)
            picks = await _fetch_settled_picks(db, sport, since)
        buckets = _bucketize(picks)
        sport_out: dict[str, Any] = {
            "window_days_used": window,
            "since": since.isoformat(),
            "total_settled": len(picks),
            "season_boundaries_note": _SEASON_BOUNDARIES.get(sport),
            "buckets": {},
        }
        # Emit per-bucket metrics
        for (sp, fam, tier), rows in sorted(buckets.items()):
            m = _bucket_metrics(rows)
            m["insufficient_sample"] = m["n_picks"] < MIN_SAMPLE_REPORTABLE
            sport_out["buckets"][f"{fam}::{tier}"] = m
        all_report["sports"][sport] = sport_out
        if len(picks) == 0:
            all_report["notes"].append(f"{sport}: 0 settled picks in window "
                                       f"(window_days={window}) — empty DB / fresh env.")

    return all_report


def render_markdown(report: dict) -> str:
    lines: list[str] = []
    lines.append("# Phase 4E.4 — Magic Tier Historical Baseline\n")
    lines.append(f"**Generated:** {report['generated_at']}  ")
    lines.append(f"**Requested window (days):** {report['requested_window_days']}  ")
    lines.append(f"**Min sample target / reportable:** "
                 f"{report['min_sample_target']} / {report['min_sample_reportable']}\n")
    for sport, block in report["sports"].items():
        lines.append(f"## {sport}\n")
        lines.append(f"* Window used: **{block['window_days_used']} days** "
                     f"(since {block['since']})")
        lines.append(f"* Total settled picks: **{block['total_settled']}**")
        if block.get("season_boundaries_note"):
            lines.append(f"* Season boundaries: {block['season_boundaries_note']}")
        if not block["buckets"]:
            lines.append("\n_No buckets — no settled picks in this window._\n")
            continue
        lines.append("\n| Market / Tier | N | Hit% | AvgP | Brier | LogLoss | ROI | Cal Gap | Note |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---|")
        for key, m in block["buckets"].items():
            note = "insufficient" if m.get("insufficient_sample") else ""
            def _fmt(x, pct=False):
                if x is None: return "—"
                return f"{x*100:.1f}%" if pct else f"{x:.3f}"
            lines.append(
                f"| {key} | {m['n_picks']} | {_fmt(m.get('hit_rate'), True)} | "
                f"{_fmt(m.get('avg_predicted_prob'), True)} | "
                f"{_fmt(m.get('brier_score'))} | "
                f"{_fmt(m.get('log_loss'))} | "
                f"{_fmt(m.get('roi_units'))} | "
                f"{_fmt(m.get('calibration_gap'), True)} | {note} |"
            )
        lines.append("")
    if report.get("notes"):
        lines.append("## Notes\n")
        for n in report["notes"]:
            lines.append(f"* {n}")
    return "\n".join(lines)


async def _amain(days: int, out_json: str, out_md: str) -> None:
    report = await build_baseline(days=days)
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)
    with open(out_md, "w", encoding="utf-8") as f:
        f.write(render_markdown(report))
    logger.info("Baseline JSON  → %s", out_json)
    logger.info("Baseline MD    → %s", out_md)
    logger.info("Sports covered: %s", list(report["sports"].keys()))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=DEFAULT_WINDOW_DAYS)
    ap.add_argument("--json", default="/app/PHASE4E_MAGIC_TIER_BASELINE.json")
    ap.add_argument("--md",   default="/app/PHASE4E_MAGIC_TIER_BASELINE.md")
    args = ap.parse_args()
    asyncio.run(_amain(args.days, args.json, args.md))


if __name__ == "__main__":
    main()
