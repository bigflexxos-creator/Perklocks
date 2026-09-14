"""
UNIVERSAL EVIDENCE AUTHORITY — LIVE DISTRIBUTION VALIDATION
(2026-06 · P23 / P24 / P25)
============================================================

Read-only script that reports:
    * Structural reachability certification for every mature market
      family in every in-scope sport (via ``compute_lock_score``).
    * Live distribution per SPORT × MARKET_FAMILY (from the current
      ``picks`` collection) with count buckets at 85–89 / 90–92 /
      93–95 / 96–97 / 98 / 99 / 100 and the maximum LS on the board.
    * For every family whose live max is < 96, the exact limiting
      evidence component (which axis is the weakest).
    * For every live 98+ / 99 pick, the full provenance block (WP,
      reliability, history, matchup/role, convergence, distribution,
      DQ, coverage, contradictions, authority source, final LS).

Usage:
    cd /app/backend
    python -m services.uea_live_distribution_report

Never mutates the database.  Never publishes anything.  Prints one
consolidated report at the end and returns a machine-readable dict
so this can also be invoked from a route or a pytest.
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
    compute_authority_score, peak_non_apex_eligible, enabled as uea_enabled,
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


def _bucket_of(ls: float) -> str:
    for name, lo, hi in TIER_BUCKETS:
        if lo <= ls <= hi:
            return name
    if ls < 85.0:
        return "below_85"
    return "unknown"


def _limiting_axis(components: dict) -> tuple[str, Any]:
    """Return (axis_name, score) of the weakest available axis."""
    best = None
    for name, c in (components or {}).items():
        if c.get("status") != "AVAILABLE":
            return (name, None)
        s = c.get("score")
        if s is None:
            continue
        if best is None or s < best[1]:
            best = (name, s)
    return best or ("unknown", None)


async def build_report(*, db=None) -> dict:
    if db is None:
        # ── UEA live-distribution runs from the CLI too — make sure
        # we load .env so we hit the same lockscore_db the server uses.
        from dotenv import load_dotenv
        load_dotenv()
        from services.database import get_database
        db = get_database()
    now = __import__("datetime").datetime.utcnow()

    dist: dict[str, dict[str, Any]] = {}
    for s in IN_SCOPE_SPORTS:
        dist[s] = {}

    live_98_99: list[dict] = []

    # Only look at CURRENT, on-board, non-shadow picks — never touch
    # settled/historical rows.
    query = {"off_board": {"$ne": True}, "no_bet": {"$ne": True}}
    cursor = db.picks.find(query, projection={
        "_id": 0, "sport": 1, "market": 1, "lock_score": 1,
        "published_lock_score": 1, "evidence_authority": 1,
        "bet_quality_authority": 1, "win_probability": 1,
        "probability_provenance": 1, "data_quality": 1,
        "peak_non_apex_eligible": 1,
    })

    async for pick in cursor:
        sport = str(pick.get("sport") or "").upper()
        # Case-normalise from the DB where docs use ``Soccer`` / ``Tennis``.
        if sport not in IN_SCOPE_SPORTS:
            continue
        family = _classify_market_family(sport, pick.get("market") or "")
        if family not in dist[sport]:
            dist[sport][family] = {
                "total": 0, "max_ls": 0.0, "max_pick": None,
                "buckets": {b[0]: 0 for b in TIER_BUCKETS},
                "below_85": 0,
                "limiting_axes": defaultdict(int),
            }
        entry = dist[sport][family]
        ls = float(pick.get("lock_score") or 0.0)
        entry["total"] += 1
        if ls > entry["max_ls"]:
            entry["max_ls"] = ls
            entry["max_pick"] = {
                "market": pick.get("market"),
                "lock_score": ls,
                "wp": pick.get("win_probability"),
                "provenance": pick.get("probability_provenance"),
            }
        bkt = _bucket_of(ls)
        if bkt == "below_85":
            entry["below_85"] += 1
        else:
            entry["buckets"][bkt] = entry["buckets"].get(bkt, 0) + 1
        # Limiting axis tracking
        ea = pick.get("evidence_authority") or {}
        comps = ea.get("components") or {}
        axis, _ = _limiting_axis(comps)
        entry["limiting_axes"][axis] += 1
        # 98+ / 99 provenance
        if ls >= 98.0:
            live_98_99.append({
                "sport": sport, "market": pick.get("market"),
                "lock_score": ls, "wp": pick.get("win_probability"),
                "provenance": pick.get("probability_provenance"),
                "evidence_authority": ea,
                "bet_quality_authority": pick.get("bet_quality_authority"),
                "peak_non_apex_eligible": pick.get("peak_non_apex_eligible"),
            })

    # Convert defaultdicts for JSON
    for s in dist:
        for fam in dist[s]:
            e = dist[s][fam]
            e["limiting_axes"] = dict(e["limiting_axes"])

    # Attach limiting-axis explainer for families whose max < 96
    for s in dist:
        for fam, e in dist[s].items():
            if e["max_ls"] < 96.0 and e["max_pick"]:
                # Load that specific pick's authority for detail
                mkt = e["max_pick"]["market"]
                doc = await db.picks.find_one(
                    {"sport": s, "market": mkt},
                    projection={"_id": 0, "evidence_authority": 1})
                if doc and doc.get("evidence_authority"):
                    ax, sc = _limiting_axis(
                        doc["evidence_authority"].get("components") or {})
                    e["limit_explanation"] = {
                        "axis": ax, "score": sc,
                        "coverage": doc["evidence_authority"].get("coverage"),
                        "strong_axes": doc["evidence_authority"].get("strong_axes"),
                        "contradictions": doc["evidence_authority"].get("contradictions"),
                    }

    report = {
        "generated_at": now.isoformat(),
        "sports": IN_SCOPE_SPORTS,
        "distribution": dist,
        "peak_provenance": live_98_99,
        "structural_reachability_tests": (
            "See tests/test_universal_evidence_authority.py::"
            "TestProgressiveTierReachability for 85/90/93/96/98/99 proofs."),
    }
    return report


def main() -> None:
    async def _run():
        try:
            report = await build_report()
        except Exception as e:
            print(json.dumps({"error": str(e)}, indent=2))
            sys.exit(1)
        print(json.dumps(report, indent=2, default=str))
    asyncio.run(_run())


if __name__ == "__main__":
    main()
