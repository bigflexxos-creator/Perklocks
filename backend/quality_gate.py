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
from typing import Iterable, Optional


# Error codes for alt-line validation failures (user spec 2026-06-30):
#   line_not_found       — pick's (sport, market, line) has no live row
#   market_removed       — market_key not in live feed for this event
#   stale_odds           — last_seen older than 15 min
#   invalid_alt_mapping  — pick references an alt-line market we don't
#                          map cleanly to an Odds API market key
ALT_LINE_ERR = ("line_not_found", "market_removed",
                "stale_odds", "invalid_alt_mapping")


# Map our internal market labels → Odds API market_key. If we generate
# a pick on an alt-line market that's NOT in this map, the pick is
# rejected with `invalid_alt_mapping` — we won't ship a pick we can't
# validate.
INTERNAL_TO_ODDSAPI_MARKET: dict[str, str] = {
    # Soccer
    "anytime goal scorer":    "player_goal_scorer_anytime",
    "anytime scorer":         "player_goal_scorer_anytime",
    "first goal scorer":      "player_first_goal_scorer",
    "first scorer":           "player_first_goal_scorer",
    "to score or assist":     "player_to_score_or_assist",
    "score or assist":        "player_to_score_or_assist",
    # MLB alt
    "alt hits":               "batter_hits_alternate",
    "batter hits over":       "batter_hits_alternate",
    "batter hits alt":        "batter_hits_alternate",
    "total bases over":       "batter_total_bases_alternate",
    "alt total bases":        "batter_total_bases_alternate",
    "pitcher strikeouts over":"pitcher_strikeouts_alternate",
    "alt strikeouts":         "pitcher_strikeouts_alternate",
    # NFL alt
    "passing yards over":     "player_pass_yds_alternate",
    "alt passing yards":      "player_pass_yds_alternate",
    "rushing yards over":     "player_rush_yds_alternate",
    "alt rushing yards":      "player_rush_yds_alternate",
    "receiving yards over":   "player_reception_alternate",
    "anytime touchdown":      "player_anytime_td",
    "anytime td":             "player_anytime_td",
    # Tennis alt
    "total games over":       "alternate_totals_games",
    "total games under":      "alternate_totals_games",
    "alt games spread":       "alternate_spreads_games",
}


def _map_market_to_oddsapi_key(internal_market: str) -> Optional[str]:
    """Best-effort map from our pick's `market` text to an Odds API key.
    Returns None if we can't classify it as an alt-line — caller decides
    whether to flag as `invalid_alt_mapping` or skip validation entirely
    (for non-alt-line markets like ML / Spread / base Total)."""
    if not internal_market:
        return None
    m = internal_market.lower()
    for needle, mkey in INTERNAL_TO_ODDSAPI_MARKET.items():
        if needle in m:
            return mkey
    return None


def _is_alt_line_pick(pick: dict) -> bool:
    """Should this pick be validated against the live alt-line feed?"""
    mkt = (pick.get("market") or "").lower()
    if not mkt:
        return False
    # Base markets (ML, base spread, base total) are NOT alt-lines and
    # come from the regular odds API path — skip them here.
    if any(k in mkt for k in ("moneyline", "h2h", "run line", "spread")):
        # Spread / run line can be base or alt — only flag as alt if the
        # caller has explicitly tagged it `alt_line: true`.
        if pick.get("alt_line") is True:
            return True
        return False
    # All goalscorer + alt-* + anytime-TD markets are alt-lines.
    if any(k in mkt for k in (
        "goal scorer", "anytime", "first scorer", "last scorer",
        "to score", "score or assist",
        "alt ", "alternate", "over ", "under ",
        "passing yards", "rushing yards", "receiving yards",
        "total bases", "hits", "strikeouts", "outs recorded",
        "total games",
    )):
        return True
    return False
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

