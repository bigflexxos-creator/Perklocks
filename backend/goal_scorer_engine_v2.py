"""GoalScorer Engine v2 — Poisson-per-player scoring probabilities.

Computes the full goal-scorer prop suite from a 12-feature player model:

    INPUTS:
        xG, xA, shot_volume, shot_quality, minutes_projection, team_xG,
        opponent_xGA, penalty_share, set_piece_share,
        opening_goal_involvement, lineup_certainty, recent_form

    OUTPUTS:
        P(anytime)         — at least one goal
        P(first)           — scores the FIRST goal of the match
        P(last)            — scores the LAST goal of the match
        P(2+)              — brace or hat-trick
        P(score_or_assist) — goal OR assist

Math
----
We model each player's match goal count G_p as Poisson(λ_p) where:

    λ_p = (team_xG · goal_share_p · lineup_factor) +
          penalty_share_p · league_pen_rate +
          set_piece_share_p · league_sp_rate

with:
    goal_share_p = base_share_from_form(xG, xA, shots) · matchup_mult
    matchup_mult = √(team_xG · opp_xGA / league_avg²)   ← strength-of-schedule
    lineup_factor = expected_minutes / 90

Then the Poisson tail identities give:
    P(anytime)  = 1 − e^−λ
    P(2+)       = 1 − e^−λ − λ·e^−λ
    P(first)    = (1 − P(0 goals in match)) · λ_p / λ_match
    P(last)     = symmetric ≈ P(first)     (Poisson process)
    P(score_or_assist) = P(goal) + (1 − P(goal)) · P(assist | no goal)

Calibration
-----------
After every match we store the prediction and grade it against FotMob's
goal/assist event feed. A rolling residual per (market, league) drives a
multiplicative calibration factor; the corrected probability is
    P_corrected = clamp(P_raw · calibration_mult, 0.001, 0.999).

Versioned via `calibration_version` so we can A/B old vs new offline.

Market residual
---------------
For every prediction we also store (book_implied − P_corrected). Rolling
average reveals when our model drifts from the market — early warning
that a feature pipeline broke.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger("lockscore.gs_engine_v2")


# ──────────────────────────────────────────────────────────────────
#   Constants — hierarchical fallback priors (per user spec)
# ──────────────────────────────────────────────────────────────────
ENGINE_VERSION       = "gs_v2.0.0"
CALIBRATION_VERSION  = "gs_v2.cal.2026-06-24"

# Position priors when historical data is unavailable.
PENALTY_POSITION_PRIOR = {
    "FW": 0.12, "F": 0.12, "ST": 0.12,
    "AM": 0.07, "CAM": 0.07, "AMC": 0.07,
    "W": 0.05, "LW": 0.05, "RW": 0.05, "WG": 0.05,
    "CM": 0.03, "M": 0.03, "DM": 0.03,
    "DEF": 0.01, "D": 0.01, "CB": 0.01, "LB": 0.01, "RB": 0.01, "WB": 0.01,
    "GK": 0.0,
}
SET_PIECE_POSITION_PRIOR = {
    "FW": 0.06, "F": 0.06,
    "AM": 0.14, "CAM": 0.14, "AMC": 0.14,
    "W": 0.10, "LW": 0.10, "RW": 0.10,
    "CM": 0.08, "DM": 0.04,
    "DEF": 0.02, "CB": 0.01,
    "GK": 0.0,
}

# Lineup certainty → expected-minutes multiplier (user spec).
LINEUP_CONFIDENCE_MULT = {
    "starting_xi":     1.00,
    "high_confidence": 0.85,
    "rotation":        0.60,
    "bench_risk":      0.35,
    "unknown":         0.20,
}

# League baseline rates per match (open-play goals per game, penalty
# rate per game, free-kick + corner-derived goals per game). Seeded
# from public soccer references; refined by calibration over time.
LEAGUE_DEFAULTS = {
    "open_play_goals_per_match":  2.30,
    "penalty_goals_per_match":    0.20,
    "set_piece_goals_per_match":  0.30,
    "first_goal_share":           0.50,   # % chance the team scores the opener
    "first_half_goal_share":      0.45,
}


# ──────────────────────────────────────────────────────────────────
#   Feature container — everything we need to score a single player
# ──────────────────────────────────────────────────────────────────
@dataclass
class PlayerFeatures:
    # Identity
    player:       str
    team:         str
    opponent:     str
    league:       str = ""

    # Volume / quality
    xG:                 float = 0.0   # season xG total
    xA:                 float = 0.0
    shot_volume:        float = 0.0   # shots per 90
    shot_quality:       float = 0.0   # xG per shot
    minutes_played:     int   = 0
    games_played:       int   = 0

    # Match-level model inputs
    minutes_projection: int   = 80    # raw expected minutes (pre-lineup mult)
    team_xG:            float = 1.20
    opponent_xGA:       float = 1.20

    # Set-piece / penalty hierarchy
    explicit_pen_taker:        Optional[bool]  = None
    historical_penalty_rate:   Optional[float] = None
    historical_kp_share:       Optional[float] = None
    crossing_share:            Optional[float] = None
    position:                  str             = "FW"

    # Opening-goal model
    historical_first_goal_share:    Optional[float] = None
    first_half_goal_share:          Optional[float] = None
    recent_form:                    Optional[float] = None   # 0..1
    starts:                         int             = 0

    # Lineup
    lineup_confidence:  str = "unknown"  # starting_xi | high_confidence | rotation | bench_risk | unknown


@dataclass
class FeatureSource:
    """Per-feature provenance — which tier of the hierarchy supplied each value.
    Stored alongside the prediction so we can audit 'why is Mbappé only
    getting position-prior penalty share?'"""
    penalty_share:           str = "position_prior"
    set_piece_share:         str = "position_prior"
    opening_goal_involvement: str = "position_prior"
    lineup_certainty:        str = "unknown"


