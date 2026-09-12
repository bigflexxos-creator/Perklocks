"""RAW ODDS API VERIFICATION — CFB (2026-06-12).

Read-only. Bypasses app cache/budget/circuit-breaker.  Direct HTTP
calls to https://api.the-odds-api.com/v4/*.

Prints exact provider response (HTTP status, event counts, quota
headers, sample events) for:
  A. GET /v4/sports              → is FBS/FCS active?
  B. GET /v4/sports/americanfootball_ncaaf/odds
  C. GET /v4/sports/americanfootball_ncaaf_fcs/odds

API key masked in output.

Run:
    cd /app/backend && python -m scripts.diagnostics.cfb_odds_api_raw_probe
"""
from __future__ import annotations

import json
import os
import sys
from urllib import request, error

sys.path.insert(0, "/app/backend")

from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

API_KEY = os.getenv("THE_ODDS_API_KEY") or ""
BASE    = "https://api.the-odds-api.com/v4"
MASKED  = f"{API_KEY[:4]}…{API_KEY[-3:]}" if len(API_KEY) >= 8 else "<MISSING>"


def _get(url: str, timeout: int = 15) -> dict:
    """Perform GET and return {status, headers, body_json, body_len,
    error, url_masked}."""
    display = url.replace(API_KEY, "<APIKEY>") if API_KEY else url
    try:
        req = request.Request(url, headers={"User-Agent": "cfb-raw-probe/1.0"})
        with request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            headers = dict(resp.headers.items())
            try:
                parsed = json.loads(body) if body else None
            except json.JSONDecodeError as e:
                parsed = {"__parse_error": str(e), "__raw_first_200": body[:200]}
            return {
                "url": display,
                "status": resp.status,
                "quota_remaining": headers.get("X-Requests-Remaining"),
                "quota_used":      headers.get("X-Requests-Used"),
                "quota_last_cost": headers.get("X-Requests-Last"),
                "content_length":  len(body),
                "body": parsed,
            }
    except error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace") if e.fp else ""
        try:
            parsed = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            parsed = raw[:500]
        return {
            "url": display, "status": e.code,
            "quota_remaining": e.headers.get("X-Requests-Remaining") if e.headers else None,
            "content_length": len(raw), "error": str(e), "body": parsed,
        }
    except Exception as e:
        return {"url": display, "status": None, "error": repr(e)}


def _sports_summary(entries: list, keys: list[str]) -> dict:
    matches = {}
    if not isinstance(entries, list):
        return matches
    for row in entries:
        k = row.get("key")
        if k in keys:
            matches[k] = {
                "key":    row.get("key"),
                "active": row.get("active"),
                "title":  row.get("title"),
                "group":  row.get("group"),
                "description": row.get("description"),
                "has_outrights": row.get("has_outrights"),
            }
    return matches


def _events_summary(events: list) -> dict:
    if not isinstance(events, list):
        return {"__unexpected_body_type": type(events).__name__}
    total = len(events)
    with_bookies = [e for e in events if e.get("bookmakers")]
    bookies_total = sum(len(e.get("bookmakers") or []) for e in events)
    first5 = []
    for e in events[:5]:
        first5.append({
            "id":            e.get("id"),
            "commence_time": e.get("commence_time"),
            "home_team":     e.get("home_team"),
            "away_team":     e.get("away_team"),
            "bookmakers_n":  len(e.get("bookmakers") or []),
            "markets_first_bookie": [
                m.get("key") for m in
                ((e.get("bookmakers") or [{}])[0].get("markets") or [])
            ][:6],
        })
    return {
        "event_count_total":         total,
        "event_count_with_bookies":  len(with_bookies),
        "bookmakers_total_across":   bookies_total,
        "first_5_events":            first5,
    }


def main() -> None:
    if not API_KEY:
        print("❌ THE_ODDS_API_KEY missing from /app/backend/.env — abort")
        sys.exit(1)

    print("=" * 78)
    print(f"THE_ODDS_API_KEY (masked): {MASKED}")
    print("=" * 78)

    # ── A · discovery ────────────────────────────────────────────────
    print("\n──── A · GET /v4/sports ────")
    a = _get(f"{BASE}/sports?apiKey={API_KEY}")
    print(json.dumps({
        "url":              a.get("url"),
        "status":           a.get("status"),
        "quota_remaining":  a.get("quota_remaining"),
        "quota_used":       a.get("quota_used"),
        "content_length":   a.get("content_length"),
        "total_sports":     (len(a.get("body")) if isinstance(a.get("body"), list) else "n/a"),
        "cfb_matches":      _sports_summary(
            a.get("body") or [],
            ["americanfootball_ncaaf", "americanfootball_ncaaf_fcs"],
        ),
        "error":            a.get("error"),
    }, indent=2, default=str))

    # ── B · FBS ─────────────────────────────────────────────────────
    print("\n──── B · GET /v4/sports/americanfootball_ncaaf/odds ────")
    url_fbs = (f"{BASE}/sports/americanfootball_ncaaf/odds"
               f"?apiKey={API_KEY}"
               "&regions=us&markets=h2h,spreads,totals&oddsFormat=american")
    b = _get(url_fbs)
    print(json.dumps({
        "url":              b.get("url"),
        "status":           b.get("status"),
        "quota_remaining":  b.get("quota_remaining"),
        "quota_used":       b.get("quota_used"),
        "quota_last_cost":  b.get("quota_last_cost"),
        "content_length":   b.get("content_length"),
        "summary":          _events_summary(b.get("body") or []),
        "error":            b.get("error"),
    }, indent=2, default=str))

    # ── C · FCS ─────────────────────────────────────────────────────
    print("\n──── C · GET /v4/sports/americanfootball_ncaaf_fcs/odds ────")
    url_fcs = (f"{BASE}/sports/americanfootball_ncaaf_fcs/odds"
               f"?apiKey={API_KEY}"
               "&regions=us&markets=h2h,spreads,totals&oddsFormat=american")
    c = _get(url_fcs)
    print(json.dumps({
        "url":              c.get("url"),
        "status":           c.get("status"),
        "quota_remaining":  c.get("quota_remaining"),
        "quota_used":       c.get("quota_used"),
        "quota_last_cost":  c.get("quota_last_cost"),
        "content_length":   c.get("content_length"),
        "summary":          _events_summary(c.get("body") or []),
        "error":            c.get("error"),
    }, indent=2, default=str))


if __name__ == "__main__":
    main()
