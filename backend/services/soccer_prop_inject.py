"""Generalized Soccer Player Prop Injector (Big-5 European leagues).

Extends the same Player Prop Intelligence approach as
`mls_direct_inject.py` to EPL / La Liga / Serie A / Bundesliga /
Ligue 1 by sourcing candidates from `soccer_player_form` (Understat).

Sport keys handled:
   soccer_epl                    (English Premier League)
   soccer_spain_la_liga
   soccer_italy_serie_a
   soccer_germany_bundesliga
   soccer_france_ligue_one
   soccer_uefa_champs_league     (players from Big-5 clubs)

Runs every 15 min as a background task from server startup.
"""
from __future__ import annotations

import asyncio
import logging
import os
import unicodedata
from datetime import datetime, timezone
from typing import Optional

import httpx

from services.player_props import (
    Archetype,
    build_matchup_context,
    classify_archetype,
    get_matchup_split,
    get_player_stats,
    select_markets,
)

logger = logging.getLogger("lockscore.soccer_prop_inject")


# ─────────── League config ───────────
# sport_key → Understat league label in `soccer_player_form`
_SPORT_TO_LEAGUE = {
    "soccer_epl":                    "EPL",
    "soccer_spain_la_liga":          "La_liga",
    "soccer_italy_serie_a":          "Serie_A",
    "soccer_germany_bundesliga":     "Bundesliga",
    "soccer_france_ligue_one":       "Ligue_1",
    # UCL players come from all Big-5 — search all leagues.
    "soccer_uefa_champs_league":     None,
}


def _norm(name: str) -> str:
    if not name:
        return ""
    nk = unicodedata.normalize("NFKD", name)
    return "".join(c for c in nk if not unicodedata.combining(c)).lower().strip()


def _team_match(a: str, b: str) -> bool:
    if not a or not b:
        return False
    a = a.lower(); b = b.lower()
    for suf in (" fc", " f.c.", " sc", " cf", " united", " city",
                " football club"):
        a = a.replace(suf, ""); b = b.replace(suf, "")
    a = a.strip(); b = b.strip()
    return a == b or a in b or b in a


def _american(r: float) -> int:
    r = max(0.05, min(0.95, r))
    if r >= 0.5:
        fair = int(round(-100.0 * r / (1.0 - r)))
        juiced = int(fair * 0.92)
        if -100 < juiced <= 0:
            juiced = -105
        return max(min(juiced, -100), -800)
    fair = int(round(100.0 * (1.0 - r) / r))
    juiced = int(fair * 1.08)
    if 0 <= juiced < 100:
        juiced = 105
    return min(max(juiced, 100), 1500)


