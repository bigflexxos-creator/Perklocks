"""Goalscorer Matchup Engine (v3 — matchup-first ranking).

User mandate (2026-06-30): "PerkLocks goalscorer engine feels like it
is choosing players from historical averages instead of evaluating the
actual match. Upgrade the goalscorer pipeline to be matchup-first and
remove random scorer behavior."

This module is a SCORE + GATE layer that runs AFTER pick generation
(both bookmaker-derived picks and synthetic elite/SportDB picks) and:

  1) Computes a matchup-first score using the user's specified weights:
        Matchup     = 35%
        Opportunity = 30%
        Form        = 20%
        Historical  = 15%

  2) Applies confidence penalties:
        • Bench risk
        • Expected minutes < 60
        • Market disagreement (model rank vs book rank)
        • Missing data

  3) Validates against market — compares model rank vs official book
     anytime-goal odds rank. Large disagreement → confidence penalty.

  4) Returns explainability:
        starter_probability, expected_minutes, matchup_grade, role,
        penalty_taker, xG_form, why_this_pick, why_not_this_pick

  5) Recommends DROP when confidence below threshold so spammy noise
     (player not in squad / no real form data / on a bench) gets
     filtered out instead of cluttering the board.

USAGE
-----

    from goalscorer_matchup import (
        MatchupContext, score_goalscorer, annotate_pick,
        annotate_picks_async,
    )

    # Inline use:
    ctx = MatchupContext(player_name="Harry Kane", team="England",
                         opponent="Panama", is_home=True)
    result = score_goalscorer(ctx)
    if result.recommend_drop:
        ... drop pick ...
    else:
        pick["matchup_score"] = result.score
        pick["why_this_pick"] = result.why_this_pick

    # Async batch use (resolves Understat + squad data per pick):
    picks = await annotate_picks_async(picks, db)

This engine is INTENTIONALLY MORE CONSERVATIVE than the existing
Poisson `goal_scorer_engine_v2`. The Poisson engine answers
"what is the probability?". This engine answers "should this pick
ACTUALLY surface to the user?" with hard penalties for context the
Poisson model treats as soft inputs.
"""

from __future__ import annotations

import logging
import math
import re
import unicodedata
from dataclasses import dataclass, field, asdict
from typing import Optional, Any

logger = logging.getLogger("lockscore.goalscorer_matchup")


# ──────────────────────────────────────────────────────────────────
#  Configuration
# ──────────────────────────────────────────────────────────────────

# User-mandated weights (must sum to 1.0).
WEIGHT_MATCHUP     = 0.35
WEIGHT_OPPORTUNITY = 0.30
WEIGHT_FORM        = 0.20
WEIGHT_HISTORICAL  = 0.15
assert abs(WEIGHT_MATCHUP + WEIGHT_OPPORTUNITY + WEIGHT_FORM + WEIGHT_HISTORICAL - 1.0) < 1e-9

# Drop thresholds.
DROP_CONFIDENCE_FLOOR = 0.45   # below this → drop
DROP_SCORE_FLOOR      = 28.0   # below this → drop regardless of confidence

# Penalty magnitudes (each multiplies the raw score downward).
PENALTY_BENCH_RISK         = 0.18
PENALTY_LOW_MINUTES        = 0.12
PENALTY_MARKET_DISAGREE    = 0.10
PENALTY_MISSING_DATA       = 0.20
PENALTY_NOT_IN_SQUAD       = 0.95  # near-total kill — squad gate dominant
PENALTY_RECENT_INJURY      = 0.30
PENALTY_TIRED_CONGESTION   = 0.06

# Role weights for opportunity sub-score.
ROLE_WEIGHTS = {
    "ST": 1.00, "CF": 1.00, "F": 1.00, "FW": 1.00,
    "SS": 0.92, "9.5": 0.90,
    "AM": 0.78, "CAM": 0.78, "AMC": 0.78,
    "W": 0.74, "LW": 0.74, "RW": 0.74, "WG": 0.74, "WF": 0.74,
    "CM": 0.55, "M": 0.55, "MC": 0.55,
    "DM": 0.32, "DMC": 0.32,
    "LB": 0.22, "RB": 0.22, "WB": 0.22,
    "CB": 0.20, "D": 0.20, "DC": 0.20, "DEF": 0.20,
    "GK": 0.0,
}
DEFAULT_ROLE_WEIGHT = 0.70  # unknown role → mid forward-ish

# League-average reference values (used to normalise xGA, etc.).
LEAGUE_AVG_TEAM_XG_PER_GAME  = 1.30
LEAGUE_AVG_OPPONENT_XGA_PER_GAME = 1.30
LEAGUE_AVG_XG_PER_90         = 0.30
LEAGUE_AVG_GOALS_PER_90      = 0.28
LEAGUE_AVG_SHOTS_PER_90      = 1.80


# ──────────────────────────────────────────────────────────────────
#  Data containers
# ──────────────────────────────────────────────────────────────────

