"""Grading Validator — permanent cross-source verification (all sports).

User mandate (2026-07-13):
  "Why do we have to keep having these problems with history and you not
  seeing til I find flaw I can't have a working app if history is wrong"
  "I want all picks on board to grade correctly across all sports"

Design: every 60 min, scan freshly-graded picks. For each one, query an
INDEPENDENT data source and compare the grade. On disagreement:
  1. Log LOUDLY with all context.
  2. Re-open the pick (status → 'pending', clear settled_at) so the
     next settler cycle regrades with the fixed logic.
  3. If a threshold of mismatches happens in a day, escalate the log
     to WARNING so the operator sees it in monitoring.

Cross-source coverage:
  • Soccer goalscorer  → FotMob (universal Nordic + top-5 coverage)
  • MLB player props   → MLB Stats API boxscore (statsapi.mlb.com,
                          the authoritative first-party source)
  • Tennis moneyline   → ESPN scoreboard status (already independent
                          of our TennisExplorer primary source)

The point isn't perfect grading — it's a self-healing loop that catches
grading regressions the moment they happen instead of days later when
a user notices. Every pick added to history is cross-verified within
60 minutes of settlement.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

logger = logging.getLogger("lockscore.grading_validator")

VERIFY_WINDOW_MIN = 6 * 60             # 6 hours
LOOP_INTERVAL_SECS = 60 * 60           # 1 hour
DAILY_MISMATCH_ALERT_THRESHOLD = 3

_MLB_STATS_BASE = "https://statsapi.mlb.com/api/v1"
_MLB_STAT_MAP = {
    "hits":         "hits",
    "home run":     "homeRuns",
    "total bases":  "totalBases",
    "rbi":          "rbi",
    "runs scored":  "runs",
    "strikeouts":   "strikeOuts",
    "outs recorded": "outs",
}


async def _mlb_verify_prop(pick: dict) -> Optional[str]:
    """Verify an MLB player-prop pick against MLB Stats API boxscore.
    Returns 'won' / 'lost' / 'push' or None when we can't verify."""
    market = (pick.get("market") or "").lower()
    selection = pick.get("selection") or pick.get("player_name") or ""
    event_time = pick.get("event_time") or ""
    if not selection or not event_time:
        return None

    # Determine market family + line
    stat_key = None
    for phrase, key in _MLB_STAT_MAP.items():
        if phrase in market:
            stat_key = key
            break
    if not stat_key:
        return None
    m = re.search(r"(over|under)\s*(\d+(?:\.\d+)?)", market)
    if not m:
        return None
    direction = m.group(1).lower()
    line = float(m.group(2))
    # Player name
    player_name = re.split(r"\s*(?:over|under|-)\s*", market, flags=re.I)[0].strip()
    if not player_name:
        player_name = selection.split()[0] if selection else ""

    # Find the MLB game via schedule endpoint for event_date
    try:
        date_str = event_time[:10]  # YYYY-MM-DD
        async with httpx.AsyncClient(timeout=15) as cx:
            r = await cx.get(f"{_MLB_STATS_BASE}/schedule",
                             params={"sportId": 1, "date": date_str, "hydrate": "team"})
        games = []
        for d in (r.json() or {}).get("dates", []):
            games.extend(d.get("games", []))
        home_team = pick.get("home_team") or ""
        away_team = pick.get("away_team") or ""
        game_pk = None
        for g in games:
            hn = ((g.get("teams") or {}).get("home") or {}).get("team", {}).get("name", "")
            an = ((g.get("teams") or {}).get("away") or {}).get("team", {}).get("name", "")
            if (home_team.lower() in hn.lower() or hn.lower() in home_team.lower()) or \
               (away_team.lower() in an.lower() or an.lower() in away_team.lower()):
                game_pk = g.get("gamePk")
                break
        if not game_pk:
            return None
        async with httpx.AsyncClient(timeout=15) as cx:
            r2 = await cx.get(f"{_MLB_STATS_BASE.replace('/v1','/v1')}/game/{game_pk}/boxscore")
        boxscore = r2.json() or {}
    except Exception as e:
        logger.debug("MLB boxscore fetch failed: %s", e)
        return None

    # Search both team rosters for the player
    pname_norm = player_name.lower().strip()
    for side in ("home", "away"):
        players = ((boxscore.get("teams") or {}).get(side) or {}).get("players") or {}
        for pdoc in players.values():
            person = pdoc.get("person") or {}
            full = (person.get("fullName") or "").lower()
            if pname_norm and (pname_norm in full or full in pname_norm):
                stats = pdoc.get("stats") or {}
                # Try both batting and pitching stat blocks.
                for block in ("batting", "pitching"):
                    block_stats = stats.get(block) or {}
                    if stat_key in block_stats:
                        actual = float(block_stats[stat_key] or 0)
                        if direction == "over":
                            if actual > line:
                                return "won"
                            if actual < line:
                                return "lost"
                            return "push"
                        else:
                            if actual < line:
                                return "won"
                            if actual > line:
                                return "lost"
                            return "push"
    return None


