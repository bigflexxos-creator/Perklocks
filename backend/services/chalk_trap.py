"""Chalk Kill Switch — post-processing filter that flags heavy chalk
picks lacking data-driven confirmation and demotes their conviction so
they surface as informational only, never as recommendations.

USER MANDATE (2026-07-21): "auto-fade any pick priced worse than -250
unless model edge >= 8pp with >=3 aligned data signals. Right now we're
stuffing the board with -400 favorites that need 80% hit rate just to
break even."

RATIONALE:
    A -300 favorite must hit at 75% to break even at flat 1u staking.
    A -400 favorite must hit at 80%. Historical realized hit rates on
    "chalk locks" in this app track ~68-72% — a losing structure. The
    kill switch demotes any pick worse than -250 that does NOT have:
      - True model edge >= 8 percentage points, AND
      - >= 3 aligned data-driven contribs with positive total lift

WHAT HAPPENS TO A "TRAP" PICK:
    - lock_score capped at 72 (Solid Lean band — not a Lock)
    - lock_score_v2 / lock_score_raw / lock_score_peak all clamped
    - grade demoted to "Solid Lean"
    - `chalk_trap = True` flag added
    - `chalk_trap_reason` explains why
    - `pick_rationale.concerns` gets a "Chalk trap zone" warning
    - `signal_score_raw` capped at 55 (below Strong band)
    - Pick STAYS on the board — users can still see it and choose,
      but the app will not recommend it as a Lock/Elite pick.

WHAT ESCAPES THE KILL SWITCH:
    - Picks with book_odds > -250 (i.e., -240 or shorter, or dogs)
    - Picks with true model edge >= 8pp AND >= 3 DD signals with lift > 0
    - Picks explicitly marked `is_alt=True` (alt-lines have their own math)
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("lockscore.chalk_trap")

# ── Configuration ────────────────────────────────────────────────────
CHALK_ODDS_THRESHOLD = -250        # priced -250 or worse (-300, -400...)
MIN_EDGE_PP          = 8.0         # must beat book by >=8 percentage points
MIN_DD_SIGNALS       = 3           # must have >=3 data-driven contribs
MIN_DD_LIFT_PP       = 1.5         # positive DD lift >=1.5pp
DEMOTED_LOCK         = 72.0        # Solid Lean band
DEMOTED_SIGNAL       = 55          # Just below Strong band


def _is_chalk(pick: dict) -> bool:
    """Return True if the pick is priced -250 or worse."""
    try:
        odds = int(pick.get("book_odds") or 0)
    except (TypeError, ValueError):
        return False
    return odds <= CHALK_ODDS_THRESHOLD


def _dd_confirmed(pick: dict) -> tuple[bool, dict[str, Any]]:
    """Check if the pick has strong data-driven confirmation.

    Returns (passed, diagnostics) — diagnostics dict explains why."""
    contribs = pick.get("data_driven_contribs")
    if not isinstance(contribs, dict) or not contribs:
        return False, {"reason": "no_dd_contribs", "signals": 0, "lift_pp": 0.0}
    aligned = [
        v for v in contribs.values()
        if isinstance(v, (int, float)) and abs(v) >= 0.003
    ]
    total_pp = sum(
        float(v) for v in contribs.values() if isinstance(v, (int, float))
    ) * 100.0
    if len(aligned) < MIN_DD_SIGNALS:
        return False, {"reason": "too_few_signals",
                       "signals": len(aligned), "lift_pp": round(total_pp, 2)}
    if total_pp < MIN_DD_LIFT_PP:
        return False, {"reason": "insufficient_lift",
                       "signals": len(aligned), "lift_pp": round(total_pp, 2)}
    return True, {"signals": len(aligned), "lift_pp": round(total_pp, 2)}


def _edge_meets_threshold(pick: dict) -> tuple[bool, float]:
    """Check if the model edge is at least MIN_EDGE_PP."""
    try:
        edge = float(pick.get("edge_percent") or 0.0)
    except (TypeError, ValueError):
        edge = 0.0
    return (edge >= MIN_EDGE_PP), edge


def apply_chalk_kill_switch(picks: list[dict]) -> dict[str, int]:
    """Apply the chalk kill switch to all picks in-place.

    Returns a summary dict for logging:
        {"total": N, "chalk_seen": M, "trapped": K, "spared_by_edge": E,
         "spared_by_dd": D, "spared_alt": A}
    """
    stats = {
        "total": len(picks),
        "chalk_seen": 0,
        "trapped": 0,
        "spared_by_edge": 0,
        "spared_by_dd": 0,
        "spared_alt": 0,
        "already_low": 0,
    }
    for p in picks:
        if not _is_chalk(p):
            continue
        stats["chalk_seen"] += 1

        # ── Alt-line escape (narrowed 2026-07-21) ─────────────────────
        # OLD: blanket-spared ALL alt-lines because "alt-lines have their
        # own edge economics". Problem discovered via ROI analysis:
        # MLB strikeout Over Alt-Locks (295/300 settled picks were alts)
        # bled -16.6% ROI overall, -43.8% ROI on board-visible K picks
        # (89% had edge < -5%). The blanket alt-line escape sprayed
        # negative-edge -400 chalk K props onto the board unchecked.
        #
        # NEW: alt-lines are only spared when they have genuine positive
        # edge (>= +2pp). Alt-lines priced as chalk with negative edge
        # are structurally the same bleed pattern as regular chalk —
        # over-priced favorites with juice eating profit.
        is_alt = bool(p.get("is_alt") or (p.get("line_type") or "").lower().find("alt") >= 0)
        try:
            _alt_edge = float(p.get("edge_percent") or 0.0)
        except (TypeError, ValueError):
            _alt_edge = 0.0
        if is_alt and _alt_edge >= 2.0:
            stats["spared_alt"] += 1
            continue

        # ── Escape hatch 1: strong true edge (>= 8pp) ─────────────────
        edge_ok, edge = _edge_meets_threshold(p)
        # ── Escape hatch 2: strong DD confirmation (>=3 signals, +lift)
        dd_ok, dd_diag = _dd_confirmed(p)

        if edge_ok and dd_ok:
            # BOTH gates pass — this is a genuinely +EV chalk pick.
            stats["spared_by_edge"] += 1
            stats["spared_by_dd"] += 1
            p["chalk_verified"] = True
            p["chalk_verified_reason"] = (
                f"edge={edge:.1f}pp · dd_signals={dd_diag['signals']} · "
                f"dd_lift={dd_diag['lift_pp']:+.1f}pp"
            )
            continue

        # No exemption — demote the pick.
        try:
            base_lock = float(p.get("lock_score") or 0.0)
        except (TypeError, ValueError):
            base_lock = 0.0

        if base_lock <= DEMOTED_LOCK:
            # Already below the demotion floor — no need to re-write.
            stats["already_low"] += 1
            p["chalk_trap"] = True
            p["chalk_trap_reason"] = (
                f"priced {p.get('book_odds')} · edge {edge:.1f}pp < 8pp · "
                f"dd={dd_diag.get('reason','none')}"
            )
            continue

        # Cap all lock-related fields so the read-time canonicalizer
        # can't restore a stale higher value.
        p["chalk_trap"] = True
        p["chalk_trap_reason"] = (
            f"priced {p.get('book_odds')} · edge {edge:.1f}pp < 8pp · "
            f"dd={dd_diag.get('reason','none')}"
        )
        p["chalk_trap_meta"] = {
            "original_lock": base_lock,
            "book_odds": p.get("book_odds"),
            "edge_pp": edge,
            "dd_signals": dd_diag.get("signals", 0),
            "dd_lift_pp": dd_diag.get("lift_pp", 0.0),
        }
        p["lock_score"] = DEMOTED_LOCK
        p["lock_score_v2"] = DEMOTED_LOCK
        p["lock_score_raw"] = DEMOTED_LOCK
        p["lock_score_peak"] = DEMOTED_LOCK
        # Downgrade grade to Solid Lean explicitly (Elite Lock / Strong
        # Lock badges are misleading on a chalk trap).
        p["grade"] = "Solid Lean"
        # Cap the visible signal so the pick doesn't show up in the top
        # of the Sharp Slate ranker or the min_signal filter as strong.
        cur_sig = p.get("signal_score_raw")
        if isinstance(cur_sig, (int, float)) and cur_sig > DEMOTED_SIGNAL:
            p["signal_score_raw"] = DEMOTED_SIGNAL
            p["signal_score"] = min(int(p.get("signal_score") or DEMOTED_SIGNAL), DEMOTED_SIGNAL)

        # Attach a visible concern into pick_rationale so the "Why this
        # pick?" panel and the card banner surface the trap warning.
        try:
            rat = p.setdefault("pick_rationale", {}) or {}
            if not isinstance(rat, dict):
                # If rationale is a non-dict (legacy), replace it.
                rat = {}
                p["pick_rationale"] = rat
            concerns = rat.setdefault("concerns", [])
            if isinstance(concerns, list):
                warning = (
                    f"⚠️ Chalk Trap: {p.get('book_odds')} price without "
                    f"data-driven confirmation. Requires ~"
                    f"{_break_even_pct(p.get('book_odds')):.0f}% hit rate "
                    f"just to break even."
                )
                if warning not in concerns:
                    concerns.append(warning)
        except Exception:  # noqa: BLE001
            pass

        stats["trapped"] += 1

    return stats


def _break_even_pct(odds: Any) -> float:
    """Return the break-even hit-rate percentage for the given American odds."""
    try:
        o = int(odds or 0)
    except (TypeError, ValueError):
        return 0.0
    if o == 0:
        return 0.0
    if o >= 100:
        return 100.0 / (o + 100.0) * 100.0
    return (-o) / (-o + 100.0) * 100.0