# ─── Elite Scorer Anchors (2026-06-30 — user mandate) ────────────────
# The user pointed out that Mbappé / Haaland / Kane score in 75-90% of
# their club matches, but the system was tagging them "COLD 37L" because
# the historical pick-loss aggregation was poisoned by the now-fixed
# settlement bugs (Goal-Header missed, DNP-LOSS, FGS-substitute-LOSS).
#
# Until the pick history rebuilds itself (will take 50-100 more correctly
# graded picks), we anchor these stars' Anytime/To-Score-or-Assist win
# probability on PUBLIC per-match scoring rates from real recent club
# seasons (Understat / official league data). This is what every sharp
# sportsbook does — known elite scorers get a floor.
#
# The rates are conservative — they reflect the FULL season including
# bench/injury matches. Source: 2024-25 club season per-match strike rate.
ELITE_SCORER_ANCHORS: dict[str, float] = {
    # Tier S — generational scorers (rate ≥ 0.78 per match)
    "erling haaland":       0.86,  # 51 goals / 56 matches club + Norway
    "erling braut haaland": 0.86,  # ESPN full name variant
    "kylian mbappé":        0.78,  # 35/47 Real Madrid + PSG + France
    "kylian mbappe":        0.78,
    "robert lewandowski":   0.78,  # 30/40 Barça
    # Tier A — elite scorers (0.65-0.77)
    "harry kane":           0.74,  # 42/56 Bayern + England
    "lautaro martínez":     0.65,
    "lautaro martinez":     0.65,
    "julian alvarez":       0.65,
    "julián álvarez":       0.65,
    "lionel messi":         0.62,  # MLS
    "viktor gyökeres":      0.70,  # Arsenal + Sweden 30+ goals
    "viktor gyokeres":      0.70,
    "victor osimhen":       0.68,
    # Tier B — strong attackers (0.50-0.64)
    "mohamed salah":        0.59,
    "vinicius junior":      0.55,
    "vinícius júnior":      0.55,
    "ousmane dembélé":      0.55,
    "ousmane dembele":      0.55,
    "cristiano ronaldo":    0.55,  # Al-Nassr era
    "lamine yamal":         0.50,
    "bukayo saka":          0.48,
    "jude bellingham":      0.45,
    "cole palmer":          0.50,
    "phil foden":           0.45,
    "florian wirtz":        0.45,
    "rafael leão":          0.45,
    "rafael leao":          0.45,
    "dusan vlahović":       0.55,
    "dusan vlahovic":       0.55,
    "ollie watkins":        0.50,
    "alexander isak":       0.55,
    "raheem sterling":      0.40,
    "rasmus højlund":       0.50,
    "rasmus hojlund":       0.50,
    "darwin núñez":         0.50,
    "darwin nunez":         0.50,
}
# These are SCORE-OR-ASSIST anchors — slightly higher because assists boost.
ELITE_SCORE_OR_ASSIST_BOOST = 0.10  # add 10pp to Anytime rate for SoA markets


def _elite_anchor_rate(player_name: str, market: str) -> Optional[float]:
    """Return the anchored per-match scoring rate for a known elite,
    or None if not on the list. Score-or-Assist markets get a +10pp bump
    over the Anytime rate."""
    if not player_name:
        return None
    key = player_name.strip().lower()
    base = ELITE_SCORER_ANCHORS.get(key)
    if base is None:
        return None
    if "score or assist" in (market or "").lower():
        return min(0.95, base + ELITE_SCORE_OR_ASSIST_BOOST)
    return base


def _extract_player_from_pick(pick: dict) -> str:
    """Best-effort player-name extraction from a goalscorer pick.
    Picks store the player in the market text (e.g.
    "Kylian Mbappé Anytime Goal Scorer") and/or the selection."""
    sel = (pick.get("selection") or "").strip()
    mkt = (pick.get("market") or "").strip()
    # If selection is "Yes" / "No", the player is in the market title.
    if sel.lower() in ("", "yes", "no"):
        # Strip "Anytime Goal Scorer", "First Goal Scorer", etc.
        for suffix in (
            " Anytime Goal Scorer", " Anytime Scorer",
            " First Goal Scorer", " First Scorer",
            " Last Goal Scorer", " Last Scorer",
            " To Score or Assist", " To Score",
            " Score or Assist",
        ):
            if mkt.endswith(suffix):
                return mkt[: -len(suffix)].strip()
        return mkt
    return sel