@dataclass
class EngineOutputs:
    p_anytime:          float
    p_first:            float
    p_last:             float
    p_2plus:            float
    p_score_or_assist:  float
    lam_player:         float        # player's expected goals
    lam_match:          float        # total match expected goals
    expected_minutes:   float        # post-lineup-cert
    goal_share:         float        # share of team xG attributed to player
    team_xG:            float
    feature_snapshot:   dict
    feature_source:     dict
    calibration_version: str
    engine_version:     str


# ──────────────────────────────────────────────────────────────────
#   Feature resolvers (hierarchical fallbacks per user spec)
# ──────────────────────────────────────────────────────────────────
def _resolve_penalty_share(f: PlayerFeatures, src: FeatureSource) -> float:
    """explicit_taker > historical_penalty_rate > position_prior."""
    if f.explicit_pen_taker is True:
        src.penalty_share = "explicit_taker"
        return 0.85
    if isinstance(f.historical_penalty_rate, (int, float)) and f.historical_penalty_rate > 0:
        src.penalty_share = "historical_penalty_rate"
        return float(f.historical_penalty_rate)
    src.penalty_share = "position_prior"
    return PENALTY_POSITION_PRIOR.get(_norm_pos(f.position), 0.03)


def _resolve_set_piece_share(f: PlayerFeatures, src: FeatureSource) -> float:
    """historical_key_pass_share > crossing_share > position_prior."""
    if isinstance(f.historical_kp_share, (int, float)) and f.historical_kp_share > 0:
        src.set_piece_share = "historical_key_pass_share"
        return float(f.historical_kp_share)
    if isinstance(f.crossing_share, (int, float)) and f.crossing_share > 0:
        src.set_piece_share = "crossing_share"
        return float(f.crossing_share)
    src.set_piece_share = "position_prior"
    return SET_PIECE_POSITION_PRIOR.get(_norm_pos(f.position), 0.04)


def _resolve_opening_goal_involvement(f: PlayerFeatures, src: FeatureSource) -> float:
    """0.60 · first_goal_share + 0.25 · first_half_goal_share + 0.15 · recent_form
    shrunk toward league mean if starts < 10."""
    fgs = float(f.historical_first_goal_share or 0.0)
    fhgs = float(f.first_half_goal_share or 0.0)
    rf = float(f.recent_form or 0.5)
    raw = 0.60 * fgs + 0.25 * fhgs + 0.15 * rf
    league_mean = (
        0.60 * LEAGUE_DEFAULTS["first_goal_share"] +
        0.25 * LEAGUE_DEFAULTS["first_half_goal_share"] +
        0.15 * 0.50
    )
    starts = max(0, int(f.starts or 0))
    if starts < 10:
        # James-Stein-style shrinkage toward league mean.
        w = starts / 10.0
        out = w * raw + (1.0 - w) * league_mean
        src.opening_goal_involvement = "shrunk_small_sample"
        return out
    src.opening_goal_involvement = "historical_blend"
    return raw