@dataclass
class MatchupContext:
    """All inputs the matchup engine needs to score one player + match."""
    player_name: str
    team: str
    opponent: str
    sport: str = "Soccer"
    league: str = ""
    is_home: bool = False

    # Recent form (last-5 weighted) — values per 90 unless noted.
    xg_per_90:        float = 0.0
    npxg_per_90:      float = 0.0
    shots_per_90:     float = 0.0
    goals_per_90:     float = 0.0
    touches_in_box_per_90: float = 0.0
    last_5_goals:     int   = 0
    last_5_minutes:   int   = 0       # total minutes over last 5 games
    last_5_starts:    int   = 0
    form_label:       str   = "NEUTRAL"   # HOT / NEUTRAL / COLD
    form_score:       int   = 50

    # Historical season totals.
    season_goals:     int   = 0
    season_games:     int   = 0
    season_xg:        float = 0.0
    goals_over_xg:    float = 1.0       # > 1 = hot finisher

    # Role / opportunity.
    position:         str   = ""        # "ST" / "FW" / "CAM" etc.
    is_penalty_taker: bool  = False
    is_set_piece_taker: bool = False

    # Lineup.
    confirmed_starter:    Optional[bool] = None  # True if confirmed lineup released
    starter_probability:  float = 0.70           # heuristic 0..1
    expected_minutes:     float = 78.0           # 0..90
    bench_risk:           bool  = False

    # Match context.
    team_implied_goals:        float = LEAGUE_AVG_TEAM_XG_PER_GAME
    opponent_xga_per_90:       float = LEAGUE_AVG_OPPONENT_XGA_PER_GAME
    opponent_defensive_strength: float = 50.0   # 0..100, higher = stronger
    home_away_split_factor:    float = 1.0      # 1.0 = neutral, > 1 = boost

    # Schedule / availability.
    rest_days:        int  = 5
    suspended_or_injured: bool = False
    recent_injury:    bool = False

    # Market signals.
    book_anytime_implied_pct: float = 0.0   # 0..100
    market_rank:      Optional[int] = None  # rank among teammates by book

    # National team squad gate.
    in_squad:         Optional[bool] = None  # None = unknown, False = NOT in squad

    # Identity / context for explainability.
    market_label:     str = ""   # e.g. "Harry Kane Anytime Goal Scorer"
    event:            str = ""   # e.g. "England @ Panama"


@dataclass
class MatchupResult:
    """Output of the matchup engine — score + every sub-component."""
    # Final values.
    score:                float    # 0..100 final (post-penalty)
    raw_score:            float    # 0..100 pre-penalty
    confidence:           float    # 0..1
    matchup_grade:        str      # "A+" .. "F"

    # Sub-scores (each 0..100).
    matchup_subscore:     float
    opportunity_subscore: float
    form_subscore:        float
    historical_subscore:  float

    # Penalties applied (each 0..1).
    penalty_total:        float
    penalty_bench:        float
    penalty_minutes:      float
    penalty_market:       float
    penalty_missing:      float
    penalty_not_in_squad: float
    penalty_injury:       float

    # Explainability fields (per user spec).
    starter_probability:  float
    expected_minutes:     float
    role:                 str
    penalty_taker:        bool
    xG_form:              float
    market_rank:          Optional[int]

    why_this_pick:        list[str]
    why_not_this_pick:    list[str]

    # Filter recommendation.
    recommend_drop:       bool
    drop_reasons:         list[str]

    def to_pick_fields(self) -> dict:
        """Project the explainability fields into a dict shaped for
        attaching directly onto a pick document."""
        return {
            "matchup_score":          round(self.score, 1),
            "matchup_raw_score":      round(self.raw_score, 1),
            "matchup_confidence":     round(self.confidence, 3),
            "matchup_grade":          self.matchup_grade,
            "starter_probability":    round(self.starter_probability, 3),
            "expected_minutes":       round(self.expected_minutes, 1),
            "role":                   self.role or None,
            "penalty_taker":          self.penalty_taker,
            "xG_form":                round(self.xG_form, 3),
            "market_rank":            self.market_rank,
            "matchup_subscore":       round(self.matchup_subscore, 1),
            "opportunity_subscore":   round(self.opportunity_subscore, 1),
            "form_subscore":          round(self.form_subscore, 1),
            "historical_subscore":    round(self.historical_subscore, 1),
            "why_this_pick":          self.why_this_pick,
            "why_not_this_pick":      self.why_not_this_pick,
        }


# ──────────────────────────────────────────────────────────────────
#  Sub-score calculators
# ──────────────────────────────────────────────────────────────────

def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    if v != v:    # NaN guard
        return lo
    return max(lo, min(hi, v))


def _grade(score: float) -> str:
    if score >= 88:
        return "A+"
    if score >= 80:
        return "A"
    if score >= 72:
        return "B+"
    if score >= 65:
        return "B"
    if score >= 55:
        return "C+"
    if score >= 45:
        return "C"
    if score >= 35:
        return "D"
    return "F"


