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
        "independence": {
            "empirical_independent": ev.reliability >= 0.4 and not ev.missing_flags,
            "model_conditioned":     ev.reliability < 0.4 or bool(ev.missing_flags),
            "notes":                 ev.missing_flags,
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
