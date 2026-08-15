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
      "Manchester City to Win"           -> "Moneyline"
      "Liverpool Win or Draw"            -> "Double Chance"
      "Over 2.5 Goals"                   -> "Over 2.5"
      "Total Games Over 23.5"            -> "Over 23.5"   (NOT "Over 3.5")
      "Both Teams To Score"              -> "BTTS"
    """
    import re as _re
    m = (market or "").lower()
    if "win or draw" in m or "double chance" in m: return "Double Chance"
    if "both teams to score" in m or "btts" in m: return "BTTS"
    if "draw" in m and "no bet" not in m:         return "Draw"
    # Extract the FULL numeric line after "over"/"under" (not just the
    # last decimal substring). Earlier impl matched "3.5" inside "23.5"
    # because of a naive substring search — user spotted the resulting
    # mis-label: "You got 3.5 shouldn't it be 23.5".
    over_match = _re.search(r"\bover\s+(\d+(?:\.\d+)?)", m)
    if over_match:
        return f"Over {over_match.group(1)}"
    under_match = _re.search(r"\bunder\s+(\d+(?:\.\d+)?)", m)
    if under_match:
        return f"Under {under_match.group(1)}"
    if "team total" in m:    return "Team Total"
    if "moneyline" in m or "to win" in m: return "Moneyline"
    # Tennis alt-spread: "Frances Tiafoe -0.5 Games (Alt)" → "Spread -0.5"
    games_spread = _re.search(r"([+-]?\d+(?:\.\d+)?)\s+games", m)
    if games_spread:
        return f"Spread {games_spread.group(1)}"
    if "spread" in m or "+1.5" in m or "-1.5" in m: return "Spread"
    if "hits" in m and "strikeout" not in m: return "Hits"
    if "outs recorded" in m: return "Outs Recorded"
    if "strikeouts" in m:    return "Strikeouts"
    if "goal scorer" in m:   return "Goal Scorer"
    # Fallback: trim selection/player prefix
    return market.split(" ")[-1] if market else "Unknown"


# ── Side-alignment filtering ─────────────────────────────────────────
# Markets that DON'T pick a side (totals, BTTS) are always considered
# "compatible" with any side-biased pick. Everything else needs the
# selection / pick-side to align with the user's current pick.
NEUTRAL_MARKETS = {
    "Over 0.5", "Over 1.5", "Over 2.5", "Over 3.5", "Over 4.5",
    "Under 0.5", "Under 1.5", "Under 2.5", "Under 3.5", "Under 4.5",
    "Over", "Under", "BTTS",
}


def _pick_side(p: dict) -> str:
    """Best-effort extraction of the SIDE (team / player / Over / Under)
    a pick favours. Returns "" if the pick is side-neutral (totals)."""
    sel = (p.get("selection") or "").strip()
    if sel:
        return sel
    # Try to parse from the market string
    mkt = p.get("market") or ""
    # "Sweden to Win" → "Sweden"
    if " to Win" in mkt:
        return mkt.split(" to Win")[0].strip()
    # "Netherlands Win or Draw" → "Netherlands"
    if " Win or Draw" in mkt:
        return mkt.split(" Win or Draw")[0].strip()
    # "Netherlands +1.5 Spread" → "Netherlands"
    if " Spread" in mkt:
        return mkt.split(" ")[0].strip()
    # Anything else (Over/Under totals) → side-neutral
    return ""


def _sides_compatible(current_side: str, candidate: dict) -> bool:
    """Return True if the candidate market is either:
       • side-neutral (totals, BTTS), or
       • on the SAME side as the current pick (same team / player).
    Excludes opposite-side picks (e.g., Sweden Moneyline when current
    pick is Netherlands Win or Draw). Without this filter the Market
    Competition panel showed contradictory "alternatives" — user spec:
    "u got Netherlands win or draw but Sweden ml is this right?"."""
    if not current_side:
        # Current pick is itself side-neutral (e.g., Over 2.5) — any
        # candidate is fine; the rank formula does the rest.
        return True
    short = _short_market(candidate.get("market") or "")
    if short in NEUTRAL_MARKETS:
        return True
    cand_side = _pick_side(candidate)
    if not cand_side:
        return True   # candidate is neutral
    # Case-insensitive equality; tolerate small whitespace / suffix drift
    return cand_side.lower().strip() == current_side.lower().strip()


async def _rank_markets_for_event(db, event: str, sport: str, exclude_id: str = "") -> list[dict]:
    """Return all picks for an event, scored and sorted top to bottom.

    Filters out picks that contradict the SIDE of the current pick.
    Without this, the panel was showing e.g. "Sweden Moneyline" as a
    sibling of "Netherlands Win or Draw" — two opposite-side bets that
    cannot both be the best play in the same match.

    SOCCER_REGRESSION_RUNTIME §8 — cross-book duplicates of the CURRENT
    wager must be filtered out.  When the same Over 2.5 @ 2.5 line
    exists at 5 sportsbooks, the ID-only exclusion left 4 duplicates
    behind — the panel then showed "Over 2.5 scores 54 vs your pick
    at 97" as a self-comparison.  We now also compare on the canonical
    wager key (short_market + selection + line) to catch same-wager
    cross-book duplicates that were not deduped upstream.
    """
    current_side = ""
    current_short = ""
    current_sel_norm = ""
    current_line_norm = ""
    if exclude_id:
        cur = await db.picks.find_one(
            {"id": exclude_id},
            {"_id": 0, "selection": 1, "market": 1, "line": 1},
        )
        if cur:
            current_side = _pick_side(cur)
            current_short = _short_market(cur.get("market") or "")
            current_sel_norm = (cur.get("selection") or "").strip().lower()
            line = cur.get("line")
            try:
                current_line_norm = "" if line is None else f"{float(line):g}"
            except Exception:
                current_line_norm = str(line)

    def _is_same_canonical_wager(p: dict) -> bool:
        """Return True when `p` is a cross-book duplicate of the
        current selection (same short_market + selection + line)."""
        if not exclude_id:
            return False
        if p.get("id") == exclude_id:
            return True   # literal self
        p_short = _short_market(p.get("market") or "")
        if p_short != current_short:
            return False
        p_sel = (p.get("selection") or "").strip().lower()
        if p_sel != current_sel_norm:
            return False
        p_line = p.get("line")
        try:
            p_line_norm = "" if p_line is None else f"{float(p_line):g}"
        except Exception:
            p_line_norm = str(p_line)
        return p_line_norm == current_line_norm

    cursor = db.picks.find(
        {"event": event, "sport": sport},
        {
            "_id": 0, "id": 1, "sport": 1, "market": 1, "selection": 1,
            "win_probability": 1, "edge_percent": 1, "book_odds": 1,
            "lock_score": 1, "grade": 1, "line": 1,
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
        # Skip literal self AND cross-book duplicates of the current wager.
        if _is_same_canonical_wager(p):
            continue
        # Side-alignment filter (same as before).
        if not _sides_compatible(current_side, p):
            continue
        short = _short_market(p.get("market") or "")
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
            "is_current":       False,
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
