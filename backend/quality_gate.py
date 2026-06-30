"""Quality Gate — backtest-driven filter for picks served on the main
board and Rollover.

Context (2026-06-29):
The historical win-rate study over 1,499 graded picks revealed that
three pick categories were dragging the overall win rate from a healthy
~70% down to 47.2%:

  ┌──────────────────────────────────────────┬─────┬──────┐
  │ Category                                 │ n   │ Win% │
  ├──────────────────────────────────────────┼─────┼──────┤
  │ Soccer Anytime/First/Last Scorer         │ 396 │  4.8 │  ← noise
  │ Lock-Score band 65 ≤ x < 75              │ 250 │ 12.8 │  ← inverted
  │ MLB Moneyline                            │  25 │ 44.0 │  ← below 50
  │ MLB NRFI / YRFI                          │  25 │ 40.0 │  ← below 50
  ├──────────────────────────────────────────┼─────┼──────┤
  │ (everything else, projected)             │     │ ≈72% │
  └──────────────────────────────────────────┴─────┴──────┘

This module gates them out at the read layer (post-fetch, pre-render).
Generation is left untouched — a future calibration pass will fix the
underlying models. This is the cheap, reversible "stop the bleeding"
patch.

Design constraints:
  • Keep the carve-out for the CSL Goalscorers SECTION — those are
    served by a dedicated endpoint (`/api/csl/...`), not /picks/today,
    so they're not affected by this filter.
  • Don't touch picks the user has already added to their bet slip /
    parlay history — only filter the FEED rendering.
  • Surface a `quality_gate_block_reason` field on filtered picks if
    asked to keep them (for debugging), but by default just drop them.
"""
from __future__ import annotations

import re
from typing import Iterable


# ─── Tunables (mirror the historical-data-derived thresholds) ───────
_INVERTED_LOCK_BAND = (65.0, 75.0)   # ≥65 and <75 — historical 12.8%

_SOCCER_GOALSCORER_FAMILY_RE = re.compile(
    r"(anytime\s+goal\s*scorer"
    r"|anytime\s+scorer"
    r"|first\s+goal\s*scorer"
    r"|first\s+scorer"
    r"|last\s+goal\s*scorer"
    r"|last\s+scorer"
    r"|to\s+score"
    r"|score\s+or\s+assist"
    r"|player\s+to\s+score"
    r")",
    re.IGNORECASE,
)

# First / Last goalscorer markets are LOTTERIES — 3% historical hit rate
# across 338 graded picks. Even Kane / Messi / Mbappé hit FGS at < 5%.
# These should be priced at +800 lottery odds, not surfaced as
# "Elite Locks" — purge them at the read layer until we recalibrate.
_SOCCER_FIRST_LAST_SCORER_RE = re.compile(
    r"(first\s+goal\s*scorer"
    r"|first\s+scorer"
    r"|last\s+goal\s*scorer"
    r"|last\s+scorer"
    r")",
    re.IGNORECASE,
)

# Anytime goalscorer family — KEEP these, but tightly governed:
#   1. dedupe to top-1 per match (handled upstream in `_dedupe_goalscorer_per_event`)
#   2. only displayed when our system's lock_score >= ANYTIME_SCORER_MIN_LOCK,
#      AND the displayed lock_score is capped at ANYTIME_SCORER_DISPLAY_CAP
#      so they read as "Solid Lock"/longshot, not "Elite Lock 95"
_SOCCER_ANYTIME_SCORER_RE = re.compile(
    r"(anytime\s+goal\s*scorer"
    r"|anytime\s+scorer"
    r"|score\s+or\s+assist"
    r"|to\s+score"
    r")",
    re.IGNORECASE,
)
ANYTIME_SCORER_MIN_LOCK = 85.0     # require a strong relative ranking
ANYTIME_SCORER_DISPLAY_CAP = 75.0  # cap displayed score (true prob ≈ 25-45%)

# MLB markets that historically underperform — these are coin-flips at
# best so they pull our headline win % below the user's 75-80% target.
_MLB_BLOCKED_MARKET_RE = re.compile(
    r"(moneyline"
    r"|h2h\b"
    r"|nrfi"
    r"|yrfi"
    r"|first\s+inning"
    r"|no\s+runs"
    r")",
    re.IGNORECASE,
)


