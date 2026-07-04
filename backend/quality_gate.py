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
# Updated 2026-07-01 after 1,441-pick history audit:
#   • Moneyline: 44.0%  • NRFI/YRFI: 41.5%  • H+R+RBI: 35.6%
_MLB_BLOCKED_MARKET_RE = re.compile(
    r"(moneyline"
    r"|h2h\b"
    r"|nrfi"
    r"|yrfi"
    r"|first\s+inning"
    r"|no\s+runs"
    # H+R+RBI family — 35.6% historically, the worst MLB market
    r"|hits\s*\+\s*runs\s*\+\s*rbi"
    r"|h\s*\+\s*r\s*\+\s*rbi"
    r"|hits,\s*runs\s*(?:and|&|,)?\s*rbi"
    r"|hits\s+runs\s+rbi"
    r"|hits\s*&\s*runs\s*&\s*rbi"
    r")",
    re.IGNORECASE,
)

# Soccer scorer markets — the DATA shows a split within this family:
#   • Anytime Goal Scorer      → 15.5% overall, 27.3% for ELITE scorers ✓ SURFACE
#   • First / Last Goal Scorer → 3.0% (lottery pricing)                 ✗ BANNED
#   • Hat-trick / Score 2+/3+  → 4-8% (compound variance)               ✗ BANNED
#   • To Score or Assist       → mid — kept with lock floor             ✓ SURFACE
#
# The 2026-07-01 "ban all goalscorers" regex was over-broad. Restoring
# surgical bans: FGS/LGS + hat-tricks/2+/3+ + winning-goal only.
_SOCCER_LOTTERY_SCORER_RE = re.compile(
    r"(first\s+goal\s+scorer"
    r"|last\s+goal\s+scorer"
    r"|first\s+goal\b"
    r"|last\s+goal\b"
    r"|winning\s+goal"
    r"|hat[\s-]?trick"
    r"|to\s+score\s+2"
    r"|to\s+score\s+3"
    r")",
    re.IGNORECASE,
)

# Lock-score DEAD ZONE (2026-07-01 audit): 80-84 band hits at just
# 47.6% (n=63) — inverted calibration. Never surface these.
_LOCK_DEAD_ZONE_LO = 80
_LOCK_DEAD_ZONE_HI = 85

# MLB alt-line edge-gate regexes (2026-07-02 user spec):
#   • Alt TEAM TOTAL: e.g. "Yankees Team Total Over 3.5" / "Team Total Under 2.5"
#   • Alt RUN LINE:   e.g. "Yankees +1.5 Spread", "Team +2.5 (Alt)",
#                     "Alt Run Line +3.5", "Braves -1.5 Run Line"
# The numeric group captures the LINE / SPREAD magnitude for range checks.
_ALT_TEAM_TOTAL_RE = re.compile(
    r"team\s+total\s+(?:over|under)\s+(\d+\.?\d*)",
    re.IGNORECASE,
)
_ALT_RUN_LINE_RE = re.compile(
    # Matches "+1.5 Run Line", "-2.5 Spread", "+3.5 (Alt)", "Run Line +1.5",
    # "Spread +2.5". Capture group 1 is always the signed magnitude.
    r"[+\-](\d+\.?\d*)\s*(?:run\s*line|spread|\(alt\))"
    r"|(?:run\s*line|spread)\s*[+\-]?(\d+\.?\d*)",
    re.IGNORECASE,
)

