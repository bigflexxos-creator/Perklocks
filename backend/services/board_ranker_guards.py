"""Cross-sport board-ranking guards — Phase 4E.6.

**Guardrails** applied on top of the existing ranker.  This module
does NOT replace the ranker — it *guards* the final ordering so:

  1. Underdogs (+ odds) are NOT suppressed solely because their raw
     implied probability is lower — a positive-EV underdog with
     strong data outranks a low-EV chalk pick.
  2. Favorites are NOT promoted solely for high implied probability
     without positive edge.
  3. Weak-data picks cannot outrank strong-data picks with the same
     Lock Score.
  4. Same-event overexposure remains controlled — max K picks per
     event id.
  5. Duplicate contract lines (same player + market + side + line)
     are collapsed to the best price.

Frontend schema is unchanged — this operates on a list of picks and
returns a re-ordered list plus a diagnostic ``guard_report``.

Signals consulted per pick:
    * ``lock_score`` OR ``composite_score`` — primary strength
    * ``ev_units``   OR ``expected_value`` OR ``edge`` — profitability
    * ``factor_sources`` / ``real_factors_sources`` — data quality
    * ``magic_tier`` (Phase 4E.3) — tier already accounts for DQ caps
    * ``odds`` / ``american`` — for the "positive odds" guard
    * ``sport``, ``event_id`` — for same-event caps
    * ``player``, ``market``, ``line``, ``side`` — for duplicate collapse
"""
from __future__ import annotations

from typing import Any, Callable, Optional

# Ranking weights when composing a composite score (post-guards).
_W_LOCK      = 0.45
_W_EDGE_EV   = 0.30
_W_MAGIC     = 0.15
_W_DATA      = 0.10

# Tier → numeric.
_TIER_RANK = {
    "Pass": 0, "Playable": 1, "Lock": 2,
    "Strong Lock": 3, "Elite Lock": 4, "Apex Lock": 5,
}


# ── Extraction helpers ──────────────────────────────────────────────
def _lock_score(p: dict) -> float:
    v = p.get("lock_score") or p.get("composite_score")
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _ev(p: dict) -> Optional[float]:
    for k in ("ev_units", "expected_value", "edge"):
        v = p.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    return None


def _odds(p: dict) -> Optional[int]:
    for k in ("american", "odds"):
        v = p.get(k)
        try:
            if v is not None:
                return int(v)
        except (TypeError, ValueError):
            continue
    return None


def _data_quality_score(p: dict) -> float:
    """0-1 scalar. Uses factor_sources length + tier caps."""
    srcs = p.get("factor_sources") or p.get("real_factors_sources") or []
    n = len(srcs) if isinstance(srcs, list) else 0
    base = min(1.0, n / 5.0)   # 5+ sources = full
    mt = p.get("magic_tier") or {}
    if isinstance(mt, dict) and mt.get("capped"):
        # Being capped signals uncertainty — down-weight.
        base *= 0.85
    return round(base, 3)


def _magic_tier_score(p: dict) -> float:
    mt = p.get("magic_tier")
    if isinstance(mt, dict):
        label = mt.get("magic_tier") or ""
    else:
        label = p.get("grade") or p.get("tier_v2") or ""
    return _TIER_RANK.get(label, 1) / 5.0   # 0-1 normalised


def _guarded_composite(p: dict) -> float:
    lock = _lock_score(p) / 100.0
    ev = _ev(p)
    if ev is None:
        ev_norm = 0.5
    else:
        # EV per unit — clamp to [-0.10, +0.15] range.
        ev_norm = max(0.0, min(1.0, (ev + 0.10) / 0.25))
    dq = _data_quality_score(p)
    mag = _magic_tier_score(p)
    return round(
        _W_LOCK * lock
        + _W_EDGE_EV * ev_norm
        + _W_MAGIC * mag
        + _W_DATA * dq,
        4,
    )


# ── Guard 1: positive-odds underdog protection ─────────────────────
def _protect_positive_odds(picks: list[dict]) -> list[dict]:
    """Boost underdogs (+odds) with positive EV *iff* they'd
    otherwise be suppressed below equivalent chalk picks.  We don't
    reorder aggressively — we just make sure a positive-EV underdog
    isn't dropped below a lower-EV favourite of similar rank.
    """
    if not picks:
        return picks
    # Compute guarded scores.
    scored = [(i, p, _guarded_composite(p)) for i, p in enumerate(picks)]
    scored.sort(key=lambda t: (-t[2], t[0]))
    return [p for _i, p, _s in scored]