def _resolve_lineup_certainty(f: PlayerFeatures, src: FeatureSource) -> float:
    """Returns the minutes-multiplier (NOT a probability factor)."""
    key = (f.lineup_confidence or "unknown").lower()
    src.lineup_certainty = key
    return LINEUP_CONFIDENCE_MULT.get(key, LINEUP_CONFIDENCE_MULT["unknown"])


def _norm_pos(pos: str) -> str:
    p = (pos or "").upper().strip()
    if not p:
        return "FW"
    # Take the first identifying token — "F S" → "F", "LW/CAM" → "LW".
    for sep in (" ", "/", ","):
        if sep in p:
            p = p.split(sep, 1)[0]
    # Map common variants.
    if p in {"F", "ST", "CF"}:               return "FW"
    if p in {"CAM", "AMC", "OAM", "ATT"}:    return "AM"
    if p in {"LW", "RW", "WG", "LM", "RM"}:  return "W"
    if p in {"DM", "DMF", "CDM"}:            return "DM"
    if p in {"CB", "LB", "RB", "LWB", "RWB"}: return "DEF"
    return p


# ──────────────────────────────────────────────────────────────────
#   The model
# ──────────────────────────────────────────────────────────────────
def compute_probabilities(features: PlayerFeatures,
                          *,
                          calibration_mult: float = 1.0,
                          league_avg_team_xG: float = 1.30,
                          ) -> EngineOutputs:
    """Run the v2 engine on a single player → full prop suite.

    `calibration_mult` ≥ 0 is the rolling multiplicative correction
    derived from `compute_calibration_factors()`. Defaults to 1.0 so
    a fresh engine output equals the raw Poisson result.
    """
    src = FeatureSource()

    # ── 1. Effective minutes ─────────────────────────────────────
    minutes_mult = _resolve_lineup_certainty(features, src)
    expected_minutes = max(0.0, float(features.minutes_projection or 0)) * minutes_mult

    # ── 2. Goal-share — what fraction of team xG belongs to this player ──
    # Base from xG/90 normalised against team_xG.
    minutes_per_90 = max(1.0, float(features.minutes_played) / 90.0)
    xg_per_90 = (features.xG or 0.0) / minutes_per_90
    base_share = 0.0
    if features.team_xG > 0:
        base_share = min(0.95, xg_per_90 / max(0.4, features.team_xG))
    # Strength-of-schedule multiplier.
    sos = 1.0
    if features.team_xG > 0 and features.opponent_xGA > 0 and league_avg_team_xG > 0:
        sos = math.sqrt(
            (features.team_xG * features.opponent_xGA) / (league_avg_team_xG ** 2)
        )
    goal_share = base_share * sos
    goal_share = max(0.0, min(0.95, goal_share))

    # ── 3. Player λ from open-play + penalties + set pieces ──────
    open_play_lam = (features.team_xG or LEAGUE_DEFAULTS["open_play_goals_per_match"]) \
                     * goal_share * (expected_minutes / 90.0)
    pen_share = _resolve_penalty_share(features, src)
    sp_share = _resolve_set_piece_share(features, src)
    pen_lam = pen_share * LEAGUE_DEFAULTS["penalty_goals_per_match"]
    sp_lam  = sp_share  * LEAGUE_DEFAULTS["set_piece_goals_per_match"]
    lam_player = open_play_lam + pen_lam + sp_lam
    lam_player = max(0.001, min(4.0, lam_player))

    # Match λ — combine both teams' expected goals.
    lam_match = (features.team_xG or 0.0) + (features.opponent_xGA or 0.0)
    lam_match = max(0.5, lam_match)

    # ── 4. Poisson tail identities ────────────────────────────────
    e_neg_lam = math.exp(-lam_player)
    p_anytime = 1.0 - e_neg_lam
    p_2plus   = max(0.0, 1.0 - e_neg_lam - lam_player * e_neg_lam)

    # P(first) = P(some goal in match) · share of player among match-λ.
    p_match_has_goal = 1.0 - math.exp(-lam_match)
    p_first_given_goal = lam_player / lam_match if lam_match > 0 else 0.0
    p_first = p_match_has_goal * p_first_given_goal

    # Apply opening-goal involvement as a *small* multiplicative tilt.
    ogi = _resolve_opening_goal_involvement(features, src)
    # ogi is centered around ~0.5 (league mean); >0.5 = above avg starter scorer.
    ogi_mult = 0.5 + ogi   # 1.0 at league mean, up to ~1.5 for elite openers
    p_first = max(0.0, min(0.95, p_first * ogi_mult))

    # P(last) — by symmetry of stationary Poisson, equal to P(first)
    # given at least one goal. We apply a small recency tilt so late-game
    # subs / closers get a marginal lift.
    p_last = p_first  # symmetric — no extra tilt without lineup-timing data

    # ── 5. Score-or-assist via assist Poisson ────────────────────
    xa_per_90 = (features.xA or 0.0) / minutes_per_90
    lam_assist_player = xa_per_90 * (expected_minutes / 90.0)
    lam_assist_player = max(0.0, min(3.0, lam_assist_player))
    p_anytime_assist = 1.0 - math.exp(-lam_assist_player)
    # P(SoA) = 1 − P(no goal) · P(no assist) approximating independence.
    p_no_goal_no_assist = math.exp(-(lam_player + lam_assist_player))
    p_score_or_assist = 1.0 - p_no_goal_no_assist

    # ── 6. Calibration application ────────────────────────────────
    def _cal(p: float) -> float:
        return max(0.001, min(0.999, p * calibration_mult))

    p_anytime         = _cal(p_anytime)
    p_first           = _cal(p_first)
    p_last            = _cal(p_last)
    p_2plus           = _cal(p_2plus)
    p_score_or_assist = _cal(p_score_or_assist)

    feature_snapshot = {
        "xG":                features.xG,
        "xA":                features.xA,
        "shot_volume":       features.shot_volume,
        "shot_quality":      features.shot_quality,
        "minutes_projection": features.minutes_projection,
        "team_xG":           features.team_xG,
        "opponent_xGA":      features.opponent_xGA,
        "penalty_share":     pen_share,
        "set_piece_share":   sp_share,
        "opening_goal_involvement": ogi,
        "lineup_certainty":  minutes_mult,
        "recent_form":       features.recent_form,
        "position":          features.position,
        "minutes_played":    features.minutes_played,
        "games_played":      features.games_played,
        "starts":            features.starts,
    }

    return EngineOutputs(
        p_anytime=round(p_anytime, 4),
        p_first=round(p_first, 4),
        p_last=round(p_last, 4),
        p_2plus=round(p_2plus, 4),
        p_score_or_assist=round(p_score_or_assist, 4),
        lam_player=round(lam_player, 4),
        lam_match=round(lam_match, 4),
        expected_minutes=round(expected_minutes, 1),
        goal_share=round(goal_share, 4),
        team_xG=features.team_xG,
        feature_snapshot=feature_snapshot,
        feature_source=asdict(src),
        calibration_version=CALIBRATION_VERSION,
        engine_version=ENGINE_VERSION,
    )


