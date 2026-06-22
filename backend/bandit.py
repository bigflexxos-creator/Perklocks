"""Multi-Armed Bandit (Thompson sampling) for pick-selection strategies.

Phase 3 of the learning system. Where the earlier layers learn AT THE
DATA LEVEL (per-market, per-player), this layer learns AT THE STRATEGY
LEVEL — which combinations of filters (lock-floor, edge-floor, market,
sport, odds band) are currently most profitable.

Why Thompson sampling?
  Classic ε-greedy explores randomly; UCB explores deterministically.
  Thompson samples from each arm's posterior Beta(α, β) distribution
  and plays the highest. It's optimal in regret for Bernoulli rewards
  (win/loss) and inherently balances exploration vs exploitation.

  → Each arm's posterior mean represents the model's current estimate
    of that strategy's hit rate. Sampling adds noise proportional to
    uncertainty (high-variance arms get more exploration).

Storage: `bandit_arms` collection — one row per arm, persisted with
posterior parameters and recent performance metrics.

Schema:
  {
    arm: "chalk_locks",
    description: "Lock ≥92, Edge ≥0, Odds ≥-300 — high-prob favorites",
    alpha: 7,                  # Beta(α, β) posterior; α = wins + 1
    beta: 4,                   # β = losses + 1
    n: 9,                      # decisive picks since last reset
    wins: 6,
    losses: 3,
    push: 0,
    units_risked: 9.0,
    units_profit: 1.45,
    roi: 16.1,
    posterior_mean: 0.667,     # α / (α + β)
    posterior_thompson: 0.71,  # last sampled value (refreshed each refresh)
    last_updated: ISO,
  }

Interface:
  await refresh_arm_states(db)         # rebuild from settled picks
  sampled = await sample_arms(db)      # Thompson sample → {arm: x}
  apply_bandit_lift(picks, sampled)    # tilt lock_scores by ±LIFT_MAX
  await update_arm_on_settle(db, pick) # called per settled pick
"""
from __future__ import annotations

import logging
import random
from datetime import datetime, timezone
from typing import Any, Callable, Optional

logger = logging.getLogger("lockscore.bandit")

# Max ±lift the bandit can apply to lock_score so it never dominates the
# engine — it's a *tilt*, like player_form.
LIFT_MAX = 4.0
LIFT_THRESHOLD_TOP = 0.65    # arms with posterior_thompson ≥ this get a positive lift
LIFT_THRESHOLD_BOT = 0.40    # arms below this get a negative tilt

# Prior strength — Beta(1, 1) is uniform (no prior belief).
PRIOR_ALPHA = 1.0
PRIOR_BETA = 1.0

# ─────────────────────────────────────────────────────────────────────
# Arm definitions — each predicate(pick) → True if pick "belongs" to arm.
# A pick can belong to multiple arms simultaneously; all get updated.
# ─────────────────────────────────────────────────────────────────────


def _odds(p: dict) -> float:
    try: return float(p.get("book_odds") or 0)
    except Exception: return 0.0


def _lock(p: dict) -> float:
    try: return float(p.get("lock_score") or 0)
    except Exception: return 0.0


def _edge(p: dict) -> float:
    try: return float(p.get("edge_percent") or 0)
    except Exception: return 0.0


def _sport(p: dict) -> str:
    return (p.get("sport") or "").lower()


def _market_lower(p: dict) -> str:
    return (p.get("market") or "").lower()