async def _fetch_events(cx: httpx.AsyncClient, sport_key: str) -> list[dict]:
    key = os.getenv("THE_ODDS_API_KEY", "")
    if not key:
        return []
    try:
        r = await cx.get(
            f"https://api.the-odds-api.com/v4/sports/{sport_key}/events",
            params={"apiKey": key}, timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning("Events fetch %s failed: %s", sport_key, e)
    return []


async def _fetch_scorers_for_league(league_hint: Optional[str]
                                     ) -> list[dict]:
    """Return top attacking players from `soccer_player_form`.

    If `league_hint` is None (UCL), pull all Big-5 top-90 combined.
    """
    from deps import db
    query = {"league": league_hint} if league_hint else {
        "league": {"$in": ["EPL", "La_liga", "Serie_A",
                            "Bundesliga", "Ligue_1"]}
    }
    # Prioritize elite attackers: at least 5 goals OR 5 assists this
    # season, in the current season doc.
    query["$or"] = [{"goals": {"$gte": 5}}, {"assists": {"$gte": 5}}]
    docs = await db.soccer_player_form.find(query).sort(
        [("goals", -1)]
    ).to_list(length=400)
    return docs


async def _generate_for_event(ev: dict, sport_key: str,
                               league_hint: Optional[str],
                               all_scorers: list[dict]) -> list[dict]:
    home = (ev.get("home_team") or "").strip()
    away = (ev.get("away_team") or "").strip()
    if not home or not away:
        return []

    # Bucket scorers to teams by (best-effort) name match. Understat
    # stores team as e.g. "Manchester City", oddsapi as "Manchester City".
    home_players: list[dict] = []
    away_players: list[dict] = []
    for r in all_scorers:
        team = r.get("team") or ""
        name = r.get("player_name") or ""
        if not name:
            continue
        if _team_match(team, home):
            home_players.append({"name": name, "team": team})
        elif _team_match(team, away):
            away_players.append({"name": name, "team": team})

    # Rank by combined output (goals + assists per game) for pick priority.
    def _rank_key(r: dict) -> float:
        s = next((s for s in all_scorers
                  if (s.get("player_name") or "") == r["name"]
                  and (s.get("team") or "") == r["team"]), None)
        if not s:
            return 0.0
        g = int(s.get("goals") or 0)
        a = int(s.get("assists") or 0)
        games = int(s.get("games") or 0) or 1
        return (g + a) / games

    home_players.sort(key=_rank_key, reverse=True)
    away_players.sort(key=_rank_key, reverse=True)

    picks: list[dict] = []
    commence = ev.get("commence_time") or ""
    event_id = ev.get("id") or f"{sport_key}-{home}-{away}"

    async def _emit_for(entry: dict, opp: str, is_home: bool) -> list[dict]:
        name = entry["name"]
        stats = await get_player_stats(name, league_hint=league_hint)
        if not stats or not stats.data_ok:
            return []

        archetype = classify_archetype(stats)
        if archetype in (Archetype.LOW_INVOLVEMENT, Archetype.UNKNOWN):
            return []

        split = await get_matchup_split(name, opp)  # None for non-MLS

        # Build matchup context. We don't have per-team defense stats
        # for Big-5 yet — home/away + form remains the main signals.
        matchup_ctx = build_matchup_context(
            stats, opp,
            is_home=is_home,
            event_commence=commence,
            last_match_iso=None,     # would come from schedule feed (future)
            split=split,
        )

        routes = select_markets(stats, archetype, split, matchup_ctx)
        if not routes:
            return []

        out: list[dict] = []
        for route in routes:
            p = route.probability
            book_odds = _american(p)

            if p >= 0.55: lock = 95.0
            elif p >= 0.40: lock = 91.0
            elif p >= 0.25: lock = 87.0
            elif p >= 0.15: lock = 83.0
            else:            lock = 80.0
            if route.confidence == "HIGH": lock = min(99.0, lock + 2.0)
            elif route.confidence == "LOW": lock = max(75.0, lock - 3.0)
            # market_fit adjusts +/- 1
            if route.market_fit >= 90:
                lock = min(99.0, lock + 1.0)
            elif route.market_fit < 40:
                lock = max(75.0, lock - 2.0)

            grade = ("Strong Lock" if lock >= 95 else
                      ("Lock" if lock >= 90 else "Playable"))

            pick = {
                "id": f"soccer-prop-{route.market}-{event_id}-{name.replace(' ', '_').lower()}",
                "external_id": f"SOCCER-PROP-{route.market}-{event_id}-{name}",
                "sport": "Soccer",
                "league": (league_hint or "UCL").replace("_", " "),
                "event": f"{away} @ {home}",
                "event_time": commence,
                "market": f"{name} {route.label}",
                "market_type": route.market,
                "selection": name,
                "pick_side": name,
                "model_win_prob": p,
                "win_probability": p,
                "book_odds": book_odds,
                "book_implied_prob": round(p / 1.08, 4),
                "lock_score": lock,
                "lock_score_v2": lock,
                "lock_score_v2_raw": lock,
                "lock_score_peak": lock,
                "edge_percent": 4.0,
                "grade": grade,
                "confidence": grade,
                "status": "pending",
                "no_bet": False,
                "off_board": False,
                "elite_player": True,
                "is_elite": True,
                "is_synthetic_scorer": True,
                "is_long_shot": True,
                "synthetic": True,
                "synthetic_source": "player_prop_intelligence_v2",
                "source": "player_prop_intelligence_v2",
                "home_team": home,
                "away_team": away,
                "home_team_name": home,
                "away_team_name": away,
                "sport_key": sport_key,
                "archetype": archetype.value,
                "archetype_display": archetype.display(),
                "market_fit": route.market_fit,
                "samples": {
                    "goals": stats.goals,
                    "assists": stats.assists,
                    "games": stats.games,
                    "minutes": stats.minutes,
                    "goals_per_90": stats.goals_per_90,
                    "assists_per_90": stats.assists_per_90,
                    "key_passes_per_90": stats.key_passes_per_90,
                    "npxg_per_90": stats.npxg_per_90,
                    "source": stats.source,
                    "league": stats.league,
                },
                "pick_rationale": {
                    "engine": "player_prop_intelligence_v2",
                    "summary": (
                        f"{name} ({archetype.display()}): "
                        f"model p={p*100:.0f}% · {stats.goals}G/{stats.assists}A "
                        f"in {stats.games} games. Market fit {route.market_fit}%."
                    ),
                    "evidence": route.recommendation.evidence,
                    "concerns": route.recommendation.concerns,
                    "matchup": {
                        "player": name,
                        "team": stats.team,
                        "opponent": opp,
                        "is_home": is_home,
                    },
                    "recent_form": {
                        "engine": "player_prop_intelligence_v2",
                        "form_score": stats.form_score,
                        "form_label": stats.form_label,
                    },
                    "model_debug": route.recommendation.debug,
                    "market_fit": route.market_fit,
                },
            }
            out.append(pick)
        return out

    sem = asyncio.Semaphore(6)
    async def _run(entry: dict, opp: str, is_home: bool):
        async with sem:
            return await _emit_for(entry, opp, is_home)

    tasks = [_run(e, away, True) for e in home_players[:6]]
    tasks += [_run(e, home, False) for e in away_players[:6]]
    for lst in await asyncio.gather(*tasks, return_exceptions=True):
        if isinstance(lst, list):
            picks.extend(lst)
    return picks


async def run_once() -> dict:
    """One full injection pass across all Big-5 + UCL sport keys."""
    from deps import db
    from pymongo import ReplaceOne

    now = datetime.now(timezone.utc).isoformat()
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    totals: dict[str, int] = {}
    total_picks_written = 0

    async with httpx.AsyncClient(timeout=15) as cx:
        for sport_key, league_hint in _SPORT_TO_LEAGUE.items():
            events = await _fetch_events(cx, sport_key)
            if not events:
                totals[sport_key] = 0
                continue
            scorers = await _fetch_scorers_for_league(league_hint)
            if not scorers:
                totals[sport_key] = 0
                continue

            all_picks: list[dict] = []
            for ev in events:
                ct = ev.get("commence_time") or ""
                try:
                    dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
                    if dt < datetime.now(timezone.utc):
                        continue
                except Exception:
                    pass
                picks = await _generate_for_event(
                    ev, sport_key, league_hint, scorers,
                )
                all_picks.extend(picks)

            totals[sport_key] = len(all_picks)
            total_picks_written += len(all_picks)

            if not all_picks:
                continue
            ops = []
            for p in all_picks:
                p["created_at"] = now
                p["pick_date"] = today_str
                p["updated_at"] = now
                ops.append(ReplaceOne({"id": p["id"]}, p, upsert=True))
            try:
                await db.picks.bulk_write(ops, ordered=False)
            except Exception as e:
                logger.warning("Soccer prop upsert error (%s): %s", sport_key, e)
            logger.info(
                "Soccer Prop Inject %s: %d picks across %d events",
                sport_key, len(all_picks), len(events),
            )

    return {"ok": True, "picks_written": total_picks_written,
            "by_sport_key": totals, "pick_date": today_str}


async def loop() -> None:
    """Fire-and-forget refresh loop — runs every 15 min."""
    await asyncio.sleep(45)   # let stats warm up
    while True:
        try:
            summary = await run_once()
            logger.info("Soccer Prop Inject cycle: %s", summary)
        except Exception as e:
            logger.warning("Soccer Prop Inject failed: %s", e)
        await asyncio.sleep(15 * 60)
