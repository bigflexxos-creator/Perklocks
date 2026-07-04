"""Parlay Optimizer V1 — highest-probability parlay builder.

Goal: build parlays with the HIGHEST PROBABILITY of all legs surviving — not
the biggest payout.  Sample preference (per spec):

    Preferred:  Leg A 90%  Leg B 89%  Leg C 87%   (Survival ≈ 70%)
    NOT:        Leg A 95%  Leg B 55%  Leg C 50%   (Survival ≈ 26%)

The optimizer enforces:

1. **Hard eligibility**: Lock ≥ 88, Edge ≥ +3%, no `no_bet`, no negative
   bucket ROI, no extreme volatility flags.
2. **Per-leg composite score**: 40 % lock + 25 % edge + 20 % bucket ROI +
   10 % correlation (penalty) + 5 % market stability.
3. **Survival damage control**: BEFORE adding a leg, compare current vs
   projected survival.  Reject the leg if survival drops too much (relative
   drop > `MAX_RELATIVE_DROP` or absolute drop > `MAX_ABS_DROP`).
4. **Stop when quality drops**: never force max legs.  If 8 legs is stronger
   than 15, return 8.
5. **No filler legs**: every leg must improve avg edge AND avg ROI.
6. **Diversification**: max 40 % same sport, max 2 same game.
7. **Anti-hero detection**: penalise inflated odds / extreme recent streak /
   low sample picks.
8. **Smart build**: generate ~50 candidate parlays, score, return Top 3
   labelled SAFE / BALANCED / AGGRESSIVE.
9. **Health grade**: A / B / C / D / F per parlay.
10. **"Why this parlay"** human-readable reasoning string.
"""
from __future__ import annotations

import itertools
import logging
import math
import random
from typing import Any

logger = logging.getLogger("lockscore.parlay")


# ──────────────────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────────────────

# Eligibility thresholds (per spec)
MIN_LOCK_SCORE = 88.0
MIN_EDGE_PCT = 3.0

# Survival damage-control thresholds.  When adding a leg drops combined
# survival probability by more than MAX_REL_DROP relative or MAX_ABS_DROP
# absolute, the leg is rejected.
MAX_REL_DROP_STANDARD = 0.25   # 25 % relative drop allowed for standard mode
MAX_REL_DROP_HIGH_RISK = 0.45  # 45 % for high-risk parlays (lottery)
MAX_ABS_DROP_STANDARD = 0.15   # 15 percentage points absolute
MAX_ABS_DROP_HIGH_RISK = 0.30  # 30 pp absolute for high-risk