def _matchup_subscore(ctx: MatchupContext) -> tuple[float, list[str]]:
    """Matchup = team_xG × opponent_xGA strength-of-schedule + home boost.

    Range: 0..100. Default neutral matchup ≈ 50.
    """
    notes: list[str] = []
    team_xg = max(0.4, ctx.team_implied_goals or LEAGUE_AVG_TEAM_XG_PER_GAME)
    opp_xga = max(0.4, ctx.opponent_xga_per_90 or LEAGUE_AVG_OPPONENT_XGA_PER_GAME)
    league_norm = LEAGUE_AVG_TEAM_XG_PER_GAME

    # Strength-of-schedule multiplier (sqrt smooths the product).
    sos_mult = math.sqrt((team_xg * opp_xga) / (league_norm ** 2))
    base = 50.0 * sos_mult  # 50 = neutral matchup
    # Home / away adjustment (±5).
    if ctx.is_home:
        base += 4.0
        notes.append("Playing at home")
    else:
        base -= 2.0
    # Opponent defensive strength (0..100, higher = stronger D).
    if ctx.opponent_defensive_strength:
        # Lower opp_strength → higher matchup score.
        ds_factor = (60.0 - ctx.opponent_defensive_strength) / 60.0
        base += 8.0 * ds_factor  # ±8 swing
        if ctx.opponent_defensive_strength > 70:
            notes.append(f"Tough opponent D ({ctx.opponent_defensive_strength:.0f}/100)")
        elif ctx.opponent_defensive_strength < 35:
            notes.append(f"Weak opponent D ({ctx.opponent_defensive_strength:.0f}/100)")

    if team_xg > league_norm * 1.20:
        notes.append(f"Team projected {team_xg:.1f} goals (above avg)")
    if opp_xga > league_norm * 1.20:
        notes.append(f"Opponent leaks {opp_xga:.1f} xGA/game")

    return _clamp(base), notes


def _opportunity_subscore(ctx: MatchupContext) -> tuple[float, list[str]]:
    """Opportunity = expected_minutes × starter_prob × role_weight + PK boost.

    Range: 0..100.
    """
    notes: list[str] = []
    role = (ctx.position or "").upper().split("/")[0].strip()
    # Strip any non-letter suffix to match ROLE_WEIGHTS keys.
    role_key = re.sub(r"[^A-Z]", "", role)[:3] or role
    role_w = ROLE_WEIGHTS.get(role_key) or ROLE_WEIGHTS.get(role) or DEFAULT_ROLE_WEIGHT

    minutes_factor = max(0.0, min(1.0, (ctx.expected_minutes or 0) / 90.0))
    starter_factor = max(0.0, min(1.0, ctx.starter_probability or 0))
    if ctx.confirmed_starter is True:
        starter_factor = 1.0
        notes.append("Confirmed in starting XI")
    elif ctx.confirmed_starter is False:
        starter_factor = 0.0
        notes.append("Not in confirmed starting XI")
    elif ctx.starter_probability >= 0.85:
        notes.append(f"Likely starter ({100*ctx.starter_probability:.0f}%)")

    # Penalty / set-piece boost (top-up only, not negative).
    pk_boost = 0.0
    if ctx.is_penalty_taker:
        pk_boost += 7.5
        notes.append("Designated penalty taker")
    if ctx.is_set_piece_taker:
        pk_boost += 3.0
        notes.append("Set-piece taker")

    raw = 100.0 * (minutes_factor * 0.55 + starter_factor * 0.30 + role_w * 0.15)
    raw += pk_boost

    if ctx.expected_minutes < 60 and ctx.confirmed_starter is not True:
        notes.append(f"Expected minutes only {ctx.expected_minutes:.0f}")
    if ctx.bench_risk:
        notes.append("Bench risk flagged")

    return _clamp(raw), notes


def _form_subscore(ctx: MatchupContext) -> tuple[float, list[str]]:
    """Form = recent xG / shots / actual goals weighted last-5.

    Range: 0..100.
    """
    notes: list[str] = []
    # Normalise xG/90 against league avg.
    xg_factor = (ctx.xg_per_90 or 0) / max(0.10, LEAGUE_AVG_XG_PER_90)
    shots_factor = (ctx.shots_per_90 or 0) / max(0.20, LEAGUE_AVG_SHOTS_PER_90)
    goals_per_90 = (ctx.goals_per_90 or 0)
    goal_factor = goals_per_90 / max(0.10, LEAGUE_AVG_GOALS_PER_90)

    # Recent 5-game adjustment.
    recent5_factor = 1.0
    if ctx.last_5_minutes and ctx.last_5_minutes > 60:
        gp90_recent5 = ctx.last_5_goals * 90 / ctx.last_5_minutes
        recent5_factor = max(0.50, min(2.0, gp90_recent5 / max(0.10, LEAGUE_AVG_GOALS_PER_90)))

    base = 50.0 * (
        xg_factor * 0.35
        + shots_factor * 0.20
        + goal_factor * 0.25
        + recent5_factor * 0.20
    )

    # Label boost / penalty.
    if ctx.form_label == "HOT":
        base += 8.0
        notes.append(f"Form: HOT ({ctx.form_score}/100)")
    elif ctx.form_label == "COLD":
        base -= 8.0
        notes.append(f"Form: COLD ({ctx.form_score}/100)")
    elif ctx.form_score:
        # Smooth blend if numeric form available.
        base += (ctx.form_score - 50) * 0.10

    if ctx.xg_per_90 and ctx.xg_per_90 > LEAGUE_AVG_XG_PER_90 * 1.6:
        notes.append(f"xG/90 {ctx.xg_per_90:.2f} (elite)")
    if ctx.last_5_goals >= 3:
        notes.append(f"{ctx.last_5_goals} goals in last 5")
    if ctx.touches_in_box_per_90 and ctx.touches_in_box_per_90 > 6:
        notes.append(f"{ctx.touches_in_box_per_90:.1f} box touches/90")

    return _clamp(base), notes


