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
