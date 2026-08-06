"""Magic Tier post-processing policy — Phase 4E.3.

WRAPPER around the existing tier system (Apex / Elite / Strong /
Lock / Playable / Pass).  This module does **NOT** replace the
legacy tier assignment — it evaluates each pick after grading and
returns a capped/downgraded final tier based on data-quality,
sample-size, stale-odds, lineup-certainty, and calibration signals.

Design rules (per user spec 2026-08-06):

  * NEVER upgrades a tier — only caps or downgrades.
  * ``posterior_uncertainty`` from Phase 4B is NOT counted as an
    independent model vote.  It contributes only to a *stability*
    signal, not to model_agreement.
  * Weak data quality caps.
  * Small sample size caps.
  * Missing lineup / starter certainty caps (soccer + basketball).
  * Stale odds cap or block.
  * One inflated feature cannot dominate the tier.
  * Magic Tier must not simply mirror Lock Score — thresholds are
    applied on the joint (Lock Score × Data Quality × Sample) plane.
  * Magic Tier is NEVER presented as guaranteed win probability.

Frontend schema is unchanged.  We add an internal ``magic_tier`` field
alongside the existing ``grade`` / ``tier_v2`` fields so the
frontend can continue reading whatever it currently reads.  If the
policy caps a pick, we DO write back the capped label into ``grade``
(the primary user-facing tier) so users don't see "Elite Lock" when
the policy says the data doesn't support it — that was the whole
point of the audit.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional, Any

logger = logging.getLogger("lockscore.magic_tier_policy")

# ── Tier ranks (mirrors services.tennis_data_quality) ────────────────
_TIER_RANK = {
    "Pass":        0,
    "Playable":    1,
    "Lock":        2,
    "Strong Lock": 3,
    "Elite Lock":  4,
    "Apex Lock":   5,
}

_RANK_TO_TIER = {v: k for k, v in _TIER_RANK.items()}


def tier_rank(tier: str) -> int:
    return _TIER_RANK.get(tier or "", 0)


def tier_from_rank(r: int) -> str:
    r = max(0, min(r, max(_RANK_TO_TIER)))
    return _RANK_TO_TIER[r]


def cap_tier(current: str, cap: str) -> str:
    """Return whichever of ``current`` / ``cap`` is weaker."""
    return current if tier_rank(current) <= tier_rank(cap) else cap


# ── Policy configuration (per-sport overridable) ─────────────────────
@dataclass
class MagicTierConfig:
    # Data-quality caps
    min_signals_for_apex: int = 4
    min_signals_for_elite: int = 3
    min_signals_for_strong: int = 2

    # Sample-size caps (settled historical picks in this sport×market).
    min_sample_for_apex: int = 200
    min_sample_for_elite: int = 100
    min_sample_for_strong: int = 30

    # Odds freshness cap (seconds since snapshot).
    stale_odds_cap_seconds: int = 30 * 60      # 30m → cap at Strong Lock
    block_odds_seconds:     int = 3 * 60 * 60  # 3h → block Elite+

    # Calibration gap thresholds (|predicted - historical|).
    max_calibration_gap_apex: float = 0.05     # 5pp
    max_calibration_gap_elite: float = 0.08
    max_calibration_gap_strong: float = 0.12

    # Simulator validity — posterior_uncertainty stability threshold.
    max_posterior_std_for_apex: float = 0.10


# Per-sport defaults.
_DEFAULT_CONFIGS: dict[str, MagicTierConfig] = {
    "MLB":    MagicTierConfig(),
    "NBA":    MagicTierConfig(),
    "NFL":    MagicTierConfig(min_sample_for_apex=150, min_sample_for_elite=75),
    "CFB":    MagicTierConfig(min_sample_for_apex=100, min_sample_for_elite=50),
    "Tennis": MagicTierConfig(min_sample_for_apex=100, min_sample_for_elite=50),
    "Soccer": MagicTierConfig(),
}


def _config_for(sport: str) -> MagicTierConfig:
    return _DEFAULT_CONFIGS.get((sport or "").strip().upper(),
                                 _DEFAULT_CONFIGS.get(sport, MagicTierConfig()))


# ── Signal extraction ────────────────────────────────────────────────
def _extract_data_quality_signals(pick: dict) -> dict:
    """Pull DQ signals off a pick regardless of sport-specific dict."""
    tennis = pick.get("tennis_components") or {}
    # NBA / CFB precompute markers.
    ctx = pick.get("_ctx") or {}
    signals_present = 0

    # Tennis (Phase 4E.1 fields)
    if isinstance(tennis.get("data_quality_signal_count"), (int, float)):
        signals_present = max(signals_present, int(tennis["data_quality_signal_count"]))
    # Generic factor sources (MLB / NBA / CFB / Soccer / NFL live wiring)
    factor_sources = pick.get("factor_sources") or pick.get("real_factors_sources") or []
    if isinstance(factor_sources, list):
        signals_present = max(signals_present, len(factor_sources))

    # Identity / lineup markers
    identity_source = (
        tennis.get("identity_source")
        or (pick.get("tennis_identity") or {}).get("identity_source")
        or pick.get("player_identity_source")
        or "unknown"
    )
    stable_identity = bool(
        tennis.get("stable_identity")
        or (pick.get("tennis_identity") or {}).get("stable_identity")
        or pick.get("player_identity_stable")
        or False
    )

    # Explicit sport-provided caps (advisory).
    provided_cap = (
        tennis.get("data_quality_max_tier")
        or pick.get("scorer_eligibility_max_tier")
        or None
    )

    return {
        "signals_present":  signals_present,
        "identity_source":  identity_source,
        "stable_identity":  stable_identity,
        "provided_cap":     provided_cap,
    }


def _extract_sample_size(pick: dict) -> Optional[int]:
    """How many historical settled picks back this sport×market bucket?"""
    # Prefer explicit calibration bucket size when present.
    for key in ("sample_size", "calibration_sample_size", "n_settled"):
        v = pick.get(key)
        if isinstance(v, int) and v >= 0:
            return v
    lc = pick.get("lock_calibration") or {}
    if isinstance(lc.get("sample_size"), int):
        return lc["sample_size"]
    return None


def _extract_odds_freshness_seconds(pick: dict) -> Optional[float]:
    """Seconds elapsed since the pick's odds snapshot.  None = unknown."""
    for key in ("odds_snapshot_at", "odds_captured_at", "line_captured_at"):
        v = pick.get(key)
        if not v:
            continue
        try:
            if isinstance(v, str):
                dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
            elif isinstance(v, datetime):
                dt = v
            else:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - dt).total_seconds()
        except Exception:
            continue
    return None


