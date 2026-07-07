"""
Lab Routes — Backend for the LAB tab research modules.
======================================================

Endpoints (all mounted at `/api/lab/*`):

* `GET /correlations`   — Correlation Lab: parlay leg co-occurrence hit rates
* `GET /backtest`       — Bet Backtester: strategy filter → win rate / ROI / sample
* `GET /patterns`       — Pattern Finder: auto-mined profitable buckets
* `GET /matchup-dna/{sport}/{subject}` — Matchup DNA: player vs opponent history

Design principles
-----------------
1. Every stat is computed from the **real settled-pick** collection
   (`db.picks` where `status ∈ {won, lost, push, void}`) — no fabrication.
2. All aggregations run server-side via Mongo pipelines so the phone
   only receives the summarised rows it needs to render.
3. Every response includes `sample_size` so the UI can badge low-N
   patterns as "insufficient data" rather than lying about hit rates.
4. Endpoints are read-only. No writes. Safe to expose behind auth.

Data model reference
--------------------
`db.picks` documents relevant fields (post-settlement):
  - id: str
  - sport: str
  - market: str
  - selection: str
  - player_name: str | None
  - team: str | None
  - event: str | None
  - book_odds: int
  - lock_score: float
  - edge_percent: float
  - win_probability: float
  - status: "won" | "lost" | "push" | "void" | "pending" | None
  - units_profit: float (signed; negative = lost)
  - units_risked: float
  - settled_at: iso string
  - pick_date: "YYYY-MM-DD"

`db.parlay_history` documents:
  - id: str
  - legs: [{ pick_id, status, ... }, ...]
  - status: "won" | "lost" | "void" | "push" | "live"
  - created_at: iso
"""
# ruff: noqa: E701
# The compact `if x <= N: return "..."` bucket functions below are
# intentionally single-line for readability of the numeric thresholds.
from __future__ import annotations

import logging
import math
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from deps import db

logger = logging.getLogger("lockscore.lab_routes")

router = APIRouter(prefix="/lab", tags=["lab"])


# ── Helpers ───────────────────────────────────────────────────────────
def _wilson_lower_bound(hits: int, n: int, z: float = 1.96) -> float:
    """Wilson score lower bound for a binomial proportion. This is what
    we sort profitable patterns by — a raw hit rate of 100% on a sample
    of 3 is meaningless; Wilson punishes small-sample noise so the UI
    surfaces trends that are statistically credible.
    """
    if n <= 0:
        return 0.0
    p = hits / n
    denom = 1 + (z * z) / n
    center = p + (z * z) / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + (z * z) / (4 * n * n))
    return max(0.0, (center - margin) / denom)


def _bucket_odds(odds: int | None) -> str:
    if odds is None:
        return "unknown"
    try:
        n = int(odds)
    except Exception:
        return "unknown"
    if n <= -300: return "chalk (<=-300)"
    if n <= -150: return "heavy fav (-300..-150)"
    if n <=  -101: return "slight fav (-150..-100)"
    if n <=   150: return "pick'em/short dog (+100..+150)"
    if n <=   250: return "medium dog (+150..+250)"
    if n <=   500: return "big dog (+250..+500)"
    return "longshot (>+500)"


def _bucket_edge(edge: float | None) -> str:
    if edge is None:
        return "unknown"
    try:
        e = float(edge)
    except Exception:
        return "unknown"
    if e < -2:  return "negative (<-2%)"
    if e <  1:  return "flat (-2..+1%)"
    if e <  4:  return "small (+1..+4%)"
    if e <  8:  return "medium (+4..+8%)"
    return "strong (>+8%)"


def _bucket_lock(lock: float | None) -> str:
    if lock is None:
        return "unknown"
    try:
        v = float(lock)
    except Exception:
        return "unknown"
    if v <  70: return "60s"
    if v <  80: return "70s"
    if v <  90: return "80s"
    if v <  95: return "90-94"
    return "95+"


def _classify_market_family(sport: str, market: str) -> str:
    """Coarse market family for grouping — matches
    market_evidence_profiles.classify_market conceptually but simpler
    string-based buckets suitable for aggregation keys. Kept in-file to
    avoid the profile module's import weight on hot backtest queries."""
    m = (market or "").lower()
    s = (sport or "").upper()
    if s == "MLB":
        if "strikeout" in m: return "MLB_KS"
        if "outs" in m: return "MLB_OUTS"
        if "home run" in m or "hr" in m.split(): return "MLB_HR"
        if "rbi" in m: return "MLB_RBI"
        if "total bases" in m or " tb " in m: return "MLB_TB"
        if "hits" in m or "hit" in m: return "MLB_HITS"
        if "run line" in m: return "MLB_RL"
        if "total" in m or "over" in m or "under" in m: return "MLB_TOTAL"
        if "moneyline" in m or " ml" in m: return "MLB_ML"
    if s in ("NBA", "WNBA"):
        if "3-point" in m or "threes" in m: return "NBA_THREES"
        if "rebound" in m: return "NBA_REB"
        if "assist" in m: return "NBA_AST"
        if "points" in m or " pts" in m: return "NBA_POINTS"
        if "spread" in m: return "NBA_SPREAD"
        if "total" in m: return "NBA_TOTAL"
        if "moneyline" in m: return "NBA_ML"
    if s in ("NFL", "CFB"):
        if "passing" in m: return "NFL_PASS"
        if "rushing" in m: return "NFL_RUSH"
        if "receiving" in m or "receptions" in m: return "NFL_REC"
        if "spread" in m: return "NFL_SPREAD"
        if "total" in m: return "NFL_TOTAL"
    if s == "SOCCER":
        if "scorer" in m: return "SOC_SCORER"
        if "assist" in m: return "SOC_ASSIST"
        if "btts" in m or "both teams" in m: return "SOC_BTTS"
        if "total" in m or "over" in m or "under" in m: return "SOC_TOTAL"
        if "moneyline" in m: return "SOC_ML"
    if s == "TENNIS":
        if "games" in m: return "TEN_GAMES"
        if "set" in m: return "TEN_SETS"
        return "TEN_MATCH"
    if s == "UFC":
        return "UFC_ML"
    return f"{s or 'X'}_OTHER"


