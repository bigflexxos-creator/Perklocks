"""Universal Joint De-vig + Canonical Edge — Pass 1 §5/§6.

Single source of truth for paired-market fair probability and canonical
Edge on totals. Deterministic, side-symmetric, unit-tested.
"""
from __future__ import annotations
import logging
from typing import Any

log = logging.getLogger("lockscore.totals_devig")


def _american_to_implied(odds: int | float | None) -> float | None:
    if odds is None: return None
    try: o = float(odds)
    except Exception: return None
    if o == 0: return None
    if o >= 100: return 100.0 / (o + 100.0)
    if o <= -100: return -o / (-o + 100.0)
    return None


def joint_devig(over_odds: int | float | None,
                under_odds: int | float | None) -> dict[str, Any]:
    """Convert paired Over/Under prices to jointly-devigged fair probs.

    Returns {available, fair_over, fair_under, vig_pct, raw_over_implied,
    raw_under_implied}. `available=False` means one side is missing —
    caller must NOT use one-sided implied probability as fair market.
    """
    po = _american_to_implied(over_odds)
    pu = _american_to_implied(under_odds)
    if po is None or pu is None:
        return {"available": False, "reason": "paired_odds_missing",
                "raw_over_implied": po, "raw_under_implied": pu}
    s = po + pu
    if s <= 0:
        return {"available": False, "reason": "invalid_implied_sum"}
    fair_o = po / s
    fair_u = pu / s
    vig = max(0.0, (s - 1.0) * 100.0)
    return {"available": True,
            "fair_over": round(fair_o, 6),
            "fair_under": round(fair_u, 6),
            "vig_pct": round(vig, 3),
            "raw_over_implied": round(po, 6),
            "raw_under_implied": round(pu, 6)}


def canonical_totals_edge(model_prob: float | None,
                          side: str,
                          over_odds: int | float | None,
                          under_odds: int | float | None) -> dict[str, Any]:
    """Canonical Edge = model_prob − fair_market_prob for the SELECTED
    side (Over or Under). Uses JOINT de-vig. Returns
    {available, edge, fair_market_prob, model_prob, source}.

    Both candidate-selection and displayed Edge must call this function
    with the SAME (model_prob, side, over_odds, under_odds) — no other
    Edge formula is permitted on totals.
    """
    if model_prob is None:
        return {"available": False, "reason": "no_model_prob"}
    dv = joint_devig(over_odds, under_odds)
    if not dv["available"]:
        return {"available": False, "reason": dv["reason"]}
    side_l = (side or "").lower()
    fair = dv["fair_over"] if "over" in side_l else dv["fair_under"] if "under" in side_l else None
    if fair is None:
        return {"available": False, "reason": "unknown_side"}
    return {"available": True,
            "edge": round(float(model_prob) - fair, 6),
            "fair_market_prob": fair,
            "model_prob": round(float(model_prob), 6),
            "vig_pct": dv["vig_pct"],
            "source": "canonical_totals_edge_v1"}


def check_alt_ladder_monotonic(rungs: list[dict]) -> tuple[bool, str]:
    """Ladder rungs: [{line: float, over_prob: float, under_prob: float}, ...].
    Verifies P(Over) monotone-decreasing in line AND P(Under) monotone-
    increasing. Returns (ok, reason)."""
    if not rungs or len(rungs) < 2:
        return True, "single_rung"
    ordered = sorted(rungs, key=lambda r: float(r["line"]))
    prev_o = None; prev_u = None
    for i, r in enumerate(ordered):
        o = float(r.get("over_prob") or 0)
        u = float(r.get("under_prob") or 0)
        if prev_o is not None and o > prev_o + 1e-4:
            return False, f"over_ladder_break_at_line={r['line']} (prev={prev_o:.4f} < curr={o:.4f})"
        if prev_u is not None and u + 1e-4 < prev_u:
            return False, f"under_ladder_break_at_line={r['line']} (prev={prev_u:.4f} > curr={u:.4f})"
        prev_o, prev_u = o, u
    return True, "ladder_monotonic"
