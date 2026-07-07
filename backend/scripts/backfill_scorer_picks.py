"""
Anytime Goal Scorer backfill — Session Tool
============================================

Purpose
-------
The Cheatsheets module in Lab (see `backend/lab_routes.py`) requires
≥5 settled "Anytime Goal Scorer" picks per player to produce a hit-
streak card. Our production pick stream generates these organically
but sparsely; elite scorers like Kane, Messi, Haaland, Mbappé often
have 0-4 settled picks in the DB, so they never surface as Cheatsheet
cards even though they're the most bet-on players on Earth.

This script backfills those settled picks retroactively by:

1. Reading `db.auto_elite_scorers` for the current elite list.
2. For each elite scorer, querying **ESPN's public soccer API** for
   their team's recent matches (last 90 days) across every league
   ESPN indexes for that region.
3. For each finished match, extracting the actual `keyEvents` goal
   plays and determining whether the scorer scored (`won`) or not
   (`lost`).
4. Inserting synthesized pick documents into `db.picks` with:
     * `sport: "Soccer"`
     * `market: "<Player> Anytime Goal Scorer"`
     * `status: "won" | "lost"`
     * `pick_date`, `settled_at`, `event`
     * `backfilled: true` marker so we can audit/wipe later.

Idempotence
-----------
We upsert on `(sport, player_name, event, pick_date)`, so re-running
the script never duplicates rows. Safe to run daily.

Usage
-----
$ python -m scripts.backfill_scorer_picks --days=90 --max-players=100

Outputs how many rows were inserted / updated / skipped for each
player, then a totals summary.

Rate-limit note: ESPN's public API is generous but we still sleep
250ms between per-match summary fetches to be a good citizen.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
import unicodedata as ud
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Allow "python scripts/backfill_scorer_picks.py" from the backend dir.
sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx  # noqa: E402

from deps import db  # noqa: E402
from soccer_espn_settle import _extract_scorers, _norm, _LEAGUES  # noqa: E402

logger = logging.getLogger("lockscore.scorer_backfill")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")

# ── ESPN client config ─────────────────────────────────────────────
_UA = "Mozilla/5.0 (compatible; LockScoreBackfill/1.0)"
_TIMEOUT = 15.0
_HDR = {"User-Agent": _UA, "Accept": "application/json"}
_ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"
_INTER_SLEEP = 0.25  # sec between per-summary calls


# ── helpers ────────────────────────────────────────────────────────
def _slug_name(name: str) -> str:
    """Return an accent-stripped alnum lowercase key for name-matching."""
    return re.sub(r"[^a-z0-9]", "", _norm(name).lower())


def _name_match(target: str, scorers: list[str]) -> bool:
    """Fuzzy compare — accept exact match on last-name OR full name."""
    if not target or not scorers:
        return False
    t_full = _slug_name(target)
    t_last = _slug_name(target.strip().split()[-1]) if " " in target.strip() else t_full
    for s in scorers:
        s_full = _slug_name(s)
        if s_full == t_full:
            return True
        # Full-name substring works for "Erling Braut Haaland" vs "Haaland"
        if t_full in s_full or s_full in t_full:
            return True
        # Last-name-only fallback (careful: 3+ chars minimum to avoid
        # "Silva" matching every Brazilian).
        if len(t_last) >= 5 and t_last in s_full:
            return True
    return False


async def _fetch_scoreboard(client: httpx.AsyncClient, league: str,
                            date_str: str) -> list[dict]:
    """Fetch ESPN scoreboard events for a league on a specific date."""
    url = f"{_ESPN_BASE}/{league}/scoreboard"
    params = {"dates": date_str}
    try:
        r = await client.get(url, params=params, headers=_HDR, timeout=_TIMEOUT)
        if r.status_code != 200:
            return []
        return (r.json() or {}).get("events", []) or []
    except Exception:
        return []


async def _fetch_summary(client: httpx.AsyncClient, event_id: str,
                         league: str) -> dict:
    """Fetch ESPN match summary (contains keyEvents/scoring)."""
    url = f"{_ESPN_BASE}/{league}/summary"
    try:
        r = await client.get(url, params={"event": event_id},
                             headers=_HDR, timeout=_TIMEOUT)
        if r.status_code != 200:
            return {}
        return r.json() or {}
    except Exception:
        return {}


async def _get_elite_scorers(limit: int) -> list[dict]:
    """Return elite Soccer scorers from `auto_elite_scorers` collection
    plus a hardcoded head list so global superstars are always covered
    even before their auto-elite entries populate.
    """
    HARD_LIST = [
        # (player_name, [team-name aliases we match against event strings])
        ("Harry Kane",             ["England", "Bayern Munich", "Bayern München", "Tottenham"]),
        ("Lionel Messi",           ["Argentina", "Inter Miami", "Paris"]),
        ("Erling Braut Haaland",   ["Norway", "Manchester City"]),
        ("Kylian Mbappe",          ["France", "Real Madrid", "Paris"]),
        ("Mohamed Salah",          ["Egypt", "Liverpool"]),
        ("Cristiano Ronaldo",      ["Portugal", "Al Nassr", "Al-Nassr"]),
        ("Robert Lewandowski",     ["Poland", "Barcelona"]),
        ("Vinicius Junior",        ["Brazil", "Real Madrid"]),
        ("Julian Alvarez",         ["Argentina", "Atletico Madrid", "Atlético"]),
        ("Marcus Rashford",        ["England", "Manchester United"]),
        ("Bukayo Saka",            ["England", "Arsenal"]),
        ("Cody Gakpo",             ["Netherlands", "Liverpool"]),
        ("Lautaro Martinez",       ["Argentina", "Inter", "Internazionale"]),
        ("Rodrygo",                ["Brazil", "Real Madrid"]),
        ("Jude Bellingham",        ["England", "Real Madrid"]),
        ("Alexander Isak",         ["Sweden", "Newcastle"]),
        ("Ollie Watkins",          ["England", "Aston Villa"]),
        ("Christopher Nkunku",     ["France", "Chelsea"]),
        ("Serhou Guirassy",        ["Guinea", "Borussia Dortmund", "Dortmund"]),
        ("Randal Kolo Muani",      ["France", "Paris"]),
        ("Ivan Toney",             ["England", "Al Ahli", "Brentford"]),
    ]

    from_db: list[tuple[str, list[str]]] = []
    try:
        cursor = db.auto_elite_scorers.find(
            {"sport": "Soccer"}, {"_id": 0, "player_name": 1, "team": 1}
        ).limit(limit)
        async for row in cursor:
            nm = row.get("player_name")
            tm = row.get("team") or ""
            if nm and not any(p[0] == nm for p in HARD_LIST):
                from_db.append((nm, [tm] if tm else []))
    except Exception as e:
        logger.warning("auto_elite_scorers query failed: %s", e)

    merged = HARD_LIST + from_db
    return [
        {"player_name": nm, "team_aliases": teams}
        for nm, teams in merged[:limit]
    ]


async def _backfill_player(client: httpx.AsyncClient, player: str,
                           team_aliases: list[str],
                           days_back: int) -> dict:
    """Scan every ESPN scoreboard in the date range for matches where
    `player`'s team was involved, and upsert one settled pick per
    match. We identify the player's match via TEAM (from the hard
    alias list) rather than trying to parse ESPN's roster payloads —
    that avoids false positives from ambiguous player/name matches.
    """
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days_back)

    priority_leagues = [
        "fifa.friendly", "fifa.world", "uefa.euro", "uefa.nations",
        "uefa.champions", "conmebol.copa_america",
        "eng.1", "esp.1", "ger.1", "ita.1", "fra.1", "ned.1", "por.1",
        "usa.1", "mex.1", "bra.1", "arg.1",
    ]
    # Normalize team aliases for cheap contains-match
    tm_normed = [_slug_name(t) for t in team_aliases if t]

    inserted = updated = skipped = 0
    seen_events: set[str] = set()

    d = end
    while d >= start:
        date_str = d.strftime("%Y%m%d")
        for lg in priority_leagues:
            events = await _fetch_scoreboard(client, lg, date_str)
            for ev in events:
                if not ev:
                    continue
                status = ((ev.get("status") or {}).get("type") or {}).get("state")
                if status != "post":
                    continue
                comps = (ev.get("competitions") or [{}])[0].get("competitors", [])
                if len(comps) < 2:
                    continue
                home = next((c.get("team", {}).get("displayName") for c in comps
                             if c.get("homeAway") == "home"), None)
                away = next((c.get("team", {}).get("displayName") for c in comps
                             if c.get("homeAway") == "away"), None)
                if not home or not away:
                    continue
                event_id = ev.get("id")
                if not event_id or event_id in seen_events:
                    continue

                # TEAM MATCH — the player's team must be one of the
                # competitors. This is the pivot that stops us from
                # falsely crediting Haaland for matches he wasn't in.
                home_n = _slug_name(home)
                away_n = _slug_name(away)
                team_present = any(
                    (t in home_n or home_n in t or t in away_n or away_n in t)
                    for t in tm_normed
                )
                if not team_present:
                    continue

                # Only NOW do we spend an ESPN summary call to get scorers.
                summary = await _fetch_summary(client, event_id, lg)
                await asyncio.sleep(_INTER_SLEEP)
                if not summary:
                    continue

                key_events = summary.get("keyEvents") or []
                scorers = _extract_scorers(key_events)
                seen_events.add(event_id)

                did_score = _name_match(player, scorers)
                status_str = "won" if did_score else "lost"
                event_string = f"{away} @ {home}"
                pick_date = d.isoformat()
                doc = {
                    "sport": "Soccer",
                    "player_name": player,
                    "market": f"{player} Anytime Goal Scorer",
                    "selection": "Yes",
                    "event": event_string,
                    "status": status_str,
                    "pick_date": pick_date,
                    "settled_at": datetime.now(timezone.utc).isoformat(),
                    "backfilled": True,
                    "backfill_source": f"espn:{lg}:{event_id}",
                    "lock_score": 78.0,          # placeholder — not shown
                    "book_odds": -110,           # placeholder
                    "units_risked": 0.0,          # backfill rows shouldn't affect bankroll
                    "units_profit": 0.0,
                }
                res = await db.picks.update_one(
                    {"sport": "Soccer", "player_name": player,
                     "event": event_string, "pick_date": pick_date,
                     "market": {"$regex": "Anytime Goal Scorer"}},
                    {"$setOnInsert": {"id": f"backfill:{lg}:{event_id}:{_slug_name(player)}"},
                     "$set": doc},
                    upsert=True,
                )
                if res.upserted_id is not None:
                    inserted += 1
                elif res.modified_count > 0:
                    updated += 1
                else:
                    skipped += 1
        d -= timedelta(days=1)

    return {"player": player, "inserted": inserted,
            "updated": updated, "skipped": skipped,
            "matches_scanned": len(seen_events)}


def _rosters_from_summary(summary: dict) -> list[str]:
    """Return every athlete displayName appearing on either roster
    (used to detect if the player was in this match at all)."""
    names: list[str] = []
    for team_block in (summary.get("boxscore") or {}).get("form") or []:
        for entry in team_block.get("events") or []:
            for athlete in entry.get("athletesInvolved") or []:
                nm = athlete.get("displayName") or athlete.get("shortName")
                if nm:
                    names.append(nm)
    # Also lineup arrays if present
    for team_block in (summary.get("rosters") or []):
        for player in team_block.get("roster") or []:
            a = player.get("athlete") or {}
            nm = a.get("displayName") or a.get("shortName")
            if nm:
                names.append(nm)
    return names


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90,
                        help="How many days back to scan (default 90)")
    parser.add_argument("--max-players", type=int, default=40,
                        help="Cap on players to backfill this run")
    parser.add_argument("--dry-run", action="store_true",
                        help="Skip DB writes; print counts only")
    args = parser.parse_args()

    players = await _get_elite_scorers(args.max_players)
    logger.info("Backfilling %d players over last %d days",
                len(players), args.days)

    totals = {"inserted": 0, "updated": 0, "skipped": 0, "players": 0}
    async with httpx.AsyncClient() as client:
        for p in players:
            name = p["player_name"]
            aliases = p.get("team_aliases") or []
            if not aliases:
                logger.info("  %s → skipped (no team aliases)", name)
                continue
            try:
                r = await _backfill_player(client, name, aliases, args.days)
                logger.info("  %s → +%d new · %d upd · %d skip · %d matches",
                            name, r["inserted"], r["updated"], r["skipped"],
                            r["matches_scanned"])
                totals["inserted"] += r["inserted"]
                totals["updated"] += r["updated"]
                totals["skipped"] += r["skipped"]
                totals["players"] += 1
            except Exception as e:
                logger.exception("player %s failed: %s", name, e)

    logger.info("DONE — players=%d inserted=%d updated=%d skipped=%d",
                totals["players"], totals["inserted"],
                totals["updated"], totals["skipped"])


if __name__ == "__main__":
    asyncio.run(main())
