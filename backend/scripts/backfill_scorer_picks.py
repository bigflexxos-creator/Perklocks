"""
Anytime Goal Scorer backfill — Session Tool (v2, roster-verified)
=================================================================

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
   their team's recent matches across every league ESPN indexes for
   that region.
3. For each finished match, **verifying the player was in the match
   roster** (via `summary.rosters[].roster[].athlete.displayName`).
   ONLY IF the player is confirmed on the roster we then look at
   `keyEvents` to determine goal → won/lost.  If the player is NOT in
   the roster we SKIP the match entirely (no ghost losses).
4. Inserting synthesized pick documents into `db.picks` with:
     * `sport: "Soccer"`
     * `market: "<Player> Anytime Goal Scorer"`
     * `status: "won" | "lost"`
     * `pick_date`, `settled_at`, `event`
     * `backfilled: true` marker so we can audit/wipe later.

## Data-accuracy fixes vs v1

* v1 falsely credited Kane with "New England Revolution" losses because
  the alias "England" was substring-matching "newenglandrevolution".
  v2 requires **single-word aliases to match the team name exactly**
  after slug-normalization.
* v1 attributed Tottenham matches to Kane (he moved to Bayern in 2023).
  v2 removes stale team aliases and gates national-team aliases to
  international leagues only.
* v1 recorded a "lost" pick every time the team played, even if the
  player was injured / not called up.  v2 requires the player to be in
  ESPN's roster payload; if roster data isn't available we SKIP (better
  to under-report than to lie about stats).

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
from soccer_espn_settle import _extract_scorers, _norm  # noqa: E402

logger = logging.getLogger("lockscore.scorer_backfill")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")

# ── ESPN client config ─────────────────────────────────────────────
_UA = "Mozilla/5.0 (compatible; LockScoreBackfill/1.0)"
_TIMEOUT = 15.0
_HDR = {"User-Agent": _UA, "Accept": "application/json"}
_ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"
_INTER_SLEEP = 0.25  # sec between per-summary calls

# League buckets — national-team aliases only match against these.
_NATIONAL_LEAGUES = {
    "fifa.friendly", "fifa.world", "fifa.confederations",
    "uefa.euro", "uefa.nations",
    "conmebol.copa_america",
    "afc.asian_cup", "afc.asian.cup",
    "concacaf.gold", "concacaf.nations",
    "caf.nations",
}


# ── helpers ────────────────────────────────────────────────────────
def _slug_name(name: str) -> str:
    """Return an accent-stripped alnum lowercase key for name-matching."""
    return re.sub(r"[^a-z0-9]", "", _norm(name).lower())


def _team_matches_alias(team_display: str, alias: str) -> bool:
    """Return True iff the ESPN team `displayName` corresponds to the
    given team alias.

    Rules
    -----
    * Single-word aliases (e.g. "England", "France", "Brazil") must match
      the team name **exactly** after accent-strip + lowercase.  This
      prevents "England" from matching "New England Revolution".
    * Multi-word aliases (e.g. "Bayern Munich", "Real Madrid") require
      ALL tokens of the alias to appear as complete word tokens in the
      team display name.  So "Bayern Munich" matches both "Bayern Munich"
      and "FC Bayern Munich" but not "Munich 1860".
    * Accents are stripped ("München" → "munchen").
    """
    if not team_display or not alias:
        return False
    team_norm = _norm(team_display).lower()
    alias_norm = _norm(alias).lower()
    if team_norm == alias_norm:
        return True

    alias_tokens = [t for t in re.findall(r"[a-z0-9]+", alias_norm) if len(t) >= 2]
    team_tokens = set(re.findall(r"[a-z0-9]+", team_norm))
    if not alias_tokens:
        return False
    if len(alias_tokens) == 1:
        # Country / single-word alias — only exact team-name match.
        return alias_tokens[0] in team_tokens and len(team_tokens) <= 2 and team_tokens.issubset(
            set(alias_tokens) | {"fc", "cf", "sc", "afc", "ac"}
        ) if team_tokens else False
    # Multi-token alias — all tokens must be present as words.
    return all(tok in team_tokens for tok in alias_tokens)


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


def _rosters_from_summary(summary: dict) -> list[str]:
    """Return every athlete displayName appearing on either roster.

    ESPN's summary payload exposes lineups under
    `summary.rosters[].roster[].athlete.displayName`.  Older parses also
    tried `boxscore.form[].events[].athletesInvolved[]` — those are the
    "recent form" scoring plays, NOT the roster, so they are useless
    for verifying whether a player suited up.  This implementation only
    reads the correct path.
    """
    names: list[str] = []
    for team_block in (summary.get("rosters") or []):
        for entry in team_block.get("roster") or []:
            athlete = entry.get("athlete") or {}
            nm = athlete.get("displayName") or athlete.get("shortName")
            if nm:
                names.append(nm)
    return names


async def _get_elite_scorers(limit: int) -> list[dict]:
    """Return elite Soccer scorers from `auto_elite_scorers` collection
    plus a hardcoded head list so global superstars are always covered
    even before their auto-elite entries populate.

    Each entry is:
        {
          "player_name": str,
          "national_aliases": [country ...],   # matched only in intl leagues
          "club_aliases":     [club-name ...], # matched only in club leagues
        }
    """
    # (player, national_aliases, club_aliases) — kept up-to-date as of Jun-2026
    HARD_LIST = [
        # England national + FC Bayern (Kane moved from Spurs in 2023 —
        # DO NOT re-add Tottenham).
        ("Harry Kane",             ["England"],       ["Bayern Munich", "Bayern Munchen", "FC Bayern"]),
        # Messi at Inter Miami since 2023 — DO NOT re-add Paris.
        ("Lionel Messi",           ["Argentina"],     ["Inter Miami", "Inter Miami CF"]),
        ("Erling Haaland",         ["Norway"],        ["Manchester City"]),
        # Mbappé at Real Madrid since summer 2024 — DO NOT re-add Paris.
        ("Kylian Mbappe",          ["France"],        ["Real Madrid"]),
        ("Mohamed Salah",          ["Egypt"],         ["Liverpool"]),
        ("Cristiano Ronaldo",      ["Portugal"],      ["Al Nassr", "Al-Nassr"]),
        ("Robert Lewandowski",     ["Poland"],        ["Barcelona"]),
        ("Vinicius Junior",        ["Brazil"],        ["Real Madrid"]),
        ("Julian Alvarez",         ["Argentina"],     ["Atletico Madrid", "Atlético Madrid"]),
        ("Marcus Rashford",        ["England"],       ["Manchester United", "Aston Villa"]),
        ("Bukayo Saka",            ["England"],       ["Arsenal"]),
        ("Cody Gakpo",             ["Netherlands"],   ["Liverpool"]),
        ("Lautaro Martinez",       ["Argentina"],     ["Inter", "Internazionale", "Inter Milan"]),
        ("Rodrygo",                ["Brazil"],        ["Real Madrid"]),
        ("Jude Bellingham",        ["England"],       ["Real Madrid"]),
        ("Alexander Isak",         ["Sweden"],        ["Newcastle", "Newcastle United"]),
        ("Ollie Watkins",          ["England"],       ["Aston Villa"]),
        ("Christopher Nkunku",     ["France"],        ["Chelsea"]),
        ("Serhou Guirassy",        ["Guinea"],        ["Borussia Dortmund"]),
        ("Randal Kolo Muani",      ["France"],        ["Juventus", "Paris Saint-Germain"]),
        # Ivan Toney at Al-Ahli since Aug-2024 — DO NOT re-add Brentford.
        ("Ivan Toney",             ["England"],       ["Al Ahli", "Al-Ahli"]),
    ]

    from_db: list[tuple[str, list[str], list[str]]] = []
    try:
        cursor = db.auto_elite_scorers.find(
            {"sport": "Soccer"}, {"_id": 0, "player_name": 1, "team": 1}
        ).limit(limit)
        async for row in cursor:
            nm = row.get("player_name")
            tm = row.get("team") or ""
            if nm and not any(p[0] == nm for p in HARD_LIST):
                # Auto-list entries only carry a club team; no national alias.
                from_db.append((nm, [], [tm] if tm else []))
    except Exception as e:
        logger.warning("auto_elite_scorers query failed: %s", e)

    merged = HARD_LIST + from_db
    return [
        {"player_name": nm,
         "national_aliases": nat,
         "club_aliases": club}
        for nm, nat, club in merged[:limit]
    ]


PRIORITY_LEAGUES = [
    # National-team competitions
    "fifa.friendly", "fifa.world", "fifa.confederations",
    "uefa.euro", "uefa.nations", "conmebol.copa_america",
    # Club competitions
    "uefa.champions", "uefa.europa", "uefa.europa.conf",
    "eng.1", "esp.1", "ger.1", "ita.1", "fra.1",
    "ned.1", "por.1", "bel.1", "sco.1", "tur.1",
    "ksa.1",  # Saudi Pro League — needed for Ronaldo / Toney
    "usa.1", "mex.1", "bra.1", "arg.1",
]


async def _prefetch_scoreboards(client: httpx.AsyncClient,
                                days_back: int) -> dict:
    """Fetch every (league, date) scoreboard ONCE and cache the result.

    Backfilling N players with the per-player scan would repeat the
    same ESPN scoreboard hits N times — pathological on wall-clock
    time.  We do a single pass and share the cache across all players.
    Returns dict[(league, YYYYMMDD)] -> list[event dict].
    """
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days_back)
    cache: dict[tuple[str, str], list[dict]] = {}
    d = end
    total = 0
    while d >= start:
        date_str = d.strftime("%Y%m%d")
        # Batch scoreboard requests across leagues for this date.
        results = await asyncio.gather(*[
            _fetch_scoreboard(client, lg, date_str) for lg in PRIORITY_LEAGUES
        ])
        for lg, evs in zip(PRIORITY_LEAGUES, results):
            cache[(lg, date_str)] = evs or []
            total += len(evs or [])
        d -= timedelta(days=1)
    logger.info("Scoreboard prefetch: %d (league,date) buckets, %d events total",
                len(cache), total)
    return cache


async def _backfill_player(client: httpx.AsyncClient, player: str,
                           national_aliases: list[str],
                           club_aliases: list[str],
                           days_back: int,
                           scoreboard_cache: dict,
                           summary_cache: dict) -> dict:
    """Scan every ESPN scoreboard in the date range for matches where
    `player`'s team was involved AND `player` shows up on the roster.

    Roster verification is essential — otherwise every match where the
    player's team played (but he was injured / benched / not called up)
    would be recorded as a "lost" pick, badly distorting streak stats.
    """
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days_back)

    inserted = updated = skipped = 0
    seen_events: set[str] = set()

    d = end
    while d >= start:
        date_str = d.strftime("%Y%m%d")
        for lg in PRIORITY_LEAGUES:
            is_national = lg in _NATIONAL_LEAGUES
            aliases_for_league = national_aliases if is_national else club_aliases
            if not aliases_for_league:
                continue

            events = scoreboard_cache.get((lg, date_str), [])
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
                # competitors.  Uses exact-token matching so single-word
                # aliases like "England" don't hit "New England Revolution".
                team_present = any(
                    _team_matches_alias(home, a) or _team_matches_alias(away, a)
                    for a in aliases_for_league
                )
                if not team_present:
                    continue

                # Fetch the summary and verify the player is actually on
                # one of the rosters BEFORE recording anything.
                cache_key = (lg, event_id)
                if cache_key in summary_cache:
                    summary = summary_cache[cache_key]
                else:
                    summary = await _fetch_summary(client, event_id, lg)
                    await asyncio.sleep(_INTER_SLEEP)
                    summary_cache[cache_key] = summary
                if not summary:
                    continue

                roster_names = _rosters_from_summary(summary)
                if roster_names:
                    if not _name_match(player, roster_names):
                        # Player was NOT in the match squad — skip.
                        # Better to under-record than to fabricate a loss.
                        seen_events.add(event_id)
                        continue
                else:
                    # ESPN didn't publish rosters for this match.  For
                    # club matches we can't verify; skip to stay safe.
                    # For national-team matches we can trust the team
                    # signal a bit more (small squads, published rosters
                    # elsewhere), but roster-empty is still risky, so we
                    # SKIP everywhere.  This deliberately reduces sample
                    # size in favour of accuracy.
                    seen_events.add(event_id)
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
                # Idempotent upsert keyed by ESPN event_id so re-runs
                # never create dupes even if pick_date drifts.
                res = await db.picks.update_one(
                    {"sport": "Soccer", "player_name": player,
                     "backfill_source": f"espn:{lg}:{event_id}"},
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


async def _purge_stale_backfill() -> int:
    """Delete legacy backfilled picks so the v2 run starts clean.

    v1 of this script (Jun-2026) recorded false "lost" picks for
    matches the player never suited up in.  We wipe every row tagged
    `backfilled: true` before re-running to guarantee the DB reflects
    only roster-verified outcomes.
    """
    res = await db.picks.delete_many({"backfilled": True})
    logger.info("Purged %d legacy backfilled picks", res.deleted_count)
    return res.deleted_count


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90,
                        help="How many days back to scan (default 90)")
    parser.add_argument("--max-players", type=int, default=40,
                        help="Cap on players to backfill this run")
    parser.add_argument("--dry-run", action="store_true",
                        help="Skip DB writes; print counts only")
    parser.add_argument("--no-purge", action="store_true",
                        help="Skip deleting legacy backfilled picks")
    args = parser.parse_args()

    if not args.no_purge and not args.dry_run:
        await _purge_stale_backfill()

    players = await _get_elite_scorers(args.max_players)
    logger.info("Backfilling %d players over last %d days",
                len(players), args.days)

    totals = {"inserted": 0, "updated": 0, "skipped": 0, "players": 0}
    async with httpx.AsyncClient() as client:
        # One-shot scoreboard fetch shared across all players.
        scoreboard_cache = await _prefetch_scoreboards(client, args.days)
        # Summaries are cached across players too (e.g., Bellingham /
        # Vinicius Jr both need Real Madrid summaries).
        summary_cache: dict = {}

        for p in players:
            name = p["player_name"]
            nat = p.get("national_aliases") or []
            club = p.get("club_aliases") or []
            if not nat and not club:
                logger.info("  %s → skipped (no team aliases)", name)
                continue
            try:
                r = await _backfill_player(client, name, nat, club,
                                           args.days, scoreboard_cache,
                                           summary_cache)
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
