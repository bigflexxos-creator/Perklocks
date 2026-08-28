"""Walk-Forward Validation + False-Discovery Control — §6.

Strategy Lab 10X requires proper temporal separation and multiple-
testing control before promoting a SHADOW signal to VALIDATED.

Implementation:
  * Chronologically split settled picks by two cutoff dates:
        - train: rows.date < validation_start
        - validation: validation_start <= date < test_start
        - test: date >= test_start
  * Pregame-only feature enforcement: only fields frozen at pick time
    (win_probability, book_odds, lock_score, market, sport, player_name)
    may be used — settled outcome is only the label.
  * Duplicate-event clustering: cap per-event weight at 1 to avoid a
    hot game inflating a bucket.
  * Multiple testing: Benjamini-Hochberg (BH) FDR at q=0.10 across the
    tested hypotheses in a single run.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from deps import db


def _z(p: float, n: int) -> tuple[float, float]:
    if n <= 0: return (0.0, 0.0)
    z = 1.96
    denom = 1 + (z * z) / n
    center = p + (z * z) / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + (z * z) / (4 * n * n))
    lo = max(0.0, (center - margin) / denom)
    hi = min(1.0, (center + margin) / denom)
    return (lo, hi)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _p_value_two_prop(p1: float, n1: int, p0: float, n0: int) -> float:
    """One-sided z-test for difference in two proportions."""
    if n1 <= 0 or n0 <= 0: return 1.0
    p_pool = (p1 * n1 + p0 * n0) / (n1 + n0)
    se = math.sqrt(p_pool * (1 - p_pool) * (1 / n1 + 1 / n0)) or 1e-9
    z = (p1 - p0) / se
    # one-sided upper tail
    return 1 - _norm_cdf(z)


def bh_fdr(pvals: list[float], q: float = 0.10) -> list[bool]:
    """Benjamini-Hochberg FDR at level q. Returns bool list — True means
    reject the null (signal survives) at threshold q."""
    m = len(pvals)
    if m == 0: return []
    order = sorted(range(m), key=lambda i: pvals[i])
    accept = [False] * m
    threshold_k = -1
    for rank, idx in enumerate(order, start=1):
        thresh = (rank / m) * q
        if pvals[idx] <= thresh:
            threshold_k = rank
    if threshold_k >= 0:
        for rank in range(threshold_k):
            accept[order[rank]] = True
    return accept


async def walk_forward_validate(
    sport: str,
    validation_start: str,   # ISO date
    test_start: str,          # ISO date
    min_events: int = 40,
    q_fdr: float = 0.10,
) -> dict[str, Any]:
    """Chronological three-way split and BH-FDR across market×odds buckets.

    Returns per-bucket rows with train/validation/test hit rates + BH
    survival flag. Pure read over `db.picks` — never writes settlement.
    """
    try:
        cursor = db.picks.find(
            {"sport": sport, "status": {"$in": ["won", "lost"]}},
            {"_id": 0, "market": 1, "status": 1, "book_odds": 1,
             "settled_at": 1, "pick_date": 1, "event_id": 1,
             "player_name": 1, "win_probability": 1},
        ).limit(80000)
        rows = await cursor.to_list(length=80000)
    except Exception:
        rows = []
    if not rows:
        return {"available": False, "reason": "no_settled"}

    def _dtkey(r: dict) -> str:
        return r.get("settled_at") or r.get("pick_date") or ""

    train: dict[str, dict] = {}
    val: dict[str, dict] = {}
    test: dict[str, dict] = {}
    seen_events: dict[str, set] = {"train": set(), "val": set(), "test": set()}

    for r in rows:
        d = _dtkey(r)
        if not d: continue
        if d < validation_start: slot, sm = train, "train"
        elif d < test_start:    slot, sm = val, "val"
        else:                   slot, sm = test, "test"
        m = (r.get("market") or "?").split(" - ")[0].strip()
        odds = r.get("book_odds")
        try: o = int(odds) if odds is not None else 0
        except Exception: o = 0
        if o <= -300: bk = "chalk"
        elif o <= -150: bk = "heavy_fav"
        elif o <= -110: bk = "slight_fav"
        elif o <= 100: bk = "pickem"
        elif o <= 200: bk = "medium_dog"
        else: bk = "big_dog"
        key = f"{m} / {bk}"
        # de-dupe per-event weight = 1
        ev = r.get("event_id") or f"{d}_{r.get('player_name')}"
        if ev in seen_events[sm]: continue
        seen_events[sm].add(ev)
        s = slot.setdefault(key, {"n": 0, "w": 0})
        s["n"] += 1
        if r.get("status") == "won":
            s["w"] += 1

    # Baseline hit rate for the sport (label proportion in train)
    tr_n = sum(v["n"] for v in train.values())
    tr_w = sum(v["w"] for v in train.values())
    baseline = (tr_w / tr_n) if tr_n else 0.5

    keys = set(train) | set(val) | set(test)
    buckets = []
    pvals: list[float] = []
    for k in keys:
        t = train.get(k, {"n": 0, "w": 0})
        v = val.get(k, {"n": 0, "w": 0})
        te = test.get(k, {"n": 0, "w": 0})
        if v["n"] < min_events:
            continue
        p_val = v["w"] / v["n"] if v["n"] else 0
        p_tr = t["w"] / t["n"] if t["n"] else 0
        p_te = te["w"] / te["n"] if te["n"] else 0
        lo, hi = _z(p_val, v["n"])
        pv = _p_value_two_prop(p_val, v["n"], baseline, tr_n)
        pvals.append(pv)
        buckets.append({
            "bucket": k,
            "train_n": t["n"], "train_hr": round(p_tr, 3),
            "val_n": v["n"], "val_hr": round(p_val, 3),
            "test_n": te["n"], "test_hr": round(p_te, 3),
            "baseline_hr": round(baseline, 3),
            "lift_pp": round((p_val - baseline) * 100.0, 2),
            "ci_lower": round(lo, 3), "ci_upper": round(hi, 3),
            "p_value": round(pv, 4),
            "provenance": "SHADOW_SIGNAL",
        })

    # BH-FDR across the tested hypotheses
    survive = bh_fdr(pvals, q=q_fdr) if pvals else []
    for i, b in enumerate(buckets):
        b["survives_fdr"] = bool(survive[i]) if i < len(survive) else False

    buckets.sort(key=lambda r: (r["survives_fdr"], r["val_hr"]), reverse=True)
    return {
        "available": True,
        "sport": sport,
        "validation_start": validation_start,
        "test_start": test_start,
        "baseline_hr": round(baseline, 3),
        "n_hypotheses": len(buckets),
        "q_fdr": q_fdr,
        "survivors": sum(1 for b in buckets if b["survives_fdr"]),
        "buckets": buckets,
    }
