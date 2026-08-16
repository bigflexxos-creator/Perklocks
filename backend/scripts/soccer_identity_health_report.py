"""Generate the required SOCCER_PLAYER_IDENTITY_HEALTH report per league.

Uses existing DB records only — no provider calls.
"""
import asyncio, os
from datetime import datetime, timezone
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

load_dotenv("/app/backend/.env")


async def main():
    c = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = c[os.environ["DB_NAME"]]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    identity_statuses = [
        "IDENTITY_RESOLVED",
        "PLAYER_IDENTITY_UNRESOLVED",
        "PLAYER_IDENTITY_AMBIGUOUS",
        "PLAYER_TEAM_MISMATCH",
        "STALE_ROSTER",
        "PLAYER_SOURCE_ID_UNMAPPED",
        "EVENT_IDENTITY_FAILURE",
        "TEAM_IDENTITY_FAILURE",
    ]
    reject_codes = [
        "PLAYER_HISTORY_NOT_FOUND",
        "PLAYER_FORM_NOT_FOUND",
        "ENRICHMENT_NOT_FOUND",
        "PLAYER_IDENTITY_UNRESOLVED",
        "PLAYER_IDENTITY_AMBIGUOUS",
        "MISSING_FEATURE_DATA",
        "PLAYER_IDENTITY_FAILURE",
    ]

    q_base = {
        "sport": "Soccer", "pick_date": today,
        "source": "real_line_alt_scorer_v1",
    }

    total = await db.picks.count_documents(q_base)
    print(f"=== SOCCER_PLAYER_IDENTITY_HEALTH (pick_date={today}) ===")
    print(f"total scorer rows: {total}\n")

    print("── Identity status breakdown (app-wide) ──")
    identity_totals: dict[str, int] = {}
    for st in identity_statuses:
        n = await db.picks.count_documents({**q_base, "identity_status": st})
        identity_totals[st] = n
        print(f"  {st:32s}: {n}")

    print("\n── Rejection reason breakdown (app-wide) ──")
    for code in reject_codes:
        n = await db.picks.count_documents({
            **q_base, "off_board_reasons": {"$in": [code]},
        })
        print(f"  {code:32s}: {n}")

    modeled = await db.picks.count_documents({
        **q_base, "model_probability": {"$exists": True, "$ne": None},
    })
    canonical_eligible = await db.picks.count_documents({
        **q_base, "off_board": {"$ne": True},
    })
    print(f"\nmodeled:             {modeled}")
    print(f"canonical eligible:  {canonical_eligible}")

    print("\n=== PER-LEAGUE IDENTITY HEALTH ===")
    print(f"{'league':30s} {'raw':>6s} {'resolved':>10s} {'unresolved':>12s} {'ambiguous':>10s} {'hist_nf':>8s} {'form_nf':>8s} {'modeled':>8s} {'eligible':>9s}")
    print("─" * 130)
    leagues = await db.picks.distinct("league", q_base)
    for lg in sorted(leagues, key=lambda x: (x or "")):
        if not lg:
            continue
        base = {**q_base, "league": lg}
        raw = await db.picks.count_documents(base)
        resolved = await db.picks.count_documents(
            {**base, "identity_status": "IDENTITY_RESOLVED"}
        )
        unresolved = await db.picks.count_documents(
            {**base, "identity_status": "PLAYER_IDENTITY_UNRESOLVED"}
        )
        ambiguous = await db.picks.count_documents(
            {**base, "identity_status": "PLAYER_IDENTITY_AMBIGUOUS"}
        )
        hist_nf = await db.picks.count_documents(
            {**base, "off_board_reasons": "PLAYER_HISTORY_NOT_FOUND"}
        )
        form_nf = await db.picks.count_documents(
            {**base, "off_board_reasons": "PLAYER_FORM_NOT_FOUND"}
        )
        modeled_l = await db.picks.count_documents(
            {**base, "model_probability": {"$exists": True, "$ne": None}}
        )
        eligible_l = await db.picks.count_documents(
            {**base, "off_board": {"$ne": True}}
        )
        print(
            f"{lg[:30]:30s} {raw:>6d} {resolved:>10d} {unresolved:>12d} "
            f"{ambiguous:>10d} {hist_nf:>8d} {form_nf:>8d} {modeled_l:>8d} {eligible_l:>9d}"
        )

    # ── MLS FIRST LIVE REGRESSION: 20 scorer trace ──────────────
    print("\n=== MLS SCORER TRACE (first 20) ===")
    n = 0
    async for d in db.picks.find(
        {**q_base, "league": "MLS",
         "market_key": "player_goal_scorer_anytime"},
        {"provider_player_name": 1, "canonical_player_name": 1,
         "canonical_team_name": 1, "canonical_event_id": 1,
         "identity_status": 1, "identity_resolution_method": 1,
         "off_board": 1, "off_board_reasons": 1, "model_probability": 1,
         "lock_score": 1, "event": 1, "consumer_disposition": 1},
    ).limit(20):
        n += 1
        pp = d.get("provider_player_name")
        cn = d.get("canonical_player_name") or "?"
        ct = d.get("canonical_team_name") or "?"
        status = d.get("identity_status") or "?"
        obr = ",".join(d.get("off_board_reasons") or []) or "-"
        mp = d.get("model_probability")
        ls = d.get("lock_score")
        ob = d.get("off_board")
        disp = "VISIBLE" if not ob else obr
        print(
            f"  {n:2d}. {pp[:26]:26s} → {status:28s} → {cn[:20]:20s} @ {ct[:16]:16s} "
            f"→ model={mp} lock={ls} disp={disp}"
        )


asyncio.run(main())