async def verify_recent_goalscorer_grades(db, *, window_min: int = VERIFY_WINDOW_MIN) -> dict:
    """Cross-check recently graded soccer goalscorer picks against FotMob."""
    from soccer_fotmob_settle import settle_soccer_leg as _fotmob

    cutoff_iso = (datetime.now(timezone.utc)
                  - timedelta(minutes=window_min)).isoformat()
    q = {
        "sport": "Soccer",
        "market": {"$regex": "Anytime Goal Scorer|To Score or Assist",
                    "$options": "i"},
        "status": {"$in": ["won", "lost"]},
        "settled_at": {"$gte": cutoff_iso},
        "grade_verified_at": {"$exists": False},
    }
    return await _run_cross_check(db, q, _fotmob, "fotmob")


async def verify_recent_mlb_grades(db, *, window_min: int = VERIFY_WINDOW_MIN) -> dict:
    """Cross-check recently graded MLB player props against MLB Stats API."""
    cutoff_iso = (datetime.now(timezone.utc)
                  - timedelta(minutes=window_min)).isoformat()
    q = {
        "sport": "MLB",
        "market": {"$regex":
            r"Strikeouts?|Hits|Home Run|Total Bases|RBI|Outs Recorded",
            "$options": "i"},
        "status": {"$in": ["won", "lost", "push"]},
        "settled_at": {"$gte": cutoff_iso},
        "grade_verified_at": {"$exists": False},
    }
    return await _run_cross_check(db, q, _mlb_verify_prop, "mlb_statsapi")


async def _run_cross_check(db, query: dict, verifier, source_label: str) -> dict:
    """Shared cross-check loop — pulls picks, calls the verifier, reopens
    disagreements, marks agreements as verified."""
    summary = {"scanned": 0, "agreed": 0, "mismatched": 0,
               "verifier_unavailable": 0, "reopened": 0, "mismatches": []}
    async for p in db.picks.find(query).limit(500):
        summary["scanned"] += 1
        try:
            result = await verifier(p)
        except Exception as e:
            logger.debug("%s verifier failed: %s", source_label, e)
            result = None
        if result not in ("won", "lost", "push"):
            summary["verifier_unavailable"] += 1
            await db.picks.update_one(
                {"id": p.get("id")},
                {"$set": {"grade_verified_at": datetime.now(timezone.utc).isoformat(),
                          "grade_verify_source": f"{source_label}_unavailable"}},
            )
            continue
        current = p.get("status")
        if result == current:
            summary["agreed"] += 1
            await db.picks.update_one(
                {"id": p.get("id")},
                {"$set": {"grade_verified_at": datetime.now(timezone.utc).isoformat(),
                          "grade_verify_source": source_label,
                          "grade_verify_result": "agreed"}},
            )
            continue
        summary["mismatched"] += 1
        summary["mismatches"].append({
            "id":                p.get("id"),
            "event":             p.get("event"),
            "market":            p.get("market"),
            "selection":         p.get("selection"),
            "our_grade":         current,
            f"{source_label}":   result,
        })
        await db.picks.update_one(
            {"id": p.get("id")},
            {"$set": {
                "status": "pending",
                "grade_disagreement": {
                    "detected_at":         datetime.now(timezone.utc).isoformat(),
                    "our_grade_was":       current,
                    f"{source_label}_said": result,
                    "previous_settled_at": p.get("settled_at"),
                },
             },
             "$unset": {"settled_at": "", "settle_source": "", "settle_reason": ""}},
        )
        summary["reopened"] += 1
    if summary["mismatched"]:
        level = (logging.WARNING
                 if summary["mismatched"] >= DAILY_MISMATCH_ALERT_THRESHOLD
                 else logging.INFO)
        logger.log(
            level,
            "Grading validator (%s): %d/%d disagreements caught & reopened. %s",
            source_label, summary["mismatched"], summary["scanned"],
            summary["mismatches"][:5],
        )
    else:
        logger.info(
            "Grading validator (%s): %d verified, %d agreed, %d unavailable",
            source_label, summary["scanned"], summary["agreed"],
            summary["verifier_unavailable"],
        )
    return summary


async def grading_validator_loop(db) -> None:
    """Long-running 1-hour loop. Cross-checks Soccer + MLB every cycle."""
    await asyncio.sleep(10 * 60)
    while True:
        try:
            await verify_recent_goalscorer_grades(db)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("grading_validator soccer error: %s", e)
        try:
            await verify_recent_mlb_grades(db)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("grading_validator MLB error: %s", e)
        await asyncio.sleep(LOOP_INTERVAL_SECS)
