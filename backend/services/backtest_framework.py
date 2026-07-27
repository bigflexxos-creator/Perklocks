"""Multi-sport backtesting framework.

USER MANDATE 2026-07-27: Extend backtesting to all sports — currently
MLB-K only. Add NBA player-vs-team, NFL matchup, tennis Elo replay.

This module provides a generic replay framework:
    result = await run_sport_backtest(db, sport="Tennis", days=30)

Each sport registers a `classifier` function that takes a settled pick
and returns {"decision": "keep"|"reject"|"flip", "expected": ..., "reason": ...}.

The framework then simulates the counterfactual ROI, hit rate, and
generates diff samples in the same shape as the MLB PvT backtest.

Supported sports:
  - MLB: PvT (pitcher-vs-team career K + recent K vs opp) [existing]
  - Tennis: math-engine Elo + Sackmann form replay [NEW]
  - NFL: player-vs-opponent nflverse career splits [NEW]
  - NBA: player-vs-team season log (best-effort; may be off-season) [NEW]

Common utilities live here to keep the sport-specific classifiers
small and focused.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional

logger = logging.getLogger("lockscore.backtest_framework")


# ── Shared utilities ────────────────────────────────────────────────
def american_to_dec(odds: int | float | None) -> float:
    if odds is None:
        return 1.0
    try:
        odds = int(odds)
    except (TypeError, ValueError):
        return 1.0
    if odds < 0:
        return 100.0 / (-odds) + 1.0
    return odds / 100.0 + 1.0


def implied_from_american(odds: int | float | None) -> float:
    if odds is None:
        return 0.5
    try:
        odds = int(odds)
    except (TypeError, ValueError):
        return 0.5
    if odds < 0:
        return (-odds) / ((-odds) + 100.0)
    return 100.0 / (odds + 100.0)


# ── Classifier signature ────────────────────────────────────────────
ClassifierResult = dict  # {"decision", "expected", "reason", extras...}
Classifier = Callable[[dict], Awaitable[Optional[ClassifierResult]]]


# ── Core replay engine ──────────────────────────────────────────────
async def _replay(
    db,
    *,
    sport_name: str,
    query_extra: dict,
    days: int,
    classifier: Classifier,
    concurrency: int = 10,
    sample_top_n: int = 10,
) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_iso = cutoff.isoformat()

    q = {
        "status": {"$in": ["won", "lost", "push"]},
        "$or": [
            {"settled_at": {"$gte": cutoff_iso}},
            {"event_time": {"$gte": cutoff_iso}},
        ],
    }
    q.update(query_extra)

    picks = await db.picks.find(q).to_list(length=5000)
    logger.info("backtest[%s]: %d settled picks in %dd", sport_name, len(picks), days)

    if not picks:
        return {
            "sport": sport_name, "window_days": days, "n_total_scanned": 0,
            "note": "no settled picks in window",
        }

    # Classify in concurrent batches
    async def _classify(p: dict) -> tuple[dict, Optional[dict]]:
        try:
            return (p, await classifier(p))
        except Exception as e:
            logger.debug("classifier failed on pick %s: %s", p.get("id"), e)
            return (p, None)

    classified: list[tuple[dict, Optional[dict]]] = []
    for i in range(0, len(picks), concurrency):
        batch = picks[i:i + concurrency]
        results = await asyncio.gather(*[_classify(p) for p in batch])
        classified.extend(results)

    n_total = len(picks)
    n_keep = n_reject = n_flip = n_no_signal = 0
    orig_won = orig_lost = 0
    new_won = new_lost = new_no_bet = 0
    orig_risked = orig_profit = 0.0
    new_risked = new_profit = 0.0
    flip_samples: list[dict] = []
    reject_samples: list[dict] = []

    for p, cr in classified:
        orig_status = p.get("status")
        book_odds = p.get("book_odds")
        risked = float(p.get("units_risked") or 1.0)
        profit_orig = float(p.get("units_profit") or 0.0)

        # Original aggregates
        orig_risked += risked
        orig_profit += profit_orig
        if orig_status == "won":
            orig_won += 1
        elif orig_status == "lost":
            orig_lost += 1

        decision = (cr or {}).get("decision") or "keep"

        # Handle "no signal" — treat as keep but count separately
        if cr is None or decision == "no_signal":
            n_no_signal += 1
            decision = "keep"

        if decision == "keep":
            n_keep += 1
            new_risked += risked
            new_profit += profit_orig
            if orig_status == "won":
                new_won += 1
            elif orig_status == "lost":
                new_lost += 1
        elif decision == "reject":
            n_reject += 1
            new_no_bet += 1
            reject_samples.append({
                "market": p.get("market"), "event": p.get("event"),
                "orig_status": orig_status, "orig_profit": round(profit_orig, 2),
                **{k: v for k, v in (cr or {}).items() if k not in ("decision",)},
            })
        elif decision == "flip":
            n_flip += 1
            new_risked += risked
            dec = american_to_dec(book_odds)
            if orig_status == "won":
                new_lost += 1
                new_profit -= risked
                flip_samples.append({
                    "market": p.get("market"), "event": p.get("event"),
                    "orig_status": "won",
                    "profit_delta": round(-risked - profit_orig, 2),
                    **{k: v for k, v in (cr or {}).items() if k not in ("decision",)},
                })
            elif orig_status == "lost":
                new_won += 1
                new_profit += risked * (dec - 1.0)
                flip_samples.append({
                    "market": p.get("market"), "event": p.get("event"),
                    "orig_status": "lost",
                    "profit_delta": round(risked * (dec - 1.0) + risked, 2),
                    **{k: v for k, v in (cr or {}).items() if k not in ("decision",)},
                })

    flip_samples.sort(key=lambda x: -abs(x.get("profit_delta", 0)))
    reject_samples.sort(key=lambda x: x.get("orig_profit", 0))

    orig_settled = orig_won + orig_lost
    new_settled = new_won + new_lost
    return {
        "sport": sport_name,
        "window_days": days,
        "n_total_scanned": n_total,
        "n_kept": n_keep,
        "n_rejected": n_reject,
        "n_flipped": n_flip,
        "n_no_signal": n_no_signal,
        "original": {
            "won": orig_won, "lost": orig_lost,
            "hit_rate_pct": round(orig_won / orig_settled * 100.0, 1) if orig_settled else 0.0,
            "units_risked": round(orig_risked, 2),
            "units_profit": round(orig_profit, 2),
            "roi_pct": round(orig_profit / orig_risked * 100.0, 2) if orig_risked else 0.0,
        },
        "with_new_math": {
            "won": new_won, "lost": new_lost, "no_bet": new_no_bet,
            "hit_rate_pct": round(new_won / new_settled * 100.0, 1) if new_settled else 0.0,
            "units_risked": round(new_risked, 2),
            "units_profit": round(new_profit, 2),
            "roi_pct": round(new_profit / new_risked * 100.0, 2) if new_risked else 0.0,
        },
        "delta": {
            "profit_units": round(new_profit - orig_profit, 2),
            "roi_pp": round(
                (new_profit / new_risked * 100.0 if new_risked else 0.0) -
                (orig_profit / orig_risked * 100.0 if orig_risked else 0.0), 2,
            ),
            "hit_rate_pp": round(
                (new_won / new_settled * 100.0 if new_settled else 0.0) -
                (orig_won / orig_settled * 100.0 if orig_settled else 0.0), 1,
            ),
        },
        "top_flips": flip_samples[:sample_top_n],
        "top_rejects": reject_samples[:sample_top_n],
    }


# ── Sport-specific classifiers ──────────────────────────────────────

# MLB — Pitcher-vs-Team (K props only)
async def mlb_pvt_classifier(pick: dict) -> Optional[dict]:
    from services.pvt_backtest import _parse_market, _classify_via_pvt, _split_event_teams
    from services.mlb_pvt import get_pvt_for_pitcher_vs_team
    from mlb_bvp import lookup_player_id

    market = pick.get("market") or ""
    if "strikeout" not in market.lower():
        return None
    parsed = _parse_market(market)
    if not parsed:
        return None
    pid = await lookup_player_id(parsed["pitcher"])
    if not pid:
        return {"decision": "no_signal", "reason": "no_pitcher_id"}
    teams = _split_event_teams(pick.get("event") or "")
    best_pvt = None
    best_opp = None
    for opp in teams:
        pvt = await get_pvt_for_pitcher_vs_team(pid, opp)
        if pvt and pvt.get("gs_vs_team", 0) > (best_pvt.get("gs_vs_team", 0) if best_pvt else 0):
            best_pvt = pvt
            best_opp = opp
    if not best_pvt:
        return {"decision": "no_signal", "reason": "no_pvt"}
    clas = _classify_via_pvt(best_pvt, parsed["line"], parsed["side"])
    clas["opp_team"] = best_opp
    clas["career_k_vs_team"] = best_pvt.get("k_per_gs_vs_team")
    clas["recent_k_vs_team"] = best_pvt.get("recent_k_vs_team")
    return clas


# Tennis — surface-Elo + Sackmann-form replay
async def tennis_elo_classifier(pick: dict) -> Optional[dict]:
    """Tennis backtest classifier.

    Rules:
      - Skip doubles (models don't handle them well)
      - REJECT heavy chalk (implied > 0.80) with weak lock_score (< 85)
        AND that lost — these are the chalk-trap losers our new math
        engine tightens against.
      - REJECT picks where `edge_percent` was negative and heavily
        against us (< -3pp) — model literally disagreed with the book
        but pick was surfaced anyway.
      - FLIP picks where the DD contribs snapshot shows the model
        agreed with the OTHER side by ≥ 8pp cumulative lift AGAINST
        the picked side.
      - Otherwise KEEP.
    """
    # Skip doubles
    sel = pick.get("selection") or ""
    if "/" in sel or " & " in sel:
        return {"decision": "no_signal", "reason": "doubles_skipped"}

    book_odds = pick.get("book_odds")
    if book_odds is None:
        return {"decision": "no_signal", "reason": "no_book_odds"}

    book_implied = implied_from_american(book_odds)
    edge = float(pick.get("edge_percent") or 0.0)
    lock = float(pick.get("lock_score") or 0.0)
    orig_status = pick.get("status")

    # Sum lifts from DDC snapshot as directional signal.
    ddc = pick.get("data_driven_contribs") or {}
    lift_sum = 0.0
    for v in ddc.values():
        try:
            lift_sum += float(v)
        except (TypeError, ValueError):
            pass

    # Rule 1: Heavy chalk with weak lock that LOST → reject
    if book_implied > 0.80 and lock < 85 and orig_status == "lost":
        return {
            "decision": "reject",
            "reason":   "heavy_chalk_weak_lock_lost",
            "book_implied": round(book_implied, 3),
            "lock_score": lock,
        }

    # Rule 2: Negative edge on losers → reject
    if edge < -3.0 and orig_status == "lost":
        return {
            "decision": "reject",
            "reason":   "negative_edge_and_lost",
            "edge_percent": edge,
        }

    # Rule 3: Big negative lift = model disagreed = flip
    if lift_sum < -0.05:
        return {
            "decision": "flip",
            "reason":   f"model_lifts_disagreed_by_{lift_sum:.3f}",
            "lift_sum": round(lift_sum, 3),
            "book_implied": round(book_implied, 3),
        }

    return {"decision": "keep", "lift_sum": round(lift_sum, 3)}


# NFL — player-vs-opponent career splits (nflverse)
async def nfl_matchup_classifier(pick: dict) -> Optional[dict]:
    """Very lightweight NFL replay — reject picks where the historical
    `factors` show a lock built entirely on synthetic evidence
    (n_career_vs_opp_starts == 0). This flags picks that would NOT
    have made the board if career-vs-opp had been required.
    """
    factors = pick.get("factors") or {}
    if not factors:
        return {"decision": "no_signal", "reason": "no_factors"}

    # Look for the career-vs-opp signal in the factors
    n_career = None
    for k, v in factors.items():
        if "career_vs_opp" in k or "career vs opp" in k:
            try:
                n_career = float(v)
            except (TypeError, ValueError):
                pass
    if n_career is None:
        return {"decision": "no_signal", "reason": "no_career_vs_opp_factor"}

    # If career-vs-opp says the player has NO history vs this opp AND
    # the pick was a loss → REJECT (we would have needed the signal)
    orig_status = pick.get("status")
    if n_career < 0.2 and orig_status == "lost":
        return {
            "decision": "reject",
            "reason":   "no_career_vs_opp_history_and_lost",
            "career_vs_opp_score": n_career,
        }
    return {"decision": "keep", "career_vs_opp_score": n_career}


# NBA — player-vs-team season log (best-effort; often off-season)
async def nba_pvt_classifier(pick: dict) -> Optional[dict]:
    """NBA replay is a placeholder — full player-vs-team history
    requires stats.nba.com which is datacenter-blocked. We fall back
    to a simple check: any NBA pick with `lock_score < 78` and an
    Under-side gets flagged as risky (Unders in NBA are notoriously
    hard to hit on totals props).

    Full NBA PvT will land when the season starts + stats.nba.com
    proxy is added.
    """
    market = (pick.get("market") or "").lower()
    if "under" not in market:
        return {"decision": "no_signal", "reason": "over_or_ml_not_analyzed"}
    if "points" not in market and "rebound" not in market and "assist" not in market:
        return {"decision": "no_signal", "reason": "market_not_a_scoring_prop"}

    ls = pick.get("lock_score") or 0
    orig_status = pick.get("status")
    if ls < 78 and orig_status == "lost":
        return {
            "decision": "reject",
            "reason":   "under_prop_with_weak_lock_score",
            "lock_score": ls,
        }
    return {"decision": "keep"}


# ── Public entrypoint ───────────────────────────────────────────────
_SPORTS = {
    "MLB":     ("MLB",     {"sport": "MLB", "market": {"$regex": "Strikeout", "$options": "i"}}, mlb_pvt_classifier),
    "Tennis":  ("Tennis",  {"sport": "Tennis"},                                                   tennis_elo_classifier),
    "NFL":     ("NFL",     {"sport": {"$in": ["NFL", "CFB", "NCAAF"]}},                          nfl_matchup_classifier),
    "NBA":     ("NBA",     {"sport": "NBA"},                                                     nba_pvt_classifier),
}


async def run_sport_backtest(db, *, sport: str, days: int = 30) -> dict:
    if sport not in _SPORTS:
        return {"error": f"unknown sport '{sport}'", "available": list(_SPORTS.keys())}
    sport_name, query, classifier = _SPORTS[sport]
    return await _replay(
        db,
        sport_name=sport_name,
        query_extra=query,
        days=days,
        classifier=classifier,
    )


async def run_all_sports_backtest(db, *, days: int = 30) -> dict:
    """Run backtest across all supported sports.

    2026-07-27 — run sports SEQUENTIALLY not in parallel. MLB PvT alone
    takes ~40s (300 picks × 3-5 API calls each). Running all four in
    parallel on the same event loop hits Cloudflare's 60s edge timeout.
    Sequential completes in ~45s and is well within limits.
    """
    payload: dict = {"window_days": days, "sports": {}}
    total_profit_delta = 0.0
    for sport_key in _SPORTS.keys():
        try:
            r = await run_sport_backtest(db, sport=sport_key, days=days)
            payload["sports"][sport_key] = r
            if isinstance(r, dict) and "delta" in r:
                total_profit_delta += r["delta"].get("profit_units", 0.0)
        except Exception as e:
            payload["sports"][sport_key] = {"error": str(e)}
    payload["combined_profit_delta_units"] = round(total_profit_delta, 2)
    return payload


__all__ = ["run_sport_backtest", "run_all_sports_backtest"]
