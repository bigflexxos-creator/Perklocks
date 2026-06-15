"""Settlement engine — pulls final scores from The Odds API and marks picks
as Won/Lost/Push for moneyline, spread, totals, and win-or-draw markets.

Player props (e.g. "Buxton Over 0.5 Hits", "Anytime Goal Scorer") are NOT
auto-settled here because The Odds API doesn't expose individual player stats.
Those remain as `pending` until a future stats integration or manual mark.
"""
import logging
import re
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx

from sports_engine import ODDS_KEY, BASE, SPORT_KEYS

logger = logging.getLogger("lockscore.settlement")


SETTLEABLE_KEYWORDS = (
    "moneyline", "spread", "total goals", "total runs", "total points",
    "total games", "total ", "win or draw",
)
PROP_KEYWORDS = (
    "hits", "home runs", "total bases", "rebounds", "assists", "points",
    "anytime goal scorer", "first goal scorer",
)


def is_player_prop(pick: dict) -> bool:
    market = (pick.get("market") or "").lower()
    league = (pick.get("league") or "").lower()
    if "props" in league:
        return True
    return any(k in market for k in PROP_KEYWORDS)


def parse_event_teams(event_str: str) -> tuple[Optional[str], Optional[str]]:
    """'Away @ Home' → ('Away', 'Home')."""
    if not event_str or "@" not in event_str:
        return (None, None)
    parts = event_str.split("@", 1)
    return (parts[0].strip(), parts[1].strip())


def _score_for(scores: list[dict], team: str) -> Optional[float]:
    if not team:
        return None
    target = team.strip().lower()
    for s in scores:
        name = (s.get("name") or "").strip().lower()
        if name == target:
            try:
                return float(s.get("score", 0))
            except Exception:
                return None
    return None


def _parse_spread(market: str) -> tuple[Optional[str], Optional[float]]:
    """Extract the team and line from a market string like 'Team +1.5 Spread' or 'Team -1.5 Spread'."""
    m = re.match(r"^(.+?)\s+([+-]?\d+(?:\.\d+)?)\s+Spread\s*$", market, re.IGNORECASE)
    if not m:
        return (None, None)
    return (m.group(1).strip(), float(m.group(2)))


def _parse_total_line(market: str) -> Optional[float]:
    m = re.search(r"Over\s+(\d+(?:\.\d+)?)", market, re.IGNORECASE)
    if m:
        return float(m.group(1))
    return None


def settle_pick(pick: dict, score_payload: dict) -> Optional[str]:
    """Return 'won' / 'lost' / 'push' / None (not yet settleable)."""
    if not score_payload.get("completed"):
        return None
    scores = score_payload.get("scores") or []
    if not scores:
        return None

    market = (pick.get("market") or "").lower()
    selection = pick.get("selection") or ""
    away, home = parse_event_teams(pick.get("event") or "")
    away_score = _score_for(scores, away)
    home_score = _score_for(scores, home)
    if away_score is None or home_score is None:
        return None
    total = away_score + home_score

    # Moneyline (and Win or Draw)
    if "moneyline" in market:
        if away_score == home_score:
            return "push"
        winner = away if away_score > home_score else home
        return "won" if winner == selection else "lost"

    if "win or draw" in market:
        # Selection wins if their team didn't lose.
        if not selection:
            return None
        if selection == away:
            return "won" if away_score >= home_score else "lost"
        if selection == home:
            return "won" if home_score >= away_score else "lost"
        return None

    # Spread
    if "spread" in market:
        team, line = _parse_spread(pick.get("market") or "")
        if not team or line is None:
            return None
        team_score = _score_for(scores, team)
        opp_score = _score_for(scores, home if team == away else away)
        if team_score is None or opp_score is None:
            return None
        margin = team_score - opp_score + line
        if abs(margin) < 0.01:
            return "push"
        return "won" if margin > 0 else "lost"

    # Totals
    if "over" in market and ("total" in market or " runs" in market or " goals" in market or " points" in market or " games" in market):
        line = _parse_total_line(pick.get("market") or "")
        if line is None:
            return None
        if abs(total - line) < 0.01:
            return "push"
        return "won" if total > line else "lost"

    return None  # unrecognized market — leave pending


async def _fetch_scores(sport_key: str) -> list[dict]:
    """Fetch completed-game scores for a sport over the last 3 days."""
    if not ODDS_KEY:
        return []
    url = f"{BASE}/sports/{sport_key}/scores"
    params = {"apiKey": ODDS_KEY, "daysFrom": 3}
    try:
        async with httpx.AsyncClient(timeout=15) as cx:
            r = await cx.get(url, params=params)
            if r.status_code != 200:
                return []
            data = r.json()
            return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning("scores fetch failed for %s: %s", sport_key, e)
        return []