# ─── Lock-score coherence caps (2026-06-30 — Mbappé bug) ─────────────
# A pick can't simultaneously be a "PEAK 99 Lock" AND have negative edge,
# AND be "70% Win" with -333 odds where implied is 76.9%. The math has
# to add up. When it doesn't, demote the lock-score to match reality.
#
# Real example from the wild:  Kylian Mbappé · To Score or Assist
#   ─ lock_score:    99  (Peak Elite Lock)
#   ─ win_prob:      70%
#   ─ implied:       76.9%   ← market price is HIGHER than our model
#   ─ edge:          -6.9%    ← we'd be paying ABOVE fair value
# Math says: book odds imply 76.9%, our model says 70%, so edge < 0.
# This means we EXPECT to LOSE money on this pick — calling it "Peak 99"
# is misleading. Cap the lock at 70.
NEG_EDGE_LOCK_CAP_HARSH = 60.0   # edge ≤ -3% → cap at 60 (clear bad bet)
NEG_EDGE_LOCK_CAP_SOFT = 70.0    # edge < 0 → cap at 70 (mild value loss)
LOW_WINPROB_LOCK_CAP = 75.0      # win_probability < 0.65 → cap at 75
NO_FORM_DATA_LOCK_CAP = 78.0     # soccer scorer w/o real form data → cap at 78
ELITE_LOCK_FLOOR_PROB = 0.65     # below this, never tag "Elite Lock"

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

