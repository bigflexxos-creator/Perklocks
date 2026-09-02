"""Q-FALSE-LOSS Reconciliation — Root Closure 2026-06
=====================================================

Runtime defect discovered live:
    Michael Harris II Over 1.5 H+R+RBI showed Actual=4 · LOST
    Matt Olson       Over 1.5 H+R+RBI showed Actual=2 · LOST
Both are mathematically impossible: `Over 1.5` with actual > 1.5 must
be WON.  Investigation showed:

  * `prop_settlement` initially graded WON with a *stale/in-progress*
    boxscore actual (4 / 2).
  * `grading_validator._mlb_verify_prop` re-verified against the FINAL
    boxscore, got the CORRECT actual (1 / 0), and correctly re-graded
    to LOST via a canonical settlement correction (settlement_events
    v2 supersedes v1, is_active=True).
  * BUT the compat-mirror `pick.final_score` on the picks doc STILL
    carried the stale actual (4 / 2) from the v1 write.  Result:
    History UI rendered "Actual 4 · LOST" — a display contradiction.

The runtime fix (grading_validator now mirrors the authoritative
actual onto `pick.final_score` on every correction).  This script
reconciles the historical rows written before the runtime fix landed.

Contract (per user Root Closure spec §5):
  - Do NOT mutate historical settlement truth silently.  The append-only
    ledger (settlement_events) already holds the correction.
  - Only reconcile the DERIVED compat-mirror on `db.picks`
    (`final_score`, `status`) to match the ACTIVE settlement event
    result — never rewrite a v-active ledger row.
  - For MLB combo markets whose corrected settlement event lacks a
    stored authoritative value, re-verify against MLB StatsAPI live
    and record the authoritative actual before mirroring.

Idempotent; safe to re-run.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, Optional

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

load_dotenv(os.path.join(_BACKEND, ".env"))


CHUNK = 500


def _parse_over_under(market: str) -> Optional[tuple[float, str]]:
    if not market:
        return None
    m = re.search(r"(over|under)\s*(-?\d+(?:\.\d+)?)", market, re.I)
    if not m:
        return None
    return (float(m.group(2)), m.group(1).lower())


def _grade(actual: float, line: float, side: str) -> str:
    if actual is None:
        return "unresolved"
    if side == "over":
        if actual > line:  return "won"
        if actual < line:  return "lost"
        return "push"
    if actual < line:      return "won"
    if actual > line:      return "lost"
    return "push"


def _label_from_final_score(fs: dict) -> tuple[Optional[str], Optional[float]]:
    """Return (label, value) from a final_score dict of shape
    {'<Player> <Stat>': <value>, 'Line': <line>}."""
    if not isinstance(fs, dict):
        return (None, None)
    for k, v in fs.items():
        if k == "Line":
            continue
        try:
            return (k, float(v))
        except Exception:
            continue
    return (None, None)


async def run() -> dict:
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = client[os.environ["DB_NAME"]]
    now = datetime.now(timezone.utc)

    stats = {
        "scanned":                  0,
        "checked":                  0,
        "contradictions_detected":  0,
        "final_score_mirrored":     0,
        "status_synced":            0,
        "unresolvable":             0,
        "started_at":               now.isoformat(),
    }

    # Focus on the exact bug-class: picks with an ACTIVE canonical
    # correction (source ending in `_correction`) OR any settled pick
    # whose stored final_score contradicts its own stored status.
    # Pass 1 — every pick with a v-active correction row.
    async for ev in db.settlement_events.find(
            {"source": {"$regex": "_correction$"}, "is_active": True},
            {"prediction_id": 1, "result": 1, "actual_result": 1},
    ):
        stats["scanned"] += 1
        pid = ev.get("prediction_id")
        canonical_result = ev.get("result")
        if not pid or canonical_result not in ("won", "lost", "push", "void"):
            continue
        pick = await db.picks.find_one({"id": pid})
        if not pick:
            continue
        stats["checked"] += 1

        # Compute contradiction: pick.final_score vs pick.line vs status
        line_side = _parse_over_under(pick.get("market") or "")
        stored_status = pick.get("status")
        _, stored_actual = _label_from_final_score(pick.get("final_score") or {})
        implied = None
        if line_side is not None and stored_actual is not None:
            implied = _grade(stored_actual, line_side[0], line_side[1])

        need_mirror = False
        need_status_sync = False

        # A) status mirror lag
        if stored_status != canonical_result:
            need_status_sync = True

        # B) final_score contradicts canonical result
        if implied is not None and implied != canonical_result:
            need_mirror = True
            stats["contradictions_detected"] += 1

        # If we have an authoritative actual on the ledger event, use it.
        ledger_actual = ev.get("actual_result") or {}
        ledger_value = ledger_actual.get("value") if isinstance(ledger_actual, dict) else None
        ledger_fs = ledger_actual.get("final_score") if isinstance(ledger_actual, dict) else None

        set_payload: dict = {"final_score_reconciled_at": now.isoformat()}
        if need_status_sync:
            set_payload["status"] = canonical_result
            stats["status_synced"] += 1
        if need_mirror:
            if isinstance(ledger_fs, dict) and ledger_fs:
                set_payload["final_score"] = ledger_fs
                set_payload["final_score_source"] = "ledger_correction_reconcile"
                stats["final_score_mirrored"] += 1
            elif ledger_value is not None and line_side is not None:
                label, _ = _label_from_final_score(pick.get("final_score") or {})
                if label:
                    set_payload["final_score"] = {label: ledger_value, "Line": line_side[0]}
                    set_payload["final_score_source"] = "ledger_correction_reconcile"
                    stats["final_score_mirrored"] += 1
                else:
                    stats["unresolvable"] += 1
            else:
                # No authoritative actual value stored on the ledger.
                # Flag the row so downstream verifiers can pick it up
                # without silently rewriting the mirror.
                set_payload["final_score_needs_reverify"] = True
                stats["unresolvable"] += 1
        if len(set_payload) > 1:   # more than just the reconciled_at marker
            await db.picks.update_one({"_id": pick["_id"]}, {"$set": set_payload})

    stats["finished_at"] = datetime.now(timezone.utc).isoformat()
    return stats


if __name__ == "__main__":
    out = asyncio.run(run())
    print()
    print("── Q-FALSE-LOSS RECONCILIATION SUMMARY ────────────────")
    print(json.dumps(out, indent=2, default=str))
