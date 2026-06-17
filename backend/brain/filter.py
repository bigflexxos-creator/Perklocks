"""Decision Filter — formal PASS verdict.

A pick is REJECTED (verdict = PASS) when any of these gates trip:

  • edge_percent < MIN_EDGE_PCT                    (no model value)
  • simulator.expected_value < MIN_EV              (-EV per Monte Carlo)
  • simulator.variance > MAX_VARIANCE              (too volatile)
  • simulator.agreement_score < MIN_AGREEMENT      (factors disagree)
  • data_completeness (from candidate_components) < MIN_DATA
  • bucket ROI < MIN_BUCKET_ROI with n ≥ MIN_BUCKET_N    (historically bad)
  • bucket calibration_err > MAX_CAL_ERR with n ≥ MIN_BUCKET_N

PASS picks set the existing `no_bet=True` flag, which the feed endpoints
(/picks/today, /picks/rollover, /picks/under-of-the-day, /picks/parlay)
already respect — guarantees the brain culls picks WITHOUT touching the
UI layer.

The PASS reason list (`brain.pass_reasons`) is preserved on the pick so
the Analytics screen / audits can attribute outcomes.

Elite-tagged picks (the curated Lock board) skip the filter to honour the
product rule that Elite picks always surface. Even so we still record
the verdict in `brain.elite_override_pass` for audit.
"""
from __future__ import annotations

import logging

from .memory import BrainMemory

logger = logging.getLogger("lockscore.brain.filter")

# ───────────────────────── Thresholds ─────────────────────────
MIN_EDGE_PCT     = 0.5      # below this = no model value
MIN_EV           = 0.0      # -EV after MC = PASS
MAX_VARIANCE     = 6.0      # >6 = wildly volatile
MIN_AGREEMENT    = 0.30     # factor stdev signal
MIN_DATA         = 0.30     # data completeness
MIN_BUCKET_ROI   = -15.0    # historically bad bucket
MAX_CAL_ERR      = 0.12     # bucket mean-predicted vs actual gap > 12pp = drifted
MIN_BUCKET_N     = 25       # require sample size for ROI/cal gates


def _bucket_for(pick: dict, memory: BrainMemory):
    sport = pick.get("sport") or ""
    sv2 = pick.get("selection_v2") or {}
    family = (sv2.get("market") or {}).get("family") or "other"
    return memory.market(sport, family)


def decision_filter(picks: list[dict], memory: BrainMemory) -> dict:
    """Apply PASS verdicts; mutate `brain.verdict` + `no_bet` flag."""
    counts = {"PASS": 0, "KEEP": 0, "elite_override": 0}
    reason_tally: dict[str, int] = {}

    for p in picks:
        brain = p.setdefault("brain", {})
        sim = brain.get("simulator") or {}
        comp = brain.get("candidate_components") or {}
        reasons: list[str] = []

        # 1) Hard edge gate
        if (p.get("edge_percent") or 0.0) < MIN_EDGE_PCT:
            reasons.append("edge<0.5%")
        # 2) Simulator EV (top-K only)
        if brain.get("top_k") and sim:
            if sim.get("expected_value", 0) < MIN_EV:
                reasons.append("sim_ev<0")
            if sim.get("variance", 0) > MAX_VARIANCE:
                reasons.append("sim_var_high")
            if sim.get("agreement_score", 1) < MIN_AGREEMENT:
                reasons.append("factor_disagreement")
        # 3) Data completeness
        if (comp.get("data") or 0) < MIN_DATA:
            reasons.append("missing_data")
        # 4) Bucket history gates
        bucket = _bucket_for(p, memory)
        if bucket and bucket.n >= MIN_BUCKET_N:
            if bucket.roi_pct < MIN_BUCKET_ROI:
                reasons.append(f"bucket_roi<{MIN_BUCKET_ROI:.0f}%")
            if bucket.calibration_err > MAX_CAL_ERR:
                reasons.append("bucket_cal_drift")

        for r in reasons:
            reason_tally[r] = reason_tally.get(r, 0) + 1

        if reasons:
            # Honour the product rule: Elite picks always surface, but we
            # still record the verdict for audit.
            if p.get("elite_player") or (p.get("lock_score") or 0) >= 99:
                brain["verdict"] = "KEEP"
                brain["elite_override_pass"] = reasons
                counts["elite_override"] += 1
                counts["KEEP"] += 1
            else:
                brain["verdict"] = "PASS"
                brain["pass_reasons"] = reasons
                # Hook into the existing no_bet flag → feed endpoints drop it.
                p["no_bet"] = True
                p["brain_pass"] = True   # explicit attribution
                counts["PASS"] += 1
        else:
            brain["verdict"] = "KEEP"
            counts["KEEP"] += 1

    if reason_tally:
        logger.info("Brain filter PASS reasons: %s", reason_tally)
    return counts | {"reasons": reason_tally}
