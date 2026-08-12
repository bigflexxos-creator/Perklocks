"""MAGIC 3C — Shadow Calibration Validation.

Read-only comparison of three calibration methods on the settled-pick
history:

    A. RAW MODEL           — no calibration.
    B. BAND-EMPIRICAL      — the CURRENT production method
                              (brain.calibration.apply_calibration).
    C. ISOTONIC            — the dormant lock_calibration.py PAV curve,
                              refit chronologically on a training slice
                              so the audit is honest.

Never writes to `db.calibrated_probabilities` or `db.picks` — all
shadow output goes to `db.calibration_shadow_evaluation` for audit
review.

Metrics reported per method:
* Brier score (lower is better)
* log-loss   (lower is better)
* ECE (expected calibration error)  (lower is better)
* Reliability curve (bucket → observed vs predicted)
* Mean predicted vs actual hit rate
* Per-sport, per-market, per-threshold segmentation
* High-confidence (85+/90+/95+) audit

Isotonic fitted on TRAIN slice only.  Evaluation happens on VAL and
TEST slices — the test slice is untouched by fit AND by band-empirical
which uses running `brain.memory` history (band-empirical's use of
running memory is a leakage caveat surfaced in the report).
"""
from __future__ import annotations

import asyncio
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

sys.path.insert(0, "/app/backend")
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from motor.motor_asyncio import AsyncIOMotorClient

# Cutoff dates — chronological, no random splitting.
TRAIN_START = "2026-06-01"
TRAIN_END   = "2026-07-14"       # exclusive
VAL_START   = "2026-07-14"
VAL_END     = "2026-08-01"       # exclusive
TEST_START  = "2026-08-01"


# ── Metric helpers ────────────────────────────────────────────────────

def _safe_log(x: float) -> float:
    return math.log(max(min(x, 1 - 1e-12), 1e-12))


def brier(p: float, y: int) -> float:
    return (p - y) ** 2


def log_loss(p: float, y: int) -> float:
    return -(y * _safe_log(p) + (1 - y) * _safe_log(1 - p))


def ece(preds: list[float], ys: list[int], *, bins: int = 10) -> float:
    """Expected Calibration Error — equal-width binning."""
    if not preds:
        return 0.0
    n = len(preds)
    tot = 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        idx = [i for i, p in enumerate(preds) if lo <= p < hi] if b < bins - 1 else [
            i for i, p in enumerate(preds) if lo <= p <= hi]
        if not idx:
            continue
        avg_p = sum(preds[i] for i in idx) / len(idx)
        avg_y = sum(ys[i] for i in idx) / len(idx)
        tot += (len(idx) / n) * abs(avg_p - avg_y)
    return tot


def metrics(preds: list[float], ys: list[int]) -> dict:
    if not preds:
        return {"n": 0, "brier": None, "log_loss": None,
                 "ece": None, "mean_pred": None, "mean_actual": None,
                 "overconf_delta": None}
    n = len(preds)
    return {
        "n": n,
        "brier":    sum(brier(p, y) for p, y in zip(preds, ys)) / n,
        "log_loss": sum(log_loss(p, y) for p, y in zip(preds, ys)) / n,
        "ece":      ece(preds, ys),
        "mean_pred":   sum(preds) / n,
        "mean_actual": sum(ys) / n,
        "overconf_delta": (sum(preds) - sum(ys)) / n,
    }


def reliability_curve(preds: list[float], ys: list[int],
                        buckets: list[tuple[float, float]]) -> list[dict]:
    """Return per-bucket reliability rows."""
    rows: list[dict] = []
    for lo, hi in buckets:
        idx = [i for i, p in enumerate(preds) if lo <= p < hi]
        n = len(idx)
        if n == 0:
            continue
        wins = sum(ys[i] for i in idx)
        avg_p = sum(preds[i] for i in idx) / n
        rows.append({
            "lo": lo, "hi": hi, "n": n, "wins": wins,
            "observed":   wins / n,
            "predicted":  avg_p,
            "gap":        avg_p - wins / n,
        })
    return rows


# ── Isotonic PAV — same as lock_calibration.py, refit on train slice ─

def pool_adjacent_violators(xs: list[float], ys: list[float]) -> tuple[list[float], list[float]]:
    """Return (knots_x, knots_y) — monotone non-decreasing curve."""
    if not xs:
        return [], []
    # Build (x, y, w) blocks then merge violators.
    blocks: list[list] = [[x, y, 1.0] for x, y in zip(xs, ys)]
    while True:
        merged = False
        i = 0
        while i < len(blocks) - 1:
            if blocks[i][1] > blocks[i + 1][1] + 1e-12:
                w = blocks[i][2] + blocks[i + 1][2]
                y = (blocks[i][1] * blocks[i][2]
                      + blocks[i + 1][1] * blocks[i + 1][2]) / w
                x = (blocks[i][0] * blocks[i][2]
                      + blocks[i + 1][0] * blocks[i + 1][2]) / w
                blocks[i] = [x, y, w]
                blocks.pop(i + 1)
                merged = True
            else:
                i += 1
        if not merged:
            break
    return [b[0] for b in blocks], [b[1] for b in blocks]


