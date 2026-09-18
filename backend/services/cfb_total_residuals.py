"""CFB TOTAL RESIDUAL AUTHORITY — empirically fitted predictive sigma.

residual = actual_total − expected_total_at_publication

Rows come ONLY from frozen published CFB total picks that have settled
(point-in-time by construction: `published_at` < `event_time`).  The
fitted sigma replaces the TOTAL_SIGMA_BASE prior ONLY when the held-out
sample is sufficient (N ≥ MIN_N); otherwise the prior stays and the
registry records NOT_ENOUGH_EVIDENCE.  Nothing is capped or nudged —
the distribution is whatever the residuals say it is.
"""
from __future__ import annotations

import logging
import math
import os
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("lockscore.cfb_total_residuals")

REGISTRY = "calibrator_registry"
FAMILY = "CFB_TOTAL_SIGMA"
MIN_N = int(os.environ.get("CFB_TOTAL_SIGMA_MIN_N", "60"))
# Normal-inverse-gamma prior pseudo-count for the predictive total
# distribution.  With ZERO verified out-of-sample residuals the predictive
# is Student-t(ν0) around the model total — heavier tails than the old
# fixed-σ normal because σ itself is unverified.  Each settled residual
# adds one degree of freedom; the predictive converges to the normal as
# evidence accumulates.  This is the exact Bayesian posterior predictive,
# not a cap and not a Lock Score adjustment.
PRIOR_DF = float(os.environ.get("CFB_TOTAL_PRIOR_DF", "6"))
_cache: dict = {"sigma": None, "n": 0, "sigma_raw": None}


def empirical_total_sigma() -> Optional[float]:
    """In-process champion sigma (None → caller keeps the prior)."""
    return _cache.get("sigma")


def residual_evidence() -> tuple[Optional[float], int]:
    """(raw residual sigma, n) from the latest fit — used for the Bayesian
    predictive blend even when n < MIN_N (small n contributes little)."""
    return _cache.get("sigma_raw"), int(_cache.get("n") or 0)


def predictive_total_over_probability(expected_total: float, book_line: float,
                                      prior_sigma: float) -> dict:
    """P(total > line) under the posterior predictive Student-t.

    scale² = (ν0·σ_prior² + n·σ_emp²) / (ν0 + n),  df = ν0 + n.
    Push mass on integer lines is removed; Over/Under complementary."""
    from scipy.stats import t as _t
    s_emp, n = residual_evidence()
    nu0 = max(1.0, PRIOR_DF)
    if s_emp and n > 0:
        scale = math.sqrt((nu0 * prior_sigma ** 2 + n * s_emp ** 2) / (nu0 + n))
    else:
        scale = float(prior_sigma)
    df = nu0 + n
    dist = _t(df, loc=float(expected_total), scale=scale)
    line = float(book_line)
    if line.is_integer():
        p_push = dist.cdf(line + 0.5) - dist.cdf(line - 0.5)
        p_over_raw = 1.0 - dist.cdf(line + 0.5)
        p_over = p_over_raw / max(1e-9, 1.0 - p_push)
    else:
        p_push = 0.0
        p_over = 1.0 - dist.cdf(line)
    return {"p_over": float(min(0.999, max(0.001, p_over))), "p_push": float(p_push),
            "df": float(df), "scale": round(float(scale), 3), "prior_sigma": float(prior_sigma),
            "residual_sigma": s_emp, "residual_n": n,
            "method": "student_t_posterior_predictive.v1"}


async def load(db) -> None:
    doc = await db[REGISTRY].find_one({"family": FAMILY, "is_active": True}, {"_id": 0})
    if not doc:
        return
    if doc.get("status") == "promoted" and doc.get("sigma"):
        _cache.update({"sigma": float(doc["sigma"])})
    _cache.update({"n": int(doc.get("n") or 0), "sigma_raw": doc.get("sigma_raw")})


async def fit(db) -> dict:
    """Chronological fit: sigma estimated on the first 70% of settled
    residuals, validated on the last 30% (held-out log score vs prior)."""
    rows = []
    cur = db.picks.find(
        {"sport": "CFB", "market": {"$regex": "Total", "$options": "i"},
         "result": {"$in": ["won", "lost", "push"]}, "actual_result": {"$ne": None}},
        {"_id": 0, "event_time": 1, "published_at": 1, "actual_result": 1, "line": 1,
         "model_output": 1, "expected_total": 1, "cfb_model": 1})
    async for d in cur:
        et, pa = str(d.get("event_time") or ""), str(d.get("published_at") or "")
        if not et or not pa or pa[:19] >= et[:19]:
            continue  # leakage guard
        mo = d.get("model_output") or {}
        exp = d.get("expected_total") or mo.get("expected_total") or (d.get("cfb_model") or {}).get("expected_total")
        try:
            actual = float(d.get("actual_result")); exp = float(exp)
        except (TypeError, ValueError):
            continue
        rows.append((et, actual - exp))
    rows.sort()
    n = len(rows)
    now = datetime.now(timezone.utc).isoformat()
    doc = {"family": FAMILY, "n": n, "fitted_at": now, "is_active": True,
           "prior_sigma": 13.5, "prior_df": PRIOR_DF, "status": "NOT_ENOUGH_EVIDENCE",
           "sigma": None, "sigma_raw": None}
    if n >= 2:
        _m = sum(r[1] for r in rows) / n
        doc["sigma_raw"] = round(math.sqrt(sum((r[1] - _m) ** 2 for r in rows) / (n - 1)), 3)
    if n >= MIN_N:
        cut = int(n * 0.7)
        train = [r[1] for r in rows[:cut]]; test = [r[1] for r in rows[cut:]]
        mean = sum(train) / len(train)
        sigma = math.sqrt(sum((r - mean) ** 2 for r in train) / max(1, len(train) - 1))
        def _nll(s):
            return sum(0.5 * math.log(2 * math.pi * s * s) + (r ** 2) / (2 * s * s) for r in test) / len(test)
        doc.update({"sigma": round(sigma, 3), "bias": round(mean, 3), "train_n": cut, "test_n": n - cut,
                    "test_nll_fitted": round(_nll(sigma), 4), "test_nll_prior": round(_nll(13.5), 4),
                    "training_range": [rows[0][0], rows[cut - 1][0]], "test_range": [rows[cut][0], rows[-1][0]]})
        better = doc["test_nll_fitted"] <= doc["test_nll_prior"] + 1e-6
        doc["status"] = "promoted" if (better and os.environ.get("PROB_AUTH_PROMOTE_CFB_TOTAL_SIGMA", "0") == "1") \
            else ("shadow_ready" if better else "prior_champion")
    await db[REGISTRY].update_many({"family": FAMILY, "is_active": True}, {"$set": {"is_active": False}})
    await db[REGISTRY].insert_one(dict(doc))
    _cache.update({"n": n, "sigma_raw": doc.get("sigma_raw")})
    if doc["status"] == "promoted":
        _cache.update({"sigma": doc["sigma"]})
    logger.info("CFB total residual fit: %s", {k: doc.get(k) for k in ("n", "sigma", "status")})
    return doc