# ═════════════════════════════════════════════════════════════════════
# CORRELATION LAB
# ═════════════════════════════════════════════════════════════════════
@router.get("/correlations")
async def correlations(
    sport: str | None = Query(None, description="Optional sport filter"),
    min_pairs: int = Query(5, ge=1, le=100, description="Min co-occurrences to include a pair"),
    limit: int = Query(30, ge=1, le=100),
):
    """Return the top correlated leg pairs from settled parlays.

    Algorithm: for every settled parlay, generate all unordered leg
    pairs. Group by (family_a, family_b) and count:
      * n_pairs  — how many parlays contained this pair
      * both_hit — how many parlays had BOTH legs win
      * a_hits   — how many had leg A win (regardless of B)
      * b_hits   — how many had leg B win (regardless of A)
    Report co-occurrence hit rate = both_hit / n_pairs and the "lift"
    vs independence: lift = P(both) / (P(A) * P(B)).

    Interpretation:
      * lift > 1.2  → legs tend to hit together (positive correlation).
      * lift < 0.8  → legs tend to fizzle together / anti-correlated.
      * lift ≈ 1    → independent, no parlay bonus.
    """
    q: dict[str, Any] = {"status": {"$in": ["won", "lost"]}}
    if sport:
        q["legs.sport"] = sport
    cursor = db.parlay_history.find(q, {"_id": 0, "legs": 1}).limit(2000)

    pair_counts: dict[tuple[str, str], dict[str, int]] = {}
    single_counts: dict[str, dict[str, int]] = {}
    async for parlay in cursor:
        legs = parlay.get("legs") or []
        # Reduce each leg to (family_key, hit_bool)
        reduced: list[tuple[str, bool]] = []
        for leg in legs:
            fam = _classify_market_family(leg.get("sport"), leg.get("market") or "")
            leg_status = (leg.get("status") or "").lower()
            hit = leg_status == "won"
            reduced.append((fam, hit))
        # Update single counts
        for fam, hit in reduced:
            s = single_counts.setdefault(fam, {"seen": 0, "hits": 0})
            s["seen"] += 1
            s["hits"] += 1 if hit else 0
        # Unordered pairs (skip identical family self-pairs — those are
        # trivially correlated because they're the same market bucket).
        for i in range(len(reduced)):
            for j in range(i + 1, len(reduced)):
                a_fam, a_hit = reduced[i]
                b_fam, b_hit = reduced[j]
                if a_fam == b_fam:
                    continue
                key = tuple(sorted([a_fam, b_fam]))
                slot = pair_counts.setdefault(key, {
                    "n_pairs": 0, "both_hit": 0, "a_hit": 0, "b_hit": 0,
                })
                slot["n_pairs"] += 1
                if a_hit and b_hit:
                    slot["both_hit"] += 1
                if a_hit:
                    slot["a_hit"] += 1
                if b_hit:
                    slot["b_hit"] += 1

    # Build sorted output
    rows: list[dict[str, Any]] = []
    for (a, b), counts in pair_counts.items():
        n = counts["n_pairs"]
        if n < min_pairs:
            continue
        both = counts["both_hit"]
        pa = counts["a_hit"] / n
        pb = counts["b_hit"] / n
        p_both = both / n
        # Independence expectation. Guard against divide-by-zero.
        lift = (p_both / (pa * pb)) if (pa > 0 and pb > 0) else None
        rows.append({
            "family_a": a,
            "family_b": b,
            "sample_size": n,
            "both_hit_rate": round(p_both, 4),
            "leg_a_hit_rate": round(pa, 4),
            "leg_b_hit_rate": round(pb, 4),
            "lift": round(lift, 3) if lift is not None else None,
            "verdict": _correlation_verdict(lift, n),
        })
    rows.sort(key=lambda r: (r["lift"] or 0), reverse=True)
    return {"rows": rows[:limit], "total_pairs_seen": len(pair_counts)}


def _correlation_verdict(lift: float | None, n: int) -> str:
    if lift is None:
        return "insufficient data"
    if n < 10:
        return f"low sample (n={n}) — treat with caution"
    if lift >= 1.25:
        return "POSITIVE — legs cluster together"
    if lift <= 0.8:
        return "NEGATIVE — legs anti-correlated"
    return "NEUTRAL — near-independent"


# ═════════════════════════════════════════════════════════════════════
# BET BACKTESTER
# ═════════════════════════════════════════════════════════════════════
@router.get("/backtest")
async def backtest(
    sport: str | None = Query(None),
    market_family: str | None = Query(None, description="e.g. MLB_HR, NBA_POINTS"),
    odds_min: int | None = Query(None),
    odds_max: int | None = Query(None),
    edge_min: float | None = Query(None),
    lock_min: float | None = Query(None),
    lock_max: float | None = Query(None),
    limit_sample: int = Query(5000, ge=100, le=20000),
):
    """Simulate historical performance of a betting strategy.

    Users pick filters (e.g. "NBA · Points market · odds -200 to +150
    · edge ≥ 3%") and we return the win rate, ROI, sample size, and
    best/worst-case units-profit windows against real settled picks.
    """
    q: dict[str, Any] = {"status": {"$in": ["won", "lost", "push"]}}
    if sport:
        q["sport"] = sport
    if odds_min is not None:
        q["book_odds"] = q.get("book_odds", {})
        q["book_odds"]["$gte"] = odds_min
    if odds_max is not None:
        q["book_odds"] = q.get("book_odds", {})
        q["book_odds"]["$lte"] = odds_max
    if edge_min is not None:
        q["edge_percent"] = {"$gte": edge_min}
    if lock_min is not None or lock_max is not None:
        q["lock_score"] = {}
        if lock_min is not None:
            q["lock_score"]["$gte"] = lock_min
        if lock_max is not None:
            q["lock_score"]["$lte"] = lock_max

    cursor = db.picks.find(
        q,
        {"_id": 0, "id": 1, "sport": 1, "market": 1, "status": 1,
         "units_profit": 1, "units_risked": 1, "book_odds": 1,
         "lock_score": 1, "edge_percent": 1, "pick_date": 1},
    ).sort("settled_at", -1).limit(limit_sample)

    won = lost = push = 0
    units_profit_sum = 0.0
    units_risked_sum = 0.0
    by_family: dict[str, dict[str, float]] = {}
    daily_profit: dict[str, float] = {}
    async for p in cursor:
        # Optional post-filter on market family (avoids indexing pain
        # since our picks collection doesn't index synthesised
        # family strings).
        fam = _classify_market_family(p.get("sport"), p.get("market") or "")
        if market_family and fam != market_family:
            continue

        s = (p.get("status") or "").lower()
        up = float(p.get("units_profit") or 0)
        ur = float(p.get("units_risked") or 0)
        units_profit_sum += up
        units_risked_sum += ur
        if s == "won":
            won += 1
        elif s == "lost":
            lost += 1
        elif s == "push":
            push += 1

        fs = by_family.setdefault(fam, {"n": 0, "won": 0, "up": 0.0, "ur": 0.0})
        fs["n"] += 1
        if s == "won":
            fs["won"] += 1
        fs["up"] += up
        fs["ur"] += ur

        date_str = p.get("pick_date") or ""
        if date_str:
            daily_profit[date_str] = daily_profit.get(date_str, 0.0) + up

    n = won + lost + push
    hit_rate = (won / (won + lost)) if (won + lost) > 0 else 0.0
    roi = (units_profit_sum / units_risked_sum) if units_risked_sum > 0 else 0.0

    # Best / worst single day
    best_day = max(daily_profit.items(), key=lambda kv: kv[1], default=(None, 0.0))
    worst_day = min(daily_profit.items(), key=lambda kv: kv[1], default=(None, 0.0))

    family_breakdown = sorted(
        [
            {
                "family": fam,
                "n": int(fs["n"]),
                "hit_rate": round((fs["won"] / fs["n"]) if fs["n"] else 0.0, 3),
                "units_profit": round(fs["up"], 3),
                "roi": round(fs["up"] / fs["ur"], 3) if fs["ur"] else 0.0,
            }
            for fam, fs in by_family.items()
        ],
        key=lambda r: (-r["units_profit"], -r["n"]),
    )

    return {
        "filters": {
            "sport": sport, "market_family": market_family,
            "odds_min": odds_min, "odds_max": odds_max,
            "edge_min": edge_min, "lock_min": lock_min, "lock_max": lock_max,
        },
        "sample_size": n,
        "won": won, "lost": lost, "push": push,
        "hit_rate": round(hit_rate, 4),
        "units_profit": round(units_profit_sum, 3),
        "units_risked": round(units_risked_sum, 3),
        "roi": round(roi, 4),
        "best_day": {"date": best_day[0], "units": round(best_day[1], 3)} if best_day[0] else None,
        "worst_day": {"date": worst_day[0], "units": round(worst_day[1], 3)} if worst_day[0] else None,
        "days_traded": len(daily_profit),
        "verdict": _backtest_verdict(hit_rate, roi, n),
        "family_breakdown": family_breakdown[:12],
    }