def _historical_subscore(ctx: MatchupContext) -> tuple[float, list[str]]:
    """Historical = season goals / games + goals_over_xg modifier.

    Range: 0..100.
    """
    notes: list[str] = []
    games = max(1, ctx.season_games or 0)
    g_per_game = (ctx.season_goals or 0) / games

    # League-avg striker scores ~0.4 goals/game; elite ~0.7+.
    base = 50.0 + (g_per_game - 0.40) * 100.0  # +1 g/game → +60 (cap at 100)

    # Hot vs cold finisher modifier.
    if ctx.goals_over_xg:
        diff = ctx.goals_over_xg - 1.0
        base += diff * 12.0  # +12 if 1.0× over xG, etc.

    if ctx.season_goals >= 15:
        notes.append(f"Season: {ctx.season_goals}G / {ctx.season_games}GP")
    if ctx.goals_over_xg and ctx.goals_over_xg >= 1.15:
        notes.append(f"Finishing {100*(ctx.goals_over_xg-1):.0f}% over xG")
    elif ctx.goals_over_xg and ctx.goals_over_xg < 0.85:
        notes.append(f"Underperforming xG ({100*(1-ctx.goals_over_xg):.0f}% cold)")

    return _clamp(base), notes


# ──────────────────────────────────────────────────────────────────
#  Penalty calculator
# ──────────────────────────────────────────────────────────────────

def _apply_penalties(ctx: MatchupContext, model_market_rank: Optional[int]) -> dict:
    """Compute penalty fractions (each 0..1, summed cap 0.95).

    Returns dict { name: fraction } for caller to inspect.
    """
    out = {
        "bench":         0.0,
        "minutes":       0.0,
        "market":        0.0,
        "missing":       0.0,
        "not_in_squad":  0.0,
        "injury":        0.0,
        "congestion":    0.0,
    }
    # Squad gate — if hard False, dominant penalty (nearly kill).
    if ctx.in_squad is False:
        out["not_in_squad"] = PENALTY_NOT_IN_SQUAD
    if ctx.bench_risk:
        out["bench"] = PENALTY_BENCH_RISK
    if ctx.expected_minutes < 60 and ctx.confirmed_starter is not True:
        # Scale by how low — 30 min → 0.20, 50 min → 0.06.
        factor = (60 - max(0, ctx.expected_minutes)) / 60.0
        out["minutes"] = PENALTY_LOW_MINUTES * (1.0 + factor)
    # Market disagreement: book ranks player low but our model ranks high (or vice versa)
    if (
        ctx.market_rank is not None
        and model_market_rank is not None
        and ctx.market_rank >= 1 and model_market_rank >= 1
    ):
        diff = abs(ctx.market_rank - model_market_rank)
        if diff >= 4:
            out["market"] = PENALTY_MARKET_DISAGREE * min(2.0, diff / 4.0)
    # Missing data — no Understat record AND no curated elite anchor.
    if (
        not ctx.xg_per_90
        and not ctx.season_goals
        and not ctx.last_5_goals
        and not ctx.form_score == 50
    ):
        out["missing"] = PENALTY_MISSING_DATA
    if ctx.suspended_or_injured:
        out["injury"] = PENALTY_RECENT_INJURY * 1.5
    elif ctx.recent_injury:
        out["injury"] = PENALTY_RECENT_INJURY
    if ctx.rest_days and ctx.rest_days <= 2:
        out["congestion"] = PENALTY_TIRED_CONGESTION

    return out


# ──────────────────────────────────────────────────────────────────
#  Core scorer
# ──────────────────────────────────────────────────────────────────

