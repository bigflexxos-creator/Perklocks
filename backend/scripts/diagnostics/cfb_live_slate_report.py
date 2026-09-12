"""CFB LIVE SLATE REPORT — HIGH-TIER REACHABILITY CLOSURE
Generated 2026-06-12 · Provenance-first safety net + Apex-enabled CFB.

Read-only diagnostic — never mutates DB.

Reports:
  1. CFB Lock Score distribution across all main-board-eligible picks
     for today's slate (buckets 85-89, 90-92, 93-95, 96-98, 99, 100 Apex).
  2. Top 10 current CFB candidates with:
       event, selection, book_odds, implied_prob, model_prob,
       sim_prob, calibrated_prob, lock_score, tier, evidence count,
       engine/model/publication versions, apex gates passed/total,
       apex_block_reason.

Zero manufacture: if the slate has zero picks in a tier, that number
is reported honestly.

Run: python -m scripts.diagnostics.cfb_live_slate_report
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from typing import Any

sys.path.insert(0, "/app/backend")

from deps import db


BUCKETS = [
    ("85-89",   85.0,  89.999),
    ("90-92",   90.0,  92.999),
    ("93-95",   93.0,  95.999),
    ("96-98",   96.0,  98.999),
    ("99",      99.0,  99.999),
    ("100 Apex", 100.0, 100.0),
]

_CFB_CURRENT_FACTOR_KEYS = (
    "Model Fair Prob", "__data_quality",
    "SP+ Margin Base", "Sportsbook Implied Prob",
)


def _has_current_provenance(p: dict) -> bool:
    """Factor-level provenance check — must match routes.picks_routes
    logic exactly."""
    factors = p.get("factors") or {}
    if not isinstance(factors, dict) or not factors:
        return False
    for fk in _CFB_CURRENT_FACTOR_KEYS:
        if factors.get(fk) not in (None, "", {}, []):
            return True
    return False


def _bucket_of(ls: float) -> str:
    for name, lo, hi in BUCKETS:
        if lo <= ls <= hi:
            return name
    return "<85"


def _pick_lock_score(p: dict) -> float:
    for k in ("published_lock_score", "lock_score_v2", "lock_score"):
        v = p.get(k)
        try:
            f = float(v) if v is not None else None
        except (TypeError, ValueError):
            f = None
        if f is not None and f > 0:
            return f
    return 0.0


def _apex_gates_summary(p: dict) -> dict[str, Any]:
    """Reconstruct apex gate status from persisted fields."""
    reasons_met = p.get("apex_reasons") or []
    block_reason = p.get("apex_block_reason")
    # Total gates enforced by evaluate_apex (see services/magic/apex_gate.py):
    #   1. sport_market_eligible
    #   2. real_market_line_present
    #   3. magic_tier=ALIGNED_STRONG
    #   4. magic_score_available
    #   5. no_contradictory_categories
    #   6. no_risk_flags
    #   7. base_score>=97
    #   8. positive_categories>=5
    #   9. context_evidence_present (role_or_matchup)
    #  10. market_intelligence_present
    #  11. model_family_not_sole_signal
    #  (12. sport_specific_strict_gate — only for soccer_ATS / NFL_ATD)
    total_gates = 11
    passed = len(reasons_met) if isinstance(reasons_met, list) else 0
    return {
        "passed": passed,
        "total":  total_gates,
        "blocker": block_reason,
        "apex_lock": bool(p.get("apex_lock")),
        "apex_status": p.get("apex_status"),
    }


def _engine_versions(p: dict) -> dict[str, Any]:
    return {
        "engine_version":       p.get("block8_integrator_version"),
        "apex_gate_version":    p.get("apex_gate_version"),
        "model_source":         p.get("model_source"),
        "publication_version":  p.get("publication_snapshot_version"),
        "generated_at":         p.get("generated_at")
                                or p.get("created_at"),
    }


def _prob_triple(p: dict) -> dict[str, Any]:
    return {
        "model_probability":       p.get("model_probability"),
        "simulator_probability":   p.get("simulator_probability")
                                    or p.get("sim_win_probability"),
        "calibrated_probability":  p.get("calibrated_probability"),
        "win_probability":         p.get("win_probability"),
        "implied_probability":     p.get("implied_probability"),
    }


async def main() -> None:
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    horizon = (now.replace(microsecond=0)).isoformat()
    # Current slate = CFB picks whose event_time is in the future
    # (matches the same "in-play window" logic /picks/today uses).
    q = {"sport": "CFB", "event_time": {"$gte": horizon},
         "off_board": {"$ne": True}}
    cursor = db.picks.find(q, {"_id": 0}).limit(5000)
    rows: list[dict] = await cursor.to_list(length=5000)

    # ── Apply the same provenance-first safety net used by the API
    kept: list[dict] = []
    blocked_by_safety = 0
    for p in rows:
        ls = _pick_lock_score(p)
        if ls >= 95.0 and not _has_current_provenance(p):
            blocked_by_safety += 1
            continue
        # Also drop no_bet / off_board so we report the same pool as
        # the main board.
        if p.get("no_bet") is True or p.get("off_board") is True:
            continue
        kept.append(p)

    # ── Distribution
    dist: dict[str, int] = {name: 0 for name, _, _ in BUCKETS}
    dist["<85"] = 0
    for p in kept:
        ls = _pick_lock_score(p)
        b = _bucket_of(ls)
        dist[b] = dist.get(b, 0) + 1

    # ── Top 10 by lock score
    kept_sorted = sorted(kept, key=_pick_lock_score, reverse=True)
    top10 = []
    for p in kept_sorted[:10]:
        ls = _pick_lock_score(p)
        top10.append({
            "canonical_prediction_id": p.get("id") or p.get("pick_id"),
            "event":       p.get("event"),
            "selection":   p.get("selection") or p.get("side")
                             or p.get("market"),
            "market":      p.get("market"),
            "book_odds":   p.get("book_odds"),
            "prob_triple": _prob_triple(p),
            "lock_score":  ls,
            "grade":       p.get("grade"),
            "tier":        p.get("tier"),
            "evidence_count": p.get("evidence_count")
                              or p.get("magic_categories_available")
                              or len((p.get("factors") or {})),
            "engine_versions": _engine_versions(p),
            "apex_gates":  _apex_gates_summary(p),
            "provenance_ok": _has_current_provenance(p),
        })

    report = {
        "generated_at":       datetime.now(timezone.utc).isoformat(),
        "slate_window":       f"event_time >= {horizon}",
        "total_cfb_rows":     len(rows),
        "kept_after_safety":  len(kept),
        "blocked_by_safety":  blocked_by_safety,
        "distribution":       dist,
        "top10":              top10,
        "notes": [
            "Distribution reflects ONLY main-board-eligible CFB picks "
            "(safety-net applied, no_bet/off_board dropped).",
            "Zero picks in a tier is a valid, honest observation.",
            "Provenance-first safety net requires current CFB provenance "
            "markers before an LS>=95 pick is allowed.",
        ],
    }
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