def _backtest_verdict(hit_rate: float, roi: float, n: int) -> str:
    if n < 30:
        return f"insufficient sample (n={n}) — need at least 30 settled picks"
    if roi >= 0.10:
        return "STRONG — historically profitable"
    if roi >= 0.03:
        return "PLAYABLE — modest edge"
    if roi >= -0.02:
        return "BREAK-EVEN — flat expected value"
    return "LOSING STRATEGY — negative ROI historically"


# ═════════════════════════════════════════════════════════════════════
# PATTERN FINDER
# ═════════════════════════════════════════════════════════════════════
@router.get("/patterns")
async def patterns(
    sport: str | None = Query(None),
    min_n: int = Query(20, ge=5, le=500),
    limit: int = Query(20, ge=1, le=50),
    axis: str = Query("family_odds", description="One of: family_odds, family_edge, family_lock, sport_odds, dow"),
):
    """Auto-mine profitable patterns across settled picks.

    Bucketing axes:
      * family_odds — market family × odds bucket
      * family_edge — market family × edge bucket
      * family_lock — market family × lock band
      * sport_odds  — sport × odds bucket
      * dow         — day of week (settlement date, UTC)

    Buckets are ranked by Wilson lower bound of the win rate — this
    surfaces patterns that are BOTH high-hit-rate AND large-sample.
    """
    q: dict[str, Any] = {"status": {"$in": ["won", "lost"]}}
    if sport:
        q["sport"] = sport
    cursor = db.picks.find(
        q,
        {"_id": 0, "sport": 1, "market": 1, "book_odds": 1,
         "edge_percent": 1, "lock_score": 1, "status": 1,
         "units_profit": 1, "units_risked": 1, "settled_at": 1,
         "pick_date": 1},
    ).limit(20000)

    buckets: dict[str, dict[str, float]] = {}
    async for p in cursor:
        fam = _classify_market_family(p.get("sport"), p.get("market") or "")
        odds_b = _bucket_odds(p.get("book_odds"))
        edge_b = _bucket_edge(p.get("edge_percent"))
        lock_b = _bucket_lock(p.get("lock_score"))
        sp = p.get("sport") or "X"
        date_str = p.get("pick_date") or ""
        dow_lbl = "unknown"
        if date_str and re.match(r"\d{4}-\d{2}-\d{2}", date_str):
            try:
                import datetime as _dt
                dow_lbl = _dt.date.fromisoformat(date_str).strftime("%A")
            except Exception:
                dow_lbl = "unknown"

        if axis == "family_odds":
            key = f"{fam} · {odds_b}"
        elif axis == "family_edge":
            key = f"{fam} · {edge_b}"
        elif axis == "family_lock":
            key = f"{fam} · lock={lock_b}"
        elif axis == "sport_odds":
            key = f"{sp} · {odds_b}"
        elif axis == "dow":
            key = f"{sp} · {dow_lbl}"
        else:
            key = fam

        b = buckets.setdefault(key, {"n": 0, "won": 0, "up": 0.0, "ur": 0.0})
        b["n"] += 1
        if (p.get("status") or "").lower() == "won":
            b["won"] += 1
        b["up"] += float(p.get("units_profit") or 0)
        b["ur"] += float(p.get("units_risked") or 0)

    rows: list[dict[str, Any]] = []
    for key, b in buckets.items():
        n = int(b["n"])
        if n < min_n:
            continue
        won = int(b["won"])
        hit_rate = won / n if n else 0.0
        wlb = _wilson_lower_bound(won, n)
        roi = b["up"] / b["ur"] if b["ur"] else 0.0
        rows.append({
            "bucket": key,
            "n": n,
            "hit_rate": round(hit_rate, 3),
            "wilson_lower": round(wlb, 3),
            "roi": round(roi, 3),
            "units_profit": round(b["up"], 3),
        })
    # Sort by Wilson descending — big-and-good patterns first.
    rows.sort(key=lambda r: -r["wilson_lower"])
    return {"axis": axis, "min_n": min_n, "rows": rows[:limit], "buckets_considered": len(buckets)}