ARMS: dict[str, dict] = {
    # ── Quality / value bands (cross-sport) ──────────────────────────
    "chalk_locks": {
        "description": "Lock ≥92, Edge ≥0, Odds ≥-300 — high-prob favorites",
        "predicate": lambda p: _lock(p) >= 92 and _edge(p) >= 0 and _odds(p) >= -300,
    },
    "value_balanced": {
        "description": "Lock ≥85, Edge ≥3, Odds [-180,+120]",
        "predicate": lambda p: _lock(p) >= 85 and _edge(p) >= 3 and -180 <= _odds(p) <= 120,
    },
    "value_aggressive": {
        "description": "Lock ≥80, Edge ≥5, Odds [-150,+200]",
        "predicate": lambda p: _lock(p) >= 80 and _edge(p) >= 5 and -150 <= _odds(p) <= 200,
    },
    "long_shots": {
        "description": "Lock ≥75, Edge ≥10, Odds ≥+150",
        "predicate": lambda p: _lock(p) >= 75 and _edge(p) >= 10 and _odds(p) >= 150,
    },
    # ── Sport+market segments ────────────────────────────────────────
    "mlb_pitcher_props": {
        "description": "MLB pitcher props (Strikeouts / Outs)",
        "predicate": lambda p: _sport(p) == "mlb" and any(
            k in _market_lower(p) for k in ("strikeouts", "outs recorded")),
    },
    "mlb_hitter_props": {
        "description": "MLB hitter props (Hits / HRR / Home Runs)",
        "predicate": lambda p: _sport(p) == "mlb" and any(
            k in _market_lower(p) for k in ("hits", "home runs", "rbis", "total bases")),
    },
    "nba_player_props": {
        "description": "NBA player props (Points / Rebs / Asts)",
        "predicate": lambda p: _sport(p) == "nba" and any(
            k in _market_lower(p) for k in ("points", "rebounds", "assists", "threes")),
    },
    "soccer_specials": {
        "description": "Soccer specials (Anytime/First Scorer, Double Chance, BTTS)",
        "predicate": lambda p: _sport(p) == "soccer" and any(
            k in _market_lower(p) for k in
            ("anytime goal scorer", "first goal scorer", "double chance", "btts")),
    },
    "tennis_moneyline": {
        "description": "Tennis Moneyline picks",
        "predicate": lambda p: _sport(p) == "tennis" and "moneyline" in _market_lower(p),
    },
    "game_lines": {
        "description": "Game-level lines: Moneyline, Spread, Total (any sport)",
        "predicate": lambda p: any(
            k in _market_lower(p) for k in
            ("moneyline", "spread", "run line", "puck line", "total", "over/under")),
    },
}


def matching_arms(pick: dict) -> list[str]:
    """Return all arm names that match this pick."""
    return [name for name, spec in ARMS.items() if spec["predicate"](pick)]


# ─────────────────────────────────────────────────────────────────────
# Arm state aggregation (from settled picks)
# ─────────────────────────────────────────────────────────────────────


async def refresh_arm_states(db) -> dict[str, dict]:
    """Recompute Beta(α, β) posteriors for every arm from settled picks.

    Cheap aggregation — runs after each settlement pass. Returns a map of
    arm → state dict.
    """
    cursor = db.picks.find(
        {"status": {"$in": ["won", "lost", "push"]}},
        {"_id": 0, "sport": 1, "market": 1, "status": 1,
         "lock_score": 1, "edge_percent": 1, "book_odds": 1,
         "units_profit": 1, "units_risked": 1, "settled_at": 1},
    )
    picks = await cursor.to_list(length=20_000)

    # Init counters per arm
    states: dict[str, dict] = {
        name: {
            "arm": name,
            "description": spec["description"],
            "n": 0, "wins": 0, "losses": 0, "push": 0,
            "units_risked": 0.0, "units_profit": 0.0,
        }
        for name, spec in ARMS.items()
    }

    for p in picks:
        for name in matching_arms(p):
            s = states[name]
            s["n"] += 1
            status = p["status"]
            if status == "won":
                s["wins"] += 1
            elif status == "lost":
                s["losses"] += 1
            else:
                s["push"] += 1
            if status != "push":
                s["units_risked"] += float(p.get("units_risked") or 0)
            s["units_profit"] += float(p.get("units_profit") or 0)

    # Compute posterior params + ROI + Thompson sample
    now_iso = datetime.now(timezone.utc).isoformat()
    for s in states.values():
        s["alpha"] = PRIOR_ALPHA + s["wins"]
        s["beta"] = PRIOR_BETA + s["losses"]
        decisive = s["wins"] + s["losses"]
        s["roi"] = round((s["units_profit"] * 100 / s["units_risked"]), 2) if s["units_risked"] else 0.0
        s["posterior_mean"] = round(s["alpha"] / (s["alpha"] + s["beta"]), 4)
        s["posterior_thompson"] = round(random.betavariate(s["alpha"], s["beta"]), 4)
        s["last_updated"] = now_iso

    # Persist atomically: delete + bulk insert
    await db.bandit_arms.delete_many({})
    if states:
        await db.bandit_arms.insert_many(list(states.values()))
    return states


