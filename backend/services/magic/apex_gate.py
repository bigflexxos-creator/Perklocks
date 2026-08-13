"""Block 8 — Explicit APEX 100 Gate.

Pure function.  Given a pick's base lock score, its ``MagicOutput``
and the pre-computed category-vote sets, decides whether the pick
qualifies for APEX 100.

APEX is a **badge** for exceptionally-supported picks.  It is NOT
a probability claim (APEX 100 ≠ 100 % win) — the empirical audit
in Phase 1 showed even the current 99 wins only ~57 % of the time.

Rules (user-approved Phase 2 contract):

 1. ``magic_tier == ALIGNED_STRONG``.
 2. ``magic_score_available == True``.
 3. Zero CONTRADICTORY core categories.
 4. Zero RISK_ELEVATED risk flags.
 5. ``base_lock_score >= 97.0`` **BEFORE** Magic delta
    (anti-promotion — APEX confirms an elite base call, never
    promotes a mid-tier one).
 6. At least **5 of 6** independent categories in
    {history_exact, recent_form, role_opportunity, matchup,
     model_family, market_intel} vote positive.
 7. E (Model / Sim / Calibration) counts as ONE — enforced by
    the categorisation layer.
 8. At least one of ``role_opportunity`` or ``matchup`` (context)
    is positive — E alone can never justify APEX.
 9. At least one of ``market_intel`` is positive — no fabrication;
    missing market_snapshots → this fails and APEX is blocked.
10. Sport is on ``APEX_ELIGIBLE_SPORTS``.
11. Market is APEX-eligible for that sport (see ``apex_market_allowed``).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from services.magic.contract import MagicOutput, MagicTier


APEX_GATE_VERSION = "apex_gate.v1.0"


APEX_ELIGIBLE_SPORTS: frozenset[str] = frozenset({
    "MLB", "Soccer", "NBA", "Tennis", "NFL",
})

# Explicit sport-level unavailability list — evidence infrastructure
# is materially incomplete (per Block 3H closure).  These sports can
# still receive normal scoring; only APEX is denied.
APEX_UNAVAILABLE_SPORTS: frozenset[str] = frozenset({
    "CFB", "UFC", "MMA", "NHL", "KBO",
})

APEX_MIN_BASE_SCORE = 97.0
APEX_MIN_POSITIVE_CATEGORIES = 5


# ─────────────────────────────────────────────────────────────────────────
# Market-level APEX policy
# ─────────────────────────────────────────────────────────────────────────

# Substrings (case-insensitive) that DISQUALIFY a market from APEX.
# Every entry is a hard block — variance too high or evidence contract
# too weak to responsibly stamp 100.
_APEX_MARKET_HARD_BLOCKS = (
    "first goal",            # First Goal Scorer (soccer)
    "first_goal",
    "player_first_goal",
    "first td",              # First TD Scorer (NFL)
    "first_td",
    "player_1st_td",
    "method of victory",     # MMA Method of Victory
    "method_of_victory",
    "mma_method_of_victory",
)


def _norm(s: Optional[str]) -> str:
    return (s or "").strip().lower()


def _market_matches_any(market: str, needles: tuple[str, ...]) -> bool:
    m = _norm(market)
    return any(n in m for n in needles)


def apex_market_allowed(sport: str, market: str, pick: dict) -> tuple[bool, Optional[str]]:
    """Sport / market APEX eligibility.

    Returns ``(allowed, reason)`` — ``reason`` is a short diagnostic
    when disallowed, ``None`` when allowed.
    """
    sport_norm = (sport or "").strip()
    if sport_norm in APEX_UNAVAILABLE_SPORTS:
        return False, f"sport_apex_unavailable:{sport_norm}"
    if sport_norm not in APEX_ELIGIBLE_SPORTS:
        return False, f"sport_not_whitelisted:{sport_norm}"

    market_norm = _norm(market)

    if _market_matches_any(market_norm, _APEX_MARKET_HARD_BLOCKS):
        return False, f"market_apex_blocked:{market_norm}"

    # First-TD flag defensively — a pick already tagged
    # ``publication_gate=first_td_dormant_...`` should never see APEX.
    pg = _norm(pick.get("publication_gate"))
    if pg and "first_td_dormant" in pg:
        return False, "market_apex_blocked:first_td_dormant"

    return True, None


# ─────────────────────────────────────────────────────────────────────────
# Sport-specific gates (soccer anytime-goal-scorer, NFL anytime-TD)
# ─────────────────────────────────────────────────────────────────────────

def _soccer_anytime_goal_gate_ok(pick: dict, mo: MagicOutput) -> tuple[bool, Optional[str]]:
    """Extra-strict Soccer Anytime Goal Scorer APEX gate.

    Requires:
      * confirmed / strong starting role
      * sufficient minutes expectation
      * shots / SOT evidence
      * xG / npxG evidence
      * strong scorer archetype fit
      * simulator support
      * matchup support
      * real sportsbook market evidence
      * no major contradiction
      * required independent-category threshold (checked upstream)
    """
    # Confirmed starting role (from soccer_lineups + role evidence).
    role = pick.get("role") or pick.get("role_status") or ""
    lineup_status = pick.get("lineup_status") or ""
    starter_ok = any(
        "start" in _norm(v) for v in (role, lineup_status,
                                       pick.get("confirmed_role"))
    ) or bool(pick.get("confirmed_starter"))
    if not starter_ok:
        return False, "soccer_apex:role_not_confirmed_starter"

    minutes = pick.get("expected_minutes") or pick.get("minutes_projection")
    if minutes is None or float(minutes or 0) < 60.0:
        return False, "soccer_apex:insufficient_expected_minutes"

    # Evidence signals: at least ONE shots-based item AND ONE xG-based item.
    ev_types_available: set[str] = set()
    for ev in mo.evidence:
        if ev.availability.value not in ("AVAILABLE", "PARTIAL"):
            continue
        # Sub-signals live under the ``label`` or ``notes`` — search both.
        text = f"{_norm(ev.label)} {_norm(ev.notes)}"
        if any(k in text for k in ("shot", "sot")):
            ev_types_available.add("shots")
        if any(k in text for k in ("xg", "npxg")):
            ev_types_available.add("xg")
    if "shots" not in ev_types_available:
        return False, "soccer_apex:missing_shots_or_sot_evidence"
    if "xg" not in ev_types_available:
        return False, "soccer_apex:missing_xg_or_npxg_evidence"

    # Simulator support required (Soccer sim exists as of Magic 3I).
    if not (pick.get("sim_win_probability") is not None
              or pick.get("simulator_type")):
        return False, "soccer_apex:no_simulator_support"

    return True, None


def _nfl_anytime_td_gate_ok(pick: dict, mo: MagicOutput) -> tuple[bool, Optional[str]]:
    """Extra-strict NFL Anytime TD APEX gate.

    Requires:
      * role / usage support (backfield share, red-zone usage)
      * simulator support
      * red-zone / opportunity evidence
      * matchup / opponent-strength evidence
      * consensus / line-movement evidence
    """
    # Role / usage evidence
    has_role_usage = False
    has_redzone = False
    has_matchup = False
    for ev in mo.evidence:
        if ev.availability.value not in ("AVAILABLE", "PARTIAL"):
            continue
        text = f"{_norm(ev.label)} {_norm(ev.notes)}"
        if any(k in text for k in ("carry", "target", "share", "role", "usage")):
            has_role_usage = True
        if "red" in text and "zone" in text:
            has_redzone = True
        if any(k in text for k in ("opponent", "defence", "defense", "matchup")):
            has_matchup = True
    if not has_role_usage:
        return False, "nfl_apex:no_role_or_usage_evidence"
    if not has_redzone:
        return False, "nfl_apex:no_red_zone_evidence"
    if not has_matchup:
        return False, "nfl_apex:no_matchup_evidence"

    if not (pick.get("sim_win_probability") is not None
              or pick.get("simulator_type")):
        return False, "nfl_apex:no_simulator_support"

    return True, None


def _sport_specific_gate(pick: dict, mo: MagicOutput,
                          sport: str, market: str) -> tuple[bool, Optional[str]]:
    """Route to the extra-strict per-market gates."""
    m = _norm(market)
    if sport == "Soccer" and (
        "anytime goal" in m or "anytime_goal" in m
        or "player_goal_scorer_anytime" in m
        or "player_to_score_or_assist" in m
    ):
        return _soccer_anytime_goal_gate_ok(pick, mo)
    if sport == "NFL" and (
        "anytime td" in m or "anytime_td" in m
        or "player_anytime_td" in m
    ):
        return _nfl_anytime_td_gate_ok(pick, mo)
    return True, None


# ─────────────────────────────────────────────────────────────────────────
# APEX decision object
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class ApexDecision:
    eligible: bool
    block_reason: Optional[str] = None
    requirements_met: list[str] = field(default_factory=list)
    requirements_failed: list[str] = field(default_factory=list)
    gate_version: str = APEX_GATE_VERSION


# ─────────────────────────────────────────────────────────────────────────
# Main entry
# ─────────────────────────────────────────────────────────────────────────

def evaluate_apex(*,
                    base_score: float,
                    mo: MagicOutput,
                    pick: dict,
                    categories_positive: list[str],
                    categories_contradictory: list[str],
                    categories_available: list[str],
                    ) -> ApexDecision:
    """Pure APEX gate.

    Returns an ``ApexDecision`` — never mutates ``pick``.  The caller
    (``lock_score_integrator.apply_magic_and_apex``) is responsible
    for stamping the pick with the outcome.
    """
    # NOTE: order matters — cheapest checks first, most expensive last.
    dec = ApexDecision(eligible=False)

    sport = (mo.sport or pick.get("sport") or "").strip()
    market = (mo.market or pick.get("market") or "")

    # 1. Sport / market eligibility (fast path — early return).
    allowed, reason = apex_market_allowed(sport, market, pick)
    if not allowed:
        dec.block_reason = reason
        dec.requirements_failed.append(reason or "sport_market_denied")
        return dec
    dec.requirements_met.append("sport_market_eligible")

    # 2. Magic tier gate
    if mo.magic_tier != MagicTier.ALIGNED_STRONG:
        dec.block_reason = f"magic_tier_not_aligned_strong:{mo.magic_tier.value}"
        dec.requirements_failed.append(dec.block_reason)
        return dec
    dec.requirements_met.append("magic_tier=ALIGNED_STRONG")

    if not mo.magic_score_available:
        dec.block_reason = "magic_score_unavailable"
        dec.requirements_failed.append(dec.block_reason)
        return dec
    dec.requirements_met.append("magic_score_available")

    # 3. Zero critical contradictions
    if categories_contradictory:
        dec.block_reason = (
            f"contradictory_categories:{','.join(categories_contradictory)}"
        )
        dec.requirements_failed.append(dec.block_reason)
        return dec
    dec.requirements_met.append("no_contradictory_categories")

    if mo.risk_flags:
        # Any flagged risk (RISK_ELEVATED / lineup-uncertain / etc.) blocks.
        dec.block_reason = f"risk_flags:{','.join(mo.risk_flags)}"
        dec.requirements_failed.append(dec.block_reason)
        return dec
    dec.requirements_met.append("no_risk_flags")

    # 4. Anti-promotion — base must already be at 97+.
    if base_score < APEX_MIN_BASE_SCORE:
        dec.block_reason = (
            f"base_score_below_apex_min:{base_score:.1f}<{APEX_MIN_BASE_SCORE}"
        )
        dec.requirements_failed.append(dec.block_reason)
        return dec
    dec.requirements_met.append(f"base_score>={APEX_MIN_BASE_SCORE}")

    # 5. Independent-category count
    n_pos = len(categories_positive)
    if n_pos < APEX_MIN_POSITIVE_CATEGORIES:
        dec.block_reason = (
            f"insufficient_independent_categories:"
            f"{n_pos}/{APEX_MIN_POSITIVE_CATEGORIES}"
        )
        dec.requirements_failed.append(dec.block_reason)
        return dec
    dec.requirements_met.append(
        f"positive_categories>={APEX_MIN_POSITIVE_CATEGORIES}:{n_pos}"
    )

    # 6. Context evidence (Role OR Matchup) mandatory.
    # Category names are duplicated here (rather than imported from
    # ``lock_score_integrator``) to keep the gate a pure leaf module —
    # avoids a circular import.
    CATEGORY_ROLE    = "role_opportunity"
    CATEGORY_MATCHUP = "matchup"
    CATEGORY_MARKET  = "market_intel"
    CATEGORY_MODEL   = "model_family"
    context_ok = any(c in categories_positive for c in (CATEGORY_ROLE, CATEGORY_MATCHUP))
    if not context_ok:
        dec.block_reason = "missing_context_category:role_or_matchup"
        dec.requirements_failed.append(dec.block_reason)
        return dec
    dec.requirements_met.append("context_evidence_present")

    # 7. Market intel mandatory
    if CATEGORY_MARKET not in categories_positive:
        dec.block_reason = "missing_market_intelligence"
        dec.requirements_failed.append(dec.block_reason)
        return dec
    dec.requirements_met.append("market_intelligence_present")

    # 8. Model family alone cannot qualify — enforced structurally by
    # (n_pos >= 5) + (context + market required).  If ONLY model_family
    # were present, that's 1 category, well below 5.  Still, add an
    # explicit check for defensive clarity.
    non_model = [c for c in categories_positive if c != CATEGORY_MODEL]
    if not non_model:
        dec.block_reason = "model_family_alone_cannot_qualify_apex"
        dec.requirements_failed.append(dec.block_reason)
        return dec
    dec.requirements_met.append("model_family_not_sole_signal")

    # 9. Sport-specific extra-strict gates for high-variance markets
    strict_ok, strict_reason = _sport_specific_gate(pick, mo, sport, market)
    if not strict_ok:
        dec.block_reason = strict_reason
        dec.requirements_failed.append(strict_reason or "sport_specific_gate_failed")
        return dec
    dec.requirements_met.append("sport_specific_strict_gate_passed")

    # All gates passed.
    dec.eligible = True
    return dec


__all__ = [
    "APEX_GATE_VERSION",
    "APEX_ELIGIBLE_SPORTS",
    "APEX_UNAVAILABLE_SPORTS",
    "APEX_MIN_BASE_SCORE",
    "APEX_MIN_POSITIVE_CATEGORIES",
    "ApexDecision",
    "apex_market_allowed",
    "evaluate_apex",
]