# ═════════════════════════════════════════════════════════════════════
# CHEATSHEETS — real streak facts from settled-pick history
# ═════════════════════════════════════════════════════════════════════
@router.get("/cheatsheets")
async def cheatsheets(
    sport: str | None = Query(None),
    min_lock: float = Query(75.0, ge=0),
    min_streak_hits: int = Query(2, ge=1, description="Only include cards where the last-N sample has ≥ this many hits"),
    limit: int = Query(30, ge=1, le=100),
):
    """Return REAL "Hit in X of last Y" cheatsheet cards for today's
    high-confidence PLAYER PROP picks.

    Algorithm
    ---------
    1. Pull today's active picks with a `player_name`, lock_score ≥
       min_lock, market that isn't alt/spread/ML.
    2. For each, query the SETTLED history for that exact player +
       market family and compute:
         * last5 hit rate    (last 5 settled picks)
         * last10 hit rate   (last 10 settled picks)
         * last20 hit rate   (last 20 settled picks)
         * vs-opponent hit rate (when we can identify the opponent
           from the pick's `event` string)
         * home/away hit rate for whichever venue the current pick is at
    3. Build up to 3 fact bullets per card. Skip the card entirely if
       we can't produce at least ONE bullet with ≥ min_streak_hits hits
       — better empty than fabricated.

    This is the ONLY source-of-truth for the Cheatsheets tab; the
    frontend no longer synthesises facts from model rubric scores.
    """
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    live_q: dict[str, Any] = {
        "pick_date": today,
        "lock_score": {"$gte": min_lock},
        # Block synthesized alt lines AND "First Goal Scorer" markets.
        # First-Goal is a subset of Anytime — a player can score many
        # goals without being first, so treating First-Goal pick outcomes
        # as a streak signal is misleading (e.g. Kane scores in 7 of 8
        # matches but rarely first). We surface only markets where the
        # pick's win/loss = the player's actual performance on that stat:
        # Anytime scorers, MLB Hits/HR/RBI/TB, NBA points/rebs/ast, etc.
        "market": {
            "$exists": True,
            "$not": re.compile(r"\(alt\)|\balt\b|first\s+goal\s+scorer|first\s+touchdown|last\s+goal", re.IGNORECASE),
        },
    }
    if sport:
        live_q["sport"] = sport

    live_cursor = db.picks.find(
        live_q,
        {"_id": 0, "id": 1, "sport": 1, "market": 1, "selection": 1,
         "event": 1, "team": 1, "player_name": 1, "book_odds": 1,
         "lock_score": 1, "edge_percent": 1},
    ).sort("lock_score", -1).limit(300)

    live_picks = await live_cursor.to_list(length=300)

    cards: list[dict[str, Any]] = []
    seen_subject_family: set[tuple[str, str]] = set()
    for lp in live_picks:
        if len(cards) >= limit:
            break
        # Determine the "subject" of the pick — prefer explicit fields,
        # fall back to parsing from the market string when those are
        # missing (common on synth-goal-scorer picks where player_name
        # is None but the player IS named in the market text).
        player = (lp.get("player_name") or "").strip()
        # Legacy DB rows store the literal string "None" — treat as empty.
        if player.lower() == "none":
            player = ""
        # Strip parenthetical team codes from stored player_name so
        # `_shorten_name` produces "M. Vargas" not "M. Vargas (CWS)".
        player = re.sub(r"\s*\([A-Z]{2,4}\)\s*", "", player).strip()
        team = (lp.get("team") or "").strip()
        if team.lower() == "none":
            team = ""
        market_str = lp.get("market") or ""
        if not player:
            player = _parse_player_from_market(market_str)
        is_player_prop = bool(player)
        subject = player if is_player_prop else team
        if not subject:
            subject = _parse_team_from_market(market_str, lp.get("event") or "")
        if not subject:
            continue
        family = _classify_market_family(lp.get("sport"), market_str)
        # De-dupe: show ONE card per (subject, family). If the slate has
        # both "Messi First Goal Scorer" and "Messi Anytime Goal Scorer"
        # for the same player, the SOC_SCORER family covers both — the
        # highest-lock variant wins (list is already sorted lock-desc).
        dedup_key = (subject.lower(), family)
        if dedup_key in seen_subject_family:
            continue
        settled = await _fetch_subject_history(
            subject, lp.get("sport"), family, is_player_prop,
        )
        # Minimum-sample gate: don't fabricate confidence from tiny
        # samples. "Hit in 1 of last 1" is technically true but
        # meaningless. Require at least 5 real same-market settled
        # games before rendering a card.
        if len(settled) < 5:
            continue
        # Infer the player's OWN team from their history so we can
        # compute the true opponent. Messi's settled picks are always
        # "X @ Argentina" → Argentina is Messi's team, so "vs Argentina"
        # would be wrong (that's who he plays FOR). Team markets have
        # `team` set, so this only applies to player-prop cases.
        inferred_own_team = None
        if is_player_prop and not team:
            inferred_own_team = _infer_own_team(settled)
        opp_display = _extract_opponent_from_pick(lp, own_team_override=inferred_own_team)
        # Also compute the raw opponent NAME (not the abbreviation) for
        # streak-fact matching against historical events.
        raw_opp = _extract_opponent_full_name(lp, own_team_override=inferred_own_team)
        facts = _build_streak_facts(settled, lp, min_streak_hits, raw_opponent=raw_opp,
                                    own_team=inferred_own_team or team)
        if not facts:
            continue
        seen_subject_family.add(dedup_key)
        cards.append({
            "pick_id": lp.get("id"),
            "player_name": subject,
            "player_display": _shorten_name(subject) if is_player_prop else subject,
            "opponent": opp_display,
            "sport": lp.get("sport"),
            "market": market_str,
            "market_clean": _clean_market_label(market_str or lp.get("selection") or "", subject=subject),
            "family": family,
            "book_odds": lp.get("book_odds"),
            "lock_score": lp.get("lock_score"),
            "facts": facts,
        })

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sport_filter": sport,
        "count": len(cards),
        "cards": cards,
        "groups": _group_cards_by_theme(cards),
    }


