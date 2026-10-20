"""Post-publish A-S report — queries db.picks after a canonical publish.

Does NOT re-run the pipeline.  Pulls the already-published NBA board
from `db.picks` using the correct field names
(`pick_date` + `sport='NBA'`) and emits the full A-S acceptance
report requested by the user.
"""
from __future__ import annotations
import asyncio, os, sys
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, "/app/backend")
from dotenv import load_dotenv  # noqa: E402
load_dotenv("/app/backend/.env")


def _market_bucket(market: str) -> str:
    m = (market or "").lower()
    if " moneyline" in m: return "ML"
    if "spread" in m: return "SPREAD"
    if m.startswith("total points") or m.startswith("total "): return "TOTAL"
    for fam, kw in [
        ("PTS", ("points",)), ("REB", ("rebounds",)), ("AST", ("assists",)),
        ("3PM", ("threes", "three pointers", "3 pointers")),
        ("STL", ("steals",)), ("BLK", ("blocks",)),
        ("TO", ("turnovers",)),
        ("DD", ("double double", "double-double")),
        ("TD", ("triple double", "triple-double")),
    ]:
        if any(k in m for k in kw): return fam
    return "OTHER"


async def main():
    from services.database import get_database
    date_str = os.environ.get("NBA_PUBLISH_DATE") or \
               datetime.now(timezone.utc).strftime("%Y-%m-%d")
    db = get_database()

    print("=" * 78)
    print(f"STEP C ACCEPTANCE — Post-Publish NBA Preview Report | {date_str}")
    print("=" * 78)

    picks = await db.picks.find({"sport": "NBA", "pick_date": date_str}).to_list(length=5000)
    print(f"\n[R] db.picks rows (sport=NBA, pick_date={date_str}): {len(picks)}")

    if not picks:
        print("[!] no NBA picks found for date — nothing to report")
        return

    bkt = Counter()
    pub_bkt_85 = Counter()
    lock_dist = Counter()
    off_board = 0
    for p in picks:
        b = _market_bucket(p.get("market") or "")
        bkt[b] += 1
        if (p.get("lock_score") or 0) >= 85:
            pub_bkt_85[b] += 1
        lb = int((p.get("lock_score") or 0))
        lock_dist[(lb // 5) * 5] += 1
        if p.get("off_board"):
            off_board += 1

    print(f"\n[S] off_board picks (filtered from UI): {off_board}")
    print(f"\n[S] Published candidates by market family:")
    for b, c in sorted(bkt.items(), key=lambda x: -x[1]):
        print(f"   {b:8s}: {c}")

    print("\n[S] Published candidates with lock>=85 (publishable gate):")
    for b, c in sorted(pub_bkt_85.items(), key=lambda x: -x[1]):
        print(f"   {b:8s}: {c}")
    print(f"   TOTAL >=85: {sum(pub_bkt_85.values())}")

    print("\n[S] Published Lock Score distribution (bucketed by 5):")
    for lb in sorted(lock_dist.keys(), reverse=True):
        bar = "#" * min(60, lock_dist[lb])
        print(f"   {lb:3d}-{lb+4:3d}: {lock_dist[lb]:4d} {bar}")

    on_board = [p for p in picks if not p.get("off_board")]
    print(f"\n[S] on-board picks: {len(on_board)}")
    print("\n[S] Top 20 ON-BOARD picks by lock_score:")
    top = sorted(on_board, key=lambda x: -(x.get("lock_score") or 0))[:20]
    for i, p in enumerate(top, 1):
        wp = p.get("model_win_probability")
        if wp is None:
            wp = p.get("win_probability")
        try:
            wp_f = float(wp) if wp is not None else 0.0
        except (TypeError, ValueError):
            wp_f = 0.0
        if wp_f and wp_f <= 1.0:
            wp_f *= 100
        print(f"   [{i:2d}] lock={(p.get('lock_score') or 0):5.1f}  "
              f"wp={wp_f:5.1f}  "
              f"edge={p.get('edge_percent') or 0:+6.2f}  "
              f"book={p.get('book_odds')}  "
              f"grade={p.get('grade') or '?':4s}  "
              f"{p.get('market')} ({p.get('selection') or p.get('pick') or ''})")

    # Market type breakdown
    ml = [p for p in on_board if _market_bucket(p.get('market') or '') == "ML"]
    sp = [p for p in on_board if _market_bucket(p.get('market') or '') == "SPREAD"]
    tt = [p for p in on_board if _market_bucket(p.get('market') or '') == "TOTAL"]
    print(f"\n[N] NBA Moneyline on-board: {len(ml)}  (top 3)")
    for p in sorted(ml, key=lambda x: -(x.get('lock_score') or 0))[:3]:
        wp = p.get('model_win_probability') or p.get('win_probability') or 0
        try: wp = float(wp)
        except: wp = 0.0
        if wp <= 1: wp *= 100
        print(f"   lock={(p.get('lock_score') or 0):5.1f} wp={wp:5.1f} "
              f"edge={p.get('edge_percent') or 0:+6.2f} {p.get('market')}")
    print(f"\n[O] NBA Spread on-board: {len(sp)}  (top 3)")
    for p in sorted(sp, key=lambda x: -(x.get('lock_score') or 0))[:3]:
        wp = p.get('model_win_probability') or p.get('win_probability') or 0
        try: wp = float(wp)
        except: wp = 0.0
        if wp <= 1: wp *= 100
        print(f"   lock={(p.get('lock_score') or 0):5.1f} wp={wp:5.1f} "
              f"edge={p.get('edge_percent') or 0:+6.2f} {p.get('market')}")
    print(f"\n[P] NBA Total on-board: {len(tt)}  (top 3)")
    for p in sorted(tt, key=lambda x: -(x.get('lock_score') or 0))[:3]:
        wp = p.get('model_win_probability') or p.get('win_probability') or 0
        try: wp = float(wp)
        except: wp = 0.0
        if wp <= 1: wp *= 100
        print(f"   lock={(p.get('lock_score') or 0):5.1f} wp={wp:5.1f} "
              f"edge={p.get('edge_percent') or 0:+6.2f} {p.get('market')}")

    # Prediction snapshots
    try:
        snap_n = await db.prediction_snapshots.count_documents(
            {"sport": "NBA", "pick_date": date_str})
        print(f"\n[Q] prediction_snapshots (NBA/{date_str}): {snap_n} rows")
    except Exception as _sn_err:
        print(f"[!] prediction_snapshots read failed: {_sn_err}")

    # Publication events
    try:
        events = await db.publication_events.find(
            {"$or": [{"sport": "NBA"}, {"sports": "NBA"}]}
        ).sort("ts", -1).to_list(length=5)
        print(f"\n[Q] publication_events (recent NBA-touching): {len(events)}")
        for e in events[:3]:
            print(f"   t={e.get('ts') or e.get('event_time') or e.get('created_at')} "
                  f"type={e.get('event_type') or e.get('type')} "
                  f"source={e.get('publication_source')} "
                  f"accepted={e.get('accepted_count')} rejected={e.get('rejected_count')}")
    except Exception as _pe_err:
        print(f"[!] publication_events read failed: {_pe_err}")


if __name__ == "__main__":
    asyncio.run(main())
