"""Parlay ModePolicy — ONE authoritative mode contract
=============================================================

P0 Root Closure — Parlay 3.0 (2026-06-21).

Before this module: the same conceptual "mode" (HIGH_RISK etc.) had
DIFFERENT thresholds across:
  * route admission               (routes/parlay_routes.py)
  * optimizer eligibility          (parlay_optimizer.py)
  * intelligence profile           (services/parlay_intelligence/parlay_modes.py)
  * damage-control budget          (parlay_optimizer.py MAX_REL_DROP_*)

That drift is exactly why HIGH_RISK could admit at route level, then
starve inside the optimizer, then produce zero tickets with no
truthful diagnostic.

``ModePolicy`` is the SINGLE source of truth.  Every layer that makes
a mode-sensitive decision imports the same policy dataclass.

Rules
-----
* STANDARD: strongest compact general-purpose ticket, 2–5 legs.
* ADVANCED_SAFER: strictest lock floor, prioritise survival.
* ADVANCED_HIGH_EV: positive-edge gate, EV-weighted selection, 2–6 legs.
* HIGH_RISK: 10–20 legs, larger variance BUT never garbage.

The policy is READ-ONLY.  Never mutate a returned ModePolicy — use
``dataclasses.replace()`` for what-if experiments.
"""
from __future__ import annotations

from dataclasses import dataclass, replace, field
from typing import Optional, Dict


@dataclass(frozen=True)
class ModePolicy:
    """Immutable per-mode admission / ranking / risk contract."""
    # Machine ID + human labels
    key: str
    display_name: str
    intelligence_profile: str      # maps to parlay_intelligence "safe/balanced/aggressive"

    # Ticket size
    min_target_legs: int
    max_target_legs: int
    default_target_legs: int
    min_useful_legs: int           # partial-success floor (don't return 0)

    # Leg admission
    lock_floor: float              # min published_lock_score to admit
    min_edge_pct: Optional[float]  # None ⇒ edge is a RANKING input, not a gate
    positive_edge_required: bool

    # Risk budget (damage control) — see damage_control_ok()
    max_relative_drop_per_leg: float  # relative survival reduction allowed
    max_absolute_drop_per_leg: float  # absolute pp survival reduction allowed

    # Same-sport / same-event / market-family safety
    same_sport_soft_cap_ratio: float  # e.g. 0.4 = max 40 % same sport
    max_same_market_family: int
    same_event_hard_block_sports: tuple = (
        # dependent sports where SGP would need a real joint model
        "mlb", "ufc", "tennis", "nba", "nfl", "nhl",
    )

    # Health-score weighting override (0..1).  Values sum to 1.0 within
    # the health function.  HIGH_RISK deprioritises raw survival because
    # a 15-leg ticket naturally has low survival — it's the ticket goal.
    health_weights: Dict[str, float] = field(default_factory=lambda: {
        "survival": 0.35, "edge": 0.25, "roi": 0.20,
        "correlation": 0.10, "stability": 0.10,
    })


# ─── Concrete policies ──────────────────────────────────────────────────

STANDARD = ModePolicy(
    key="standard",
    display_name="Standard",
    intelligence_profile="balanced",
    min_target_legs=2,
    max_target_legs=8,
    default_target_legs=3,
    min_useful_legs=2,
    lock_floor=85.0,
    min_edge_pct=None,             # edge is a ranking input, not a gate
    positive_edge_required=False,
    max_relative_drop_per_leg=0.25,
    max_absolute_drop_per_leg=0.15,
    same_sport_soft_cap_ratio=0.50,
    max_same_market_family=2,
)


ADVANCED_SAFER = ModePolicy(
    key="advanced_safer",
    display_name="Advanced · Safer",
    intelligence_profile="safe",
    min_target_legs=2,
    max_target_legs=4,
    default_target_legs=3,
    min_useful_legs=2,
    lock_floor=92.0,
    min_edge_pct=None,
    positive_edge_required=False,
    max_relative_drop_per_leg=0.20,
    max_absolute_drop_per_leg=0.12,
    same_sport_soft_cap_ratio=0.40,
    max_same_market_family=2,
    health_weights={
        "survival": 0.45, "edge": 0.15, "roi": 0.15,
        "correlation": 0.15, "stability": 0.10,
    },
)