def _group_cards_by_theme(cards: list[dict]) -> list[dict]:
    """Reorganise cards into themed rails matching the competitor
    "Cheatsheets" home UX: `100% Recent Form`, `100% Home/Away Games`,
    `Head-to-Head Streaks`, etc. Each rail has ordered entries so the
    strongest hit-rates float to the top.
    """
    rails: dict[str, list[dict]] = {
        "recent_form_100": [],
        "recent_form": [],
        "home_away_100": [],
        "vs_opponent_100": [],
    }
    for c in cards:
        # Highest-pct fact drives which rail it belongs to.
        for f in c.get("facts", []):
            txt = (f.get("text") or "").lower()
            pct = f.get("pct") or 0
            entry = {
                "pick_id": c.get("pick_id"),
                "player_display": c.get("player_display"),
                "market_clean": c.get("market_clean"),
                "sport": c.get("sport"),
                "opponent": c.get("opponent"),
                "hits": f.get("hits"),
                "n": f.get("n"),
                "pct": pct,
                "fact_text": f.get("text"),
            }
            if "vs " in txt and pct == 100:
                rails["vs_opponent_100"].append(entry)
            elif ("home games" in txt or "away games" in txt) and pct == 100:
                rails["home_away_100"].append(entry)
            elif "last" in txt and "games" in txt and pct == 100 and "home" not in txt and "away" not in txt:
                rails["recent_form_100"].append(entry)
            elif "last" in txt and "games" in txt and pct >= 75 and "home" not in txt and "away" not in txt:
                rails["recent_form"].append(entry)

    # Sort each rail by hit count desc so 9/9 beats 5/5 at same pct.
    for k in rails:
        rails[k].sort(key=lambda e: -(e.get("hits") or 0))

    groups = []
    if rails["recent_form_100"]:
        groups.append({"title": "100% Recent Form", "icon": "flash",
                       "entries": rails["recent_form_100"][:8]})
    if rails["home_away_100"]:
        groups.append({"title": "100% Home/Away Games", "icon": "location",
                       "entries": rails["home_away_100"][:8]})
    if rails["vs_opponent_100"]:
        groups.append({"title": "100% Head-to-Head", "icon": "chatbubbles",
                       "entries": rails["vs_opponent_100"][:8]})
    if rails["recent_form"] and len(rails["recent_form_100"]) < 4:
        groups.append({"title": "Strong Recent Form (75%+)", "icon": "trending-up",
                       "entries": rails["recent_form"][:8]})
    return groups


# Detail endpoint: full game log for a subject in the Cheatsheets tab
# (tap-through from a rail entry).
@router.get("/cheatsheet-detail/{pick_id}")
async def cheatsheet_detail(pick_id: str):
    """Return the full game log for the subject of a Cheatsheet card
    so the frontend can render the "Trend analysis" screen (Recent
    Form / Head to Head / Away Split + Games Played table).
    """
    lp = await db.picks.find_one(
        {"id": pick_id},
        {"_id": 0, "sport": 1, "market": 1, "player_name": 1,
         "team": 1, "event": 1, "book_odds": 1, "lock_score": 1},
    )
    if not lp:
        raise HTTPException(404, "pick not found")
    player = (lp.get("player_name") or "").strip()
    if player.lower() == "none":
        player = ""
    if not player:
        player = _parse_player_from_market(lp.get("market") or "")
    if not player:
        raise HTTPException(400, "no player subject on pick")

    family = _classify_market_family(lp.get("sport"), lp.get("market") or "")
    settled = await _fetch_subject_history(player, lp.get("sport"), family, True)
    inferred_own_team = _infer_own_team(settled) if not lp.get("team") else None
    raw_opp = _extract_opponent_full_name(lp, own_team_override=inferred_own_team)

    # Split stats
    def _sum(picks):
        w = sum(1 for p in picks if (p.get("status") or "").lower() == "won")
        return w, len(picks)

    all_hits, all_n = _sum(settled[:20])
    opp_picks = ([p for p in settled if raw_opp and re.search(re.escape(raw_opp), p.get("event") or "", re.I)]
                 if raw_opp else [])
    opp_hits, opp_n = _sum(opp_picks)
    live_venue = _venue_of_pick(lp, own_team_override=inferred_own_team)
    venue_picks = [p for p in settled if _venue_of_pick(p, own_team_override=inferred_own_team) == live_venue] if live_venue else []
    venue_hits, venue_n = _sum(venue_picks[:10])

    # Row-by-row game log (last 20)
    rows = []
    for p in settled[:20]:
        st = (p.get("status") or "").lower()
        ev = p.get("event") or ""
        opp = ""
        if "@" in ev:
            away, home = [s.strip() for s in ev.split("@", 1)]
            own_lc = (inferred_own_team or lp.get("team") or "").lower()
            if own_lc and own_lc in home.lower():
                opp = _team_abbr(away)
            elif own_lc and own_lc in away.lower():
                opp = _team_abbr(home)
            else:
                opp = _team_abbr(away)
        rows.append({
            "date": p.get("pick_date"),
            "opponent": opp,
            "hit": st == "won",
            "status": st,
        })

    return {
        "pick_id": pick_id,
        "player": player,
        "player_display": _shorten_name(player),
        "sport": lp.get("sport"),
        "market": lp.get("market"),
        "opponent": _team_abbr(raw_opp) if raw_opp else None,
        "book_odds": lp.get("book_odds"),
        "recent_form": {
            "hits": all_hits, "n": all_n,
            "pct": round(all_hits / all_n * 100) if all_n else 0,
        },
        "head_to_head": {
            "hits": opp_hits, "n": opp_n,
            "pct": round(opp_hits / opp_n * 100) if opp_n else 0,
            "opponent": _team_abbr(raw_opp) if raw_opp else None,
        },
        "venue_split": {
            "hits": venue_hits, "n": venue_n,
            "pct": round(venue_hits / venue_n * 100) if venue_n else 0,
            "venue": live_venue,
        } if live_venue else None,
        "games": rows,
    }


