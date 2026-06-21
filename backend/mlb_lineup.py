"""MLB Lineup Verification — auto-void picks for scratched players.

Runs ~30 min before first pitch and pulls the official starting lineup
from the FREE MLB Stats API. Any pick whose player is NOT in the lineup
gets auto-voided so it doesn't get marked as a loss when the player
sits out.

Why this matters:
  • MLB managers post lineups ~2h before first pitch.
  • Hitters get scratched for rest, minor injury, or platoon decisions.
  • Without this check, a "Aaron Judge Over 0.5 Hits" pick that the
    Yankees scratch becomes an automatic LOSS in our settlement engine.
  • Voiding instead of losing protects model performance metrics.

Strategy:
  • Every 5 min, find pending MLB player-prop picks with first-pitch
    within the next 90 min and ≥ -30 min (i.e. 30 min before to 90 min after start).
  • For each game, pull the lineup ONCE per game per refresh window.
  • Match pick player name against lineup. If not present → void.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger("lockscore.mlb_lineup")

_MLB_BASE = "https://statsapi.mlb.com/api/v1"
_TIMEOUT = 15.0
_PROP_TOKENS = ("hit", "home run", "rbi", "total base", "stolen base",
                "strikeout", "double", "triple", "single", "walk")


def _norm(name: str) -> str:
    if not name:
        return ""
    n = name.lower().strip()
    n = re.sub(r"\s+(jr|sr|ii|iii|iv)\.?\s*$", "", n)
    n = re.sub(r"[^a-z\s]", "", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


async def _schedule_today(cx: httpx.AsyncClient, date_str: str) -> list[dict]:
    r = await cx.get(f"{_MLB_BASE}/schedule",
                      params={"sportId": 1, "date": date_str, "hydrate": "lineups"})
    if r.status_code != 200:
        return []
    data = r.json()
    games: list[dict] = []
    for d in data.get("dates", []):
        games.extend(d.get("games", []))
    return games


def _lineup_names(game: dict) -> set[str]:
    """Return normalized player names from the game's lineups (both teams)."""
    names: set[str] = set()
    lineups = game.get("lineups") or {}
    for side in ("homePlayers", "awayPlayers"):
        for p in lineups.get(side) or []:
            full = ((p.get("fullName") or "")
                     or f"{(p.get('firstName') or '')} {(p.get('lastName') or '')}")
            n = _norm(full)
            if n:
                names.add(n)
    return names


def _is_player_prop(pick: dict) -> bool:
    m = (pick.get("market") or "").lower()
    return any(t in m for t in _PROP_TOKENS)


def _extract_player_name(pick: dict) -> Optional[str]:
    sel = (pick.get("selection") or "").strip()
    if not sel:
        return None
    sl = sel.lower()
    if sl in ("over", "under", "yes", "no"):
        return None
    if "(" in sel and sel.endswith(")"):
        sel = sel.split("(")[0].strip()
    if len(sel.split()) < 2:
        return None
    return sel


async def verify_today_lineups(db, date_str: str) -> dict:
    """Pull today's MLB lineups and void picks for scratched players.

    Only runs against picks whose first_pitch is within the next 90 min.
    Returns counters for observability.
    """
    now = datetime.now(timezone.utc)
    summary = {"games_with_lineup": 0, "picks_checked": 0, "voided": 0, "errors": []}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT,
                                       headers={"User-Agent": "PerksLocks/1.0"}) as cx:
            games = await _schedule_today(cx, date_str)
    except Exception as e:
        summary["errors"].append(str(e)[:200])
        return summary

    # Index lineups by team name for fast matching.
    games_with_lineup: list[tuple[dict, set[str]]] = []
    for g in games:
        names = _lineup_names(g)
        if names:
            games_with_lineup.append((g, names))
    summary["games_with_lineup"] = len(games_with_lineup)
    if not games_with_lineup:
        return summary

    # All pending MLB player-prop picks for today.
    cursor = db.picks.find({"sport": "MLB", "status": "pending",
                              "pick_date": date_str})
    async for p in cursor:
        if not _is_player_prop(p):
            continue
        player = _extract_player_name(p)
        if not player:
            continue
        summary["picks_checked"] += 1
        player_norm = _norm(player)
        # Find the game this pick belongs to.
        ev = (p.get("event") or "").lower()
        match: Optional[tuple[dict, set[str]]] = None
        for g, names in games_with_lineup:
            home = (g.get("teams", {}).get("home", {}).get("team", {}).get("name") or "").lower()
            away = (g.get("teams", {}).get("away", {}).get("team", {}).get("name") or "").lower()
            if (home and home in ev) or (away and away in ev):
                match = (g, names)
                break
        if not match:
            continue
        _, lineup = match
        if player_norm in lineup:
            continue  # player IS in the lineup → leave pick alone
        # Player NOT in lineup — but only void if first_pitch is imminent
        # (within ±30 min). Earlier than that, the lineup might still be
        # incomplete from MLB Stats API.
        try:
            pitch = datetime.fromisoformat((p.get("event_time") or "").replace("Z", "+00:00"))
        except Exception:
            continue
        if pitch < now or (pitch - now).total_seconds() > 30 * 60:
            continue
        # Within 30 min of first pitch and player is not listed → SCRATCHED.
        await db.picks.update_one(
            {"id": p["id"]},
            {"$set": {
                "status": "void",
                "void_reason": "Player not in starting lineup (scratched)",
                "voided_at": now.isoformat(),
                "auto_voided_by": "mlb_lineup_verifier",
            }},
        )
        summary["voided"] += 1
        logger.info("Voided pick %s — %s not in lineup", p.get("id"), player)
    return summary


async def lineup_verifier_loop(db, get_date_str) -> None:
    """Long-running loop — call every 5 min. Stops on cancellation."""
    while True:
        try:
            await asyncio.sleep(5 * 60)
            date_str = get_date_str()
            summary = await verify_today_lineups(db, date_str)
            if summary.get("voided"):
                logger.info("MLB lineup verifier: %s", summary)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("lineup verifier loop error: %s", e)
