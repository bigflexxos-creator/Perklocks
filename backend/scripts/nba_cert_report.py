"""NBA full-slate certification diagnostic — Step C acceptance report."""
from __future__ import annotations
import asyncio, os, sys, json
from collections import Counter

sys.path.insert(0, "/app/backend")
from dotenv import load_dotenv  # noqa: E402
load_dotenv("/app/backend/.env")
os.environ.setdefault("THEODDS_FILTER_SPORT", "NBA")

import sports_engine as se  # noqa: E402


def _market_bucket(market: str) -> str:
    m = (market or "").lower()
    if " moneyline" in m: return "ML"
    if "spread" in m: return "SPREAD"
    if m.startswith("total points") or " total " in m or m.endswith(" total") or "o/u" in m or m.startswith("total "): return "TOTAL"
    # Player prop families
    for fam, kw in [
        ("PTS", ("points",)), ("REB", ("rebounds",)), ("AST", ("assists",)),
        ("PRA", ("points rebounds assists",)),
        ("3PM", ("threes", "three pointers", "3 pointers")),
        ("STL", ("steals",)), ("BLK", ("blocks",)),
        ("TO", ("turnovers",)), ("PR", ("player rebounds",)),
        ("PA", ("player points assists",)),
        ("RA", ("player rebounds assists",)),
        ("BS", ("blocks steals",)),
        ("DD", ("double double", "double-double")),
        ("TD", ("triple double", "triple-double")),
    ]:
        if any(k in m for k in kw): return fam
    return "OTHER"


async def main():
    picks = await se.generate_all_picks(sport_filter="NBA")
    print(f"\n{'='*78}\nSTEP C ACCEPTANCE — NBA Preview Candidate Report\n{'='*78}")
    print(f"[E] model attempts  → candidates produced: {len(picks)}")

    bkt = Counter()
    pub_bkt = Counter()
    lock_dist = Counter()
    for p in picks:
        m = p.get("market") or ""
        b = _market_bucket(m)
        bkt[b] += 1
        if (p.get("lock_score") or 0) >= 85:
            pub_bkt[b] += 1
        lb = int((p.get("lock_score") or 0))
        lock_dist[(lb // 5) * 5] += 1

    print(f"\n[G] Candidates by market family:")
    for b, c in sorted(bkt.items(), key=lambda x: -x[1]):
        print(f"   {b:8s}: {c}")
    print(f"\n[H] Candidates with lock >= 85 (publishable gate):")
    for b, c in sorted(pub_bkt.items(), key=lambda x: -x[1]):
        print(f"   {b:8s}: {c}")
    total_pub = sum(pub_bkt.values())
    print(f"   TOTAL >=85: {total_pub}")

    print(f"\n[L] Lock Score distribution (bucketed by 5):")
    for lb in sorted(lock_dist.keys(), reverse=True):
        bar = "#" * min(60, lock_dist[lb])
        print(f"   {lb:3d}-{lb+4:3d}: {lock_dist[lb]:4d} {bar}")

    print(f"\n[M] Top 20 picks by lock_score:")
    top = sorted(picks, key=lambda x: -(x.get("lock_score") or 0))[:20]
    for i, p in enumerate(top, 1):
        wp = p.get("model_win_prob") or p.get("win_probability")
        print(f"   [{i:2d}] lock={p.get('lock_score'):5.1f}  wp={wp:5.1f}  "
              f"edge={p.get('edge_percent') or 0:+6.2f}  book={p.get('book_odds')}  "
              f"{p.get('market')}  ({p.get('pick')})")

    # [N/O/P] Game markets evidence proof
    ml = [p for p in picks if _market_bucket(p.get('market') or '') == "ML"]
    sp = [p for p in picks if _market_bucket(p.get('market') or '') == "SPREAD"]
    tt = [p for p in picks if _market_bucket(p.get('market') or '') == "TOTAL"]
    print(f"\n[N] Moneyline: {len(ml)} candidates (sample top 3):")
    for p in sorted(ml, key=lambda x: -(x.get('lock_score') or 0))[:3]:
        wp = p.get('model_win_prob') if p.get('model_win_prob') is not None else p.get('win_probability')
        print(f"   lock={(p.get('lock_score') or 0):5.1f} wp={(wp or 0):5.1f} "
              f"edge={p.get('edge_percent') or 0:+6.2f} {p.get('market')} ({p.get('pick')})")
    print(f"\n[O] Spread: {len(sp)} candidates (sample top 3):")
    for p in sorted(sp, key=lambda x: -(x.get('lock_score') or 0))[:3]:
        wp = p.get('model_win_prob') if p.get('model_win_prob') is not None else p.get('win_probability')
        print(f"   lock={(p.get('lock_score') or 0):5.1f} wp={(wp or 0):5.1f} "
              f"edge={p.get('edge_percent') or 0:+6.2f} {p.get('market')} ({p.get('pick')})")
    print(f"\n[P] Total: {len(tt)} candidates (sample top 3):")
    for p in sorted(tt, key=lambda x: -(x.get('lock_score') or 0))[:3]:
        wp = p.get('model_win_prob') if p.get('model_win_prob') is not None else p.get('win_probability')
        print(f"   lock={(p.get('lock_score') or 0):5.1f} wp={(wp or 0):5.1f} "
              f"edge={p.get('edge_percent') or 0:+6.2f} {p.get('market')} ({p.get('pick')})")


if __name__ == "__main__":
    asyncio.run(main())
