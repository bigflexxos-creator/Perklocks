"""P0.4 WRITE-PASS — Verified Historical Settlement Correction.

Applies the reviewed and approved dry-run proposals from
``/tmp/p04_historical_correction_report.json`` to ``db.picks``.

Contract (spec-exact):

  * Do NOT expand the correction population.  If the live DB state
    differs from the reviewed proposal, STOP that row and record it
    as a ``skipped_drift`` row.  Never overwrite a row that the
    operator did not authorise.
  * Missing data ≠ zero.  For every ``unresolved`` proposal we set:
        actual = None
        settlement_verified = False
        settlement_status = "unresolved"
        status = "unresolved"
        result = "unresolved"
    and REMOVE any stale ``authoritative_zero`` marker on the
    settlement detail.
  * Loss → win: persist previous/corrected result, actual, source,
    provider_event_id, matched_player_name, event/player confidence,
    reconciliation_trail, corrected_at.  Recalculate units_profit
    using the ORIGINAL ``book_odds`` (never the current market).
    ``$unset`` stale loss-only fields: ``failure_analysis``,
    ``why_lock_failed``, ``loss_narrative``,
    ``failure_generated_at``.  Do NOT regenerate post-mortems now.
  * Downstream: for every mutated pick, insert one
    ``db.reconciliation_downstream`` row so the next pass can
    deterministically rebuild parlays/rollovers/history/analytics.

Do NOT touch:
  * the 79 legacy synthetic ``book_odds`` rows
  * scoring/ranking/simulator/Lock Score/>85 gate
  * Magic Layer / Player History / Parlay engine
  * live_smoke tests
  * settlement pipeline code
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from motor.motor_asyncio import AsyncIOMotorClient

from analytics import american_profit_per_unit
from services.universal_settlement_contract import (
    RESULT_WON, RESULT_LOST, RESULT_UNRESOLVED,
)


REPORT_PATH = "/tmp/p04_historical_correction_report.json"


# ── Helpers ─────────────────────────────────────────────────────────
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sport_of(proposal: dict) -> str:
    return (proposal.get("sport") or "unknown")


def _market_family(m: Optional[str]) -> str:
    m = (m or "").lower()
    mn = m.replace(" ", "")
    if "hits+runs+rbi" in mn or "h+r+rbi" in mn: return "hits+runs+rbi"
    if "total bases" in m:                       return "total_bases"
    if "home run" in m:                          return "home_runs"
    if "strikeout" in m:                         return "strikeouts"
    if "pitcher outs" in m or "outs recorded" in m or " outs" in m:
        return "pitcher_outs"
    if "rbi" in m and "runs" not in m:           return "rbi"
    if "runs" in m and "rbi" not in m:           return "runs"
    if "hits" in m:                              return "hits"
    if "goal scorer" in m or "anytime" in m:     return "anytime_scorer"
    if ("score or assist" in m or "score & assist" in m
            or "score and assist" in m):        return "score_or_assist"
    if "assist" in m:                            return "assists"
    if "point" in m:                             return "points"
    if "rebound" in m:                           return "rebounds"
    return "other"


def _drift_key(pick: dict) -> tuple:
    """The (status, settlement_detail.value) tuple the proposal was
    generated against.  We require this to match before writing so
    that we never overwrite a pick a competing worker just updated."""
    return (
        pick.get("status"),
        ((pick.get("settlement_detail") or {}).get("value")),
    )


# ── Core writers ────────────────────────────────────────────────────
async def _apply_loss_to_win(db, proposal: dict) -> dict:
    pid   = proposal["pick_id"]
    sport = _sport_of(proposal)
    p = await db.picks.find_one({"id": pid})
    if p is None:
        return {"pick_id": pid, "sport": sport, "outcome": "not_found"}
    # Drift check: only apply if the live pick still matches the
    # reviewed (previous_result, previous_actual) tuple.
    prev_result_live, prev_actual_live = _drift_key(p)
    if prev_result_live != proposal["previous_result"] \
       or prev_actual_live != proposal["previous_actual"]:
        return {"pick_id": pid, "sport": sport, "outcome": "drift_skipped",
                 "live_status": prev_result_live,
                 "live_actual": prev_actual_live,
                 "proposal_previous_result": proposal["previous_result"],
                 "proposal_previous_actual": proposal["previous_actual"]}

    line     = proposal.get("line")
    side     = proposal.get("side")
    actual   = proposal["authoritative_actual"]
    market   = proposal.get("market") or p.get("market")
    stat_key = ((p.get("settlement_detail") or {}).get("stat")
                 or "").strip()
    now = _now_iso()

    # Recompute units_profit using the ORIGINAL book/pick odds — NEVER
    # substitute current market odds (spec §3).
    odds = (p.get("book_odds") if p.get("book_odds") is not None else
             p.get("odds_at_pick") if p.get("odds_at_pick") is not None else
             p.get("published_odds") if p.get("published_odds") is not None else
             p.get("closing_odds") if p.get("closing_odds") is not None else
             -110)
    units_risked = float(p.get("units_risked") or 1.0)
    base = american_profit_per_unit(odds, RESULT_WON)
    unit_profit = round(base * units_risked, 4)

    trail = {
        "phase":             "P0.4",
        "correction_reason": ("historical_settlement_reconciliation_"
                              "p04_verified"),
        "previous_actual":   proposal["previous_actual"],
        "previous_result":   proposal["previous_result"],
        "corrected_actual":  actual,
        "corrected_result":  RESULT_WON,
        "line":              line,
        "side":              side,
        "settlement_source": proposal["authoritative_source"],
        "provider_event_id": proposal["provider_event_id"],
        "matched_player_name": proposal["matched_player_name"],
        "event_match_confidence":  proposal["event_match_confidence"],
        "player_match_confidence": proposal["player_match_confidence"],
        "corrected_at":       now,
    }

    # Build final_score key from settlement_detail shape
    final_score_key = None
    fs = p.get("final_score") or {}
    if isinstance(fs, dict):
        for k in fs.keys():
            if k.lower() != "line":
                final_score_key = k
                break

    set_doc: dict[str, Any] = {
        "status":                RESULT_WON,
        "result":                RESULT_WON,
        "settlement_verified":   True,
        "settlement_source":     proposal["authoritative_source"],
        "settlement_verified_at": now,
        "settlement_detail.value":               actual,
        "settlement_detail.authoritative_zero": False,
        "units_profit":          unit_profit,
        "reconciliation_trail":  trail,
    }
    if final_score_key:
        set_doc[f"final_score.{final_score_key}"] = actual

    unset_doc = {
        "failure_analysis":       "",
        "why_lock_failed":        "",
        "loss_narrative":         "",
        "failure_generated_at":   "",
        "false_loss_post_mortem": "",
    }

    await db.picks.update_one({"id": pid},
                                {"$set": set_doc, "$unset": unset_doc})

    await db.reconciliation_downstream.update_one(
        {"pick_id": pid, "phase": "P0.4"},
        {"$set": {
            "pick_id":               pid,
            "phase":                 "P0.4",
            "flagged_at":            now,
            "reason":                "loss_to_win",
            "sport":                 sport,
            "market":                market,
            "market_family":         _market_family(market),
            "trail":                 trail,
            "downstream_dependencies": {
                "flag_parlay_legs":  True,
                "flag_rollovers":    True,
                "flag_tracked_bets": True,
                "flag_post_mortem":  True,
                "flag_learning":     True,
                "flag_calibration":  True,
                "flag_analytics":    True,
                "flag_history":      True,
                "flag_badges":       True,
            },
        }},
        upsert=True)

    return {"pick_id": pid, "sport": sport, "outcome": "loss_to_win",
             "corrected_actual": actual,
             "units_profit": unit_profit,
             "settlement_source": proposal["authoritative_source"]}


async def _apply_unresolved(db, proposal: dict) -> dict:
    pid   = proposal["pick_id"]
    sport = _sport_of(proposal)
    p = await db.picks.find_one({"id": pid})
    if p is None:
        return {"pick_id": pid, "sport": sport, "outcome": "not_found"}
    prev_result_live, prev_actual_live = _drift_key(p)
    if prev_result_live != proposal["previous_result"] \
       or prev_actual_live != proposal["previous_actual"]:
        return {"pick_id": pid, "sport": sport, "outcome": "drift_skipped",
                 "live_status": prev_result_live,
                 "live_actual": prev_actual_live,
                 "proposal_previous_result": proposal["previous_result"],
                 "proposal_previous_actual": proposal["previous_actual"]}

    market = proposal.get("market") or p.get("market")
    now = _now_iso()
    trail = {
        "phase":              "P0.4",
        "correction_reason":  "historical_settlement_reconciliation_"
                              "p04_unresolved",
        "previous_actual":    proposal["previous_actual"],
        "previous_result":    proposal["previous_result"],
        "corrected_actual":   None,
        "corrected_result":   RESULT_UNRESOLVED,
        "line":               proposal.get("line"),
        "side":               proposal.get("side"),
        "settlement_source":  proposal["authoritative_source"],
        "provider_event_id":  proposal.get("provider_event_id"),
        "matched_player_name": proposal.get("matched_player_name"),
        "unresolved_bucket":  proposal.get("unresolved_bucket"),
        "correction_reason_detail": proposal.get("correction_reason"),
        "event_match_confidence":  proposal.get("event_match_confidence"),
        "player_match_confidence": proposal.get("player_match_confidence"),
        "corrected_at":       now,
    }

    # For unresolved rows we no longer credit or debit a unit.  The
    # book has no P/L position on the pick, so units_profit → 0.0.
    # Note: this may need re-visited by the downstream ROI pass.
    unset_doc = {
        "failure_analysis":       "",
        "why_lock_failed":        "",
        "loss_narrative":         "",
        "failure_generated_at":   "",
        "false_loss_post_mortem": "",
    }
    set_doc: dict[str, Any] = {
        "status":                RESULT_UNRESOLVED,
        "result":                RESULT_UNRESOLVED,
        "settlement_status":     "unresolved",
        "settlement_verified":   False,
        "settlement_source":     proposal["authoritative_source"],
        "settlement_verified_at": now,
        "settlement_detail.value":               None,
        "settlement_detail.authoritative_zero": False,
        "units_profit":          0.0,
        "reconciliation_trail":  trail,
    }
    await db.picks.update_one({"id": pid},
                                {"$set": set_doc, "$unset": unset_doc})

    await db.reconciliation_downstream.update_one(
        {"pick_id": pid, "phase": "P0.4"},
        {"$set": {
            "pick_id":              pid,
            "phase":                "P0.4",
            "flagged_at":           now,
            "reason":               "unresolved",
            "unresolved_bucket":    proposal.get("unresolved_bucket"),
            "sport":                sport,
            "market":               market,
            "market_family":        _market_family(market),
            "trail":                trail,
            "downstream_dependencies": {
                "flag_parlay_legs":  True,
                "flag_rollovers":    True,
                "flag_tracked_bets": True,
                "flag_post_mortem":  True,
                "flag_learning":     True,
                "flag_calibration":  True,
                "flag_analytics":    True,
                "flag_history":      True,
                "flag_badges":       True,
            },
        }},
        upsert=True)

    return {"pick_id": pid, "sport": sport, "outcome": "to_unresolved",
             "unresolved_bucket": proposal.get("unresolved_bucket"),
             "settlement_source": proposal["authoritative_source"]}


# ── Entry point ─────────────────────────────────────────────────────
async def _run(db, *, dry_run: bool, report_path: str,
                confirm: str) -> dict:
    with open(report_path) as fh:
        report = json.load(fh)
    proposals = report["proposals"]

    lw_proposals = [p for p in proposals
                     if p["previous_result"] == "lost"
                     and p["proposed_result"] == "won"]
    ur_proposals = [p for p in proposals
                     if p["proposed_result"] == "unresolved"]

    if len(lw_proposals) != 152 or len(ur_proposals) != 79:
        return {"error": "population_mismatch",
                 "expected_lw": 152, "actual_lw": len(lw_proposals),
                 "expected_ur": 79,  "actual_ur": len(ur_proposals)}

    if confirm != "APPLY_P04_WRITE_PASS" and not dry_run:
        return {"error": "confirm_flag_missing",
                 "hint": "Pass --confirm APPLY_P04_WRITE_PASS to run writes."}

    results: list[dict] = []
    outcome_counts: dict[str, int] = Counter()
    per_sport_family: dict[str, Counter] = defaultdict(Counter)
    per_sport_gt85: dict[str, Counter] = defaultdict(Counter)
    drift_rows: list[dict] = []

    for pr in lw_proposals:
        if dry_run:
            outcome_counts["would_loss_to_win"] += 1
            continue
        r = await _apply_loss_to_win(db, pr)
        results.append(r)
        outcome_counts[r["outcome"]] += 1
        if r["outcome"] == "drift_skipped":
            drift_rows.append(r)
        elif r["outcome"] == "loss_to_win":
            fam = _market_family(pr["market"])
            per_sport_family[pr["sport"]][f"loss_to_win::{fam}"] += 1
            try:
                if float(pr.get("published_lock_score") or 0) > 85:
                    per_sport_gt85[pr["sport"]][f"loss_to_win::{fam}"] += 1
            except (TypeError, ValueError):
                pass

    for pr in ur_proposals:
        if dry_run:
            outcome_counts["would_unresolved"] += 1
            continue
        r = await _apply_unresolved(db, pr)
        results.append(r)
        outcome_counts[r["outcome"]] += 1
        if r["outcome"] == "drift_skipped":
            drift_rows.append(r)
        elif r["outcome"] == "to_unresolved":
            fam = _market_family(pr["market"])
            per_sport_family[pr["sport"]][f"unresolved::{fam}"] += 1
            try:
                if float(pr.get("published_lock_score") or 0) > 85:
                    per_sport_gt85[pr["sport"]][f"unresolved::{fam}"] += 1
            except (TypeError, ValueError):
                pass

    return {
        "phase":                    "P0.4",
        "mode":                     ("DRY_RUN" if dry_run else "APPLIED"),
        "generated_at":             _now_iso(),
        "review_report":            report_path,
        "review_report_generated":  report.get("generated_at"),
        "population_scanned":       len(proposals),
        "population_expected":      {"loss_to_win": 152, "unresolved": 79},
        "outcome_counts":           dict(outcome_counts),
        "per_sport_family":         {s: dict(c)
                                       for s, c in per_sport_family.items()},
        "per_sport_gt85":           {s: dict(c)
                                       for s, c in per_sport_gt85.items()},
        "drift_skipped":            drift_rows,
    }


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", default=False)
    ap.add_argument("--report", type=str, default=REPORT_PATH)
    ap.add_argument("--confirm", type=str, default="")
    ap.add_argument("--output",  type=str,
                     default="/tmp/p04_write_pass_result.json")
    args = ap.parse_args()

    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = client[os.environ.get("DB_NAME", "perkslocks_production")]

    result = await _run(db, dry_run=args.dry_run,
                         report_path=args.report,
                         confirm=args.confirm)

    with open(args.output, "w") as fh:
        json.dump(result, fh, indent=2, default=str)

    print("=" * 78)
    print(f"P0.4 WRITE-PASS — mode={result.get('mode')}")
    print("=" * 78)
    print(json.dumps(
        {k: v for k, v in result.items()
          if k not in ("drift_skipped",)},
        indent=2, default=str))
    if result.get("drift_skipped"):
        print(f"\n[!] {len(result['drift_skipped'])} drift-skipped rows — "
               "see output file for details.")
    print(f"\n[report] {args.output}")
    client.close()


if __name__ == "__main__":
    asyncio.run(main())


__all__ = ["_run", "_apply_loss_to_win", "_apply_unresolved"]