# Diversification (per spec, leg-count-aware soft limits)
# These define the maximum number of legs that may share the same sport.
# SINGLE-SPORT mode bypasses these entirely.
#
# User feedback (parlay "all soccer" complaint): even with the 50 % cap
# for 6-10 leg parlays, a 5-of-9 soccer card *feels* sport-monocultural
# because the 4 non-soccer legs split across MLB/Tennis. Tightened to
# 40 % for the 6-10 band and 33 % for 11-20 leg high-risk so the user
# always sees ≥3 sports on a 9-leg ticket and ≥4 sports on a 12-leg.
def max_same_sport_for_target(target_legs: int) -> int:
    """Per-spec soft limit on legs sharing the same sport, by target size."""
    if target_legs <= 5:
        return 2                       # 2-5 legs:   max 2 same sport
    if target_legs <= 10:
        return max(2, (target_legs * 4) // 10)  # 6-10 legs: max 40% same sport
    return max(3, target_legs // 3)             # 11-20 legs: max 33% same sport

MAX_SAME_GAME = 2              # Max 2 legs from same game (always)

# Per-leg composite score weights — REBALANCED so win_probability is a
# first-class signal. The old formula ignored win_probability entirely
# (it sat in a comment as "reserved for future calibration"), which is
# why the optimizer kept picking Win-or-Draw rows whose lock=98 masked
# their middling win%. User spec: "should picking highest winning pct
# pick into parlay simulator". New mix:
#   30% Lock + 30% Win Probability + 20% Edge + 12% ROI + 5% Correlation + 3% Stability
# This gives equal voice to model confidence (win_p) and market-side
# confidence (lock_score) so the parlay can never again build a 5-leg
# soccer ML monoculture on lock-score alone.
W_LOCK = 0.30
W_WIN_PROB = 0.30
W_EDGE = 0.20
W_ROI = 0.12
W_CORRELATION = 0.05
W_MARKET_STABILITY = 0.03

# Max legs per parlay belonging to the same MARKET FAMILY (Win-or-Draw,
# Moneyline, Goal Scorer, Over/Under, …). Prevents the "all Win-or-Draw"
# Soccer high-risk parlay that prompted this overhaul.
MAX_SAME_MARKET_FAMILY = 2

# Health grade thresholds (parlay-level composite)
GRADE_THRESHOLDS = [
    ("A", 85.0),
    ("B", 72.0),
    ("C", 58.0),
    ("D", 45.0),
    ("F", 0.0),
]

# How many candidate parlays to generate before ranking (per spec)
N_CANDIDATES = 50

# Risk labels and their target survival bands.  We pick one candidate from
# each band so the user gets meaningful diversity.
RISK_LABELS = [
    ("SAFE", 0.55, 1.00),
    ("BALANCED", 0.30, 0.55),
    ("AGGRESSIVE", 0.10, 0.30),
]


# ──────────────────────────────────────────────────────────────────────────
# BUCKET ROI HELPERS (uses learning_system_v2's market performance map)
# ──────────────────────────────────────────────────────────────────────────

def _market_family(market: str) -> str:
    """Coarse market family for ROI bucket lookup."""
    m = (market or "").lower()
    if "anytime goal scorer" in m:
        return "goal_scorer"
    if "first goal scorer" in m:
        return "first_goal"
    if "to score or assist" in m:
        return "score_or_assist"
    if "win or draw" in m or "double chance" in m:
        return "win_or_draw"
    if "moneyline" in m:
        return "moneyline"
    if "spread" in m or "run line" in m:
        return "spread"
    if "over" in m and ("hits" in m or "total bases" in m):
        return "batter_over"
    if "over" in m or "under" in m:
        return "total_over_under"
    if "wins by" in m:
        return "mma_method"
    return "other"


def _leg_bucket_roi(pick: dict, bucket_map: dict) -> float:
    """Look up historical ROI for the (sport, market_family) of this leg.

    bucket_map shape: {(sport_lower, family): {"roi": pct, "n": int, ...}}
    Returns ROI as a fraction (0.05 = +5 %), or 0.0 if no data / low sample.
    """
    if not bucket_map:
        return 0.0
    sport = (pick.get("sport") or "").lower()
    family = _market_family(pick.get("market") or "")
    row = bucket_map.get((sport, family))
    if not row or row.get("n", 0) < 8:
        return 0.0
    return float(row.get("roi", 0.0))


# ──────────────────────────────────────────────────────────────────────────
# ANTI-HERO DETECTION
# ──────────────────────────────────────────────────────────────────────────

def detect_anti_hero(pick: dict, bucket_map: dict) -> dict:
    """Return {"is_anti_hero": bool, "penalty": float (0-30), "reasons": [..]}

    Flags suspicious picks:
      • inflated odds for low win-prob (book is sharper than model)
      • bucket ROI deeply negative
      • extreme recent streak from learning system
      • low historical sample for the market bucket
    """
    reasons = []
    penalty = 0.0

    win_p = float(pick.get("win_probability") or 0)
    edge = float(pick.get("edge_percent") or 0)
    book_odds = float(pick.get("book_odds") or 0)

    # ─ Inflated odds: book offering a juicy price suggests sharps fading.
    # If odds are +250+ but our model win-prob is only 30-40 %, that's
    # exactly the "book knows something" zone.
    if book_odds >= 250 and win_p < 45:
        reasons.append("Inflated odds vs low model prob")
        penalty += 8

    # ─ Edge too good to be true: 15 %+ edge is rare and often a stale line
    # or a market the book is intentionally mispricing because they know more.
    if edge > 15 and win_p < 70:
        reasons.append("Suspicious edge (line may be stale)")
        penalty += 6

    # ─ Bucket ROI deeply negative
    sport = (pick.get("sport") or "").lower()
    family = _market_family(pick.get("market") or "")
    row = bucket_map.get((sport, family)) if bucket_map else None
    if row and row.get("n", 0) >= 15:
        roi = float(row.get("roi", 0.0))
        if roi < -0.08:  # bucket losing 8 %+ over the long run
            reasons.append(f"Bucket ROI {roi:+.0%} (losing market)")
            penalty += 10
        elif roi < -0.04:
            reasons.append(f"Bucket ROI {roi:+.0%}")
            penalty += 4

    # ─ Low sample bucket: we don't trust unknown territory at high edge
    if (not row or row.get("n", 0) < 8) and edge > 8:
        reasons.append("Unknown market sample (low confidence)")
        penalty += 5

    # ─ Volatile recent streak (if learning_v2 surfaces it)
    streak = pick.get("recent_streak_volatility")
    if streak and abs(float(streak)) > 0.30:
        reasons.append("Extreme recent streak (mean reversion risk)")
        penalty += 7

    return {
        "is_anti_hero": penalty >= 12,
        "penalty": min(penalty, 30.0),
        "reasons": reasons,
    }


# ──────────────────────────────────────────────────────────────────────────
# PER-LEG COMPOSITE SCORE (0-100)
# ──────────────────────────────────────────────────────────────────────────

def score_leg(pick: dict, bucket_map: dict, current_legs: list[dict],
              *, target_legs: int = 5, single_sport_mode: bool = False,
              synergy_map: dict | None = None) -> float:
    """Composite leg score 0-100 per spec weighting:
        30 % Lock  +  30 % Win Probability  +  20 % Edge  +  12 % ROI
        +  5 % Correlation  +  3 % Stability  +  Synergy Bonus (-15..+15)
    `current_legs` is the parlay-in-progress (for correlation calc).
    `target_legs` & `single_sport_mode` change the same-sport penalty.
    `synergy_map` is the learned per-(sport, market_family) parlay hit rate
    map from parlay_learning. When provided, the score gets a small bonus
    for combos historically won and a penalty for combos historically lost.
    """
    lock = float(pick.get("lock_score") or 0)
    edge = float(pick.get("edge_percent") or 0)
    win_p = float(pick.get("win_probability") or 0)

    # Lock component (already 0-99) — clamp to 0-100
    lock_component = min(100.0, max(0.0, lock))

    # Win-probability component. Spec: should "pick highest winning pct
    # picks". Map 50 % → 0, 90 % → 80, 99 % → 98. So a pick at 60 % gets
    # 20, at 75 % gets 50, at 90 % gets 80. This makes win_p a major
    # signal alongside lock_score instead of an ignored bystander.
    win_prob_component = min(100.0, max(0.0, (win_p - 50.0) * 2.0))

    # Edge component: map -5 → 0, +5 → 50, +15+ → 100
    edge_component = min(100.0, max(0.0, (edge + 5.0) * 10.0))

    # ROI component: map -10 % → 0, 0 → 50, +20 %+ → 100
    roi = _leg_bucket_roi(pick, bucket_map)
    roi_component = min(100.0, max(0.0, (roi * 100.0 + 10.0) * (100.0 / 30.0)))

    # Correlation component: penalty if same sport/event/player already in parlay
    correlation_component = 100.0
    if current_legs:
        same_sport = sum(1 for L in current_legs
                        if (L.get("sport") or "") == (pick.get("sport") or ""))
        same_event = sum(1 for L in current_legs
                        if (L.get("event") or "") == (pick.get("event") or ""))
        same_player = 0
        pick_player = pick.get("elite_player_name")
        if pick_player:
            same_player = sum(1 for L in current_legs
                            if L.get("elite_player_name") == pick_player)
        # Leg-count-aware same-sport tolerance.
        # In single-sport mode there is no penalty.
        if not single_sport_mode:
            max_same = max_same_sport_for_target(target_legs)
            if (same_sport + 1) > max_same:
                correlation_component -= 30
        if same_event >= MAX_SAME_GAME:
            correlation_component -= 50
        elif same_event == 1:
            correlation_component -= 15  # mild penalty for same game
        if same_player >= 1:
            correlation_component -= 25  # same player = correlated outcome
    correlation_component = max(0.0, correlation_component)

    # Market stability: prefer mains over alts, prefer non-synthetic markets
    stability_component = 100.0
    if pick.get("is_alt"):
        stability_component -= 30
    if pick.get("synthetic_fgs"):  # synthetic FGS has higher variance
        stability_component -= 20
    if pick.get("synthetic_ags"):
        stability_component -= 10
    if pick.get("synthetic_soa"):
        stability_component -= 8
    # First Goal Scorer is inherently higher variance than Anytime Goal
    # Scorer (only ONE player scores first vs anyone scoring anytime).
    # AGS win rate is ~3x FGS. User complaint 2026-07-04 "shouldn't it
    # be anytime goalscorer" — heavily deprioritise FGS so the optimizer
    # gravitates to AGS whenever a player has both markets available.
    if "first goal scorer" in (pick.get("market") or "").lower() \
       or "first scorer" in (pick.get("market") or "").lower():
        stability_component -= 45
    stability_component = max(0.0, stability_component)

    composite = (
        W_LOCK * lock_component
        + W_WIN_PROB * win_prob_component
        + W_EDGE * edge_component
        + W_ROI * roi_component
        + W_CORRELATION * correlation_component
        + W_MARKET_STABILITY * stability_component
    )

    # Apply anti-hero penalty
    anti = detect_anti_hero(pick, bucket_map)
    composite -= anti["penalty"]

    # Apply learned parlay synergy bonus / penalty. When the (sport,
    # market_family) of this leg has a track record of cashing parlays
    # in our settled history (≥3 settled parlays of evidence), nudge the
    # composite up to +15 / down to -15. Picks that LOOK strong but
    # systematically tank our parlays (the kind of bug the user wanted
    # solved) get scored down here. Picks that quietly cash parlays
    # over and over get an organic boost.
    if synergy_map is not None:
        from parlay_learning import synergy_bonus  # local import — avoid cycle
        family = _market_family(pick.get("market") or "")
        sport = pick.get("sport") or ""
        composite += synergy_bonus(synergy_map, sport, family)

    return max(0.0, min(100.0, composite))


# ──────────────────────────────────────────────────────────────────────────
# ELIGIBILITY FILTER
# ──────────────────────────────────────────────────────────────────────────

def is_eligible_leg(pick: dict, bucket_map: dict, *, high_risk: bool = False) -> tuple[bool, str]:
    """Hard filters per spec.  Returns (ok, reason_if_rejected)."""
    if pick.get("no_bet"):
        return False, "no_bet flag"
    if pick.get("is_under_lock"):
        return False, "under_lock"

    lock = float(pick.get("lock_score") or 0)
    edge = float(pick.get("edge_percent") or 0)
    win_p = float(pick.get("win_probability") or 0)

    # High-risk mode: relax lock to 75 but require positive edge.
    # Standard: full spec Lock≥88, Edge≥+3.
    min_lock = 75.0 if high_risk else MIN_LOCK_SCORE
    min_edge = 1.0 if high_risk else MIN_EDGE_PCT

    # Alt-prop carve-out (added 2026-06-23 per user spec "Add ALT
    # picks to the Parlay Optimizer's eligible legs"). Chalk-ladder
    # alt-spread picks (e.g. "Svitolina -1.5 Games (Alt)") and the
    # Tennis/MLB alt-total ladders are book-anchored chalkier-than-
    # main-line bets where the FAVORED side is priced at -300 to
    # -800 — by construction the model edge sits at +1 to +2%, well
    # below the standard +3% gate. They have strong locks (≥95) and
    # win-probabilities ≥75%, so they DESERVE to be in the parlay
    # leg pool. Carve-out: any leg flagged `is_alt` (or legacy
    # `is_alt_prop`) clears with min_edge of +1.0% without forcing
    # the user into high-risk mode globally.
    if (pick.get("is_alt") or pick.get("is_alt_prop")) and min_edge > 1.0:
        min_edge = 1.0

    if lock < min_lock:
        return False, f"lock {lock:.0f} < {min_lock:.0f}"
    if edge < min_edge:
        return False, f"edge {edge:+.1f}% < {min_edge:+.0f}%"
    if win_p <= 0:
        return False, "no win_probability"

    # Bucket ROI must be non-negative (with sample) — standard mode only
    if not high_risk:
        sport = (pick.get("sport") or "").lower()
        family = _market_family(pick.get("market") or "")
        row = bucket_map.get((sport, family)) if bucket_map else None
        if row and row.get("n", 0) >= 20 and float(row.get("roi", 0.0)) < -0.05:
            return False, f"bucket ROI {row['roi']:+.0%} (losing)"

    return True, ""


# ──────────────────────────────────────────────────────────────────────────
# SURVIVAL CALCULATIONS
# ──────────────────────────────────────────────────────────────────────────

def parlay_survival(legs: list[dict], correlation_haircut: bool = True) -> float:
    """Combined survival probability of all legs cashing.

    Pure product of individual model win-probabilities, with an optional
    correlation haircut: each additional leg from the same SPORT shaves
    1.5 % off the joint probability, and each leg from the same EVENT
    shaves 8 %.  (Crude but well-calibrated for typical parlay correlations.)

    Hardened against malformed input — any leg without a usable win_probability
    is skipped from the product (returns conservative answer rather than crash).
    """
    if not legs or not isinstance(legs, list):
        return 1.0
    prob = 1.0
    valid_legs = []
    for L in legs:
        if not isinstance(L, dict):
            continue
        try:
            wp_raw = L.get("win_probability")
            if wp_raw is None:
                continue
            wp = max(0.01, min(0.99, float(wp_raw) / 100.0))
            prob *= wp
            valid_legs.append(L)
        except (TypeError, ValueError):
            continue
    if correlation_haircut and valid_legs:
        from collections import Counter
        sport_counts = Counter((L.get("sport") or "") for L in valid_legs)
        event_counts = Counter((L.get("event") or "") for L in valid_legs)
        for sport, c in sport_counts.items():
            if c > 1:
                prob *= (1.0 - 0.015) ** (c - 1)
        for event, c in event_counts.items():
            if c > 1:
                prob *= (1.0 - 0.08) ** (c - 1)
    return prob


def damage_control_ok(current_legs: list[dict], candidate: dict, *,
                     high_risk: bool = False) -> tuple[bool, float, float]:
    """Returns (ok, current_survival, new_survival).

    Rejects candidate when adding it drops survival too aggressively.
    """
    current = parlay_survival(current_legs)
    proposed = parlay_survival(current_legs + [candidate])
    abs_drop = current - proposed
    rel_drop = abs_drop / max(current, 1e-9)
    max_rel = MAX_REL_DROP_HIGH_RISK if high_risk else MAX_REL_DROP_STANDARD
    max_abs = MAX_ABS_DROP_HIGH_RISK if high_risk else MAX_ABS_DROP_STANDARD
    ok = (rel_drop <= max_rel) and (abs_drop <= max_abs)
    return ok, current, proposed


# ──────────────────────────────────────────────────────────────────────────
# DIVERSIFICATION GUARD
# ──────────────────────────────────────────────────────────────────────────

def diversification_ok(current_legs: list[dict], candidate: dict,
                       *, target_legs: int = 5,
                       single_sport_mode: bool = False) -> tuple[bool, str]:
    """Enforce same-sport and same-game caps. Leg-count-aware per spec:
      • 2-5 legs:  max 2 same sport
      • 6-10 legs: max 50% same sport
      • 11-20 legs: max 40% same sport
    SINGLE-SPORT mode bypasses the same-sport cap entirely."""
    if not current_legs:
        return True, ""
    if not single_sport_mode:
        same_sport = sum(1 for L in current_legs
                        if (L.get("sport") or "") == (candidate.get("sport") or ""))
        max_same = max_same_sport_for_target(target_legs)
        if same_sport + 1 > max_same:
            if target_legs <= 5:
                msg = f"Max {max_same} same sport"
            elif target_legs <= 10:
                msg = "Max 50% same sport"
            else:
                msg = "Max 40% same sport"
            return False, msg
    same_event = sum(1 for L in current_legs
                    if (L.get("event") or "") == (candidate.get("event") or ""))
    if same_event >= MAX_SAME_GAME:
        return False, "Max 2 legs from same game"
    # ─── Same-game-parlay (SGP) HARD BLOCK for correlated sports ───
    # MLB / UFC / Tennis are one-event-per-pick sports — any two legs from the
    # same event are dangerously correlated (Team A ML + Team A Hitter Over =
    # one positive outcome, not two independent). Soccer allows up to 2 because
    # there are legit independent angles (Total Goals + Anytime Scorer can both
    # land on different scoring events). For everything else, hard-block.
    cand_sport = (candidate.get("sport") or "").lower()
    if cand_sport in ("mlb", "ufc", "tennis", "nba", "nfl", "nhl") and same_event >= 1:
        return False, f"Same-game blocked ({cand_sport})"
    # Market-family cap — prevent the "all Win-or-Draw" monoculture.
    # Without this, soccer high-risk parlays would consist of 5 W-or-D
    # picks because they have the highest lock scores. User spec: "high
    # risk soccer only putting win or draw" → fixed by capping each
    # family to MAX_SAME_MARKET_FAMILY legs per parlay.
    cand_family = _market_family(candidate.get("market") or "")
    if cand_family != "other":
        same_family = sum(
            1 for L in current_legs
            if _market_family(L.get("market") or "") == cand_family
        )
        if same_family >= MAX_SAME_MARKET_FAMILY:
            return False, f"Max {MAX_SAME_MARKET_FAMILY} {cand_family.replace('_',' ')} legs"
    # ─── HARD BLOCK: same player twice in same parlay ────────────────
    # User complaint 2026-07-04 "app keep putting mbappe in there twice".
    # Extract player name from the market (works for both AGS and FGS
    # variants: "Kylian Mbappe - Anytime Goal Scorer", "Mbappe First
    # Goal Scorer", "Mbappe To Score or Assist", etc.). If ANY existing
    # leg has the same player name (case-insensitive, accent-stripped),
    # hard-block the candidate — regardless of market type. Two picks
    # on the same player are ~100 % correlated and shouldn't parlay.
    try:
        from quality_gate import _extract_player_from_pick
        import unicodedata as _ud
        def _norm(n: str) -> str:
            if not n:
                return ""
            return "".join(
                c for c in _ud.normalize("NFD", n)
                if _ud.category(c) != "Mn"
            ).lower().strip()
        cand_player = _norm(_extract_player_from_pick(candidate))
        # Only enforce when the candidate is actually a player market
        # (else Mets ML would collide with e.g. "Mets" AGS names).
        cand_is_player_market = _market_family(candidate.get("market") or "") in (
            "goal_scorer", "first_goal", "score_or_assist", "batter_over",
        ) or "goal scorer" in (candidate.get("market") or "").lower() \
             or "to score" in (candidate.get("market") or "").lower()
        if cand_player and cand_is_player_market:
            for L in current_legs:
                lp = _norm(_extract_player_from_pick(L))
                if lp and lp == cand_player:
                    return False, f"Same player already in parlay ({cand_player.title()})"
    except Exception:
        # Non-fatal — if extraction fails we fall through and rely on the
        # soft correlation penalty in score_leg().
        pass

    # No duplicate picks
    if any(L.get("id") == candidate.get("id") for L in current_legs):
        return False, "Duplicate pick"
    return True, ""


# ──────────────────────────────────────────────────────────────────────────
# PARLAY HEALTH GRADE
# ──────────────────────────────────────────────────────────────────────────

def parlay_health(legs: list[dict], bucket_map: dict) -> dict:
    """Return health summary: grade A-F + components.

    Composite from:
      • Survival (35 %)
      • Avg edge (25 %)
      • Avg bucket ROI (20 %)
      • Correlation cleanliness (10 %)
      • Variance / stability (10 %)
    """
    if not legs:
        return {"grade": "F", "score": 0.0, "components": {}}

    n = len(legs)
    survival = parlay_survival(legs)
    avg_edge = sum(float(L.get("edge_percent") or 0) for L in legs) / n
    avg_roi = sum(_leg_bucket_roi(L, bucket_map) for L in legs) / n
    avg_win_p = sum(float(L.get("win_probability") or 0) for L in legs) / n

    # Correlation cleanliness
    from collections import Counter
    sports = Counter((L.get("sport") or "") for L in legs)
    events = Counter((L.get("event") or "") for L in legs)
    sport_concentration = max(sports.values()) / n
    event_dupes = sum(1 for c in events.values() if c > 1)
    correlation_score = 100.0
    if sport_concentration > 0.6:
        correlation_score -= (sport_concentration - 0.6) * 200
    correlation_score -= event_dupes * 15
    correlation_score = max(0.0, correlation_score)

    # Variance / stability: lower variance in win-probs = more stable.
    win_probs = [float(L.get("win_probability") or 0) for L in legs]
    if len(win_probs) > 1:
        mean = sum(win_probs) / len(win_probs)
        var = sum((p - mean) ** 2 for p in win_probs) / len(win_probs)
        std = math.sqrt(var)
        stability_score = max(0.0, 100.0 - std * 4.0)  # std of 25 → 0
    else:
        stability_score = 70.0

    # Survival component: 50 % → 50 points, 25 % → 25 points
    survival_component = min(100.0, survival * 100.0)
    # Edge component
    edge_component = min(100.0, max(0.0, (avg_edge + 2.0) * 8.0))
    # ROI component
    roi_component = min(100.0, max(0.0, (avg_roi * 100.0 + 10.0) * (100.0 / 30.0)))

    composite = (
        0.35 * survival_component
        + 0.25 * edge_component
        + 0.20 * roi_component
        + 0.10 * correlation_score
        + 0.10 * stability_score
    )

    grade = "F"
    for g, thr in GRADE_THRESHOLDS:
        if composite >= thr:
            grade = g
            break

    return {
        "grade": grade,
        "score": round(composite, 1),
        "survival_pct": round(survival * 100.0, 1),
        "avg_edge": round(avg_edge, 2),
        "avg_roi_pct": round(avg_roi * 100.0, 2),
        "avg_win_prob": round(avg_win_p, 1),
        "diversification_pct": round(100.0 - sport_concentration * 100.0, 1),
        "correlation_score": round(correlation_score, 1),
        "stability_score": round(stability_score, 1),
    }


# ──────────────────────────────────────────────────────────────────────────
# "WHY THIS PARLAY" EXPLAINER
# ──────────────────────────────────────────────────────────────────────────

def explain_parlay(legs: list[dict], health: dict, bucket_map: dict) -> list[str]:
    """Human-readable bullets explaining selection."""
    bullets = []
    avg_edge = health.get("avg_edge", 0)
    avg_roi = health.get("avg_roi_pct", 0)
    correlation = health.get("correlation_score", 0)
    survival = health.get("survival_pct", 0)
    stability = health.get("stability_score", 0)

    if avg_edge >= 6:
        bullets.append(f"+ Strong avg edge ({avg_edge:+.1f}%)")
    elif avg_edge >= 3:
        bullets.append(f"+ Positive edge ({avg_edge:+.1f}%)")
    else:
        bullets.append(f"- Modest edge ({avg_edge:+.1f}%)")

    if correlation >= 90:
        bullets.append("+ Low correlation between legs")
    elif correlation >= 75:
        bullets.append("+ Diversified across games")
    else:
        bullets.append("- Some correlated legs (same sport/game)")

    if avg_roi >= 5:
        bullets.append(f"+ Strong historical ROI ({avg_roi:+.1f}%)")
    elif avg_roi >= 0:
        bullets.append(f"+ Positive bucket ROI ({avg_roi:+.1f}%)")
    elif avg_roi >= -3:
        bullets.append(f"- Flat ROI history ({avg_roi:+.1f}%)")
    else:
        bullets.append(f"- Negative ROI history ({avg_roi:+.1f}%)")

    if stability >= 80:
        bullets.append("+ Stable confidence across legs")
    elif stability < 60:
        bullets.append("- Slight volatility (mixed confidence)")

    if survival >= 50:
        bullets.append(f"+ High survival ({survival:.0f}%)")
    elif survival >= 25:
        bullets.append(f"~ Moderate survival ({survival:.0f}%)")
    else:
        bullets.append(f"! Lottery survival ({survival:.0f}%)")

    # Elite-player flag
    n_elite = sum(1 for L in legs if L.get("elite_player"))
    if n_elite >= 2:
        bullets.append(f"+ {n_elite} elite-player anchor legs")

    # Anti-hero warning
    n_anti = sum(1 for L in legs
                if detect_anti_hero(L, bucket_map)["is_anti_hero"])
    if n_anti > 0:
        bullets.append(f"! {n_anti} flagged as anti-hero (review)")

    return bullets


# ──────────────────────────────────────────────────────────────────────────
# GREEDY BUILDER WITH DAMAGE CONTROL
# ──────────────────────────────────────────────────────────────────────────

def build_one_parlay(pool: list[dict], *, target_legs: int, high_risk: bool,
                    bucket_map: dict, seed_pick: dict | None = None,
                    locked_picks: list[dict] | None = None,
                    randomness: float = 0.0,
                    single_sport_mode: bool = False,
                    rng_salt: int = 0,
                    synergy_map: dict | None = None) -> list[dict]:
    """Build a single parlay greedily.

    1. Start with locked_picks (if any) + seed_pick (if provided).
    2. Repeatedly pick the highest-scoring eligible candidate that passes
       damage-control + diversification.
    3. Stop early if no candidate improves quality OR target_legs reached.

    `rng_salt` mixes into the per-leg jitter so consecutive refresh nonces
    produce different leg pickups even with the same seed.
    """
    locked_picks = list(locked_picks or [])
    legs = list(locked_picks)
    if seed_pick is not None and not any(L.get("id") == seed_pick.get("id") for L in legs):
        legs.append(seed_pick)

    used_ids = {L.get("id") for L in legs}
    rng = random.Random(
        hash((target_legs, high_risk,
              seed_pick.get("id") if seed_pick else "x",
              rng_salt))
    )

    while len(legs) < target_legs:
        # Score all remaining eligible candidates
        scored: list[tuple[float, dict]] = []
        for cand in pool:
            if cand.get("id") in used_ids:
                continue
            ok, _ = is_eligible_leg(cand, bucket_map, high_risk=high_risk)
            if not ok:
                continue
            div_ok, _ = diversification_ok(
                legs, cand,
                target_legs=target_legs,
                single_sport_mode=single_sport_mode,
            )
            if not div_ok:
                continue
            dmg_ok, _, _ = damage_control_ok(legs, cand, high_risk=high_risk)
            if not dmg_ok:
                continue
            s = score_leg(
                cand, bucket_map, legs,
                target_legs=target_legs,
                single_sport_mode=single_sport_mode,
                synergy_map=synergy_map,
            )
            # Inject randomness for candidate diversity (small)
            if randomness > 0:
                s += rng.uniform(-randomness, randomness)
            scored.append((s, cand))
        if not scored:
            break
        scored.sort(key=lambda x: x[0], reverse=True)
        # Take the best
        best_score, best_cand = scored[0]

        # ── No filler legs: candidate must improve overall parlay quality.
        if legs:
            cur_health = parlay_health(legs, bucket_map)
            new_health = parlay_health(legs + [best_cand], bucket_map)
            # Allow up to a quality drop for diversity. We're more lenient
            # in high-risk mode (user wants 10-20 legs even if each adds
            # variance) than in standard mode (user wants tight 2-5 legs).
            quality_tolerance = 8.0 if high_risk else 5.0
            min_legs = 5 if high_risk else 2
            if (new_health["score"] < cur_health["score"] - quality_tolerance
                    and len(legs) >= min_legs):
                break
        legs.append(best_cand)
        used_ids.add(best_cand.get("id"))

    return legs


# ──────────────────────────────────────────────────────────────────────────
# TOP-3 SMART BUILD: SAFE / BALANCED / AGGRESSIVE
# ──────────────────────────────────────────────────────────────────────────

def build_top_parlays(pool: list[dict], *, target_legs: int, high_risk: bool,
                     bucket_map: dict, n_candidates: int = N_CANDIDATES,
                     locked_picks: list[dict] | None = None,
                     rank: int = 1,
                     single_sport_mode: bool = False,
                     refresh_nonce: int = 0,
                     avoid_signatures: set[tuple] | None = None,
                     synergy_map: dict | None = None) -> list[dict]:
    """Generate ~N candidate parlays, score them, return Top 3 labelled.

    `rank` lets the frontend "refresh" cycle through next-best candidates
    without randomising — rank=1 returns the canonical top-3, rank=2 returns
    the 4th-6th best parlays, etc.
    `single_sport_mode` disables the same-sport diversification cap.
    `refresh_nonce` perturbs the seed-pick shuffle so consecutive refreshes
    return *different* parlays even when the underlying pool is identical
    (user spec: "the app should try to build a better parlay with every
    refresh"). nonce=0 → canonical run, nonce>0 → shuffled.
    `avoid_signatures` is an optional set of leg-id tuples to skip — used
    by the regenerate flow so the user never sees the same parlay twice
    in a row.
    """
    if not pool:
        return []

    # Pre-filter pool by hard eligibility once
    eligible_pool = [p for p in pool
                    if is_eligible_leg(p, bucket_map, high_risk=high_risk)[0]]
    if len(eligible_pool) < 2:
        return []
    # Sort pool by composite leg-score for stable seed-picking
    eligible_pool.sort(
        key=lambda p: score_leg(p, bucket_map, [],
                                target_legs=target_legs,
                                single_sport_mode=single_sport_mode,
                                synergy_map=synergy_map),
        reverse=True,
    )

    candidates: list[dict] = []
    # Seed from the top-N picks AND a few mid-pool picks to get variety.
    # When refresh_nonce > 0 we widen + shuffle so the next refresh sees
    # different seeds (and therefore different parlays).
    if refresh_nonce > 0:
        # Bigger pool of seeds and a per-request shuffle.
        seed_count = min(len(eligible_pool), max(40, n_candidates))
        seed_indices = list(range(seed_count))
        random.Random(refresh_nonce + 1).shuffle(seed_indices)
    else:
        seed_indices = list(range(min(len(eligible_pool), max(20, n_candidates // 2))))
        extra_seeds = list(range(len(eligible_pool)))
        random.Random(7).shuffle(extra_seeds)
        seed_indices.extend(extra_seeds[:max(0, n_candidates - len(seed_indices))])
    seen_signatures: set = set(avoid_signatures or set())

    for i, idx in enumerate(seed_indices[:n_candidates]):
        if idx >= len(eligible_pool):
            continue
        seed = eligible_pool[idx]
        # Slight randomness for non-top seeds for variety. With nonce>0,
        # bump the randomness floor so the build path diverges noticeably
        # between refreshes.
        if refresh_nonce > 0:
            randomness = 2.0 + (i % 7)         # 2-8 range, varies per seed
        else:
            randomness = 0.0 if i < 10 else 4.0
        legs = build_one_parlay(eligible_pool, target_legs=target_legs,
                                high_risk=high_risk, bucket_map=bucket_map,
                                seed_pick=seed, locked_picks=locked_picks,
                                randomness=randomness,
                                single_sport_mode=single_sport_mode,
                                rng_salt=refresh_nonce)
        min_legs = 5 if high_risk else 2
        if len(legs) < min_legs:
            continue
        sig = tuple(sorted(L.get("id") for L in legs if L.get("id")))
        if sig in seen_signatures:
            continue
        seen_signatures.add(sig)
        health = parlay_health(legs, bucket_map)
        candidates.append({
            "legs": legs,
            "health": health,
            "score": health["score"],
            "survival": health["survival_pct"],
        })
    if not candidates:
        return []
    # Sort by composite health score (highest first)
    candidates.sort(key=lambda c: c["score"], reverse=True)

    # Pick three by SURVIVAL BAND (SAFE / BALANCED / AGGRESSIVE).  We slide
    # the cursor by `rank` so refresh cycles to next-best in each band.
    chosen: list[dict] = []
    used_ids: set = set()
    skip = rank - 1
    for label, lo, hi in RISK_LABELS:
        # Filter candidates whose survival falls in this band
        band = [c for c in candidates
               if lo <= c["survival"] / 100.0 < hi
               and id(c) not in used_ids]
        # Fallback for empty bands: nearest candidate to band centre
        if not band:
            target = (lo + hi) / 2.0
            band = sorted(candidates, key=lambda c: abs(c["survival"]/100.0 - target))
            band = [c for c in band if id(c) not in used_ids]
        if not band:
            continue
        pick_idx = min(skip, len(band) - 1)
        chosen_card = band[pick_idx]
        used_ids.add(id(chosen_card))
        chosen.append({
            **chosen_card,
            "label": label,
        })
    return chosen


# ──────────────────────────────────────────────────────────────────────────
# PUBLIC: build the API response payload
# ──────────────────────────────────────────────────────────────────────────

def american_to_decimal(american: int) -> float:
    return 1 + (american / 100 if american > 0 else 100 / abs(american))


def parlay_to_payload(parlay: dict, bucket_map: dict) -> dict:
    """Convert one built parlay to API response shape."""
    legs = parlay["legs"]
    health = parlay["health"]
    decimal_total = 1.0
    for L in legs:
        try:
            decimal_total *= american_to_decimal(int(L.get("book_odds") or 100))
        except (ValueError, TypeError):
            continue
    if decimal_total >= 2.0:
        combined_american = int(round((decimal_total - 1) * 100))
        combined_str = f"+{combined_american}"
    else:
        combined_american = int(round(-100 / max(decimal_total - 1, 0.001)))
        combined_str = str(combined_american)

    reasons = explain_parlay(legs, health, bucket_map)
    return {
        "label": parlay.get("label", "BALANCED"),
        "grade": health["grade"],
        "strength_score": health["score"],
        "leg_count": len(legs),
        "legs": legs,
        "survival_pct": health["survival_pct"],
        "avg_edge_pct": health["avg_edge"],
        "avg_roi_pct": health["avg_roi_pct"],
        "avg_win_prob": health["avg_win_prob"],
        "diversification_pct": health["diversification_pct"],
        "correlation_score": health["correlation_score"],
        "stability_score": health["stability_score"],
        "combined_decimal_odds": round(decimal_total, 3),
        "combined_american_odds": combined_str,
        "payout_on_100": round(100 * decimal_total, 2),
        "profit_on_100": round(100 * (decimal_total - 1), 2),
        "reasons": reasons,
    }
