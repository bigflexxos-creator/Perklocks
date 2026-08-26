"""CFB game-market model — Slice-P0 (2026-08-26 acceptance closure).

Independent authoritative CFB game-market probabilities computed from
existing SP+ ratings (`db.cfb_sp_ratings`) that were previously
ingested. This is a SMALL adapter that wires existing intelligence
into the game-market dispatch loop — NOT a new model.

Contract:
    estimate_cfb_game(ctx, home_team, away_team) -> CFBGameResult

    ctx MUST carry a preloaded SP+ ratings dict on
    `ctx["cfb_sp_ratings_by_team"]` (built by fetch_cfb_picks pre-loader).

Method:
    expected_margin = (home_rating - away_rating) + HOME_FIELD_ADV
    expected_total  = f(offense_ratings, defense_ratings)
    P(home_ml)      = logistic(margin_k * expected_margin)
    P(cover line)   = normal-cdf(margin - line, sigma=CFB_MARGIN_SIGMA)
    P(over total)   = normal-cdf(expected_total - line, sigma=CFB_TOTAL_SIGMA)

Sigma constants: CFB long-run empirical values (margin σ≈13.7,
total σ≈13.5). These are DISTRIBUTION parameters — not model
weights that need retuning; they mirror the same causal parameters
NFL platinum uses.  When either team's SP+ is missing the model
returns AVAILABLE=False and the caller records MODEL_UNAVAILABLE —
never a sportsbook-follow.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from math import exp, sqrt, erf
from typing import Optional


HOME_FIELD_ADV = 2.5    # SP+ / Vegas standard for CFB
MARGIN_K       = 0.10   # empirical SP+ margin → win-prob k
MARGIN_SIGMA   = 13.7   # CFB game-margin SD (empirical)
TOTAL_SIGMA    = 13.5   # CFB game-total SD (empirical)
AVG_TEAM_RATING = 0.0   # SP+ is zero-centered; missing team → this

_TIER_MIN_RATING_COVERAGE = 2   # both teams


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via erf."""
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def _logistic(x: float) -> float:
    return 1.0 / (1.0 + exp(-x))


@dataclass
class CFBGameResult:
    available: bool
    p_home_ml: Optional[float] = None
    expected_margin: Optional[float] = None   # positive = home favored
    expected_total: Optional[float] = None
    reason: Optional[str] = None
    tier: str = "UNAVAILABLE"     # "SP_PLUS_FULL" / "UNAVAILABLE"
    sources: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "available": self.available,
            "p_home_ml": self.p_home_ml,
            "expected_margin": self.expected_margin,
            "expected_total": self.expected_total,
            "reason": self.reason,
            "tier": self.tier,
            "sources": list(self.sources),
        }


def _team_key(name: str) -> str:
    return (name or "").strip().lower()


def _lookup_rating(ratings_by_team: dict, name: str) -> Optional[dict]:
    """Robust name → rating row (matches multiple aliases)."""
    if not name:
        return None
    n = _team_key(name)
    if n in ratings_by_team:
        return ratings_by_team[n]
    # try stripping common suffixes (Horned Frogs / Tar Heels / etc.)
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
            trimmed = n[: -len(stop)]
            if trimmed in ratings_by_team:
                return ratings_by_team[trimmed]
    # short-token lookup — match on first word ("indiana", "auburn")
    first = n.split()[0] if n.split() else ""
    if first and first in ratings_by_team:
        return ratings_by_team[first]
    return None


def estimate_cfb_game(ctx: dict, home_team: str, away_team: str) -> CFBGameResult:
    """Return CFBGameResult with independent SP+-based probabilities."""
    ratings = (ctx or {}).get("cfb_sp_ratings_by_team") or {}
    if not ratings:
        return CFBGameResult(available=False,
                             reason="MODEL_UNAVAILABLE:no_sp_ratings_ctx",
                             tier="UNAVAILABLE")
    h = _lookup_rating(ratings, home_team)
    a = _lookup_rating(ratings, away_team)
    if not h or not a:
        missing = []
        if not h: missing.append(home_team)
        if not a: missing.append(away_team)
        return CFBGameResult(available=False,
                             reason=f"MODEL_UNAVAILABLE:sp_missing:{','.join(missing)[:80]}",
                             tier="UNAVAILABLE")

    try:
        h_rate = float(h.get("rating") or AVG_TEAM_RATING)
        a_rate = float(a.get("rating") or AVG_TEAM_RATING)
        h_off  = float(h.get("offense_rating") or 25.0)
        a_off  = float(a.get("offense_rating") or 25.0)
        h_def  = float(h.get("defense_rating") or 25.0)
        a_def  = float(a.get("defense_rating") or 25.0)
    except (TypeError, ValueError) as e:
        return CFBGameResult(available=False,
                             reason=f"MODEL_UNAVAILABLE:sp_bad_types:{type(e).__name__}",
                             tier="UNAVAILABLE")

    # Expected margin: positive → home favored.
    expected_margin = (h_rate - a_rate) + HOME_FIELD_ADV
    # SP+ offense_rating is "points per game vs avg defense";
    # defense_rating is "points allowed vs avg offense".  Combined
    # expected points per team = own_off − opp_def_advantage.
    # Baseline avg CFB offense = ~28; use own_off adjusted by opp_def.
    h_pts = h_off + (25.0 - a_def)
    a_pts = a_off + (25.0 - h_def)
    expected_total = max(20.0, h_pts + a_pts)

    p_home_ml = _logistic(MARGIN_K * expected_margin)
    return CFBGameResult(
        available=True,
        p_home_ml=round(p_home_ml, 4),
        expected_margin=round(expected_margin, 3),
        expected_total=round(expected_total, 2),
        tier="SP_PLUS_FULL",
        sources=["cfb_sp_ratings"],
    )


def cfb_cover_probability(expected_margin: float, book_line: float,
                          side_is_home: bool) -> float:
    """Return P(pick_side covers book_line) from margin distribution.
    book_line is the number attached to that side (e.g. -8.5 for
    favorite, +8.5 for dog). expected_margin is home_score - away_score.
    """
    # For HOME side with line L: home covers iff (home_score - away_score) > -L
    # For AWAY side with line L: away covers iff (away_score - home_score) > -L,
    #   i.e. -expected_margin > -L → expected_margin < L
    if side_is_home:
        threshold = -book_line
        z = (expected_margin - threshold) / MARGIN_SIGMA
    else:
        threshold = book_line
        z = (threshold - expected_margin) / MARGIN_SIGMA
    return round(_norm_cdf(z), 4)


def cfb_over_probability(expected_total: float, book_line: float,
                         side_is_over: bool) -> float:
    """Return P(over/under book_line) using normal-cdf on expected_total."""
    z = (expected_total - book_line) / TOTAL_SIGMA
    p_over = _norm_cdf(z)
    return round(p_over if side_is_over else (1.0 - p_over), 4)


__all__ = [
    "estimate_cfb_game", "CFBGameResult",
    "cfb_cover_probability", "cfb_over_probability",
    "HOME_FIELD_ADV", "MARGIN_SIGMA", "TOTAL_SIGMA",
]
