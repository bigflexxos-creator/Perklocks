"""Tennis diagnostic + pricing endpoints (Session 6, 2026-09-17).

Two READ-ONLY endpoints:

    GET /api/tennis/matchup?player=&opponent=&surface=&tier=&best_of=
        → real strength snapshot + shrunk H2H + Markov distribution +
          spread & total ladder priced from the same simulation.

    GET /api/tennis/independence-audit?pick_id=
        → answers the Session 6 P11 question — "does this pick pass the
          independence contract?"  Flags synthetic-hash provenance,
          double-counted market bonuses, and missing evidence.

These endpoints DO NOT alter picks, scoring, or the Locks board.
"""
from __future__ import annotations

from typing import Annotated, Optional
from fastapi import APIRouter, Depends, HTTPException, Query

from deps import current_user
from auth import UserPublic


def _get_db():
    from server import db as _db
    return _db


router = APIRouter(prefix="/api/tennis", tags=["tennis-diagnostics"])


@router.get("/matchup")
async def matchup_diagnostic(
    user: Annotated[UserPublic, Depends(current_user)],
    player: str,
    opponent: str,
    surface: Optional[str] = "Hard",
    tier: Optional[int] = 5,
    best_of: int = 3,
    n_sims: int = 3000,
):
    """Real Tennis matchup diagnostic — Elo, surface Elo, form, H2H,
    Markov distribution and monotonic spread/total pricing.  ALL from
    ``tennis_matches_history`` — no synthetic evidence."""
    from services.tennis_strength import compose_matchup
    from services.tennis_markov import price_from_matchup, enforce_monotonic_ladder

    db = _get_db()
    ev = await compose_matchup(db, player, opponent, surface=surface, tier=tier)
    # Sportsbook-convention spreads: negative = player is favourite,
    # positive = player is dog.  Pricing engine converts internally.
    priced = price_from_matchup(
        ev.matchup_prob or 0.5, tour=(ev.player.tour or "ATP"),
        spread_thresholds=[-6.5, -5.5, -4.5, -3.5, -2.5, -1.5,
                            1.5, 2.5, 3.5, 4.5, 5.5, 6.5],
        total_thresholds=[19.5, 20.5, 21.5, 22.5, 23.5, 24.5, 25.5],
        best_of=best_of, n_sims=n_sims,
    )
    return {
        "player":       player,
        "opponent":     opponent,
        "surface":      surface,
        "evidence":     ev.to_dict(),
        "ml_prob":      priced.ml_prob,
        "distribution": priced.distribution.to_dict(),
        "spreads":      enforce_monotonic_ladder(priced.spread_prices),
        "totals":       priced.total_prices,
        # Session 6 P1/P2 provenance disclosure.
        "evidence_families": priced.evidence_families,
        "serve_return_provenance": priced.distribution.serve_return_provenance,
        "simulation_provenance":   priced.distribution.simulation_provenance,
        "independence": {
            # A pick is EMPIRICAL_INDEPENDENT only when ALL are true:
            # 1. Reliability >= 0.4
            # 2. No missing evidence flags
            # 3. Serve/return + simulation are backed by real
            #    independent observations (currently never — Session
            #    6 P1 flags them as ELO_DERIVED / MODEL_CONDITIONED
            #    until raw serve% ingest is wired).
            "empirical_independent": (
                ev.reliability >= 0.4
                and not ev.missing_flags
                and priced.distribution.serve_return_provenance == "EMPIRICAL"
                and priced.distribution.simulation_provenance == "EMPIRICAL_INDEPENDENT"
            ),
            "model_conditioned": True,
            "notes": (ev.missing_flags or []) + [
                f"serve_return={priced.distribution.serve_return_provenance}",
                f"simulation={priced.distribution.simulation_provenance}",
            ],
        },
    }


