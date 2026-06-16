"""KBO Settlement Engine — settles KBO picks using Naver Sports' free public
KBO scoreboard API as a fallback for The Odds API (which doesn't expose
completed KBO scores).

Endpoint (no auth required):
  GET https://api-gw.sports.naver.com/schedule/games
      ?fields=basic&upperCategoryId=kbaseball&categoryId=kbo
      &fromDate=YYYY-MM-DD&toDate=YYYY-MM-DD

Returns Korean team names + team codes + final scores + winner + statusCode.

This module mirrors the surface of `settlement_engine.settle_due_picks` so
it can be invoked from the same loop. It only handles KBO picks (game ML +
Run Line + Totals). Player props are out of scope (KBO has no public player
stats feed equivalent to MLB Stats API).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx

logger = logging.getLogger("lockscore.kbo_settle")

# Naver team-code → Odds API team name (English).
# Verified against The Odds API's `baseball_kbo` payload.
KBO_TEAM_CODE_TO_NAME = {
    "HH": "Hanwha Eagles",
    "WO": "Kiwoom Heroes",
    "KT": "KT Wiz",
    "NC": "NC Dinos",
    "LG": "LG Twins",
    "HT": "Kia Tigers",   # Naver's actual code for Kia (legacy from Haitai era)
    "KA": "Kia Tigers",
    "KIA": "Kia Tigers",
    "OB": "Doosan Bears", # legacy "OB Bears" → Doosan
    "DS": "Doosan Bears",
    "DB": "Doosan Bears",
    "SK": "SSG Landers",  # legacy SK Wyverns rebrand
    "SS": "Samsung Lions",  # Naver code for Samsung
    "SSG": "SSG Landers",
    "LT": "Lotte Giants",
    "SM": "Samsung Lions",
    "SAMSUNG": "Samsung Lions",
}


NAVER_URL = "https://api-gw.sports.naver.com/schedule/games"


async def fetch_kbo_scores(from_date: str, to_date: str) -> list[dict]:
    """Fetch completed KBO games from Naver Sports between two dates."""
    params = {
        "fields": "basic",
        "upperCategoryId": "kbaseball",
        "categoryId": "kbo",
        "fromDate": from_date,
        "toDate": to_date,
    }
    try:
        async with httpx.AsyncClient(timeout=15) as cx:
            r = await cx.get(
                NAVER_URL,
                params=params,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0",
                    "Referer": "https://m.sports.naver.com/",
                },
            )
            if r.status_code != 200:
                logger.warning("Naver KBO API HTTP %s", r.status_code)
                return []
            data = r.json()
            games = (data.get("result") or {}).get("games") or []
            return games
    except Exception as e:
        logger.warning("Naver KBO fetch failed: %s", e)
        return []


def _team_name_for_code(code: str) -> str:
    return KBO_TEAM_CODE_TO_NAME.get((code or "").upper(), "")


def _match_kbo_game_for_pick(pick: dict, games: list[dict]) -> Optional[dict]:
    """Find the Naver game corresponding to a pick (Pirates@A's style event).

    Note: Naver's `reversedHomeAway` flag is a display-side artefact (Korean
    sportsbooks render away on the right). The `homeTeamCode` / `awayTeamCode`
    fields already reflect the physical home/away, so we do NOT swap based
    on that flag.
    """
    event = pick.get("event") or ""
    if "@" not in event:
        return None
    away_raw, home_raw = [x.strip().lower() for x in event.split("@", 1)]
    for g in games:
        h_code = g.get("homeTeamCode") or ""
        a_code = g.get("awayTeamCode") or ""
        h_name = _team_name_for_code(h_code).lower()
        a_name = _team_name_for_code(a_code).lower()
        if h_name == home_raw and a_name == away_raw:
            return g
    return None


def _kbo_outcome(pick: dict, game: dict) -> Optional[str]:
    """Determine won/lost/push for a KBO pick given the Naver game payload."""
    if (game.get("statusCode") or "").upper() != "RESULT":
        return None  # not finished yet

    h_score = game.get("homeTeamScore")
    a_score = game.get("awayTeamScore")
    if h_score is None or a_score is None:
        return None

    h_team = _team_name_for_code(game.get("homeTeamCode") or "")
    a_team = _team_name_for_code(game.get("awayTeamCode") or "")

    total = (h_score or 0) + (a_score or 0)
    selection = (pick.get("selection") or "").lower()
    market = (pick.get("market") or "")
    market_l = market.lower()

    # Helper: extract numeric line from market string when pick.line is None.
    def _extract_line() -> Optional[float]:
        line = pick.get("line")
        if line is not None:
            try:
                return float(line)
            except Exception:
                pass
        # Patterns: "Over 7.5", "Under 9.5", "+1.5 Spread", "-1.5 Spread"
        import re
        m = re.search(r"(?:over|under)\s+(\d+(?:\.\d+)?)", market_l)
        if m:
            return float(m.group(1))
        m = re.search(r"([+-]?\d+(?:\.\d+)?)\s*spread", market_l)
        if m:
            return float(m.group(1))
        return None

    # Moneyline
    if "moneyline" in market_l or "money line" in market_l:
        winner = h_team if (h_score > a_score) else a_team if (a_score > h_score) else None
        if not winner:
            return "push"
        return "won" if winner.lower() in selection else "lost"

    # Run Line / Spread
    if "spread" in market_l or "run line" in market_l or "runline" in market_l:
        line = _extract_line()
        if line is None:
            return None
        # Determine which team the pick is on.
        if h_team.lower() in selection:
            margin = (h_score - a_score) + line
        elif a_team.lower() in selection:
            margin = (a_score - h_score) + line
        else:
            return None
        if margin > 0:
            return "won"
        if margin < 0:
            return "lost"
        return "push"

    # Totals (Over/Under runs)
    if "total" in market_l or "over" in selection or "under" in selection:
        line = _extract_line()
        if line is None:
            return None
        if "over" in selection:
            if total > line:
                return "won"
            if total < line:
                return "lost"
            return "push"
        if "under" in selection:
            if total < line:
                return "won"
            if total > line:
                return "lost"
            return "push"
    return None


async def settle_kbo_picks(db) -> dict:
    """Find pending KBO picks past their event time and grade them via Naver.

    Returns: {settled, won, lost, push, skipped, no_match}.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    cursor = db.picks.find(
        {"sport": "KBO", "status": {"$in": [None, "pending"]}},
        {"_id": 0},
    )
    picks = await cursor.to_list(length=1000)
    counts = {"settled": 0, "won": 0, "lost": 0, "push": 0,
              "skipped": 0, "no_match": 0}
    if not picks:
        return counts

    # Build the date range from the earliest pending pick to today (Naver lets
    # us fetch up to ~14 days in one shot, but we cap to 7).
    earliest = datetime.now(timezone.utc)
    for p in picks:
        et = p.get("event_time") or ""
        try:
            dt = datetime.strptime(et, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            if dt < earliest:
                earliest = dt
        except Exception:
            continue
    from_date = max(earliest, datetime.now(timezone.utc) - timedelta(days=14)).strftime("%Y-%m-%d")
    to_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    logger.info("KBO settler: %d pending picks, fetching Naver %s..%s",
                len(picks), from_date, to_date)
    games = await fetch_kbo_scores(from_date, to_date)
    logger.info("KBO settler: Naver returned %d games", len(games))

    for pick in picks:
        # Game must be at least 1 hr past commence_time.
        et = pick.get("event_time") or ""
        try:
            dt = datetime.strptime(et, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            if dt > cutoff:
                counts["skipped"] += 1
                continue
        except Exception:
            pass

        game = _match_kbo_game_for_pick(pick, games)
        if not game:
            counts["no_match"] += 1
            continue

        outcome = _kbo_outcome(pick, game)
        if not outcome:
            counts["skipped"] += 1
            continue

        # Build final-score dict for storage.
        h_team = _team_name_for_code(game.get("homeTeamCode") or "")
        a_team = _team_name_for_code(game.get("awayTeamCode") or "")
        h_score = game.get("homeTeamScore")
        a_score = game.get("awayTeamScore")
        if game.get("reversedHomeAway"):
            h_team, a_team = a_team, h_team
            h_score, a_score = a_score, h_score
        scores_dict = {h_team: h_score, a_team: a_score}

        # Reuse analytics helpers from settlement_engine for consistency.
        from analytics import (american_profit_per_unit, clv_units,
                                confidence_bucket)
        from bet_type import classify_bet_type, unit_weight
        odds_used = pick.get("closing_odds") or pick.get("book_odds")
        bet_type = classify_bet_type(odds_used)
        w = unit_weight(odds_used)
        raw_profit = american_profit_per_unit(odds_used or 0, outcome)
        units_profit = round(raw_profit * w, 4)
        units_risked = w if outcome != "push" else 0.0
        clv = clv_units(pick.get("odds_at_pick"),
                        pick.get("closing_odds") or pick.get("book_odds"))

        await db.picks.update_one(
            {"id": pick["id"]},
            {"$set": {
                "status": outcome,
                "settled_at": datetime.now(timezone.utc).isoformat(),
                "final_score": scores_dict,
                "units_risked": units_risked,
                "units_profit": units_profit,
                "bet_type": bet_type,
                "unit_weight": w,
                "clv_value": clv,
                "confidence_bucket": confidence_bucket(pick.get("lock_score")),
                "settlement_source": "naver_kbo",
            }},
        )
        counts[outcome] += 1
        counts["settled"] += 1

    logger.info("KBO settler complete: %s", counts)
    return counts
