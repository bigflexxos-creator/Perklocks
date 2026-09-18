"""SHADOW REPORT — market-by-market promotion proof for the Probability
Authority.  Read-only: it evaluates the current board through the boundary
(shadow) and joins the calibrator registry.  Nothing here mutates picks.

GET  /api/probability-authority/shadow-report
POST /api/probability-authority/refit         (re-fits calibrators + CFB residual sigma)
"""
from __future__ import annotations

import collections
import time
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends

from auth import UserPublic
from deps import current_user
from services import probability_authority as pa
from services.probability_closures import CLOSURE_VERSION

router = APIRouter(prefix="/api/probability-authority", tags=["probability-authority"])


def _get_db():
    from server import db as _db
    return _db


def _readiness(doc: dict) -> dict:
    cm, rm = doc.get("calibrated_metrics") or {}, doc.get("raw_metrics") or {}
    improves = (cm.get("log_loss") is not None and rm.get("log_loss") is not None
                and cm["log_loss"] < rm["log_loss"] - 0.002)
    ready = doc.get("status") == "shadow_ready" and (doc.get("test_n") or 0) >= pa.MIN_TEST_N
    return {
        "promotion_ready": bool(ready and (improves or doc.get("champion") == "identity")),
        "improves_log_loss": bool(improves),
        "held_out_n": doc.get("test_n"),
        "verdict": ("PROMOTED" if doc.get("promotion_status") == "promoted"
                    else "READY_FOR_PROMOTION" if (ready and improves)
                    else "IDENTITY_IS_CHAMPION" if (ready and doc.get("champion") == "identity")
                    else "NOT_ENOUGH_EVIDENCE"),
    }


async def _accumulate(per_fam: dict, p: dict) -> None:
    c = pa.evaluate(p)
    f = per_fam[c.market_family]
    f["n"] += 1
    if (p.get("lock_score") or 0) >= 85:
        f["lock85"] += 1
    if c.publication_eligible:
        f["eligible"] += 1
    else:
        f["ineligible_reasons"][c.fallback_reason or "UNKNOWN"] += 1
    f["evidence"][c.evidence_quality] += 1
    if c.closure:
        f["closure_methods"][c.closure.get("method")] += 1
    if c.raw_model_probability is not None and c.calibrated_probability is not None:
        f["n_prob"] += 1
        f["sum_raw"] += c.raw_model_probability
        f["sum_closure"] += (c.closure_probability if c.closure_probability is not None else c.raw_model_probability)
        f["sum_cal"] += c.calibrated_probability
        f["max_abs_delta_pts"] = max(f["max_abs_delta_pts"], abs(c.probability_delta or 0.0))


async def build_shadow_report(db) -> dict[str, Any]:
    t0 = time.perf_counter()
    registry: dict[str, dict] = {}
    async for d in db[pa.REGISTRY].find({"is_active": True}, {"_id": 0}):
        registry[d["family"]] = d

    now_iso = datetime.now(timezone.utc).isoformat()
    per_fam: dict[str, dict] = collections.defaultdict(lambda: {
        "n": 0, "eligible": 0, "ineligible_reasons": collections.Counter(),
        "sum_raw": 0.0, "sum_closure": 0.0, "sum_cal": 0.0, "n_prob": 0,
        "closure_methods": collections.Counter(), "evidence": collections.Counter(),
        "lock85": 0, "max_abs_delta_pts": 0.0,
    })
    # Sample per sport so one high-volume sport cannot crowd others out.
    sports = await db.picks.distinct("sport", {"event_time": {"$gte": now_iso[:19]}, "lock_score": {"$ne": None}})
    for sp in sports:
        cur = db.picks.find({"sport": sp, "event_time": {"$gte": now_iso[:19]}, "lock_score": {"$ne": None}},
                            {"_id": 0}).sort("lock_score", -1).limit(1500)
        async for p in cur:
            await _accumulate(per_fam, p)

    families = []
    for fam in sorted(set(per_fam) | set(k for k in registry if k != "CFB_TOTAL_SIGMA")):
        f = per_fam.get(fam) or {}
        reg = registry.get(fam) or {}
        n_prob = f.get("n_prob") or 0
        families.append({
            "family": fam,
            "live": {
                "n": f.get("n", 0), "lock85": f.get("lock85", 0), "eligible": f.get("eligible", 0),
                "ineligible_reasons": dict(f.get("ineligible_reasons") or {}),
                "mean_raw": round(f["sum_raw"] / n_prob, 4) if n_prob else None,
                "mean_after_closure": round(f["sum_closure"] / n_prob, 4) if n_prob else None,
                "mean_calibrated": round(f["sum_cal"] / n_prob, 4) if n_prob else None,
                "max_abs_delta_pts": round(f.get("max_abs_delta_pts", 0.0), 2),
                "closure_methods": dict(f.get("closure_methods") or {}),
                "evidence": dict(f.get("evidence") or {}),
            },
            "registry": {k: reg.get(k) for k in ("version", "n_total", "test_n", "champion", "status",
                                                 "promotion_status", "calibration_cutoff", "fitted_at")},
            "raw_metrics": {k: (reg.get("raw_metrics") or {}).get(k) for k in ("brier", "log_loss", "calibration_error", "hi_conf_n", "hi_conf_predicted", "hi_conf_observed")},
            "calibrated_metrics": {k: (reg.get("calibrated_metrics") or {}).get(k) for k in ("brier", "log_loss", "calibration_error", "hi_conf_n", "hi_conf_predicted", "hi_conf_observed")},
            "readiness": _readiness(reg) if reg else {"verdict": "NO_SETTLED_EVIDENCE", "promotion_ready": False},
        })

    cfb_sigma = registry.get("CFB_TOTAL_SIGMA") or {}
    return {
        "generated_at": now_iso,
        "authority_version": pa.AUTHORITY_VERSION,
        "closure_version": CLOSURE_VERSION,
        "mode": pa._MODE(),
        "families": families,
        "cfb_total_sigma": {k: cfb_sigma.get(k) for k in ("n", "sigma", "sigma_raw", "prior_sigma", "prior_df", "status", "fitted_at")},
        "summary": {
            "families_live": sum(1 for x in families if x["live"]["n"]),
            "promoted": sum(1 for x in families if x["readiness"].get("verdict") == "PROMOTED"),
            "ready_for_promotion": sum(1 for x in families if x["readiness"].get("verdict") == "READY_FOR_PROMOTION"),
            "identity_champion": sum(1 for x in families if x["readiness"].get("verdict") == "IDENTITY_IS_CHAMPION"),
            "not_enough_evidence": sum(1 for x in families if x["readiness"].get("verdict") in ("NOT_ENOUGH_EVIDENCE", "NO_SETTLED_EVIDENCE")),
        },
        "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
    }


@router.get("/shadow-report")
async def shadow_report(user: Annotated[UserPublic, Depends(current_user)]):
    return await build_shadow_report(_get_db())


@router.post("/refit")
async def refit(user: Annotated[UserPublic, Depends(current_user)]):
    db = _get_db()
    from services import cfb_total_residuals as cfb_res
    summary = await pa.fit_all_calibrators(db)
    cfb_doc = await cfb_res.fit(db)
    return {"families": summary, "cfb_total_sigma": {k: cfb_doc.get(k) for k in ("n", "sigma", "sigma_raw", "status")}}
