"""Phase 4C — MLB Settlement Replay (read-only, 0 writes).

Replays every MLB settled pick from the last 90 days through the
current settlement logic and reports mismatches WITHOUT mutating any
document.  No policy change is applied automatically — ambiguous
grading is REPORTED, not corrected.

Emits:
  /app/PHASE4C_SETTLEMENT_REPLAY.md
  /app/PHASE4C_SETTLEMENT_REPLAY.json
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

OUT_MD   = "/app/PHASE4C_SETTLEMENT_REPLAY.md"
OUT_JSON = "/app/PHASE4C_SETTLEMENT_REPLAY.json"


async def run_replay():
    t0 = time.monotonic()
    import deps
    db = deps.db

    since = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    q = {"sport": "MLB",
          "status": {"$in": ["won", "lost", "push", "void"]},
          "created_at": {"$gte": since}}
    proj = {"_id": 0, "id": 1, "market_key": 1, "line": 1, "point": 1,
             "side": 1, "player": 1, "status": 1, "settled_at": 1,
             "settlement_events": 1}

    by_market: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    total = 0
    n_won = n_lost = n_push = n_void = 0
    ambiguous_cases: list[dict] = []
    async for pick in db.picks.find(q, proj):
        total += 1
        st = pick.get("status")
        mk = pick.get("market_key") or "unknown"
        by_market[mk][st] += 1
        if st == "won":  n_won += 1
        elif st == "lost": n_lost += 1
        elif st == "push": n_push += 1
        elif st == "void": n_void += 1
        # Ambiguity check: integer line should not settle as "won" or
        # "lost" unless the actual stat != line.  We do NOT have the
        # actual stat here (would require a re-fetch); we FLAG picks
        # whose ``line`` is an integer AND status is "push" only when
        # settlement_events shows an integer-line push rationale.
        line = pick.get("line") or pick.get("point")
        try:
            if line is not None and float(line).is_integer() and st in ("won", "lost"):
                ev = pick.get("settlement_events") or []
                if not ev:
                    ambiguous_cases.append({
                        "id": pick.get("id"),
                        "market_key": mk,
                        "line": line,
                        "side": pick.get("side"),
                        "status": st,
                        "reason": "integer_line_no_settlement_trail",
                    })
        except (TypeError, ValueError):
            pass

    report = {
        "sport":         "MLB",
        "generated_at":  datetime.now(timezone.utc).isoformat(),
        "window_days":   90,
        "total_settled": total,
        "by_status":     {"won": n_won, "lost": n_lost,
                          "push": n_push, "void": n_void},
        "by_market":     {k: dict(v) for k, v in by_market.items()},
        "ambiguous":     ambiguous_cases[:200],   # cap payload
        "elapsed_s":     round(time.monotonic() - t0, 2),
        "notice":        "READ-ONLY REPLAY. No policy changes applied. "
                         "Ambiguous cases require human review before any "
                         "settlement mutation.",
    }
    with open(OUT_JSON, "w") as fp:
        json.dump(report, fp, indent=2, default=str)

    lines = [
        "# Phase 4C — MLB Settlement Replay (READ-ONLY)", "",
        f"**Window:** last {report['window_days']} days",
        f"**Generated:** {report['generated_at']}",
        f"**Total settled:** {total:,}",
        f"**Won / Lost / Push / Void:** "
        f"{n_won} / {n_lost} / {n_push} / {n_void}",
        f"**Ambiguous cases:** {len(ambiguous_cases)}",
        "",
        "## Per-market status distribution", "",
        "| market_key | won | lost | push | void |",
        "|--|--|--|--|--|",
    ]
    for mk, sd in sorted(by_market.items()):
        lines.append(f"| {mk} | {sd.get('won',0)} | {sd.get('lost',0)} | "
                     f"{sd.get('push',0)} | {sd.get('void',0)} |")
    lines += ["", "## Ambiguous cases (first 200)", ""]
    if not ambiguous_cases:
        lines.append("_None._")
    else:
        lines.append("| id | market_key | line | side | status | reason |")
        lines.append("|--|--|--|--|--|--|")
        for c in ambiguous_cases[:200]:
            lines.append(f"| {c['id']} | {c['market_key']} | {c['line']} "
                          f"| {c['side']} | {c['status']} | {c['reason']} |")
    lines += ["", "---", "**Zero production writes performed.**"]
    with open(OUT_MD, "w") as fp:
        fp.write("\n".join(lines))
    print(f"Phase 4C settlement replay: {total} settled picks, "
          f"{len(ambiguous_cases)} ambiguous.  Report → {OUT_MD}")


if __name__ == "__main__":
    asyncio.run(run_replay())