# ──────────────────────────────────────────────────────────────────
#   Storage — predictions + grading + calibration
# ──────────────────────────────────────────────────────────────────
async def store_prediction(db, *, fixture_id, event: str, player: str,
                            team: str, opponent: str, league: str,
                            outputs: EngineOutputs,
                            book_prices: Optional[dict] = None) -> str:
    """Persist the v2 prediction for later grading.

    `book_prices` may include keys: anytime / first / last / 2plus /
    score_or_assist mapped to American odds. Stored as-is so the
    market-residual report has both sides.
    """
    pred_id = f"gsv2_{fixture_id}_{_slug(player)}"
    doc = {
        "id":              pred_id,
        "fixture_id":      str(fixture_id),
        "event":           event,
        "player":          player,
        "team":            team,
        "opponent":        opponent,
        "league":          league,
        "engine_version":  outputs.engine_version,
        "calibration_version": outputs.calibration_version,
        "created_at":      datetime.now(timezone.utc),
        "predictions": {
            "p_anytime":         outputs.p_anytime,
            "p_first":           outputs.p_first,
            "p_last":            outputs.p_last,
            "p_2plus":           outputs.p_2plus,
            "p_score_or_assist": outputs.p_score_or_assist,
        },
        "expected_minutes": outputs.expected_minutes,
        "goal_share":       outputs.goal_share,
        "team_xG":          outputs.team_xG,
        "lam_player":       outputs.lam_player,
        "lam_match":        outputs.lam_match,
        "feature_snapshot": outputs.feature_snapshot,
        "feature_source":   outputs.feature_source,
        "book_prices":      book_prices or {},
        "graded":           False,
    }
    await db.gs_v2_predictions.update_one(
        {"id": pred_id}, {"$set": doc}, upsert=True,
    )
    return pred_id


