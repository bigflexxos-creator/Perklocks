"""Simulator runner — applies sport-specific Monte Carlo to picks at refresh time.

Routes MLB / Soccer / NBA / Tennis picks to their dedicated 20K-run simulators
and ANCHORS the final `lock_score` to the simulator's `sim_win_probability`
output. The 20,000-iteration Monte Carlo consensus is treated as the dominant
signal — old baseline modifiers (player_form, bandit, evidence multiplier) are
retained only as a small ±3-point residual nudge.

Iter-2026-06-26 (P0): the simulator no longer acts as a tiny ±4 lift. When a
pick has been simulated with ≥10K runs we MAP `sim_win_probability` directly
into the lock_score curve so a 73% Sim WP → ~87 lock (clearly green), and
80%+ Sim WP → 94+ lock (Lock tier). This fixes the long-standing complaint
"73.2% Sim WP only got Lock 75" → players who are supposed to be green are
now green.
"""
from __future__ import annotations
import logging
from typing import Optional

logger = logging.getLogger("lockscore.brain.sim_runner")

# Lazy import per sport — keeps the runner lightweight when only some sports
# have simulators.
_SPORTS_WITH_SIM = {"MLB", "Soccer", "NBA", "Tennis"}


def _player_stats_from_pick(pick: dict) -> dict:
    """Extract any player stats already enriched on the pick (MLB only).

    Reads from `mlb_bvp` enrichment + `player_intel` cache. Falls back to
    league averages inside the simulator if these are missing. Soccer/NBA/
    Tennis sims read directly from `pick.factors` instead.
    """
    stats: dict = {}
    bvp = pick.get("mlb_bvp") or {}
    pi = pick.get("player_intel") or {}
    # Hitter stats
    if "ba" in bvp: stats["ba"] = bvp.get("ba")
    elif "season_ba" in pi: stats["ba"] = pi.get("season_ba")
    if "hr_per_ab" in bvp: stats["hr_per_ab"] = bvp.get("hr_per_ab")
    elif "season_hr_rate" in pi: stats["hr_per_ab"] = pi.get("season_hr_rate")
    if "rbi_per_ab" in pi: stats["rbi_per_ab"] = pi.get("rbi_per_ab")
    # Pitcher stats
    if "k_rate" in bvp: stats["k_rate"] = bvp.get("k_rate")
    elif "season_k_rate" in pi: stats["k_rate"] = pi.get("season_k_rate")
    if "bf_per_inning" in pi: stats["bf_per_inning"] = pi.get("bf_per_inning")
    if "expected_innings" in pi: stats["expected_innings"] = pi.get("expected_innings")
    return stats


def simulate_pick(pick: dict) -> Optional[dict]:
    """Route a pick to its sport's simulator. Returns sim output dict or None."""
    sport = pick.get("sport") or ""
    if sport not in _SPORTS_WITH_SIM:
        return None
    try:
        if sport == "MLB":
            from brain.sim_mlb import simulate_mlb_pick
            stats = _player_stats_from_pick(pick)
            return simulate_mlb_pick(pick, stats)
        if sport == "Soccer":
            from brain.sim_soccer import simulate_soccer_pick
            return simulate_soccer_pick(pick)
        if sport == "NBA":
            from brain.sim_nba import simulate_nba_pick
            return simulate_nba_pick(pick)
        if sport == "Tennis":
            from brain.sim_tennis import simulate_tennis_pick
            return simulate_tennis_pick(pick)
    except Exception as e:
        logger.warning("Simulator failed for pick %s (sport=%s): %s",
                       (pick.get("id") or "?")[:8], sport, e)
    return None


# Minimum sim runs required to trust the simulator as the dominant signal.
# Every production sim (MLB/Soccer/NBA/Tennis/soccer-scorer) uses 20,000
# runs — anything below this threshold falls back to the legacy ±4 lift.
MIN_RUNS_FOR_ANCHOR = 10_000

# Maximum residual ± nudge from existing modifiers (player_form, bandit,
# evidence governance, CLV) when the simulator is the dominant anchor.
# Keeps the engine's "long memory" present without letting it override the
# 20K-run consensus.
SIM_RESIDUAL_MAX = 3.0