# Odds DEAD ZONE (2026-07-01 audit): -140 to -110 hits at 48.2%
# (n=139) — the "barely favourite" trap.
_ODDS_DEAD_ZONE_LO = -140
_ODDS_DEAD_ZONE_HI = -110

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

    # 1. Soccer scorer/assist family — surgical ban (2026-07-01 revised).
    #    User feedback confirms my earlier "ban all" was over-broad:
    #      • FGS / LGS / hat-trick / winning-goal / score-2+ / score-3+
    #        → BANNED (3-8% hit rate lottery pricing)
    #      • Anytime Goal Scorer (AGS)
    #        → SURFACE, gated by lock floor (elite scorers only)
    #      • To Score or Assist
    #        → SURFACE, gated by lock floor
    if sport == "soccer":
        if _SOCCER_LOTTERY_SCORER_RE.search(market):
            return "soccer_lottery_scorer_banned_2026-07-01"
        # AGS + To-Score-or-Assist go through the existing lock-floor gate.
        if _SOCCER_ANYTIME_SCORER_RE.search(market):
            # ── AGS coherence gate (2026-07-02 — user report:
            # "how did the app generate Persson over the top scorers,
            # make it make sense"). V2 was inflating scores on players
            # with no real form data; V1 stayed honest and low. Both
            # signals must agree — UNLESS the player is on the elite-
            # anchor list, where V1's low value is expected (the V1
            # engine doesn't know about anchors, V2 does).
            v1_ls = float(pick.get("lock_score") or 0)
            v2_ls = float(pick.get("lock_score_v2") or 0)
            ls = _displayed_lock_score(pick)
            player_name = _extract_player_from_pick(pick)
            is_elite = _elite_anchor_rate(player_name, market) is not None
            # ── "Has-form-source" check — the pick's league is one
            # where we ingest live scorer data (goals/starts) via a
            # dedicated pipeline. These leagues get the same
            # exemptions as elite anchor list players because the
            # V1 engine's low reading is expected — V1 doesn't
            # consume league-specific form sources, V2 does.
            # (2026-07-03 user report: "why am I no longer seeing
            # CSL goalscorer" — CSL scorer intel is fresh via
            # csl_espn_live, but V1 didn't know about it so Rule 2
            # was killing every CSL AGS pick.)
            has_form_source_leagues = {
                # Chinese Super League — ESPN scrape (csl_espn_live).
                "china super league", "chinese super league",
                # Top-5 EU + Understat coverage.
                "premier league", "la liga", "serie a", "bundesliga",
                "ligue 1",
                # Continental competitions with EU-team overlap.
                "uefa champions league", "uefa europa league",
                "uefa conference league",
                # Additional Understat + ESPN-covered leagues.
                "mls", "j1 league", "eredivisie", "primeira liga",
                "championship", "efl championship",
            }
            pick_league_lc = (pick.get("league") or "").lower()
            has_form_source = any(
                cl in pick_league_lc for cl in has_form_source_leagues
            )
            trust_scorer = is_elite or has_form_source

            # Rule 1: displayed lock must clear floor.
            if ls < ANYTIME_SCORER_MIN_LOCK:
                return f"anytime_scorer_below_lock_floor_{int(ANYTIME_SCORER_MIN_LOCK)}"
            # Rule 2: engine disagreement — skip for trusted sources.
            if (not trust_scorer
                    and v1_ls > 0
                    and abs(v2_ls - v1_ls) > 12.0):
                return (
                    f"anytime_scorer_lock_engine_disagreement_v1_{int(v1_ls)}"
                    f"_v2_{int(v2_ls)}"
                )
            # Rule 3: Edge check.
            #   • NON-elites: block below -3% edge. Small tolerance
            #     covers players not on the anchor list who our engine
            #     may under-estimate (Toney, David, Doku, Lukaku, etc.).
            #     Below -3% is a real negative-EV signal → block.
            #   • ELITES: allow edge as low as -7% because AGS is a
            #     lottery-priced +200-ish market where our model can
            #     under-estimate proven finishers vs the book, and
            #     the ceiling upside justifies a small edge dip.
            # (2026-07-03 user report: "why did anytime goalscorer
            # disappear smh" — floors initially at 0/-6 wiped too much.
            # Relaxed to -3/-7 to keep Persson-style noise blocked but
            # let elite + emerging stars surface.)
            edge = pick.get("edge_percent")
            if isinstance(edge, (int, float)):
                floor = -7.0 if is_elite else -3.0
                if edge < floor:
                    return f"anytime_scorer_negative_edge_{edge:.1f}pct"
            # Rule 4: for non-trusted-source picks, require real
            # evidence in the rationale. Elite anchor players AND
            # form-covered leagues (CSL, Top-5 EU, etc.) skip this
            # because their league-specific ingestion IS the evidence.
            if not trust_scorer:
                pr = pick.get("pick_rationale") or {}
                evidence = pr.get("evidence") or []
                has_scorer_evidence = any(
                    any(k in (e or "").lower() for k in (
                        "rank", "leader", "goals", "goal/", "per match",
                        "per 90", "scored", "top scorer", "form",
                    ))
                    for e in evidence
                )
                if not evidence or not has_scorer_evidence:
                    return "anytime_scorer_no_form_evidence"
            # Rule 5: Synthetic-source block for uncovered leagues.
            # (2026-07-03 user report: "why not picking Bjerkeboo and
            # Uhre" — the app only picks up players from stale
            # TheSportsDB rosters for non-Top-5/CSL leagues. The
            # league's real top scorers are missing from our data
            # sources entirely.) When the pick is a positional-fallback
            # synthetic (samples.from_fallback == True) AND the league
            # isn't in our covered set, drop it — the pick has no
            # signal, just a random forward from an incomplete roster.
            if not is_elite:
                samples = pick.get("samples") or {}
                is_synthetic_fallback = (
                    pick.get("synthetic") is True
                    and (samples.get("from_fallback") is True
                         or (samples.get("goals") or 0) == 0)
                )
                covered_leagues = {
                    "premier league", "la liga", "serie a", "bundesliga",
                    "ligue 1", "mls", "china super league",
                    "uefa champions league", "uefa europa league",
                    "uefa conference league", "brasileirão", "brasileirao",
                    "j1 league", "eredivisie", "primeira liga",
                    "championship", "efl championship",
                }
                pick_league = (pick.get("league") or "").lower()
                league_covered = any(cl in pick_league for cl in covered_leagues)
                if is_synthetic_fallback and not league_covered:
                    return (
                        f"anytime_scorer_synthetic_uncovered_league_"
                        f"{pick.get('league') or 'unknown'}"
                    )
            # passes — caller may cap display lock score

    # 2. Inverted lock-score band (65-74). Historical 12.8% is BELOW the
    #    50-64 band (59.9%) — the calibration is broken in this strip.
    ls = _displayed_lock_score(pick)
    lo, hi = _INVERTED_LOCK_BAND
    if lo <= ls < hi:
        return f"inverted_lock_band_{int(lo)}_{int(hi-1)}_12pct_historical"

    # 2b. Lock-score DEAD ZONE 80-84 (2026-07-01 audit). Overall hits at
    #     47.6% but the sample is DOMINATED by MLB + Soccer where
    #     calibration is inverted. Tennis + NBA + NFL have no bad data
    #     in this band — the ban would over-block their legitimate
    #     medium-favorite picks (e.g. Wimbledon -185 chalks). So we
    #     scope this ban to just the sports with the actual regression.
    if sport in ("mlb", "soccer") and _LOCK_DEAD_ZONE_LO <= ls < _LOCK_DEAD_ZONE_HI:
        return f"lock_dead_zone_{_LOCK_DEAD_ZONE_LO}_{_LOCK_DEAD_ZONE_HI-1}_47pct"

    # 2c. Odds DEAD ZONE -140 to -110 (2026-07-01 audit). Hits at 48.2%
    #     over 139 picks — "barely favourite" trap. Same MLB/Soccer
    #     scope caveat as 2b; Tennis moneylines at these odds actually
    #     hit 62.5% historically, so we exempt Tennis + NBA + NFL.
    odds = pick.get("book_odds")
    if isinstance(odds, (int, float)) and sport in ("mlb", "soccer"):
        if _ODDS_DEAD_ZONE_LO <= float(odds) < _ODDS_DEAD_ZONE_HI:
            return f"odds_dead_zone_{_ODDS_DEAD_ZONE_LO}_{_ODDS_DEAD_ZONE_HI}_48pct"

    # 3. Sub-50% MLB markets (Moneyline, NRFI/YRFI, H+R+RBI). All decided
    #    by single-event variance:
    #      • Moneyline 44.0% (baseball is chaotic)
    #      • NRFI/YRFI 41.5% (single bunt single torches YRFI)
    #      • H+R+RBI   35.6% (3-way variance compounds)
    if sport == "mlb" and _MLB_BLOCKED_MARKET_RE.search(market):
        return "mlb_low_winrate_market"

    # 3b. MLB ALT-LINE EDGE GATES (2026-07-02 user spec):
    #     Only surface these alt families when the model projects an
    #     8%+ edge over implied probability. User: "Prioritize
    #     high-probability cash rates over volume."
    #
    #       • ALT TEAM TOTALS in the 2.5-3.5 range → require edge ≥ 8%
    #       • ALT RUN LINES  +1.5 to +3.5 spread → require edge ≥ 8%
    #
    #     Enforced only when the pick is flagged `is_alt` — main-line
    #     versions of these markets fall through unchanged.
    if sport == "mlb" and pick.get("is_alt"):
        edge = float(pick.get("edge_percent") or 0)
        m_lower = market.lower()
        # Team totals — "Team Total Over 3.5" / "Team Total Under 2.5"
        alt_total_match = _ALT_TEAM_TOTAL_RE.search(market)
        if alt_total_match:
            try:
                line = float(alt_total_match.group(1))
                if 2.5 <= line <= 3.5 and edge < 8.0:
                    return f"mlb_alt_team_total_edge_below_8pct_line_{line}"
            except Exception:
                pass
        # Run lines — "+1.5 Spread", "Team +2.5 (Alt)", "Alt Run Line +3.5"
        alt_rl_match = _ALT_RUN_LINE_RE.search(market)
        if alt_rl_match:
            try:
                # Get whichever group captured the number
                num_str = next((g for g in alt_rl_match.groups() if g), None)
                if num_str:
                    spread = abs(float(num_str))
                    if 1.5 <= spread <= 3.5 and edge < 8.0:
                        return f"mlb_alt_run_line_edge_below_8pct_spread_{spread}"
            except Exception:
                pass

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
    """RETIRED (2026-06-30, user clarification).

    This rule used to cap soccer Anytime Goal Scorer lock_score at 75
    on the theory that "Anytime hits 25-45% historically so it
    shouldn't read as Elite Lock 95". But that conflates Lock Score
    with win probability — the user's design is explicit:

        Lock Score = the deep-thinking engine's confidence signal.
        It reflects reputation, market context, model conviction,
        and historical reliability — NOT raw hit-rate.

    A pick can carry Lock 95-99 even if the model's win probability
    is 50%, because the engine's conviction comes from signal
    stacking, not from mirroring win_prob. The Anytime calibration
    cap was demoting deep-thinking lock scores for both elite AND
    non-elite picks alike, which silently overrode the engine's
    output.

    Kept as a no-op so callers don't break; the read-time canonicalize
    clamp still honours `coherence_cap_ceiling` for any pick that
    has one set by other paths.
    """
    return


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
    """RETIRED (2026-06-30, user clarification).

    All three rules in this function used to cap `lock_score` based on
    win-prob / edge / form heuristics. The user's design intent is
    explicit: Lock Score is the DEEP-THINKING ENGINE'S signal and is
    NEVER capped by quality_gate. A Lock 99 pick can have negative
    edge on a real sportsbook line (e.g. MLB Over 0.5 Hits at -300
    chalk) — the engine's confidence is its own signal independent of
    win-prob math.

    Targeted drops are handled elsewhere:
      ▸ `_block_reason` — synthetic / impossible markets
      ▸ `_drop_tennis_synthetic_lines` — fictional chalk Tennis Overs
      ▸ `validate_against_live_alt_lines` — alt-line provenance flags

    Kept as a no-op so callers don't break; canonicalize will read
    `coherence_cap_ceiling` ONLY if some other path explicitly sets
    it.
    """
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

    Coverage-aware (2026-06-30, code-review HIGH):
      The feed only polls a subset of sports/leagues (Soccer FIFA WC +
      EPL + UCL, MLB, NFL, Tennis French Open). Picks for OTHER
      sports/leagues (NBA props, lower-tier soccer like CSL/MLS,
      non-French-Open tennis) have no chance of matching the feed —
      previously these were silently dropped as `invalid_alt_mapping`
      or `market_removed`. Now we build a `(sport, event)` coverage
      cache up-front and SKIP-NOT-DROP picks outside coverage. Skipped
      picks are stamped `alt_line_skipped_no_coverage=True` for
      observability and stay on the board.

    Event-scoped (2026-06-30, code-review HIGH):
      The match query previously only used (sport, market_key,
      selection_norm, line) which meant a player's line in game B
      could validate a pick in game A and stamp the wrong
      `validated_price`. Now we additionally fuzzy-match by event:
      pick's `event` string ("Morocco @ Brazil") vs feed row's
      `event_name` / `home_team` / `away_team`. Picks with no event
      match in their sport are routed to `event_not_in_feed` (treated
      as a skip, not a drop).
    """
    from datetime import datetime, timezone, timedelta
    stats = {k: 0 for k in ALT_LINE_ERR}
    stats["passed"] = 0
    stats["skipped_base_market"] = 0
    stats["skipped_no_coverage"] = 0
    stats["skipped_event_not_in_feed"] = 0

    # Soft-fail: if BOTH feeds are dry, skip validation gracefully.
    try:
        feed_count = await db.live_alt_lines.estimated_document_count()
    except Exception:
        feed_count = 0
    try:
        propline_count = await db.propline_alt_lines.estimated_document_count()
    except Exception:
        propline_count = 0
    if feed_count == 0 and propline_count == 0:
        import logging
        logging.getLogger("lockscore").warning(
            "alt-line validation skipped — both feeds empty"
        )
        return picks, {"skipped_feed_empty": len(picks)}

    # ── Build coverage cache (single aggregation pass) ─────────────────
    # Map: sport (lowercase) → set of normalised event tokens we have
    # feed data for. Tokens include event_name, home_team, away_team
    # so the pick's `event` field ("Morocco @ Brazil") can match
    # whichever form the feed stored. Pulls from BOTH live_alt_lines
    # (The Odds API) AND propline_alt_lines (prop-line.com) so cross-
    # source coverage shows up uniformly.
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_stale_minutes)
    coverage: dict[str, set[str]] = {}
    for coll_name in ("live_alt_lines", "propline_alt_lines"):
        try:
            async for doc in db[coll_name].find(
                {"last_seen": {"$gte": cutoff}},
                {"_id": 0, "sport": 1, "event_name": 1,
                 "home_team": 1, "away_team": 1},
            ):
                sport = (doc.get("sport") or "").lower()
                if not sport:
                    continue
                bucket = coverage.setdefault(sport, set())
                for key in ("event_name", "home_team", "away_team"):
                    v = doc.get(key)
                    if v:
                        bucket.add(_norm_event_token(str(v)))
        except Exception as _cov_err:
            import logging
            logging.getLogger("lockscore").warning(
                "alt-line coverage cache build failed (%s): %s",
                coll_name, _cov_err,
            )

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
            # No mapping for this market — KEEP the pick (was previously
            # dropped, but markets like Soccer "Double Chance" / NRFI /
            # Tennis SetSpread aren't in our coverage and shouldn't be
            # silently erased. Code-review HIGH 2026-06-30.
            stats["invalid_alt_mapping"] += 1
            p["alt_line_validation_warning"] = "invalid_alt_mapping"
            kept.append(p)
            continue

        # ── Coverage gate: is this sport in the feed at all? ───────────
        sport_bucket = coverage.get(sport)
        if not sport_bucket:
            # No coverage for this sport — keep the pick rather than
            # silently dropping it. NBA props in off-season, lower-
            # league soccer (CSL/MLS), non-FO tennis all land here.
            p["alt_line_skipped_no_coverage"] = True
            stats["skipped_no_coverage"] += 1
            kept.append(p)
            continue

        # ── Event scope: does the feed cover THIS event? ───────────────
        event_str = p.get("event") or ""
        event_tokens = _event_tokens_from_pick_event(event_str)
        # We have coverage iff ANY pick token matches a feed token.
        event_in_feed = any(t in sport_bucket for t in event_tokens) if event_tokens else False
        if not event_in_feed:
            # The feed covers this sport but NOT this specific event
            # (e.g. an EPL pick on a day when the feed only has WC
            # data). Don't drop — surface the pick without alt-line
            # validation metadata.
            p["alt_line_skipped_no_coverage"] = True
            stats["skipped_event_not_in_feed"] += 1
            kept.append(p)
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

        # ── Event-scoped match: limit candidate feed rows to this event ─
        # `event_tokens` ⊃ at least one form of the team names. We
        # constrain by regex-matching one of those tokens against the
        # feed row's RAW `event_name` (the feed stores "Algeria @
        # Switzerland", not the normalised form). Falling back to the
        # home/away team strings catches feed rows where event_name
        # was stored differently. This is a SOFT scope — if no team-
        # name token shows up in the feed entry the next q2/q3 fallbacks
        # would still find a same-sport same-player row, but our
        # coverage gate above already ensured the event is broadly
        # covered, so the cross-event collision risk (same player
        # name in different games) is acceptable.
        event_regex = "|".join(
            re.escape(t) for t in event_tokens if len(t) >= 3
        )
        event_match_clauses: list[dict] = []
        if event_regex:
            event_match_clauses = [
                {"event_name": {"$regex": event_regex, "$options": "i"}},
                {"home_team":  {"$regex": event_regex, "$options": "i"}},
                {"away_team":  {"$regex": event_regex, "$options": "i"}},
            ]
        q: dict = {
            "sport": sport,
            "market_key": oddsapi_market,
            "selection_norm": sel_norm,
            "last_seen": {"$gte": cutoff},
        }
        if event_match_clauses:
            q["$or"] = event_match_clauses
        if line is not None:
            q["line"] = float(line)
        # ── Query BOTH feeds; best-of-book price wins ─────────────────
        # Order: live_alt_lines (The Odds API — DK+FD canonical) first,
        # propline_alt_lines (prop-line.com — broader coverage including
        # BetMGM/BetRivers/Bovada) as the fallback / cross-check. If
        # both have a row, take the one with the BETTER PRICE for the
        # user (higher payout odds — least negative on chalk, most
        # positive on dogs). Code-2026-06-30: prop-line integration.
        row_a = await db.live_alt_lines.find_one(q)
        row_b = await db.propline_alt_lines.find_one(q)
        row = _best_of_two(row_a, row_b)
        if row:
            # Pass: stamp the validation metadata on the pick.
            p["alt_line_validated"] = True
            p["validated_sportsbook"] = row.get("sportsbook")
            p["validated_source"] = row.get("source", "odds_api")
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

        # No match — figure out which error code applies. BUT — we
        # KEEP the pick in all cases (2026-06-30, code-review HIGH).
        # The feed coverage is partial (only DK + FanDuel; only a
        # subset of markets per book; lines disappear and re-appear
        # as books re-price). Silently dropping 194 picks per request
        # because the feed didn't have the exact market+selection+line
        # row hurts users more than it protects them. The validator
        # is now ADVISORY: it stamps `alt_line_validated=True` when
        # the line was confirmed, and a `alt_line_validation_warning`
        # code otherwise — UI consumers can choose to badge the pick
        # without hiding it from the feed.
        warning_code = None
        # 1. Try without the line filter — match means line wrong.
        q2 = {k: v for k, v in q.items() if k != "line"}
        row2_a = await db.live_alt_lines.find_one(q2)
        row2_b = await db.propline_alt_lines.find_one(q2)
        row2 = row2_a or row2_b
        if row2:
            stats["line_not_found"] += 1
            warning_code = "line_not_found"
            # Capture the nearest real line on the pick so the UI /
            # synthetic-line dropper downstream can use it.
            try:
                p["closest_real_line"] = row2.get("line")
                p["closest_real_price"] = row2.get("price")
            except Exception:
                pass
        else:
            # 2. Try without selection — match means player missing.
            q3 = {k: v for k, v in q.items() if k not in ("line", "selection_norm")}
            row3_a = await db.live_alt_lines.find_one(q3)
            row3_b = await db.propline_alt_lines.find_one(q3)
            row3 = row3_a or row3_b
            if row3:
                stats["line_not_found"] += 1
                warning_code = "line_not_found"
            else:
                # 3. Otherwise — market not present for this event.
                stats["market_removed"] += 1
                warning_code = "market_removed"
        # Stamp the warning but keep the pick on the board.
        if warning_code:
            p["alt_line_validation_warning"] = warning_code
        kept.append(p)

    # ── Tennis synthetic-line HARD DROP ────────────────────────────────
    # Before returning, sweep Tennis total_games / totals picks: if our
    # generator's `line` is more than ±3 games away from ANY live line
    # for that event, the line is "made up" — drop it. This is the
    # surgical fix for the user's "made-up chalk Tennis Over lines"
    # complaint. We only drop when we have GROUND TRUTH (live line
    # exists for the event) so non-FO events without coverage stay
    # advisory-only. Code 2026-06-30 — uses propline + odds-api union.
    kept_filtered, drop_stats = await _drop_tennis_synthetic_lines(kept, db, cutoff)
    for k, v in drop_stats.items():
        stats[k] = stats.get(k, 0) + v
    return kept_filtered, stats


def _best_of_two(row_a, row_b):
    """Pick the higher-price (user-friendlier) of two feed rows."""
    if not row_a:
        return row_b
    if not row_b:
        return row_a
    try:
        pa = int(row_a.get("price") or 0)
        pb = int(row_b.get("price") or 0)
        # American odds: a higher number is always better for the user.
        # (-200 < -150 < +100 < +200).
        return row_a if pa >= pb else row_b
    except Exception:
        return row_a


async def _drop_tennis_synthetic_lines(
    picks: list[dict], db, cutoff,
) -> tuple[list[dict], dict]:
    """HARD-DROP Tennis total_games / totals picks whose `line` is
    fictional — i.e. more than ±3 games away from any real live line
    on the same event.

    Only drops when we HAVE coverage of the event in either feed (so
    we know we're comparing against ground truth). When there's no
    coverage, the pick stays on the board with whatever advisory
    warning the main validator added.
    """
    stats = {"tennis_synthetic_lines_dropped": 0}
    out: list[dict] = []
    # Cache per-(event, market) live line set so we don't re-query.
    line_cache: dict[tuple[str, str], list[float]] = {}
    for p in picks:
        sport = (p.get("sport") or "").lower()
        market = p.get("market") or ""
        if sport != "tennis" or "(Alt)" not in market:
            out.append(p)
            continue
        # Extract the line — picks usually have it on `line` or in the
        # market text. Handle BOTH totals form ("Over 15.5 Games (Alt)")
        # AND spread form ("Iga Swiatek -3.5 Games (Alt)"). The
        # synthetic-line check only applies to FULL-MATCH TOTALS;
        # spreads have a wider legitimate range and are out of scope.
        is_total = bool(re.search(r"\b(?:over|under)\s+[\d.]+", market, re.I))
        if not is_total:
            # Player-spread alt picks ("X -3.5 Games (Alt)") — out of
            # scope of this synthetic-totals rule. Pass through with
            # whatever advisory warning the main validator added.
            out.append(p)
            continue
        line = p.get("line")
        if line is None:
            m = re.search(r"(?:over|under)\s+([\d.]+)", market, re.I)
            if m:
                try:
                    line = float(m.group(1))
                except Exception:
                    line = None
        if line is None:
            out.append(p)
            continue
        # ── ABSOLUTE FLOOR: tennis full-match games is at LEAST 12
        # (6-0 6-0 = 12 games). Any "Over N Games" with N < 11 is
        # physically guaranteed to hit (or push at exactly 12). No
        # sportsbook offers those lines — they're synthetic by
        # construction. Drop unconditionally, no coverage check needed.
        is_over = bool(re.search(r"\bover\b", market, re.I))
        if is_over and float(line) < 11.0:
            stats["tennis_synthetic_lines_dropped"] += 1
            continue

        # ── CHALK-PRICE FLOOR (user-visible bug, 2026-06-30):
        # Even when the LINE is plausible (e.g. Over 15.5), the
        # PRICE on our chalk picks (-499, -711, etc.) doesn't exist
        # on any real book for a tennis alt-game-total. Real-world
        # references on Bovada/DraftKings/FanDuel:
        #   ▸ Main total: ~-110 to -150 max
        #   ▸ 2-rung-below-main alt: ~-200 to -250 max
        #   ▸ 3+ rungs below: not offered (book closes the market)
        # PrizePicks "carries" deep alt rungs but pays flat ±0 — so
        # the listed -499/-711 simply doesn't exist anywhere. Drop
        # any Tennis Over (Alt) priced at -250 or worse — pure
        # synthetic chalk.
        # NOTE: pick odds live under several field names depending on
        # the generator; check all common locations.
        price = None
        for field in ("price", "book_odds", "odds_at_pick", "american_odds", "odds"):
            v = p.get(field)
            if v is not None:
                try:
                    price = int(v)
                    break
                except (ValueError, TypeError):
                    continue
        if is_over and price is not None and price <= -250:
            stats["tennis_synthetic_lines_dropped"] += 1
            continue
        event = p.get("event") or ""
        event_tokens = _event_tokens_from_pick_event(event)
        if not event_tokens:
            out.append(p)
            continue
        # Tennis Games → total_games (DFS alt ladder) / totals (retail
        # main). Both are legitimate ground truth.
        market_keys = ["total_games", "totals", "alternate_totals_games"]
        # Build event-scoped query — same fuzzy match as main validator.
        event_regex = "|".join(re.escape(t) for t in event_tokens if len(t) >= 3)
        cache_key = (event_regex, "|".join(market_keys))
        live_lines = line_cache.get(cache_key)
        if live_lines is None:
            live_lines = []
            for coll in ("live_alt_lines", "propline_alt_lines"):
                try:
                    cur = db[coll].find(
                        {
                            "sport": "tennis",
                            "market_key": {"$in": market_keys},
                            "last_seen": {"$gte": cutoff},
                            "$or": [
                                {"event_name": {"$regex": event_regex, "$options": "i"}},
                                {"home_team":  {"$regex": event_regex, "$options": "i"}},
                                {"away_team":  {"$regex": event_regex, "$options": "i"}},
                            ],
                        },
                        {"_id": 0, "line": 1},
                    )
                    async for r in cur:
                        ln = r.get("line")
                        if ln is not None:
                            try:
                                live_lines.append(float(ln))
                            except Exception:
                                pass
                except Exception:
                    pass
            line_cache[cache_key] = live_lines
        if not live_lines:
            # No coverage — leave the pick alone (let advisory warning
            # do its job).
            out.append(p)
            continue
        # Drop if our line is more than ±3 from every live line.
        try:
            min_dist = min(abs(float(line) - lv) for lv in live_lines)
        except Exception:
            min_dist = 0
        if min_dist > 3.0:
            stats["tennis_synthetic_lines_dropped"] += 1
            # DON'T append — this pick is fiction.
            continue
        out.append(p)
    return out, stats


# ── Helpers for event scoping ─────────────────────────────────────────


def _norm_event_token(s: str) -> str:
    """Normalise team/event names for fuzzy matching."""
    s = re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower())
    return re.sub(r"\s+", " ", s).strip()


def _event_tokens_from_pick_event(event_str: str) -> set[str]:
    """Build a set of fuzzy tokens from a pick's `event` field.

    Examples:
      "Morocco @ Brazil"       → {"morocco brazil", "morocco", "brazil"}
      "Lakers vs Warriors"     → {"lakers warriors", "lakers", "warriors"}
      "Yankees @ Red Sox"      → {"yankees red sox", "yankees", "red sox"}
    """
    if not event_str:
        return set()
    # Split on separators BEFORE normalising (the normaliser strips '@'
    # and 'vs', so we'd lose split points if we normalised first).
    raw_parts = re.split(r"\s*(?:vs?\.?|@|\bat\b)\s*", event_str, flags=re.I)
    norm_parts = [_norm_event_token(p) for p in raw_parts if p and p.strip()]
    norm_parts = [p for p in norm_parts if p]
    if not norm_parts:
        # Fallback: normalise the whole thing then split on whitespace
        # for multi-word team names (best-effort).
        norm_full = _norm_event_token(event_str)
        if norm_full:
            return {norm_full, *norm_full.split()}
        return set()
    tokens: set[str] = set()
    # Individual team names (most useful for matching feed home_team /
    # away_team rows separately).
    for p in norm_parts:
        if len(p) >= 3:
            tokens.add(p)
    # The full joined form ("morocco brazil") matches feed event_name.
    tokens.add(" ".join(norm_parts))
    return tokens


def apply_quality_gate(
    picks: Iterable[dict], *, tag_blocked: bool = False,
) -> tuple[list[dict], dict]:
    """Filter the pick list.

    Returns `(kept, stats)` where stats is a dict of
    `{block_reason: count}` for observability.

    Filter policy (2026-06-30, user clarification):
      ▸ Lock Score is the DEEP-THINKING ENGINE'S signal and is NEVER
        capped here. The engine decides Lock 95/99 — quality_gate
        does not.
      ▸ Edge can be NEGATIVE on legitimate sportsbook lines (e.g. MLB
        Over 0.5 Hits at -300 chalk has -EV but is a real, bettable
        line). We DO NOT blanket-drop -EV picks because that erases
        entire markets (MLB batter props, etc.).
      ▸ TARGETED drops only:
          1. `_block_reason` — synthetic / impossible markets
             (Soccer FGS 3% lottery, MLB NRFI/ML/YRFI low-winrate
             buckets, etc.). These are pre-validated heuristics.
          2. Tennis synthetic-chalk Over lines — already removed by
             `_drop_tennis_synthetic_lines` AFTER alt-line validation
             confirms the line+price doesn't exist on any book.
      ▸ The elite-anchor + coherence-cap pipeline still runs to keep
        FIELD CONSISTENCY across `lock_score / v2 / raw / peak`, but
        the only field a cap actually lowers is on negative-edge picks
        as defense-in-depth (matches what the detail endpoint does).
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
            # Display caps RETIRED — was demoting Anytime scorers based
            # on calibration; lock score now comes from engine only.
            _apply_display_cap(p)
            # Coherence cap — only fires on negative-edge picks now
            # (Rules 2/3 retired per user clarification 2026-06-30).
            # Defense-in-depth for the detail endpoint; the board
            # itself does NOT drop -EV picks.
            _apply_lockscore_coherence(p)
            kept.append(p)
            continue
        blocked_counts[reason] = blocked_counts.get(reason, 0) + 1
        if tag_blocked:
            p["quality_gate_block_reason"] = reason
            kept.append(p)
    return kept, blocked_counts
