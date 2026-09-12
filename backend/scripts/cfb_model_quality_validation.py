"""
CFB Model-Quality Upgrade Validation
=====================================

Runs 5 representative CFB scenarios through both the OLD and NEW
scoring paths and prints a compact before/after comparison.

Scenarios:
  1. Strong favorite    — Georgia @ Vanderbilt (SP+ delta ~+22)
  2. Moderate favorite  — Michigan @ Michigan St (SP+ delta ~+9)
  3. Pick-em            — Iowa @ Wisconsin (SP+ delta ~0)
  4. Legitimate dog     — Auburn @ Alabama Auburn side (+7-10)
  5. FCS on FBS         — Texas Southern @ UTEP (should FAIL CLOSED)

The purpose is NOT to prove the model wins vs sportsbook — it is to
show the fix removes weak-data 98s while preserving legitimate
high-confidence Locks.
"""
from __future__ import annotations

import asyncio
import os
import sys
from math import erf, sqrt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from services.cfb_game_model import (  # noqa: E402
    estimate_cfb_game, HOME_FIELD_ADV, MARGIN_K,
)
from sports_engine import compute_lock_score, _implied_prob  # noqa: E402


def _american_odds_from_prob(p: float) -> int:
    """Round-trip helper: convert prob → typical American ML price."""
    if p >= 0.5:
        return -round(100 * p / (1 - p))
    return round(100 * (1 - p) / p)


async def _load_context():
    load_dotenv()
    mongo = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = mongo[os.environ.get("DB_NAME", "lockscore")]
    year = 2026
    ratings = {}
    async for d in db.cfb_sp_ratings.find({"year": year}, {"_id": 0}):
        ratings[(d.get("team") or "").strip().lower()] = d
    rp_map = {}
    async for d in db.cfb_returning_production.find({"year": year}, {"_id": 0}):
        rp_map[(d.get("team") or "").strip().lower()] = d
    pt_map = {}
    async for d in db.cfb_portal.find({"year": year}, {"_id": 0}):
        team = (d.get("team") or "").strip().lower()
        pt_map.setdefault(team, {"incoming_n": 0, "outgoing_n": 0, "net": 0.0})
    return {
        "cfb_sp_ratings_by_team": ratings,
        "cfb_returning_prod_by_team": rp_map,
        "cfb_portal_net_by_team": pt_map,
    }


def _score(mp: float, side_ml: int, factors: dict, pick_extras: dict):
    """Return (lock_score, breakdown) using the current NEW code path."""
    _e_ml = round((mp - _implied_prob(side_ml)) * 100, 2)
    pick = {
        "book_odds": side_ml,
        "edge_percent": _e_ml,
        "win_probability": mp * 100,
        "sport": "CFB",
        "market": "Home Moneyline",
        **pick_extras,
    }
    lock, breakdown = compute_lock_score(
        factors, win_prob=mp * 100, pick=pick, edge_percent=_e_ml,
    )
    return lock, breakdown, _e_ml


