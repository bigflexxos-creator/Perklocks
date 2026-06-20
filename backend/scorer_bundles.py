"""Scorer Bundles — Synthesized soccer same-game-parlay insights.

The Odds API does not expose `player_to_score_2_or_more` or
`player_anytime_assist` as standalone markets. To still give users a
"2+ goals" or "Goal + Assist" read, we synthesize the probabilities from
the anytime goal scorer market using Poisson and correlated-prop math.

Math:
  * λ (expected goals) from anytime price:  P_anytime = 1 − e^−λ  →  λ = −ln(1 − P)
  * P(2+ goals) = 1 − e^−λ − λ·e^−λ
  * P(hat trick) = 1 − e^−λ − λ·e^−λ − (λ²/2)·e^−λ
  * P(goal + assist) ≈ P_anytime × P_assist_given_goal
    where P_assist_given_goal is a position-derived prior:
      Forward (e.g. Kane, Mbappé, Vini Jr): 0.30
      Mid/playmaker (Messi, KDB):           0.45
      Default attacker:                     0.32

Mounted at /api/picks/{id}/scorer-bundles (read-only insight).
"""
from __future__ import annotations

import math
import re

from fastapi import APIRouter, Depends, HTTPException

router = APIRouter(tags=["scorer_bundles"])


def _get_db():
    from server import db
    return db


def _require_auth():
    from server import current_user
    return current_user


# Position priors — best-effort lookup by player name
PLAYMAKER_NAMES = {
    "lionel messi", "kevin de bruyne", "bruno fernandes", "kai havertz",
    "phil foden", "jude bellingham", "florian wirtz", "pedri",
}
FORWARD_NAMES = {
    "harry kane", "kylian mbappé", "kylian mbappe", "erling braut haaland",
    "erling haaland", "vinicius junior", "vinícius júnior", "robert lewandowski",
    "cristiano ronaldo", "lautaro martínez", "lautaro martinez",
    "darwin nuñez", "darwin nunez", "victor osimhen",
}


def _assist_prior(player_name: str) -> float:
    n = (player_name or "").lower()
    if n in PLAYMAKER_NAMES:
        return 0.45
    if n in FORWARD_NAMES:
        return 0.30
    return 0.32


def _american_to_implied(american) -> float | None:
    try:
        a = float(american)
    except Exception:
        return None
    if a == 0:
        return None
    return -a / (-a + 100) if a < 0 else 100 / (a + 100)


def _implied_to_american(p: float) -> int:
    if p <= 0 or p >= 1:
        return 0
    return int(round(-100 * p / (1 - p))) if p >= 0.5 else int(round(100 * (1 - p) / p))


def _extract_player(market: str) -> str:
    """Extract player name from 'X Anytime Goal Scorer' / similar."""
    m = re.match(r"^(?P<name>[A-Z][A-Za-zÀ-ÿ.\-' ]+?)\s+(?:Anytime|First|Last|To Score)",
                 market or "", re.IGNORECASE)
    return m.group("name").strip() if m else ""


@router.get("/picks/{pick_id}/scorer-bundles")
async def scorer_bundles(
    pick_id: str,
    user=Depends(_require_auth()),
):
    """Synthesized 2+ goals / hat-trick / goal+assist probabilities.

    Only meaningful for Soccer Anytime Goal Scorer picks. For other markets
    returns `{eligible: False}` with no payload.
    """
    db = _get_db()
    pick = await db.picks.find_one({"id": pick_id}, {"_id": 0})
    if not pick:
        raise HTTPException(404, "pick not found")

    market = (pick.get("market") or "")
    sport = pick.get("sport") or ""
    market_l = market.lower()
    eligible = (sport == "Soccer"
                and "goal scorer" in market_l
                and "first" not in market_l
                and "last" not in market_l)
    if not eligible:
        return {"pick_id": pick_id, "eligible": False,
                "note": "Scorer bundles only synthesize for Anytime Goal Scorer picks."}

    player = _extract_player(market) or (pick.get("elite_player_name") or "")
    american = pick.get("book_odds")
    p_anytime = _american_to_implied(american)
    if p_anytime is None or p_anytime <= 0 or p_anytime >= 0.95:
        # Edge case: extreme chalk or junk price — bail
        return {"pick_id": pick_id, "eligible": True, "synthesizable": False,
                "note": "Anytime odds out of range for reliable synthesis."}

    # Expected goals via Poisson inversion: P(≥1) = 1 - e^-λ
    lam = -math.log(1.0 - p_anytime)
    p_2plus = 1.0 - math.exp(-lam) - lam * math.exp(-lam)
    p_hat = 1.0 - math.exp(-lam) - lam * math.exp(-lam) - (lam ** 2 / 2.0) * math.exp(-lam)

    # Goal + Assist same-game synthesis
    p_assist_given_goal = _assist_prior(player)
    p_goal_and_assist = p_anytime * p_assist_given_goal

    def fmt(p: float) -> dict:
        p = max(0.0001, min(0.9999, p))
        american = _implied_to_american(p)
        sign = "+" if american > 0 else ""
        return {"probability": round(p * 100, 1),
                "fair_american": f"{sign}{american}",
                "decimal": round(1 / p, 2)}

    return {
        "pick_id":     pick_id,
        "eligible":    True,
        "synthesizable": True,
        "player":      player,
        "primary_market": market,
        "primary_odds": american,
        "primary_implied_pct": round(p_anytime * 100, 1),
        "expected_goals_λ":  round(lam, 3),
        "bundles": [
            {"name": "Anytime Goal",            "type": "primary",     **fmt(p_anytime)},
            {"name": "2+ Goals",                "type": "synthesized", **fmt(p_2plus)},
            {"name": "Hat Trick (3+)",          "type": "synthesized", **fmt(max(p_hat, 0.0001))},
            {"name": "Goal + Assist (SGP)",     "type": "synthesized", **fmt(p_goal_and_assist)},
        ],
        "method": "Poisson inversion from anytime odds. Synthesized lines are "
                  "model estimates — book prices may differ. Use for sizing "
                  "edge expectations, not direct settlement.",
    }
