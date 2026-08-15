"""Longshot Trap — post-processing filter that catches over-confident Strong
Lock picks on long-odds markets (esp. Soccer 92+ scorers + long-shot MLs).

USER MANDATE (2026-07-21): Historical ROI analysis of 5,309 settled picks
showed Soccer 92+ Strong-Lock picks bleed -21% ROI (-48u over 226 picks).
The bleed is entirely concentrated in two subsets:

  • Goal Scorer / Score-or-Assist market: -41% ROI (-62u over 151 picks)
  • Long-odds picks (>= +100 American): -46% to -74% ROI

The chalk 92+ picks (< -150 odds) are actually PROFITABLE (+1.6% to +11%
ROI). So the fix is odds-tier-specific, not blanket.

WHAT IS A "LONGSHOT TRAP":
    A Strong Lock (92+ lock_score) priced at +100 American or worse where
    the model's ~92% "high confidence" implied prob is mathematically
    impossible — a +200 line implies 33% max win probability, not 92%.
    Something in the lock calculation is over-crediting evidence for
    small-market longshot goalscorers / Latin-American win-draw dogs / etc.

WHAT ESCAPES THE TRAP (whitelist):
    1. Elite anchor players (Kane / Haaland / Mbappé / Ronaldo / Salah)
       flagged via is_elite=True — their +200 goalscorer prices are
       genuinely undervalued when they face weak defenses.
    2. Extreme +EV picks (edge >= 12pp) with >= 3 aligned DD signals.
    3. Non-soccer sports (this trap is Soccer-specific per the ROI
       analysis; MLB/Tennis 92+ bands are near-flat, not bleeding).

WHAT HAPPENS TO A TRAPPED PICK:
    - lock_score / lock_v2 / lock_raw / lock_peak all capped at 82 (still
      a Lock, but drops out of the Strong Lock 92+ Board tier)
    - grade demoted to "Lock" (from Strong Lock / Elite Lock)
    - signal_score_raw capped at 68 (below Strong Signal band)
    - `longshot_trap = True` flag + reason attached
    - pick_rationale.concerns gets a warning
    - Pick STAYS in DB and searchable — just doesn't dominate the board
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("lockscore.longshot_trap")

# ── Configuration ────────────────────────────────────────────────────
LOCK_TRIGGER = 92.0     # Only apply to Strong-Lock / Elite-Lock band
ODDS_TRIGGER = -100     # American odds >= -100 (i.e., -99, +100, +250...)
MIN_EDGE_ESCAPE_PP = 12.0
MIN_DD_SIGNALS_ESCAPE = 3
MIN_DD_LIFT_PP_ESCAPE = 2.0
DEMOTED_LOCK = 82.0     # Standard Lock band (drops out of 92+ Board tier)
DEMOTED_SIGNAL = 68     # Below the Strong Signal 78 floor


def _is_soccer(pick: dict) -> bool:
    return (pick.get("sport") or "").lower() == "soccer"


def _lock_v(pick: dict) -> float:
    def _f(x):
        try:
            return float(x) if x is not None else 0.0
        except (TypeError, ValueError):
            return 0.0
    return max(
        _f(pick.get("lock_score")),
        _f(pick.get("lock_score_v2")),
        _f(pick.get("lock_score_peak")),
    )


def _odds(pick: dict) -> int:
    try:
        return int(pick.get("book_odds") or 0)
    except (TypeError, ValueError):
        return 0


def _is_elite_anchor(pick: dict) -> bool:
    """Elite ATP-tier soccer players get the escape hatch — their +200
    goalscorer prices are systematically undervalued vs. weak defenses."""
    if pick.get("is_elite") or pick.get("elite_boost") or pick.get("elite_striker"):
        return True
    if (pick.get("player_tags") or {}).get("elite"):
        return True
    # ── Phase 2A.5 (2026-08) — MLS 2025 hardcoded scorer/starter
    # whitelist RETIRED as an eligibility gate.  Historical
    # scorer/starter information is downstream *evidence only* and
    # cannot decide whether a player escapes a longshot trap.
    # (Previous behavior: an MLS player was granted an "elite escape"
    # if they appeared in the stale 2025 whitelist — that reintroduces
    # reputation-based Lock Score decisions Phase 2A.5 retired.)
    return False


def _dd_confirmed(pick: dict) -> tuple[bool, dict[str, Any]]:
    contribs = pick.get("data_driven_contribs")
    if not isinstance(contribs, dict) or not contribs:
        return False, {"reason": "no_dd", "signals": 0, "lift_pp": 0.0}
    aligned = [v for v in contribs.values()
               if isinstance(v, (int, float)) and abs(v) >= 0.003]
    total_pp = sum(float(v) for v in contribs.values()
                   if isinstance(v, (int, float))) * 100.0
    if len(aligned) < MIN_DD_SIGNALS_ESCAPE:
        return False, {"reason": "too_few", "signals": len(aligned),
                       "lift_pp": round(total_pp, 2)}
    if total_pp < MIN_DD_LIFT_PP_ESCAPE:
        return False, {"reason": "no_lift", "signals": len(aligned),
                       "lift_pp": round(total_pp, 2)}
    return True, {"signals": len(aligned), "lift_pp": round(total_pp, 2)}


def apply_longshot_trap(picks: list[dict]) -> dict[str, int]:
    """Apply longshot-trap demotion in place. Returns stats dict."""
    stats = {
        "total": len(picks),
        "seen": 0,           # picks matching the trigger criteria
        "trapped": 0,
        "spared_elite": 0,
        "spared_edge_dd": 0,
    }
    for p in picks:
        if not _is_soccer(p):
            continue
        lock = _lock_v(p)
        if lock < LOCK_TRIGGER:
            continue
        odds = _odds(p)
        if odds == 0 or odds < ODDS_TRIGGER:
            # Chalk favorite (-150 or shorter) — not the bleeding tier.
            continue
        stats["seen"] += 1

        # ── Escape 1: elite anchor player ────────────────────────────
        if _is_elite_anchor(p):
            stats["spared_elite"] += 1
            p["longshot_verified"] = True
            p["longshot_verified_reason"] = "elite anchor (Kane/Haaland/Mbappé tier)"
            continue

        # ── Escape 2: extreme +EV with data-driven confirmation ──────
        try:
            edge = float(p.get("edge_percent") or 0.0)
        except (TypeError, ValueError):
            edge = 0.0
        dd_ok, dd_diag = _dd_confirmed(p)
        if edge >= MIN_EDGE_ESCAPE_PP and dd_ok:
            stats["spared_edge_dd"] += 1
            p["longshot_verified"] = True
            p["longshot_verified_reason"] = (
                f"extreme edge {edge:.1f}pp + {dd_diag['signals']} DD signals "
                f"({dd_diag['lift_pp']:+.1f}pp lift)"
            )
            continue

        # ── Trap: demote ─────────────────────────────────────────────
        original_lock = lock
        original_grade = p.get("grade")
        original_signal = p.get("signal_score_raw")

        p["longshot_trap"] = True
        p["longshot_trap_reason"] = (
            f"soccer 92+ at {odds:+d} · edge {edge:.1f}pp < 12pp · "
            f"dd={dd_diag.get('reason', 'none')} · historically -30% to -74% ROI"
        )
        p["longshot_trap_meta"] = {
            "original_lock": original_lock,
            "original_grade": original_grade,
            "original_signal_raw": original_signal,
            "book_odds": odds,
            "edge_pp": edge,
            "dd_signals": dd_diag.get("signals", 0),
            "dd_lift_pp": dd_diag.get("lift_pp", 0.0),
        }
        p["lock_score"] = DEMOTED_LOCK
        p["lock_score_v2"] = DEMOTED_LOCK
        p["lock_score_raw"] = DEMOTED_LOCK
        p["lock_score_peak"] = DEMOTED_LOCK
        p["grade"] = "Lock"  # Downgrade from Strong / Elite

        # Cap signal so the pick doesn't rank in the top of the Signal
        # slate. Only apply the cap when the current value is higher —
        # never uplift a low signal by "capping" it.
        cur_sig = p.get("signal_score_raw")
        if isinstance(cur_sig, (int, float)) and cur_sig > DEMOTED_SIGNAL:
            p["signal_score_raw"] = DEMOTED_SIGNAL
            p["signal_score"] = min(
                int(p.get("signal_score") or DEMOTED_SIGNAL),
                DEMOTED_SIGNAL,
            )

        # Attach concern to rationale.
        try:
            rat = p.setdefault("pick_rationale", {}) or {}
            if not isinstance(rat, dict):
                rat = {}
                p["pick_rationale"] = rat
            concerns = rat.setdefault("concerns", [])
            if isinstance(concerns, list):
                warning = (
                    f"⚠️ Longshot Trap: Strong Lock priced {odds:+d} without "
                    f"data-driven confirmation. Historical ROI on this tier "
                    f"is -30% to -74%."
                )
                if warning not in concerns:
                    concerns.append(warning)
        except Exception:  # noqa: BLE001
            pass

        stats["trapped"] += 1

    return stats


__all__ = ["apply_longshot_trap"]
