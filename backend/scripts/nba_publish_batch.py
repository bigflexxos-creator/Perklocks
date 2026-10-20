"""NBA Preview Publication — Step C auto-authorized execution.

Runs the full canonical pick-refresh pipeline for NBA ONLY and reports
the final published state (what the API/UI will serve).  This invokes
the orchestrator (``_refresh_picks``) which internally calls
``PredictionPublicationService.publish_batch()`` and emits the
canonical publication events, so the published artefacts are
identical to a scheduled refresh for NBA.
"""
from __future__ import annotations
import asyncio, os, sys, json
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, "/app/backend")
from dotenv import load_dotenv  # noqa: E402
load_dotenv("/app/backend/.env")
os.environ.setdefault("THEODDS_FILTER_SPORT", "NBA")


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
    from services.pick_refresh_orchestrator import _refresh_picks
    from services.database import get_database
    date_str = os.environ.get("NBA_PUBLISH_DATE") or \
               datetime.now(timezone.utc).strftime("%Y-%m-%d")

    print("=" * 78)
    print(f"STEP C PREVIEW PUBLICATION — NBA | date={date_str}")
    print("=" * 78)
    print("\n[Q] Invoking canonical refresh pipeline (sport_filter='NBA')…")
    try:
        n = await _refresh_picks(date_str, sport_filter="NBA")
    except Exception as e:
        print(f"[!] REFRESH FAILED: {type(e).__name__}: {e}")
        raise
    print(f"    orchestrator returned picks_written={n}")

    db = get_database()
    nba_cur = db.picks.find({"sport": "NBA", "date": date_str})
    published = await nba_cur.to_list(length=5000)
    print(f"\n[R] db.picks rows (sport=NBA, date={date_str}): {len(published)}")

    bkt = Counter()
    pub_bkt_85 = Counter()
    lock_dist = Counter()
    for p in published:
        b = _market_bucket(p.get("market") or "")
        bkt[b] += 1
        if (p.get("lock_score") or 0) >= 85:
            pub_bkt_85[b] += 1
        lb = int((p.get("lock_score") or 0))
        lock_dist[(lb // 5) * 5] += 1

    print("\n[S] Published candidates by market family:")
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

    print("\n[S] Top 20 published picks by lock_score:")
    top = sorted(published, key=lambda x: -(x.get("lock_score") or 0))[:20]
    for i, p in enumerate(top, 1):
        wp = p.get("model_win_prob")
        if wp is None:
            wp = p.get("win_probability")
        print(f"   [{i:2d}] lock={(p.get('lock_score') or 0):5.1f}  "
              f"wp={(wp or 0):5.1f}  "
              f"edge={p.get('edge_percent') or 0:+6.2f}  "
              f"book={p.get('book_odds')}  "
              f"{p.get('market')}  (team={p.get('team') or ''})")

    # Also fetch canonical publication_events for proof
    try:
        events = await db.publication_events.find(
            {"sport": "NBA", "date": date_str}
        ).to_list(length=10)
        print(f"\n[S] publication_events (NBA/{date_str}): {len(events)} rows")
        for e in events[:3]:
            print(f"   event={e.get('event_type')} status={e.get('status')} "
                  f"accepted={e.get('accepted_count')} rejected={e.get('rejected_count')} "
                  f"source={e.get('publication_source')}")
    except Exception as _pe_err:
        print(f"[!] publication_events read failed: {_pe_err}")

    # Prediction snapshots
    try:
        snap_n = await db.prediction_snapshots.count_documents(
            {"sport": "NBA", "date": date_str})
        print(f"\n[S] prediction_snapshots (NBA/{date_str}): {snap_n} rows")
    except Exception as _sn_err:
        print(f"[!] prediction_snapshots read failed: {_sn_err}")


if __name__ == "__main__":
    asyncio.run(main())
