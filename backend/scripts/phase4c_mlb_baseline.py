"""Phase 4C — MLB Baseline (read-only, zero writes).

Builds on the Phase 4B baseline reporter, filtered to MLB and adding
MLB-specific axes: lineup-confirmed vs projected, data-quality band.

Emits:
  /app/PHASE4C_MLB_BASELINE.md
  /app/PHASE4C_MLB_BASELINE.json
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Reuse phase4b helpers.
from scripts.phase4b_calibration_baseline import (       # noqa: E402
    _american_to_decimal, _implied_prob, _brier, _log_loss_pt,
    _win_units, _clv_delta, _empty_bucket, _finalise,
    _lock_band, _magic_band, _sim_used, _is_alt, _side, _market_family,
    MIN_SAMPLE_FOR_METRICS, INSUFFICIENT_LABEL,
)
from services.calibration_segmentation import (          # noqa: E402
    classify_odds_band, classify_line_band,
    CALIBRATION_SEGMENTATION_VERSION,
)


OUT_DIR   = "/app"
MD_PATH   = os.path.join(OUT_DIR, "PHASE4C_MLB_BASELINE.md")
JSON_PATH = os.path.join(OUT_DIR, "PHASE4C_MLB_BASELINE.json")


def _lineup_status(pick):
    if pick.get("lineup_confirmed") is True:
        return "confirmed"
    if pick.get("lineup_confirmed") is False:
        return "projected"
    return "unknown"


def _dq_band(pick):
    dq = ((pick.get("brain") or {}).get("candidate_components") or {}).get("data")
    if dq is None:
        return "unknown"
    try:
        d = float(dq)
    except (TypeError, ValueError):
        return "unknown"
    if d >= 0.85: return "high_dq"
    if d >= 0.60: return "medium_dq"
    if d >= 0.30: return "low_dq"
    return "very_low_dq"


async def run_baseline():
    t0 = time.monotonic()
    import deps
    db = deps.db

    axis_bkts = {
        "by_market_family":     defaultdict(_empty_bucket),
        "by_market_side":       defaultdict(_empty_bucket),
        "by_market_side_line":  defaultdict(_empty_bucket),
        "by_market_side_odds":  defaultdict(_empty_bucket),
        "by_main_alt":          defaultdict(_empty_bucket),
        "by_lock_band":         defaultdict(_empty_bucket),
        "by_magic_band":        defaultdict(_empty_bucket),
        "by_sim_used":          defaultdict(_empty_bucket),
        "by_lineup_status":     defaultdict(_empty_bucket),
        "by_data_quality_band": defaultdict(_empty_bucket),
    }
    global_bkt = _empty_bucket()

    projection = {
        "_id": 0, "sport": 1, "market_key": 1, "market_family": 1,
        "selection_v2": 1, "side": 1, "line": 1, "point": 1,
        "book_odds": 1, "odds_at_bet": 1, "status": 1,
        "lock_score": 1, "win_probability": 1, "confidence": 1,
        "brain": 1, "sim_win_probability": 1, "created_at": 1,
        "clv_delta": 1, "closing_line_delta": 1, "clv_pp": 1,
        "magic_composite": 1, "lineup_confirmed": 1,
    }
    query = {"sport": "MLB",
              "status": {"$in": ["won", "lost", "push", "void"]}}
    scanned = 0
    scored = 0
    async for pick in db.picks.find(query, projection):
        scanned += 1
        status = pick.get("status")
        american = pick.get("book_odds") or pick.get("odds_at_bet")
        wp = pick.get("win_probability")
        try:
            pred = float(wp) / 100.0 if wp is not None else _implied_prob(american)
        except (TypeError, ValueError):
            pred = _implied_prob(american)
        if pred is None:
            continue

        outcome = 1 if status == "won" else 0
        if status in ("won", "lost"):
            brier = _brier(pred, outcome)
            log_loss = _log_loss_pt(pred, outcome)
            risked = 1.0
            units = _win_units(american, stake=1.0) if outcome == 1 else -1.0
        else:
            brier = log_loss = risked = units = 0.0

        clv = _clv_delta(pick)

        def _add(bkt):
            bkt["n"] += 1
            if status == "won":  bkt["won"] += 1
            if status == "lost": bkt["lost"] += 1
            if status == "push": bkt["push"] += 1
            if status == "void": bkt["void"] += 1
            if status in ("won", "lost"):
                bkt["sum_pred"] += pred
                bkt["sum_outcome_bin"] += outcome
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

        fam = _market_family(pick)
        side = _side(pick)
        line = pick.get("line") or pick.get("point")
        ob = classify_odds_band(american)
        lb = classify_line_band(line)
        is_alt = _is_alt(pick)
        lockb = _lock_band(pick.get("lock_score"))
        magicb = _magic_band(pick.get("magic_composite"))
        sim = _sim_used(pick)
        lstatus = _lineup_status(pick)
        dqb = _dq_band(pick)

        _add(axis_bkts["by_market_family"][fam])
        _add(axis_bkts["by_market_side"][f"{fam}|{side}"])
        _add(axis_bkts["by_market_side_line"][f"{fam}|{side}|{lb}"])
        _add(axis_bkts["by_market_side_odds"][f"{fam}|{side}|{ob}"])
        _add(axis_bkts["by_main_alt"][f"{fam}|{'alt' if is_alt else 'main'}"])
        _add(axis_bkts["by_lock_band"][lockb])
        _add(axis_bkts["by_magic_band"][magicb])
        _add(axis_bkts["by_sim_used"]["sim_used" if sim else "no_sim"])
        _add(axis_bkts["by_lineup_status"][lstatus])
        _add(axis_bkts["by_data_quality_band"][dqb])
        scored += 1

    report = {
        "version":            f"4C.mlb.{CALIBRATION_SEGMENTATION_VERSION}",
        "generated_at":       datetime.now(timezone.utc).isoformat(),
        "sport":              "MLB",
        "scanned_picks":      scanned,
        "scored_picks":       scored,
        "min_sample_for_metrics": MIN_SAMPLE_FOR_METRICS,
        "global":             _finalise(global_bkt),
        "axes":               {},
    }
    for name, buckets in axis_bkts.items():
        report["axes"][name] = {k: _finalise(b) for k, b in sorted(buckets.items())}
    report["elapsed_s"] = round(time.monotonic() - t0, 2)

    with open(JSON_PATH, "w", encoding="utf-8") as fp:
        json.dump(report, fp, indent=2, default=str)

    # Human-readable MD.
    lines = [
        "# Phase 4C — MLB Baseline (READ-ONLY)", "",
        f"**Generated:** `{report['generated_at']}`",
        f"**Sport:** MLB",
        f"**Version:** `{report['version']}`",
        f"**Scanned:** {report['scanned_picks']:,}",
        f"**Scored:** {report['scored_picks']:,}",
        f"**Elapsed:** {report['elapsed_s']}s",
        f"**Min sample for metrics:** {report['min_sample_for_metrics']}",
        "",
        f"Buckets below the min-sample threshold are marked `{INSUFFICIENT_LABEL}`.",
        "",
    ]

    def _row(label, m):
        if m.get("status") == INSUFFICIENT_LABEL:
            return (f"| {label} | {m['n']} | {m['won']} | {m['lost']} | "
                     f"{m['push']} | {m['void']} | — | — | — | — | — | — | "
                     f"— | — | INSUFFICIENT |")
        return (f"| {label} | {m['n']} | {m['won']} | {m['lost']} | "
                 f"{m['push']} | {m['void']} | {m.get('hit_rate','-')} | "
                 f"{m.get('avg_pred_prob','-')} | {m.get('avg_odds','-')} | "
                 f"{m.get('brier','-')} | {m.get('log_loss','-')} | "
                 f"{m.get('roi_pct','-')}% | {m.get('units','-')} | "
                 f"{m.get('clv_mean_pp','-')} | "
                 f"{m.get('calibration_gap','-')} |")

    header = ("| Bucket | n | W | L | P | V | HitRate | AvgPred | AvgOdds | "
              "Brier | LogLoss | ROI | Units | CLV | CalGap |")
    sep = "|--|--|--|--|--|--|--|--|--|--|--|--|--|--|--|"

    lines += ["## Global", header, sep, _row("global", report["global"]), ""]
    for axis, buckets in report["axes"].items():
        lines += [f"## {axis}", header, sep]
        for k, m in buckets.items():
            lines.append(_row(k, m))
        lines.append("")
    lines += ["---", "**Zero production writes performed.** Read-only baseline."]
    with open(MD_PATH, "w", encoding="utf-8") as fp:
        fp.write("\n".join(lines))

    print(f"Phase 4C MLB baseline written to {MD_PATH} + {JSON_PATH} "
          f"({scanned} scanned, {scored} scored, {report['elapsed_s']}s)")


if __name__ == "__main__":
    asyncio.run(run_baseline())