def _match_score_for_pick(pick: dict, all_scores: list[dict]) -> Optional[dict]:
    away, home = parse_event_teams(pick.get("event") or "")
    if not away or not home:
        return None
    al = away.lower()
    hl = home.lower()
    for s in all_scores:
        h_team = (s.get("home_team") or "").lower()
        a_team = (s.get("away_team") or "").lower()
        if h_team == hl and a_team == al:
            return s
    return None


async def settle_due_picks(db) -> dict:
    """Find all pending picks whose game has completed, mark each as won/lost/push.

    Returns counts: {settled, won, lost, push, skipped}.
    """
    cutoff_recent = datetime.now(timezone.utc) + timedelta(hours=1)
    cursor = db.picks.find({"status": {"$in": [None, "pending"]}}, {"_id": 0})
    picks = await cursor.to_list(length=2000)
    counts = {"settled": 0, "won": 0, "lost": 0, "push": 0, "skipped": 0, "props_pending": 0}
    if not picks:
        return counts

    # Group by sport so we batch score fetches.
    by_sport: dict[str, list[dict]] = {}
    for p in picks:
        sp = p.get("sport")
        if not sp:
            continue
        by_sport.setdefault(sp, []).append(p)

    # Fetch scores per sport_key
    scores_cache: dict[str, list[dict]] = {}
    for sport, sport_picks in by_sport.items():
        keys = SPORT_KEYS.get(sport, [])
        all_scores: list[dict] = []
        for key in keys:
            data = await _fetch_scores(key)
            if data:
                all_scores.extend(data)
            await asyncio.sleep(0.6)  # throttle to avoid 429
        scores_cache[sport] = all_scores

    for sport, sport_picks in by_sport.items():
        all_scores = scores_cache.get(sport, [])
        if not all_scores:
            continue
        for pick in sport_picks:
            # Skip player props — can't settle without player stats.
            if is_player_prop(pick):
                counts["props_pending"] += 1
                continue
            # Only consider picks whose game time has clearly passed.
            et = pick.get("event_time") or ""
            try:
                dt = datetime.strptime(et, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                # Game should have ended at least an hour ago.
                if dt > cutoff_recent:
                    counts["skipped"] += 1
                    continue
            except Exception:
                pass
            score_payload = _match_score_for_pick(pick, all_scores)
            if not score_payload:
                counts["skipped"] += 1
                continue
            outcome = settle_pick(pick, score_payload)
            if not outcome:
                counts["skipped"] += 1
                continue
            scores_dict = {s["name"]: s["score"] for s in (score_payload.get("scores") or [])}
            # Compute units_profit + CLV at settle time so the analytics
            # dashboard never has to recompute from raw odds.
            from analytics import (american_profit_per_unit, clv_units,
                                    confidence_bucket)
            odds_used = pick.get("closing_odds") or pick.get("book_odds")
            units_profit = american_profit_per_unit(odds_used or 0, outcome)
            clv = clv_units(pick.get("odds_at_pick"), pick.get("closing_odds") or pick.get("book_odds"))
            await db.picks.update_one(
                {"id": pick["id"]},
                {"$set": {
                    "status": outcome,
                    "settled_at": datetime.now(timezone.utc).isoformat(),
                    "final_score": scores_dict,
                    "units_risked": 1.0 if outcome != "push" else 0.0,
                    "units_profit": units_profit,
                    "clv_value": clv,
                    "confidence_bucket": confidence_bucket(pick.get("lock_score")),
                }},
            )
            counts[outcome] += 1
            counts["settled"] += 1
    logger.info("Settlement complete: %s", counts)

    # Player props can't be graded from The Odds API scores; delegate to the
    # dedicated prop engine which pulls player stats from MLB Stats API + ESPN.
    try:
        from prop_settlement import settle_player_props
        prop_counts = await settle_player_props(db)
        counts["props_settled"] = prop_counts.get("settled", 0)
        counts["props_won"] = prop_counts.get("won", 0)
        counts["props_lost"] = prop_counts.get("lost", 0)
        counts["props_push"] = prop_counts.get("push", 0)
        counts["won"] += prop_counts.get("won", 0)
        counts["lost"] += prop_counts.get("lost", 0)
        counts["push"] += prop_counts.get("push", 0)
        counts["settled"] += prop_counts.get("settled", 0)
        if prop_counts.get("settled"):
            logger.info("Player-prop settlement: %s", prop_counts)
    except Exception as e:
        logger.warning("prop settlement failed: %s", e)

    # ── Recompute self-tuning weights from the freshly-updated outcomes.
    # Cheap (pure aggregation over `picks`), runs after every settlement.
    try:
        from learning_engine import recompute_learned_weights
        weights = await recompute_learned_weights(db)
        counts["learning_buckets"] = sum(1 for b in weights.get("buckets", []) if b.get("active"))
    except Exception as e:
        logger.warning("learning recompute failed: %s", e)

    return counts
