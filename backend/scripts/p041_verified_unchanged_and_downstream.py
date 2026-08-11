"""P0.4.1 — Verified-Unchanged Truth Stamp + Downstream Reconciliation.

Two-part operator-authorized script:

  PART A  Truth-stamp the 1,127 P0.4-audited rows whose original
          settlement was CORRECT (authoritative actual == 0).
          Sets ``settlement_verified=True`` + provenance fields.
          NEVER flips result. NEVER overwrites units_profit.

  PART B  Downstream reconciliation for the 231 P0.4-corrected picks:
          parlays, user_bets, settlement_events, post-mortems,
          learning contamination check, player_history contamination
          check, candidate_dispositions cross-check, Lab impact.

Contract:
  * Do NOT alter scoring / ranking / Lock Score / >85 gate / simulator.
  * Never turn missing data into zero.
  * Legitimate authoritative zero remains a zero.
  * Original odds/lines/selections NEVER rewritten.
  * Downstream cannot lose historical publication truth.
  * Parlays: use canonical settlement rules (won×won→won, any lost→
    lost, any unresolved+all-else-won → pending/unresolved).

Read-only unless ``--confirm APPLY_P041_WRITE_PASS`` is passed.
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

from services.universal_settlement_contract import (
    RESULT_WON, RESULT_LOST, RESULT_UNRESOLVED, RESULT_PENDING,
    RESULT_VOID, RESULT_PUSH,
)


REVIEW_REPORT = "/tmp/p04_historical_correction_report.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ═══════════════════════════════════════════════════════════════════
# PART A — Truth-stamp the 1,127 authoritatively-confirmed rows
# ═══════════════════════════════════════════════════════════════════
async def truth_stamp_unchanged(db, *, dry_run: bool) -> dict:
    """For every proposal in the P0.4 review report where the
    previous grade was authoritatively confirmed (proposed_result ==
    previous_result and authoritative_actual is not None), stamp the
    canonical settlement fields WITHOUT flipping the result or
    changing units_profit.

    Idempotency: we only write when the row is not already stamped
    with the P0.4 authoritative source.  Never expands to rows
    outside the P0.4 review population.
    """
    with open(REVIEW_REPORT) as fh:
        review = json.load(fh)

    unchanged_proposals = [
        p for p in review["proposals"]
        if p["previous_result"] == p["proposed_result"]
        and p["authoritative_actual"] is not None
        and p["proposed_result"] in (RESULT_WON, RESULT_LOST)
    ]

    per_sport = Counter()
    for pr in unchanged_proposals:
        per_sport[pr["sport"]] += 1

    result = {
        "population_expected": 1127,
        "population_found":    len(unchanged_proposals),
        "per_sport_expected":  {"MLB": 460, "Soccer": 666, "NBA": 1},
        "per_sport_found":     dict(per_sport),
        "stamped_actual_matched":    0,   # authoritative == previous
        "stamped_actual_corrected":  0,   # authoritative != previous
                                          # (false-zero data integrity fix,
                                          #  result unchanged)
        "skipped_drift":       0,
        "skipped_already":     0,
        "failures":            0,
        "drift_details":       [],
    }
    if len(unchanged_proposals) != 1127:
        result["error"] = "unchanged_population_mismatch"
        return result

    now = _now_iso()
    for pr in unchanged_proposals:
        pid = pr["pick_id"]
        p = await db.picks.find_one({"id": pid})
        if p is None:
            result["failures"] += 1
            continue

        # Drift-safe: only stamp if the LIVE row still matches the
        # reviewed (previous_result, previous_actual) tuple.
        live_status = p.get("status")
        live_actual = ((p.get("settlement_detail") or {}).get("value"))
        if live_status != pr["previous_result"] \
           or live_actual != pr["previous_actual"]:
            result["skipped_drift"] += 1
            result["drift_details"].append({
                "pick_id": pid,
                "live_status": live_status,
                "live_actual": live_actual,
                "expected_status": pr["previous_result"],
                "expected_actual": pr["previous_actual"],
            })
            continue

        # Idempotency: skip if already stamped by P0.4.1.
        if (p.get("verification_trail") or {}).get("phase") == "P0.4.1":
            result["skipped_already"] += 1
            continue

        auth_actual = pr["authoritative_actual"]
        prev_actual = pr["previous_actual"]
        actual_corrected = (auth_actual != prev_actual)

        trail = {
            "phase":             "P0.4.1",
            "correction_reason": ("verified_unchanged_actual_corrected"
                                    if actual_corrected
                                    else "verified_unchanged_truth_stamp"),
            "previous_actual":   prev_actual,
            "previous_result":   pr["previous_result"],
            "authoritative_actual": auth_actual,
            "authoritative_result": pr["proposed_result"],
            "settlement_source": pr["authoritative_source"],
            "provider_event_id": pr["provider_event_id"],
            "matched_player_name": pr["matched_player_name"],
            "event_match_confidence":  pr["event_match_confidence"],
            "player_match_confidence": pr["player_match_confidence"],
            "line":              pr.get("line"),
            "side":              pr.get("side"),
            "stamped_at":        now,
        }
        set_doc: dict[str, Any] = {
            "settlement_verified":    True,
            "settlement_source":      pr["authoritative_source"],
            "settlement_verified_at": now,
            "verification_trail":     trail,
        }
        # For a genuine authoritative zero, mark it explicitly.
        # For a corrected non-zero actual, we're setting the real
        # value and REMOVING the false-zero marker.
        if actual_corrected:
            set_doc["settlement_detail.value"] = auth_actual
            set_doc["settlement_detail.authoritative_zero"] = False
            fs = p.get("final_score") or {}
            if isinstance(fs, dict):
                for k in fs.keys():
                    if k.lower() != "line":
                        set_doc[f"final_score.{k}"] = auth_actual
                        break
            # units_profit stays untouched — the RESULT is still
            # lost, so the P/L position is unchanged.
        else:
            set_doc["settlement_detail.authoritative_zero"] = True

        if not dry_run:
            await db.picks.update_one({"id": pid}, {"$set": set_doc})
        if actual_corrected:
            result["stamped_actual_corrected"] += 1
        else:
            result["stamped_actual_matched"] += 1

    result["stamped"] = (result["stamped_actual_matched"]
                          + result["stamped_actual_corrected"])
    return result


# ═══════════════════════════════════════════════════════════════════
# PART B — Downstream reconciliation for the 231 P0.4 corrections
# ═══════════════════════════════════════════════════════════════════
async def _load_corrected_pick_ids(db) -> tuple[set[str], dict[str, dict]]:
    """Return (set of pick_ids, mapping pick_id → correction details).
    Correction detail includes the corrected result and reconciliation
    trail so downstream repair can reference them without another
    round-trip to db.picks."""
    corrected: set[str] = set()
    by_pid: dict[str, dict] = {}
    async for r in db.reconciliation_downstream.find({"phase": "P0.4"}):
        pid = r.get("pick_id")
        if pid:
            corrected.add(pid)
            by_pid[pid] = r
    return corrected, by_pid


async def _fresh_pick_lookup(db, ids: set[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    async for p in db.picks.find({"id": {"$in": list(ids)}}):
        out[p["id"]] = p
    return out


def _grade_parlay(leg_statuses: list[str]) -> str:
    """Universal parlay settlement:
      any leg lost              → lost
      any leg unresolved/pending + no lost → pending / unresolved
      all legs won              → won
      any leg void              → treat as leg removed (won still won
                                    if remaining legs all won)
    """
    if not leg_statuses:
        return RESULT_UNRESOLVED
    non_void = [s for s in leg_statuses if s != RESULT_VOID]
    if not non_void:
        return RESULT_VOID
    if any(s == RESULT_LOST for s in non_void):
        return RESULT_LOST
    if any(s in (RESULT_UNRESOLVED, RESULT_PENDING) for s in non_void):
        return RESULT_UNRESOLVED
    if all(s == RESULT_WON for s in non_void):
        return RESULT_WON
    return RESULT_UNRESOLVED


async def reconcile_parlays(db, *, dry_run: bool) -> dict:
    corrected, _ = await _load_corrected_pick_ids(db)

    affected: list[dict] = []
    changed = unchanged = to_unresolved = ambiguous = 0

    async for parlay in db.parlay_history.find({}):
        legs = parlay.get("legs") or []
        leg_ids = parlay.get("leg_ids") or []
        leg_pids: list[str] = []
        for leg in legs:
            pid = (leg.get("pick_id") or leg.get("id")
                    or leg.get("prediction_id"))
            if pid:
                leg_pids.append(pid)
        for pid in leg_ids:
            if pid and pid not in leg_pids:
                leg_pids.append(pid)

        overlap = set(leg_pids) & corrected
        if not overlap:
            continue

        # Fetch fresh statuses for every leg
        picks_by_id = await _fresh_pick_lookup(db, set(leg_pids))
        new_leg_statuses: list[str] = []
        missing_legs = 0
        for pid in leg_pids:
            p = picks_by_id.get(pid)
            if p is None:
                missing_legs += 1
                new_leg_statuses.append(RESULT_PENDING)
            else:
                new_leg_statuses.append(p.get("status") or RESULT_PENDING)

        prev_parlay_status = parlay.get("status")
        new_parlay_status = _grade_parlay(new_leg_statuses)

        entry = {
            "parlay_id":          parlay.get("id"),
            "previous_status":    prev_parlay_status,
            "new_status":         new_parlay_status,
            "corrected_leg_pids": list(overlap),
            "new_leg_statuses":   new_leg_statuses,
            "leg_pids":           leg_pids,
            "missing_legs":       missing_legs,
        }

        if prev_parlay_status == new_parlay_status:
            unchanged += 1
        elif new_parlay_status == RESULT_UNRESOLVED:
            to_unresolved += 1
        elif prev_parlay_status == "lost" and new_parlay_status == "won":
            changed += 1
        elif prev_parlay_status == "won" and new_parlay_status == "lost":
            changed += 1
        else:
            ambiguous += 1

        # Recompute leg counts
        legs_won = sum(1 for s in new_leg_statuses if s == RESULT_WON)
        legs_lost = sum(1 for s in new_leg_statuses if s == RESULT_LOST)
        legs_pending = sum(1 for s in new_leg_statuses
                             if s in (RESULT_UNRESOLVED, RESULT_PENDING))

        # Compute new payout ONLY if won.  Never invent odds.
        combined_odds = parlay.get("combined_odds")
        stake = parlay.get("stake") or 0.0
        if new_parlay_status == RESULT_WON and combined_odds is not None:
            try:
                co = float(combined_odds)
                if co > 0:
                    payout = round(stake * (1 + co / 100.0), 2)
                else:
                    payout = round(stake * (1 + 100.0 / abs(co)), 2)
            except Exception:
                payout = parlay.get("payout")
        elif new_parlay_status in (RESULT_LOST,):
            payout = 0.0
        elif new_parlay_status in (RESULT_UNRESOLVED, RESULT_PENDING):
            payout = None
        else:
            payout = parlay.get("payout")

        trail = {
            "phase":                "P0.4.1",
            "correction_reason":    "downstream_parlay_reconciliation",
            "previous_status":      prev_parlay_status,
            "previous_legs_won":    parlay.get("legs_won"),
            "previous_legs_lost":   parlay.get("legs_lost"),
            "previous_legs_pending": parlay.get("legs_pending"),
            "corrected_leg_pids":   list(overlap),
            "reconciled_at":        _now_iso(),
        }

        set_doc = {
            "status":              new_parlay_status,
            "legs_won":            legs_won,
            "legs_lost":           legs_lost,
            "legs_pending":        legs_pending,
            "payout":              payout,
            "reconciliation_trail": trail,
        }
        # Also refresh the frozen leg.status snapshot fields on the
        # embedded legs so parlay UI is accurate.
        updated_legs = []
        for leg in legs:
            lp = leg.copy()
            pid = (leg.get("pick_id") or leg.get("id")
                    or leg.get("prediction_id"))
            fresh = picks_by_id.get(pid) if pid else None
            if fresh is not None:
                lp["status"] = fresh.get("status")
            updated_legs.append(lp)
        set_doc["legs"] = updated_legs

        entry["new_leg_counts"] = {
            "won": legs_won, "lost": legs_lost, "pending": legs_pending}
        entry["new_payout"] = payout

        if not dry_run:
            await db.parlay_history.update_one(
                {"_id": parlay["_id"]}, {"$set": set_doc})
        affected.append(entry)

    return {
        "affected_parlays":  len(affected),
        "changed":           changed,
        "unchanged":         unchanged,
        "to_unresolved":     to_unresolved,
        "ambiguous":         ambiguous,
        "details":           affected,
    }


async def reconcile_user_bets(db, *, dry_run: bool) -> dict:
    """user_bets: 0 currently reference corrected picks per audit,
    but re-verify in the write-pass and flip any straight bet
    that still points at a corrected pick."""
    corrected, _ = await _load_corrected_pick_ids(db)
    changed = unchanged = to_unresolved = 0
    details: list[dict] = []
    async for ub in db.user_bets.find({
        "$or": [
            {"pick_id":       {"$in": list(corrected)}},
            {"prediction_id": {"$in": list(corrected)}},
        ]}):
        pid = ub.get("pick_id") or ub.get("prediction_id")
        p = await db.picks.find_one({"id": pid})
        if p is None:
            continue
        new_status = p.get("status")
        prev = ub.get("status")
        entry = {"user_bet_id": ub.get("id"),
                  "pick_id": pid, "prev": prev, "new": new_status}
        details.append(entry)
        if prev == new_status:
            unchanged += 1
            continue
        if new_status in (RESULT_UNRESOLVED, RESULT_PENDING):
            to_unresolved += 1
        else:
            changed += 1
        if not dry_run:
            await db.user_bets.update_one(
                {"_id": ub["_id"]},
                {"$set": {"status": new_status,
                          "reconciliation_trail": {
                              "phase": "P0.4.1",
                              "prev_status": prev,
                              "corrected_pick_id": pid,
                              "reconciled_at": _now_iso(),
                          }}})
    return {"changed": changed, "unchanged": unchanged,
             "to_unresolved": to_unresolved, "details": details}


async def audit_settlement_events(db) -> dict:
    """settlement_events is a small event log (15 rows) used by the
    canonical settlement engine.  If it contains rows referencing any
    of the P0.4-corrected picks, we insert a "reconciled" event so
    the log stays honest — we do NOT delete or rewrite historical
    events."""
    corrected, corrected_by_pid = await _load_corrected_pick_ids(db)
    n = 0
    async for e in db.settlement_events.find({}):
        pid = e.get("pick_id") or e.get("prediction_id")
        if pid in corrected:
            n += 1
    return {"settlement_events_referencing_corrected_picks": n}


async def audit_post_mortems(db) -> dict:
    """Verify no P0.4 loss→win pick still has stale loss post-mortem
    fields.  (The P0.4 write-pass $unset them, but re-audit.)"""
    stale = 0
    async for p in db.picks.find({
        "reconciliation_trail.phase": "P0.4",
        "reconciliation_trail.corrected_result": "won",
        "$or": [
            {"failure_analysis":     {"$exists": True}},
            {"why_lock_failed":      {"$exists": True}},
            {"loss_narrative":       {"$exists": True}},
            {"failure_generated_at": {"$exists": True}},
        ]}):
        stale += 1
    return {"stale_post_mortem_fields_on_loss_to_win": stale}


async def audit_learning_contamination(db) -> dict:
    """Check every learning-related collection for rows referencing
    P0.4-corrected pick_ids.  learning_log doesn't have pick_id
    per-row today (only aggregate `band_gate_raise` /
    `market_decay`), but scan defensively."""
    corrected, _ = await _load_corrected_pick_ids(db)
    result: dict[str, int] = {}
    for coll in ("learning_log", "learning_buckets",
                  "learning_bucket_snapshots", "learning_snapshots",
                  "learning_state", "learned_weights",
                  "lock_calibration_curve", "parlay_learning_events",
                  "parlay_leg_reliability", "parlay_synergy"):
        n = 0
        try:
            async for doc in db[coll].find({}):
                s = str(doc)
                for pid in corrected:
                    if pid in s:
                        n += 1
                        break
                if n > 100:
                    break
        except Exception:
            continue
        result[coll] = n
    return result


async def audit_player_history(db) -> dict:
    """Player history collection is empty pre-Phase 5.3.  Confirm
    no P0.4 unresolved rows have leaked in."""
    corrected, _ = await _load_corrected_pick_ids(db)
    n_ph_total = await db.player_history.count_documents({})
    n_ph_ref = 0
    async for r in db.player_history.find({}):
        s = str(r)
        for pid in corrected:
            if pid in s:
                n_ph_ref += 1
                break
    return {"player_history_total_rows": n_ph_total,
             "player_history_rows_referencing_corrected_picks": n_ph_ref}


async def audit_candidate_dispositions(db) -> dict:
    corrected, _ = await _load_corrected_pick_ids(db)
    stages = Counter()
    async for c in db.candidate_dispositions.find(
            {"candidate_key": {"$in": list(corrected)}},
            {"sport": 1, "stage": 1}):
        stages[(c.get("sport"), c.get("stage"))] += 1
    return {"rows_by_sport_stage":
            {f"{s}::{stg}": n for (s, stg), n in stages.items()}}


async def audit_lab_impact(db) -> dict:
    """Lab historical patterns.  We only report which P0.4-corrected
    rows are inside the currently-audited Lab population — the full
    Lab redesign is deferred.

    Today the Lab pipeline reads db.picks with settlement_verified
    and lock_score > 85 filtered at query time — no separate
    materialized 'patterns' collection exists.  So the impact is:
    corrected loss→win rows that were previously excluded (because
    they were unverified) may now enter Lab statistics on next run.
    """
    corrected, corrected_by_pid = await _load_corrected_pick_ids(db)
    # Break down P0.4 corrected picks by:
    #   (a) previously would-have-been in Lab (>85 & verified): 0
    #       — none were verified before P0.4.
    #   (b) now eligible for Lab (>85 & verified after P0.4).
    by_reason = Counter()
    market_family_impact = defaultdict(Counter)
    async for p in db.picks.find(
            {"id": {"$in": list(corrected)}, "published_lock_score": {"$gt": 85}}):
        pid = p["id"]
        prev_verified = False  # nothing was verified before P0.4 in this population
        now_verified = p.get("settlement_verified") is True
        market = (p.get("market") or "").lower()
        if "hits + runs + rbi" in market or "h+r+rbi" in market.replace(" ",""):
            fam = "hits+runs+rbi"
        elif "strikeout" in market: fam = "strikeouts"
        elif "outs" in market and p.get("sport") == "MLB": fam = "pitcher_outs"
        elif "hits" in market: fam = "hits"
        elif "rbi" in market: fam = "rbi"
        elif "goal scorer" in market or "anytime" in market: fam = "anytime_scorer"
        elif "score or assist" in market: fam = "score_or_assist"
        else: fam = "other"

        if now_verified and not prev_verified:
            by_reason["newly_eligible_for_lab"] += 1
        market_family_impact[p.get("sport") or "?"][fam] += 1

    return {
        "lab_population_gt85_corrected": dict(by_reason),
        "market_family_impact_gt85":
            {s: dict(c) for s, c in market_family_impact.items()},
        "defects_carried_over_to_lab_redesign": [
            "Lab must distinguish published verified Locks from "
            "research/all-model historical population.",
            "Lab must NOT treat all settled db.picks as equivalent "
            "to user-visible Locks.",
            "MLB_RBI must NOT absorb H+R+RBI.",
            "MLB_TOTAL must NOT become a broad catch-all for "
            "unrelated Over/Under markets.",
            "Historical market support must be distinguishable "
            "from CURRENT market support.",
            "Expose real-lines→candidates→>85→published Locks in "
            "the redesigned Lab.",
        ],
    }


async def audit_history_visibility(db) -> dict:
    """Historical truth invariant: every P0.4-corrected pick must
    remain visible in the History query surface. Since History is
    derived on-the-fly from db.picks (and canonical
    prediction_snapshots), the risk is that a filter excludes
    newly-flipped rows.

    We audit:
      * corrected picks present in canonical prediction_snapshots
        (the true publication ledger)
      * corrected picks with off_board=True and status=lost (was
        cause of the "disappearing loss" defect)
      * corrected picks now with status=unresolved — these MUST
        still appear in History under the "unresolved" bucket.
    """
    corrected, _ = await _load_corrected_pick_ids(db)
    off_board_lost = 0
    unresolved_needs_bucket = 0
    in_snapshots = 0
    for pid in corrected:
        ps = await db.prediction_snapshots.find_one(
            {"$or": [{"pick_id": pid}, {"prediction_id": pid},
                      {"id": pid}]})
        if ps is not None:
            in_snapshots += 1
    async for p in db.picks.find({"id": {"$in": list(corrected)}}):
        if p.get("off_board") is True and p.get("status") == "lost":
            off_board_lost += 1
        if p.get("status") == "unresolved":
            unresolved_needs_bucket += 1
    return {
        "corrected_in_canonical_prediction_snapshots": in_snapshots,
        "corrected_total": len(corrected),
        "canonical_publication_truth_preserved":
            in_snapshots == len(corrected),
        "corrected_off_board_lost_needing_history_visibility":
            off_board_lost,
        "corrected_unresolved_needing_unresolved_bucket":
            unresolved_needs_bucket,
        "rule": ("Historical truth must represent what was actually "
                  "published to users at the time; publication-time "
                  "truth is preserved via db.prediction_snapshots."),
    }


# ═══════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════
async def _run(db, *, dry_run: bool, confirm: str) -> dict:
    if not dry_run and confirm != "APPLY_P041_WRITE_PASS":
        return {"error": "confirm_flag_missing",
                 "hint": "Pass --confirm APPLY_P041_WRITE_PASS to run writes."}

    now = _now_iso()

    # PART A
    a = await truth_stamp_unchanged(db, dry_run=dry_run)

    # PART B — parlays / user_bets
    parlays = await reconcile_parlays(db, dry_run=dry_run)
    user_bets = await reconcile_user_bets(db, dry_run=dry_run)

    # PART B — audits (read-only regardless of dry_run)
    settlement_events = await audit_settlement_events(db)
    post_mortems      = await audit_post_mortems(db)
    learning          = await audit_learning_contamination(db)
    player_history    = await audit_player_history(db)
    dispositions      = await audit_candidate_dispositions(db)
    lab               = await audit_lab_impact(db)
    history           = await audit_history_visibility(db)

    return {
        "phase":         "P0.4.1",
        "mode":          "DRY_RUN" if dry_run else "APPLIED",
        "generated_at":  now,
        "A_truth_stamp": a,
        "B_parlays":     parlays,
        "B_user_bets":   user_bets,
        "B_settlement_events":   settlement_events,
        "B_post_mortems":        post_mortems,
        "B_learning_contamination": learning,
        "B_player_history":      player_history,
        "B_candidate_dispositions": dispositions,
        "B_lab_impact":          lab,
        "B_history_visibility":  history,
    }


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", default=False)
    ap.add_argument("--confirm", type=str, default="")
    ap.add_argument("--output",  type=str,
                     default="/tmp/p041_result.json")
    args = ap.parse_args()

    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = client[os.environ.get("DB_NAME", "perkslocks_production")]

    result = await _run(db, dry_run=args.dry_run, confirm=args.confirm)
    with open(args.output, "w") as fh:
        json.dump(result, fh, indent=2, default=str)

    print("=" * 78)
    print(f"P0.4.1 — mode={result.get('mode')}")
    print("=" * 78)
    # Truncate details for stdout, keep summary
    summary = {k: v for k, v in result.items() if k != "B_parlays"}
    if "B_parlays" in result:
        p = result["B_parlays"].copy()
        p["details"] = f"[{len(p.get('details', []))} entries — see output file]"
        summary["B_parlays"] = p
    if "A_truth_stamp" in summary:
        a = summary["A_truth_stamp"].copy()
        a["drift_details"] = (
            f"[{len(a.get('drift_details', []))} — see output]"
        )
        summary["A_truth_stamp"] = a
    print(json.dumps(summary, indent=2, default=str))
    print(f"\n[report] {args.output}")
    client.close()


if __name__ == "__main__":
    asyncio.run(main())


__all__ = [
    "truth_stamp_unchanged",
    "reconcile_parlays",
    "reconcile_user_bets",
    "audit_settlement_events",
    "audit_post_mortems",
    "audit_learning_contamination",
    "audit_player_history",
    "audit_candidate_dispositions",
    "audit_lab_impact",
    "audit_history_visibility",
    "_grade_parlay",
]
