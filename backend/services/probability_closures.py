"""SPORT-FAMILY PROBABILITY CLOSURES — applied INSIDE the Probability
Authority boundary (services.probability_authority.evaluate) between the
specialized model's raw probability and the market calibrator.

Each closure is a pure function: (pick, family, raw_probability) → Closure.
They never touch Lock Score, never cap, never manufacture volume; they make
the probability honest about uncertainty the upstream model ignored:

  SOCCER player props  : minutes / lineup availability (Poisson exposure
                         scaling + availability mixture; OUT → fail-closed)
  NBA player props     : threshold distribution over projected mean with
                         minutes/role uncertainty (Student-t, n_games df)
  NBA game markets     : book-implied seeds fail-closed (authority-level)
  CFB TOTAL            : Student-t posterior predictive — see
                         services.cfb_total_residuals (applied at model
                         time; recorded here for provenance)
  TENNIS               : canonical Lock Score authority recorded (no
                         base=90 / eligibility→99 construction survives)

Shadow-first: the authority records the closure on the contract; only a
PROMOTED family in ``active`` mode lets it drive win_probability.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, asdict
from typing import Any, Optional

CLOSURE_VERSION = "prob_closures.v1.2026-09-18"

SOCCER_PLAYER_FAMILIES = {"GOALSCORER", "ASSISTS", "SHOTS", "SOT", "GOAL_CONTRIBUTION"}
NBA_PLAYER_FAMILIES = {"POINTS", "REBOUNDS", "ASSISTS", "3PM", "PRA", "STEALS", "BLOCKS"}

# lineup status → (P(available for meaningful minutes), expected minutes)
# Priors are documented, deterministic, and only used when the upstream
# model did NOT already condition on minutes (``lineup_minutes_applied``).
LINEUP_PRIORS: dict[str, tuple[float, float]] = {
    "confirmed": (0.99, 84.0), "starting": (0.99, 84.0), "starter": (0.99, 84.0),
    "starting_xi": (0.99, 84.0), "xi": (0.99, 84.0),
    "projected": (0.88, 80.0), "probable": (0.88, 80.0), "expected": (0.88, 80.0),
    "unknown": (0.80, 72.0), "": (0.80, 72.0),
    "questionable": (0.65, 68.0), "doubtful": (0.50, 62.0), "gtd": (0.65, 68.0),
    "bench": (0.72, 28.0), "sub": (0.72, 28.0), "substitute": (0.72, 28.0), "rotation": (0.75, 45.0),
    "out": (0.0, 0.0), "injured": (0.0, 0.0), "suspended": (0.0, 0.0),
    "not_in_squad": (0.0, 0.0), "unavailable": (0.0, 0.0),
}


@dataclass
class Closure:
    family: str
    method: str
    input_probability: Optional[float]
    probability: Optional[float]
    uncertainty_extra: float = 0.0
    publication_eligible: bool = True
    fallback_reason: Optional[str] = None
    evidence_note: Optional[str] = None
    details: dict = None  # type: ignore[assignment]
    version: str = CLOSURE_VERSION

    def to_dict(self) -> dict:
        d = asdict(self)
        d["details"] = d.get("details") or {}
        return d


def _clip(p: float) -> float:
    return min(0.999, max(0.001, float(p)))


# ── Poisson helpers ───────────────────────────────────────────────────
def _pois_tail(lam: float, k: int) -> float:
    """P(N ≥ k) for N ~ Poisson(lam)."""
    if lam <= 0:
        return 0.0 if k > 0 else 1.0
    cdf = 0.0
    term = math.exp(-lam)
    for i in range(k):
        cdf += term
        term *= lam / (i + 1)
    return max(0.0, min(1.0, 1.0 - cdf))


def _pois_tail_inverse(p: float, k: int) -> float:
    """λ such that P(Poisson(λ) ≥ k) = p (bisection; monotone in λ)."""
    p = _clip(p)
    if k <= 1:
        return -math.log(1.0 - p)
    lo, hi = 1e-6, 60.0
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if _pois_tail(mid, k) < p:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def _threshold_k(pick: dict) -> int:
    """Count threshold implied by the market: "2+ Shots" → 2, "Over 1.5" → 2,
    anytime → 1."""
    m = str(pick.get("market") or "")
    mm = re.search(r"(\d+)\+", m)
    if mm:
        return max(1, int(mm.group(1)))
    line = pick.get("line")
    if isinstance(line, (int, float)) and line > 0:
        return int(math.floor(float(line))) + 1
    mm = re.search(r"over\s+(\d+(?:\.\d+)?)", m, re.I)
    if mm:
        return int(math.floor(float(mm.group(1)))) + 1
    return 1


def _lineup_status(pick: dict) -> str:
    ls = pick.get("lineup_status")
    if isinstance(ls, dict):
        ls = ls.get("status")
    s = str(ls or "").strip().lower().replace(" ", "_")
    return s if s in LINEUP_PRIORS else ("unknown" if not s else _nearest_status(s))


def _nearest_status(s: str) -> str:
    for key in ("out", "injur", "suspend", "confirm", "start", "project", "probable",
                "doubt", "question", "bench", "sub"):
        if key in s:
            return {"injur": "injured", "suspend": "suspended", "confirm": "confirmed",
                    "start": "starting", "project": "projected", "doubt": "doubtful",
                    "question": "questionable", "sub": "sub"}.get(key, key)
    return "unknown"


# ── SOCCER player closure ─────────────────────────────────────────────
def soccer_player_closure(pick: dict, family: str, raw: Optional[float]) -> Closure:
    fam = family.split("_", 1)[-1]
    if raw is None:
        return Closure(family, "soccer_minutes_lineup.v1", None, None,
                       publication_eligible=False, fallback_reason="MODEL_PROBABILITY_INVALID")
    status = _lineup_status(pick)
    p_avail, exp_min = LINEUP_PRIORS[status]
    details: dict[str, Any] = {"lineup_status": status, "p_available": p_avail, "expected_minutes": exp_min}
    if p_avail <= 0.0:
        return Closure(family, "soccer_minutes_lineup.v1", round(raw, 4), 0.0,
                       publication_eligible=False, fallback_reason="LINEUP_OUT", details=details)
    _mm = pick.get("model_meta")
    _engine = str((pick.get("pick_rationale") or {}).get("engine") if isinstance(pick.get("pick_rationale"), dict) else "") \
        or str(pick.get("source") or "")
    if pick.get("lineup_minutes_applied") or (isinstance(_mm, dict) and _mm.get("expected_minutes")) \
            or _engine == "goal_scorer_v3":
        # Upstream model (goal_scorer_v3) already conditioned on expected
        # minutes — apply ONLY the availability mixture (it assumes the
        # player appears), never re-scale exposure.
        p = _clip(raw * p_avail)
        details["passthrough"] = "upstream_minutes_conditioned"
        return Closure(family, "soccer_minutes_lineup.v1", round(raw, 4), round(p, 4),
                       uncertainty_extra=round(raw * (1 - p_avail) * 0.5, 4), details=details)
    k = _threshold_k(pick) if fam in ("SHOTS", "SOT") else 1
    # Upstream probabilities are full-appearance probabilities (per-90
    # rates × form × matchup).  Recover the 90-minute rate, re-scale to
    # expected exposure, mix with availability.
    model_minutes = float(pick.get("expected_minutes") or 90.0)
    lam_model = _pois_tail_inverse(raw, k)
    lam90 = lam_model * 90.0 / max(1.0, model_minutes)
    lam_exp = lam90 * exp_min / 90.0
    p_given_play = _pois_tail(lam_exp, k)
    p = _clip(p_avail * p_given_play)
    details.update({"k": k, "lambda_90": round(lam90, 4), "lambda_exposed": round(lam_exp, 4),
                    "p_given_play": round(p_given_play, 4)})
    unc = p * (1 - p_avail) + (0.02 if status == "unknown" else 0.0)
    note = None
    if status == "unknown":
        note = "LINEUP_UNKNOWN"
    return Closure(family, "soccer_minutes_lineup.v1", round(raw, 4), round(p, 4),
                   uncertainty_extra=round(unc, 4), evidence_note=note, details=details)


# ── NBA threshold distribution ────────────────────────────────────────
def nba_threshold_probability(mean: float, sd: float, line: float, side: str,
                              minutes_mean: Optional[float] = None,
                              minutes_sd: Optional[float] = None,
                              n_games: Optional[int] = None) -> dict:
    """P(X over/under line) for a box-score stat with role/minutes
    uncertainty folded into the variance:
        Var = sd² + (mean/minutes_mean)² · minutes_sd²
    Student-t with df = n_games − 1 when the sample is known (small
    samples → fatter tails); integer lines remove push mass."""
    from scipy.stats import t as _t, norm as _norm
    var = float(sd) ** 2
    role_var = 0.0
    if minutes_mean and minutes_sd and minutes_mean > 0:
        rate = float(mean) / float(minutes_mean)
        role_var = (rate * float(minutes_sd)) ** 2
        var += role_var
    scale = math.sqrt(max(var, 1e-6))
    if n_games and n_games >= 3:
        dist = _t(df=n_games - 1, loc=float(mean), scale=scale)
    else:
        dist = _norm(loc=float(mean), scale=scale)
    line = float(line)
    if line.is_integer():
        p_push = dist.cdf(line + 0.5) - dist.cdf(line - 0.5)
        p_over = (1.0 - dist.cdf(line + 0.5)) / max(1e-9, 1.0 - p_push)
    else:
        p_push = 0.0
        p_over = 1.0 - dist.cdf(line)
    p = p_over if str(side).lower().startswith("o") else 1.0 - p_over
    return {"p": _clip(p), "p_push": float(p_push), "scale": round(scale, 4),
            "role_var": round(role_var, 4), "df": (n_games - 1) if (n_games and n_games >= 3) else None}


def nba_player_closure(pick: dict, family: str, raw: Optional[float]) -> Closure:
    mean = _first_num(pick, ("projection_mean", "proj_mean", "model_mean", "projected_value", "projection"))
    sd = _first_num(pick, ("projection_sd", "proj_sd", "model_sd", "projected_sd", "std"))
    line = pick.get("line") if isinstance(pick.get("line"), (int, float)) else None
    side = "over" if "under" not in str(pick.get("market") or "").lower() else "under"
    if mean is None or sd is None or line is None:
        return Closure(family, "nba_threshold_distribution.v1", round(raw, 4) if raw is not None else None,
                       round(raw, 4) if raw is not None else None,
                       evidence_note="NO_THRESHOLD_DISTRIBUTION",
                       details={"reason": "projection mean/sd/line missing — raw model probability kept"})
    n_games = _first_num(pick, ("sample_games", "n_games", "sample_size", "games"))
    r = nba_threshold_probability(mean, sd, line, side,
                                  minutes_mean=_first_num(pick, ("minutes_projection", "minutes_mean", "proj_minutes")),
                                  minutes_sd=_first_num(pick, ("minutes_sd", "minutes_std")),
                                  n_games=int(n_games) if n_games else None)
    return Closure(family, "nba_threshold_distribution.v1", round(raw, 4) if raw is not None else None,
                   round(r["p"], 4), uncertainty_extra=round(0.5 * math.sqrt(r["role_var"]) / max(1.0, float(sd)) * 0.05, 4),
                   details={"mean": mean, "sd": sd, "line": line, "side": side, **r})


def _first_num(d: dict, keys: tuple) -> Optional[float]:
    for k in keys:
        v = d.get(k)
        if isinstance(v, (int, float)) and not (isinstance(v, float) and math.isnan(v)):
            return float(v)
    return None


# ── CFB / TENNIS provenance closures ──────────────────────────────────
def cfb_total_closure(pick: dict, family: str, raw: Optional[float]) -> Closure:
    gm = pick.get("cfb_game_sim") or {}
    details = {"predictive": "student_t_posterior_predictive.v1",
               "expected_total": gm.get("expected_total"), "prior_sigma": gm.get("total_sigma"),
               "line": pick.get("line")}
    try:
        from services.cfb_total_residuals import residual_evidence, PRIOR_DF
        s, n = residual_evidence()
        details.update({"residual_sigma": s, "residual_n": n, "df": PRIOR_DF + n})
    except Exception:
        pass
    return Closure(family, "cfb_total_predictive.v1", round(raw, 4) if raw is not None else None,
                   round(raw, 4) if raw is not None else None, details=details)


def tennis_closure(pick: dict, family: str, raw: Optional[float]) -> Closure:
    auth = pick.get("lock_score_authority")
    ok = auth == "canonical_compute_lock_score"
    return Closure(family, "tennis_canonical_lock.v1", round(raw, 4) if raw is not None else None,
                   round(raw, 4) if raw is not None else None,
                   evidence_note=None if ok else "LEGACY_LOCK_CONSTRUCTION",
                   details={"lock_score_authority": auth})


# ── dispatcher ───────────────────────────────────────────────────────
def apply_closure(pick: dict, family: str, raw: Optional[float]) -> Optional[Closure]:
    sport, _, fam = family.partition("_")
    if sport == "SOCCER" and fam in SOCCER_PLAYER_FAMILIES:
        return soccer_player_closure(pick, family, raw)
    if sport == "NBA" and fam in NBA_PLAYER_FAMILIES:
        return nba_player_closure(pick, family, raw)
    if sport == "CFB" and fam == "TOTAL":
        return cfb_total_closure(pick, family, raw)
    if sport == "TENNIS":
        return tennis_closure(pick, family, raw)
    return None


__all__ = ["Closure", "apply_closure", "soccer_player_closure", "nba_player_closure",
           "nba_threshold_probability", "LINEUP_PRIORS", "CLOSURE_VERSION"]
