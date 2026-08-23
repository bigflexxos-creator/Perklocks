"""
Market-Specific Evidence Profiles
==================================

The **contract layer** that maps every possible (sport, market) combination
to an ordered list of evidence keys with per-market importance weights.

**Why this exists**
-------------------
The historical "Why This Pick" panel showed generic bullets: an MLB
Home-Run pick surfaced the same pitcher K/9 line as a Pitcher-Strikeouts
pick, and an Outs-Recorded pick showed strikeout stats it shouldn't
care about. Users saw statistically-irrelevant reasons and lost trust.

This module owns the FIXED, market-specific ranking so the panel only
shows evidence that actually predicts the outcome of THAT market.

**Contract**
------------
1. `classify_market(pick)` → MarketFamily
2. `PROFILES[MarketFamily]` → ordered list of `EvidenceKey`
3. `select_top_evidence(pick, bullets, max_n)` → ranked short list

Every profile is a **declarative list** — no logic, no if/elif. Add a
new market by adding a new MarketFamily + a new PROFILES entry. Done.

**Data reality check**
----------------------
Some keys in the profiles reference stats we don't yet ingest
(Statcast barrel %, hard-hit %, potential assists, chase rate, etc.).
Those keys will simply not match any bullet and will be silently
skipped — the profile still ranks the bullets we DO have correctly.
When new data sources land, adding a bullet with that key surfaces
automatically. No code change needed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Optional


# ── Market families ──────────────────────────────────────────────────
class MarketFamily(str, Enum):
    """Every distinct market family we build a Why-This-Pick for.

    Naming convention: `<SPORT>_<MARKET>`. Team-level markets use the
    market name; player prop markets use the stat name. Alt vs main is
    handled inside the profile, not the family (an Alt Hits pick has
    the same evidence needs as a Main Hits pick).
    """
    # ── MLB ──
    MLB_HR = "MLB_HR"                # Batter home runs (0.5, 1.5, Anytime)
    MLB_HITS = "MLB_HITS"            # Batter hits (0.5, 1.5, 2.5)
    MLB_RBI = "MLB_RBI"              # Batter RBI
    MLB_TB = "MLB_TB"                # Batter total bases
    MLB_HRR = "MLB_HRR"              # Hits + Runs + RBI
    MLB_OUTS = "MLB_OUTS"            # Pitcher outs recorded / IP
    MLB_KS = "MLB_KS"                # Pitcher strikeouts
    MLB_ER = "MLB_ER"                # Pitcher earned runs
    MLB_HA = "MLB_HA"                # Pitcher hits allowed
    MLB_ML = "MLB_ML"                # Moneyline
    MLB_RL = "MLB_RL"                # Run line
    MLB_TOTAL = "MLB_TOTAL"          # Total runs / team total
    MLB_NRFI = "MLB_NRFI"            # NRFI / YRFI

    # ── NBA / WNBA ──
    NBA_POINTS = "NBA_POINTS"
    NBA_REB = "NBA_REB"
    NBA_AST = "NBA_AST"
    NBA_THREES = "NBA_THREES"
    NBA_BLK = "NBA_BLK"
    NBA_STL = "NBA_STL"
    NBA_PRA = "NBA_PRA"              # Pts + Reb + Ast combo
    NBA_ML = "NBA_ML"
    NBA_SPREAD = "NBA_SPREAD"
    NBA_TOTAL = "NBA_TOTAL"

    # ── NFL / CFB ──
    NFL_PASS_YDS = "NFL_PASS_YDS"
    NFL_PASS_TD = "NFL_PASS_TD"
    NFL_RUSH_YDS = "NFL_RUSH_YDS"
    NFL_REC = "NFL_REC"
    NFL_REC_YDS = "NFL_REC_YDS"
    NFL_ML = "NFL_ML"
    NFL_SPREAD = "NFL_SPREAD"
    NFL_TOTAL = "NFL_TOTAL"

    # ── Soccer ──
    SOC_GOALSCORER = "SOC_GOALSCORER"    # Anytime / First / Score-or-Assist
    SOC_ASSIST = "SOC_ASSIST"
    SOC_SHOTS = "SOC_SHOTS"
    SOC_ML = "SOC_ML"
    SOC_DOUBLE_CHANCE = "SOC_DOUBLE_CHANCE"
    SOC_BTTS = "SOC_BTTS"
    SOC_TOTAL = "SOC_TOTAL"

    # ── Tennis ──
    TEN_MATCH = "TEN_MATCH"          # Match winner / moneyline
    TEN_GAMES = "TEN_GAMES"          # Total games / game handicap
    TEN_SETS = "TEN_SETS"            # Set handicap / correct score

    # ── UFC ──
    UFC_ML = "UFC_ML"

    # ── Fallback ──
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class EvidenceKey:
    """A single evidence slot in a market profile.

    `regexes` is a list of case-insensitive substrings/regex fragments
    used to *match* a candidate bullet. Bullets are matched by their
    text (feature name, reason, or the whole bullet string). First
    match wins — `weight` becomes the ranking score.

    `label` is used only for debug/audit. `desc` is a one-liner
    describing what this evidence key represents, useful for docs
    and future ingestion planning.
    """
    label: str            # human-readable key, e.g. "pitcher_hr_rate"
    weight: float         # 0..1 importance in THIS market
    regexes: tuple[str, ...] = field(default_factory=tuple)
    desc: str = ""


# ── Cross-market blocklists ──────────────────────────────────────────
# Bullets containing ANY of these substrings for a given market family
# are dropped BEFORE ranking — they belong to a different stat.  This
# is what prevents a Pitcher-Outs card from surfacing "avg 6.8 K"
# leftovers when the same pitcher's card was previously enriched as a
# Strikeouts pick.
_CROSS_MARKET_BLOCK: dict[str, tuple[str, ...]] = {
    "MLB_OUTS":    ("k's / start", "k's/start", "k avg", "k's cushion",
                    "strikeout rate", "strikeout form", "k/9",
                    "averaging 6.8 k", "averaging \\d+.\\d+ k"),
    "MLB_KS":      ("outs / start", "ip / start", "walks / start",
                    "er / start", "hits / start", "ha / start"),
    "MLB_ER":      ("k's / start", "k avg", "outs / start", "walks / start"),
    "MLB_HA":      ("k's / start", "k avg", "outs / start", "walks / start"),
    # Batter markets — reject pitcher-stat leftovers if a batter card
    # was ever enriched as a pitcher (rare but has happened via mis-map).
    "MLB_HR":      ("k's / start", "outs / start", "ip / start"),
    "MLB_HITS":    ("k's / start", "outs / start", "ip / start"),
    "MLB_RBI":     ("k's / start", "outs / start", "ip / start"),
    "MLB_TB":      ("k's / start", "outs / start", "ip / start"),
    "MLB_HRR":     ("k's / start", "outs / start", "ip / start"),
}


# ── Sport / market classifier ─────────────────────────────────────────
_MLB_PITCHER_MARKETS = (
    "strikeouts", "k's", " ks ", " k ", "pitcher_ks",
    "outs recorded", "outs allowed", "pitching outs",
    "walks allowed",
    "earned runs",
    "hits allowed",
)


def _norm_market(market: str) -> str:
    return re.sub(r"\s+", " ", (market or "").strip().lower())


def classify_market(pick: dict) -> MarketFamily:
    """Return the MarketFamily for a pick. Best-effort; falls back to
    UNKNOWN so downstream code always has a valid enum to key on.
    """
    sport = (pick.get("sport") or "").strip().lower()
    market = _norm_market(pick.get("market") or "")

    # ── 2026-08-23 CHEAP SURGICAL — canonical market_key first ──
    # Fragile display-text matching (e.g. requiring "NBA" in the
    # market string, or letting a Soccer "Anytime Assist" leak into
    # the NBA_AST branch) is superseded by sport + canonical market
    # key.  The upstream ingest already stamps ``provider_market_key``
    # from The Odds API.  When available we use it verbatim and
    # skip the substring search entirely.  All previously supported
    # families (NFL / MLB / Tennis) are preserved below.
    _mk_canonical = str(
        pick.get("provider_market_key")
        or pick.get("market_key")
        or ""
    ).strip().lower()
    _canonical_soccer = {
        "player_goal_scorer_anytime":  MarketFamily.SOC_GOALSCORER,
        "player_first_goal_scorer":    MarketFamily.SOC_GOALSCORER,
        "player_to_score_or_assist":   MarketFamily.SOC_GOALSCORER,
        "player_anytime_assist":       MarketFamily.SOC_ASSIST,
        "anytime_assist":              MarketFamily.SOC_ASSIST,
        "both_teams_to_score":         MarketFamily.SOC_BTTS,
        "btts":                        MarketFamily.SOC_BTTS,
        "double_chance":               MarketFamily.SOC_DOUBLE_CHANCE,
        "totals":                      MarketFamily.SOC_TOTAL,
        "alternate_totals":            MarketFamily.SOC_TOTAL,
        "h2h":                         MarketFamily.SOC_ML,
        "moneyline":                   MarketFamily.SOC_ML,
    }
    _canonical_nba = {
        "player_points":               MarketFamily.NBA_POINTS,
        "player_points_alternate":     MarketFamily.NBA_POINTS,
        "player_rebounds":             MarketFamily.NBA_REB,
        "player_rebounds_alternate":   MarketFamily.NBA_REB,
        "player_assists":              MarketFamily.NBA_AST,
        "player_assists_alternate":    MarketFamily.NBA_AST,
        "player_threes":               MarketFamily.NBA_THREES,
        "player_threes_alternate":     MarketFamily.NBA_THREES,
        "player_blocks":               MarketFamily.NBA_BLK,
        "player_steals":               MarketFamily.NBA_STL,
        "player_points_rebounds_assists":  MarketFamily.NBA_PRA,
        "player_points_rebounds_assists_alternate": MarketFamily.NBA_PRA,
    }
    if sport == "soccer" and _mk_canonical in _canonical_soccer:
        return _canonical_soccer[_mk_canonical]
    if sport in ("nba", "wnba") and _mk_canonical in _canonical_nba:
        return _canonical_nba[_mk_canonical]
    # NB — existing NFL / MLB / Tennis display-text paths remain the
    # source of truth for those sports (they already work correctly).

    # ── MLB ──
    if sport == "mlb":
        # PITCHER PROPS first (they contain "outs" / "strikeout" tokens)
        if "strikeout" in market or "k's" in market or " ks " in market or market.endswith(" ks"):
            return MarketFamily.MLB_KS
        if "outs recorded" in market or "outs allowed" in market or "pitching outs" in market:
            return MarketFamily.MLB_OUTS
        if "earned runs" in market:
            return MarketFamily.MLB_ER
        if "hits allowed" in market:
            return MarketFamily.MLB_HA
        # BATTER PROPS
        if "home run" in market or "hr " in market or market.endswith(" hr"):
            return MarketFamily.MLB_HR
        if "hits + runs + rbi" in market or "h+r+rbi" in market:
            return MarketFamily.MLB_HRR
        if "total bases" in market or "tb " in market:
            return MarketFamily.MLB_TB
        if "rbi" in market:
            return MarketFamily.MLB_RBI
        if "hit" in market:
            return MarketFamily.MLB_HITS
        # TEAM MARKETS
        if "nrfi" in market or "yrfi" in market or "1st inning" in market or "first inning" in market:
            return MarketFamily.MLB_NRFI
        if "run line" in market or "runline" in market or " -1.5" in market or " +1.5" in market:
            return MarketFamily.MLB_RL
        if "total" in market or "over" in market or "under" in market:
            return MarketFamily.MLB_TOTAL
        if "moneyline" in market or " ml" in market:
            return MarketFamily.MLB_ML

    # ── NBA / WNBA ──
    if sport in ("nba", "wnba"):
        if "3-pointer" in market or "threes" in market or "made 3" in market or "3pt" in market:
            return MarketFamily.NBA_THREES
        if "pts+reb+ast" in market or "p+r+a" in market or "pra" in market:
            return MarketFamily.NBA_PRA
        if "rebound" in market:
            return MarketFamily.NBA_REB
        if "assist" in market:
            return MarketFamily.NBA_AST
        if "block" in market:
            return MarketFamily.NBA_BLK
        if "steal" in market:
            return MarketFamily.NBA_STL
        if "points" in market or " pts" in market:
            return MarketFamily.NBA_POINTS
        if "spread" in market:
            return MarketFamily.NBA_SPREAD
        if "total" in market or "over" in market or "under" in market:
            return MarketFamily.NBA_TOTAL
        if "moneyline" in market:
            return MarketFamily.NBA_ML

    # ── NFL / CFB ──
    if sport in ("nfl", "cfb"):
        if "passing yards" in market or "pass yds" in market:
            return MarketFamily.NFL_PASS_YDS
        if "passing td" in market or "pass td" in market:
            return MarketFamily.NFL_PASS_TD
        if "rushing yards" in market or "rush yds" in market:
            return MarketFamily.NFL_RUSH_YDS
        if "receiving yards" in market or "rec yds" in market:
            return MarketFamily.NFL_REC_YDS
        if "receptions" in market or "receiving" in market:
            return MarketFamily.NFL_REC
        if "spread" in market:
            return MarketFamily.NFL_SPREAD
        if "total" in market:
            return MarketFamily.NFL_TOTAL
        if "moneyline" in market:
            return MarketFamily.NFL_ML

    # ── Soccer ──
    if sport == "soccer":
        if ("goal scorer" in market or "goalscorer" in market
                or "anytime scorer" in market or "first scorer" in market
                or "score or assist" in market or "shots on goal" in market and False):
            return MarketFamily.SOC_GOALSCORER
        if "assist" in market and "score or assist" not in market:
            return MarketFamily.SOC_ASSIST
        if "shots on" in market or "shots " in market:
            return MarketFamily.SOC_SHOTS
        if "btts" in market or "both teams to score" in market:
            return MarketFamily.SOC_BTTS
        if "double chance" in market or "win or draw" in market:
            return MarketFamily.SOC_DOUBLE_CHANCE
        if "total" in market or "over" in market or "under" in market:
            return MarketFamily.SOC_TOTAL
        if "moneyline" in market or " ml" in market:
            return MarketFamily.SOC_ML

    # ── Tennis ──
    if sport == "tennis":
        if "set" in market and ("handicap" in market or "correct score" in market):
            return MarketFamily.TEN_SETS
        if "games" in market or "game handicap" in market or "game spread" in market:
            return MarketFamily.TEN_GAMES
        # Match winner / moneyline
        return MarketFamily.TEN_MATCH

    # ── UFC ──
    if sport == "ufc":
        return MarketFamily.UFC_ML

    return MarketFamily.UNKNOWN


# ── The Profiles ──────────────────────────────────────────────────────
#
# Each list is ORDERED by predictive importance (highest first).
# `weight` is the raw importance in [0, 1] used for ranking.
# `regexes` are the substring patterns we match against candidate
# bullets (case-insensitive). Match short and specific — the first key
# whose regex matches a bullet claims that bullet.
#
# When adding stats we don't currently have (e.g. barrel %), still
# include them here — they'll auto-surface the day the ingestion lands.
PROFILES: dict[MarketFamily, tuple[EvidenceKey, ...]] = {
    # ─────────── MLB batter props ───────────
    MarketFamily.MLB_HR: (
        EvidenceKey("pitcher_hr_allowed_rate", 0.95, ("hr/9", "hr allowed", "homers allowed", "pitcher hr rate"),
                    desc="Starting pitcher's HR-allowed rate"),
        EvidenceKey("batter_iso", 0.90, ("iso", "isolated power"),
                    desc="Isolated power (extra-base %)"),
        EvidenceKey("batter_barrel", 0.88, ("barrel", "barrel%", "barrel rate"),
                    desc="Barrel % — data source TBD"),
        EvidenceKey("batter_hard_hit", 0.85, ("hard-hit", "hard hit", "exit velo"),
                    desc="Hard-hit % / exit velocity"),
        EvidenceKey("handedness_matchup", 0.80, ("hp split", "vs lhp", "vs rhp", "handedness")),
        EvidenceKey("park_factor", 0.75, ("park factor", "venue factor", "park:")),
        EvidenceKey("weather", 0.70, ("weather", "wind", "temp")),
        EvidenceKey("bvp_history", 0.65, ("h2h history", "career pa", "bvp", "vs pitcher history")),
        EvidenceKey("batter_hr_rate", 0.62, ("hr/pa", "homer rate", "hr per")),
        EvidenceKey("batter_ops", 0.55, ("ops",)),
        EvidenceKey("batter_recent_form", 0.50, ("recent form", "last 10", "l10", "l5")),
        EvidenceKey("lineup_pos", 0.45, ("batting order", "lineup", "batting position")),
        EvidenceKey("market_edge", 0.30, ("edge", "closing line", "clv")),
    ),
    MarketFamily.MLB_HITS: (
        EvidenceKey("batter_vs_hand_avg", 0.92, ("hp split", "vs lhp", "vs rhp", "avg vs")),
        EvidenceKey("batter_contact_rate", 0.88, ("contact%", "contact rate", "swstr")),
        EvidenceKey("batter_avg_rolling", 0.85, ("rolling avg", "avg over last", "batting avg")),
        EvidenceKey("batter_k_rate", 0.78, ("k%", "strikeout rate", "k rate")),
        EvidenceKey("lineup_pos", 0.72, ("batting order", "lineup", "batting position")),
        EvidenceKey("batter_recent_form", 0.68, ("recent form", "last 10", "l10", "l5")),
        EvidenceKey("pitcher_hits_allowed", 0.60, ("hits allowed", "opp avg")),
        EvidenceKey("bvp_history", 0.55, ("h2h", "career pa", "bvp")),
        EvidenceKey("market_edge", 0.30, ("edge", "closing line")),
    ),
    MarketFamily.MLB_RBI: (
        EvidenceKey("lineup_pos", 0.90, ("batting order", "lineup", "3-hole", "cleanup", "batting position")),
        EvidenceKey("team_run_projection", 0.85, ("team total", "team projection", "run projection")),
        EvidenceKey("runners_on_opportunity", 0.82, ("runners on", "rbi opp")),
        EvidenceKey("pitcher_matchup", 0.75, ("pitcher matchup", "opp starter", "pitcher hand")),
        EvidenceKey("batter_iso", 0.70, ("iso", "isolated power")),
        EvidenceKey("batter_recent_form", 0.65, ("recent form", "last 10", "l10")),
        EvidenceKey("batter_ops", 0.55, ("ops",)),
        EvidenceKey("market_edge", 0.30, ("edge", "closing line")),
    ),
    MarketFamily.MLB_TB: (
        EvidenceKey("batter_iso", 0.90, ("iso", "isolated power")),
        EvidenceKey("batter_slg", 0.85, ("slg", "slugging")),
        EvidenceKey("pitcher_matchup", 0.80, ("pitcher matchup", "opp starter")),
        EvidenceKey("handedness_matchup", 0.78, ("hp split", "vs lhp", "vs rhp")),
        EvidenceKey("park_factor", 0.70, ("park factor", "venue factor")),
        EvidenceKey("batter_recent_form", 0.60, ("recent form", "last 10", "l10")),
        EvidenceKey("market_edge", 0.30, ("edge", "closing line")),
    ),
    MarketFamily.MLB_HRR: (
        EvidenceKey("batter_ops", 0.85, ("ops",)),
        EvidenceKey("lineup_pos", 0.82, ("batting order", "lineup")),
        EvidenceKey("team_run_projection", 0.80, ("team total", "team projection")),
        EvidenceKey("batter_avg_rolling", 0.75, ("rolling avg", "batting avg")),
        EvidenceKey("batter_iso", 0.70, ("iso", "isolated power")),
        EvidenceKey("pitcher_matchup", 0.65, ("pitcher matchup", "opp starter")),
        EvidenceKey("market_edge", 0.30, ("edge", "closing line")),
    ),
    # ─────────── MLB pitcher props ───────────
    MarketFamily.MLB_OUTS: (
        EvidenceKey("innings_per_start", 0.95,
                    ("ip/gs", "innings per start", "avg ip",
                     "outs / start", "outs/start", "outs recorded",
                     "ip / start", " ip)", "avg outs")),
        EvidenceKey("recent_workload", 0.90,
                    ("last 5", "l5 starts", "last 10 starts", "last-5 workload",
                     "recent form", "l10 form", "prior starts")),
        EvidenceKey("pitch_count", 0.85, ("pitch count", "pitches per")),
        EvidenceKey("quality_start_rate", 0.80, ("quality start", "qs rate", "qs%")),
        EvidenceKey("pull_tendency", 0.72, ("manager pull", "hook", "quick hook")),
        EvidenceKey("opponent_ops", 0.65, ("opponent ops", "opp team ops", "opposing")),
        EvidenceKey("bullpen_fatigue", 0.55, ("bullpen fatigue", "bullpen usage")),
        EvidenceKey("pitcher_bb9", 0.45, ("bb/9", "walk rate")),
        EvidenceKey("market_edge", 0.30, ("edge", "closing line")),
    ),
    MarketFamily.MLB_KS: (
        EvidenceKey("pitcher_k_rate", 0.95, ("k%", "k rate", "k/9", "strikeout rate")),
        EvidenceKey("opponent_k_rate", 0.90, ("opp k%", "opponent k rate", "opp k")),
        EvidenceKey("swinging_strike", 0.85, ("swstr", "swinging strike", "whiff")),
        EvidenceKey("chase_rate", 0.80, ("chase rate", "o-swing", "chase%")),
        EvidenceKey("pitch_arsenal", 0.72, ("arsenal", "pitch mix", "put-away pitch")),
        EvidenceKey("recent_workload", 0.65, ("last 5", "l5 starts", "last-5 workload")),
        EvidenceKey("innings_per_start", 0.55, ("ip/gs", "innings per start")),
        EvidenceKey("pitcher_bb9", 0.30, ("bb/9", "walk rate")),
        EvidenceKey("market_edge", 0.30, ("edge", "closing line")),
    ),
    MarketFamily.MLB_ER: (
        EvidenceKey("pitcher_era", 0.90, ("era", "earned run avg")),
        EvidenceKey("pitcher_whip", 0.85, ("whip",)),
        EvidenceKey("opponent_ops", 0.80, ("opponent ops", "opp team ops")),
        EvidenceKey("recent_workload", 0.72, ("last 5", "l5 starts")),
        EvidenceKey("park_factor", 0.62, ("park factor", "venue")),
        EvidenceKey("weather", 0.55, ("weather", "wind", "temp")),
        EvidenceKey("market_edge", 0.30, ("edge", "closing line")),
    ),
    MarketFamily.MLB_HA: (
        EvidenceKey("pitcher_whip", 0.90, ("whip",)),
        EvidenceKey("opponent_avg", 0.85, ("opp avg", "opponent avg")),
        EvidenceKey("pitcher_h9", 0.80, ("h/9", "hits per 9")),
        EvidenceKey("innings_per_start", 0.65, ("ip/gs", "innings per start")),
        EvidenceKey("market_edge", 0.30, ("edge", "closing line")),
    ),
    # ─────────── MLB team markets ───────────
    MarketFamily.MLB_ML: (
        EvidenceKey("starting_pitcher_edge", 0.90, ("starting pitcher", "sp edge", "pitcher matchup")),
        EvidenceKey("team_form_l10", 0.85, ("last 10", "l10", "recent form")),
        EvidenceKey("bullpen_quality", 0.75, ("bullpen era", "bullpen quality")),
        EvidenceKey("h2h_head_to_head", 0.65, ("h2h", "head-to-head", "season series")),
        EvidenceKey("park_factor", 0.55, ("park factor", "venue")),
        EvidenceKey("market_edge", 0.45, ("edge", "closing line")),
    ),
    MarketFamily.MLB_RL: (
        EvidenceKey("starting_pitcher_edge", 0.85, ("starting pitcher", "sp edge", "pitcher matchup")),
        EvidenceKey("team_run_projection", 0.85, ("team total", "run projection")),
        EvidenceKey("bullpen_quality", 0.75, ("bullpen era", "bullpen quality")),
        EvidenceKey("team_form_l10", 0.70, ("last 10", "l10", "recent form")),
        EvidenceKey("market_edge", 0.45, ("edge", "closing line")),
    ),
    MarketFamily.MLB_TOTAL: (
        EvidenceKey("both_sp_era", 0.90, ("both starters", "combined era", "starters era")),
        EvidenceKey("weather", 0.85, ("weather", "wind", "temp")),
        EvidenceKey("park_factor", 0.80, ("park factor", "venue")),
        EvidenceKey("bullpen_quality", 0.72, ("bullpen era", "bullpen")),
        EvidenceKey("team_form_l10", 0.55, ("last 10", "l10", "recent form")),
        EvidenceKey("market_edge", 0.40, ("edge", "closing line")),
    ),
    MarketFamily.MLB_NRFI: (
        EvidenceKey("both_sp_first_inning_era", 0.90, ("1st inning era", "first inning", "top of the 1st")),
        EvidenceKey("leadoff_ops", 0.80, ("leadoff", "top of order")),
        EvidenceKey("recent_first_inning", 0.72, ("l10 first inning", "first inning trend")),
        EvidenceKey("market_edge", 0.35, ("edge", "closing line")),
    ),
    # ─────────── NBA / WNBA ───────────
    MarketFamily.NBA_POINTS: (
        EvidenceKey("minutes_projection", 0.92, ("minutes projection", "min/g", "minutes")),
        EvidenceKey("usage_rate", 0.90, ("usage rate", "usg%", "usage")),
        EvidenceKey("shot_attempts", 0.85, ("shot attempts", "fga", "attempts per")),
        EvidenceKey("matchup_defense", 0.78, ("opp def", "opponent defense", "def rating")),
        EvidenceKey("role_change", 0.72, ("injury", "role change", "starter out", "load management")),
        EvidenceKey("ppg_season", 0.65, ("ppg", "points per game")),
        EvidenceKey("recent_form", 0.55, ("last 10", "l10", "recent form")),
        EvidenceKey("market_edge", 0.30, ("edge", "closing line")),
    ),
    MarketFamily.NBA_REB: (
        EvidenceKey("rebound_chances", 0.90, ("rebound chances", "reb opp", "board opp")),
        EvidenceKey("minutes_projection", 0.85, ("minutes projection", "min/g", "minutes")),
        EvidenceKey("opp_shot_profile", 0.80, ("opp shot profile", "miss rate", "opp fg%")),
        EvidenceKey("position_matchup", 0.72, ("position matchup", "opposing center", "opposing pf")),
        EvidenceKey("reb_season", 0.62, ("reb season", "reb/g")),
        EvidenceKey("market_edge", 0.30, ("edge", "closing line")),
    ),
    MarketFamily.NBA_AST: (
        EvidenceKey("potential_assists", 0.92, ("potential assist", "pot ast")),
        EvidenceKey("usage_rate", 0.85, ("usage rate", "usg%")),
        EvidenceKey("teammates_available", 0.78, ("teammates", "shooter out", "scorer out")),
        EvidenceKey("pace", 0.72, ("pace", "possessions")),
        EvidenceKey("ast_season", 0.62, ("ast season", "ast/g")),
        EvidenceKey("market_edge", 0.30, ("edge", "closing line")),
    ),
    MarketFamily.NBA_THREES: (
        EvidenceKey("threes_attempted", 0.90, ("3pa", "threes attempted")),
        EvidenceKey("three_pt_pct", 0.85, ("3p%", "three pt%")),
        EvidenceKey("opp_three_defense", 0.75, ("opp 3p%", "three-pt defense")),
        EvidenceKey("minutes_projection", 0.65, ("minutes projection", "min/g")),
        EvidenceKey("market_edge", 0.30, ("edge", "closing line")),
    ),
    MarketFamily.NBA_PRA: (
        EvidenceKey("minutes_projection", 0.92, ("minutes projection", "min/g")),
        EvidenceKey("usage_rate", 0.88, ("usage rate", "usg%")),
        EvidenceKey("pace", 0.72, ("pace", "possessions")),
        EvidenceKey("ppg_season", 0.65, ("ppg",)),
        EvidenceKey("reb_season", 0.55, ("reb/g",)),
        EvidenceKey("ast_season", 0.55, ("ast/g",)),
        EvidenceKey("market_edge", 0.30, ("edge", "closing line")),
    ),
    MarketFamily.NBA_BLK: (
        EvidenceKey("blk_season", 0.85, ("blk", "blocks per")),
        EvidenceKey("minutes_projection", 0.72, ("minutes",)),
        EvidenceKey("opp_shot_profile", 0.65, ("opp shot", "opp 2p%")),
        EvidenceKey("market_edge", 0.30, ("edge",)),
    ),
    MarketFamily.NBA_STL: (
        EvidenceKey("stl_season", 0.75, ("stl", "steals per")),
        EvidenceKey("opp_tov_rate", 0.70, ("opp tov", "opp turnover")),
        EvidenceKey("market_edge", 0.30, ("edge",)),
    ),
    MarketFamily.NBA_ML: (
        EvidenceKey("team_form_l10", 0.85, ("last 10", "l10", "recent form")),
        EvidenceKey("net_rating", 0.80, ("net rating", "netrtg")),
        EvidenceKey("injuries", 0.78, ("injury", "out", "questionable")),
        EvidenceKey("pace_diff", 0.55, ("pace",)),
        EvidenceKey("market_edge", 0.45, ("edge", "closing line")),
    ),
    MarketFamily.NBA_SPREAD: (
        EvidenceKey("net_rating_diff", 0.90, ("net rating", "netrtg")),
        EvidenceKey("team_form_l10", 0.75, ("last 10", "l10")),
        EvidenceKey("injuries", 0.72, ("injury", "out", "questionable")),
        EvidenceKey("home_court", 0.55, ("home", "road")),
        EvidenceKey("market_edge", 0.40, ("edge",)),
    ),
    MarketFamily.NBA_TOTAL: (
        EvidenceKey("pace_projection", 0.90, ("pace", "possessions")),
        EvidenceKey("def_rating_combined", 0.85, ("def rating", "drtg")),
        EvidenceKey("recent_totals", 0.65, ("l10 totals", "team totals",)),
        EvidenceKey("market_edge", 0.40, ("edge",)),
    ),
    # ─────────── NFL / CFB ───────────
    MarketFamily.NFL_PASS_YDS: (
        EvidenceKey("pass_attempts", 0.90, ("pass attempts", "pass att", "attempts")),
        EvidenceKey("opp_pass_defense", 0.88, ("opp pass def", "pass def rank", "pass yards allowed")),
        EvidenceKey("game_script", 0.80, ("game script", "trailing", "shootout")),
        EvidenceKey("pressure_rate", 0.72, ("pressure rate", "sack rate")),
        EvidenceKey("ypg_season", 0.60, ("ypg", "yards per game")),
        EvidenceKey("market_edge", 0.30, ("edge",)),
    ),
    MarketFamily.NFL_PASS_TD: (
        EvidenceKey("td_rate", 0.85, ("td rate", "td%", "passing td rate")),
        EvidenceKey("opp_pass_defense", 0.80, ("opp pass def", "red zone def")),
        EvidenceKey("pass_attempts", 0.72, ("pass attempts",)),
        EvidenceKey("game_script", 0.65, ("game script",)),
        EvidenceKey("market_edge", 0.30, ("edge",)),
    ),
    MarketFamily.NFL_RUSH_YDS: (
        EvidenceKey("carries_projection", 0.92, ("carries", "rush att", "touches")),
        EvidenceKey("ol_grade", 0.82, ("offensive line", "ol grade", "run block")),
        EvidenceKey("opp_run_defense", 0.85, ("opp run def", "run def rank", "rush yards allowed")),
        EvidenceKey("game_script", 0.75, ("game script", "leading", "grind")),
        EvidenceKey("ypc", 0.62, ("ypc", "yards per carry")),
        EvidenceKey("market_edge", 0.30, ("edge",)),
    ),
    MarketFamily.NFL_REC: (
        EvidenceKey("targets", 0.92, ("targets", "target share")),
        EvidenceKey("routes_run", 0.85, ("routes run", "route participation")),
        EvidenceKey("matchup_coverage", 0.80, ("coverage", "shadow", "cornerback matchup")),
        EvidenceKey("qb_volume", 0.72, ("qb attempts", "pass attempts", "qb volume")),
        EvidenceKey("rec_season", 0.55, ("rec/g", "receptions per")),
        EvidenceKey("market_edge", 0.30, ("edge",)),
    ),
    MarketFamily.NFL_REC_YDS: (
        EvidenceKey("targets", 0.90, ("targets",)),
        EvidenceKey("adot", 0.85, ("adot", "aDOT", "avg depth")),
        EvidenceKey("matchup_coverage", 0.80, ("coverage", "shadow")),
        EvidenceKey("qb_volume", 0.72, ("qb volume", "pass attempts")),
        EvidenceKey("yprc", 0.55, ("yards per catch", "ypr")),
        EvidenceKey("market_edge", 0.30, ("edge",)),
    ),
    MarketFamily.NFL_ML: (
        EvidenceKey("team_form", 0.82, ("last 5", "l5", "recent form")),
        EvidenceKey("dvoa", 0.78, ("dvoa", "efficiency")),
        EvidenceKey("injuries", 0.75, ("injury", "starter out")),
        EvidenceKey("market_edge", 0.45, ("edge",)),
    ),
    MarketFamily.NFL_SPREAD: (
        EvidenceKey("dvoa_diff", 0.85, ("dvoa", "efficiency edge")),
        EvidenceKey("injuries", 0.78, ("injury", "starter out")),
        EvidenceKey("home_field", 0.55, ("home", "road")),
        EvidenceKey("market_edge", 0.45, ("edge",)),
    ),
    MarketFamily.NFL_TOTAL: (
        EvidenceKey("combined_ppg", 0.85, ("ppg", "points per game")),
        EvidenceKey("pace_projection", 0.75, ("pace", "plays per game")),
        EvidenceKey("weather", 0.72, ("weather", "wind", "cold")),
        EvidenceKey("market_edge", 0.40, ("edge",)),
    ),
    # ─────────── Soccer ───────────
    MarketFamily.SOC_GOALSCORER: (
        EvidenceKey("shots", 0.92, ("shots/90", "shots per", " shots ", "shots on")),
        EvidenceKey("shots_on_target", 0.90, ("shots on target", "sot")),
        EvidenceKey("xg", 0.90, ("xg", "expected goals")),
        EvidenceKey("minutes_probability", 0.85, ("start", "minutes", "starter")),
        EvidenceKey("penalties", 0.80, ("penalty", "pk taker", "spot kick")),
        EvidenceKey("opp_defense", 0.75, ("opp def", "concede", "goals against", "defensive")),
        EvidenceKey("recent_form", 0.60, ("l5", "l10", "recent form", "in form")),
        EvidenceKey("market_edge", 0.30, ("edge", "closing line")),
    ),
    MarketFamily.SOC_ASSIST: (
        EvidenceKey("key_passes", 0.92, ("key passes", "kp/90")),
        EvidenceKey("chances_created", 0.88, ("chances created", "cc/90", "ncc")),
        EvidenceKey("crosses", 0.80, ("crosses", "cross accuracy")),
        EvidenceKey("set_pieces", 0.75, ("set piece", "corners", "free kick taker")),
        EvidenceKey("minutes_probability", 0.72, ("start", "minutes")),
        EvidenceKey("recent_form", 0.55, ("l5", "l10", "recent form")),
        EvidenceKey("market_edge", 0.30, ("edge",)),
    ),
    MarketFamily.SOC_SHOTS: (
        EvidenceKey("shots", 0.92, ("shots/90", "shots per")),
        EvidenceKey("minutes_probability", 0.78, ("minutes", "start")),
        EvidenceKey("opp_defense", 0.65, ("opp def", "defensive")),
        EvidenceKey("market_edge", 0.30, ("edge",)),
    ),
    MarketFamily.SOC_ML: (
        EvidenceKey("xg_diff", 0.90, ("xg diff", "expected goals diff", "npxg")),
        EvidenceKey("form_5", 0.82, ("l5", "last 5", "recent form")),
        EvidenceKey("home_away_split", 0.72, ("home form", "away form")),
        EvidenceKey("injuries", 0.70, ("injury", "starter out")),
        EvidenceKey("h2h_head_to_head", 0.55, ("h2h", "head-to-head")),
        EvidenceKey("market_edge", 0.45, ("edge",)),
    ),
    MarketFamily.SOC_DOUBLE_CHANCE: (
        EvidenceKey("xg_diff", 0.85, ("xg diff", "expected goals")),
        EvidenceKey("home_away_split", 0.75, ("home form", "away form")),
        EvidenceKey("form_5", 0.72, ("l5", "last 5")),
        EvidenceKey("market_edge", 0.40, ("edge",)),
    ),
    MarketFamily.SOC_BTTS: (
        EvidenceKey("btts_rate_teams", 0.88, ("btts", "both teams scoring", "%_btts")),
        EvidenceKey("goals_scored_conceded", 0.82, ("gpg", "goals per", "concede")),
        EvidenceKey("xg", 0.72, ("xg",)),
        EvidenceKey("market_edge", 0.30, ("edge",)),
    ),
    MarketFamily.SOC_TOTAL: (
        EvidenceKey("combined_xg", 0.90, ("combined xg", "xg total")),
        EvidenceKey("goals_per_game", 0.82, ("goals per game", "gpg")),
        EvidenceKey("pace", 0.65, ("pace",)),
        EvidenceKey("market_edge", 0.35, ("edge",)),
    ),
    # ─────────── Tennis ───────────
    MarketFamily.TEN_MATCH: (
        EvidenceKey("elo", 0.92, ("elo",)),
        EvidenceKey("surface_record", 0.90, ("surface", "hard", "clay", "grass")),
        EvidenceKey("recent_form", 0.80, ("30-day", "l10", "form", "recent form")),
        EvidenceKey("serve_return", 0.75, ("hold", "break", "serve", "return")),
        EvidenceKey("injury_fatigue", 0.72, ("injury", "fatigue", "withdrawal", "retirement")),
        EvidenceKey("h2h_head_to_head", 0.60, ("h2h", "head-to-head")),
        EvidenceKey("market_edge", 0.30, ("edge", "closing line")),
    ),
    MarketFamily.TEN_GAMES: (
        EvidenceKey("hold_pct", 0.92, ("hold %", "service hold", "serve hold")),
        EvidenceKey("break_pct", 0.90, ("break %", "return break")),
        EvidenceKey("tiebreak_history", 0.75, ("tiebreak", "tb record")),
        EvidenceKey("surface_matchup", 0.72, ("surface", "hard", "clay", "grass")),
        EvidenceKey("market_edge", 0.30, ("edge",)),
    ),
    MarketFamily.TEN_SETS: (
        EvidenceKey("elo", 0.85, ("elo",)),
        EvidenceKey("hold_pct", 0.80, ("hold %", "service hold")),
        EvidenceKey("surface_matchup", 0.72, ("surface",)),
        EvidenceKey("market_edge", 0.30, ("edge",)),
    ),
    # ─────────── UFC ───────────
    MarketFamily.UFC_ML: (
        EvidenceKey("elo_or_ranking", 0.85, ("elo", "ranking", "ranked")),
        EvidenceKey("reach_advantage", 0.72, ("reach",)),
        EvidenceKey("finish_rate", 0.68, ("ko rate", "sub rate", "finish rate")),
        EvidenceKey("recent_form", 0.60, ("last 5", "recent form")),
        EvidenceKey("market_edge", 0.40, ("edge",)),
    ),
    # ─────────── Fallback ───────────
    MarketFamily.UNKNOWN: (
        EvidenceKey("recent_form", 0.60, ("recent form", "l10", "l5", "last 10", "last 5")),
        EvidenceKey("matchup", 0.55, ("matchup", "opponent")),
        EvidenceKey("market_edge", 0.40, ("edge", "closing line")),
    ),
}


# ── Selection / ranking API ──────────────────────────────────────────
def _to_text(bullet) -> str:
    """Normalize a bullet (string OR dict) into a single lowercase
    string we can regex against."""
    if isinstance(bullet, str):
        return bullet.lower()
    if isinstance(bullet, dict):
        # Concatenate every stringy field so we match against label +
        # reason + reason_text etc.
        parts = []
        for k in ("reason", "explanation_text", "text", "label", "name"):
            v = bullet.get(k)
            if isinstance(v, str):
                parts.append(v)
        return " ".join(parts).lower()
    return str(bullet).lower()


def match_evidence_key(bullet, keys: Iterable[EvidenceKey]) -> Optional[EvidenceKey]:
    """Return the first key whose regex list matches this bullet, or
    None if no key claims it. Higher-importance keys are listed first
    in the profile, so this naturally prefers the strongest match.
    """
    text = _to_text(bullet)
    if not text:
        return None
    for key in keys:
        for rx in key.regexes:
            # Substring first (fast path); regex fallback if the frag
            # looks like a regex. Keep it cheap — profiles ship 100+
            # keys total.
            if rx.lower() in text:
                return key
    return None


def select_top_evidence(
    pick: dict,
    bullets: list,
    *,
    max_n: int = 5,
    min_weight: float = 0.0,
) -> list:
    """Rank + filter a list of candidate evidence bullets against the
    market profile for this pick. Returns the top ``max_n`` bullets
    ordered by market-specific importance (highest first).

    Bullets that don't match ANY key in the profile are kept at the
    end but only if there's room — this avoids stripping useful
    context (e.g. a bespoke sport_rationale bullet the profile hasn't
    catalogued yet) while still preferring recognised keys.

    Inputs:
      pick     — the mongo pick dict
      bullets  — list[str | dict] candidates from any upstream source
      max_n    — hard cap on returned bullets (default 5, spec 3-5)
      min_weight — drop bullets whose matched key is below this weight
                    (default 0.0 = keep everything)

    Returns:
      list[same-type-as-input] — order = ranked by weight desc, dedup'd
    """
    if not bullets:
        return []
    family = classify_market(pick)
    profile = PROFILES.get(family) or PROFILES[MarketFamily.UNKNOWN]

    # ── Cross-market blocklist ────────────────────────────────────
    # Drop bullets that reference stats belonging to a DIFFERENT market
    # family (e.g. K-averages leaking into an Outs Recorded card). This
    # is the last line of defence when a pick's rationale block was
    # previously enriched under a different market subtype and stale
    # bullets are still sitting there. See `_CROSS_MARKET_BLOCK` at the
    # top of the file for the per-family blacklist.
    block_terms = _CROSS_MARKET_BLOCK.get(family.value, ())
    if block_terms:
        filtered: list = []
        for b in bullets:
            txt = _to_text(b)
            if any(term.lower() in txt for term in block_terms):
                continue
            filtered.append(b)
        bullets = filtered
    if not bullets:
        return []

    matched: list[tuple[float, int, object]] = []
    unmatched: list[tuple[int, object]] = []
    seen_text: set[str] = set()

    for idx, b in enumerate(bullets):
        # De-dupe by leading 60 chars (matches the deep_dive convention).
        key_txt = _to_text(b)[:60]
        if key_txt in seen_text:
            continue
        seen_text.add(key_txt)

        matched_key = match_evidence_key(b, profile)
        if matched_key and matched_key.weight >= min_weight:
            matched.append((matched_key.weight, idx, b))
        else:
            unmatched.append((idx, b))

    # Sort matched by (weight desc, original idx asc so ties are stable)
    matched.sort(key=lambda t: (-t[0], t[1]))
    ranked = [b for _, _, b in matched]

    # Fill remaining slots with unmatched bullets in original order
    # (they may still be useful — sport_rationale writes hand-crafted
    # context we haven't catalogued yet).
    if len(ranked) < max_n:
        ranked.extend(b for _, b in unmatched[: max_n - len(ranked)])

    return ranked[:max_n]


# ── Debug helper (used in tests / admin inspector) ───────────────────
def explain_selection(pick: dict, bullets: list) -> dict:
    """Return the full selection trace: which bullet matched which key
    and its weight. Useful for debugging why a specific bullet was
    dropped or promoted. Never called on the hot path.
    """
    family = classify_market(pick)
    profile = PROFILES.get(family) or PROFILES[MarketFamily.UNKNOWN]
    trace: list[dict] = []
    for b in (bullets or []):
        matched_key = match_evidence_key(b, profile)
        trace.append({
            "bullet": b if isinstance(b, str) else _to_text(b)[:80],
            "matched_key": matched_key.label if matched_key else None,
            "weight": matched_key.weight if matched_key else None,
        })
    return {
        "sport": pick.get("sport"),
        "market": pick.get("market"),
        "family": family.value,
        "profile_keys": [k.label for k in profile],
        "trace": trace,
    }