async def grade_pending_predictions(db, *, lookback_days: int = 3) -> dict:
    """Grade un-graded predictions older than 90 min against FotMob actuals.

    Returns a summary dict logged by the scheduler.
    """
    from soccer_fotmob_settle import _http_get  # reuse the FotMob client

    cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
    floor  = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    cursor = db.gs_v2_predictions.find({
        "graded":      False,
        "created_at":  {"$gte": floor, "$lte": cutoff},
    })

    summary = {"scanned": 0, "graded": 0, "skipped_no_match": 0, "errors": 0}
    async for pred in cursor:
        summary["scanned"] += 1
        try:
            fid = pred.get("fixture_id")
            if not fid:
                summary["skipped_no_match"] += 1
                continue
            detail = await _http_get("matchDetails", {"matchId": str(fid)})
            if not detail:
                summary["skipped_no_match"] += 1
                continue
            outcome = _grade_one(pred, detail)
            if outcome is None:
                summary["skipped_no_match"] += 1
                continue
            await db.gs_v2_predictions.update_one(
                {"id": pred["id"]},
                {"$set": {
                    "graded":          True,
                    "outcome":         outcome,
                    "graded_at":       datetime.now(timezone.utc),
                }},
            )
            summary["graded"] += 1
        except Exception as e:
            logger.warning("gs_v2 grade error pred=%s: %s", pred.get("id"), e)
            summary["errors"] += 1

    if summary["graded"]:
        await compute_calibration_factors(db)
    logger.info("GoalScorer v2 grading: %s", summary)
    return summary


def _grade_one(pred: dict, detail: dict) -> Optional[dict]:
    """Extract actual outcomes for a graded prediction from FotMob detail."""
    content = detail.get("content") or {}
    mf = content.get("matchFacts") or {}
    events = mf.get("events") or {}
    ev_list = events.get("events") if isinstance(events, dict) else events
    if not ev_list:
        return None

    player_norm = (pred.get("player") or "").lower()
    goals: list = []
    assists: list = []
    for e in ev_list:
        type_ = (e.get("type") or "").lower()
        if "goal" not in type_:
            continue
        if "own" in type_ or "miss" in type_ or "shootout" in str(e.get("ownGoal", "")).lower():
            continue
        scorer = ""
        assister = ""
        p = e.get("player") or {}
        if isinstance(p, dict):
            scorer = (p.get("name") or "").lower()
        # FotMob exposes assister in `assistPlayer` or similar.
        ap = e.get("assistPlayer") or e.get("assist") or {}
        if isinstance(ap, dict):
            assister = (ap.get("name") or "").lower()
        if scorer and player_norm and (scorer == player_norm or scorer.endswith(" " + player_norm.split()[-1])):
            goals.append(e)
        if assister and player_norm and (assister == player_norm or assister.endswith(" " + player_norm.split()[-1])):
            assists.append(e)

    goals_n = len(goals)
    assists_n = len(assists)
    return {
        "actual_goals":            goals_n,
        "actual_assists":          assists_n,
        "scored":                  goals_n >= 1,
        "first_goal":              bool(goals and goals[0].get("isFirstGoalInMatch", False)),
        "last_goal":               False,  # FotMob doesn't tag this — derive in calibration pass if needed
        "two_plus":                goals_n >= 2,
        "score_or_assist":         (goals_n + assists_n) >= 1,
    }


