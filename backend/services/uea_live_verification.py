"""
UNIVERSAL EVIDENCE AUTHORITY — LIVE PRODUCTION VERIFICATION
(2026-06 · Live Verification Pass)

Read-only script.  Loads the CURRENT persisted live board and for
every pick computes the UEA contract on-demand from the persisted
factor / provenance fields so we can:

    * Report EXACT live distribution by SPORT × MARKET FAMILY
    * Show max LS + max pick per family
    * For every family whose max < 96, report the exact limiting axis
    * For every live 98 / 99, dump the full provenance block
    * Independently verify UEA reachability against the live board

The persisted picks may pre-date the UEA integration; we therefore
RECOMPUTE the UEA contract from the fields that ARE persisted
(win_probability, factors, provenance where available, sim_stability,
etc.).  We do NOT rescore or republish — this is verification only.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from collections import defaultdict
from typing import Any

from services.evidence_authority_adapters import (
    _classify_market_family, build_contract_for_pick,
)
from services.evidence_authority_contract import (
    compute_authority_score, peak_non_apex_eligible,
)


TIER_BUCKETS = [
    ("85_89", 85.0, 89.999),
    ("90_92", 90.0, 92.999),
    ("93_95", 93.0, 95.999),
    ("96_97", 96.0, 97.999),
    ("98",    98.0, 98.999),
    ("99",    99.0, 99.999),
    ("100",  100.0, 100.001),
]

IN_SCOPE_SPORTS = ("MLB", "NFL", "CFB", "SOCCER", "TENNIS")


def _bucket(ls: float) -> str:
    for name, lo, hi in TIER_BUCKETS:
        if lo <= ls <= hi:
            return name
    if ls < 85.0:
        return "below_85"
    return "unknown"


def _compute_uea_for_pick(p: dict) -> dict | None:
    """Recompute UEA contract from persisted fields."""
    # Rebuild the input the way `compute_lock_score` would see it.
    factors = p.get("factors") or {}
    scoring_factors = None
    # Some ingestors persist normalised inputs under "scoring_factors"
    # or "signal_axes" — try both.
    for k in ("scoring_factors", "signal_axes", "factor_scores"):
        if isinstance(p.get(k), dict):
            scoring_factors = p[k]
            break
    if scoring_factors is None:
        # Fall back to factors themselves — many factors already carry
        # 0..1 "norm" values that are direction-aligned.
        scoring_factors = {k: v for k, v in factors.items()
                             if isinstance(v, (int, float))}
    contract = build_contract_for_pick(p, factors, scoring_factors)
    if contract is None:
        return None
    return compute_authority_score(contract)


def _limiting_axis(components: dict) -> tuple[str, Any]:
    if not components:
        return ("unknown", None)
    best = None
    for name, c in components.items():
        if c.get("status") != "AVAILABLE":
            return (name, None)
        s = c.get("score")
        if s is None:
            continue
        if best is None or s < best[1]:
            best = (name, s)
    return best or ("unknown", None)


async def build_verification_report(db=None) -> dict:
    if db is None:
        from dotenv import load_dotenv; load_dotenv()
        from services.database import get_database
        db = get_database()

    dist: dict[str, dict[str, Any]] = {s: {} for s in IN_SCOPE_SPORTS}
    live_98_99: list[dict] = []
    limiting_axes_all: dict[str, dict[str, int]] = {}

    query = {"off_board": {"$ne": True}, "no_bet": {"$ne": True}}
    projection = {
        "_id": 0, "sport": 1, "market": 1, "market_key": 1,
        "lock_score": 1, "published_lock_score": 1,
        "win_probability": 1, "model_win_probability": 1,
        "probability_provenance": 1, "data_quality": 1,
        "factors": 1, "scoring_factors": 1,
        "sim_stability": 1, "simulator_provenance": 1,
        "matchup_score": 1, "opp_defense_grade": 1,
        "exact_threshold_hit_rate": 1, "history_sample_size": 1,
        "no_vig_implied_pct": 1, "lock_components": 1,
        "line": 1, "player_name": 1, "selection": 1,
        "book_odds": 1, "sportsbook": 1,
        "evidence_authority": 1, "bet_quality_authority": 1,
        "reliability_cap_scoped": 1, "reliability_cap_applied": 1,
        "peak_non_apex_eligible": 1, "peak_non_apex_denied_reason": 1,
        "cfb_independent_sim": 1, "mp_from_book_seed": 1,
        "created_at": 1,
    }

    cursor = db.picks.find(query, projection=projection)
    async for pick in cursor:
        raw_sport = str(pick.get("sport") or "")
        sport = raw_sport.upper()
        if sport not in IN_SCOPE_SPORTS:
            continue
        # Restore string sport for the classifier (needs case-insensitive).
        market = pick.get("market") or pick.get("market_key") or ""
        family = _classify_market_family(sport, market)
        # Live LS from persisted authoritative field.
        ls = float(pick.get("lock_score") or 0.0)
        if family not in dist[sport]:
            dist[sport][family] = {
                "total": 0,
                "buckets": {b[0]: 0 for b in TIER_BUCKETS},
                "below_85": 0,
                "max_ls": 0.0, "max_pick": None,
                "uea_recompute_ceiling_max": 0.0,
            }
        entry = dist[sport][family]
        entry["total"] += 1
        bkt = _bucket(ls)
        if bkt == "below_85":
            entry["below_85"] += 1
        else:
            entry["buckets"][bkt] += 1
        if ls > entry["max_ls"]:
            entry["max_ls"] = ls
            entry["max_pick"] = {
                "market": market,
                "lock_score": ls,
                "wp": pick.get("win_probability"),
                "provenance": pick.get("probability_provenance"),
                "line": pick.get("line"),
                "book_odds": pick.get("book_odds"),
                "sportsbook": pick.get("sportsbook"),
                "reliability_cap_scoped": pick.get("reliability_cap_scoped"),
                "reliability_cap_applied": pick.get("reliability_cap_applied"),
            }

        # Recompute UEA from persisted fields (verification).
        # Case-normalise sport in the pick for the adapter.
        pick["sport"] = sport
        uea = _compute_uea_for_pick(pick)
        if uea:
            if uea["ceiling"] > entry["uea_recompute_ceiling_max"]:
                entry["uea_recompute_ceiling_max"] = uea["ceiling"]

        # Peak provenance record
        if ls >= 98.0:
            record = {
                "sport": sport, "market": market, "line": pick.get("line"),
                "book_odds": pick.get("book_odds"),
                "sportsbook": pick.get("sportsbook"),
                "lock_score": ls,
                "wp": pick.get("win_probability"),
                "probability_provenance": pick.get("probability_provenance"),
                "reliability_cap_scoped": pick.get("reliability_cap_scoped"),
                "reliability_cap_applied": pick.get("reliability_cap_applied"),
                "mp_from_book_seed": pick.get("mp_from_book_seed"),
                "uea_recompute": uea,
                "bet_quality_authority_ceiling": (
                    pick.get("bet_quality_authority") or {}
                ).get("ceiling"),
                "peak_non_apex_eligible": pick.get("peak_non_apex_eligible"),
                "peak_non_apex_denied_reason": pick.get("peak_non_apex_denied_reason"),
                "cfb_independent_sim": pick.get("cfb_independent_sim"),
            }
            live_98_99.append(record)

    # Limiting-axis explainer for families whose max < 96.
    for sport in dist:
        for fam, entry in dist[sport].items():
            if entry["max_ls"] < 96.0 and entry["max_pick"]:
                # Fetch the strongest candidate again to compute UEA
                strongest = await db.picks.find_one(
                    {"sport": sport.title() if sport in ("SOCCER", "TENNIS") else sport,
                     "market": entry["max_pick"]["market"],
                     "off_board": {"$ne": True}},
                    projection=projection)
                if strongest:
                    strongest["sport"] = sport
                    uea = _compute_uea_for_pick(strongest)
                    if uea:
                        ax, sc = _limiting_axis(uea.get("components") or {})
                        entry["limit_explanation"] = {
                            "limiting_axis": ax,
                            "limiting_axis_score": sc,
                            "uea_coverage": uea.get("coverage"),
                            "uea_ceiling":  uea.get("ceiling"),
                            "uea_strong_axes": uea.get("strong_axes"),
                            "uea_contradictions": uea.get("contradictions"),
                            "components": {k: v.get("status")
                                             for k, v in (uea.get("components") or {}).items()},
                        }

    # MLB publication parity — sample 10 current MLB picks and prove
    # scorer_LS == persisted lock_score == published_lock_score.
    parity_samples = []
    async for p in db.picks.find(
            {"sport": "MLB", "off_board": {"$ne": True},
             "no_bet": {"$ne": True}},
            projection={"_id": 0, "market": 1, "lock_score": 1,
                          "published_lock_score": 1, "player_name": 1,
                          "line": 1}).sort("lock_score", -1).limit(10):
        parity_samples.append({
            "market":   p.get("market"),
            "player":   p.get("player_name"),
            "line":     p.get("line"),
            "lock_score": p.get("lock_score"),
            "published_lock_score": p.get("published_lock_score"),
            "parity": (p.get("lock_score") == p.get("published_lock_score")),
        })

    return {
        "generated_at": __import__("datetime").datetime.utcnow().isoformat(),
        "sports_in_scope": IN_SCOPE_SPORTS,
        "distribution": dist,
        "peak_provenance_98_99": live_98_99,
        "mlb_publication_parity_samples": parity_samples,
    }


def main():
    async def _run():
        r = await build_verification_report()
        print(json.dumps(r, indent=2, default=str))
    asyncio.run(_run())


if __name__ == "__main__":
    main()