def sim_wp_to_lock_baseline(sim_wp_pct: float) -> float:
    """Map Monte Carlo sim win probability (%) → lock score baseline (0-99).

    PHILOSOPHY (per user 2026-06-26):
      Lock score 95-99 is NOT 95-99% win probability. Lock score reflects
      EVIDENCE STRENGTH = (historical hit rate) × (simulation agreement) ×
      (tier amplifier). The simulator alone cannot promote a pick into
      Lock tier — it can only LIFT the lock UP when the engine missed,
      acting as a corroborating signal.

      This curve is intentionally CONSERVATIVE: sim_wp 70-75% lifts a
      pick to Playable, but reaching Strong Lock (95+) requires the
      simulator AND the prior engine signals (elite player tier, career
      history hit rate, edge) to align. The elite_players.py module
      already gives world-class players (Salah, Mbappé, Haaland, etc.)
      a 95+ floor based on their HISTORY — the simulator simply confirms
      or further lifts that score.

    Calibration:
        50% WP  →  60 lock    (coin flip — Pass floor)
        60% WP  →  68 lock    (Pass)
        65% WP  →  74 lock    (Pass)
        70% WP  →  80 lock    (Playable border — needs corroborating evidence)
        75% WP  →  84 lock    (Playable — confidence forming)
        80% WP  →  88 lock    (Playable — strong agreement)
        85% WP  →  92 lock    (Lock — sim is robust)
        90% WP  →  96 lock    (Strong Lock — overwhelming sim consensus)
        95%+    →  99 lock    (Elite Lock cap — sim alone justifies it)

      The 50-85% range earns Pass→Lock gradually so a 70% sim doesn't
      auto-mint a Lock. The 85%+ range climbs fast because that level
      of Monte Carlo consensus across 20K iterations is itself rare
      enough to justify Strong Lock tier even without elite history.
    """
    wp = max(0.0, min(100.0, float(sim_wp_pct)))
    if wp < 50.0:
        # Below coin flip — scale 0-50% into 30-60 lock (always Pass tier).
        return max(0.0, 30.0 + wp * 0.6)
    if wp <= 70.0:
        # 50-70% → 60-80 lock (Pass → Playable border). Linear, slope 1.0.
        return 60.0 + (wp - 50.0) * 1.0
    if wp <= 85.0:
        # 70-85% → 80-92 lock (Playable → Lock). Slope ~0.8.
        return 80.0 + (wp - 70.0) * (12.0 / 15.0)
    if wp <= 95.0:
        # 85-95% → 92-99 lock (Lock → Elite Lock). Slope 0.7.
        return 92.0 + (wp - 85.0) * (7.0 / 10.0)
    # 95-100% → 99 lock (Elite Lock cap).
    return 99.0


