"""PvT-aware backtesting engine for MLB Strikeout picks.

USER MANDATE 2026-07-27: "Backtesting engine — historical replay with
the new PvT math baked in." Following the Wheeler bug fix, this
answers "How many bad K picks would the new PvT math have caught if
it had been live?" by replaying settled picks through the new gate.

Approach:
  1. Pull all settled MLB K picks in the last N days
  2. For each pick, look up PvT (career + recent K's vs opposing team)
  3. Compute what the new math would say:
       • PASS → pick stays, original result stands
       • REJECT (edge too small / model prob too low) → pick would not
         have been made → counted as 0-unit no-bet
       • FLIP (model says other side wins) → pick would have been on
         the OTHER side → result is INVERTED
  4. Compare original ROI/hit-rate vs new-math-simulated ROI/hit-rate
  5. Return a diff report + samples of the biggest changes

This is a READ-ONLY replay. No picks are modified.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger("lockscore.pvt_backtest")

# Reuse the same math constants as the live gate
LEAGUE_AVG_K_PER_GS = 5.5
LEAGUE_AVG_K_PER_9 = 8.5
LEAGUE_AVG_IP_PER_START = 5.4

MARKET_PATTERN = re.compile(
    r"(?P<pitcher>.+?)\s+\(\w+\)\s+(?P<side>Over|Under)\s+(?P<line>[\d.]+)\s+Strikeouts",
    re.IGNORECASE,
)


def _parse_market(market: str) -> Optional[dict]:
    m = MARKET_PATTERN.match(market or "")
    if not m:
        return None
    return {
        "pitcher": m.group("pitcher").strip(),
        "side":    m.group("side").capitalize(),
        "line":    float(m.group("line")),
    }


def _implied_from_american(odds: int | float | None) -> float:
    if odds is None:
        return 0.5
    try:
        odds = int(odds)
    except (TypeError, ValueError):
        return 0.5
    if odds < 0:
        return (-odds) / ((-odds) + 100.0)
    return 100.0 / (odds + 100.0)


def _american_to_dec(odds: int | float | None) -> float:
    """American → decimal for profit-per-unit calc."""
    if odds is None:
        return 1.0
    try:
        odds = int(odds)
    except (TypeError, ValueError):
        return 1.0
    if odds < 0:
        return 100.0 / (-odds) + 1.0
    return odds / 100.0 + 1.0


def _split_event_teams(event: str) -> list[str]:
    parts = re.split(r"\s+@\s+|\s+vs\.?\s+", event or "")
    return [p.strip() for p in parts if p.strip()]


def _classify_via_pvt(
    pvt: dict | None,
    line: float,
    side: str,
) -> dict:
    """Given PvT dict, line and side, decide: keep / reject / flip.

    Returns {"decision": "keep"|"reject"|"flip", "expected_k": float,
             "reason": str}.
    """
    if not pvt or pvt.get("significance") == "low":
        return {"decision": "keep", "expected_k": None, "reason": "no_pvt_signal"}

    recent = pvt.get("recent_avg_k") or 0
    career = pvt.get("k_per_gs_vs_team") or 0
    # Same blend the live math uses (50/30/20 recent/career/league)
    exp_k = 0.5 * recent + 0.3 * career + 0.2 * LEAGUE_AVG_K_PER_GS

    # Wrong-side detection: line and expected K conflict by >= 0.5
    if side == "Under" and exp_k > line + 0.5:
        return {"decision": "flip", "expected_k": exp_k, "reason": "expected_over_but_pick_under"}
    if side == "Over" and exp_k < line - 0.5:
        return {"decision": "flip", "expected_k": exp_k, "reason": "expected_under_but_pick_over"}

    # Weak edge: expected K within 0.3 of the line means model is
    # basically 50/50 → not enough conviction to bet
    if abs(exp_k - line) < 0.3:
        return {"decision": "reject", "expected_k": exp_k, "reason": "expected_k_too_close_to_line"}

    return {"decision": "keep", "expected_k": exp_k, "reason": "aligned_with_pick"}


def _simulated_result(orig_status: str, decision: str) -> tuple[str, float]:
    """Return (new_status, units_profit) given original status + decision."""
    if decision == "reject":
        return ("no_bet", 0.0)
    if decision == "keep":
        return (orig_status, None)  # profit unchanged from original
    if decision == "flip":
        # Invert: won → lost, lost → won, push → push
        if orig_status == "won":
            return ("lost", None)
        if orig_status == "lost":
            return ("won", None)
        return ("push", 0.0)
    return (orig_status, None)


async def backtest_mlb_k_with_pvt(db, *, days: int = 30) -> dict:
    """Replay settled MLB K picks with the new PvT math applied.

    Returns a rich diff report — see docstring at top of module.

    2026-07-27: added asyncio.gather parallelism (batch=10) so a 30-day
    window resolves in ~15s instead of timing out at 60s. Each pick
    requires 3-5 MLB Stats API calls (player-id + 2 team PvT lookups).
    """
    import asyncio
    from services.mlb_pvt import get_pvt_for_pitcher_vs_team
    from mlb_bvp import lookup_player_id

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_iso = cutoff.isoformat()

    q = {
        "sport": "MLB",
        "market": {"$regex": "Strikeout", "$options": "i"},
        "status": {"$in": ["won", "lost", "push"]},
        "$or": [
            {"settled_at": {"$gte": cutoff_iso}},
            {"event_time": {"$gte": cutoff_iso}},
        ],
    }

    picks = await db.picks.find(q).to_list(length=5000)
    logger.info("pvt_backtest scanning %d settled MLB K picks", len(picks))

    async def _resolve_pvt_for_pick(p: dict) -> tuple[dict, dict | None, str | None]:
        parsed = _parse_market(p.get("market") or "")
        if not parsed:
            return (p, None, None)
        pid = await lookup_player_id(parsed["pitcher"])
        if not pid:
            return (p, None, None)
        teams = _split_event_teams(p.get("event") or "")
        best_pvt = None
        best_opp = None
        # Concurrent PvT lookups for both possible opponents
        pvts = await asyncio.gather(
            *[get_pvt_for_pitcher_vs_team(pid, opp) for opp in teams],
            return_exceptions=True,
        )
        for opp, pvt in zip(teams, pvts):
            if isinstance(pvt, Exception) or not pvt:
                continue
            if not best_pvt or pvt.get("gs_vs_team", 0) > best_pvt.get("gs_vs_team", 0):
                best_pvt = pvt
                best_opp = opp
        return (p, best_pvt, best_opp)

    # Chunk parallel resolution to keep peak concurrent connections reasonable
    CHUNK = 12
    resolved: list[tuple[dict, dict | None, str | None]] = []
    for i in range(0, len(picks), CHUNK):
        chunk = picks[i:i + CHUNK]
        results = await asyncio.gather(*[_resolve_pvt_for_pick(p) for p in chunk])
        resolved.extend(results)

    n_total = len(picks)
    n_keep = n_reject = n_flip = n_no_pvt = 0
    orig_won = orig_lost = 0
    new_won = new_lost = new_no_bet = 0
    orig_risked = orig_profit = 0.0
    new_risked = new_profit = 0.0

    flip_samples: list[dict] = []
    reject_samples: list[dict] = []

    for p, best_pvt, best_opp in resolved:
        parsed = _parse_market(p.get("market") or "")
        if not parsed:
            n_keep += 1
            continue
        side = parsed["side"]
        line = parsed["line"]

        if not best_pvt:
            n_no_pvt += 1
            n_keep += 1
            continue

        clas = _classify_via_pvt(best_pvt, line, side)

        orig_status = p.get("status")
        book_odds = p.get("book_odds")
        risked = float(p.get("units_risked") or 1.0)
        profit_orig = float(p.get("units_profit") or 0.0)

        orig_risked += risked
        orig_profit += profit_orig
        if orig_status == "won":
            orig_won += 1
        elif orig_status == "lost":
            orig_lost += 1

        decision = clas["decision"]
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
                "market":         p.get("market"),
                "event":          p.get("event"),
                "opp_team":       best_opp,
                "expected_k":     round(clas.get("expected_k") or 0, 2),
                "line":           line,
                "orig_status":    orig_status,
                "orig_profit":    round(profit_orig, 2),
                "career_k_vs_team": best_pvt.get("k_per_gs_vs_team"),
                "recent_k_vs_team": best_pvt.get("recent_k_vs_team"),
            })
        elif decision == "flip":
            n_flip += 1
            new_risked += risked
            dec = _american_to_dec(book_odds)
            if orig_status == "won":
                new_lost += 1
                new_profit -= risked
                flip_samples.append({
                    "market": p.get("market"), "event": p.get("event"),
                    "opp_team": best_opp, "expected_k": round(clas.get("expected_k") or 0, 2),
                    "line": line, "orig_status": "won",
                    "profit_delta": round(-risked - profit_orig, 2),
                    "career_k_vs_team": best_pvt.get("k_per_gs_vs_team"),
                    "recent_k_vs_team": best_pvt.get("recent_k_vs_team"),
                })
            elif orig_status == "lost":
                new_won += 1
                new_profit += risked * (dec - 1.0)
                flip_samples.append({
                    "market": p.get("market"), "event": p.get("event"),
                    "opp_team": best_opp, "expected_k": round(clas.get("expected_k") or 0, 2),
                    "line": line, "orig_status": "lost",
                    "profit_delta": round(risked * (dec - 1.0) + risked, 2),
                    "career_k_vs_team": best_pvt.get("k_per_gs_vs_team"),
                    "recent_k_vs_team": best_pvt.get("recent_k_vs_team"),
                })

    # Sort samples by absolute profit delta
    flip_samples.sort(key=lambda x: -abs(x.get("profit_delta", 0)))
    reject_samples.sort(key=lambda x: x.get("orig_profit", 0))

    orig_settled = orig_won + orig_lost
    new_settled = new_won + new_lost
    return {
        "window_days":       days,
        "n_total_scanned":   n_total,
        "n_kept":            n_keep,
        "n_rejected":        n_reject,
        "n_flipped":         n_flip,
        "n_no_pvt_signal":   n_no_pvt,
        "original": {
            "won":            orig_won,
            "lost":           orig_lost,
            "hit_rate_pct":   round(orig_won / orig_settled * 100.0, 1) if orig_settled else 0.0,
            "units_risked":   round(orig_risked, 2),
            "units_profit":   round(orig_profit, 2),
            "roi_pct":        round(orig_profit / orig_risked * 100.0, 2) if orig_risked else 0.0,
        },
        "with_pvt_math": {
            "won":            new_won,
            "lost":           new_lost,
            "no_bet":         new_no_bet,
            "hit_rate_pct":   round(new_won / new_settled * 100.0, 1) if new_settled else 0.0,
            "units_risked":   round(new_risked, 2),
            "units_profit":   round(new_profit, 2),
            "roi_pct":        round(new_profit / new_risked * 100.0, 2) if new_risked else 0.0,
        },
        "delta": {
            "profit_units":   round(new_profit - orig_profit, 2),
            "roi_pp":         round(
                (new_profit / new_risked * 100.0 if new_risked else 0.0) -
                (orig_profit / orig_risked * 100.0 if orig_risked else 0.0), 2,
            ),
            "hit_rate_pp":    round(
                (new_won / new_settled * 100.0 if new_settled else 0.0) -
                (orig_won / orig_settled * 100.0 if orig_settled else 0.0), 1,
            ),
        },
        "top_flips":         flip_samples[:15],
        "top_rejects":       reject_samples[:15],
        "note":              "Simulated replay only. No picks were modified.",
    }


__all__ = ["backtest_mlb_k_with_pvt"]