# ── Tennis quality controls (2026-06-29) ──────────────────────────────
# Backtest over 178 graded tennis picks:
#   • Odds in -120..-149 (47 picks)        → 44.7% win  (coin-flip)
#   • Alt-total Over ≥ 33 games (8 picks)  → 25.0% win
#   • Alt-total Under ≤ 22 games (10 picks) → 30.0% win
#   • Heavy chalk ≤ -300 (57)              → 86.0% ✅
#   • Lock-Score 95+ (22)                  → 86.4% ✅
#   • Alt Over 14.5/16.5 Games (24)        → 91.7% ✅
# So we keep the winners and prune the coin-flip strip.
_TENNIS_COIN_FLIP_ODDS = (-149, -120)  # inclusive both — historical 44.7%
_TENNIS_LONGSHOT_TOTAL_RE = re.compile(
    r"(over\s+(33|34|35|36|37|38|39|4\d)(\.5)?\s+games"
    r"|under\s+(22|21|20|19|18|17|16|15|14|13|12|11|10|\d)(\.5)?\s+games"
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

    # 4. Tennis quality controls (2026-06-29):
    #
    #    (a) Coin-flip odds band — book_odds in [-149, -120] historically
    #        win at 44.7% (47 sample), worse than just flipping a coin.
    #        These are "barely-favorites" priced like locks; the market
    #        knows something we don't. Drop them.
    #
    #    (b) Long-shot game-total alt-lines (Over ≥ 33, Under ≤ 22).
    #        Historical 25-33% — pure variance, single break of serve
    #        decides it.
    if sport == "tennis":
        odds = pick.get("book_odds")
        if isinstance(odds, (int, float)):
            lo, hi = _TENNIS_COIN_FLIP_ODDS
            if lo <= int(odds) <= hi:
                return "tennis_coin_flip_odds_band_44pct_historical"
        if _TENNIS_LONGSHOT_TOTAL_RE.search(market):
            return "tennis_longshot_alt_total"

    return None


def _apply_display_cap(pick: dict) -> None:
    """Cap the displayed lock_score for goalscorer Anytime picks so they
    don't appear as `Elite Lock 95` when their true calibrated hit rate
    is closer to 25-45%."""
    sport = (pick.get("sport") or "").lower()
    market = (pick.get("market") or "")
    if sport != "soccer" or not _SOCCER_ANYTIME_SCORER_RE.search(market):
        return
    # NOTE: must cap ALL shadow lock fields (`lock_score_raw`,
    # `lock_score_peak`) too, otherwise `_canonicalize_lock_score` does
    # `max(v1, v2, raw, peak)` at read time and silently RESTORES the
    # uncapped value, defeating this cap. Code-review HIGH 2026-06-30.
    for field in ("lock_score", "lock_score_v2", "lock_score_raw", "lock_score_peak"):
        v = pick.get(field)
        if isinstance(v, (int, float)) and v > ANYTIME_SCORER_DISPLAY_CAP:
            pick[field] = ANYTIME_SCORER_DISPLAY_CAP
    # Mark the hard ceiling so canonicalize/elite-floor read-time logic
    # cannot promote past this cap.
    existing_ceiling = pick.get("coherence_cap_ceiling")
    if (not isinstance(existing_ceiling, (int, float))
            or ANYTIME_SCORER_DISPLAY_CAP < existing_ceiling):
        pick["coherence_cap_ceiling"] = float(ANYTIME_SCORER_DISPLAY_CAP)
    # If the tier was "Elite Lock", demote to "Solid Lock" so the
    # frontend renders the right color/badge.
    tier = pick.get("tier_v2") or pick.get("tier")
    if tier and "elite" in str(tier).lower():
        pick["tier_v2"] = "Solid Lock"
        pick["tier"] = "Solid Lock"
    pick["display_capped_reason"] = (
        "anytime_scorer_calibration_cap"
    )


def _apply_elite_scorer_anchor(pick: dict) -> None:
    """Anchor goalscorer pick probability on real per-match scoring rate.

    When a known elite scorer (Mbappé / Haaland / Kane / Messi / Salah …)
    is on the card for an Anytime / Score-or-Assist market, override the
    model's win_probability with the real-world rate from
    ELITE_SCORER_ANCHORS, recompute edge against the book's implied
    probability, and re-set lock_score to a sane value (= anchor × 100,
    capped at 88). Also suppress the misleading "COLD" tag drawn from
    the pre-fix poisoned pick history.
    """
    sport = (pick.get("sport") or "").lower()
    market = (pick.get("market") or "")
    if sport != "soccer" or not _SOCCER_GOALSCORER_FAMILY_RE.search(market):
        return

    player = _extract_player_from_pick(pick)
    anchor = _elite_anchor_rate(player, market)
    if anchor is None:
        return

    is_anytime = bool(_SOCCER_ANYTIME_SCORER_RE.search(market))
    if is_anytime:
        prev_wp = _coerce_float(pick.get("win_probability"))
        anchor_stored = anchor * 100 if (prev_wp is not None and prev_wp > 1.5) else anchor
        pick["win_probability"] = anchor_stored
        pick.setdefault("anchor_source", []).append(
            f"elite_scorer:{player}={anchor:.2f}"
        )
        implied = _coerce_float(pick.get("implied_probability"))
        if implied is None:
            odds = _coerce_float(pick.get("book_odds"))
            if odds is not None:
                implied = (
                    abs(odds) / (abs(odds) + 100)
                    if odds <= -100 else 100 / (odds + 100)
                )
        if implied is not None:
            if implied > 1.5:
                implied = implied / 100.0
            pick["edge_percent"] = round((anchor - implied) * 100, 2)
            new_lock = round(min(88.0, anchor * 100), 1)
            for ls in ("lock_score", "lock_score_v2", "lock_score_peak"):
                if isinstance(pick.get(ls), (int, float)):
                    pick[ls] = new_lock

    # Always suppress false cold tag for elites.
    pick["suppress_cold_tag"] = True
    pick["player_elite_anchored"] = True
    # CRITICAL (2026-06-30 user audit): the streak chip ("COLD · 15L")
    # was computed from POISONED pick history (pre-fix Goal-Header /
    # DNP-LOSS bugs). The user's mandate: "I want to keep the current
    # streaks but accurate ones". So we DO NOT zero them here — the
    # `enrich_picks_with_real_streaks` step (in picks_routes.py, after
    # quality_gate) replaces them with REAL match data from
    # `soccer_player_form` (Understat per-match). For non-elite picks
    # outside that pipeline, the streak stays as-is. Anchor flag still
    # gets set so the suppress_cold_tag UI signal is available if needed.
    for blob_field in ("historical_signal", "player_profile_pp", "deep_dive_v2"):
        blob = pick.get(blob_field)
        if isinstance(blob, dict):
            for k in ("cold_streak", "recent_losses", "current_streak",
                      "trending_label", "hot_cold"):
                blob.pop(k, None)


def _coerce_float(v):
    try:
        return float(v) if v is not None else None
    except Exception:
        return None


def _apply_lockscore_coherence(pick: dict) -> None:
    """Hard cap lock_score / lock_score_v2 / lock_score_peak so the card
    math is internally consistent.

    Three guardrails (2026-06-30 Mbappé bug):
      1. Negative edge → max lock_score = NEG_EDGE_LOCK_CAP_SOFT (70)
         If edge ≤ -3% → max = NEG_EDGE_LOCK_CAP_HARSH (60)
      2. win_probability < 0.65 → max lock_score = LOW_WINPROB_LOCK_CAP (75)
      3. Soccer goalscorer family w/ player_form.games_logged == 0
         (no real recent-form data) → max lock_score = NO_FORM_DATA_LOCK_CAP (78)

    These are NOT generation gates — generation already runs. This caps
    the DISPLAYED number so the card doesn't lie. Generation will be
    re-calibrated in a separate pass.
    """
    edge = _coerce_float(pick.get("edge_percent"))
    wp = _coerce_float(pick.get("win_probability"))
    # Some pipelines store win_probability as 0-100, others as 0-1.
    if wp is not None and wp > 1.5:
        wp = wp / 100.0

    cap = None
    reason = None

    # 1. Negative edge → demote.
    if edge is not None and edge <= -3:
        cap = NEG_EDGE_LOCK_CAP_HARSH
        reason = "harsh_negative_edge"
    elif edge is not None and edge < 0:
        cap = NEG_EDGE_LOCK_CAP_SOFT
        reason = "negative_edge"

    # 2. Low model win probability → demote.
    if wp is not None and wp < ELITE_LOCK_FLOOR_PROB:
        if cap is None or LOW_WINPROB_LOCK_CAP < cap:
            cap = LOW_WINPROB_LOCK_CAP
            reason = "win_prob_below_elite_floor"

    # 3. Soccer scorer with no real recent-form data → demote.
    sport = (pick.get("sport") or "").lower()
    market = (pick.get("market") or "")
    if sport == "soccer" and _SOCCER_GOALSCORER_FAMILY_RE.search(market):
        pf = pick.get("player_form") or {}
        games_logged = _coerce_float(pf.get("games_logged"))
        if games_logged is not None and games_logged < 1:
            if cap is None or NO_FORM_DATA_LOCK_CAP < cap:
                cap = NO_FORM_DATA_LOCK_CAP
                reason = "no_recent_form_data"

    if cap is None:
        return

    capped = False
    # NOTE: must cap ALL shadow lock fields (`lock_score_raw` too),
    # otherwise `_canonicalize_lock_score` does `max(v1, v2, raw, peak)`
    # at read time and silently RESTORES the uncapped value, defeating
    # this cap. Code-review HIGH 2026-06-30.
    for field in ("lock_score", "lock_score_v2",
                  "lock_score_raw", "lock_score_peak"):
        v = pick.get(field)
        if isinstance(v, (int, float)) and v > cap:
            pick[field] = round(float(cap), 1)
            capped = True
    if capped:
        # Record the hard ceiling so `_canonicalize_lock_score` and the
        # elite-floor logic can honour it (otherwise the elite-floor
        # would bump back to 95 and the max() would re-promote).
        existing_ceiling = pick.get("coherence_cap_ceiling")
        if (not isinstance(existing_ceiling, (int, float))
                or cap < existing_ceiling):
            pick["coherence_cap_ceiling"] = float(cap)
        # Demote tier label to match the lower number — so the UI
        # doesn't paint a "PEAK 99" gold badge on a 60-cap pick.
        for tier_field in ("tier_v2", "tier"):
            t = pick.get(tier_field)
            if t and "elite" in str(t).lower() and cap < 95:
                pick[tier_field] = "Solid Lock"
        # Also nuke any "PEAK" / "PRIME" tag that depends on lock score.
        for badge_field in ("peak_tag", "prime_tag", "lock_badge"):
            if pick.get(badge_field):
                pick[badge_field] = None
        pick.setdefault("coherence_caps", []).append(reason)


async def validate_against_live_alt_lines(
    picks: list[dict], db, *, max_stale_minutes: int = 15,
) -> tuple[list[dict], dict]:
    """Post-fetch validation: every alt-line pick must match a live row
    in `live_alt_lines`. Picks that don't are removed.

    Returns (kept, stats) where stats is `{error_code: count}` with the
    four user-specified codes: line_not_found / market_removed /
    stale_odds / invalid_alt_mapping.

    Soft-fail design: if the live_alt_lines collection is empty (e.g.
    the refresh loop hasn't run yet), we DO NOT block any picks —
    blocking everything would dump the app. Instead we log a warning
    and skip validation until the feed populates.
    """
    from datetime import datetime, timezone, timedelta
    stats = {k: 0 for k in ALT_LINE_ERR}
    stats["passed"] = 0
    stats["skipped_base_market"] = 0

    # Soft-fail: if the feed is dry, skip validation gracefully.
    try:
        feed_count = await db.live_alt_lines.estimated_document_count()
    except Exception:
        feed_count = 0
    if feed_count == 0:
        import logging
        logging.getLogger("lockscore").warning(
            "alt-line validation skipped — live_alt_lines is empty"
        )
        return picks, {"skipped_feed_empty": len(picks)}

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_stale_minutes)
    kept: list[dict] = []
    for p in picks:
        if not _is_alt_line_pick(p):
            stats["skipped_base_market"] += 1
            kept.append(p)
            continue
        sport = (p.get("sport") or "").lower()
        market_text = p.get("market") or ""
        oddsapi_market = _map_market_to_oddsapi_key(market_text)
        if not oddsapi_market:
            stats["invalid_alt_mapping"] += 1
            continue
        # Extract player / selection
        sel = p.get("selection") or ""
        if sel.lower() in ("", "yes", "no", "over", "under"):
            # For Yes/Over/etc., the player/team name is in the market text.
            sel = market_text
            # Strip suffixes like "Anytime Goal Scorer"
            for suffix in (
                " Anytime Goal Scorer", " First Goal Scorer",
                " Last Goal Scorer", " To Score or Assist",
                " Anytime Touchdown", " Anytime TD",
            ):
                if sel.endswith(suffix):
                    sel = sel[: -len(suffix)].strip()
                    break
        sel_norm = re.sub(r"\s+", " ",
                          re.sub(r"[^a-z0-9 ]+", " ", sel.lower())).strip()

        line = p.get("line")
        if line is None:
            # Try common alt-extract: market "Over 0.5 Hits" → 0.5
            m = re.search(r"(?:over|under)\s+([\d.]+)", market_text, re.I)
            if m:
                try:
                    line = float(m.group(1))
                except Exception:
                    line = None

        q: dict = {
            "sport": sport if sport != "nfl" else "nfl",
            "market_key": oddsapi_market,
            "selection_norm": sel_norm,
            "last_seen": {"$gte": cutoff},
        }
        if line is not None:
            q["line"] = float(line)
        row = await db.live_alt_lines.find_one(q)
        if row:
            # Pass: stamp the validation metadata on the pick.
            p["alt_line_validated"] = True
            p["validated_sportsbook"] = row.get("sportsbook")
            p["validated_market_id"] = row.get("market_id")
            p["validated_selection_id"] = row.get("selection_id")
            p["validated_price"] = row.get("price")
            p["validated_last_seen"] = (
                row.get("last_seen").isoformat()
                if hasattr(row.get("last_seen"), "isoformat") else None
            )
            stats["passed"] += 1
            kept.append(p)
            continue

        # No match — figure out which error code applies.
        # 1. Try the same query WITHOUT the line filter — if we find a
        #    match, the market exists but our line is wrong.
        q2 = {k: v for k, v in q.items() if k != "line"}
        row2 = await db.live_alt_lines.find_one(q2)
        if row2:
            stats["line_not_found"] += 1
            continue
        # 2. Try without selection — if we find one, the market exists
        #    but our player/selection doesn't.
        q3 = {k: v for k, v in q.items() if k not in ("line", "selection_norm")}
        row3 = await db.live_alt_lines.find_one(q3)
        if row3:
            stats["line_not_found"] += 1
            continue
        # 3. Otherwise — market not present for this event at all.
        stats["market_removed"] += 1

    return kept, stats


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
            # ELITE ANCHOR FIRST — if this is a Mbappé / Haaland / Kane
            # / Messi / Salah etc. goalscorer pick, override win_prob
            # with the real per-match scoring rate and recompute edge.
            _apply_elite_scorer_anchor(p)
            # Then display caps for Anytime markets (75 ceiling) and
            # coherence guardrails (negative edge / low win_prob).
            _apply_display_cap(p)
            _apply_lockscore_coherence(p)
            kept.append(p)
            continue
        blocked_counts[reason] = blocked_counts.get(reason, 0) + 1
        if tag_blocked:
            p["quality_gate_block_reason"] = reason
            kept.append(p)
    return kept, blocked_counts
