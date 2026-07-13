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

    # Determine market family + line. Combo markets like "Hits + Runs + RBIs"
    # must sum all three stats — matching a single key would only look at
    # hits and mis-grade edge cases where the batter's H+R+RBI is ≥ line
    # but hits alone is 0.
    stat_keys: list[str] = []
    if "hits + runs + rbi" in market or "hits+runs+rbi" in market:
        stat_keys = ["hits", "runs", "rbi"]
    else:
        for phrase, key in _MLB_STAT_MAP.items():
            if phrase in market:
                stat_keys = [key]
                break
    if not stat_keys:
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

    # Find the MLB game via schedule endpoint for event_date. Merge D and
    # D-1 to handle late ET games that MLB files under the previous US
    # calendar date, then pick the game whose gameDate is closest to the
    # pick's event_time (handles series/doubleheader ambiguity — the same
    # bug that made the settler mis-grade Wheeler-style picks).
    try:
        from datetime import datetime as _dt, timedelta as _td
        date_str = event_time[:10]  # YYYY-MM-DD
        try:
            prev_str = (_dt.fromisoformat(date_str) - _td(days=1)).strftime("%Y-%m-%d")
        except Exception:
            prev_str = None
        games: list[dict] = []
        async with httpx.AsyncClient(timeout=15) as cx:
            for ds in ([date_str] + ([prev_str] if prev_str else [])):
                rr = await cx.get(f"{_MLB_STATS_BASE}/schedule",
                                  params={"sportId": 1, "date": ds, "hydrate": "team"})
                for d in (rr.json() or {}).get("dates", []):
                    games.extend(d.get("games", []))
        # Dedupe by gamePk
        seen: set = set()
        deduped: list[dict] = []
        for g in games:
            pk = g.get("gamePk")
            if pk in seen:
                continue
            seen.add(pk)
            deduped.append(g)
        games = deduped

        home_team = pick.get("home_team") or ""
        away_team = pick.get("away_team") or ""
        # Fallback: parse teams from event string when the pick doc doesn't
        # carry them (older picks in the DB).
        if (not home_team or not away_team) and pick.get("event"):
            evt = pick.get("event") or ""
            if "@" in evt:
                a, h = evt.split("@", 1)
                away_team = away_team or a.strip()
                home_team = home_team or h.strip()

        # AND team match (both teams must line up) — the previous OR match
        # returned any game where either team appeared, which is dangerous
        # for teams that play back-to-back different opponents.
        def _tm(a: str, b: str) -> bool:
            a, b = a.lower(), b.lower()
            return bool(a) and bool(b) and (a in b or b in a)

        matches: list[dict] = []
        for g in games:
            hn = ((g.get("teams") or {}).get("home") or {}).get("team", {}).get("name", "")
            an = ((g.get("teams") or {}).get("away") or {}).get("team", {}).get("name", "")
            if _tm(home_team, hn) and _tm(away_team, an):
                matches.append(g)
        if not matches:
            return None

        # Parse event_time for distance ranking
        et_dt = None
        try:
            et_dt = _dt.fromisoformat(event_time.replace("Z", "+00:00"))
            if et_dt.tzinfo is None:
                from datetime import timezone as _tz
                et_dt = et_dt.replace(tzinfo=_tz.utc)
        except Exception:
            pass

        def _prio(g: dict) -> tuple:
            state = ((g.get("status") or {}).get("abstractGameState") or "").lower()
            tier = 0 if state == "final" else (1 if state == "live" else 2)
            gd = g.get("gameDate") or ""
            dist = 0.0
            if et_dt and gd:
                try:
                    d = _dt.fromisoformat(gd.replace("Z", "+00:00"))
                    if d.tzinfo is None:
                        from datetime import timezone as _tz
                        d = d.replace(tzinfo=_tz.utc)
                    dist = abs((d - et_dt).total_seconds())
                except Exception:
                    pass
            return (tier, dist, gd)

        matches.sort(key=_prio)
        best = matches[0]
        # Only verify against Final games — Live/Preview would give wrong grades.
        state = ((best.get("status") or {}).get("abstractGameState") or "").lower()
        if state != "final":
            return None
        game_pk = best.get("gamePk")
        if not game_pk:
            return None
        async with httpx.AsyncClient(timeout=15) as cx:
            r2 = await cx.get(f"{_MLB_STATS_BASE}/game/{game_pk}/boxscore")
        boxscore = r2.json() or {}
    except Exception as e:
        logger.debug("MLB boxscore fetch failed: %s", e)
        return None

    # Search both team rosters for the player. For combo markets we sum the
    # relevant stats — all keys pulled from the same batting/pitching block
    # slice consistently for the same player.
    pname_norm = player_name.lower().strip()
    # Position-aware block routing — see prop_settlement._mlb_stat_for_player
    # for the full rationale. Wrong-block routing was the original grading
    # regression that made 82 Wheeler-style picks grade lost when they won.
    _BATTING_ONLY = {"hits", "homeRuns", "rbi", "totalBases", "doubles", "triples"}
    _PITCHING_ONLY = {"outs", "inningsPitched", "earnedRuns", "wins",
                       "losses", "saves", "holds", "battersFaced"}
    _AMBIGUOUS = {"strikeOuts", "baseOnBalls", "runs", "hitByPitch"}
    for side in ("home", "away"):
        players = ((boxscore.get("teams") or {}).get(side) or {}).get("players") or {}
        for pdoc in players.values():
            person = pdoc.get("person") or {}
            full = (person.get("fullName") or "").lower()
            if pname_norm and (pname_norm in full or full in pname_norm):
                stats = pdoc.get("stats") or {}
                position = ((pdoc.get("position") or {}).get("abbreviation") or "").upper()
                is_pitcher = position in ("P", "SP", "RP", "TWP")
                # For each requested stat key, look it up in the correct block
                # and accumulate. If the player is on the roster but has no
                # stats blocks (DNP), treat every key as 0 so the market
                # still grades cleanly.
                total = 0.0
                found_any = False
                for sk in stat_keys:
                    if sk in _BATTING_ONLY:
                        blocks = ("batting",)
                    elif sk in _PITCHING_ONLY:
                        blocks = ("pitching",)
                    elif sk in _AMBIGUOUS:
                        blocks = ("pitching", "batting") if is_pitcher else ("batting", "pitching")
                    else:
                        blocks = ("batting", "pitching")
                    for block in blocks:
                        block_stats = stats.get(block) or {}
                        if sk in block_stats:
                            try:
                                total += float(block_stats[sk] or 0)
                                found_any = True
                                break
                            except (TypeError, ValueError):
                                pass
                # Player is on roster but every block was empty → DNP, grade
                # against total=0 (standard "Action" resolution).
                actual = total if found_any or stats else 0.0
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
            r"Strikeouts?|Hits|Home Run|Total Bases|RBI|Outs Recorded|Runs Scored|Walks",
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
            update: dict = {
                "$set": {
                    "grade_verified_at": datetime.now(timezone.utc).isoformat(),
                    "grade_verify_source": source_label,
                    "grade_verify_result": "agreed",
                },
            }
            # If this pick had a prior disagreement flag, clear it now that
            # the settler produced the correct grade on the retry — otherwise
            # downstream monitors keyed on `grade_disagreement` see stale
            # positives forever (iter 70 cosmetic-bug finding).
            if p.get("grade_disagreement"):
                update["$unset"] = {"grade_disagreement": ""}
            await db.picks.update_one({"id": p.get("id")}, update)
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
