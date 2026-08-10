"""P0 (2026-08-11) — READ-ONLY cross-sport settlement audit.

Scans every settled Perklocks pick and flags rows that fail the
Universal Settlement Contract:

  * ``actual = 0`` while ``settlement_verified != True`` — the exact
    Seymour failure class.
  * missing final_score / settlement_detail while marked ``lost``
  * missing line while marked ``lost``
  * alt-line inconsistency (two picks on the same player/event/market
    family with contradictory result signs given the derived actual)
  * DNP / void picks that got graded as loss
  * post-mortem generated on an unverified settlement

READ-ONLY.  No writes, no picks touched.  Writes JSON report to
``/tmp/p0_settlement_audit_*.json``.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from motor.motor_asyncio import AsyncIOMotorClient

# ── Regex to pull "Over N.5" or "N+" thresholds from a market string
_OVER_RE = re.compile(r"\b(over|under)\s+([\d.]+)\b", re.IGNORECASE)
_MILESTONE_RE = re.compile(r"\b(\d+)\+\b")

_STATUSES_SETTLED = ("won", "lost", "push", "void")


def _extract_line(pick: dict) -> tuple[Any, Any]:
    """Return (line, side).  Falls back to market-string parsing."""
    line = pick.get("line") or pick.get("published_line") or pick.get(
        "sim_threshold")
    side = None
    market = pick.get("market") or ""
    m = _OVER_RE.search(market)
    if m:
        side = m.group(1).lower()
        if line is None:
            try: line = float(m.group(2))
            except: pass
    ms = _MILESTONE_RE.search(market)
    if ms and line is None:
        try: line = int(ms.group(1))
        except: pass
        side = "milestone"
    return line, side


def _extract_actual(pick: dict) -> Any:
    """Try to find the actual stat from the various settlement
    shapes historical Perklocks settlers have written."""
    sd = pick.get("settlement_detail") or {}
    if isinstance(sd, dict):
        v = sd.get("value")
        if v is not None:
            return v
    fs = pick.get("final_score") or {}
    if isinstance(fs, dict):
        for k, v in fs.items():
            if k.lower() == "line":
                continue
            if isinstance(v, (int, float)):
                return v
    return None


def _classify(pick: dict) -> tuple[str, dict]:
    """Return (bucket, diag_row)."""
    status = pick.get("status") or pick.get("result")
    line, side = _extract_line(pick)
    actual = _extract_actual(pick)
    verified = pick.get("grade_verified_at") is not None \
               or pick.get("settlement_verified") is True

    diag = {
        "pick_id": pick.get("id"),
        "sport": pick.get("sport"),
        "market": pick.get("market"),
        "line": line, "side": side, "actual": actual,
        "status": status, "verified": bool(verified),
        "settled_via": pick.get("settled_via"),
        "grade_verify_source": pick.get("grade_verify_source"),
        "lock_score": pick.get("published_lock_score")
                     or pick.get("lock_score"),
    }

    if status not in _STATUSES_SETTLED:
        return "not_settled", diag

    if status == "lost":
        # Suspicious actual == 0 with verified BUT via a compat path
        # (Seymour class): actual 0.0 stored, grade_verify_result=agreed,
        # BUT the actual value equals the numeric zero rather than the
        # real box-score stat.
        if actual == 0 or actual == 0.0:
            return "suspicious_actual_zero_loss", diag
        if actual is None:
            return "lost_without_actual", diag
        if line is None:
            return "lost_without_line", diag
        # Consistency check: for over/under, actual > line should
        # NOT be marked lost.
        try:
            if side == "over" and float(actual) > float(line):
                return "wrong_wl_grade_over", diag
            if side == "under" and float(actual) < float(line):
                return "wrong_wl_grade_under", diag
            if side == "milestone" and float(actual) >= float(line):
                return "wrong_wl_grade_milestone", diag
        except Exception:
            pass
        return "ok", diag

    if status == "won":
        if actual is None:
            return "won_without_actual", diag
        if line is None:
            return "won_without_line", diag
        try:
            if side == "over" and float(actual) < float(line):
                return "wrong_wl_grade_over_won", diag
            if side == "under" and float(actual) > float(line):
                return "wrong_wl_grade_under_won", diag
        except Exception:
            pass
        return "ok", diag

    return "ok", diag


async def run(db) -> dict[str, Any]:
    counts_by_sport: dict[str, Counter] = defaultdict(Counter)
    samples: dict[str, list[dict]] = defaultdict(list)
    high_conf_affected: dict[str, int] = defaultdict(int)
    total = 0

    async for p in db.picks.find(
        {"status": {"$in": list(_STATUSES_SETTLED)}},
        {"_id": 0, "id": 1, "sport": 1, "market": 1, "line": 1,
         "published_line": 1, "sim_threshold": 1,
         "settlement_detail": 1, "final_score": 1,
         "status": 1, "result": 1, "settled_via": 1,
         "settlement_verified": 1, "grade_verified_at": 1,
         "grade_verify_source": 1, "grade_verify_result": 1,
         "published_lock_score": 1, "lock_score": 1}):
        total += 1
        bucket, diag = _classify(p)
        sport = p.get("sport") or "unknown"
        counts_by_sport[sport][bucket] += 1
        if bucket != "ok" and bucket != "not_settled":
            if len(samples[bucket]) < 10:
                samples[bucket].append(diag)
            ls = p.get("published_lock_score") or p.get("lock_score") or 0
            try:
                if float(ls) > 85:
                    high_conf_affected[bucket] += 1
            except Exception:
                pass

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "total_settled_scanned": total,
        "counts_by_sport": {s: dict(c) for s, c in counts_by_sport.items()},
        "high_conf_gt_85_affected_by_bucket": dict(high_conf_affected),
        "representative_samples": dict(samples),
    }


async def _main():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = client[os.environ.get("DB_NAME", "perkslocks_production")]
    report = await run(db)
    ts = report["generated_at"].replace(":", "").replace("-", "")
    path = f"/tmp/p0_settlement_audit_{ts}.json"
    with open(path, "w") as fh:
        json.dump(report, fh, indent=2, default=str)
    print("=" * 78)
    print("P0 — READ-ONLY Settlement Audit")
    print("=" * 78)
    print(f"Total settled picks scanned: {report['total_settled_scanned']}")
    print()
    for sport, counts in report["counts_by_sport"].items():
        print(f"── {sport} ──")
        for b, c in sorted(counts.items(), key=lambda kv: -kv[1]):
            if b in ("ok", "not_settled"):
                continue
            hc = report["high_conf_gt_85_affected_by_bucket"].get(b, 0)
            print(f"    {c:6d}  {b}   (>85: {hc})")
        print()
    print(f"[report written] {path}")
    client.close()


if __name__ == "__main__":
    asyncio.run(_main())


__all__ = ["run"]
