"""
UNIVERSAL EVIDENCE AUTHORITY — SPORT ADAPTERS (2026-06)
========================================================

Each adapter reads EXISTING pick / factor data emitted by the frozen
sport-specific models and produces an ``EvidenceAuthorityContract``.
Adapters must NEVER invent evidence, downgrade legitimate data, or
recompute win probability — they only reshape what is already there.

Sports in scope: MLB · NFL · CFB · SOCCER · TENNIS.

The adapter dispatch is:
    build_contract_for_pick(pick, factors, scoring_factors) ->
        EvidenceAuthorityContract | None
Returning ``None`` means "this pick's sport / market isn't handled by
UEA" (e.g. NBA / NHL / UFC).  Callers should fall back to legacy
authority in that case.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from .evidence_authority_contract import (
    EvidenceAuthorityContract,
    EvidenceStatus,
    EvidenceValue,
    enabled as uea_enabled,
    UEA_VERSION,
)

# ─────────────────────────────────────────────────────────────────────
# small helpers
# ─────────────────────────────────────────────────────────────────────
def _num(x: Any) -> Optional[float]:
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        try:
            return float(x)
        except Exception:
            return None
    return None


def _pct_to_100(v: Optional[float]) -> Optional[float]:
    """Normalize ``v`` into a 0..100 quality score.  Accepts 0..1 or
    0..100 inputs; returns None if input is None."""
    if v is None:
        return None
    if v <= 1.5:
        v = v * 100.0
    return max(0.0, min(100.0, float(v)))


def _first_present(d: Mapping[str, Any], keys) -> Optional[float]:
    for k in keys:
        v = _num(d.get(k)) if isinstance(d, Mapping) else None
        if v is not None:
            return v
    return None


# ─────────────────────────────────────────────────────────────────────
# 1. MODEL PROBABILITY axis — same for every sport
# ─────────────────────────────────────────────────────────────────────
def _model_probability_axis(pick: Mapping[str, Any]) -> EvidenceValue:
    """Read the calibrated model probability of the exact wager and
    convert to axis quality in [0, 100].  MISSING when the model
    couldn't produce a real prob (book-implied seed does NOT count
    as a legitimate model probability)."""
    prov = str(pick.get("probability_provenance") or "").upper()
    if prov in ("BOOK_IMPLIED", "PRIOR_ONLY", "INVALID"):
        return EvidenceValue.missing(f"provenance={prov or 'none'}")
    # NFL Prop Authority already ran — trust its win-probability.
    wp = _num(pick.get("nfl_prop_authority_wp"))
    if wp is not None and wp <= 1.5:
        wp *= 100.0
    if wp is None:
        wp = _num(pick.get("model_win_probability")) \
            or _num(pick.get("win_probability")) \
            or _num(pick.get("calibrated_win_probability"))
    if wp is None:
        wp = _num(pick.get("sim_win_probability"))
    if wp is None:
        return EvidenceValue.missing("no_model_probability")
    if wp <= 1.5:
        wp *= 100.0
    # Quality curve — a strong 80% WP is ~96; 90% ~99.
    if wp <= 50:  s = 55.0 * (wp / 50.0)
    elif wp <= 60: s = 55.0 + (wp - 50) * (19.0 / 10.0)
    elif wp <= 70: s = 74.0 + (wp - 60) * (12.0 / 10.0)
    elif wp <= 80: s = 86.0 + (wp - 70) * (10.0 / 10.0)
    elif wp <= 90: s = 96.0 + (wp - 80) * (3.0 / 10.0)
    else:         s = 99.0 + (wp - 90) * (1.0 / 10.0)
    return EvidenceValue.available_score(min(100.0, s), f"wp={wp:.1f}")


# ─────────────────────────────────────────────────────────────────────
# 2. RELIABILITY axis
# ─────────────────────────────────────────────────────────────────────
_PROV_QUALITY = {
    "PLATINUM":              98.0,
    "APEX":                  97.0,
    "CAUSAL_INDEPENDENT":    95.0,
    "MODEL_CONDITIONED":     92.0,
    "MODEL_FIRST":           90.0,
    "MODEL_BLENDED":         86.0,
    "PROP_MODEL_PRIMARY":    90.0,
    "PROP_MODEL_BLENDED":    84.0,
    "PROP_MODEL_HEURISTIC":  70.0,
    "BOOK_IMPLIED_CALIBRATED":78.0,
    "BOOK_IMPLIED":          60.0,
    "HEURISTIC":             55.0,
    "PRIOR_ONLY":            50.0,
}


def _reliability_axis(pick: Mapping[str, Any]) -> EvidenceValue:
    prov = str(pick.get("probability_provenance") or "").upper()
    if prov in _PROV_QUALITY:
        return EvidenceValue.available_score(
            _PROV_QUALITY[prov], f"provenance={prov}")
    # Fall back to simulator_provenance / sim confidence when present.
    sim_prov = str(pick.get("simulator_provenance") or "").upper()
    if sim_prov and sim_prov in _PROV_QUALITY:
        return EvidenceValue.available_score(
            _PROV_QUALITY[sim_prov], f"sim_provenance={sim_prov}")
    # Inferred reliability from downstream authority flags — the NFL
    # Prop Authority / MLB pick paths persist these when the scorer
    # ran with real evidence.
    if pick.get("nfl_prop_authority_applied") is True:
        return EvidenceValue.available_score(92.0, "nfl_prop_authority")
    if pick.get("magic_tier_at_integration") in ("STRONG", "ELITE",
                                                    "APEX", "PEAK"):
        return EvidenceValue.available_score(94.0,
            f"magic_tier={pick.get('magic_tier_at_integration')}")
    if pick.get("tier") == "PEAK_NON_APEX":
        return EvidenceValue.available_score(93.0, "tier=PEAK_NON_APEX")
    if pick.get("apex_lock") is True:
        return EvidenceValue.available_score(96.0, "apex_lock")
    if pick.get("identity_class") == "AUTHORITATIVE":
        return EvidenceValue.available_score(88.0, "identity=AUTHORITATIVE")
    return EvidenceValue.missing("no_provenance")


# ─────────────────────────────────────────────────────────────────────
# 3. HISTORY axis
# ─────────────────────────────────────────────────────────────────────
_HISTORY_HR_KEYS = (
    "exact_threshold_hit_rate", "historical_hit_rate",
    "at_or_over_hit_rate", "L10 Hit Rate", "Recent L10 Hit Rate",
    "L10_hit_rate", "l10_hit_rate", "hit_rate_at_line",
    # NFL/CFB Alt Prop scorer already persists these:
    "Historical Threshold Rate", "L5 Threshold Support",
    "L3 Threshold Support", "Threshold Distribution Support",
)


def _history_axis(pick: Mapping[str, Any],
                   factors: Optional[Mapping[str, Any]]) -> EvidenceValue:
    factors = factors or {}
    for k in _HISTORY_HR_KEYS:
        v = _num(pick.get(k)) if k in pick else _num(factors.get(k))
        if v is not None:
            if v > 1.5:
                v /= 100.0
            v = max(0.0, min(1.0, v))
            # Sample size sanity — very tiny n cannot be strong.
            n = _num(pick.get("history_sample_size")) \
                    or _num(factors.get("history_sample_size")) or 0.0
            base = 55.0 + (max(0.0, v - 0.30) * (43.0 / 0.70)) if v >= 0.30 \
                   else v * (55.0 / 0.30)
            if n and n < 4:
                base = min(base, 75.0)
            return EvidenceValue.available_score(
                base, f"hit_rate={v:.2f} n={int(n)}")
    n = _num(pick.get("history_sample_size")) or _num(factors.get("history_sample_size"))
    if n is not None and n >= 4:
        # Coarse fallback — sample present but rate not stashed.
        s = 74.0 if n >= 6 else 68.0
        if n >= 12: s = 80.0
        if n >= 20: s = 85.0
        return EvidenceValue.available_score(s, f"n={int(n)}")
    return EvidenceValue.missing("no_history")


# ─────────────────────────────────────────────────────────────────────
# 4. MATCHUP / ROLE axis
# ─────────────────────────────────────────────────────────────────────
_MATCHUP_KEYS = (
    "Matchup Advantage", "Matchup Rating", "Role Quality",
    "Opponent Defense", "Opp Defense", "Opp Bullpen",
    "Platoon Advantage", "SP+ Rating Δ (norm)", "SP+ Rating Δ",
    "matchup_score", "opp_defense_grade", "opp_xga_norm",
    "opp_ba_allowed", "opp_iso_allowed",
    "opp_shot_suppression", "opp_xga_per90_norm",
    # NFL / MLB / Soccer / Tennis scorers already persist these:
    "Home/Away Split", "Home/Away Splits",
    "Career vs Opponent Hit%", "Matchup History",
    "H2H Record", "H2H", "Injuries / Suspensions",
    "Defensive Rating", "Recent Volume / Usage",
    "Matchup vs Defense",
)


def _matchup_axis(pick: Mapping[str, Any],
                   factors: Optional[Mapping[str, Any]]) -> EvidenceValue:
    factors = factors or {}
    best = None
    for k in _MATCHUP_KEYS:
        v = _num(factors.get(k))
        if v is None:
            v = _num(pick.get(k))
        if v is None:
            continue
        if v > 1.5: v /= 100.0
        v = max(0.0, min(1.0, v))
        if best is None or v > best:
            best = v
    if best is None:
        # Role-only markers — starter/expected minutes are matchup context.
        role_ok = (pick.get("lineup_status") or {}).get("status") in (
            "CONFIRMED", "PROJECTED")
        if role_ok:
            return EvidenceValue.available_score(72.0, "role_only")
        return EvidenceValue.missing("no_matchup")
    s = 55.0 + (best - 0.30) * (43.0 / 0.70) if best >= 0.30 else \
        best * (55.0 / 0.30)
    return EvidenceValue.available_score(s, f"matchup={best:.2f}")


# ─────────────────────────────────────────────────────────────────────
# 5. CONVERGENCE axis — genuinely independent signals
# ─────────────────────────────────────────────────────────────────────
_CORRELATED_GROUPS = (
    # Elo family — all derivative
    ("surface_elo_norm", "overall_elo_norm", "elo_norm",
     "elo_diff_norm", "elo_delta_norm"),
    # SP+ family — all derivative
    ("SP+ Rating Δ (norm)", "SP+ Rating Δ", "sp_plus_rating_norm",
     "sp_plus_margin_norm"),
    # xG family — same base
    ("xg_norm", "xga_norm", "xg_diff_norm", "poisson_prob_norm"),
)

# Axes that are MARKET ANCHORS not independent evidence.  Sportsbook
# implied probability tells us what the market thinks — it is never
# counted as a separate confirming signal.  Model-vs-Market Δ is an
# edge measure, not an independent axis (already implied by the
# model + market pair).
_MARKET_ANCHOR_KEYS = {
    "Sportsbook Implied (norm)", "Book Implied (norm)",
    "Market Implied (norm)", "sportsbook_implied_norm",
    "Model-vs-Market Δ",  # edge, not independent axis
}


def _independent_convergence_axis(
    pick: Mapping[str, Any],
    factors: Optional[Mapping[str, Any]],
    scoring_factors: Optional[Mapping[str, float]],
) -> EvidenceValue:
    if not scoring_factors:
        return EvidenceValue.missing("no_scoring_factors")
    # Deduplicate correlated derivative signals — keep the strongest
    # single representative per correlated group.
    keep_map: dict[str, float] = {}
    used_groups: set[int] = set()
    for k, v in scoring_factors.items():
        vv = _num(v)
        if vv is None:
            continue
        if k in _MARKET_ANCHOR_KEYS:
            # Market anchors are context, not independent evidence.
            continue
        gid = None
        for idx, grp in enumerate(_CORRELATED_GROUPS):
            if k in grp:
                gid = idx
                break
        if gid is None:
            keep_map[k] = vv
            continue
        if gid in used_groups:
            existing_k = next(kk for kk in keep_map if kk in _CORRELATED_GROUPS[gid])
            if abs(vv) > abs(keep_map[existing_k]):
                del keep_map[existing_k]
                keep_map[k] = vv
        else:
            used_groups.add(gid)
            keep_map[k] = vv
    vals = list(keep_map.values())
    if len(vals) < 2:
        return EvidenceValue.missing(f"insufficient_independent_signals:{len(vals)}")
    # Normalize into [0, 1] direction-agnostic support (all axes are
    # expected to already reflect direction — support for the pick).
    supports = []
    for v in vals:
        if v > 1.5:
            v /= 100.0
        supports.append(max(0.0, min(1.0, v)))
    n = len(supports)
    agree = sum(1 for x in supports if x >= 0.55) / n
    mean_support = sum(supports) / n
    axis = 100.0 * (0.5 * agree + 0.5 * mean_support)
    return EvidenceValue.available_score(
        axis, f"axes={n} agree={agree:.2f} mean={mean_support:.2f}")


# ─────────────────────────────────────────────────────────────────────
# 6. DISTRIBUTION / SIMULATION axis
# ─────────────────────────────────────────────────────────────────────
def _distribution_axis(pick: Mapping[str, Any]) -> EvidenceValue:
    # Direct stability signal
    for k in ("sim_stability", "distribution_stability",
              "volatility_component"):
        v = _num(pick.get(k))
        if v is not None:
            s = _pct_to_100(v)
            if s is not None:
                return EvidenceValue.available_score(s, f"{k}={v:.2f}")
    # Legacy simulator fields
    for k in ("simulator_probability", "sim_win_probability",
              "monte_carlo_probability", "simulation_probability"):
        v = _num(pick.get(k))
        if v is not None:
            return EvidenceValue.available_score(
                75.0 + min(20.0, (abs((v if v > 1.5 else v * 100) - 50.0)) * 0.4),
                f"{k}_present")
    # Downstream simulator/component blocks — trust their presence
    # and any embedded stability metric.
    sim = pick.get("sim_result")
    if isinstance(sim, dict) and sim:
        stab = _num(sim.get("stability")) or _num(sim.get("simulation_stability"))
        if stab is not None:
            s = _pct_to_100(stab)
            if s is not None:
                return EvidenceValue.available_score(s, "sim_result.stability")
        return EvidenceValue.available_score(82.0, "sim_result_present")
    if pick.get("cfb_independent_sim"):
        stab = _num((pick["cfb_independent_sim"] or {}).get("stability"))
        s = _pct_to_100(stab) or 80.0
        return EvidenceValue.available_score(s, "cfb_indep_sim")
    if _num(pick.get("simulation_pass")) is not None:
        v = _num(pick.get("simulation_pass"))
        return EvidenceValue.available_score(min(100.0, max(60.0, v)),
            f"simulation_pass={v}")
    lc = pick.get("lock_components") or {}
    if isinstance(lc, dict) and lc.get("volatility") is not None:
        v = _num(lc.get("volatility"))
        if v is not None:
            return EvidenceValue.available_score(
                max(0.0, min(100.0, v)),
                "lock_components.volatility")
    return EvidenceValue.missing("no_simulation")


# ─────────────────────────────────────────────────────────────────────
# 7. DATA QUALITY axis
# ─────────────────────────────────────────────────────────────────────
def _data_quality_axis(pick: Mapping[str, Any],
                        factors: Optional[Mapping[str, Any]]) -> EvidenceValue:
    dq = pick.get("data_quality")
    if isinstance(dq, (int, float)) and not isinstance(dq, bool):
        v = float(dq)
        if v > 1.5:
            return EvidenceValue.available_score(max(0.0, min(100.0, v)), "dq_numeric")
        return EvidenceValue.available_score(v * 100.0, "dq_numeric")
    dq_str = str(dq or "").lower()
    if "returning_prod_both+portal_both" in dq_str:
        return EvidenceValue.available_score(99.0, dq_str)
    if "returning_prod_both" in dq_str or "portal_both" in dq_str:
        return EvidenceValue.available_score(95.0, dq_str)
    if "sp_plus" in dq_str or "elo_full" in dq_str:
        return EvidenceValue.available_score(92.0, dq_str)
    if "full" in dq_str:
        return EvidenceValue.available_score(94.0, dq_str)
    if "book_implied_calibrated" in dq_str:
        return EvidenceValue.available_score(80.0, dq_str)
    if "book_implied" in dq_str:
        return EvidenceValue.available_score(65.0, dq_str)
    if "heuristic" in dq_str:
        return EvidenceValue.available_score(55.0, dq_str)
    # NFL / MLB / Soccer scorers persist real_data_count / real_data_sources.
    rdc = _num(pick.get("real_data_count"))
    if rdc is not None and rdc >= 4:
        return EvidenceValue.available_score(min(97.0, 82.0 + rdc * 3),
            f"real_data_count={int(rdc)}")
    # Coarse fallback — factor count as a soft proxy.  Exclude market
    # anchors (they are context, not evidence) so a market-only factor
    # bag doesn't manufacture DQ.
    factors = factors or {}
    n = sum(1 for k, v in factors.items()
            if isinstance(v, (int, float)) and k not in _MARKET_ANCHOR_KEYS)
    if n >= 8:  return EvidenceValue.available_score(90.0, f"factors={n}")
    if n >= 5:  return EvidenceValue.available_score(85.0, f"factors={n}")
    if n >= 3:  return EvidenceValue.available_score(78.0, f"factors={n}")
    return EvidenceValue.missing("no_dq_signal")


# ─────────────────────────────────────────────────────────────────────
# 8. CONTRADICTIONS — pick up obvious integrity flaws
# ─────────────────────────────────────────────────────────────────────
def _contradictions(pick: Mapping[str, Any],
                     factors: Optional[Mapping[str, Any]]) -> list[str]:
    out: list[str] = []
    # Bench / scratched slipped through
    lu = (pick.get("lineup_status") or {})
    if str(lu.get("status") or "").upper() in ("BENCH", "SCRATCHED"):
        out.append(f"lineup:{lu.get('status')}")
    # Book-implied primary probability
    prov = str(pick.get("probability_provenance") or "").upper()
    if prov == "BOOK_IMPLIED":
        out.append("book_implied_primary")
    # Explicit mp_from_book_seed leakage
    if pick.get("mp_from_book_seed") is True:
        out.append("mp_from_book_seed")
    # Marked market_bad
    if pick.get("market_bad") is True:
        out.append("market_bad")
    # Contradiction if implied prob far above win prob (line moved
    # against us but we still claim edge).
    wp = _num(pick.get("win_probability"))
    fair = _num(pick.get("no_vig_implied_pct"))
    if wp is not None and fair is not None:
        wp_f = wp / 100.0 if wp > 1.5 else wp
        fair_f = fair / 100.0 if fair > 1.5 else fair
        if fair_f - wp_f >= 0.15:
            out.append("edge_negative_over_15pct")
    return out


# ─────────────────────────────────────────────────────────────────────
# 9. MARKET-FAMILY CLASSIFIER
# ─────────────────────────────────────────────────────────────────────
def _classify_market_family(sport: str, market: str) -> str:
    s = (sport or "").upper()
    m = (market or "").lower()
    if s == "NFL":
        if any(t in m for t in ("yards", "yds", "receptions",
                                  "completions", "attempts",
                                  "touchdowns", "tds", "atd",
                                  "anytime", "longest", "first td",
                                  "player ", "pass ", "rush ", "reception")):
            return "NFL_PLAYER"
        return "NFL_GAME"
    if s == "MLB":
        pitcher_hint = any(t in m for t in ("strikeouts", "outs", "walks",
                                              "earned runs", "pitcher"))
        if pitcher_hint:
            return "MLB_PITCHER"
        if any(t in m for t in ("hits", "home_runs", "hr", "total_bases",
                                  "runs", "rbi", "singles", "doubles",
                                  "triples", "stolen_bases", "player_")):
            return "MLB_HITTER"
        return "MLB_GAME"
    if s == "CFB":
        return "CFB_GAME" if any(t in m for t in (
            "moneyline", "spread", "total")) else "CFB_PLAYER"
    if s == "SOCCER":
        if any(t in m for t in ("goalscorer", "goal_scorer", "assist",
                                  "shots", "sot", "player_")):
            return "SOCCER_PLAYER"
        return "SOCCER_GAME"
    if s == "TENNIS":
        return "TENNIS"
    return "UNKNOWN"


# ─────────────────────────────────────────────────────────────────────
# 10. UNIVERSAL ADAPTER
# ─────────────────────────────────────────────────────────────────────
def build_contract_for_pick(
    pick: Mapping[str, Any],
    factors: Optional[Mapping[str, Any]] = None,
    scoring_factors: Optional[Mapping[str, float]] = None,
) -> Optional[EvidenceAuthorityContract]:
    """Sport-dispatching contract builder.  Returns ``None`` when
    the pick's sport isn't opted in (NBA / NHL / UFC etc)."""
    sport = str(pick.get("sport") or "").upper()
    if not uea_enabled(sport):
        return None
    market_family = _classify_market_family(sport, pick.get("market") or "")

    contract = EvidenceAuthorityContract(
        model_probability=          _model_probability_axis(pick),
        prediction_reliability=     _reliability_axis(pick),
        history_threshold_support=  _history_axis(pick, factors),
        matchup_role_support=       _matchup_axis(pick, factors),
        independent_convergence=    _independent_convergence_axis(pick, factors, scoring_factors),
        simulation_distribution_support= _distribution_axis(pick),
        data_quality=               _data_quality_axis(pick, factors),
        contradictions=             _contradictions(pick, factors),
        sport=                      sport,
        market_family=              market_family,
        adapter_version=            UEA_VERSION,
    )
    # Tennis without a distribution simulator legitimately marks that
    # axis NOT_APPLICABLE (Elo/serve/return already carry it) — this
    # keeps coverage denominator honest.
    if sport == "TENNIS" and not contract.simulation_distribution_support.available:
        contract.simulation_distribution_support = EvidenceValue.not_applicable(
            "tennis_no_dist_sim")
    return contract


__all__ = [
    "build_contract_for_pick",
    "_classify_market_family",
]