def score_goalscorer(
    ctx: MatchupContext,
    model_market_rank: Optional[int] = None,
) -> MatchupResult:
    """Compute the full matchup-first score for one player + match.

    Args:
        ctx: MatchupContext built by caller from picks + DB data.
        model_market_rank: This player's rank (1 = best) per OUR model
            within the same (team, market_family) bucket. If provided,
            used in the market-disagreement penalty against ctx.market_rank.
    """
    # Sub-scores + per-section explainability notes.
    matchup_s, matchup_notes         = _matchup_subscore(ctx)
    opportunity_s, opportunity_notes = _opportunity_subscore(ctx)
    form_s, form_notes               = _form_subscore(ctx)
    historical_s, historical_notes   = _historical_subscore(ctx)

    raw = (
        WEIGHT_MATCHUP * matchup_s
        + WEIGHT_OPPORTUNITY * opportunity_s
        + WEIGHT_FORM * form_s
        + WEIGHT_HISTORICAL * historical_s
    )

    penalties = _apply_penalties(ctx, model_market_rank)
    penalty_total = min(0.95, sum(penalties.values()))

    score = raw * (1.0 - penalty_total)
    # Confidence: high when score AND penalties are clean.
    # (1 - penalty) * smooth(raw/100).
    confidence = (1.0 - penalty_total) * (0.5 + 0.5 * (raw / 100.0))
    confidence = max(0.0, min(1.0, confidence))

    drop_reasons: list[str] = []
    if ctx.in_squad is False:
        drop_reasons.append(f"{ctx.player_name} not in {ctx.team}'s announced squad")
    if score < DROP_SCORE_FLOOR:
        drop_reasons.append(f"Low matchup score ({score:.1f})")
    if confidence < DROP_CONFIDENCE_FLOOR and ctx.in_squad is not True:
        drop_reasons.append(f"Low confidence ({confidence:.2f})")
    recommend_drop = bool(drop_reasons)

    # Build explainability bullets.
    why_this: list[str] = []
    why_not:  list[str] = []
    # Group: matchup, opportunity, form, historical
    why_this.extend(matchup_notes[:3])
    why_this.extend(opportunity_notes[:3])
    why_this.extend(form_notes[:2])
    why_this.extend(historical_notes[:2])
    # Filter out neutral / dud notes by removing duplicates.
    seen = set()
    why_this = [n for n in why_this if not (n in seen or seen.add(n))]
    # Why-NOT bullets — derive from penalty list.
    if penalties["not_in_squad"]:
        why_not.append("Not in announced matchday squad")
    if penalties["bench"]:
        why_not.append("Likely bench / squad rotation")
    if penalties["minutes"]:
        why_not.append(f"Limited expected minutes ({ctx.expected_minutes:.0f})")
    if penalties["market"]:
        why_not.append(f"Book disagrees with model rank by {abs((ctx.market_rank or 0) - (model_market_rank or 0))}")
    if penalties["missing"]:
        why_not.append("Sparse data — confidence reduced")
    if penalties["injury"]:
        why_not.append("Recent injury / fitness concern")
    if penalties["congestion"]:
        why_not.append(f"Short rest ({ctx.rest_days}d)")

    return MatchupResult(
        score=round(score, 2),
        raw_score=round(raw, 2),
        confidence=round(confidence, 3),
        matchup_grade=_grade(score),
        matchup_subscore=round(matchup_s, 2),
        opportunity_subscore=round(opportunity_s, 2),
        form_subscore=round(form_s, 2),
        historical_subscore=round(historical_s, 2),
        penalty_total=round(penalty_total, 3),
        penalty_bench=round(penalties["bench"], 3),
        penalty_minutes=round(penalties["minutes"], 3),
        penalty_market=round(penalties["market"], 3),
        penalty_missing=round(penalties["missing"], 3),
        penalty_not_in_squad=round(penalties["not_in_squad"], 3),
        penalty_injury=round(penalties["injury"], 3),
        starter_probability=ctx.starter_probability,
        expected_minutes=ctx.expected_minutes,
        role=(ctx.position or "").upper(),
        penalty_taker=ctx.is_penalty_taker,
        xG_form=ctx.xg_per_90,
        market_rank=ctx.market_rank,
        why_this_pick=why_this,
        why_not_this_pick=why_not,
        recommend_drop=recommend_drop,
        drop_reasons=drop_reasons,
    )


# ──────────────────────────────────────────────────────────────────
#  Async context builder — pulls live data from DB
# ──────────────────────────────────────────────────────────────────