def _displayed_lock_score(pick: dict) -> float:
    """Match the same V2-promotion logic used by `_canonicalize_picks`:
    prefer lock_score_v2 when it's set, fall back to lock_score."""
    v2 = pick.get("lock_score_v2")
    if isinstance(v2, (int, float)) and v2 > 0:
        return float(v2)
    return float(pick.get("lock_score") or 0)


def _block_reason(pick: dict) -> str | None:
    """Return why a pick should be blocked, or None if it passes."""
    sport = (pick.get("sport") or "").lower()
    market = (pick.get("market") or "")

    # 1. Soccer goalscorer family — historical data showed 4.8% win across
    #    396 picks, but the breakdown revealed the REAL issue:
    #
    #      First / Last Goal Scorer  →  3.0%  (lottery odds; mis-priced)
    #      Anytime Scorer            → 15.5% (27.3% for ELITE players)
    #
    #    So we don't nuke the whole family — we block ONLY First/Last
    #    Scorer (the lottery markets that were mascarading as Elite Locks)
    #    and keep Anytime / Score-or-Assist, governed by:
    #      (a) lock_score >= ANYTIME_SCORER_MIN_LOCK so only top-1
    #          mathematically-best candidates pass
    #      (b) display cap (handled in `apply_quality_gate`) so they
    #          read as "Solid Lock" / longshot, not Elite Lock 95
    if sport == "soccer":
        if _SOCCER_FIRST_LAST_SCORER_RE.search(market):
            return "first_last_scorer_3pct_lottery"
        if _SOCCER_ANYTIME_SCORER_RE.search(market):
            ls = _displayed_lock_score(pick)
            if ls < ANYTIME_SCORER_MIN_LOCK:
                return f"anytime_scorer_below_lock_floor_{int(ANYTIME_SCORER_MIN_LOCK)}"
            # passes — but caller should cap display lock score

    # 2. Inverted lock-score band (65-74). Historical 12.8% is BELOW the
    #    50-64 band (59.9%) — the calibration is broken in this strip.
    ls = _displayed_lock_score(pick)
    lo, hi = _INVERTED_LOCK_BAND
    if lo <= ls < hi:
        return f"inverted_lock_band_{int(lo)}_{int(hi-1)}_12pct_historical"

    # 3. Sub-50% MLB markets (Moneyline, NRFI/YRFI). These are decided
    #    by single-event variance — a single bunt single torches a YRFI.
    if sport == "mlb" and _MLB_BLOCKED_MARKET_RE.search(market):
        return "mlb_low_winrate_market"

    return None


def _apply_display_cap(pick: dict) -> None:
    """Cap the displayed lock_score for goalscorer Anytime picks so they
    don't appear as `Elite Lock 95` when their true calibrated hit rate
    is closer to 25-45%."""
    sport = (pick.get("sport") or "").lower()
    market = (pick.get("market") or "")
    if sport != "soccer" or not _SOCCER_ANYTIME_SCORER_RE.search(market):
        return
    for field in ("lock_score", "lock_score_v2"):
        v = pick.get(field)
        if isinstance(v, (int, float)) and v > ANYTIME_SCORER_DISPLAY_CAP:
            pick[field] = ANYTIME_SCORER_DISPLAY_CAP
    # If the tier was "Elite Lock", demote to "Solid Lock" so the
    # frontend renders the right color/badge.
    tier = pick.get("tier_v2") or pick.get("tier")
    if tier and "elite" in str(tier).lower():
        pick["tier_v2"] = "Solid Lock"
        pick["tier"] = "Solid Lock"
    pick["display_capped_reason"] = (
        "anytime_scorer_calibration_cap"
    )


def apply_quality_gate(
    picks: Iterable[dict], *, tag_blocked: bool = False,
) -> tuple[list[dict], dict]:
    """Filter the pick list.

    Returns `(kept, stats)` where stats is a dict of
    `{block_reason: count}` for observability.

    Also applies in-place display caps (e.g. Anytime-Scorer lock_score
    clamped to 75 so it doesn't read as "Elite Lock 95") on kept picks.
    """
    kept: list[dict] = []
    blocked_counts: dict[str, int] = {}
    for p in picks:
        reason = _block_reason(p)
        if reason is None:
            _apply_display_cap(p)
            kept.append(p)
            continue
        blocked_counts[reason] = blocked_counts.get(reason, 0) + 1
        if tag_blocked:
            p["quality_gate_block_reason"] = reason
            kept.append(p)
    return kept, blocked_counts
