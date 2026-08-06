"""Phase 4E.5 — Cross-sport calibration segmented report.

For each (sport, market_family) bucket with sufficient sample size,
report out-of-sample calibration metrics comparing:

    raw           — model's predicted_probability with no post-cal
    calibrated    — model's predicted_probability after the current
                    production calibrator was applied

Metrics per bucket:
    n_train, n_holdout,
    raw_brier, raw_log_loss, raw_calibration_gap, raw_roi,
    cal_brier, cal_log_loss, cal_calibration_gap, cal_roi,
    delta_brier, delta_log_loss, delta_roi,
    recommendation: "promote" | "keep_fallback" | "insufficient_sample"

Uses the Phase 4B segmentation policy from
``services.calibration_segmentation`` (fallback hierarchy L1→L6, min
sample thresholds).  When a bucket does not reach L1 sample it is
labeled insufficient and its metrics are reported for visibility only.

READ-ONLY.  Does not promote calibrators or mutate collections.
Output: ``PHASE4E_CROSS_SPORT_CALIBRATION.{json,md}`` at repo root.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import random
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.database import get_database                      # noqa: E402
from services.calibration_segmentation import (                 # noqa: E402
    DEFAULT_MIN_SAMPLE, CALIBRATION_SEGMENTATION_VERSION,
)
from scripts.phase4e_magic_tier_baseline import (               # noqa: E402
    _market_family, _implied_prob, _brier, _log_loss, _roi_for_pick,
)

logger = logging.getLogger("phase4e.calibration")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

DEFAULT_WINDOW_DAYS = 180
MAX_WINDOW_DAYS = 540
MIN_SAMPLE_L4 = DEFAULT_MIN_SAMPLE["L4"]     # 40 — our minimum reportable
HOLDOUT_FRACTION = 0.20
SEED = 4315


# ── Data extraction ─────────────────────────────────────────────────
async def _fetch_settled(db, sport: str, since: datetime) -> list[dict]:
    q = {
        "sport": {"$in": [sport, sport.upper(), sport.title()]},
        "settled_at": {"$gte": since},
        "result": {"$in": ["win", "loss", "push", "WIN", "LOSS", "PUSH"]},
    }
    return await db.picks.find(
        q,
        {
            "_id": 0, "sport": 1, "market": 1, "result": 1,
            "predicted_probability": 1, "predicted_prob": 1,
            "calibrated_probability": 1, "lock_calibration": 1,
            "odds": 1, "american": 1, "settled_at": 1,
        },
    ).to_list(length=500000)


def _p_raw(p: dict) -> Optional[float]:
    v = (p.get("predicted_probability") or p.get("predicted_prob")
         or _implied_prob(p.get("odds") or p.get("american")))
    return float(v) if v is not None else None


def _p_cal(p: dict) -> Optional[float]:
    v = p.get("calibrated_probability")
    if v is None:
        lc = p.get("lock_calibration") or {}
        v = lc.get("calibrated_probability")
    return float(v) if isinstance(v, (int, float)) else None


def _bucket_metrics(rows: list[dict], mode: str) -> dict:
    briers = []
    logs = []
    rois = []
    probs = []
    hits = []
    for p in rows:
        prob = _p_raw(p) if mode == "raw" else _p_cal(p)
        if prob is None:
            continue
        res = (p.get("result") or "").lower()
        hit = res == "win"
        push = res == "push"
        if push:
            rois.append(0.0)
            continue
        briers.append(_brier(prob, hit))
        logs.append(_log_loss(prob, hit))
        rois.append(_roi_for_pick(p, hit, push))
        probs.append(prob)
        hits.append(1 if hit else 0)
    n = len(briers)
    if n == 0:
        return {"n": 0}
    avg_p = sum(probs) / len(probs) if probs else None
    hit_rate = sum(hits) / len(hits) if hits else None
    cal_gap = abs(hit_rate - avg_p) if (hit_rate is not None and avg_p is not None) else None
    return {
        "n":               n,
        "brier":           round(sum(briers) / n, 4),
        "log_loss":        round(sum(logs) / n, 4),
        "roi":             round(sum(rois) / len(rois), 4) if rois else None,
        "hit_rate":        round(hit_rate, 4) if hit_rate is not None else None,
        "avg_prob":        round(avg_p, 4) if avg_p is not None else None,
        "calibration_gap": round(cal_gap, 4) if cal_gap is not None else None,
    }


def _split_train_holdout(rows: list[dict]) -> tuple[list, list]:
    rng = random.Random(SEED)
    shuffled = rows[:]
    rng.shuffle(shuffled)
    cut = int(len(shuffled) * (1 - HOLDOUT_FRACTION))
    return shuffled[:cut], shuffled[cut:]


async def build_report(days: int = DEFAULT_WINDOW_DAYS) -> dict:
    db = get_database()
    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "requested_window_days": days,
        "segmentation_version": CALIBRATION_SEGMENTATION_VERSION,
        "min_sample_reportable": MIN_SAMPLE_L4,
        "holdout_fraction": HOLDOUT_FRACTION,
        "sports": {},
        "notes": [],
    }
    sports = ["MLB", "NBA", "NFL", "CFB", "Tennis", "Soccer"]
    for sport in sports:
        window = days
        since = datetime.now(timezone.utc) - timedelta(days=window)
        picks = await _fetch_settled(db, sport, since)
        while len(picks) < MIN_SAMPLE_L4 and window < MAX_WINDOW_DAYS:
            window += 60
            since = datetime.now(timezone.utc) - timedelta(days=window)
            picks = await _fetch_settled(db, sport, since)
        by_fam: dict[str, list[dict]] = defaultdict(list)
        for p in picks:
            by_fam[_market_family(p.get("market") or "")].append(p)

        sport_out = {
            "window_days_used": window,
            "since": since.isoformat(),
            "total_settled": len(picks),
            "families": {},
        }

        for fam, rows in sorted(by_fam.items()):
            if len(rows) < MIN_SAMPLE_L4:
                sport_out["families"][fam] = {
                    "n_total": len(rows),
                    "recommendation": "insufficient_sample",
                    "notes": [f"n<{MIN_SAMPLE_L4}, keep fallback"],
                }
                continue
            train, holdout = _split_train_holdout(rows)
            raw_hold = _bucket_metrics(holdout, "raw")
            cal_hold = _bucket_metrics(holdout, "calibrated")
            delta = {}
            recommendation = "insufficient_calibration_data"
            if cal_hold.get("n", 0) > 0:
                for k in ("brier", "log_loss", "roi", "calibration_gap"):
                    if raw_hold.get(k) is not None and cal_hold.get(k) is not None:
                        delta[k] = round(cal_hold[k] - raw_hold[k], 4)
                # Promote only if BOTH Brier and log-loss improved.
                brier_ok = (delta.get("brier") is not None and delta["brier"] <= 0)
                log_ok = (delta.get("log_loss") is not None and delta["log_loss"] <= 0)
                recommendation = "promote" if (brier_ok and log_ok) else "keep_fallback"

            sport_out["families"][fam] = {
                "n_total":        len(rows),
                "n_train":        len(train),
                "n_holdout":      len(holdout),
                "raw":            raw_hold,
                "calibrated":     cal_hold,
                "delta":          delta,
                "recommendation": recommendation,
            }
        report["sports"][sport] = sport_out
        if len(picks) == 0:
            report["notes"].append(f"{sport}: 0 settled picks in window "
                                    f"(window_days={window}) — empty DB.")
    return report


def render_markdown(report: dict) -> str:
    ls = []
    ls.append("# Phase 4E.5 — Cross-Sport Calibration Report\n")
    ls.append(f"**Generated:** {report['generated_at']}  ")
    ls.append(f"**Segmentation policy version:** {report['segmentation_version']}  ")
    ls.append(f"**Min sample reportable:** {report['min_sample_reportable']}  ")
    ls.append(f"**Holdout fraction:** {int(report['holdout_fraction']*100)}%\n")
    for sport, block in report["sports"].items():
        ls.append(f"## {sport}\n")
        ls.append(f"* Window: **{block['window_days_used']}d** — {block['total_settled']} settled picks")
        if not block.get("families"):
            ls.append("\n_No data._\n")
            continue
        ls.append("\n| Family | N | Raw Brier | Cal Brier | ΔBrier | ΔLogLoss | ΔROI | Recommendation |")
        ls.append("|---|---:|---:|---:|---:|---:|---:|---|")
        for fam, m in block["families"].items():
            if m.get("recommendation") == "insufficient_sample":
                ls.append(f"| {fam} | {m['n_total']} | — | — | — | — | — | insufficient |")
                continue
            r = m.get("raw", {})
            c = m.get("calibrated", {})
            d = m.get("delta", {})
            def _f(x): return "—" if x is None else f"{x:.3f}"
            ls.append(
                f"| {fam} | {m['n_total']} | {_f(r.get('brier'))} | "
                f"{_f(c.get('brier'))} | {_f(d.get('brier'))} | "
                f"{_f(d.get('log_loss'))} | {_f(d.get('roi'))} | "
                f"{m['recommendation']} |"
            )
        ls.append("")
    if report.get("notes"):
        ls.append("## Notes\n")
        for n in report["notes"]:
            ls.append(f"* {n}")
    return "\n".join(ls)


async def _amain(days, out_json, out_md):
    report = await build_report(days=days)
    with open(out_json, "w") as f: json.dump(report, f, indent=2, default=str)
    with open(out_md, "w") as f:   f.write(render_markdown(report))
    logger.info("Calibration JSON → %s", out_json)
    logger.info("Calibration MD   → %s", out_md)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=DEFAULT_WINDOW_DAYS)
    ap.add_argument("--json", default="/app/PHASE4E_CROSS_SPORT_CALIBRATION.json")
    ap.add_argument("--md",   default="/app/PHASE4E_CROSS_SPORT_CALIBRATION.md")
    args = ap.parse_args()
    asyncio.run(_amain(args.days, args.json, args.md))


if __name__ == "__main__":
    main()