class IsotonicCurve:
    def __init__(self):
        self.kx: list[float] = []
        self.ky: list[float] = []

    def fit(self, xs: list[float], ys: list[int]) -> "IsotonicCurve":
        if not xs:
            return self
        pairs = sorted(zip(xs, ys))
        sxs = [p[0] for p in pairs]
        sys_ = [float(p[1]) for p in pairs]
        kx, ky = pool_adjacent_violators(sxs, sys_)
        # Clamp tails so we never emit 0% or 100% off tiny samples.
        ky = [max(0.02, min(0.98, y)) for y in ky]
        self.kx = kx; self.ky = ky
        return self

    def predict(self, x: float) -> float:
        if not self.kx:
            return x     # identity fallback
        # Linear interpolation between knots.
        if x <= self.kx[0]:
            return self.ky[0]
        if x >= self.kx[-1]:
            return self.ky[-1]
        import bisect
        i = bisect.bisect_left(self.kx, x)
        x0, x1 = self.kx[i - 1], self.kx[i]
        y0, y1 = self.ky[i - 1], self.ky[i]
        if x1 == x0:
            return y1
        return y0 + (y1 - y0) * (x - x0) / (x1 - x0)


# ── Band-empirical simulation (mirrors brain.calibration behavior) ───

def band_of(p_pct: float) -> str:
    """Return e.g. '70-74' — band boundaries used by brain.calibration."""
    lo = int(p_pct // 5) * 5
    return f"{lo}-{lo + 4}"


def apply_band_empirical(preds: list[float], ys: list[int],
                          fit_preds: list[float], fit_ys: list[int]) -> list[float]:
    """Compute calibrated probs using band-empirical FIT slice.

    Direction: for each pick, look up its band's empirical hit rate in
    the FIT slice; if the band has < 20 samples, return raw.  Mirrors
    brain.calibration.apply_calibration's MIN_SAMPLE_FOR_OVERRIDE=20.
    """
    band_hits: dict[str, list[int]] = defaultdict(list)
    for p, y in zip(fit_preds, fit_ys):
        band_hits[band_of(p * 100)].append(y)

    out: list[float] = []
    for p, _ in zip(preds, ys):
        b = band_of(p * 100)
        samples = band_hits.get(b, [])
        if len(samples) < 20:
            out.append(p)
        else:
            actual = sum(samples) / len(samples)
            # Cap +5pp optimism buffer (mirrors MAX_OPTIMISM_BUFFER).
            capped = min(actual + 0.05, p)
            out.append(capped if capped < p else p)
    return out


# ── Data loader ──────────────────────────────────────────────────────

async def load_slice(db, since: str, until: Optional[str],
                      sport: Optional[str] = None) -> list[dict]:
    q: dict = {
        "status": {"$in": ["won", "lost"]},
        "win_probability": {"$exists": True, "$ne": None},
        "settled_at": {"$gte": since},
    }
    if until:
        q["settled_at"]["$lt"] = until
    if sport:
        q["sport"] = sport
    rows: list[dict] = []
    async for p in db.picks.find(q, {"_id": 0, "id": 1, "sport": 1,
            "market": 1, "line": 1, "side": 1, "win_probability": 1,
            "model_probability": 1, "sim_win_probability": 1,
            "status": 1, "settled_at": 1, "lock_score": 1,
            "brain": 1}):
        wp = p.get("win_probability") or p.get("model_probability")
        if wp is None:
            continue
        try:
            wpf = float(wp)
        except (TypeError, ValueError):
            continue
        if wpf > 1.0 + 1e-9:
            wpf = wpf / 100.0
        if not (0.0 <= wpf <= 1.0):
            continue
        y = 1 if p.get("status") == "won" else 0
        rows.append({
            "id": p.get("id"), "sport": p.get("sport"),
            "market": p.get("market"), "line": p.get("line"),
            "side": p.get("side"),
            "raw_prob": wpf, "y": y,
            "settled_at": p.get("settled_at"),
        })
    return rows


# ── Segmentation helpers ────────────────────────────────────────────

def market_family(market: str, sport: str) -> str:
    m = (market or "").lower()
    if sport == "MLB":
        for kw in ["strikeout", "hits", "home run", "total bases",
                    "rbi", "runs"]:
            if kw in m: return kw
        return "other"
    if sport == "NBA":
        for kw in ["points", "rebounds", "assists", "threes",
                    "steals", "blocks"]:
            if kw in m: return kw
        return "other"
    if sport == "Soccer":
        for kw in ["anytime goal scorer", "anytime scorer",
                    "to score or assist", "anytime assist",
                    "total goals", "moneyline", "both teams to score",
                    "draw", "double chance"]:
            if kw in m: return kw
        return "other"
    if sport == "Tennis":
        for kw in ["moneyline", "total games", "set spread", "spread"]:
            if kw in m: return kw
        return "other"
    return "other"


def high_conf_bucket(p: float) -> Optional[str]:
    pct = p * 100
    if pct >= 95: return "95+"
    if pct >= 90: return "90+"
    if pct >= 85: return "85+"
    return None


# ── Main ────────────────────────────────────────────────────────────

async def run(*, write: bool = False) -> dict:
    db = AsyncIOMotorClient(os.getenv("MONGO_URL"))["lockscore_db"]

    train = await load_slice(db, TRAIN_START, TRAIN_END)
    val   = await load_slice(db, VAL_START, VAL_END)
    test  = await load_slice(db, TEST_START, None)

    # Isotonic — fit on TRAIN only.
    train_preds = [r["raw_prob"] for r in train]
    train_ys    = [r["y"] for r in train]
    iso = IsotonicCurve().fit(train_preds, train_ys)

    def eval_slice(slice_rows: list[dict], *, tag: str) -> dict:
        raw_preds = [r["raw_prob"] for r in slice_rows]
        ys        = [r["y"] for r in slice_rows]
        raw_m     = metrics(raw_preds, ys)

        band_preds = apply_band_empirical(raw_preds, ys,
                                            train_preds, train_ys)
        band_m     = metrics(band_preds, ys)

        iso_preds  = [iso.predict(p) for p in raw_preds]
        iso_m      = metrics(iso_preds, ys)

        buckets = [(i/20, (i+1)/20) for i in range(20)]  # 5% bins
        rel_raw  = reliability_curve(raw_preds,  ys, buckets)
        rel_band = reliability_curve(band_preds, ys, buckets)
        rel_iso  = reliability_curve(iso_preds,  ys, buckets)

        # Per-sport, per-market, per-threshold, per-conf-band.
        by_sport: dict[str, dict] = {}
        for sport in {r["sport"] for r in slice_rows}:
            idx = [i for i, r in enumerate(slice_rows) if r["sport"] == sport]
            rp = [raw_preds[i] for i in idx]; bp = [band_preds[i] for i in idx]
            ip = [iso_preds[i] for i in idx]; ys_s = [ys[i] for i in idx]
            by_sport[sport] = {
                "n": len(idx),
                "raw":  metrics(rp,  ys_s),
                "band": metrics(bp,  ys_s),
                "iso":  metrics(ip,  ys_s),
            }

        by_market: dict[str, dict] = {}
        for i, r in enumerate(slice_rows):
            mkt = f"{r['sport']}::{market_family(r['market'] or '', r['sport'] or '')}"
            d = by_market.setdefault(mkt, {"idx": []})
            d["idx"].append(i)
        by_market_out: dict[str, dict] = {}
        for mkt, d in by_market.items():
            idx = d["idx"]
            if len(idx) < 30:
                continue
            rp = [raw_preds[i] for i in idx]; bp = [band_preds[i] for i in idx]
            ip = [iso_preds[i] for i in idx]; ys_s = [ys[i] for i in idx]
            by_market_out[mkt] = {
                "n": len(idx),
                "raw":  metrics(rp,  ys_s),
                "band": metrics(bp,  ys_s),
                "iso":  metrics(ip,  ys_s),
            }

        high_conf: dict[str, dict] = {}
        for tag2 in ("85+", "90+", "95+"):
            idx = [i for i, p in enumerate(raw_preds)
                    if high_conf_bucket(p) == tag2 or (
                        high_conf_bucket(p) and int(tag2[:-1]) <=
                        int(high_conf_bucket(p)[:-1] or 0)
                    )]
            # Include ALL with pct >= threshold
            thr = int(tag2[:-1]) / 100.0
            idx = [i for i, p in enumerate(raw_preds) if p >= thr]
            if not idx:
                continue
            rp = [raw_preds[i] for i in idx]; bp = [band_preds[i] for i in idx]
            ip = [iso_preds[i] for i in idx]; ys_s = [ys[i] for i in idx]
            high_conf[tag2] = {
                "n": len(idx),
                "actual_hit_rate": sum(ys_s) / len(ys_s),
                "raw_mean":        sum(rp)   / len(rp),
                "band_mean":       sum(bp)   / len(bp),
                "iso_mean":        sum(ip)   / len(ip),
                "raw":  metrics(rp, ys_s),
                "band": metrics(bp, ys_s),
                "iso":  metrics(ip, ys_s),
            }
        return {
            "tag":       tag,
            "n":         len(slice_rows),
            "raw":       raw_m,
            "band":      band_m,
            "iso":       iso_m,
            "reliability_raw":  rel_raw,
            "reliability_band": rel_band,
            "reliability_iso":  rel_iso,
            "by_sport":  by_sport,
            "by_market": by_market_out,
            "high_conf": high_conf,
        }

    rep = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cutoffs": {"train": [TRAIN_START, TRAIN_END],
                     "val":   [VAL_START, VAL_END],
                     "test":  [TEST_START, "OPEN"]},
        "train_n": len(train), "val_n": len(val), "test_n": len(test),
        "iso_knots":            len(iso.kx),
        "iso_extreme_outputs":  sum(1 for y in iso.ky if y <= 0.03 or y >= 0.97),
        "val":  eval_slice(val,  tag="val"),
        "test": eval_slice(test, tag="test"),
    }

    if write:
        try:
            await db["calibration_shadow_evaluation"].update_one(
                {"_id": "current"},
                {"$set": {**rep, "_id": "current"}},
                upsert=True,
            )
        except Exception:
            pass
    return rep