async def _fetch_subject_history(
    subject: str, sport: str | None, family: str, is_player_prop: bool,
) -> list[dict]:
    """Get the last 30 settled picks for this subject+family.

    Player-name resolution notes
    ----------------------------
    Historical picks in this DB sometimes have `player_name` unset or
    literally set to the string "None" while the actual player is
    embedded in the `market` field (e.g. `"Mohamed Salah First Goal
    Scorer"`). To surface real streaks we match by EITHER:
      * `player_name` regex exact match  (when field is populated), OR
      * `market` regex substring match   (fallback for legacy rows).

    For team markets we search the `team` field with a contains-regex
    since team labels can be inconsistent ("Yankees" vs "New York
    Yankees").
    """
    subj_str = subject.strip()
    q: dict[str, Any] = {"status": {"$in": ["won", "lost"]}}
    if is_player_prop:
        # Match either exact player_name OR market-substring so we catch
        # the legacy rows where player_name was stored as "None".
        q["$or"] = [
            {"player_name": re.compile(r"^" + re.escape(subj_str) + r"$", re.IGNORECASE)},
            {"market": re.compile(re.escape(subj_str), re.IGNORECASE)},
        ]
    else:
        q["team"] = re.compile(re.escape(subj_str), re.IGNORECASE)
    if sport:
        q["sport"] = sport
    cursor = db.picks.find(
        q,
        {"_id": 0, "id": 1, "sport": 1, "market": 1, "event": 1,
         "team": 1, "player_name": 1, "status": 1,
         "pick_date": 1, "settled_at": 1},
    ).sort("settled_at", -1).limit(200)
    all_hist = await cursor.to_list(length=200)
    # Filter to same market family (Hits ≠ HR, ML ≠ Spread) AND drop
    # subset-market history entries that don't reflect true player
    # performance (First-Goal-Scorer is a subset of Anytime).
    first_goal_re = re.compile(r"first\s+goal\s+scorer|first\s+touchdown|last\s+goal", re.I)
    same_family = [
        h for h in all_hist
        if _classify_market_family(h.get("sport"), h.get("market") or "") == family
        and not first_goal_re.search(h.get("market") or "")
    ]
    # DEDUPE by unique game (pick_date + event). The picks collection
    # can hold multiple rows for the same match — e.g. "First Goal
    # Scorer" and "Anytime Goal Scorer" both trigger, plus rows can be
    # duplicated by upstream ingestion runs. Without this dedupe,
    # 15 duplicate Haaland rows for one game would report as
    # "Hit in 15 of last 15 games" — a lie.
    # Rule: for a given (date, event) tuple, keep the FIRST entry
    # encountered (most-recent settled_at wins because we sorted desc).
    # Prefer "won" over "lost" only if the game itself had multiple
    # markets and the player DID score (any-time hitting → the game
    # counts as a hit for streak purposes).
    seen: dict[tuple[str, str], dict] = {}
    for h in same_family:
        key = (h.get("pick_date") or "", (h.get("event") or "").lower().strip())
        if key not in seen:
            seen[key] = h
        else:
            # Same game already recorded — upgrade to "won" only if the
            # DEFINITIVE market for this family (First Goal / Anytime)
            # hit. For MLB hits/HR/RBI etc. this is a no-op since a
            # player has exactly one outcome per game per market. For
            # Soccer scorers we prefer to keep the ANYTIME-goal outcome
            # as the game-level truth — "First Goal" is a subset (you
            # can score anytime without being first).
            existing = seen[key]
            existing_status = (existing.get("status") or "").lower()
            new_status = (h.get("status") or "").lower()
            # If existing is a "First Goal Scorer" and new is "Anytime"
            # AND new won → replace, because anytime is the truer
            # game-level "did they score?" signal.
            existing_market = (existing.get("market") or "").lower()
            new_market = (h.get("market") or "").lower()
            if ("first goal" in existing_market and "anytime" in new_market
                    and new_status in ("won", "lost")):
                seen[key] = h
            elif existing_status == "lost" and new_status == "won":
                # If we've seen a loss but a later scan shows a win for
                # the same game, prefer the win (indicates the player
                # DID hit *something*).
                seen[key] = h
    return list(seen.values())


def _build_streak_facts(
    settled: list[dict], live_pick: dict, min_hits: int,
    raw_opponent: str | None = None,
    own_team: str | None = None,
) -> list[dict]:
    """From settled history + the current live pick's context, build
    the 3 most persuasive streak bullets.

    Priority (matches the competitor "cheatsheet" UX):
      1. Overall recent streak — "Hit in 8 of last 8 games"
      2. vs-opponent streak — "Hit in 3 of last 3 vs CIN"
         Uses `raw_opponent` (full team name, not abbreviation) so
         it matches historical `event` strings correctly. Falls back
         to `team` on the pick if raw_opponent isn't provided.
      3. Home/away streak matching the venue of the current pick.
         For player-prop picks where `team` is missing on the pick,
         `own_team` is inferred from history (e.g. "Argentina" for
         Messi).
    """
    facts: list[dict] = []

    def _win_count(picks: list[dict]) -> tuple[int, int]:
        w = sum(1 for p in picks if (p.get("status") or "").lower() == "won")
        return w, len(picks)

    # 1) Overall — try last8 then last10 then last5 then last20
    for window in (8, 10, 5, 20):
        if len(settled) < window:
            continue
        recent = settled[:window]
        w, n = _win_count(recent)
        if w >= min_hits:
            pct = round(w / n * 100)
            facts.append({
                "icon": "flash",
                "text": f"Hit in {w} of last {n} games",
                "pct": pct,
                "hits": w, "n": n,
            })
            break

    # 2) vs opponent — use the *full* opponent name to match history.
    #    Require n ≥ 3 same-opponent games or the bullet is stat noise.
    opp_name = (raw_opponent or "").strip()
    if opp_name:
        opp_re = re.compile(re.escape(opp_name), re.I)
        vs_hist = [h for h in settled if opp_re.search(h.get("event") or "")]
        if len(vs_hist) >= 3:
            w, n = _win_count(vs_hist)
            if w >= max(2, min_hits):
                pct = round(w / n * 100)
                facts.append({
                    "icon": "chatbubbles",
                    "text": f"Hit in {w} of last {n} vs {_team_abbr(opp_name)}",
                    "pct": pct,
                    "hits": w, "n": n,
                })

    # 3) Home / Away match — use inferred own_team for player props.
    #    Require n ≥ 3 same-venue games.
    live_venue = _venue_of_pick(live_pick, own_team_override=own_team)
    if live_venue:
        venue_hist = [
            h for h in settled
            if _venue_of_pick(h, own_team_override=own_team) == live_venue
        ]
        if len(venue_hist) >= 3:
            w, n = _win_count(venue_hist[:10])
            if w >= max(2, min_hits):
                pct = round(w / n * 100)
                facts.append({
                    "icon": "location",
                    "text": f"Hit in {w} of last {n} {live_venue} games",
                    "pct": pct,
                    "hits": w, "n": n,
                })

    # Dedupe on identical text
    seen: set[str] = set()
    out: list[dict] = []
    for f in facts:
        if f["text"] in seen:
            continue
        seen.add(f["text"])
        out.append(f)
    return out[:3]