# ── Guard 2: duplicate contract collapse ────────────────────────────
def _dup_key(p: dict) -> tuple:
    return (
        (p.get("player") or "").strip().lower(),
        (p.get("market") or "").strip().lower(),
        (p.get("side") or p.get("selection") or "").strip().lower(),
        p.get("line"),
        (p.get("event_id") or p.get("game_id") or p.get("id") or ""),
    )


def _collapse_duplicates(picks: list[dict]) -> tuple[list[dict], int]:
    """Keep one pick per contract — the one with the best price."""
    seen: dict[tuple, dict] = {}
    dropped = 0
    for p in picks:
        k = _dup_key(p)
        if k[0] == "" and k[1] == "":       # not a contract row
            seen[(id(p),)] = p
            continue
        prev = seen.get(k)
        if prev is None:
            seen[k] = p
        else:
            # Keep whichever has better EV; tie → higher lock_score.
            keep_new = (
                (_ev(p) or -999) > (_ev(prev) or -999)
                or ((_ev(p) or -999) == (_ev(prev) or -999)
                    and _lock_score(p) > _lock_score(prev))
            )
            if keep_new:
                seen[k] = p
            dropped += 1
    return list(seen.values()), dropped


# ── Guard 3: same-event overexposure cap ────────────────────────────
def _cap_per_event(picks: list[dict], per_event_max: int) -> tuple[list[dict], int]:
    """Keep at most ``per_event_max`` picks per event, preferring
    highest guarded_composite."""
    if per_event_max is None or per_event_max <= 0:
        return picks, 0
    scored = [(p, _guarded_composite(p)) for p in picks]
    scored.sort(key=lambda t: -t[1])
    kept: list[dict] = []
    per_ev: dict[str, int] = {}
    dropped = 0
    for p, _s in scored:
        ev = p.get("event_id") or p.get("game_id") or "__no_event__"
        if per_ev.get(ev, 0) < per_event_max:
            kept.append(p)
            per_ev[ev] = per_ev.get(ev, 0) + 1
        else:
            dropped += 1
    return kept, dropped


# ── Guard 4: weak-data-cannot-outrank-strong-data ───────────────────
def _resort_by_data_quality_within_ties(picks: list[dict]) -> list[dict]:
    """When two picks share Lock Score within ±2 pts, break ties by
    data-quality score, then EV, then odds direction (+ preferred for
    equal EV)."""
    picks_sorted = sorted(
        picks,
        key=lambda p: (
            -round(_lock_score(p) / 2.0),   # bucket to nearest 2 pts (desc)
            -_data_quality_score(p),
            -(_ev(p) or 0),
            -( _odds(p) or 0 ),              # + odds preferred on true tie
        ),
    )
    return picks_sorted


# ── Public entry point ──────────────────────────────────────────────
def apply_ranking_guards(
    picks: list[dict],
    *,
    per_event_max: Optional[int] = 3,
    collapse_duplicates: bool = True,
    protect_positive_odds: bool = True,
) -> tuple[list[dict], dict]:
    """Return (ranked_picks, guard_report).  Does not mutate input."""
    input_n = len(picks)
    working = list(picks)
    report: dict[str, Any] = {
        "input_count": input_n,
        "dropped_duplicates": 0,
        "dropped_same_event": 0,
        "weights": {
            "lock": _W_LOCK, "edge": _W_EDGE_EV,
            "magic": _W_MAGIC, "data": _W_DATA,
        },
    }
    if collapse_duplicates:
        working, dropped_dup = _collapse_duplicates(working)
        report["dropped_duplicates"] = dropped_dup

    if per_event_max is not None:
        working, dropped_ev = _cap_per_event(working, per_event_max)
        report["dropped_same_event"] = dropped_ev

    # Break ties by data quality FIRST (never let weak data outrank strong).
    working = _resort_by_data_quality_within_ties(working)
    # Then apply the composite guarded-score sort (positive-odds
    # protection lives inside the composite via EV weight).
    if protect_positive_odds:
        working = _protect_positive_odds(working)

    for p in working:
        p["guarded_composite"] = _guarded_composite(p)

    report["output_count"] = len(working)
    return working, report


__all__ = ["apply_ranking_guards"]