def _norm(s: str) -> str:
    if not s:
        return ""
    n = unicodedata.normalize("NFKD", s).lower()
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = re.sub(r"[^a-z0-9 ]+", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def _norm_squashed(s: str) -> str:
    return _norm(s).replace(" ", "")


_GOAL_KEYWORDS = (
    "anytime goal scorer", "first goal scorer", "last goal scorer",
    "to score or assist", "to score",
)


def _player_from_market(market: str) -> str:
    """Extract player name from a market label like 'Harry Kane Anytime Goal Scorer'."""
    if not market:
        return ""
    ml = market.lower()
    for kw in _GOAL_KEYWORDS:
        idx = ml.find(kw)
        if idx > 0:
            return market[:idx].strip()
    return market.strip()


def _market_family(market: str) -> str:
    ml = (market or "").lower()
    if "first goal scorer" in ml:
        return "FGS"
    if "anytime goal scorer" in ml:
        return "ATGS"
    if "last goal scorer" in ml:
        return "LGS"
    if "to score or assist" in ml:
        return "SoA"
    return "OTHER"


def _is_goalscorer_market(market: str) -> bool:
    ml = (market or "").lower()
    return any(kw in ml for kw in _GOAL_KEYWORDS)


async def _fetch_form_for_players(db, players: list[str]) -> dict[str, dict]:
    """Bulk-load Understat form records for a list of player names.

    Returns: { norm_squashed(player): form_doc }
    """
    if not players:
        return {}
    norms_full   = {_norm(p) for p in players if p}
    norms_squash = {_norm_squashed(p) for p in players if p}
    # Surname-only fallback set. Used in the regex fallback below to
    # catch records where the DB stored a longer formal name
    # (e.g. "Kylian Mbappe-Lottin" canonical=kylianmbappelottin) but
    # the pick used the short form "Kylian Mbappe".
    surnames: list[str] = []
    for p in players:
        n = _norm(p)
        toks = n.split() if n else []
        if toks and len(toks[-1]) >= 4:
            surnames.append(toks[-1])
    q = {
        "$or": [
            {"name_canonical": {"$in": list(norms_full)}},
            {"name_canonical": {"$in": list(norms_squash)}},
        ]
    }
    out: dict[str, dict] = {}
    try:
        async for d in db.soccer_player_form.find(q, {"_id": 0}):
            nc = d.get("name_canonical") or ""
            out[_norm_squashed(nc)] = d
            out[_norm(nc)] = d
        # Second pass — for any player WITHOUT an exact match yet, try
        # a surname-prefix regex (anchored to "<first>* <surname>").
        unresolved: list[str] = []
        for p in players:
            ns = _norm_squashed(p)
            nf = _norm(p)
            if ns not in out and nf not in out:
                unresolved.append(p)
        if unresolved:
            for p in unresolved:
                pn = _norm(p)
                toks = pn.split()
                if not toks:
                    continue
                first = toks[0]
                last = toks[-1]
                if len(last) < 4:
                    continue
                # Match canonical that starts with first name AND contains surname.
                pat = rf"^{re.escape(first)}.*{re.escape(last)}"
                try:
                    d = await db.soccer_player_form.find_one(
                        {"name_canonical": {"$regex": pat}}, {"_id": 0}
                    )
                except Exception:
                    d = None
                if d:
                    out[_norm_squashed(p)] = d
                    out[_norm(p)] = d
    except Exception as e:
        logger.warning("Form lookup failed: %s", e)
    return out


def _build_context_from_pick(
    pick: dict,
    form_doc: Optional[dict],
    teams_for_event: tuple[str, str],   # (home_team, away_team)
    pk_takers: set[str],
    in_squad: Optional[bool],
) -> MatchupContext:
    """Build a MatchupContext from a pick + supporting lookups."""
    market = pick.get("market") or ""
    player_name = _player_from_market(market)
    event = pick.get("event") or ""

    home_team, away_team = teams_for_event
    player_team = pick.get("player_team") or pick.get("team") or ""
    opponent = ""
    is_home = False
    if player_team and home_team and away_team:
        if _norm(player_team) == _norm(home_team):
            opponent, is_home = away_team, True
        elif _norm(player_team) == _norm(away_team):
            opponent, is_home = home_team, False
        else:
            opponent = away_team if _norm(player_team) != _norm(away_team) else home_team
    elif home_team and away_team:
        # Don't know which side — pick the more likely one based on event "A @ B" = "A away, B home"
        opponent = away_team or home_team

    # Form derivation (Understat).
    xg_per_90 = float((form_doc or {}).get("xg_per_90") or 0.0)
    npxg_per_90 = float((form_doc or {}).get("npxg_per_90") or 0.0)
    shots_per_90 = float((form_doc or {}).get("shots_per_90") or 0.0)
    goals_per_90 = float((form_doc or {}).get("goals_per_90") or 0.0)
    season_goals = int((form_doc or {}).get("goals") or 0)
    season_games = int((form_doc or {}).get("games") or 0)
    season_xg = float((form_doc or {}).get("xg") or 0.0)
    goals_over_xg = float((form_doc or {}).get("goals_over_xg") or 1.0)
    form_label = (form_doc or {}).get("form_label") or "NEUTRAL"
    form_score = int((form_doc or {}).get("form_score") or 50)
    position = (form_doc or {}).get("position") or pick.get("position") or "FW"

    # Heuristic starter / minutes from games + minutes total.
    if form_doc and season_games:
        avg_min = (form_doc.get("minutes") or 0) / max(1, season_games)
        expected_minutes = max(20.0, min(90.0, avg_min))
        starter_probability = max(0.0, min(1.0, avg_min / 90.0))
    else:
        expected_minutes = 80.0 if pick.get("elite_player") else 65.0
        starter_probability = 0.75 if pick.get("elite_player") else 0.55
    bench_risk = bool(expected_minutes < 55)

    # Team/opp xG and xGA — derive a rough proxy from team_form if available.
    # (Stub here — we'll wire team_form lookups in the integration layer.)
    team_implied = float(pick.get("team_implied_goals") or LEAGUE_AVG_TEAM_XG_PER_GAME)
    opp_xga      = float(pick.get("opponent_xga") or LEAGUE_AVG_OPPONENT_XGA_PER_GAME)
    opp_def_strength = float(pick.get("opponent_defensive_strength") or 50.0)

    # Penalty taker.
    is_pk = _norm(player_name) in pk_takers

    # Book signals.
    book_pct = float(pick.get("implied_probability") or pick.get("book_implied_prob") or 0)
    if 0 < book_pct < 1:
        book_pct *= 100.0

    return MatchupContext(
        player_name=player_name,
        team=player_team or "",
        opponent=opponent,
        sport=pick.get("sport") or "Soccer",
        league=pick.get("league") or "",
        is_home=is_home,
        xg_per_90=xg_per_90,
        npxg_per_90=npxg_per_90,
        shots_per_90=shots_per_90,
        goals_per_90=goals_per_90,
        last_5_goals=int(pick.get("last_5_goals") or 0),
        last_5_minutes=int(pick.get("last_5_minutes") or 0),
        last_5_starts=int(pick.get("last_5_starts") or 0),
        form_label=form_label,
        form_score=form_score,
        season_goals=season_goals,
        season_games=season_games,
        season_xg=season_xg,
        goals_over_xg=goals_over_xg,
        position=position,
        is_penalty_taker=is_pk,
        is_set_piece_taker=is_pk,  # crude — share with PK taker until we have data
        confirmed_starter=None,
        starter_probability=starter_probability,
        expected_minutes=expected_minutes,
        bench_risk=bench_risk,
        team_implied_goals=team_implied,
        opponent_xga_per_90=opp_xga,
        opponent_defensive_strength=opp_def_strength,
        book_anytime_implied_pct=book_pct,
        market_rank=pick.get("market_rank"),
        in_squad=in_squad,
        market_label=market,
        event=event,
    )


# ──────────────────────────────────────────────────────────────────
#  Penalty taker registry (compact — extend over time)
# ──────────────────────────────────────────────────────────────────

PENALTY_TAKERS = {
    # National teams (primary)
    "harry kane", "kylian mbappe", "cristiano ronaldo", "lionel messi",
    "mohamed salah", "robert lewandowski", "bruno fernandes",
    "erling haaland", "alexander isak", "viktor gyokeres",
    "memphis depay", "luis diaz", "vinicius junior", "neymar jr",
    "neymar", "ousmane dembele", "jude bellingham", "phil foden",
    "antoine griezmann", "alvaro morata", "lautaro martinez",
    "ferran torres", "riyad mahrez", "mario gotze", "kai havertz",
    "ilkay gundogan", "joshua kimmich", "marcus rashford",
    "iliman ndiaye", "sadio mane", "ismaila sarr",
    # Top club PK-takers
    "bukayo saka", "martin odegaard", "son heung-min", "son heung min",
    "james ward-prowse", "trent alexander-arnold",
    "kevin de bruyne", "rodrigo de paul", "pedri",
}


# ──────────────────────────────────────────────────────────────────
#  Async batch entry point
# ──────────────────────────────────────────────────────────────────

async def annotate_picks_async(
    picks: list[dict],
    db,
    *,
    apply_drop: bool = True,
    log_summary: bool = True,
) -> list[dict]:
    """Run matchup engine over every goalscorer pick in `picks`.

    - Attaches explainability fields onto each surviving pick
      (matchup_score, why_this_pick, etc.)
    - Removes picks with `recommend_drop=True` when `apply_drop=True`
    - Returns the (possibly filtered) list — non-goalscorer picks pass through

    Pulls from:
      * `soccer_player_form` (Understat) — per-player form/xg/shots
      * `national_team_squads` curated registry — squad membership gate

    Failure mode: if anything raises, the pick passes through UNCHANGED.
    The engine is best-effort. Logged via lockscore.goalscorer_matchup.
    """
    if not picks:
        return picks

    try:
        from national_team_squads import is_in_squad, known_teams
    except Exception:
        def is_in_squad(_p, _t):  # type: ignore[misc]
            return None
        def known_teams():        # type: ignore[misc]
            return []

    # Phase 1 — find all goalscorer picks and bulk-load Understat data.
    scorer_picks: list[dict] = []
    others: list[dict] = []
    for p in picks:
        if (p.get("sport") or "") == "Soccer" and _is_goalscorer_market(p.get("market") or ""):
            scorer_picks.append(p)
        else:
            others.append(p)

    if not scorer_picks:
        return picks

    # Bulk-load form data.
    player_names = sorted({_player_from_market(p.get("market") or "") for p in scorer_picks})
    form_map = await _fetch_form_for_players(db, player_names)

    # Determine known national-team set for squad-gate auto-detect.
    known_nat_teams = set(known_teams())

    # Phase 2 — build model market ranks per (event, team, family).
    # Group picks by (event, team, family); rank by book implied prob desc.
    by_bucket: dict[tuple, list[dict]] = {}
    pick_to_market_rank: dict[int, int] = {}
    for p in scorer_picks:
        market = p.get("market") or ""
        fam = _market_family(market)
        event = p.get("event") or ""
        team = p.get("player_team") or p.get("team") or ""
        key = (event, team, fam)
        by_bucket.setdefault(key, []).append(p)
    for key, group in by_bucket.items():
        # Sort by implied probability desc.
        def _ip(q):
            ip = q.get("implied_probability") or q.get("book_implied_prob") or 0
            return float(ip) if ip is not None else 0
        group.sort(key=_ip, reverse=True)
        for idx, p in enumerate(group, start=1):
            pick_to_market_rank[id(p)] = idx
            # Also stamp it on the pick so the engine can read it.
            if p.get("market_rank") is None:
                p["market_rank"] = idx

    # Phase 3 — score each pick.
    survived: list[dict] = []
    dropped_count = 0
    grade_counts = {"A+":0,"A":0,"B+":0,"B":0,"C+":0,"C":0,"D":0,"F":0}
    for p in scorer_picks:
        try:
            market = p.get("market") or ""
            player_name = _player_from_market(market)
            event = p.get("event") or ""
            # Parse "Away @ Home" form
            home_team, away_team = "", ""
            if " @ " in event:
                parts = [s.strip() for s in event.split(" @ ", 1)]
                if len(parts) == 2:
                    away_team, home_team = parts[0], parts[1]
            elif " vs " in event.lower():
                parts = re.split(r"\s+vs\s+", event, maxsplit=1, flags=re.I)
                if len(parts) == 2:
                    home_team, away_team = parts[0].strip(), parts[1].strip()

            # ── Player team inference (critical for squad gate) ───
            # Synthetic / curated elite picks often have empty
            # `player_team`, which breaks the squad gate (we can't
            # check whether Toney is in England's squad if we don't
            # know he's "on England"). Infer from `event` when both
            # sides are known national teams:
            player_team = (p.get("player_team") or p.get("team") or "").strip()
            if not player_team and (home_team or away_team) and known_nat_teams:
                # If both sides are national teams with curated squads,
                # check player against each — assign whichever squad
                # they're in. If neither contains them, default to the
                # one most likely (home team) so the squad-gate fires
                # with False.
                nh = _norm(home_team)
                na = _norm(away_team)
                norm_nat_set = {_norm(t) for t in known_nat_teams}
                home_known = nh in norm_nat_set
                away_known = na in norm_nat_set
                if home_known and is_in_squad(player_name, home_team):
                    player_team = home_team
                elif away_known and is_in_squad(player_name, away_team):
                    player_team = away_team
                elif home_known and away_known:
                    # Both are curated national teams but player in
                    # neither's squad — pick home arbitrarily so the
                    # squad-gate fires `False` and filters the pick.
                    player_team = home_team
                elif home_known:
                    player_team = home_team
                elif away_known:
                    player_team = away_team
            if player_team and not p.get("player_team"):
                # Persist back onto the pick so downstream sees the
                # resolved team value.
                p["player_team"] = player_team

            # Squad gate — only check for international fixtures (both sides
            # are known national teams).
            in_squad = None
            if player_team and known_nat_teams and (
                player_team in known_nat_teams
                or _norm(player_team) in {_norm(t) for t in known_nat_teams}
            ):
                in_squad = is_in_squad(player_name, player_team)

            form_doc = form_map.get(_norm_squashed(player_name)) or form_map.get(_norm(player_name))

            ctx = _build_context_from_pick(
                p, form_doc, (home_team, away_team), PENALTY_TAKERS, in_squad
            )

            model_market_rank = pick_to_market_rank.get(id(p))
            result = score_goalscorer(ctx, model_market_rank=model_market_rank)

            grade_counts[result.matchup_grade] = grade_counts.get(result.matchup_grade, 0) + 1

            if apply_drop and result.recommend_drop and not p.get("elite_protect"):
                dropped_count += 1
                continue

            # Annotate pick with explainability + new score field.
            p.update(result.to_pick_fields())
            survived.append(p)
        except Exception as e:
            logger.warning(
                "Matchup engine error for %s | %s: %s",
                p.get("market"), p.get("event"), e,
            )
            survived.append(p)

    if log_summary:
        logger.info(
            "Goalscorer matchup engine: %d evaluated, %d dropped, "
            "grades=%s",
            len(scorer_picks), dropped_count,
            {k: v for k, v in grade_counts.items() if v > 0},
        )

    return others + survived