ADVANCED_HIGH_EV = ModePolicy(
    key="advanced_high_ev",
    display_name="Advanced · High EV",
    intelligence_profile="balanced",
    min_target_legs=2,
    max_target_legs=6,
    default_target_legs=3,
    min_useful_legs=2,
    lock_floor=85.0,
    min_edge_pct=0.0,               # explicit positive-edge gate
    positive_edge_required=True,
    max_relative_drop_per_leg=0.28,
    max_absolute_drop_per_leg=0.18,
    same_sport_soft_cap_ratio=0.50,
    max_same_market_family=2,
    health_weights={
        "survival": 0.25, "edge": 0.40, "roi": 0.20,
        "correlation": 0.08, "stability": 0.07,
    },
)


HIGH_RISK = ModePolicy(
    key="high_risk",
    display_name="High Risk",
    intelligence_profile="aggressive",
    min_target_legs=5,
    max_target_legs=20,
    default_target_legs=10,
    min_useful_legs=5,               # partial: 5-of-10 target is a valid card
    lock_floor=70.0,                 # lowest admission floor
    # ── Universal Closure (2026-06-21): HIGH_RISK is a HIGH-VARIANCE /
    # HIGH-PAYOUT mode, NOT an EV-oriented mode.  ADVANCED_HIGH_EV
    # explicitly exists for EV.  Edge is now used as a RANKING input
    # (via score_leg's edge_component), not as a hard admission gate.
    # The old +1 % edge veto starved legitimate low-edge high-lock
    # chalk-ladder alts that HIGH_RISK should be free to combine.
    min_edge_pct=None,
    positive_edge_required=False,
    max_relative_drop_per_leg=0.55,  # ← bigger risk budget than before
    max_absolute_drop_per_leg=0.35,  # ← was 0.30, expanded so 12+ legs feasible
    same_sport_soft_cap_ratio=0.34,  # max 33-34 % same sport on 15-leg ticket
    max_same_market_family=3,        # allow one extra family for 15+ leg builds
    health_weights={
        # Raw survival naturally falls with leg count in HIGH_RISK — weight
        # ticket QUALITY (edge/ROI/diversification/stability) higher so
        # health doesn't auto-punish 15-leg builds for being 15 legs.
        "survival": 0.15, "edge": 0.30, "roi": 0.20,
        "correlation": 0.15, "stability": 0.20,
    },
)


TODAY_WINDOW = ModePolicy(
    key="today_window",
    display_name="Today · 1-5h",
    intelligence_profile="safe",
    min_target_legs=2,
    max_target_legs=4,
    default_target_legs=3,
    min_useful_legs=2,
    lock_floor=85.0,
    min_edge_pct=None,
    positive_edge_required=False,
    max_relative_drop_per_leg=0.22,
    max_absolute_drop_per_leg=0.14,
    same_sport_soft_cap_ratio=0.50,
    max_same_market_family=2,
)


_BY_KEY: Dict[str, ModePolicy] = {
    p.key: p for p in (STANDARD, ADVANCED_SAFER, ADVANCED_HIGH_EV, HIGH_RISK, TODAY_WINDOW)
}


def resolve_mode(mode: Optional[str], *, advanced_sub: str = "ev",
                 window_hours: int = 24) -> ModePolicy:
    """Map the (legacy) route query parameters onto a canonical ModePolicy.

    Callers should pass the query-string ``mode`` verbatim; this function
    handles legacy aliases (``high_risk`` / ``high-risk`` / ``lottery``
    ⇒ HIGH_RISK, ``today_window`` OR ``window_hours<=8`` ⇒ TODAY_WINDOW).
    """
    m = (mode or "").strip().lower().replace("-", "_")
    if m in ("high_risk", "highrisk", "lottery"):
        return HIGH_RISK
    if m in ("today_window", "today"):
        return TODAY_WINDOW
    # 2026-06 legacy: `today` was expressed as a window override.
    if m == "" and int(window_hours or 24) <= 8:
        return TODAY_WINDOW
    if m == "advanced":
        sub = (advanced_sub or "ev").strip().lower()
        return ADVANCED_SAFER if sub == "safer" else ADVANCED_HIGH_EV
    # standard / any unknown alias
    return STANDARD


def clamp_target_legs(policy: ModePolicy, requested: Optional[int]) -> int:
    """Clamp the caller's requested leg target to the policy band."""
    try:
        v = int(requested) if requested is not None else policy.default_target_legs
    except (TypeError, ValueError):
        v = policy.default_target_legs
    return max(policy.min_target_legs, min(policy.max_target_legs, v))


__all__ = [
    "ModePolicy",
    "STANDARD", "ADVANCED_SAFER", "ADVANCED_HIGH_EV", "HIGH_RISK", "TODAY_WINDOW",
    "resolve_mode", "clamp_target_legs",
]
