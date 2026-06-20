"""Market Competition Engine — ranks parallel markets for the same event.

Given a pick (or event_id), find all picks for the same event and rank them
by the Market Competition formula:

    market_score = prob*0.30 + edge*0.35 + survival*0.20 - var*0.10 - counter*0.05

Returns top alternatives so the UI can show "Best Pick / Alternative" cards
within a single match.

Mounted at /api/market-rank/{pick_id} via main router include in server.py.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

router = APIRouter(tags=["market_competition"])


def _get_db():
    from server import db
    return db


def _require_auth():
    from server import current_user
    return current_user


def _market_score(p: dict) -> float:
    """Compute the user's Market Competition score formula.

    Each component normalised to a 0-100 scale so coefficients sum sensibly:
      prob       = win_probability (0-100)
      edge       = edge_percent clamped to [0, 30] then scaled to 0-100
      survival   = survival_score (already 0-100, defaults to 60)
      variance   = variance_score (0-100, defaults to 30)
      counter    = counter_score  (0-100, defaults to 0)
    """
    prob = float(p.get("win_probability") or 0)            # 0-100
    edge_raw = float(p.get("edge_percent") or 0)
    edge_norm = max(0.0, min(30.0, edge_raw)) * (100.0 / 30.0)  # → 0-100
    survival = float(p.get("survival_score") or 60.0)
    variance = float(p.get("variance_score") or 30.0)
    counter = float(p.get("counter_score") or 0.0)

    return (
        prob * 0.30
        + edge_norm * 0.35
        + survival * 0.20
        - variance * 0.10
        - counter * 0.05
    )


def _short_market(market: str) -> str:
    """Extract a short market label from a verbose 'market' string.

    Examples:
      "Manchester City to Win"       -> "Moneyline"
      "Liverpool Win or Draw"        -> "Double Chance"
      "Over 2.5 Goals"               -> "Over 2.5"
      "Both Teams To Score"          -> "BTTS"
    """
    m = (market or "").lower()
    if "win or draw" in m or "double chance" in m: return "Double Chance"
    if "both teams to score" in m or "btts" in m: return "BTTS"
    if "draw" in m and "no bet" not in m:         return "Draw"
    if "over" in m:
        for thr in ("0.5", "1.5", "2.5", "3.5", "4.5"):
            if thr in m: return f"Over {thr}"
        return "Over"
    if "under" in m:
        for thr in ("0.5", "1.5", "2.5", "3.5", "4.5"):
            if thr in m: return f"Under {thr}"
        return "Under"
    if "team total" in m:    return "Team Total"
    if "moneyline" in m or "to win" in m: return "Moneyline"
    if "spread" in m or "+1.5" in m or "-1.5" in m: return "Spread"
    if "hits" in m and "strikeout" not in m: return "Hits"
    if "outs recorded" in m: return "Outs Recorded"
    if "strikeouts" in m:    return "Strikeouts"
    if "goal scorer" in m:   return "Goal Scorer"
    # Fallback: trim selection/player prefix
    return market.split(" ")[-1] if market else "Unknown"


async def _rank_markets_for_event(db, event: str, sport: str, exclude_id: str = "") -> list[dict]:
    """Return all picks for an event, scored and sorted top to bottom."""
    cursor = db.picks.find(
        {"event": event, "sport": sport},
        {
            "_id": 0, "id": 1, "sport": 1, "market": 1, "selection": 1,
            "win_probability": 1, "edge_percent": 1, "book_odds": 1,
            "lock_score": 1, "grade": 1,
            "lock_score_v2": 1, "tier_v2": 1, "is_apex": 1,
            "counter_score": 1, "survival_score": 1, "variance_score": 1,
            "event_time": 1, "is_alt": 1, "no_bet": 1,
        },
    )
    candidates = []
    seen_short_markets: set = set()
    async for p in cursor:
        if not p.get("market"):
            continue
        if p.get("no_bet"):
            continue
        short = _short_market(p.get("market") or "")
        # Dedupe by short market label (only top of each market type)
        if short in seen_short_markets and short != "Unknown":
            continue
        seen_short_markets.add(short)
        score = _market_score(p)
        candidates.append({
            "id":               p.get("id"),
            "market":           p.get("market"),
            "short_market":     short,
            "selection":        p.get("selection"),
            "win_probability":  p.get("win_probability"),
            "edge_percent":     p.get("edge_percent"),
            "book_odds":        p.get("book_odds"),
            "lock_score":       p.get("lock_score"),
            "lock_score_v2":    p.get("lock_score_v2"),
            "tier_v2":          p.get("tier_v2"),
            "is_apex":          p.get("is_apex", False),
            "counter_score":    p.get("counter_score"),
            "survival_score":   p.get("survival_score"),
            "variance_score":   p.get("variance_score"),
            "grade":            p.get("grade"),
            "market_score":     round(score, 2),
            "is_current":       (p.get("id") == exclude_id) if exclude_id else False,
        })
    candidates.sort(key=lambda x: x["market_score"], reverse=True)
    return candidates


def _classify(ranked: list[dict]) -> dict:
    """Apply the user's display rules.

    • If best beats #2 by >5 pts → recommend ONLY best (alternatives are
      shown but flagged as inferior).
    • If top 2 within 3 pts → show both as co-best.
    • Always cap UI to top 3.
    """
    if not ranked:
        return {"best": None, "alternatives": [], "rule": "no_candidates"}
    top = ranked[:3]
    best = top[0]
    rule = "single"
    if len(top) >= 2:
        delta_1_2 = best["market_score"] - top[1]["market_score"]
        if delta_1_2 <= 3.0:
            rule = "co_best"
        elif delta_1_2 > 5.0:
            rule = "dominant"
        else:
            rule = "best_with_alts"
    return {
        "best":         best,
        "alternatives": top[1:],
        "rule":         rule,
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@router.get("/picks/{pick_id}/market-rank")
async def market_rank_for_pick(
    pick_id: str,
    user=Depends(_require_auth()),
):
    """Ranked list of every market available for the same event as `pick_id`.

    Used by the pick-detail UI to show "OTHER MARKETS IN THIS MATCH".
    """
    db = _get_db()
    pick = await db.picks.find_one({"id": pick_id}, {"_id": 0, "event": 1, "sport": 1})
    if not pick:
        raise HTTPException(404, "pick not found")
    ranked = await _rank_markets_for_event(
        db, pick["event"], pick.get("sport") or "", exclude_id=pick_id,
    )
    classified = _classify(ranked)
    return {
        "pick_id":  pick_id,
        "event":    pick["event"],
        "sport":    pick.get("sport"),
        "total":    len(ranked),
        "ranked":   ranked,
        **classified,
    }


@router.get("/market-rank/feed")
async def market_rank_feed(
    sport: str = "Soccer",
    limit: int = 20,
    user=Depends(_require_auth()),
):
    """Grouped feed view — every event in `sport` with its best + alternatives.

    Returns one entry per event, sorted by the best pick's market_score.
    """
    db = _get_db()
    # Unique events for this sport
    events = await db.picks.distinct(
        "event",
        {"sport": sport, "no_bet": {"$ne": True}, "market": {"$exists": True}},
    )
    out = []
    for event in events:
        ranked = await _rank_markets_for_event(db, event, sport)
        if not ranked:
            continue
        classified = _classify(ranked)
        out.append({"event": event, "sport": sport, "total": len(ranked), **classified})
    out.sort(key=lambda e: (e.get("best") or {}).get("market_score", 0), reverse=True)
    return {"sport": sport, "count": len(out), "events": out[:limit]}