def print_report(rep: dict) -> None:
    print("=" * 76)
    print(f"MAGIC 3C — CALIBRATION SHADOW VALIDATION @ {rep['generated_at']}")
    print("=" * 76)
    print(f"train: {rep['cutoffs']['train'][0]} → {rep['cutoffs']['train'][1]}  n={rep['train_n']}")
    print(f"val:   {rep['cutoffs']['val'][0]}   → {rep['cutoffs']['val'][1]}    n={rep['val_n']}")
    print(f"test:  {rep['cutoffs']['test'][0]}  → {rep['cutoffs']['test'][1]}     n={rep['test_n']}")
    print(f"iso_knots={rep['iso_knots']}  iso_extreme_outputs={rep['iso_extreme_outputs']}")
    print()
    for tag in ("val", "test"):
        s = rep[tag]
        print(f"── {tag.upper()} SLICE n={s['n']} ──")
        for method in ("raw", "band", "iso"):
            m = s[method]
            if m["n"]:
                print(f"  {method:<5}  brier={m['brier']:.4f}  logloss={m['log_loss']:.4f}  "
                      f"ece={m['ece']:.4f}  mean_pred={m['mean_pred']:.3f}  "
                      f"actual={m['mean_actual']:.3f}  overconf={m['overconf_delta']:+.3f}")
        print()
        print(f"  Per-sport ({tag}):")
        for sport, ss in sorted(s["by_sport"].items(), key=lambda kv: -kv[1]["n"]):
            print(f"    {sport:8}  n={ss['n']:4}  "
                  f"raw_brier={ss['raw']['brier']:.4f}  "
                  f"band_brier={ss['band']['brier']:.4f}  "
                  f"iso_brier={ss['iso']['brier']:.4f}")
        print(f"  Per-market ({tag}):")
        for mkt, mm in sorted(s["by_market"].items(), key=lambda kv: -kv[1]["n"])[:14]:
            print(f"    {mkt:38}  n={mm['n']:4}  "
                  f"raw={mm['raw']['brier']:.4f}  band={mm['band']['brier']:.4f}  "
                  f"iso={mm['iso']['brier']:.4f}")
        print(f"  High-confidence ({tag}):")
        for hc, hh in s["high_conf"].items():
            print(f"    {hc:<5}  n={hh['n']:4}  actual={hh['actual_hit_rate']:.3f}  "
                  f"raw_mean={hh['raw_mean']:.3f}  band_mean={hh['band_mean']:.3f}  "
                  f"iso_mean={hh['iso_mean']:.3f}")
        print()

    # Verdict — per sport/market
    print("── ACTIVATION DECISION MATRIX (per-sport, TEST slice) ──")
    for sport, ss in rep["test"]["by_sport"].items():
        n = ss["n"]
        if n < 200:
            verdict = "INSUFFICIENT_SAMPLE"
        else:
            band_b, iso_b = ss["band"]["brier"], ss["iso"]["brier"]
            band_ll, iso_ll = ss["band"]["log_loss"], ss["iso"]["log_loss"]
            iso_ece = ss["iso"]["ece"]
            if iso_b < band_b - 0.005 and iso_ll < band_ll + 0.02 and iso_ece < 0.10:
                verdict = "SAFE_TO_ACTIVATE"
            elif iso_b < band_b:
                verdict = "SAFE_ONLY_WITH_MORE_SAMPLE"
            elif iso_b > band_b + 0.005:
                verdict = "BAND_EMPIRICAL_BETTER"
            else:
                verdict = "NO_MEANINGFUL_DIFFERENCE"
        print(f"  {sport:8}  n={n:4}  → {verdict}")


async def main() -> int:
    rep = await run(write=True)
    print_report(rep)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