def _run_scenario(name: str, ctx: dict, home: str, away: str,
                   book_home_ml_prob: float):
    """Print before/after LS for one scenario.

    Before  = empty factor dict + no data_quality/provenance hints
    After   = new evidence-populated factors + data_quality-driven
              provenance mapping (as the live CFB path emits now)
    """
    r = estimate_cfb_game(ctx, home, away)
    print(f"\n{'='*70}\n{name}: {away} @ {home}\n{'='*70}")
    if not r.available:
        print(f"  MODEL_UNAVAILABLE : {r.reason}")
        return
    p_home = float(r.p_home_ml or 0.5)
    mp = p_home
    side_ml = _american_odds_from_prob(book_home_ml_prob)
    _e_pct = round((mp - _implied_prob(side_ml)) * 100, 2)
    print(f"  Model:  p_home={mp:.4f}  exp_margin={r.expected_margin:+.1f}  "
          f"exp_total={r.expected_total}  data_quality='{r.data_quality}'")
    print(f"  Market: book_ML={side_ml:+d}  implied={_implied_prob(side_ml):.4f}  "
          f"edge={_e_pct:+.2f}pt")

    # ── OLD path (pre-fix): pick=None equivalent + empty factors ─
    lock_old, brk_old, _ = _score(mp, side_ml, factors={},
                                   pick_extras={})
    print(f"  BEFORE (empty factors, no data_quality/provenance):")
    print(f"    LS = {lock_old:.1f}   ← simulated pre-fix behaviour")

    # ── NEW path: with evidence factors + provenance mapping ─────
    dq = r.data_quality or ""
    if "returning_prod_both" in dq and "portal_both" in dq:
        prov = "CAUSAL_INDEPENDENT"
    elif any(k in dq for k in ("returning_prod_partial", "portal_partial",
                                "returning_prod_both", "portal_both")):
        prov = "EMPIRICAL_INDEPENDENT"
    else:
        prov = "MODEL_CONDITIONED"

    _new_mp = mp
    _new_e = _e_pct
    # Apply data-quality-aware market shrinkage (matches live path)
    _has_rp_both = "returning_prod_both" in dq
    _has_pt_both = "portal_both" in dq
    _has_ctx = any(k in dq for k in
                    ("returning_prod_both", "returning_prod_partial",
                     "portal_both", "portal_partial"))
    if _has_rp_both and _has_pt_both:
        shrink = 0.0
    elif _has_ctx:
        shrink = 0.15
    else:
        shrink = 0.25
    mkt = _implied_prob(side_ml)
    if shrink > 0 and abs(mp - mkt) > 0.15:
        _new_mp = (1 - shrink) * mp + shrink * mkt
        _new_e = round((_new_mp - mkt) * 100, 2)
        print(f"  Market prior shrink (dq={dq!r}): "
              f"{mp*100:.2f} → {_new_mp*100:.2f} (Δ{shrink*100:.0f}%)")

    factors_new = {
        "Projected Margin": round(r.expected_margin or 0, 2),
        "Expected Total":   round(r.expected_total or 0, 2),
        "Model Fair Prob":  round(_new_mp * 100, 2),
        "Sportsbook Implied Prob": round(_implied_prob(side_ml) * 100, 2),
        "__data_quality":   str(dq),
        "SP+ Margin Base":  round(float((r.provenance or {})
                                          .get("sp_base_margin") or 0), 2),
    }
    lock_new, brk_new, _ = _score(_new_mp, side_ml, factors_new,
                                   pick_extras={
                                       "data_quality": dq,
                                       "probability_provenance": prov,
                                   })
    print(f"  AFTER  (real evidence + provenance={prov}):")
    print(f"    LS = {lock_new:.1f}   Δ = {lock_new - lock_old:+.1f}")
    print(f"  Real evidence in factors: {list(factors_new.keys())}")


async def main():
    ctx = await _load_context()
    print(f"\nLoaded SP+ ratings for {len(ctx['cfb_sp_ratings_by_team'])} teams")

    _run_scenario(
        "1. STRONG FAVORITE",
        ctx, home="Georgia Bulldogs", away="Vanderbilt Commodores",
        book_home_ml_prob=0.88,
    )
    _run_scenario(
        "2. MODERATE FAVORITE",
        ctx, home="Michigan Wolverines", away="Michigan State Spartans",
        book_home_ml_prob=0.72,
    )
    _run_scenario(
        "3. PICK-EM",
        ctx, home="Iowa Hawkeyes", away="Wisconsin Badgers",
        book_home_ml_prob=0.55,
    )
    _run_scenario(
        "4. LEGITIMATE DOG (home dog)",
        ctx, home="Auburn Tigers", away="Alabama Crimson Tide",
        book_home_ml_prob=0.30,
    )
    _run_scenario(
        "5. FCS ON FBS (must fail closed)",
        ctx, home="UTEP Miners", away="Texas Southern Tigers",
        book_home_ml_prob=0.94,
    )


if __name__ == "__main__":
    asyncio.run(main())