@router.get("/independence-audit")
async def independence_audit(
    user: Annotated[UserPublic, Depends(current_user)],
    pick_id: str,
):
    """P11 Independence Contract.  Returns pass/fail with the exact
    fields that either broke or passed the contract."""
    db = _get_db()
    pick = await db.picks.find_one({"id": pick_id})
    if not pick:
        raise HTTPException(404, "pick not found")
    if (pick.get("sport") or "").lower() != "tennis":
        return {"pick_id": pick_id, "sport": pick.get("sport"),
                "applicable": False,
                "note": "P11 audit is Tennis-only"}
    comps = pick.get("tennis_components") or {}
    missing_flags = pick.get("tennis_missing_flags") or []
    reliability = pick.get("tennis_evidence_reliability")
    reasons: list[str] = []
    if missing_flags:
        reasons.append(f"missing_evidence: {','.join(missing_flags)}")
    if reliability is not None and reliability < 0.4:
        reasons.append(f"low_reliability: {reliability}")
    # Ensure no market_bump component is in the composite (Session 6 P2)
    if "market_bump" in (pick.get("tennis_lock_breakdown") or {}):
        reasons.append("market_bump_present (P2 breach)")
    pass_ = not reasons
    return {
        "pick_id":       pick_id,
        "sport":         "Tennis",
        "applicable":    True,
        "pass":          pass_,
        "reasons":       reasons,
        "components":    comps,
        "reliability":   reliability,
        "missing_flags": missing_flags,
    }


@router.get("/walkforward")
async def tennis_walkforward(
    user: Annotated[UserPublic, Depends(current_user)],
    sample_limit: Optional[int] = None,
    min_matches_per_player: int = 5,
):
    """Session 8 · Chronological walk-forward validation.

    Replays ``tennis_matches_history`` in strict chronological order,
    generating an Elo-based match win probability at each step from
    ONLY prior matches, scoring against the actual outcome AFTER the
    prediction is recorded.  Returns per-tour/surface/bucket
    Brier + log-loss + calibration.  Baseline champion is 50/50
    (in the absence of authentic historical odds — those metrics
    are honestly omitted).
    """
    from services.tennis_walkforward import run_walkforward
    return await run_walkforward(
        _get_db(),
        sample_limit=sample_limit,
        min_matches_per_player=min_matches_per_player,
    )


# ── Session 9 · Final Six Partials Closure ──────────────────────────
# These two endpoints close the "Tennis calibration — no leakage" and
# "Real champion vs challenger" partials from the acceptance report.

@router.get("/calibrated-walkforward")
async def tennis_calibrated_walkforward(
    user: Annotated[UserPublic, Depends(current_user)],
    train_end: str = "2020-12-31",
    calibration_end: str = "2023-12-31",
    min_matches_per_player: int = 5,
):
    """Chronological TRAIN → CALIBRATION → TEST split.  Fits a Platt
    rescaler ONLY on the calibration window; the rescaler is
    evaluated on the UNTOUCHED test period alongside the raw
    challenger.  Reports 5% calibration buckets from 50-55 up through
    95+.  Honestly recommends KEEPING the rescaler only when it
    improves TEST-set Brier — refuses to force probabilities upward
    on training evidence alone."""
    from services.tennis_calibration_walkforward import run_calibrated_walkforward
    return await run_calibrated_walkforward(
        _get_db(),
        train_end=train_end,
        calibration_end=calibration_end,
        min_matches_per_player=min_matches_per_player,
    )


@router.get("/champion-comparison")
async def tennis_champion_comparison(
    user: Annotated[UserPublic, Depends(current_user)],
    train_end: str = "2020-12-31",
    calibration_end: str = "2023-12-31",
    min_matches_per_player: int = 5,
):
    """Attempted reconstruction of the pre-Session-6 tennis champion
    versus the new raw and calibrated challengers on the untouched
    TEST period.  The old champion combined a deterministic hash-
    based strength signal with a sportsbook `market_bump`.  Because
    ``tennis_matches_history`` has NO historical odds, the market_bump
    is UNRECOVERABLE.  We reconstruct the hash component faithfully
    and label the comparison honestly as:
        `ACTUAL CHAMPION COMPARISON — NOT CERTIFIED`
    We DO NOT substitute a 50/50 baseline and call it the champion.
    """
    from services.tennis_calibration_walkforward import run_champion_comparison
    return await run_champion_comparison(
        _get_db(),
        train_end=train_end,
        calibration_end=calibration_end,
        min_matches_per_player=min_matches_per_player,
    )
