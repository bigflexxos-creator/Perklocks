"""
CFB Enriched Challenger — Shadow Distribution Module
=====================================================

READ-ONLY, additive challenger for CFB probability estimation.
NEVER wired into live scoring unless the backtest proves it beats
the SP+-only Champion.

Champion (current live path — see ``services/cfb_game_model``):
    expected_margin  = SP+ Δrating + HFA
    p_home_ml        = logistic(0.10 × expected_margin)

Challenger blends the Champion projection with real advanced-stats
signals when available:
    • Offensive / defensive EPA / PPA
    • Success rate (rush / pass / standard-downs / passing-downs)
    • Explosiveness
    • Havoc / defensive disruption
    • Points-per-scoring-opportunity (finishing drives)

Data-provenance ladder:
    OBSERVED   — full advanced_stats row for the team
    EMPIRICAL  — partial (some fields present)
    PRIOR_ONLY — only SP+ / returning_prod available
    MISSING    — no context at all

When PRIOR_ONLY or MISSING, the challenger falls back to Champion
projection and marks itself thin-authority so downstream Lock scoring
cannot inflate.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import exp
from typing import Optional


HFA_POINTS = 2.5     # matches Champion
MARGIN_K   = 0.10    # matches Champion


def _logistic(x: float) -> float:
    return 1.0 / (1.0 + exp(-x))


def _team_key(name: str) -> str:
    return (name or "").strip().lower()


@dataclass
class CfbChallengerResult:
    available: bool
    p_home_ml: Optional[float] = None
    expected_margin: Optional[float] = None
    reason: Optional[str] = None
    # Real evidence factors — surfaced 1:1 to the pick explainer.
    factors: dict = field(default_factory=dict)
    provenance: dict = field(default_factory=dict)


def _lookup(m: dict, name: str, alt_keys: Optional[list[str]] = None):
    if not m or not name:
        return None
    n = _team_key(name)
    if n in m:
        return m[n]
    # explicit alias trims (matches cfb_game_model style — safe)
    for stop in (" horned frogs", " tar heels", " fighting irish",
                 " crimson tide", " tigers", " bulldogs", " wildcats",
                 " ducks", " sooners", " longhorns", " aggies",
                 " gators", " seminoles", " hurricanes", " volunteers",
                 " commodores", " gamecocks", " razorbacks", " rebels",
                 " cougars", " utes", " buffaloes", " golden bears",
                 " trojans", " bruins", " wolverines", " spartans",
                 " hoosiers", " boilermakers", " badgers", " gophers",
                 " hawkeyes", " cyclones", " jayhawks", " wildcat"):
        if n.endswith(stop):
            t = n[: -len(stop)]
            if t in m:
                return m[t]
    return None


def _safe(v, default=0.0):
    try:
        f = float(v)
        return f if f == f else default   # NaN-safe
    except (TypeError, ValueError):
        return default


def _epa_margin_delta(h_adv: Optional[dict], a_adv: Optional[dict]) -> float:
    """Convert advanced-stats deltas into an expected-margin adjustment.

    The mapping is intentionally CONSERVATIVE — an EPA delta of 0.10
    translates to ~2 pts of margin, capped ±4 pts total.  This
    respects the "signals may raise OR lower" directive without
    hijacking Champion's calibrated 10-pt base.
    """
    if not h_adv and not a_adv:
        return 0.0
    delta = 0.0
    # (a) off_ppa vs opponent def_ppa (points allowed per play)
    h_off = _safe((h_adv or {}).get("off_ppa"))
    a_def = _safe((a_adv or {}).get("def_ppa"))
    a_off = _safe((a_adv or {}).get("off_ppa"))
    h_def = _safe((h_adv or {}).get("def_ppa"))
    if any((h_off, a_def, a_off, h_def)):
        # Positive = home offense produces more EPA than opponent allows.
        home_edge = (h_off - a_def) * 12.0
        away_edge = (a_off - h_def) * 12.0
        delta += (home_edge - away_edge)
    # (b) success rate delta on offense
    h_sr = _safe((h_adv or {}).get("off_success_rate"))
    a_sr = _safe((a_adv or {}).get("off_success_rate"))
    if h_sr or a_sr:
        delta += (h_sr - a_sr) * 8.0
    # (c) explosiveness delta (small nudge — value is variance not mean)
    h_ex = _safe((h_adv or {}).get("off_explosiveness"))
    a_ex = _safe((a_adv or {}).get("off_explosiveness"))
    if h_ex or a_ex:
        delta += (h_ex - a_ex) * 1.5
    # Cap magnitude so advanced-stats can nudge Champion but not flip it.
    return max(-4.0, min(4.0, round(delta, 3)))


def estimate_cfb_challenger(
    ctx: dict, home_team: str, away_team: str,
) -> CfbChallengerResult:
    """Enriched CFB projection.  Falls back to Champion when advanced
    stats are unavailable — never fabricates signal.
    """
    ratings = (ctx or {}).get("cfb_sp_ratings_by_team") or {}
    adv     = (ctx or {}).get("cfb_advanced_stats_by_team") or {}
    if not ratings:
        return CfbChallengerResult(
            available=False,
            reason="MODEL_UNAVAILABLE:no_sp_ratings_ctx",
        )
    h = _lookup(ratings, home_team)
    a = _lookup(ratings, away_team)
    if not h or not a:
        missing = [t for t, r in (("home", h), ("away", a)) if not r]
        return CfbChallengerResult(
            available=False,
            reason=f"MODEL_UNAVAILABLE:sp_missing:{','.join(missing)}",
        )

    # ── Champion baseline (SP+ delta + HFA) ─────────────────────────
    try:
        h_r = float(h.get("rating") or 0.0)
        a_r = float(a.get("rating") or 0.0)
    except (TypeError, ValueError):
        return CfbChallengerResult(
            available=False, reason="MODEL_UNAVAILABLE:sp_bad_types",
        )
    champ_margin = (h_r - a_r) + HFA_POINTS

    # ── Advanced-stats enrichment ───────────────────────────────────
    h_adv = _lookup(adv, home_team)
    a_adv = _lookup(adv, away_team)
    if h_adv and a_adv:
        prov_state = "OBSERVED"
    elif h_adv or a_adv:
        prov_state = "EMPIRICAL"
    else:
        prov_state = "PRIOR_ONLY"

    epa_delta = _epa_margin_delta(h_adv, a_adv)
    exp_margin = champ_margin + epa_delta
    p_home = _logistic(MARGIN_K * exp_margin)

    factors: dict = {
        "Projected Margin (Champion)": round(champ_margin, 2),
        "EPA Margin Delta":            round(epa_delta, 2),
        "Projected Margin (Enriched)": round(exp_margin, 2),
    }
    # Real signals — only surface when present.
    def _emit(label, adv_h, adv_a, key, scale=1.0):
        if adv_h is None and adv_a is None:
            return
        vh = _safe((adv_h or {}).get(key))
        va = _safe((adv_a or {}).get(key))
        factors[label] = round((vh - va) * scale, 3)

    _emit("Offensive EPA Advantage",   h_adv, a_adv, "off_ppa", 100)
    _emit("Defensive EPA Advantage",   h_adv, a_adv, "def_ppa", -100)  # lower def_ppa = better
    _emit("Success Rate Advantage",    h_adv, a_adv, "off_success_rate", 100)
    _emit("Explosiveness Advantage",   h_adv, a_adv, "off_explosiveness", 100)
    _emit("Pass Efficiency Edge",      h_adv, a_adv, "off_pass_ppa", 100)
    _emit("Rush Efficiency Edge",      h_adv, a_adv, "off_rush_ppa", 100)
    _emit("Havoc Rate Advantage",      h_adv, a_adv, "def_havoc_total", 100)
    _emit("Finishing Drives Advantage", h_adv, a_adv, "off_points_per_opp", 1)

    provenance = {
        "state": prov_state,
        "has_home_advanced": h_adv is not None,
        "has_away_advanced": a_adv is not None,
        "epa_delta": epa_delta,
        "champion_margin": round(champ_margin, 2),
    }
    return CfbChallengerResult(
        available=True,
        p_home_ml=round(p_home, 4),
        expected_margin=round(exp_margin, 3),
        factors=factors,
        provenance=provenance,
    )


__all__ = ["estimate_cfb_challenger", "CfbChallengerResult"]