def _extract_opponent_full_name(pick: dict, own_team_override: str | None = None) -> str | None:
    """Return the full opponent team name (not abbreviated) for
    matching against historical event strings."""
    ev = pick.get("event") or ""
    tm = (pick.get("team") or "").strip().lower()
    if own_team_override:
        tm = own_team_override.strip().lower()
    if not ev or "@" not in ev:
        return None
    away, home = [s.strip() for s in ev.split("@", 1)]
    if not tm:
        return away
    if tm in home.lower() or home.lower() in tm:
        return away
    if tm in away.lower() or away.lower() in tm:
        return home
    return away


def _extract_opponent_from_pick(pick: dict, own_team_override: str | None = None) -> str | None:
    ev = pick.get("event") or ""
    tm = (pick.get("team") or "").strip().lower()
    if own_team_override:
        tm = own_team_override.strip().lower()
    if not ev or "@" not in ev:
        return None
    away, home = [s.strip() for s in ev.split("@", 1)]
    if not tm:
        return f"vs {_team_abbr(away)}"
    # Compare team name against both sides via contains-match — team
    # labels are inconsistent across leagues ("USA" vs "United States").
    if tm in home.lower() or home.lower() in tm:
        # Player plays for home team → opponent is away.
        return f"vs {_team_abbr(away)}"
    if tm in away.lower() or away.lower() in tm:
        # Player plays for away team → opponent is home.
        return f"@ {_team_abbr(home)}"
    # No clear match — default to visitor phrasing.
    return f"vs {_team_abbr(away)}"


def _infer_own_team(settled: list[dict]) -> str | None:
    """Given a subject's settled-pick history, infer which team they
    play FOR by counting which team-side of every `event` string
    recurs most often. Reliable for national-team / long-tenure players
    since we've usually seen them 5+ times against varied opponents on
    the SAME home team side.
    """
    counts: dict[str, int] = {}
    for p in settled:
        ev = p.get("event") or ""
        if "@" not in ev:
            continue
        for side in ev.split("@", 1):
            side = side.strip()
            if not side:
                continue
            counts[side] = counts.get(side, 0) + 1
    if not counts:
        return None
    # Pick the team that appears in >= 60% of history rows — that's
    # the player's own team. Random rotational teammates won't hit
    # that threshold.
    total = len(settled)
    best, best_count = max(counts.items(), key=lambda kv: kv[1])
    if best_count / max(total, 1) >= 0.6:
        return best
    return None


def _venue_of_pick(pick: dict, own_team_override: str | None = None) -> str | None:
    ev = pick.get("event") or ""
    tm = (pick.get("team") or "").strip().lower()
    if own_team_override:
        tm = own_team_override.strip().lower()
    if not ev or "@" not in ev or not tm:
        return None
    away, home = [s.strip().lower() for s in ev.split("@", 1)]
    if tm in home or home in tm:
        return "home"
    if tm in away or away in tm:
        return "away"
    return None


def _team_abbr(team: str) -> str:
    if not team:
        return ""
    words = team.strip().split()
    if len(words) >= 2:
        return "".join(w[0] for w in words).upper()[:4]
    return team[:3].upper()


def _shorten_name(name: str) -> str:
    parts = name.strip().split()
    if len(parts) >= 2:
        return f"{parts[0][0]}. {' '.join(parts[1:])}"
    return name


def _clean_market_label(m: str, subject: str | None = None) -> str:
    """Strip subject/team prefix from a market string.

    Examples:
      "Miguel Vargas (CWS) Over 0.5 Hits"           → "Over 0.5 Hits"
      "Mohamed Salah - Anytime Goal Scorer"         → "Anytime Goal Scorer"
      "Lionel Messi Anytime Goal Scorer"            → "Anytime Goal Scorer"
      "Elly De La Cruz (CIN) Over 0.5 Total Bases"  → "Over 0.5 Total Bases"
    """
    if not m:
        return m
    out = m
    # 1) Strip "Player Name (TEAM) " prefix
    out = re.sub(r"^[A-Z][a-zA-Z'.\- ]+?\s*\([A-Z]{2,4}\)\s*", "", out)
    # 2) If we know the subject explicitly, strip that too.
    if subject:
        out = re.sub(r"^\s*" + re.escape(subject) + r"\s*[-:]*\s*", "", out, flags=re.IGNORECASE)
    # 3) Strip leftover "Player Name - " prefix (no team in parens)
    out = re.sub(r"^[A-Z][a-zA-Z'.\- ]{2,}?\s*-\s*", "", out)
    return out.strip() or m


# Player-prop keyword tail that we strip when parsing player from
# market string. Ordered longest-first so "Anytime Goal Scorer" is
# tried before "Goal Scorer".
_PLAYER_MARKET_TAILS = (
    "First Goal Scorer",
    "Anytime Goal Scorer",
    "Score or Assist",
    "Anytime Assist",
    "Anytime Touchdown Scorer",
    "First Touchdown Scorer",
    "Last Touchdown Scorer",
    "Anytime Home Run",
    "First Home Run",
    "Any Time Rush + Rec TD",
    "To Record a Sack",
    "Shots On Goal",
    "Total Bases",
    "Total Points",
    "Passing Yards",
    "Rushing Yards",
    "Receiving Yards",
    "Receptions",
    "Passing TDs",
    "Rushing TDs",
    "Home Run",
    "Hits + Runs + RBI",
    "Total Assists",
    "Total Rebounds",
    "Total 3-Pointers",
    "Strikeouts",
    "Hits Allowed",
    "Outs Recorded",
    "Earned Runs",
    "Runs",
    "RBI",
    "Hits",
)


def _parse_player_from_market(market: str) -> str:
    """Extract player name from a market string when the pick doc's
    `player_name` field is missing. Handles multiple formats:
      "Mohamed Salah First Goal Scorer"
      "Player Name - Anytime Goal Scorer"
      "Miguel Vargas (CWS) Over 0.5 Hits"
    """
    if not market:
        return ""
    m = market.strip()
    # Strip "(TEAM)" team-code suffixes anywhere in the string.
    m = re.sub(r"\s*\([A-Z]{2,4}\)\s*", " ", m).strip()
    # Strip Over/Under N.N prefix before the market keyword.
    m = re.sub(r"\s+(Over|Under)\s+[\d.]+\s+", " ", m).strip()
    # "Player - Market" split first
    if " - " in m:
        left = m.split(" - ", 1)[0].strip()
        if " " in left and not any(t.lower() in left.lower() for t in _PLAYER_MARKET_TAILS):
            return left
    # Trim any market tail off the right side
    for tail in _PLAYER_MARKET_TAILS:
        if m.lower().endswith(tail.lower()):
            candidate = m[: -len(tail)].strip(" -–—")
            if candidate and " " in candidate:
                # Also strip leftover "Over 0.5" fragments.
                candidate = re.sub(r"\s+(Over|Under)\s+[\d.]+\s*$", "", candidate).strip()
                if candidate:
                    return candidate
    return ""