# ─────────────────────────────────────────────────────────────────────
# Thompson sampling at decision time
# ─────────────────────────────────────────────────────────────────────


async def sample_arms(db) -> dict[str, float]:
    """Sample each arm's posterior Beta — returns {arm: sampled_prob}.

    Called once per refresh; the sampled values become the "current
    favored strategies" for that batch of picks. High variance arms
    naturally get more exploration without us tuning epsilon.
    """
    arms = await db.bandit_arms.find({}, {"_id": 0}).to_list(length=100)
    out: dict[str, float] = {}
    for a in arms:
        alpha = a.get("alpha", PRIOR_ALPHA)
        beta_ = a.get("beta", PRIOR_BETA)
        try:
            out[a["arm"]] = random.betavariate(alpha, beta_)
        except Exception:
            out[a["arm"]] = 0.5
    return out


def apply_bandit_lift(picks: list[dict], sampled: dict[str, float]) -> dict:
    """Tilt each pick's lock_score by the average Thompson sample of the
    arms it belongs to. Capped at ±LIFT_MAX so the engine still owns the
    base score.

    Stores `bandit_lift` + `bandit_arms_matched` on each pick for the UI.
    Returns {applied, lifted_up, lifted_down}.
    """
    if not picks or not sampled:
        return {"applied": 0, "lifted_up": 0, "lifted_down": 0}
    counts = {"applied": 0, "lifted_up": 0, "lifted_down": 0}
    for p in picks:
        matched = matching_arms(p)
        if not matched:
            continue
        # Average sampled probability across all matched arms.
        avg = sum(sampled.get(a, 0.5) for a in matched) / len(matched)
        # Map avg → lift in [-LIFT_MAX, +LIFT_MAX]. Linear above/below thresholds.
        if avg >= LIFT_THRESHOLD_TOP:
            # 0.65 → 0, 1.0 → +LIFT_MAX
            lift = (avg - LIFT_THRESHOLD_TOP) / (1.0 - LIFT_THRESHOLD_TOP) * LIFT_MAX
        elif avg <= LIFT_THRESHOLD_BOT:
            # 0.40 → 0, 0.0 → -LIFT_MAX
            lift = -((LIFT_THRESHOLD_BOT - avg) / LIFT_THRESHOLD_BOT) * LIFT_MAX
        else:
            lift = 0.0
        if abs(lift) < 0.05:
            continue
        cur = float(p.get("lock_score") or 0)
        new_lock = max(0.0, min(99.0, cur + lift))
        p["lock_score"] = round(new_lock, 1)
        p["bandit_lift"] = round(lift, 2)
        p["bandit_arms_matched"] = matched
        p["bandit_avg_sample"] = round(avg, 3)
        counts["applied"] += 1
        if lift > 0: counts["lifted_up"] += 1
        else: counts["lifted_down"] += 1
    return counts


# ─────────────────────────────────────────────────────────────────────
# Live update path (per settled pick — optional incremental hook)
# ─────────────────────────────────────────────────────────────────────


async def update_arm_on_settle(db, pick: dict) -> None:
    """Increment matching arms on a freshly-settled pick.

    Optional fast-path: refresh_arm_states() rebuilds from scratch each
    settlement cycle, so this is for tighter incremental updates if we
    ever need them. No-op safety guard if status isn't decisive.
    """
    status = pick.get("status")
    if status not in ("won", "lost"):
        return
    for name in matching_arms(pick):
        await db.bandit_arms.update_one(
            {"arm": name},
            {"$inc": {
                "n": 1,
                "wins": 1 if status == "won" else 0,
                "losses": 1 if status == "lost" else 0,
                "alpha": 1 if status == "won" else 0,
                "beta": 1 if status == "lost" else 0,
            }},
            upsert=True,
        )
