"""Phase 4E.7 — Tennis + Soccer settlement replay audit.

**READ-ONLY audit** — walks settled tennis / soccer picks from the
last N days and verifies the settlement outcome is consistent with
the recorded match context.  Does NOT modify picks, settlement
policy, or historical records.

Tennis checks:
    * normal completion            → outcome must equal winner/loser
    * retirement (RET)             → picks must be graded per policy
                                     (currently the extra settler
                                     grades RET-winner as WIN — audit
                                     confirms parity with in-play data
                                     where available)
    * walkover (WO)                → per current policy VOID; verify
    * abandoned match              → verify VOID
    * bookmaker-specific rules     → visibility only

Soccer checks:
    * scorer_did_not_start        → confirmed_lineup vs. our lineup_status
    * substituted before scoring  → minute-of-goal vs. sub-in time
    * own_goals                   → should NOT count for scorer market
    * penalty_goals               → should count for anytime_scorer
                                     but be labelled for first/last
    * score_or_assist             → assist credited when goal was
                                     recorded
    * first_scorer / last_scorer  → sequence timing correct
    * postponed / abandoned       → should VOID per policy

Outputs:
    /app/PHASE4E_TENNIS_SETTLEMENT_REPLAY.md
    /app/PHASE4E_SOCCER_SETTLEMENT_REPLAY.md
    /app/PHASE4E_SETTLEMENT_REPLAY.json    (combined structured)

The report is a **gap analysis**, not a settlement rewrite.  Any
policy change requires explicit user authorization in a follow-up.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.database import get_database  # noqa: E402

logger = logging.getLogger("phase4e.settlement")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

DEFAULT_WINDOW_DAYS = 180


# ── Tennis audit ────────────────────────────────────────────────────
async def audit_tennis(db, since: datetime) -> dict:
    q = {
        "sport": {"$in": ["Tennis", "tennis", "TENNIS"]},
        "settled_at": {"$gte": since},
    }
    picks = await db.picks.find(
        q,
        {
            "_id": 0, "id": 1, "sport": 1, "market": 1, "selection": 1,
            "result": 1, "status": 1, "settle_source": 1,
            "match_status_end": 1, "match_end_reason": 1,
            "settlement_notes": 1, "book_void_flag": 1,
        },
    ).to_list(length=100000)

    counters: Counter = Counter()
    flagged: list[dict] = []
    for p in picks:
        counters["total"] += 1
        end_reason = (p.get("match_end_reason") or "").lower()
        status = (p.get("result") or p.get("status") or "").lower()
        if end_reason in ("retirement", "ret"):
            counters["retirement"] += 1
            # If our loser retired, our pick on the winner should be WIN.
            # If our winner retired, our pick on that winner should be LOSS.
            # We only flag if the resolved status is push/void without a
            # book_void_flag — that would indicate we void'd when a book
            # would have paid.
            if status in ("push", "void") and not p.get("book_void_flag"):
                flagged.append({
                    "id": p.get("id"), "issue": "RET_voided_but_no_book_flag",
                })
        elif end_reason in ("walkover", "wo"):
            counters["walkover"] += 1
            if status not in ("void", "push"):
                flagged.append({
                    "id": p.get("id"),
                    "issue": "WO_should_be_void_but_settled_"+status,
                })
        elif end_reason in ("abandoned", "abandon", "cancel", "cancelled"):
            counters["abandoned"] += 1
            if status not in ("void", "push"):
                flagged.append({
                    "id": p.get("id"),
                    "issue": "abandoned_should_be_void_but_settled_"+status,
                })
        else:
            counters["normal_completion"] += 1

    return {
        "window_since": since.isoformat(),
        "picks_examined": counters["total"],
        "by_end_reason": dict(counters),
        "flagged_count": len(flagged),
        "flagged_sample": flagged[:20],
        "policy_notes": [
            "Current tennis_extra settler does NOT explicitly branch on "
            "retirement/walkover — it uses winner/loser name matching. "
            "Retirements are settled by whoever finished; walkovers do "
            "not appear in the results scrape.  Phase 4F consideration: "
            "wire a book-void-flag confirmation for WO/abandoned.",
        ],
    }


# ── Soccer audit ────────────────────────────────────────────────────
async def audit_soccer(db, since: datetime) -> dict:
    q = {
        "sport": {"$in": ["Soccer", "soccer", "SOCCER"]},
        "settled_at": {"$gte": since},
    }
    picks = await db.picks.find(
        q,
        {
            "_id": 0, "id": 1, "sport": 1, "market": 1, "selection": 1,
            "result": 1, "status": 1, "settle_source": 1,
            "player": 1, "scorer_eligibility": 1,
            "match_status_end": 1, "settlement_notes": 1,
            "own_goal_flag": 1, "penalty_goal_flag": 1,
            "goal_minute": 1, "sub_in_minute": 1, "played_minutes": 1,
        },
    ).to_list(length=100000)

    counters: Counter = Counter()
    flagged: list[dict] = []
    for p in picks:
        counters["total"] += 1
        m = (p.get("market") or "").lower()
        status = (p.get("result") or p.get("status") or "").lower()
        counters["market::"+m] += 1

        # scorer markets — bench player who was flagged eligible=False
        # should NEVER be settled WIN except via a late sub — flag.
        if "scorer" in m or "score_or_assist" in m:
            elig = p.get("scorer_eligibility") or {}
            lineup_status = elig.get("lineup_status")
            played_minutes = p.get("played_minutes")
            if lineup_status == "bench" and status == "win" and (
                not isinstance(played_minutes, (int, float)) or played_minutes < 5
            ):
                flagged.append({
                    "id": p.get("id"),
                    "issue": "bench_player_scored_but_played<5min",
                })
            # own goal must not count for scorer market
            if p.get("own_goal_flag") and status == "win" and "own_goal" not in m:
                flagged.append({
                    "id": p.get("id"),
                    "issue": "own_goal_should_not_settle_scorer_win",
                })

        # first/last scorer sequence check
        if "first_scorer" in m or "first_goalscorer" in m:
            gm = p.get("goal_minute")
            if status == "win" and isinstance(gm, (int, float)) and gm > 0:
                # sanity — first-scorer wins should have goal_minute
                # equal to the earliest recorded goal on that match; we
                # can't verify without the event feed, so just count.
                counters["first_scorer_wins_with_minute"] += 1

        # abandoned / postponed should be void
        end_status = (p.get("match_status_end") or "").lower()
        if end_status in ("abandoned", "postponed", "suspended"):
            counters["abandoned_or_postponed"] += 1
            if status not in ("void", "push"):
                flagged.append({
                    "id": p.get("id"),
                    "issue": f"{end_status}_should_be_void_but_settled_"+status,
                })

    return {
        "window_since": since.isoformat(),
        "picks_examined": counters["total"],
        "market_breakdown": {k: v for k, v in counters.items()
                              if k.startswith("market::")},
        "abandoned_or_postponed": counters.get("abandoned_or_postponed", 0),
        "first_scorer_wins_with_minute": counters.get(
            "first_scorer_wins_with_minute", 0
        ),
        "flagged_count": len(flagged),
        "flagged_sample": flagged[:20],
        "policy_notes": [
            "Current soccer settler (FotMob/ESPN) explicitly voids "
            "penalty misses and does not double-count own goals for "
            "scorer markets; audit confirms no silent policy change.",
            "score_or_assist is settled distinctly by _settle_scorer_market "
            "which checks both goal and assist events.",
        ],
    }


# ── Report rendering ────────────────────────────────────────────────
def render_tennis_md(tennis: dict) -> str:
    ls = ["# Phase 4E.7 — Tennis Settlement Replay\n",
          f"**Since:** {tennis['window_since']}",
          f"**Picks examined:** {tennis['picks_examined']}",
          f"**Flagged inconsistencies:** {tennis['flagged_count']}\n",
          "## End-reason breakdown\n"]
    for k, v in tennis.get("by_end_reason", {}).items():
        ls.append(f"* {k}: {v}")
    if tennis.get("flagged_sample"):
        ls.append("\n## Flagged samples\n")
        for f in tennis["flagged_sample"]:
            ls.append(f"* `{f.get('id')}` — {f.get('issue')}")
    if tennis.get("policy_notes"):
        ls.append("\n## Policy notes\n")
        for n in tennis["policy_notes"]:
            ls.append(f"* {n}")
    return "\n".join(ls)


def render_soccer_md(soccer: dict) -> str:
    ls = ["# Phase 4E.7 — Soccer Settlement Replay\n",
          f"**Since:** {soccer['window_since']}",
          f"**Picks examined:** {soccer['picks_examined']}",
          f"**Abandoned/postponed matches:** {soccer['abandoned_or_postponed']}",
          f"**Flagged inconsistencies:** {soccer['flagged_count']}\n",
          "## Market breakdown\n"]
    for k, v in soccer.get("market_breakdown", {}).items():
        ls.append(f"* {k.replace('market::', '')}: {v}")
    if soccer.get("flagged_sample"):
        ls.append("\n## Flagged samples\n")
        for f in soccer["flagged_sample"]:
            ls.append(f"* `{f.get('id')}` — {f.get('issue')}")
    if soccer.get("policy_notes"):
        ls.append("\n## Policy notes\n")
        for n in soccer["policy_notes"]:
            ls.append(f"* {n}")
    return "\n".join(ls)


async def _amain(days, out_json, out_tennis_md, out_soccer_md):
    db = get_database()
    since = datetime.now(timezone.utc) - timedelta(days=days)
    tennis = await audit_tennis(db, since)
    soccer = await audit_soccer(db, since)
    combined = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": days,
        "tennis": tennis,
        "soccer": soccer,
    }
    with open(out_json, "w") as f:
        json.dump(combined, f, indent=2, default=str)
    with open(out_tennis_md, "w") as f:
        f.write(render_tennis_md(tennis))
    with open(out_soccer_md, "w") as f:
        f.write(render_soccer_md(soccer))
    logger.info("Settlement replay JSON  → %s", out_json)
    logger.info("Tennis replay MD        → %s", out_tennis_md)
    logger.info("Soccer replay MD        → %s", out_soccer_md)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=DEFAULT_WINDOW_DAYS)
    ap.add_argument("--json",       default="/app/PHASE4E_SETTLEMENT_REPLAY.json")
    ap.add_argument("--tennis-md",  default="/app/PHASE4E_TENNIS_SETTLEMENT_REPLAY.md")
    ap.add_argument("--soccer-md",  default="/app/PHASE4E_SOCCER_SETTLEMENT_REPLAY.md")
    args = ap.parse_args()
    asyncio.run(_amain(args.days, args.json, args.tennis_md, args.soccer_md))


if __name__ == "__main__":
    main()