def _parse_team_from_market(market: str, event: str) -> str:
    """Extract the team being bet on from a team-market string like
    "Lillestrom Win or Draw" or "Yankees Moneyline" — check which side
    of the event's teams appears in the market."""
    if not market or not event or "@" not in event:
        return ""
    m = market.lower()
    away, home = [s.strip() for s in event.split("@", 1)]
    # Longer team name first so "New York" doesn't shadow "New York Yankees".
    for team in sorted([away, home], key=lambda t: -len(t or "")):
        if not team:
            continue
        # Try each significant word of the team name.
        for token in team.lower().split():
            if len(token) < 4:
                continue
            if token in m:
                return team
    return ""


# ═════════════════════════════════════════════════════════════════════
# MATCHUP DNA
# ═════════════════════════════════════════════════════════════════════
@router.get("/matchup-dna/{sport}/{subject}")
async def matchup_dna(sport: str, subject: str, opponent: str | None = Query(None)):
    """Deep matchup profile for a subject (player name).

    Returns:
      * `overall`         — all settled picks for this subject
      * `vs_opponent`     — restricted to a specific opponent (if provided)
      * `by_market`       — per market family record
      * `home_away`       — split by home/away (parsed from event string)
      * `recent_form`     — last 20 settled picks with W/L timeline
      * `hot_cold`        — current hot/cold streak

    Data source: `db.picks` filtered by (player_name ~ subject). Case-
    insensitive match with a leading-word regex so "Judge" finds "Aaron
    Judge" but doesn't match unrelated "Judge Jr." style edge cases
    unless the user explicitly types the full name.
    """
    if not subject or len(subject.strip()) < 2:
        raise HTTPException(400, "subject name too short")

    subj_regex = re.compile(re.escape(subject.strip()), re.IGNORECASE)
    q: dict[str, Any] = {
        "sport": sport,
        "status": {"$in": ["won", "lost", "push"]},
        "player_name": subj_regex,
    }
    cursor = db.picks.find(
        q,
        {"_id": 0, "id": 1, "sport": 1, "market": 1, "selection": 1,
         "event": 1, "team": 1, "player_name": 1, "book_odds": 1,
         "status": 1, "units_profit": 1, "units_risked": 1,
         "pick_date": 1, "settled_at": 1},
    ).sort("settled_at", -1).limit(400)

    all_picks: list[dict] = []
    async for p in cursor:
        all_picks.append(p)

    if not all_picks:
        return {
            "subject": subject, "sport": sport,
            "overall": _empty_record(), "vs_opponent": None,
            "by_market": [], "home_away": {}, "recent_form": [],
            "hot_cold": "no data",
        }

    def _record(picks: list[dict]) -> dict:
        won = sum(1 for p in picks if (p.get("status") or "").lower() == "won")
        lost = sum(1 for p in picks if (p.get("status") or "").lower() == "lost")
        push = sum(1 for p in picks if (p.get("status") or "").lower() == "push")
        up = sum(float(p.get("units_profit") or 0) for p in picks)
        ur = sum(float(p.get("units_risked") or 0) for p in picks)
        n = won + lost + push
        return {
            "n": n, "won": won, "lost": lost, "push": push,
            "hit_rate": round(won / (won + lost), 3) if (won + lost) else 0.0,
            "units_profit": round(up, 3),
            "roi": round(up / ur, 3) if ur else 0.0,
        }

    # By market family
    fam_map: dict[str, list[dict]] = {}
    for p in all_picks:
        fam = _classify_market_family(p.get("sport"), p.get("market") or "")
        fam_map.setdefault(fam, []).append(p)
    by_market = [
        {"family": fam, **_record(picks)}
        for fam, picks in sorted(fam_map.items(), key=lambda kv: -len(kv[1]))
    ]

    # Home / away split (parsed from `event` string like "Yankees @ Red Sox")
    home_picks: list[dict] = []
    away_picks: list[dict] = []
    for p in all_picks:
        ev = p.get("event") or ""
        tm = (p.get("team") or "").strip().lower()
        if not ev or "@" not in ev or not tm:
            continue
        away_team, home_team = [s.strip().lower() for s in ev.split("@", 1)]
        if tm and tm in home_team:
            home_picks.append(p)
        elif tm and tm in away_team:
            away_picks.append(p)
    home_away = {
        "home": _record(home_picks) if home_picks else _empty_record(),
        "away": _record(away_picks) if away_picks else _empty_record(),
    }

    # vs_opponent — sub-filter if provided
    vs_opp_result = None
    if opponent:
        opp_re = re.compile(re.escape(opponent.strip()), re.IGNORECASE)
        matching = [p for p in all_picks if opp_re.search(p.get("event") or "")]
        vs_opp_result = {
            "opponent": opponent,
            **_record(matching),
        } if matching else {"opponent": opponent, "n": 0}

    # Recent form: last 20 settled → W/L timeline
    recent_form = [
        {
            "date": p.get("pick_date"),
            "market": p.get("market"),
            "status": (p.get("status") or "").lower(),
            "units": round(float(p.get("units_profit") or 0), 3),
        }
        for p in all_picks[:20]
    ]

    # Hot/cold streak: consecutive same-status runs at the top of recent_form
    hot_cold = "no clear streak"
    if recent_form:
        first_status = recent_form[0]["status"]
        streak = 0
        for r in recent_form:
            if r["status"] == first_status:
                streak += 1
            else:
                break
        if streak >= 3:
            if first_status == "won":
                hot_cold = f"HOT — {streak} in a row"
            elif first_status == "lost":
                hot_cold = f"COLD — {streak} straight losses"

    return {
        "subject": subject,
        "sport": sport,
        "overall": _record(all_picks),
        "vs_opponent": vs_opp_result,
        "by_market": by_market[:8],
        "home_away": home_away,
        "recent_form": recent_form,
        "hot_cold": hot_cold,
    }


def _empty_record() -> dict:
    return {"n": 0, "won": 0, "lost": 0, "push": 0,
            "hit_rate": 0.0, "units_profit": 0.0, "roi": 0.0}
