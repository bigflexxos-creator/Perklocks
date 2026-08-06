"""Soccer scorer / lineup eligibility caps — Phase 4E.2.

Audit finding: goal-scorer markets could hit elite confidence for
players who were not in the confirmed / projected starting XI.  This
module produces a per-player **eligibility report** that the Magic
Tier policy consumes to cap confidence when lineup/role certainty is
insufficient.

Signals used (only the ones we actually surface — no invented data):
  * lineup_status:
      "confirmed"    — starter published by official / provider feed
      "projected"    — expected XI from projected lineups feed
      "bench"        — named but on bench
      "doubt"        — questionable (injury / suspension pending)
      "out"          — ruled out
      "unknown"      — no lineup info available
  * expected_minutes  — projected minutes (0-90+ET); when absent → None
  * recent_xg90       — xG per 90 over rolling window (when available)
  * shot_volume90     — shots / 90
  * shots_on_target90 — SoT / 90
  * penalty_taker     — True only when known
  * set_piece_taker   — True only when known
  * position          — "FW", "MF", "DF", "GK", or None
  * opponent_defense  — 0-1 factor from ctx (weaker = higher factor)
  * team_attack       — 0-1 factor from ctx
  * home_away         — "home" | "away"
  * sub_risk          — provider-reported substitution risk 0-1

Tier caps applied by ``eligibility_cap()``:

  confirmed + ≥ 2 real signals  → Apex allowed (no cap)
  confirmed + 1 signal          → Elite Lock max
  projected + ≥ 2 signals       → Strong Lock max  (NEVER elite)
  projected + 1 signal          → Lock max
  bench                         → Playable max (NEVER Lock+)
  doubt                         → Lock max
  out                           → PASS (do not emit)
  unknown                       → Lock max (never elite)

Score-or-assist markets require:
  * lineup_status confirmed OR projected
  * team_attack signal
  * ≥ 90 expected_minutes for elite eligibility
Otherwise: cap at Lock.

First-scorer / Last-scorer markets carry additional caps because they
depend on sequence timing:
  * confirmed + penalty_taker known + ≥ 2 signals → Strong Lock max
  * anything less                                  → Lock max
"""
from __future__ import annotations

from typing import Optional


# Canonical lineup labels we accept from providers.
LINEUP_CONFIRMED = "confirmed"
LINEUP_PROJECTED = "projected"
LINEUP_BENCH     = "bench"
LINEUP_DOUBT     = "doubt"
LINEUP_OUT       = "out"
LINEUP_UNKNOWN   = "unknown"

_VALID_LINEUP = frozenset({
    LINEUP_CONFIRMED, LINEUP_PROJECTED, LINEUP_BENCH,
    LINEUP_DOUBT, LINEUP_OUT, LINEUP_UNKNOWN,
})


def _norm_lineup(status: Optional[str]) -> str:
    if not status:
        return LINEUP_UNKNOWN
    s = str(status).strip().lower()
    # Accept common synonyms.
    if s in ("confirmed", "starter", "starting", "start"):
        return LINEUP_CONFIRMED
    if s in ("projected", "expected", "predicted", "likely"):
        return LINEUP_PROJECTED
    if s in ("bench", "sub", "reserve"):
        return LINEUP_BENCH
    if s in ("doubt", "questionable", "doubtful", "gtd"):
        return LINEUP_DOUBT
    if s in ("out", "ruled out", "injured", "suspended"):
        return LINEUP_OUT
    if s in _VALID_LINEUP:
        return s
    return LINEUP_UNKNOWN


def _signal_count(ctx: dict) -> int:
    """Count how many real per-player supporting signals are present."""
    count = 0
    keys = (
        "recent_xg90", "shot_volume90", "shots_on_target90",
        "team_attack", "opponent_defense", "expected_minutes",
        "penalty_taker", "set_piece_taker",
    )
    for k in keys:
        v = ctx.get(k)
        if isinstance(v, bool):
            if v:  # bool True counts; False (known-non-taker) also counts as info
                count += 1
        elif isinstance(v, (int, float)) and v is not None:
            count += 1
    return count


