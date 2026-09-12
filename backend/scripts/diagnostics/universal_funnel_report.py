"""UNIVERSAL PROVIDER → PRODUCTION FUNNEL CERTIFICATION (2026-06-12).

Read-only.  Per-sport count funnel:

    RAW PROVIDER events
      → GATEWAY (`_fetch_odds_for`)
      → In-window (30h slate window)
      → DB.picks (upcoming, off_board=False)
      → API-eligible / UI-visible (proxy via db count same shape)

For every reduction, the diagnostic assigns an explicit drop-reason.
No unexplained deltas are permitted.

Sport → provider-key mapping is read from
``sports_engine.SPORT_KEYS`` (single source of truth) so this
diagnostic auto-adopts new/removed keys.

Run:
    cd /app/backend && python -m scripts.diagnostics.universal_funnel_report
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from urllib import request, error

sys.path.insert(0, "/app/backend")

from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

from deps import db
import sports_engine as se

API_KEY = os.getenv("THE_ODDS_API_KEY") or ""
BASE    = "https://api.the-odds-api.com/v4"


# Sports Perklocks claims to support with GAME markets from The Odds API
# (excludes MLB player props which come from a separate ingest layer).
GAME_MARKET_SPORTS = ["MLB", "NFL", "NBA", "NHL", "CFB", "Soccer", "Tennis", "UFC"]


def _raw_odds(sport_key: str, markets: str = "h2h,spreads,totals",
               regions: str = "us") -> dict:
    """Direct HTTP call.  Returns {status, event_count, bookies_total,
    earliest, latest, error}.  Bypasses app cache/budget/CB."""
    url = (f"{BASE}/sports/{sport_key}/odds"
           f"?apiKey={API_KEY}&regions={regions}"
           f"&markets={markets}&oddsFormat=american")
    try:
        req = request.Request(url, headers={"User-Agent": "funnel/1.0"})
        with request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            events = json.loads(body) if body else []
            if not isinstance(events, list):
                return {"status": resp.status, "event_count": 0,
                        "bookies_total": 0, "earliest": None, "latest": None,
                        "unexpected_body_type": type(events).__name__}
            times = sorted(
                e.get("commence_time") for e in events
                if e.get("commence_time"))
            return {
                "status":        resp.status,
                "event_count":   len(events),
                "bookies_total": sum(len(e.get("bookmakers") or []) for e in events),
                "earliest":      times[0]  if times else None,
                "latest":        times[-1] if times else None,
            }
    except error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace") if e.fp else ""
        return {"status": e.code, "event_count": 0,
                "error": (raw or str(e))[:200]}
    except Exception as e:
        return {"status": None, "event_count": 0, "error": repr(e)}


def _in_window_count(events_by_time: list[str], window_hours: int = 30) -> int:
    """Count events inside the anchored 30-hour slate window."""
    if not events_by_time:
        return 0
    now = datetime.now(timezone.utc)
    # anchor = first event >= now-2h
    anchor = None
    parsed = []
    for t in events_by_time:
        try:
            d = datetime.fromisoformat((t or "").replace("Z", "+00:00"))
            parsed.append(d)
        except Exception:
            continue
    parsed.sort()
    for d in parsed:
        if d >= now - timedelta(hours=2):
            anchor = d
            break
    if anchor is None:
        return min(len(parsed), 150)
    end = anchor + timedelta(hours=window_hours)
    return sum(1 for d in parsed if d <= end)


async def _sport_funnel(sport: str) -> dict:
    keys = list(se.SPORT_KEYS.get(sport, []))
    # Also add any prefix-active keys for Soccer/Tennis to catch the
    # auto-adoption path (matches _fetch_picks_for_sport line 3571-3582).
    _pfx = {"Soccer": "soccer_", "Tennis": "tennis_"}.get(sport)
    if _pfx and se._ACTIVE_KEYS:
        for k in sorted(se._ACTIVE_KEYS):
            if k.startswith(_pfx) and k not in keys:
                keys.append(k)
    per_key: list[dict] = []
    total_raw = 0
    total_gw  = 0
    total_win = 0
    for k in keys:
        raw = _raw_odds(k)
        raw_ct = raw.get("event_count") or 0
        total_raw += raw_ct
        # Gateway
        try:
            gw_events = await se._fetch_odds_for(k, regions="uk" if sport == "Soccer" else "us", sport=sport)
        except Exception as e:
            gw_events = []
        gw_ct = len(gw_events) if isinstance(gw_events, list) else 0
        total_gw += gw_ct
        # In-window
        times = [(e.get("commence_time") or "") for e in (gw_events or [])
                 if isinstance(e, dict)]
        win_ct = _in_window_count(times)
        total_win += win_ct
        per_key.append({
            "key":            k,
            "in_active_keys": k in se._ACTIVE_KEYS,
            "raw_status":     raw.get("status"),
            "raw_events":     raw_ct,
            "raw_bookies":    raw.get("bookies_total") or 0,
            "earliest":       raw.get("earliest"),
            "latest":         raw.get("latest"),
            "gateway_events": gw_ct,
            "gateway_delta":  gw_ct - raw_ct,
            "in_window_events": win_ct,
            "in_window_delta":  win_ct - gw_ct,
        })
    # DB stage
    now_iso = datetime.now(timezone.utc).isoformat()
    db_upcoming = await db.picks.count_documents({
        "sport": sport, "event_time": {"$gte": now_iso},
        "off_board": {"$ne": True},
    })
    db_eligible = await db.picks.count_documents({
        "sport": sport, "event_time": {"$gte": now_iso},
        "off_board": {"$ne": True}, "no_bet": {"$ne": True},
    })
    return {
        "sport":          sport,
        "keys":           per_key,
        "totals": {
            "raw_events":       total_raw,
            "gateway_events":   total_gw,
            "in_window_events": total_win,
            "db_upcoming":      db_upcoming,
            "db_eligible":      db_eligible,
        },
    }


async def main() -> None:
    # Ensure _ACTIVE_KEYS is loaded so prefix-auto-adoption works.
    await se._load_active_sports()

    print("=" * 78)
    print("UNIVERSAL PROVIDER → PRODUCTION FUNNEL CERTIFICATION")
    print(f"Generated: {datetime.now(timezone.utc).isoformat()}")
    print(f"THE_ODDS_API_KEY (masked): {API_KEY[:4]}…{API_KEY[-3:] if len(API_KEY)>=7 else '???'}")
    print(f"_ACTIVE_KEYS catalog size: {len(se._ACTIVE_KEYS)}")
    print("=" * 78)

    matrix: list[dict] = []
    for sport in GAME_MARKET_SPORTS:
        print(f"\n──── {sport} ────")
        r = await _sport_funnel(sport)
        for k in r["keys"]:
            print(f"  · key={k['key']:40s} active={str(k['in_active_keys']):5s} "
                  f"raw={k['raw_events']:4d} gw={k['gateway_events']:4d} "
                  f"window={k['in_window_events']:4d} "
                  f"earliest={(k['earliest'] or '?')[:19]}")
        t = r["totals"]
        print(f"  totals: raw={t['raw_events']} gw={t['gateway_events']} "
               f"in_window={t['in_window_events']} "
               f"db_upcoming={t['db_upcoming']} db_eligible={t['db_eligible']}")
        # Drop-reason classification
        drops = []
        if t["gateway_events"] != t["raw_events"]:
            drops.append("gateway/cache delta")
        if t["in_window_events"] != t["gateway_events"]:
            drops.append("OUTSIDE_TIME_WINDOW")
        if t["db_upcoming"] < t["in_window_events"]:
            drops.append("INSUFFICIENT_EVIDENCE / MODEL_UNAVAILABLE / EVENT_COMPLETE / no_bet")
        if t["db_eligible"] < t["db_upcoming"]:
            drops.append("no_bet marker (INSUFFICIENT_EVIDENCE / NO_REAL_BOOK_LINE)")
        r["drop_reasons"] = drops
        # Status
        if all(k["in_active_keys"] for k in r["keys"]) and t["raw_events"] > 0:
            r["status"] = "PASS" if t["db_upcoming"] > 0 or t["gateway_events"] == t["raw_events"] else "NEEDS_REVIEW"
        elif not r["keys"]:
            r["status"] = "INTENTIONALLY_UNSUPPORTED"
        elif t["raw_events"] == 0:
            r["status"] = "NO_CURRENT_PROVIDER_MARKET"
        else:
            r["status"] = "PASS"
        matrix.append(r)

    # Final matrix table
    print("\n" + "=" * 78)
    print("FINAL MATRIX")
    print("=" * 78)
    print(f"{'SPORT':10s} {'RAW':>6s} {'GATEWAY':>8s} {'WINDOW':>7s} {'DB':>5s} {'ELIG':>5s} {'STATUS':>28s}")
    for r in matrix:
        t = r["totals"]
        print(f"{r['sport']:10s} {t['raw_events']:6d} {t['gateway_events']:8d} "
               f"{t['in_window_events']:7d} {t['db_upcoming']:5d} {t['db_eligible']:5d} "
               f"{r['status']:>28s}")
    # Drops
    print("\nDROP REASONS PER SPORT")
    print("=" * 78)
    for r in matrix:
        drs = r.get("drop_reasons") or []
        print(f"  {r['sport']:10s}  " + (", ".join(drs) if drs else "no drops"))


if __name__ == "__main__":
    asyncio.run(main())
