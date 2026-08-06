"""Phase 4B — Calibration + Simulator BASELINE Report (read-only).

Runs against the live production database using ONLY read operations.
Writes zero rows anywhere.  Emits:

  • /app/PHASE4B_CALIBRATION_BASELINE.md
  • /app/PHASE4B_SIMULATOR_BASELINE.json

Segments by:
  sport, market_family, side, main/alt, line_band, odds_band,
  Magic-Tier (composite), confidence/Lock-Score band, simulator-used
  vs not-used, sample size.

Metrics per bucket:
  n_settled, wins, losses, pushes, voids, hit_rate, avg_pred_prob,
  avg_odds, Brier, log_loss, ROI %, units, CLV mean (where captured),
  calibration_gap (avg_pred - avg_outcome).

Insufficient-sample buckets are labelled ``insufficient`` rather than
reporting unstable metrics.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.calibration_segmentation import (          # noqa: E402
    build_bucket_key, classify_line_band, classify_odds_band,
    CALIBRATION_SEGMENTATION_VERSION,
)

# ── Config ──────────────────────────────────────────────────────────
MIN_SAMPLE_FOR_METRICS = 30           # <30 → labelled insufficient
INSUFFICIENT_LABEL     = "INSUFFICIENT_SAMPLE"
OUT_DIR                = "/app"
MD_PATH                = os.path.join(OUT_DIR, "PHASE4B_CALIBRATION_BASELINE.md")
JSON_PATH              = os.path.join(OUT_DIR, "PHASE4B_SIMULATOR_BASELINE.json")


# ── Metric helpers ──────────────────────────────────────────────────
def _american_to_decimal(american):
    try:
        a = float(american)
    except (TypeError, ValueError):
        return None
    return 1.0 + a / 100.0 if a >= 0 else 1.0 + 100.0 / abs(a)


def _implied_prob(american):
    try:
        a = float(american)
    except (TypeError, ValueError):
        return None
    return 100.0 / (a + 100.0) if a >= 0 else abs(a) / (abs(a) + 100.0)


def _brier(p: float, y: int) -> float:
    return (p - y) ** 2


def _log_loss_pt(p: float, y: int) -> float:
    p = max(1e-6, min(1 - 1e-6, p))
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


def _win_units(american, stake=1.0):
    dec = _american_to_decimal(american)
    return (dec - 1.0) * stake if dec is not None else 0.0


def _clv_delta(pick):
    # Prefer explicit closing-line-value fields, else compute from
    # closing odds if present.
    for k in ("clv_delta", "closing_line_delta", "clv_pp"):
        v = pick.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


# ── Bucket accumulator ──────────────────────────────────────────────
def _empty_bucket():
    return {
        "n": 0, "won": 0, "lost": 0, "push": 0, "void": 0,
        "sum_pred": 0.0, "sum_outcome_bin": 0.0,
        "sum_brier": 0.0, "sum_log_loss": 0.0,
        "sum_units": 0.0, "sum_risked": 0.0,
        "sum_odds": 0.0, "n_odds": 0,
        "sum_clv": 0.0, "n_clv": 0,
    }


def _finalise(b):
    if b["n"] < MIN_SAMPLE_FOR_METRICS:
        return {
            "n": b["n"], "won": b["won"], "lost": b["lost"],
            "push": b["push"], "void": b["void"],
            "status": INSUFFICIENT_LABEL,
        }
    settled = b["won"] + b["lost"]
    if settled == 0:
        return {"n": b["n"], "won": 0, "lost": 0, "push": b["push"],
                 "void": b["void"], "status": "no_settled"}
    hit_rate = b["won"] / settled
    avg_pred = b["sum_pred"] / settled if settled else 0.0
    avg_odds = b["sum_odds"] / b["n_odds"] if b["n_odds"] else None
    brier = b["sum_brier"] / settled
    log_loss = b["sum_log_loss"] / settled
    roi_pct = (b["sum_units"] / b["sum_risked"] * 100.0
                if b["sum_risked"] else 0.0)
    clv = (b["sum_clv"] / b["n_clv"]) if b["n_clv"] else None
    gap = round(avg_pred - hit_rate, 4)
    return {
        "n": b["n"], "won": b["won"], "lost": b["lost"],
        "push": b["push"], "void": b["void"],
        "hit_rate": round(hit_rate, 4),
        "avg_pred_prob": round(avg_pred, 4),
        "avg_odds": round(avg_odds, 1) if avg_odds is not None else None,
        "brier": round(brier, 5),
        "log_loss": round(log_loss, 5),
        "roi_pct": round(roi_pct, 2),
        "units": round(b["sum_units"], 3),
        "risked": round(b["sum_risked"], 3),
        "clv_mean_pp": round(clv, 3) if clv is not None else None,
        "n_clv": b["n_clv"],
        "calibration_gap": gap,
    }


# ── Segmentation ────────────────────────────────────────────────────
def _lock_band(lock):
    try:
        l = float(lock)
    except (TypeError, ValueError):
        return "unknown"
    if l >= 95: return "elite_95+"
    if l >= 88: return "strong_88-94"
    if l >= 80: return "playable_80-87"
    if l >= 74: return "watchlist_74-79"
    if l >= 65: return "warmup_65-73"
    return "below_65"


def _magic_band(comp):
    if comp is None:
        return "no_tier"
    try:
        c = float(comp)
    except (TypeError, ValueError):
        return "no_tier"
    if c >= 0.80: return "T1_very_high"
    if c >= 0.60: return "T2_high"
    if c >= 0.40: return "T3_medium"
    if c >= 0.20: return "T4_low"
    return "T5_very_low"


def _sim_used(pick):
    b = pick.get("brain") or {}
    return bool(b.get("simulator")) or bool(pick.get("sim_win_probability"))


def _is_alt(pick):
    mk = (pick.get("market_key") or "").lower()
    return "_alternate" in mk or bool(pick.get("is_alt"))


def _market_family(pick):
    # Prefer canonical field, else best-effort from market_key.
    fam = ((pick.get("selection_v2") or {}).get("market") or {}).get("family")
    if fam:
        return fam
    return pick.get("market_family") or (pick.get("market_key") or "").split("_")[0] or "other"


def _side(pick):
    return (pick.get("side") or pick.get("direction")
             or ((pick.get("selection_v2") or {}).get("side"))
             or "unknown")


# ── Main runner ─────────────────────────────────────────────────────
async def run_baseline():
    t0 = time.monotonic()
    from motor.motor_asyncio import AsyncIOMotorClient  # noqa: F401
    import deps
    db = deps.db

    # Global + segmented buckets.
    axis_bkts = {
        "by_sport":            defaultdict(_empty_bucket),
        "by_sport_market":     defaultdict(_empty_bucket),
        "by_sport_market_side": defaultdict(_empty_bucket),
        "by_sport_market_side_line": defaultdict(_empty_bucket),
        "by_sport_market_side_odds": defaultdict(_empty_bucket),
        "by_main_alt":         defaultdict(_empty_bucket),
        "by_lock_band":        defaultdict(_empty_bucket),
        "by_magic_band":       defaultdict(_empty_bucket),
        "by_sim_used":         defaultdict(_empty_bucket),
    }
    global_bkt = _empty_bucket()

    query = {"status": {"$in": ["won", "lost", "push", "void"]}}
    projection = {
        "_id": 0, "sport": 1, "market_key": 1, "market_family": 1,
        "selection_v2": 1, "side": 1, "line": 1, "point": 1,
        "book_odds": 1, "odds_at_bet": 1, "status": 1,
        "lock_score": 1, "win_probability": 1, "confidence": 1,
        "brain": 1, "sim_win_probability": 1, "created_at": 1,
        "clv_delta": 1, "closing_line_delta": 1, "clv_pp": 1,
        "magic_composite": 1,
    }
    scanned = 0
    scored = 0
    async for pick in db.picks.find(query, projection):
        scanned += 1
        status = pick.get("status")
        if status not in ("won", "lost", "push", "void"):
            continue

        american = pick.get("book_odds") or pick.get("odds_at_bet")
        wp = pick.get("win_probability")
        try:
            pred_prob = float(wp) / 100.0 if wp is not None else _implied_prob(american)
        except (TypeError, ValueError):
            pred_prob = _implied_prob(american)
        if pred_prob is None:
            continue

        outcome_bin = 1 if status == "won" else 0
        if status in ("won", "lost"):
            brier = _brier(pred_prob, outcome_bin)
            log_loss = _log_loss_pt(pred_prob, outcome_bin)
            risked = 1.0
            if outcome_bin == 1:
                units = _win_units(american, stake=1.0)
            else:
                units = -1.0
        else:
            brier = 0.0
            log_loss = 0.0
            risked = 0.0
            units = 0.0

        clv = _clv_delta(pick)

        def _add(bkt):
            bkt["n"] += 1
            if status == "won":  bkt["won"] += 1
            if status == "lost": bkt["lost"] += 1
            if status == "push": bkt["push"] += 1
            if status == "void": bkt["void"] += 1
            if status in ("won", "lost"):
                bkt["sum_pred"] += pred_prob
                bkt["sum_outcome_bin"] += outcome_bin
                bkt["sum_brier"] += brier
                bkt["sum_log_loss"] += log_loss
                bkt["sum_units"] += units
                bkt["sum_risked"] += risked
                if american is not None:
                    try:
                        bkt["sum_odds"] += float(american)
                        bkt["n_odds"] += 1
                    except (TypeError, ValueError):
                        pass
                if clv is not None:
                    bkt["sum_clv"] += clv
                    bkt["n_clv"] += 1

        _add(global_bkt)

        sport = pick.get("sport") or "unknown"
        family = _market_family(pick)
        side = _side(pick)
        line = pick.get("line") or pick.get("point")
        odds_band = classify_odds_band(american)
        line_band = classify_line_band(line)
        is_alt = _is_alt(pick)
        lock_band = _lock_band(pick.get("lock_score"))
        magic_band = _magic_band(pick.get("magic_composite"))
        sim_used = _sim_used(pick)

        _add(axis_bkts["by_sport"][sport])
        _add(axis_bkts["by_sport_market"][f"{sport}|{family}"])
        _add(axis_bkts["by_sport_market_side"][f"{sport}|{family}|{side}"])
        _add(axis_bkts["by_sport_market_side_line"][
              f"{sport}|{family}|{side}|{line_band}"])
        _add(axis_bkts["by_sport_market_side_odds"][
              f"{sport}|{family}|{side}|{odds_band}"])
        _add(axis_bkts["by_main_alt"][f"{sport}|{family}|{'alt' if is_alt else 'main'}"])
        _add(axis_bkts["by_lock_band"][f"{sport}|{lock_band}"])
        _add(axis_bkts["by_magic_band"][f"{sport}|{magic_band}"])
        _add(axis_bkts["by_sim_used"][
              f"{sport}|{'sim_used' if sim_used else 'no_sim'}"])
        scored += 1

    # Finalise.
    report = {
        "version":            CALIBRATION_SEGMENTATION_VERSION,
        "generated_at":       datetime.now(timezone.utc).isoformat(),
        "scanned_picks":      scanned,
        "scored_picks":       scored,
        "min_sample_for_metrics": MIN_SAMPLE_FOR_METRICS,
        "global":             _finalise(global_bkt),
        "axes":               {},
    }
    for axis_name, buckets in axis_bkts.items():
        report["axes"][axis_name] = {
            key: _finalise(b) for key, b in sorted(buckets.items())
        }
    elapsed_s = round(time.monotonic() - t0, 2)
    report["elapsed_s"] = elapsed_s

    # Write JSON.
    with open(JSON_PATH, "w", encoding="utf-8") as fp:
        json.dump(report, fp, indent=2, default=str)
    # Write human-readable MD summary.
    _write_md(report)
    print(f"Phase 4B baseline written to {MD_PATH} + {JSON_PATH} "
          f"({scanned} scanned, {scored} scored, {elapsed_s}s)")


def _write_md(report: dict) -> None:
    lines: list[str] = []
    lines.append("# Phase 4B — Calibration Baseline (READ-ONLY)")
    lines.append("")
    lines.append(f"**Generated:** `{report['generated_at']}`")
    lines.append(f"**Segmentation version:** `{report['version']}`")
    lines.append(f"**Scanned picks:** {report['scanned_picks']:,}")
    lines.append(f"**Scored picks (post-filter):** {report['scored_picks']:,}")
    lines.append(f"**Elapsed:** {report['elapsed_s']}s")
    lines.append(f"**Min sample for metrics:** {report['min_sample_for_metrics']}")
    lines.append("")
    lines.append("Buckets below the min-sample threshold are marked "
                 f"`{INSUFFICIENT_LABEL}` — their raw counts are shown but "
                 "no ROI / Brier / log-loss / calibration metrics.")
    lines.append("")

    def _row(label, m):
        if m.get("status") == INSUFFICIENT_LABEL:
            return (f"| {label} | {m['n']} | {m['won']} | {m['lost']} | "
                     f"{m['push']} | {m['void']} | — | — | — | — | — | "
                     f"— | — | — | INSUFFICIENT |")
        return (f"| {label} | {m['n']} | {m['won']} | {m['lost']} | "
                 f"{m['push']} | {m['void']} | {m.get('hit_rate','-')} | "
                 f"{m.get('avg_pred_prob','-')} | "
                 f"{m.get('avg_odds','-')} | "
                 f"{m.get('brier','-')} | {m.get('log_loss','-')} | "
                 f"{m.get('roi_pct','-')}% | {m.get('units','-')} | "
                 f"{m.get('clv_mean_pp','-')} | "
                 f"{m.get('calibration_gap','-')} |")

    header = ("| Bucket | n | W | L | P | V | HitRate | AvgPred | AvgOdds "
              "| Brier | LogLoss | ROI | Units | CLV | CalGap |")
    sep = "|--|--|--|--|--|--|--|--|--|--|--|--|--|--|--|"

    lines.append("## Global")
    lines.append(header); lines.append(sep)
    lines.append(_row("global", report["global"]))
    lines.append("")

    for axis, buckets in report["axes"].items():
        lines.append(f"## {axis}")
        lines.append(header); lines.append(sep)
        for k, m in buckets.items():
            lines.append(_row(k, m))
        lines.append("")

    lines.append("---")
    lines.append("**Zero production writes performed.**  This report was "
                 "generated by `scripts/phase4b_calibration_baseline.py` in "
                 "read-only mode.")
    with open(MD_PATH, "w", encoding="utf-8") as fp:
        fp.write("\n".join(lines))


if __name__ == "__main__":
    asyncio.run(run_baseline())