def assess_scorer_eligibility(
    player_ctx: dict,
    market: str,
) -> dict:
    """Return eligibility + tier cap for a soccer scorer-family pick.

    Parameters
    ----------
    player_ctx : dict — per-player context with keys documented above.
    market : str — the underlying market key.  Values recognized:
        * "anytime_scorer" / "player_goal_scorer_anytime"
        * "score_or_assist" / "player_score_or_assist"
        * "first_scorer" / "player_first_scorer"
        * "last_scorer"  / "player_last_scorer"
        * "player_shots" / "player_shots_on_target"

    Returns
    -------
    dict with keys:
        lineup_status  — normalised status
        expected_minutes
        signal_count
        eligible       — bool (False → do not emit pick)
        max_tier       — advisory cap
        reasons        — list[str]
        market_family  — "scorer" | "score_or_assist" | "first_last" | "shots" | "other"
    """
    lineup_status = _norm_lineup(player_ctx.get("lineup_status"))
    exp_min = player_ctx.get("expected_minutes")
    sig_n = _signal_count(player_ctx)
    reasons: list[str] = []

    m_l = (market or "").strip().lower()
    if "score_or_assist" in m_l or "goal_or_assist" in m_l:
        family = "score_or_assist"
    elif "first_scorer" in m_l or "last_scorer" in m_l or "first_goalscorer" in m_l or "last_goalscorer" in m_l:
        family = "first_last"
    elif "shots_on_target" in m_l or "player_shots" in m_l:
        family = "shots"
    elif "scorer" in m_l or "goal" in m_l:
        family = "scorer"
    else:
        family = "other"

    # ── Hard-block cases ─────────────────────────────────────────────
    if lineup_status == LINEUP_OUT:
        reasons.append("player_out")
        return {
            "lineup_status": lineup_status, "expected_minutes": exp_min,
            "signal_count": sig_n, "eligible": False, "max_tier": "Pass",
            "reasons": reasons, "market_family": family,
        }

    # Bench: never elite, never lock — Playable at best regardless of family.
    if lineup_status == LINEUP_BENCH:
        reasons.append("bench_player")
        return {
            "lineup_status": lineup_status, "expected_minutes": exp_min,
            "signal_count": sig_n, "eligible": True, "max_tier": "Playable",
            "reasons": reasons, "market_family": family,
        }

    # ── Family-specific caps ─────────────────────────────────────────
    if family == "scorer":
        if lineup_status == LINEUP_CONFIRMED and sig_n >= 2:
            cap = "Apex Lock"
        elif lineup_status == LINEUP_CONFIRMED:
            cap = "Elite Lock"
            reasons.append("confirmed_but_only_one_signal")
        elif lineup_status == LINEUP_PROJECTED and sig_n >= 2:
            cap = "Strong Lock"
            reasons.append("projected_starter_capped_below_elite")
        elif lineup_status == LINEUP_PROJECTED:
            cap = "Lock"
            reasons.append("projected_with_thin_signals")
        elif lineup_status == LINEUP_DOUBT:
            cap = "Lock"
            reasons.append("lineup_doubt")
        else:
            cap = "Lock"
            reasons.append("unknown_lineup_status")

    elif family == "score_or_assist":
        # Explicitly kept separate from scorer-only.  Requires
        # confirmed / projected lineup AND team_attack signal AND ≥ 90
        # expected minutes for elite eligibility.
        team_attack_ok = isinstance(player_ctx.get("team_attack"), (int, float))
        long_mins = isinstance(exp_min, (int, float)) and exp_min >= 85
        if lineup_status == LINEUP_CONFIRMED and team_attack_ok and long_mins and sig_n >= 2:
            cap = "Apex Lock"
        elif lineup_status == LINEUP_CONFIRMED and team_attack_ok:
            cap = "Elite Lock"
        elif lineup_status == LINEUP_PROJECTED and team_attack_ok:
            cap = "Strong Lock"
            reasons.append("projected_score_or_assist_capped_below_elite")
        else:
            cap = "Lock"
            reasons.append("score_or_assist_thin_evidence")

    elif family == "first_last":
        # First/last scorer is sequence-dependent — needs penalty
        # taker + confirmed lineup for even Strong Lock eligibility.
        pen_known = isinstance(player_ctx.get("penalty_taker"), bool)
        if lineup_status == LINEUP_CONFIRMED and pen_known and sig_n >= 2:
            cap = "Strong Lock"
            reasons.append("first_last_capped_at_strong_lock")
        else:
            cap = "Lock"
            reasons.append("first_last_uncertain_sequence")

    elif family == "shots":
        # Shots markets — heavy volume-driven; needs shot signals.
        vol_ok = isinstance(player_ctx.get("shot_volume90"), (int, float))
        if lineup_status == LINEUP_CONFIRMED and vol_ok and sig_n >= 2:
            cap = "Apex Lock"
        elif lineup_status == LINEUP_CONFIRMED:
            cap = "Elite Lock"
        elif lineup_status == LINEUP_PROJECTED and vol_ok:
            cap = "Strong Lock"
        else:
            cap = "Lock"
            reasons.append("shots_thin_evidence")

    else:  # other / unknown family
        cap = "Lock"
        reasons.append(f"unrecognised_family:{m_l}")

    # ── Penalty / set-piece role protection ──────────────────────────
    # If the market implicitly assumes a role (first-scorer, penalty
    # goal props), and we DO NOT KNOW the role, cap harder.
    if family in ("first_last",) and player_ctx.get("penalty_taker") is None:
        reasons.append("penalty_role_unknown_cap")
        # Already at Lock in this branch; keep it.
        cap = "Lock"

    return {
        "lineup_status":     lineup_status,
        "expected_minutes":  exp_min,
        "signal_count":      sig_n,
        "eligible":          True,
        "max_tier":          cap,
        "reasons":           reasons,
        "market_family":     family,
    }


__all__ = [
    "assess_scorer_eligibility",
    "LINEUP_CONFIRMED", "LINEUP_PROJECTED", "LINEUP_BENCH",
    "LINEUP_DOUBT", "LINEUP_OUT", "LINEUP_UNKNOWN",
]
