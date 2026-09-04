"""Market Competition Engine — ranks parallel markets for the same event.

Given a pick (or event_id), find all picks for the same event and rank them
by the Market Competition formula:

    market_score = prob*0.30 + edge*0.35 + survival*0.20 - var*0.10 - counter*0.05

Returns top alternatives so the UI can show "Best Pick / Alternative" cards
within a single match.

Mounted at /api/market-rank/{pick_id} via main router include in server.py.
"""
from __future__ import annotations

from typing import Optional
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

    2026-08-27 — canonical-truth first.  When a pick is published, its
    ``published_win_probability`` is the frozen model output that every
    other surface displays; prefer it so the ranking never disagrees
    with the pick's header.
    """
    prob = float(
        p.get("published_win_probability")
        if p.get("published_win_probability") is not None
        else (p.get("win_probability") or 0)
    )
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
    current_event_time = ""
    if exclude_id:
        cur = await db.picks.find_one(
            {"id": exclude_id},
            {"_id": 0, "selection": 1, "market": 1, "line": 1,
             "event_time": 1},
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
            current_event_time = (cur.get("event_time") or "").strip()

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

    # ── PERKLOCKS ROOT FIX (2026-09-03) — same-game filter ───────────
    # The main-board query filters by ``pick_date == today`` — but
    # this endpoint queries by ``(event, sport)`` ONLY, so every
    # historical pick for the same team matchup (Milwaukee Brewers @
    # Chicago Cubs on 08-31, 09-01, 09-02, 09-03) bled into the
    # panel.  Users then saw "Jake Bauers Over 0.5 Hits · 94 Lock"
    # in tonight's Pick Breakdown and concluded the pick was missing
    # from the board — the pick was really from THREE DAYS AGO.
    # Universal fix: bound candidates to the same series game via
    # ``event_time`` proximity to the current pick's event_time (±12 h
    # covers same-day slates and cross-midnight ET/UTC drift; nothing
    # longer, so yesterday's game can never sneak in).  When the
    # current pick has no event_time we fall back to legacy behaviour
    # so completed pick-breakdowns keep working.
    _base_query: dict = {"event": event, "sport": sport}
    if current_event_time:
        try:
            from datetime import datetime, timedelta, timezone
            _cur_dt = datetime.fromisoformat(
                current_event_time.replace("Z", "+00:00")
            )
            if _cur_dt.tzinfo is None:
                _cur_dt = _cur_dt.replace(tzinfo=timezone.utc)
            _lo = (_cur_dt - timedelta(hours=12)).isoformat().replace(
                "+00:00", "Z",
            )
            _hi = (_cur_dt + timedelta(hours=12)).isoformat().replace(
                "+00:00", "Z",
            )
            _base_query["event_time"] = {"$gte": _lo, "$lte": _hi}
        except Exception:
            pass
    cursor = db.picks.find(
        _base_query,
        {
            "_id": 0, "id": 1, "sport": 1, "market": 1, "selection": 1,
            "win_probability": 1, "edge_percent": 1, "book_odds": 1,
            "lock_score": 1, "grade": 1, "line": 1,
            "lock_score_v2": 1, "tier_v2": 1, "is_apex": 1,
            "counter_score": 1, "survival_score": 1, "variance_score": 1,
            "event_time": 1, "is_alt": 1, "no_bet": 1,
            # 2026-08-27 — Canonical publication boundary consumer fix:
            # every user-facing surface must display the IMMUTABLE
            # canonical truth when a pick has been published, so a
            # Lock 81 in the header can never disagree with a Lock 55
            # in this panel for the same wager.  See P0 root closure
            # in `SOCCER_PLAYER_GAME_TRUTH_CERTIFIED`.
            "published_lock_score": 1, "published_grade": 1,
            "published_win_probability": 1, "canonical_published_at": 1,
            # PERKLOCKS MAIN 36 · P0-1 · P0-2 — additive projection so
            # the state resolver + separate current_pick payload have
            # everything they need to label rows without collapsing
            # distinct metrics into ``market_score``.
            "signal_score": 1,
            "publication_block_reason": 1, "publication_reject_reason": 1,
            "publish_block_reason": 1, "canonical_reject_reason": 1,
        },
    )
    # PHASE 0 §7 (2026-06) — Score ALL candidates BEFORE dedupe.
    # OLD: dedupe by short_market happened INSIDE the async iteration,
    # so the first Over/Under/etc row scanned won; a later, higher-
    # scoring row on the same short_market was silently dropped.
    # NEW: two-pass — (1) collect + score every eligible candidate,
    # (2) dedupe by short_market keeping the HIGHEST market_score.
    raw_candidates = []
    async for p in cursor:
        if not p.get("market"):
            continue
        if p.get("no_bet"):
            continue
        # Skip literal self AND cross-book duplicates of the current wager.
        if _is_same_canonical_wager(p):
            continue
        # Side-alignment filter — safe to apply pre-score (it's a hard
        # eligibility rule, not a ranking tiebreaker).
        if not _sides_compatible(current_side, p):
            continue
        short = _short_market(p.get("market") or "")
        score = _market_score(p)
        # 2026-08-27 — Canonical truth first: prefer immutable published
        # values so this panel and the pick header never disagree.  The
        # legacy mutable fields are retained ONLY as fallback for
        # pre-publication picks (which never reach the user anyway when
        # canonical publication is healthy).
        _pub_ls = p.get("published_lock_score")
        _pub_pg = p.get("published_grade")
        _pub_wp = p.get("published_win_probability")
        raw_candidates.append({
            "id":               p.get("id"),
            "market":           p.get("market"),
            "short_market":     short,
            "selection":        p.get("selection"),
            "win_probability":  _pub_wp if _pub_wp is not None else p.get("win_probability"),
            "edge_percent":     p.get("edge_percent"),
            "book_odds":        p.get("book_odds"),
            "lock_score":       _pub_ls if _pub_ls is not None else p.get("lock_score"),
            "lock_score_v2":    p.get("lock_score_v2"),
            "tier_v2":          p.get("tier_v2"),
            "is_apex":          p.get("is_apex", False),
            "counter_score":    p.get("counter_score"),
            "survival_score":   p.get("survival_score"),
            "variance_score":   p.get("variance_score"),
            "grade":            _pub_pg or p.get("grade"),
            "market_score":     round(score, 2),
            "signal_score":     p.get("signal_score"),   # kept distinct from lock_score
            "is_current":       False,
            # PERKLOCKS MAIN 36 · P0-1 — explicit state so the UI
            # never confuses a research alt with a Published Lock.
            "state":                    _pick_state(p),
            "non_publication_reason":   _non_publication_reason(p),
        })

    # Pass 2 — dedupe by short_market keeping the highest market_score.
    # "Unknown" short markets are NEVER deduped so we don't collapse
    # unrelated markets that failed classification into one group.
    best_by_short: dict[str, dict] = {}
    unknown_candidates: list[dict] = []
    for c in raw_candidates:
        short = c["short_market"]
        if short == "Unknown":
            unknown_candidates.append(c)
            continue
        prev = best_by_short.get(short)
        if prev is None or c["market_score"] > prev["market_score"]:
            best_by_short[short] = c
    candidates = list(best_by_short.values()) + unknown_candidates
    candidates.sort(key=lambda x: x["market_score"], reverse=True)
    return candidates


def _pick_state(p: dict) -> str:
    """PERKLOCKS MAIN 36 · P0-1 — explicit non-Locks state labels.

    Every Pick Breakdown / Market Competition row carries one of:

      • PUBLISHED_LOCK       — passed canonical publication; appears on Locks.
      • RESEARCH_ALTERNATIVE — high model score but did NOT publish.
      • INELIGIBLE           — flagged no_bet / disqualified pre-publication.
      • UNAVAILABLE          — missing critical evidence (no line/odds).

    A high-score research row must never masquerade as a published Lock;
    only PUBLISHED_LOCK rows may drive the canonical Locks board.
    """
    if p.get("no_bet") is True:
        return "INELIGIBLE"
    if p.get("book_odds") in (None, "") or p.get("win_probability") in (None, ""):
        return "UNAVAILABLE"
    # PUBLISHED_LOCK — the immutable ``canonical_published_at``
    # timestamp (set by PublishedPickContract when the pick passed
    # the 85+ publication rule) is the sole source of truth.  A
    # ``published_lock_score`` alone also proves the boundary was
    # crossed.  Otherwise the row is research-only.
    if p.get("canonical_published_at") or p.get("published_lock_score") is not None:
        return "PUBLISHED_LOCK"
    return "RESEARCH_ALTERNATIVE"


def _non_publication_reason(p: dict) -> Optional[str]:
    """Return a human-readable reason a high-model-score row didn't
    publish, when the pick doc carries one; else None."""
    for k in (
        "publication_block_reason", "publication_reject_reason",
        "publish_block_reason", "publish_reason",
        "canonical_reject_reason",
    ):
        v = p.get(k)
        if v:
            return str(v)
    return None


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

    PERKLOCKS MAIN 36 · P0-2 — response contract fix.  Previously
    ``market_score`` was only computed for ALTERNATIVES (the current
    pick was excluded from the ranked list), so the frontend fell
    back to 0 when it couldn't find ``is_current`` in the response.
    Now the endpoint returns the current pick separately with its
    OWN market_score using the SAME formula, and each row carries
    ``state`` + ``signal_score`` explicitly so the UI never merges
    or fabricates comparison metrics.  Missing metric → null, never 0.
    """
    db = _get_db()
    current = await db.picks.find_one(
        {"id": pick_id}, {"_id": 0}
    )
    if not current:
        raise HTTPException(404, "pick not found")
    ranked = await _rank_markets_for_event(
        db, current["event"], current.get("sport") or "", exclude_id=pick_id,
    )
    classified = _classify(ranked)

    # Compute the current pick's market_score using the SAME formula
    # and expose the metrics separately so the UI compares like to like.
    def _score_or_none(p: dict) -> Optional[float]:
        # Guard against missing win_probability / edge — return None
        # (NOT 0) so the UI can render N/A.
        if p.get("win_probability") in (None, "") \
                or p.get("edge_percent") in (None, ""):
            return None
        try:
            return round(_market_score(p), 2)
        except Exception:
            return None

    # PERKLOCKS MAIN 37 · P0.2/P0.3 — canonical read via
    # ``PublishedPickContract`` instead of recreating
    # published_* precedence locally.  This closes the exact
    # duplication warning ("don't recreate published_grade > grade
    # in two more places") — the contract owns the precedence rule.
    try:
        from services.published_pick_contract import PublishedPickContract as _PPC
        _cc = _PPC.from_pick(current).as_dict()
    except Exception:
        _cc = {}
    _pub_ls = _cc.get("published_lock_score")
    _pub_pg = _cc.get("published_grade")
    # win_probability precedence is not yet in PublishedPickContract's
    # public surface (win_expected is 0-1 unit); keep the local
    # legacy fallback for the wire but drive the canonical Lock badge
    # (lock_score + grade) from the contract.
    _pub_wp = current.get("published_win_probability")
    current_row = {
        "id":               current.get("id"),
        "market":           current.get("market"),
        "short_market":     _short_market(current.get("market") or ""),
        "selection":        _cc.get("selection") or current.get("selection"),
        "win_probability":  _pub_wp if _pub_wp is not None else current.get("win_probability"),
        "edge_percent":     current.get("edge_percent"),
        "book_odds":        _cc.get("published_odds") or current.get("book_odds"),
        # Canonical Lock authority — contract-first, legacy only when
        # the contract has no published value at all (pre-canonical
        # historical rows).
        "lock_score":       _pub_ls if _pub_ls is not None else current.get("lock_score"),
        "lock_score_v2":    current.get("lock_score_v2"),
        "tier_v2":          current.get("tier_v2"),
        "is_apex":          current.get("is_apex", False),
        "counter_score":    current.get("counter_score"),
        "survival_score":   current.get("survival_score"),
        "variance_score":   current.get("variance_score"),
        "grade":            _pub_pg or current.get("grade"),
        # signal_score is a SEPARATE research metric (slate-wide
        # percentile rank, 0-100) — see
        # ``services.signal_engine.engine`` — NEVER merge it into
        # lock_score / grade.  Explicit label surfaces on the wire so
        # UI never treats it as an authoritative Lock number.
        "signal_score":       current.get("signal_score"),
        "signal_score_label": "Research Signal (0-100 percentile)",
        "market_score":     _score_or_none(current),
        "is_current":       True,
        "state":            _pick_state(current),
        "non_publication_reason": _non_publication_reason(current),
        # Immutable contract published on the row so the frontend can
        # read the SAME canonical truth here as in /picks/today and
        # /{id}/matchup.  Zero-drift by construction.
        "published_pick_contract": _cc,
    }

    return {
        "pick_id":       pick_id,
        "event":         current["event"],
        "sport":         current.get("sport"),
        "total":         len(ranked),
        "ranked":        ranked,
        "current_pick":  current_row,   # P0-2 — SAME metric as alternatives.
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
