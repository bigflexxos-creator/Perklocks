"""
UEA LIVE-WIRE DISTRIBUTION — reports exactly what /api/picks/today
serves to the frontend Locks board.

This is the SAME truth the user's Locks screen consumes, not a raw
picks-collection dump.  It logs into the running backend as the
demo user and hits ``/api/picks/today`` per-sport (or all-sports)
so canonicalization / dedupe / in-play window / -1000 admission /
board-floor filters are applied identically to the frontend.

Usage:
    cd /app/backend
    python -m services.uea_live_wire_distribution
"""
from __future__ import annotations
import asyncio, json, os, sys
from collections import defaultdict
from typing import Any

import httpx


BACKEND = os.environ.get("BACKEND_URL", "http://localhost:8001")
EMAIL   = os.environ.get("REPORT_EMAIL", "demo@lockscore.ai")
PASS    = os.environ.get("REPORT_PASSWORD", "demo123")

TIER_BUCKETS = [
    ("85_89", 85.0, 89.999),
    ("90_92", 90.0, 92.999),
    ("93_95", 93.0, 95.999),
    ("96_97", 96.0, 97.999),
    ("98",    98.0, 98.999),
    ("99",    99.0, 99.999),
    ("100",  100.0, 100.001),
]


def _bkt(ls: float) -> str:
    for name, lo, hi in TIER_BUCKETS:
        if lo <= ls <= hi:
            return name
    if ls < 85:
        return "below_85"
    return "unknown"


def _family(sport: str, market: str) -> str:
    s = (sport or "").upper()
    m = (market or "").lower()
    if s == "NFL":
        if any(t in m for t in ("yards", "yds", "reception", "completions",
                                  "attempts", "touchdowns", "tds", "atd",
                                  "anytime", "longest", "player ",
                                  "pass ", "rush ")):
            return "NFL_PLAYER"
        return "NFL_GAME"
    if s == "MLB":
        if any(t in m for t in ("strikeouts", "outs", "walks",
                                  "earned runs", "pitcher")):
            return "MLB_PITCHER"
        if any(t in m for t in ("hits", "home run", "total bases",
                                  "rbi", "singles", "doubles",
                                  "triples", "stolen")):
            return "MLB_HITTER"
        return "MLB_GAME"
    if s == "CFB":
        return "CFB_GAME" if any(t in m for t in (
            "moneyline", "spread", "total")) else "CFB_PLAYER"
    if s == "SOCCER":
        if "goal" in m and "scorer" in m:
            return "SOCCER_GOALSCORER"
        if "sot" in m or "shots on target" in m:
            return "SOCCER_SOT"
        if "shot" in m:
            return "SOCCER_SHOTS"
        if "assist" in m:
            return "SOCCER_ASSISTS"
        if "to score or assist" in m:
            return "SOCCER_GOALSCORER"
        if any(t in m for t in ("player ", "score anytime")):
            return "SOCCER_PLAYER"
        return "SOCCER_GAME"
    if s == "TENNIS":
        m_l = m
        if "spread" in m_l or "handicap" in m_l:
            return "TENNIS_SPREAD"
        if "total" in m_l or "games" in m_l:
            return "TENNIS_TOTAL"
        return "TENNIS_ML"
    return "UNKNOWN"


async def _login(client: httpx.AsyncClient) -> str:
    r = await client.post(f"{BACKEND}/api/auth/login",
                            json={"email": EMAIL, "password": PASS})
    r.raise_for_status()
    return r.json()["access_token"]


async def _fetch_sport(client: httpx.AsyncClient, token: str,
                        sport: str) -> list[dict]:
    picks: list[dict] = []
    # /api/picks/today has no pagination — one call returns the
    # canonical current-slate universe.
    r = await client.get(f"{BACKEND}/api/picks/today",
                          params={"sport": sport, "lite": "false"},
                          headers={"Authorization": f"Bearer {token}"},
                          timeout=60.0)
    r.raise_for_status()
    payload = r.json()
    items = payload.get("picks") or payload.get("items") or payload or []
    if isinstance(items, dict):
        items = items.get("picks") or []
    for p in items:
        if isinstance(p, dict):
            picks.append(p)
    return picks


async def build_wire_report() -> dict:
    async with httpx.AsyncClient(timeout=90.0) as client:
        token = await _login(client)
        report: dict[str, dict] = {}
        for sport in ("MLB", "NFL", "CFB", "Soccer", "Tennis"):
            picks = await _fetch_sport(client, token, sport)
            for p in picks:
                sp = (p.get("sport") or sport).upper()
                fam = _family(sp, p.get("market") or "")
                entry = report.setdefault(sp, {}).setdefault(fam, {
                    "total": 0, "max_ls": 0.0, "max_pick": None,
                    "buckets": {b[0]: 0 for b in TIER_BUCKETS},
                    "below_85": 0,
                    "peak_provenance_98_99": [],
                })
                ls = float(p.get("lock_score") or 0.0)
                entry["total"] += 1
                b = _bkt(ls)
                if b == "below_85":
                    entry["below_85"] += 1
                else:
                    entry["buckets"][b] = entry["buckets"].get(b, 0) + 1
                if ls > entry["max_ls"]:
                    entry["max_ls"] = ls
                    entry["max_pick"] = {
                        "market": p.get("market"),
                        "selection": p.get("selection"),
                        "line": p.get("line"),
                        "book_odds": p.get("book_odds"),
                        "sportsbook": p.get("sportsbook"),
                        "wp": p.get("win_probability"),
                        "provenance": p.get("probability_provenance"),
                        "canonical_pick_id": p.get("canonical_pick_id")
                            or p.get("id") or p.get("_id"),
                        "publication_state": p.get("publication_state"),
                        "off_board": p.get("off_board"),
                        "uea_ceiling": (p.get("evidence_authority") or {}).get("ceiling"),
                        "uea_coverage": (p.get("evidence_authority") or {}).get("coverage"),
                        "reliability_cap_scoped": p.get("reliability_cap_scoped"),
                    }
                if ls >= 98.0:
                    entry["peak_provenance_98_99"].append({
                        "market": p.get("market"),
                        "ls":     ls,
                        "wp":     p.get("win_probability"),
                        "uea":    p.get("evidence_authority"),
                        "peak_non_apex_eligible": p.get("peak_non_apex_eligible"),
                        "peak_non_apex_denied_reason": p.get("peak_non_apex_denied_reason"),
                    })

        return {"generated_at": __import__("datetime").datetime.utcnow().isoformat(),
                 "backend": BACKEND,
                 "distribution": report}


def main() -> None:
    async def _run():
        r = await build_wire_report()
        print(json.dumps(r, indent=2, default=str))
    asyncio.run(_run())


if __name__ == "__main__":
    main()
