"""Soccer Rolling 10-match Team xG Window — Phase 2 (2026-07-19).

Extends the point-in-time ``soccer_form`` (wins/draws/losses/goals)
with an xG-quality view: how many goals a team should have scored
(xG) and conceded (xGA) over the last 10 competitive matches.

Rolling xG is a much better forward predictor of scoring than actual
goals — actual goals are noisy in a 10-match window (a hot striker
can flatter a bad team; a cold striker can hide a good one). xG
removes the finishing luck.

Data sources (all free / no key):
  • Understat (for the top-5 EU leagues + Russia + MLS) via the
    already-installed ``understat`` pipeline.
  • FBref match logs (fallback for leagues Understat doesn't cover).

This module doesn't do the actual fetch — it just:
  1. Provides ``enrich_pick_with_rolling_xg`` which reads a pre-
     populated ``xg_rolling`` field from Mongo (``soccer_team_xg``
     collection, keyed by canonical team name).
  2. Attaches ``pick['xg_rolling']`` = {'home': {...}, 'away': {...}}
     with fields: xg_avg, xga_avg, xg_diff, matches, updated_at.

A separate periodic job (see ``scripts/refresh_soccer_xg.py`` — added
as an optional background task in server startup) is responsible for
refreshing the DB collection every ~6 hours during matchday windows.
This keeps the request path 100% read-only and blocking-free.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("lockscore.soccer_rolling_xg")


_COLL = "soccer_team_xg"


async def _lookup(db, team: str) -> Optional[dict[str, Any]]:
    if not team:
        return None
    try:
        doc = await db[_COLL].find_one(
            {"team_norm": team.strip().lower()},
            {"_id": 0, "xg_avg": 1, "xga_avg": 1,
             "xg_diff": 1, "matches": 1, "updated_at": 1},
        )
    except Exception as e:
        logger.debug("soccer xg lookup failed for %r: %s", team, e)
        return None
    return doc


def _extract_teams(pick: dict) -> tuple[Optional[str], Optional[str]]:
    home = pick.get("home_team")
    away = pick.get("away_team")
    if home and away:
        return str(home), str(away)
    ev = pick.get("event") or ""
    if " @ " in ev:
        a, h = ev.split(" @ ", 1)
        return h.strip(), a.strip()
    if " vs " in ev:
        a, h = ev.split(" vs ", 1)
        return h.strip(), a.strip()
    return None, None


async def enrich_pick_with_rolling_xg(db, pick: dict) -> dict:
    """Attach ``pick['xg_rolling']`` = {'home': {...}, 'away': {...}}.

    - No-op for non-soccer picks.
    - Silently skips missing teams — signal calculator handles absence.
    - Idempotent — if ``pick['xg_rolling']`` is already set, returns.

    Two-tier lookup:
      1. Preferred: dedicated ``soccer_team_xg`` collection (real xG from
         Understat / FBref, refreshed by a periodic job).
      2. Fallback: derive a proxy from the ``soccer_form`` block already
         attached by the multi-source soccer cache (gf_avg / ga_avg).
         This is a goals-based proxy, not real xG, but it's directionally
         correct and lets the Phase 2 signal fire immediately without
         waiting on a background scraper.
    """
    if (pick.get("sport") or "").lower() != "soccer":
        return pick
    if pick.get("xg_rolling"):
        return pick
    home, away = _extract_teams(pick)
    if not home or not away:
        return pick

    h_doc = None
    a_doc = None
    # Tier 1: dedicated xG collection (best data if present).
    if db is not None:
        h_doc = await _lookup(db, home)
        a_doc = await _lookup(db, away)

    # Tier 2: fallback to goals-average proxy from soccer_form.
    form = pick.get("soccer_form") or {}
    if not h_doc:
        hf = form.get("home") or {}
        if isinstance(hf.get("gf_avg"), (int, float)) and hf.get("n_matches", 0) >= 5:
            h_doc = {
                "xg_avg":  float(hf["gf_avg"]),
                "xga_avg": float(hf.get("ga_avg") or 0.0),
                "xg_diff": float(hf["gf_avg"]) - float(hf.get("ga_avg") or 0.0),
                "matches": int(hf.get("n_matches") or 0),
                "source":  "form_proxy",
            }
    if not a_doc:
        af = form.get("away") or {}
        if isinstance(af.get("gf_avg"), (int, float)) and af.get("n_matches", 0) >= 5:
            a_doc = {
                "xg_avg":  float(af["gf_avg"]),
                "xga_avg": float(af.get("ga_avg") or 0.0),
                "xg_diff": float(af["gf_avg"]) - float(af.get("ga_avg") or 0.0),
                "matches": int(af.get("n_matches") or 0),
                "source":  "form_proxy",
            }

    if not h_doc and not a_doc:
        return pick
    pick["xg_rolling"] = {
        "home": h_doc, "away": a_doc,
    }
    return pick