# ──────────────────────────────────────────────────────────────────
#   Calibration — rolling per-(market, league) multiplier
# ──────────────────────────────────────────────────────────────────
async def compute_calibration_factors(db, *, min_sample: int = 40) -> dict:
    """Compute multiplicative calibration factor per (market, league).

    factor = mean(actual_hit) / mean(predicted_prob)
    Clamped to [0.5, 1.5] to stop a thin sample from blowing up the model.
    Stored to `gs_v2_calibration` keyed by (market, league).
    """
    out = {"updated_keys": [], "skipped_thin_sample": []}
    markets = ["p_anytime", "p_first", "p_2plus", "p_score_or_assist"]
    outcome_keys = {
        "p_anytime":         "scored",
        "p_first":           "first_goal",
        "p_2plus":           "two_plus",
        "p_score_or_assist": "score_or_assist",
    }

    pipeline = [
        {"$match": {"graded": True, "outcome": {"$ne": None}}},
        {"$group": {
            "_id": "$league",
            "rows": {"$push": {
                "predictions": "$predictions",
                "outcome":     "$outcome",
            }},
        }},
    ]
    async for league_doc in db.gs_v2_predictions.aggregate(pipeline):
        league = league_doc["_id"] or "GLOBAL"
        rows = league_doc.get("rows") or []
        for market in markets:
            ok_key = outcome_keys[market]
            preds = [(r.get("predictions") or {}).get(market) for r in rows]
            preds = [p for p in preds if isinstance(p, (int, float))]
            outs  = [(r.get("outcome") or {}).get(ok_key) for r in rows]
            outs  = [bool(o) for o in outs if o is not None]
            n = min(len(preds), len(outs))
            if n < min_sample:
                out["skipped_thin_sample"].append({"league": league, "market": market, "n": n})
                continue
            mean_pred = sum(preds[:n]) / n if n else 0
            mean_actual = sum(outs[:n]) / n if n else 0
            factor = (mean_actual / mean_pred) if mean_pred > 0 else 1.0
            factor = max(0.5, min(1.5, factor))
            await db.gs_v2_calibration.update_one(
                {"_id": f"{league}::{market}"},
                {"$set": {
                    "league":         league,
                    "market":         market,
                    "factor":         round(factor, 4),
                    "mean_predicted": round(mean_pred, 4),
                    "mean_actual":    round(mean_actual, 4),
                    "n":              n,
                    "updated_at":     datetime.now(timezone.utc),
                    "version":        CALIBRATION_VERSION,
                }},
                upsert=True,
            )
            out["updated_keys"].append({"league": league, "market": market,
                                         "factor": factor, "n": n})
    return out


async def get_calibration_factor(db, *, league: str, market: str) -> float:
    """Lookup the latest calibration multiplier. Falls back to GLOBAL→1.0."""
    for key_league in (league, "GLOBAL"):
        row = await db.gs_v2_calibration.find_one({"_id": f"{key_league}::{market}"})
        if row and isinstance(row.get("factor"), (int, float)):
            return float(row["factor"])
    return 1.0


async def market_residual_report(db, *, league: Optional[str] = None,
                                  market: str = "p_anytime",
                                  days_back: int = 30) -> dict:
    """Rolling (book_implied − model) gap report.

    Drives the early-warning monitor: if the residual systematically
    drifts > 0.05 in either direction, the feature pipeline likely broke.
    """
    floor = datetime.now(timezone.utc) - timedelta(days=days_back)
    q = {"created_at": {"$gte": floor},
         "book_prices": {"$exists": True, "$ne": {}}}
    if league:
        q["league"] = league
    diffs = []
    cursor = db.gs_v2_predictions.find(q, {"predictions": 1, "book_prices": 1, "league": 1})
    market_book_key = market.replace("p_", "")
    async for r in cursor:
        bp = (r.get("book_prices") or {}).get(market_book_key)
        mp = (r.get("predictions") or {}).get(market)
        if not isinstance(bp, (int, float)) or not isinstance(mp, (int, float)):
            continue
        # American odds → implied probability
        a = float(bp)
        implied = (-a / (-a + 100)) if a < 0 else (100 / (a + 100))
        diffs.append(implied - mp)
    if not diffs:
        return {"n": 0, "mean_residual": None, "stddev": None}
    mean = sum(diffs) / len(diffs)
    var = sum((d - mean) ** 2 for d in diffs) / len(diffs)
    return {
        "market":        market,
        "league":        league or "ALL",
        "n":             len(diffs),
        "mean_residual": round(mean, 4),
        "stddev":        round(math.sqrt(var), 4),
        "interpretation":
            "Positive = book overrates players (we should fade); "
            "negative = book underrates (model finds edge).",
    }


# ──────────────────────────────────────────────────────────────────
#   Helpers
# ──────────────────────────────────────────────────────────────────
def _slug(s: str) -> str:
    return "".join(c for c in (s or "").lower() if c.isalnum() or c == "_")[:48]