def _extract_calibration_gap(pick: dict) -> Optional[float]:
    """|predicted probability - historical hit-rate for this bucket|."""
    lc = pick.get("lock_calibration") or {}
    gap = lc.get("calibration_gap")
    if isinstance(gap, (int, float)):
        return float(gap)
    return None


def _extract_posterior_std(pick: dict) -> Optional[float]:
    """Posterior standard deviation from Phase 4B simulator.

    IMPORTANT: this is NOT an independent model vote.  It only tells
    us HOW WIDE the posterior is around the model's predicted mean.
    A tight posterior means the deterministic scorer is *stable* on
    this input, not that a second model agrees.
    """
    sim = pick.get("simulator") or pick.get("posterior_uncertainty") or {}
    for k in ("posterior_std", "std", "sim_std"):
        v = sim.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    return None


def _extract_lineup_certainty(pick: dict) -> str:
    """Return one of ``confirmed`` / ``projected`` / ``bench`` /
    ``doubt`` / ``out`` / ``unknown``.
    """
    # Scorer eligibility (Phase 4E.2) is authoritative when present.
    scorer = pick.get("scorer_eligibility") or {}
    if scorer.get("lineup_status"):
        return scorer["lineup_status"]
    # Generic lineup marker.
    return (pick.get("lineup_status") or "unknown").lower()


# ── Decision core ────────────────────────────────────────────────────
@dataclass
class MagicTierDecision:
    """Explains what the policy did to a pick.  Attached as
    ``pick["magic_tier"]`` — internal field, NOT surfaced by the FE."""
    original_tier:  str
    magic_tier:     str
    capped:         bool
    caps_applied:   list[str] = field(default_factory=list)
    reasons:        list[str] = field(default_factory=list)
    signals:        dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def _cap(current: str, new_cap: str, name: str,
         caps_applied: list[str]) -> str:
    """Apply ``new_cap`` if it's stricter; log which cap fired."""
    r_before = tier_rank(current)
    result = cap_tier(current, new_cap)
    if tier_rank(result) < r_before:
        caps_applied.append(f"{name}->{new_cap}")
    return result


