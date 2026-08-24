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
_SPORTS_WITH_SIM = {"MLB", "Soccer", "NBA", "Tennis", "NFL", "NHL"}


def _player_stats_from_pick(pick: dict) -> dict:
    """Extract any player stats already enriched on the pick (MLB only).

    Reads from `mlb_bvp` enrichment + `player_intel` cache. Falls back to
    league averages inside the simulator if these are missing. Soccer/NBA/
    Tennis sims read directly from `pick.factors` instead.

    **Phase 4C finalization (2026-08-06):** now also plumbs the H+R+RBI
    lineup / team-run / OBP context populated by the emission path.
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
    # ── Phase 4C H+R+RBI context ─────────────────────────────────────
    ls = (bvp.get("lineup_slot") or pi.get("lineup_slot")
          or pick.get("lineup_slot"))
    if ls is not None:
        stats["lineup_slot"] = ls
    trp = (pi.get("team_runs_projection") or pick.get("team_runs_projection"))
    if trp is not None:
        stats["team_runs_projection"] = trp
    obp = (bvp.get("obp") or pi.get("season_obp") or pi.get("obp"))
    if obp is not None:
        stats["obp"] = obp
    return stats


def simulate_pick(pick: dict) -> Optional[dict]:
    """Route a pick to its sport's simulator. Returns sim output dict or None.

    Phase 4B additions:
      • Deterministic per-pick seed injected into ``random.seed`` before
        each simulator runs.  Sequential execution inside
        :func:`apply_simulations` guarantees no cross-pick contamination.
      • Stamps the returned dict with the truthful ``simulator_type``,
        ``simulator_name``, ``simulator_version``, ``seed``,
        ``independent_evidence=True``, ``valid=True`` so the symmetric
        anchor can trust it.
    """
    import random as _random
    sport = pick.get("sport") or ""
    if sport not in _SPORTS_WITH_SIM:
        return None
    try:
        from services.simulation_seed import build_seed, SeedError
        sim_name = f"{sport.lower()}_simulator"
        sim_version = _SIM_VERSIONS.get(sport, "1.0.0")
        try:
            seed = build_seed(pick, sim_name, sim_version,
                                allow_name_only_fallback=True)
        except SeedError:
            seed = 0
        # Seed the GLOBAL random for the duration of THIS pick's sim.
        # apply_simulations() runs picks sequentially so there is no
        # cross-pick contamination.  See simulator_seed_thread_safety
        # in the Phase 4B docs.
        _random.seed(seed)

        if sport == "MLB":
            from brain.sim_mlb import simulate_mlb_pick
            stats = _player_stats_from_pick(pick)
            out = simulate_mlb_pick(pick, stats)
            sim_type = "distribution_monte_carlo"
        elif sport == "Soccer":
            from brain.sim_soccer import simulate_soccer_pick
            # Post-Cert Defect 2 — pass authoritative team-form context
            # when it's already stamped on the pick.  Reads existing
            # enriched fields only; no new provider calls, no new data
            # source.  Absent context → simulator falls back to
            # MODEL_CONDITIONED (documented behaviour).
            soccer_ctx = (pick.get("soccer_ctx")
                          or pick.get("game_context")
                          or pick.get("team_form_ctx"))
            out = simulate_soccer_pick(pick, soccer_ctx=soccer_ctx)
            sim_type = "distribution_monte_carlo"
        elif sport == "NBA":
            from brain.sim_nba import simulate_nba_pick
            # Post-Cert Defect 2 — pass L10 gamelog rows when stamped
            # on the pick.  Reads existing enriched fields only.
            recent_rows = (pick.get("player_recent_rows")
                           or pick.get("recent_rows")
                           or pick.get("gamelog"))
            out = simulate_nba_pick(pick, recent_rows=recent_rows)
            sim_type = "distribution_monte_carlo"
        elif sport == "Tennis":
            from brain.sim_tennis import simulate_tennis_pick
            # Post-Cert Defect 2 — pass surface / Elo / hold-break
            # context when stamped on the pick.
            tennis_ctx = (pick.get("tennis_ctx")
                          or pick.get("tennis_context")
                          or pick.get("surface_context"))
            out = simulate_tennis_pick(pick, tennis_ctx=tennis_ctx)
            sim_type = "event_simulation"
        elif sport == "NFL" or sport == "CFB":
            # Pass 2 (2026-06) — NFL now has a real distribution
            # Monte Carlo simulator at ``sim_nfl.simulate``.  It
            # honours the standard fail-closed contract (returns
            # ran=False for game markets [handled by Platinum NFL],
            # ATD markets [handled by nfl_atd_engine], and any
            # candidate with insufficient real history).  CFB is out
            # of scope for Pass 2 and continues to receive the
            # ran=False stub.
            try:
                from sim_nfl import simulate as _nfl_sim  # type: ignore
                _stub = _nfl_sim(pick)
                if _stub and _stub.get("ran"):
                    out = _stub
                    sim_type = _stub.get("simulator_type",
                                          "distribution_monte_carlo")
                else:
                    return None
            except Exception:
                return None
        elif sport == "NHL":
            # Pass 2 (2026-06) — NHL simulator lives at
            # ``brain.sim_nhl.simulate``.  Handles game markets
            # (moneyline / puck line / total) via a Poisson team-
            # score model and player markets (goals / assists /
            # points / SOG / saves) via per-game stat distributions.
            # DATA_INSUFFICIENT / UNSUPPORTED_MARKET → ran=False so
            # this pick is skipped without a fabricated probability.
            try:
                from brain.sim_nhl import simulate as _nhl_sim
                _stub = _nhl_sim(pick)
                if _stub and _stub.get("ran"):
                    out = _stub
                    sim_type = _stub.get("simulator_type",
                                          "distribution_monte_carlo")
                else:
                    return None
            except Exception:
                return None
        else:
            return None

        if out is None:
            return None
        # Stamp truthful metadata so the symmetric anchor and the
        # guardrail tests can trust the result.
        out.setdefault("simulator_name",       sim_name)
        out.setdefault("simulator_version",    sim_version)
        out.setdefault("simulator_type",       sim_type)
        out.setdefault("seed",                 seed)
        out.setdefault("independent_evidence", True)
        out.setdefault("valid",                True)
        # Post-Cert Defect 1 — PROVENANCE OVERRIDE.
        # The Phase-2 provenance contract is AUTHORITATIVE.  If the
        # simulator reports MODEL_CONDITIONED / PRIOR_ONLY / INVALID —
        # or explicitly reports decision_valid=False — the legacy
        # ``independent_evidence`` / ``valid`` compatibility defaults
        # (set above via setdefault so we don't disturb simulators that
        # already returned the correct value) MUST be overridden here.
        # Only CAUSAL_INDEPENDENT / EMPIRICAL_INDEPENDENT with a FULL /
        # STRONG confidence and decision_valid=True may count as
        # independent confirmation downstream.
        _prov = out.get("simulator_provenance") or out.get("provenance")
        _conf = (out.get("input_quality")
                 or out.get("confidence_bucket")
                 or out.get("provenance_confidence") or "").upper()
        _decision_valid = out.get("decision_valid", True)
        if _prov in ("MODEL_CONDITIONED", "PRIOR_ONLY", "INVALID"):
            out["independent_evidence"] = False
        if _prov == "INVALID":
            out["valid"] = False
        if _decision_valid is False:
            out["independent_evidence"] = False
            out["valid"] = False
        if _prov in ("CAUSAL_INDEPENDENT", "EMPIRICAL_INDEPENDENT"):
            # Independent provenance requires FULL/STRONG confidence.
            if _conf and _conf not in ("FULL", "STRONG", "HIGH"):
                out["independent_evidence"] = False
        return out
    except Exception as e:
        logger.warning("Simulator failed for pick %s (sport=%s): %s",
                       (pick.get("id") or "?")[:8], sport, e)
    return None


# Simulator versions (bump when logic changes so seed cache invalidates).
_SIM_VERSIONS = {
    "MLB":    "1.1.0",   # Phase 4B seeded
    "NBA":    "1.1.0",
    "Soccer": "1.1.0",
    "Tennis": "1.1.0",
    "NFL":    "2.0.0",   # Pass 2 real distribution
    "NHL":    "1.0.0",   # Pass 2 real distribution
}


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


def _anchor_pick_to_sim(pick: dict, sim_wp: float,
                         sim_meta: Optional[dict] = None) -> Optional[dict]:
    """Symmetric bounded-residual anchor (Phase 4B).

    The simulator baseline (:func:`sim_wp_to_lock_baseline`) is mapped
    to a **candidate lock score**.  We then apply a SYMMETRIC bounded
    residual so the sim can move the prior lock UP or DOWN by at most
    ``SIM_RESIDUAL_MAX`` (default 3.0 pp).

    Guardrails (Phase 4B):
      • Only INDEPENDENT simulators are honoured — posterior samplers
        (``independent_evidence=False``) return with zero adjustment.
      • Invalid simulator results (``valid=False``) return with zero
        adjustment.
      • Adjustment is bounded ``±SIM_RESIDUAL_MAX`` pp regardless of
        how far the sim baseline diverges.
      • Every adjustment is recorded via ``sim_lock_anchor``,
        ``sim_lock_prior``, ``sim_lock_residual``,
        ``sim_lock_applied_delta``.
      • Elite floor (95+ lock) is preserved — the sim cannot demote
        an elite-flagged pick below 95.
    """
    # ── Independence check ─────────────────────────────────────────
    if sim_meta is not None:
        if sim_meta.get("independent_evidence") is False:
            pick["sim_lock_anchor"]         = None
            pick["sim_lock_prior"]          = pick.get("lock_score")
            pick["sim_lock_residual"]       = 0.0
            pick["sim_lock_applied_delta"]  = 0.0
            pick["lock_anchored_to_sim"]    = False
            pick["sim_anchor_skip_reason"]  = "posterior_uncertainty_not_independent"
            return {"prior_lock": pick.get("lock_score"), "baseline": None,
                     "anchored": False, "new_lock": pick.get("lock_score"),
                     "reason": "not_independent"}
        if sim_meta.get("valid") is False:
            pick["sim_lock_anchor"]         = None
            pick["sim_lock_prior"]          = pick.get("lock_score")
            pick["sim_lock_residual"]       = 0.0
            pick["sim_lock_applied_delta"]  = 0.0
            pick["lock_anchored_to_sim"]    = False
            pick["sim_anchor_skip_reason"]  = "sim_invalid"
            return {"prior_lock": pick.get("lock_score"), "baseline": None,
                     "anchored": False, "new_lock": pick.get("lock_score"),
                     "reason": "invalid"}

    baseline = sim_wp_to_lock_baseline(sim_wp)
    try:
        prior_lock = float(pick.get("lock_score") or 0.0)
    except (TypeError, ValueError):
        prior_lock = 0.0

    # Symmetric bounded residual.
    residual = baseline - prior_lock
    applied_delta = max(-SIM_RESIDUAL_MAX, min(SIM_RESIDUAL_MAX, residual))

    # Elite floor: if the pick has an elite flag or a locked-in
    # 95+ historical evidence tier, do NOT demote it below 95.
    is_elite = bool(pick.get("elite_player")) or prior_lock >= 95.0
    new_lock = prior_lock + applied_delta
    if is_elite and new_lock < 95.0:
        new_lock = max(95.0, prior_lock)
        applied_delta = new_lock - prior_lock

    new_lock = round(max(0.0, min(99.0, new_lock)), 1)
    anchored = abs(applied_delta) >= 0.1        # meaningful adjustment

    # ── PERKLOCKS FIX 2 (2026-06) ────────────────────────────────
    # Simulators are EVIDENCE, not owners of canonical lock_score.
    # The prior code path here overwrote `lock_score` / `lock_score_raw`
    # / `lock_score_v2` / `lock_score_v2_raw` / `lock_score_peak` and
    # even downstream `grade` / `confidence` from the simulator baseline.
    # That violated the Universal Flow contract: canonical Lock Scores
    # are set once by the model pipeline and MUST NOT be mutated by
    # simulator anchoring. The audit fields below still record the
    # would-be delta so the Brain / analytics can weight sim agreement,
    # but the canonical `lock_score` on the pick is left untouched.
    # See Universal Flow Final Closure spec.

    # Audit fields — populated whether or not an anchor would have applied.
    pick["sim_lock_anchor"]         = round(baseline, 1)
    pick["sim_lock_prior"]          = round(prior_lock, 1)
    pick["sim_lock_residual"]       = round(residual, 2)
    pick["sim_lock_applied_delta"]  = round(applied_delta, 2)
    pick["lock_anchored_to_sim"]    = anchored

    # ── 2026-08-23 MLB MODEL-INTEGRITY SLICE 1 CLOSURE ────────────────
    # Promote the simulator's exact-line + selected-side probability to
    # the pick's canonical Win Expected (`win_probability` /
    # `model_win_prob`) — replacing the factor-average `_cal_mp` that
    # sports_engine seeded when no specialized engine was available.
    # This closes the confirmed defect: "Do not treat generic factor
    # averages as calibrated probability".
    #
    # Guard rails (surgical, opt-in only):
    #   * Only fires for INDEPENDENT + VALID distribution simulators
    #     (already gated above — reaching here implies both true).
    #   * Preserves specialized-engine probabilities (K-math /
    #     nfl_atd_engine / soccer_scorer / etc.).  When the pick already
    #     carries a specialized-engine marker, we leave `win_probability`
    #     alone and only stamp `sim_win_probability` audit.
    #   * Sim must have run enough runs (`sim_runs >= MIN_RUNS_FOR_ANCHOR`,
    #     already checked before `_anchor_pick_to_sim` is called).
    #
    # This ONLY touches the pick object in-place at the same call site
    # that already writes lock audit — no orchestrator / safe_picks /
    # canonical-publication plumbing is modified.
    _HAS_SPECIALIZED_ENGINE = any(
        pick.get(marker) is not None for marker in (
            "k_math_expected_k",         # MLB K probability engine (Poisson)
            "_atd_evidence_block",        # NFL ATD engine
            "atd_model_override",         # NFL ATD engine (legacy key)
            "nfl_yardage_engine_output",  # NFL yardage engine (if wired)
            "soccer_scorer_probability",  # Soccer scorer engine
        )
    )
    _sim_type = (sim_meta or {}).get("simulator_type", "")
    if (not _HAS_SPECIALIZED_ENGINE
            and _sim_type == "distribution_monte_carlo"
            and 0.02 <= sim_wp <= 0.99):
        # Preserve the prior factor-average as audit for telemetry so
        # any regression is diagnosable.
        prior_wp = pick.get("win_probability")
        if prior_wp is not None:
            pick["win_probability_prior_factor_mean"] = prior_wp
        pick["win_probability"]     = round(sim_wp * 100, 1)
        pick["model_win_prob"]      = float(sim_wp)
        pick["probability_source"]  = "sim_win_probability"
        pick["model_authority"]     = "distribution_monte_carlo"
        # Edge recomputation: use the same book_implied that was
        # attached upstream so callers reading `edge_percent` see the
        # sim-anchored edge, not the stale factor-mean edge.
        _book_imp = pick.get("book_implied") or pick.get("implied_probability")
        if isinstance(_book_imp, (int, float)) and _book_imp > 0:
            pick["edge_percent"] = round((sim_wp - float(_book_imp)) * 100, 2)

    return {
        "prior_lock": round(prior_lock, 1),
        "baseline":   round(baseline, 1),
        "residual":   round(residual, 2),
        "applied_delta": round(applied_delta, 2),
        "anchored":   anchored,
        "new_lock":   round(prior_lock, 1),
    }


def apply_simulations(picks: list[dict]) -> dict:
    """Run sport-specific simulators across the slate.  Mutates each
    pick in-place with sim_* fields AND (Phase 4B) applies a SYMMETRIC
    bounded residual anchor when the simulator is INDEPENDENT and VALID.

    Returns counts: {applied, stronger, weaker, neutral, anchored,
    lifted_up, lifted_down, skipped_not_independent, skipped_invalid}.
    """
    counts = {
        "applied": 0, "stronger": 0, "weaker": 0, "neutral": 0,
        "anchored": 0, "lifted_up": 0, "lifted_down": 0,
        "skipped_not_independent": 0, "skipped_invalid": 0,
    }
    for p in picks:
        sim = simulate_pick(p)
        if not sim:
            continue
        p.update(sim)
        counts["applied"] += 1
        sig = sim.get("sim_signal", "neutral")
        counts[sig] = counts.get(sig, 0) + 1

        # ── Anchor lock_score to sim_win_probability (symmetric) ───────
        sim_wp = sim.get("sim_win_probability")
        try:
            sim_runs = int(sim.get("sim_runs") or 0)
        except (TypeError, ValueError):
            sim_runs = 0

        if sim_wp is None or sim_runs < MIN_RUNS_FOR_ANCHOR:
            continue

        # Extract simulator metadata (independent_evidence / valid).
        # sport-specific sims (sim_mlb/nba/tennis/soccer/soccer_scorer)
        # are ALL true independent simulators — they never seed off μ.
        # If a caller writes an untyped result we default to
        # independent=True + valid=True for backward compatibility.
        sim_meta = {
            "independent_evidence": sim.get("independent_evidence", True),
            "valid":                sim.get("valid", True),
            "simulator_type":       sim.get("simulator_type",
                                            "distribution_monte_carlo"),
        }
        prior = float(p.get("lock_score") or 0.0)
        audit = _anchor_pick_to_sim(p, float(sim_wp), sim_meta=sim_meta)
        if audit is None:
            continue
        if audit.get("reason") == "not_independent":
            counts["skipped_not_independent"] += 1
            continue
        if audit.get("reason") == "invalid":
            counts["skipped_invalid"] += 1
            continue
        if audit["anchored"]:
            counts["anchored"] += 1
            if audit["new_lock"] > prior + 0.5:
                counts["lifted_up"] += 1
            elif audit["new_lock"] < prior - 0.5:
                counts["lifted_down"] += 1

    return counts