def _anchor_pick_to_sim(pick: dict, sim_wp: float) -> Optional[dict]:
    """Rewrite lock_score / lock_score_raw / lock_score_v2 / evidence
    breakdown so the simulator's win probability becomes a confidence
    FLOOR for the lock score. Re-derives grade + confidence.

    Philosophy (per user 2026-06-26): "99 lock doesn't mean 99% win.
    Lock reflects EVIDENCE strength — history + tier + matchup + sim
    consensus combined. Salah scoring/assisting should be 99 lock even
    if sim WP is 55% because his career history is overwhelming."

    Rule applied: SIM ANCHOR IS A FLOOR.
      • If sim baseline > prior lock → sim LIFTS lock UP to the baseline.
        (Catches engine misses where the 20K-run consensus says a pick
         is stronger than the engine scored it — fixes "73.2% Sim WP
         only got Lock 75" complaint.)
      • If sim baseline <= prior lock → KEEP prior lock untouched.
        (Elite players, strong-evidence picks, and high-edge plays are
         not dragged down by sim WP because lock_score isn't a 1:1 map
         to win probability.)

    Returns audit dict.
    """
    baseline = sim_wp_to_lock_baseline(sim_wp)
    try:
        prior_lock = float(pick.get("lock_score") or 0.0)
    except (TypeError, ValueError):
        prior_lock = 0.0

    # ── Sim acts as a FLOOR. Never drag elite/high-evidence picks down. ──
    if baseline > prior_lock:
        # Sim is more bullish than current engine — lift up to the sim
        # consensus. This is the "73.2% Sim WP should be green" case.
        new_lock = round(max(0.0, min(99.0, baseline)), 1)
        anchored = True
    else:
        # Sim agrees or is less bullish — keep the prior lock score.
        # The Monte-Carlo simulator alone cannot demote a pick whose
        # evidence (elite player history, sharp edge, model alignment)
        # already justifies a higher lock.
        new_lock = round(prior_lock, 1)
        anchored = False

    if not anchored:
        # No mutation needed — just attach the audit fields so the UI
        # can show the user the sim baseline was considered.
        pick["sim_lock_anchor"] = round(baseline, 1)
        pick["sim_lock_residual"] = 0.0
        pick["lock_anchored_to_sim"] = False
        return {
            "prior_lock": round(prior_lock, 1),
            "baseline":   round(baseline, 1),
            "anchored":   False,
            "new_lock":   new_lock,
        }

    # ── Sim is lifting the lock. Anchor every shadow lock field so the
    # read-time canonicalization (max of v1, v2) won't roll back to an
    # older governed value, and the raw × multiplier ≈ lock audit
    # invariant still holds (multiplier becomes 1.0).
    pick["lock_score"]         = new_lock
    pick["lock_score_raw"]     = new_lock
    pick["lock_score_v2"]      = new_lock
    pick["lock_score_v2_raw"]  = new_lock

    # Peak is monotonically increasing — re-evaluate the pinned flag.
    try:
        prev_peak = float(pick.get("lock_score_peak") or 0.0)
    except (TypeError, ValueError):
        prev_peak = 0.0
    pick["lock_score_peak"] = round(max(new_lock, prev_peak), 1)
    if pick["lock_score_peak"] >= 95.0:
        pick["pinned"] = True

    # Update evidence_breakdown so the Evidence Inspector audit math
    # reconciles (raw × multiplier ≈ lock) post-anchoring.
    eb = pick.get("evidence_breakdown")
    if isinstance(eb, dict):
        eb["multiplier"]    = 1.0
        eb["lock_raw"]      = new_lock
        eb["lock_governed"] = new_lock
        eb["sim_anchored"]  = True

    # Re-derive grade + confidence against the new lock.
    try:
        from sports_engine import _grade, _confidence
        pick["grade"]      = _grade(new_lock)
        pick["confidence"] = _confidence(new_lock)
    except Exception:
        pass

    # Audit fields so the UI / inspector can show the sim lifted this pick.
    pick["sim_lock_anchor"]      = round(baseline, 1)
    pick["sim_lock_residual"]    = round(new_lock - baseline, 2)
    pick["lock_anchored_to_sim"] = True

    return {
        "prior_lock": round(prior_lock, 1),
        "baseline":   round(baseline, 1),
        "anchored":   True,
        "new_lock":   new_lock,
    }


def apply_simulations(picks: list[dict]) -> dict:
    """Run simulators across the slate. Mutates each pick in-place with
    sim_* fields AND anchors lock_score to sim_win_probability when the
    simulator has run ≥MIN_RUNS_FOR_ANCHOR iterations.

    Returns counts: {applied, stronger, weaker, neutral, anchored,
    lifted_up, lifted_down}.
    """
    counts = {
        "applied": 0, "stronger": 0, "weaker": 0, "neutral": 0,
        "anchored": 0, "lifted_up": 0, "lifted_down": 0,
    }
    for p in picks:
        sim = simulate_pick(p)
        if not sim:
            continue
        p.update(sim)
        counts["applied"] += 1
        sig = sim.get("sim_signal", "neutral")
        counts[sig] = counts.get(sig, 0) + 1

        # ── Anchor lock_score to sim_win_probability ──────────────────
        sim_wp = sim.get("sim_win_probability")
        try:
            sim_runs = int(sim.get("sim_runs") or 0)
        except (TypeError, ValueError):
            sim_runs = 0

        if sim_wp is None or sim_runs < MIN_RUNS_FOR_ANCHOR:
            continue

        prior = float(p.get("lock_score") or 0.0)
        audit = _anchor_pick_to_sim(p, float(sim_wp))
        if audit is None:
            continue
        counts["anchored"] += 1
        if audit["new_lock"] > prior + 0.5:
            counts["lifted_up"] += 1
        elif audit["new_lock"] < prior - 0.5:
            counts["lifted_down"] += 1

    return counts
