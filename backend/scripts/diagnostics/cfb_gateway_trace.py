"""APP-GATEWAY VS RAW COMPARISON — CFB (2026-06-12).

Read-only.  Runs the same `_fetch_odds_for("americanfootball_ncaaf", ...)`
call the CFB pipeline uses, prints:
  * app-pipeline event count
  * cache state
  * budget state
  * circuit-breaker state
  * bad-market registry state
  * gateway policy result
Compares against the RAW provider count (must match).

Run:
    cd /app/backend && python -m scripts.diagnostics.cfb_gateway_trace
"""
from __future__ import annotations

import asyncio
import json
import sys

sys.path.insert(0, "/app/backend")

from dotenv import load_dotenv
load_dotenv("/app/backend/.env")


async def main() -> None:
    import sports_engine as se

    print("=" * 78)
    print("CFB GATEWAY vs RAW COMPARISON")
    print("=" * 78)

    # 1. Circuit-breaker state ------------------------------------------------
    print("\n──── Circuit-breaker state ────")
    cb = {
        "_API_DISABLED":        se._API_DISABLED,
        "_API_DISABLED_REASON": se._API_DISABLED_REASON,
        "_API_401_STREAK":      se._API_401_STREAK,
        "_API_FAIL_STREAK":     se._API_FAIL_STREAK,
        "_API_TOTAL_OK":        se._API_TOTAL_OK,
        "_API_TOTAL_FAIL":      se._API_TOTAL_FAIL,
        "_API_LAST_ERR":        se._API_LAST_ERR,
    }
    print(json.dumps(cb, indent=2, default=str))

    # 2. Odds provider state -------------------------------------------------
    print("\n──── odds_provider circuit state ────")
    try:
        from services import odds_provider as _op
        print(json.dumps({
            "state":         getattr(_op, "_STATE", "unknown"),
            "consecutive_failures": getattr(_op, "_CONSEC_FAIL", None),
            "open_until_ts": getattr(_op, "_OPEN_UNTIL", None),
        }, indent=2, default=str))
    except Exception as e:
        print(f"  <no odds_provider state or import failed: {e!r}>")

    # 3. Budget state --------------------------------------------------------
    print("\n──── Odds gateway budget state ────")
    try:
        from services.odds_gateway import get_budget_snapshot
        snap = get_budget_snapshot()
        print(json.dumps(snap, indent=2, default=str))
    except Exception as e:
        print(f"  <no get_budget_snapshot: {e!r}>")

    # 4. Bad-market registry --------------------------------------------------
    print("\n──── bad_market_registry for CFB ────")
    try:
        from services.bad_market_registry import current_snapshot
        snap = current_snapshot()
        if isinstance(snap, dict):
            cfb_entries = {k: v for k, v in snap.items()
                            if "ncaaf" in k.lower() or "cfb" in k.lower()}
            print(json.dumps(cfb_entries or {"note": "no CFB entries"},
                              indent=2, default=str))
        else:
            print(str(snap)[:400])
    except Exception as e:
        print(f"  <no bad_market_registry or read failed: {e!r}>")

    # 5. Cache state ---------------------------------------------------------
    print("\n──── Odds cache state for americanfootball_ncaaf ────")
    try:
        from services.odds_cache import CACHE
        cache_hits = 0
        cfb_entries = []
        for key, entry in list(CACHE.items())[:5000]:
            if "americanfootball_ncaaf" in str(key):
                cache_hits += 1
                if len(cfb_entries) < 5:
                    payload = entry.get("payload") if isinstance(entry, dict) else None
                    cfb_entries.append({
                        "key":      key[:120],
                        "fresh_until": entry.get("fresh_until") if isinstance(entry, dict) else None,
                        "stale_until": entry.get("stale_until") if isinstance(entry, dict) else None,
                        "fetched_at":  entry.get("fetched_at") if isinstance(entry, dict) else None,
                        "payload_type": type(payload).__name__,
                        "payload_len":  (len(payload) if isinstance(payload, (list, dict)) else "n/a"),
                    })
        print(f"  {cache_hits} CFB-related cache entries (of {len(CACHE)} total)")
        for e in cfb_entries:
            print("  ·", json.dumps(e, default=str))
    except Exception as e:
        print(f"  <cache introspection failed: {e!r}>")

    # 6. App pipeline fetch: what does sports_engine._fetch_odds_for return? -
    print("\n──── APP PIPELINE: _fetch_odds_for('americanfootball_ncaaf') ────")
    try:
        events = await se._fetch_odds_for(
            "americanfootball_ncaaf", regions="us", sport="CFB",
        )
        print(f"  events returned: {len(events) if isinstance(events, list) else 'not-a-list'}")
        if isinstance(events, list) and events:
            first = events[0]
            print(f"  first event: {first.get('id')} · "
                   f"{first.get('home_team')} vs {first.get('away_team')} · "
                   f"commence={first.get('commence_time')} · "
                   f"bookies={len(first.get('bookmakers') or [])}")
    except Exception as e:
        print(f"  <_fetch_odds_for raised: {e!r}>")

    # 7. Compare to RAW HTTP count -------------------------------------------
    print("\n──── DELTA vs RAW ────")
    print("  RAW /v4/sports/americanfootball_ncaaf/odds = 95 events (per raw probe)")
    print("  RAW /v4/sports/americanfootball_ncaaf_fcs/odds = 34 events (per raw probe)")
    print("  Note: sports_engine.SPORTS_KEYS['CFB'] = ['americanfootball_ncaaf']")
    print("        (americanfootball_ncaaf_fcs is NOT wired.)")


if __name__ == "__main__":
    asyncio.run(main())