def evaluate_magic_tier(pick: dict, sport: Optional[str] = None,
                        config: Optional[MagicTierConfig] = None) -> MagicTierDecision:
    """Evaluate & cap the Magic Tier for a single pick.

    Returns a ``MagicTierDecision``.  The caller is responsible for
    writing ``decision.magic_tier`` back to ``pick["grade"]`` (or
    keeping it internal-only) — this function does not mutate.
    """
    if config is None:
        config = _config_for(sport or pick.get("sport") or "")
    sport = (sport or pick.get("sport") or "").strip()

    # Establish the starting tier.  Grade > tier_v2 > "Playable".
    original = (
        pick.get("grade") or pick.get("tier_v2") or pick.get("tier")
        or "Playable"
    )
    current = original
    caps_applied: list[str] = []
    reasons: list[str] = []

    dq = _extract_data_quality_signals(pick)
    sample_n = _extract_sample_size(pick)
    freshness = _extract_odds_freshness_seconds(pick)
    cal_gap = _extract_calibration_gap(pick)
    post_std = _extract_posterior_std(pick)
    lineup = _extract_lineup_certainty(pick)

    signals = {
        "signals_present":       dq["signals_present"],
        "identity_source":       dq["identity_source"],
        "stable_identity":       dq["stable_identity"],
        "provided_cap":          dq["provided_cap"],
        "sample_size":           sample_n,
        "odds_freshness_sec":    freshness,
        "calibration_gap":       cal_gap,
        "posterior_std":         post_std,
        "lineup_certainty":      lineup,
    }

    # ── (1) Sport-provided cap wins first (tennis DQ / scorer elig.) ──
    if dq.get("provided_cap"):
        current = _cap(current, dq["provided_cap"], "sport_dq_cap", caps_applied)
        reasons.append(f"sport_dq_cap={dq['provided_cap']}")

    # ── (2) Data-quality signal count caps ──────────────────────────
    n = int(dq["signals_present"] or 0)
    if n < config.min_signals_for_strong:
        current = _cap(current, "Lock", "signals<{}".format(config.min_signals_for_strong), caps_applied)
        reasons.append(f"signals_present={n}<{config.min_signals_for_strong}")
    elif n < config.min_signals_for_elite:
        current = _cap(current, "Strong Lock", "signals<{}".format(config.min_signals_for_elite), caps_applied)
    elif n < config.min_signals_for_apex:
        current = _cap(current, "Elite Lock", "signals<{}".format(config.min_signals_for_apex), caps_applied)

    # ── (3) Sample-size caps ────────────────────────────────────────
    if sample_n is not None:
        if sample_n < config.min_sample_for_strong:
            current = _cap(current, "Lock", "small_sample", caps_applied)
            reasons.append(f"sample_size={sample_n}<{config.min_sample_for_strong}")
        elif sample_n < config.min_sample_for_elite:
            current = _cap(current, "Strong Lock", "sample<elite_min", caps_applied)
        elif sample_n < config.min_sample_for_apex:
            current = _cap(current, "Elite Lock", "sample<apex_min", caps_applied)

    # ── (4) Stale odds ──────────────────────────────────────────────
    if isinstance(freshness, (int, float)):
        if freshness >= config.block_odds_seconds:
            current = _cap(current, "Lock", "odds_very_stale", caps_applied)
            reasons.append(f"odds_freshness={int(freshness)}s>=block_threshold")
        elif freshness >= config.stale_odds_cap_seconds:
            current = _cap(current, "Strong Lock", "odds_stale", caps_applied)

    # ── (5) Lineup certainty caps ───────────────────────────────────
    if lineup == "out":
        current = "Pass"
        caps_applied.append("player_out->Pass")
        reasons.append("player_ruled_out")
    elif lineup == "bench":
        current = _cap(current, "Playable", "bench_player", caps_applied)
        reasons.append("bench_player_cap")
    elif lineup == "doubt":
        current = _cap(current, "Lock", "lineup_doubt", caps_applied)
    elif lineup == "projected":
        current = _cap(current, "Strong Lock", "projected_lineup", caps_applied)
    elif lineup == "unknown" and sport.upper() in ("SOCCER", "NBA", "NFL", "CFB"):
        # Only sports where lineup matters — MLB has its own gates,
        # Tennis handled by identity/DQ path.
        current = _cap(current, "Strong Lock", "lineup_unknown", caps_applied)

    # ── (6) Calibration-gap caps ────────────────────────────────────
    if isinstance(cal_gap, (int, float)):
        if cal_gap > config.max_calibration_gap_strong:
            current = _cap(current, "Playable", "cal_gap_huge", caps_applied)
        elif cal_gap > config.max_calibration_gap_elite:
            current = _cap(current, "Lock", "cal_gap_wide", caps_applied)
        elif cal_gap > config.max_calibration_gap_apex:
            current = _cap(current, "Strong Lock", "cal_gap_moderate", caps_applied)

    # ── (7) Posterior std (Phase 4B) — stability signal only ────────
    if isinstance(post_std, (int, float)):
        if post_std > config.max_posterior_std_for_apex:
            # DO NOT count this as a model-disagreement vote.
            current = _cap(current, "Elite Lock", "wide_posterior", caps_applied)
            reasons.append(f"posterior_std={post_std:.3f}>apex_threshold")

    # ── (8) Identity fallback (tennis / soccer) ─────────────────────
    if not dq["stable_identity"] and dq["identity_source"] == "name_fallback":
        current = _cap(current, "Strong Lock", "name_fallback_identity", caps_applied)
        reasons.append("identity=name_fallback")

    capped = tier_rank(current) < tier_rank(original)
    return MagicTierDecision(
        original_tier=original,
        magic_tier=current,
        capped=capped,
        caps_applied=caps_applied,
        reasons=reasons,
        signals=signals,
    )


def apply_magic_tier(pick: dict, sport: Optional[str] = None,
                     write_back: bool = True) -> MagicTierDecision:
    """Convenience wrapper — evaluate and optionally write the capped
    tier back to ``pick["grade"]`` and stash the decision under
    ``pick["magic_tier"]`` (internal field).  Returns the decision.
    """
    d = evaluate_magic_tier(pick, sport=sport)
    if write_back:
        pick["magic_tier"] = d.to_dict()
        if d.capped:
            pick["grade"] = d.magic_tier
    return d


__all__ = [
    "MagicTierConfig", "MagicTierDecision",
    "evaluate_magic_tier", "apply_magic_tier",
    "tier_rank", "tier_from_rank", "cap_tier",
]
