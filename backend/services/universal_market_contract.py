"""UniversalMarketContract — one authoritative sport × market registry
========================================================================

PERKLOCKS-MAIN 34 · STEP 2.

Prior state: every generator, validator, alt-line engine, and settler
carried its own hard-coded market vocabulary. That is why the
alt-market keys drifted (`alternate_spreads` vs `alternate_spreads_games`),
NFL/NBA alternate props sometimes fell into the standard-prop filter,
MLB run_line could hit an authority branch that thought it was
"spread", and NHL/UFC could look supported in one registry but
model-unavailable in another.

`UniversalMarketContract` is the single truth. Every generator /
validator / alt / settler MUST consume from it — no more parallel
vocabularies. Existing hard-coded sets should be migrated to
compatibility adapters (`UniversalMarketContract.provider_keys(...)`).

Contract keys:
    sport  ∈ {"MLB","NFL","CFB","NBA","NHL","Soccer","Tennis","UFC"}
    canonical_market_family  (see MARKET_FAMILIES below)

Each entry defines:
    provider_market_keys    real sportsbook / odds-api keys
    aliases                 legacy hardcoded strings that must
                              canonicalize to this entry
    market_class            "game_market" | "player_prop"
    line_type               "standard" | "alternate" | "both"
    selection_schema        "over_under" | "participant" | "yes_no"
                              | "moneyline"
    allowed_sides           tuple of allowed side tokens
    requires_real_line      True → no synthetic sportsbook lines
    model_authority         name of the specialized model (or None)
    exact_threshold         True → each ladder rung priced at its
                              exact line (no reuse of standard-line
                              probability)
    settlement_actuals      required actual fields to grade
    settlement_primary      primary authority (or None)
    settlement_fallbacks    tuple of fallback authorities
    capability_state        one of the 5 canonical states

Capability states:
    ACTIVE                    provider + model + settle all wired
    RESEARCH_ONLY             surface in Lab but never publish
    MODEL_UNAVAILABLE         provider present but no scoring model
    PROVIDER_UNAVAILABLE      sport itself off-season / feed gone
    SETTLEMENT_UNAVAILABLE    scores/actuals not authoritative

CONTRACT INVARIANT (test_perklocks_main_34_step2_universal_market_contract.py):
    No canonical (sport, family) key may exist in TWO states
    simultaneously across any consuming registry.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

# ── Canonical market family enum (extensible) ───────────────────────
class Family:
    # Game markets
    MONEYLINE     = "moneyline"
    RUN_LINE      = "run_line"        # MLB (canonical, not "spread")
    POINT_SPREAD  = "point_spread"    # NFL / NBA / CFB
    GAME_TOTAL    = "game_total"
    BTTS          = "btts"            # Soccer
    DOUBLE_CHANCE = "double_chance"   # Soccer
    HANDICAP      = "handicap"        # Soccer / Tennis game handicap
    # NRFI / YRFI
    NRFI_YRFI     = "nrfi_yrfi"
    # Player props — batter/pitcher/receiver/rusher/scorer/…
    HITTER_HITS       = "hitter_hits"
    HITTER_TOTAL_BASES = "hitter_total_bases"
    HITTER_HR         = "hitter_home_runs"
    HITTER_RBI        = "hitter_rbis"
    PITCHER_STRIKEOUTS = "pitcher_strikeouts"
    PITCHER_OUTS       = "pitcher_outs"
    QB_PASSING_YDS    = "qb_passing_yards"
    QB_PASSING_TDS    = "qb_passing_tds"
    RB_RUSHING_YDS    = "rb_rushing_yards"
    WR_RECEIVING_YDS  = "wr_receiving_yards"
    WR_RECEPTIONS     = "wr_receptions"
    NBA_POINTS        = "nba_points"
    NBA_REBOUNDS      = "nba_rebounds"
    NBA_ASSISTS       = "nba_assists"
    NBA_THREES        = "nba_threes"
    NBA_PRA           = "nba_pra"
    GOALSCORER_ANY    = "goalscorer_anytime"
    GOALSCORER_FIRST  = "goalscorer_first"
    GOALSCORER_SCORE_ASSIST = "goalscorer_score_or_assist"
    TENNIS_MATCH_WIN  = "tennis_match_winner"
    TENNIS_GAME_HANDICAP = "tennis_game_handicap"
    TENNIS_TOTAL_GAMES = "tennis_total_games"


ACTIVE                 = "ACTIVE"
RESEARCH_ONLY          = "RESEARCH_ONLY"
MODEL_UNAVAILABLE      = "MODEL_UNAVAILABLE"
PROVIDER_UNAVAILABLE   = "PROVIDER_UNAVAILABLE"
SETTLEMENT_UNAVAILABLE = "SETTLEMENT_UNAVAILABLE"
_CAPABILITY_STATES = frozenset({
    ACTIVE, RESEARCH_ONLY, MODEL_UNAVAILABLE,
    PROVIDER_UNAVAILABLE, SETTLEMENT_UNAVAILABLE,
})


@dataclass(frozen=True)
class MarketEntry:
    sport: str
    family: str
    provider_market_keys: Tuple[str, ...] = ()
    aliases: Tuple[str, ...] = ()
    market_class: str = "game_market"
    line_type: str = "standard"
    selection_schema: str = "over_under"
    allowed_sides: Tuple[str, ...] = ("over", "under")
    requires_real_line: bool = True
    model_authority: Optional[str] = None
    exact_threshold: bool = True
    settlement_actuals: Tuple[str, ...] = ()
    settlement_primary: Optional[str] = None
    settlement_fallbacks: Tuple[str, ...] = ()
    capability_state: str = ACTIVE


# ── Seed registry — current PerkLocks production surface ────────────
# NOTE: This module is intentionally small at first landing. Consumers
# (sports_engine, alt-line engine, settlement_engine, live_alt_lines)
# should migrate their hardcoded vocabularies to look this up. Once
# every consumer reads from here, all Tennis/NFL alt/NBA/MLB run_line
# drift becomes a single-line fix rather than a five-file coordination.
_REGISTRY: Dict[Tuple[str, str], MarketEntry] = {}


def _add(entry: MarketEntry) -> None:
    key = (entry.sport, entry.family)
    if key in _REGISTRY:
        raise ValueError(f"duplicate UniversalMarketContract entry: {key}")
    if entry.capability_state not in _CAPABILITY_STATES:
        raise ValueError(f"bad capability_state {entry.capability_state!r}")
    _REGISTRY[key] = entry


# ── MLB ─────────────────────────────────────────────────────────────
_add(MarketEntry("MLB", Family.MONEYLINE,
    provider_market_keys=("h2h",),
    aliases=("moneyline", "ml"),
    market_class="game_market", line_type="standard",
    selection_schema="moneyline", allowed_sides=("home", "away"),
    model_authority="mlb_game_model",
    settlement_actuals=("home_score", "away_score"),
    settlement_primary="mlb_statsapi",
    settlement_fallbacks=("espn_scores",),
    capability_state=ACTIVE))
_add(MarketEntry("MLB", Family.RUN_LINE,
    provider_market_keys=("spreads",),  # provider still emits "spreads"
    aliases=("run_line", "runline", "spread"),
    market_class="game_market", line_type="both",
    selection_schema="participant",
    allowed_sides=("home", "away"),
    model_authority="mlb_game_model",
    settlement_actuals=("home_score", "away_score"),
    settlement_primary="mlb_statsapi",
    settlement_fallbacks=("espn_scores",),
    capability_state=ACTIVE))
_add(MarketEntry("MLB", Family.GAME_TOTAL,
    provider_market_keys=("totals",),
    aliases=("total", "game_total"),
    market_class="game_market", line_type="both",
    model_authority="mlb_game_model",
    settlement_actuals=("home_score", "away_score"),
    settlement_primary="mlb_statsapi",
    capability_state=ACTIVE))
_add(MarketEntry("MLB", Family.HITTER_HITS,
    provider_market_keys=("batter_hits", "batter_hits_alternate"),
    aliases=("hits",),
    market_class="player_prop", line_type="both",
    model_authority="mlb_hitter_model",
    settlement_actuals=("player_hits",),
    settlement_primary="mlb_statsapi",
    settlement_fallbacks=("pitchapi",),
    capability_state=ACTIVE))
_add(MarketEntry("MLB", Family.PITCHER_STRIKEOUTS,
    provider_market_keys=("pitcher_strikeouts", "pitcher_strikeouts_alternate"),
    market_class="player_prop", line_type="both",
    model_authority="mlb_pitcher_model",
    settlement_actuals=("player_strikeouts",),
    settlement_primary="mlb_statsapi",
    capability_state=ACTIVE))
# PERKLOCKS-MAIN 35 · FINAL — MLB player-prop coverage closure.
# These families were emitted end-to-end by the MLB pipeline
# (`sport_capability_registry`) but had no canonical UMC entry, so
# cross-registry parity tests reported drift. Declaring them with
# honest capability states — every one has a real MLB hitter model
# path via `services.mlb_hitter_model` and settles via MLB Stats API.
_add(MarketEntry("MLB", Family.HITTER_HR,
    provider_market_keys=("batter_home_runs", "batter_home_runs_alternate"),
    aliases=("home_runs", "hr"),
    market_class="player_prop", line_type="both",
    model_authority="mlb_hitter_model",
    settlement_actuals=("player_home_runs",),
    settlement_primary="mlb_statsapi",
    capability_state=ACTIVE))
_add(MarketEntry("MLB", Family.HITTER_RBI,
    provider_market_keys=("batter_rbis", "batter_rbis_alternate"),
    aliases=("rbi", "rbis"),
    market_class="player_prop", line_type="both",
    model_authority="mlb_hitter_model",
    settlement_actuals=("player_rbis",),
    settlement_primary="mlb_statsapi",
    capability_state=ACTIVE))
_add(MarketEntry("MLB", Family.HITTER_TOTAL_BASES,
    provider_market_keys=("batter_total_bases", "batter_total_bases_alternate"),
    aliases=("total_bases", "tb"),
    market_class="player_prop", line_type="both",
    model_authority="mlb_hitter_model",
    settlement_actuals=("player_total_bases",),
    settlement_primary="mlb_statsapi",
    capability_state=ACTIVE))
_add(MarketEntry("MLB", "hitter_hits_runs_rbis",
    provider_market_keys=("batter_hits_runs_rbis",
                            "batter_hits_runs_rbis_alternate"),
    aliases=("hits_runs_rbis", "hits+runs+rbi"),
    market_class="player_prop", line_type="both",
    model_authority="mlb_hitter_model",
    settlement_actuals=("player_hits", "player_runs", "player_rbis"),
    settlement_primary="mlb_statsapi",
    capability_state=ACTIVE))
_add(MarketEntry("MLB", Family.PITCHER_OUTS,
    provider_market_keys=("pitcher_outs", "pitcher_outs_alternate"),
    aliases=("outs", "outs_recorded"),
    market_class="player_prop", line_type="both",
    model_authority="mlb_pitcher_model",
    settlement_actuals=("player_outs",),
    settlement_primary="mlb_statsapi",
    capability_state=ACTIVE))

# ── NFL ─────────────────────────────────────────────────────────────
_add(MarketEntry("NFL", Family.MONEYLINE,
    provider_market_keys=("h2h",),
    market_class="game_market", selection_schema="moneyline",
    allowed_sides=("home", "away"),
    model_authority="nfl_game_model",
    settlement_actuals=("home_score", "away_score"),
    settlement_primary="espn_scores", capability_state=ACTIVE))
_add(MarketEntry("NFL", Family.POINT_SPREAD,
    provider_market_keys=("spreads",),
    market_class="game_market", line_type="both",
    selection_schema="participant", allowed_sides=("home", "away"),
    model_authority="nfl_game_model",
    settlement_actuals=("home_score", "away_score"),
    settlement_primary="espn_scores", capability_state=ACTIVE))
_add(MarketEntry("NFL", Family.GAME_TOTAL,
    provider_market_keys=("totals",),
    market_class="game_market", line_type="both",
    model_authority="nfl_game_model",
    settlement_actuals=("home_score", "away_score"),
    settlement_primary="espn_scores", capability_state=ACTIVE))
_add(MarketEntry("NFL", Family.WR_RECEIVING_YDS,
    provider_market_keys=("player_reception_yds",
                            "player_reception_yds_alternate"),
    market_class="player_prop", line_type="both",
    model_authority="nfl_receiving_model",
    settlement_actuals=("player_receiving_yards",),
    settlement_primary="espn_boxscore", capability_state=ACTIVE))
_add(MarketEntry("NFL", Family.WR_RECEPTIONS,
    provider_market_keys=("player_receptions",
                            "player_receptions_alternate"),
    market_class="player_prop", line_type="both",
    model_authority="nfl_receptions_model",
    settlement_actuals=("player_receptions",),
    settlement_primary="espn_boxscore", capability_state=ACTIVE))
# PERKLOCKS-MAIN 35 · FINAL — NFL player-prop coverage closure.
# QB / RB / anytime-TD markets emit end-to-end through
# `sport_capability_registry` but had no canonical UMC entry.
_add(MarketEntry("NFL", Family.QB_PASSING_YDS,
    provider_market_keys=("player_pass_yds", "player_pass_yds_alternate"),
    aliases=("passing_yards",),
    market_class="player_prop", line_type="both",
    model_authority="nfl_passing_model",
    settlement_actuals=("player_passing_yards",),
    settlement_primary="espn_boxscore", capability_state=ACTIVE))
_add(MarketEntry("NFL", Family.QB_PASSING_TDS,
    provider_market_keys=("player_pass_tds", "player_pass_tds_alternate"),
    aliases=("passing_tds",),
    market_class="player_prop", line_type="both",
    model_authority="nfl_passing_model",
    settlement_actuals=("player_passing_tds",),
    settlement_primary="espn_boxscore", capability_state=ACTIVE))
_add(MarketEntry("NFL", Family.RB_RUSHING_YDS,
    provider_market_keys=("player_rush_yds", "player_rush_yds_alternate"),
    aliases=("rushing_yards",),
    market_class="player_prop", line_type="both",
    model_authority="nfl_rushing_model",
    settlement_actuals=("player_rushing_yards",),
    settlement_primary="espn_boxscore", capability_state=ACTIVE))
_add(MarketEntry("NFL", "player_anytime_td",
    provider_market_keys=("player_anytime_td",),
    aliases=("anytime_touchdown", "anytime_td"),
    market_class="player_prop", line_type="standard",
    selection_schema="yes_no", allowed_sides=("yes",),
    requires_real_line=False,
    model_authority="nfl_anytime_td_model",
    settlement_actuals=("player_rushing_tds", "player_receiving_tds"),
    settlement_primary="espn_boxscore", capability_state=ACTIVE))

# ── NBA — declared honestly ────────────────────────────────────────
# PERKLOCKS-MAIN 35 · P1-8 — capability alignment.  The
# `sport_capability_registry` declares NBA h2h/spreads/totals as
# MODEL_UNAVAILABLE (provider IS wired via Odds API; no authoritative
# NBA game model has been shipped).  Aligning to MODEL_UNAVAILABLE so
# the two registries no longer disagree.
_add(MarketEntry("NBA", Family.MONEYLINE, provider_market_keys=("h2h",),
    market_class="game_market", selection_schema="moneyline",
    allowed_sides=("home", "away"),
    capability_state=MODEL_UNAVAILABLE))
_add(MarketEntry("NBA", Family.POINT_SPREAD,
    provider_market_keys=("spreads",),
    market_class="game_market", line_type="both",
    selection_schema="participant", allowed_sides=("home", "away"),
    capability_state=MODEL_UNAVAILABLE))
_add(MarketEntry("NBA", Family.GAME_TOTAL,
    provider_market_keys=("totals",),
    market_class="game_market", line_type="both",
    capability_state=MODEL_UNAVAILABLE))
_add(MarketEntry("NBA", Family.NBA_POINTS,
    provider_market_keys=("player_points", "player_points_alternate"),
    market_class="player_prop", line_type="both",
    model_authority="nba_points_model",
    settlement_actuals=("player_points",),
    settlement_primary="espn_boxscore",
    capability_state=MODEL_UNAVAILABLE))
# PERKLOCKS-MAIN 35 · FINAL — NBA player-prop coverage closure.
# Every prop family declared in sport_capability_registry gets a
# canonical UMC entry with honest MODEL_UNAVAILABLE state (no NBA
# props model has been shipped yet).
_add(MarketEntry("NBA", Family.NBA_REBOUNDS,
    provider_market_keys=("player_rebounds", "player_rebounds_alternate"),
    market_class="player_prop", line_type="both",
    settlement_actuals=("player_rebounds",),
    settlement_primary="espn_boxscore",
    capability_state=MODEL_UNAVAILABLE))
_add(MarketEntry("NBA", Family.NBA_ASSISTS,
    provider_market_keys=("player_assists", "player_assists_alternate"),
    market_class="player_prop", line_type="both",
    settlement_actuals=("player_assists",),
    settlement_primary="espn_boxscore",
    capability_state=MODEL_UNAVAILABLE))
_add(MarketEntry("NBA", Family.NBA_THREES,
    provider_market_keys=("player_threes", "player_threes_alternate"),
    market_class="player_prop", line_type="both",
    settlement_actuals=("player_threes",),
    settlement_primary="espn_boxscore",
    capability_state=MODEL_UNAVAILABLE))
_add(MarketEntry("NBA", Family.NBA_PRA,
    provider_market_keys=("player_points_rebounds_assists",
                            "player_points_rebounds_assists_alternate"),
    aliases=("pra",),
    market_class="player_prop", line_type="both",
    settlement_actuals=("player_points", "player_rebounds", "player_assists"),
    settlement_primary="espn_boxscore",
    capability_state=MODEL_UNAVAILABLE))

# ── CFB — provider-supported, no authoritative model yet ────────────
_add(MarketEntry("CFB", Family.MONEYLINE,
    provider_market_keys=("h2h",),
    market_class="game_market", selection_schema="moneyline",
    allowed_sides=("home", "away"),
    model_authority="cfb_game_model",
    settlement_actuals=("home_score", "away_score"),
    settlement_primary="espn_scores", capability_state=ACTIVE))
_add(MarketEntry("CFB", Family.POINT_SPREAD,
    provider_market_keys=("spreads",),
    market_class="game_market", line_type="both",
    selection_schema="participant", allowed_sides=("home", "away"),
    model_authority="cfb_game_model",
    settlement_actuals=("home_score", "away_score"),
    settlement_primary="espn_scores", capability_state=ACTIVE))
_add(MarketEntry("CFB", Family.GAME_TOTAL,
    provider_market_keys=("totals",),
    market_class="game_market", line_type="both",
    model_authority="cfb_game_model",
    settlement_actuals=("home_score", "away_score"),
    settlement_primary="espn_scores", capability_state=ACTIVE))

# ── Tennis — the audited defect surface ────────────────────────────
_add(MarketEntry("Tennis", Family.TENNIS_MATCH_WIN,
    provider_market_keys=("h2h",),
    market_class="game_market", selection_schema="moneyline",
    allowed_sides=("home", "away"),
    model_authority="tennis_match_model",
    settlement_actuals=("match_winner",),
    settlement_primary="tennis_espn", capability_state=ACTIVE))
_add(MarketEntry("Tennis", Family.TENNIS_GAME_HANDICAP,
    # Live provider vocabulary uses `_games` suffix — canonicalise it.
    provider_market_keys=("alternate_spreads_games", "spreads_games"),
    aliases=("alternate_spreads", "spreads", "game_spread"),
    market_class="game_market", line_type="both",
    selection_schema="participant", allowed_sides=("home", "away"),
    model_authority="tennis_match_distribution",
    settlement_actuals=("games_won_home", "games_won_away"),
    settlement_primary="tennis_espn", capability_state=ACTIVE))
_add(MarketEntry("Tennis", Family.TENNIS_TOTAL_GAMES,
    provider_market_keys=("alternate_totals_games", "totals_games"),
    aliases=("alternate_totals", "totals", "game_total"),
    market_class="game_market", line_type="both",
    model_authority="tennis_match_distribution",
    settlement_actuals=("total_games",),
    settlement_primary="tennis_espn", capability_state=ACTIVE))

# ── Soccer — goalscorer surface ────────────────────────────────────
_add(MarketEntry("Soccer", Family.MONEYLINE,
    provider_market_keys=("h2h",),
    market_class="game_market", selection_schema="moneyline",
    allowed_sides=("home", "draw", "away"),
    model_authority="soccer_game_model",
    settlement_actuals=("home_score", "away_score"),
    settlement_primary="sportdb", capability_state=ACTIVE))
_add(MarketEntry("Soccer", Family.GAME_TOTAL,
    provider_market_keys=("totals",),
    market_class="game_market", line_type="both",
    model_authority="soccer_game_model",
    settlement_actuals=("home_score", "away_score"),
    settlement_primary="sportdb", capability_state=ACTIVE))
_add(MarketEntry("Soccer", Family.GOALSCORER_ANY,
    provider_market_keys=("player_goal_scorer_anytime",),
    aliases=("anytime_goalscorer", "goal_scorer_anytime"),
    market_class="player_prop", line_type="standard",
    selection_schema="yes_no", allowed_sides=("yes",),
    requires_real_line=False,
    model_authority="soccer_goalscorer_model",
    settlement_actuals=("player_goal_events",),
    settlement_primary="sportdb",
    settlement_fallbacks=("understat",), capability_state=ACTIVE))
# PERKLOCKS-MAIN 35 · FINAL — Soccer coverage closure.  Anytime /
# First / Last Goalscorer MUST remain distinct canonical markets per
# product requirement.  BTTS and Double Chance are active provider
# markets already flowing through the pipeline.  Shots-on-target and
# Anytime Assist are new SLICE 3 provider surfaces already declared
# in sport_capability_registry.
_add(MarketEntry("Soccer", Family.HANDICAP,
    provider_market_keys=("spreads",),
    aliases=("asian_handicap", "handicap"),
    market_class="game_market", line_type="both",
    selection_schema="participant", allowed_sides=("home", "away"),
    model_authority="soccer_game_model",
    settlement_actuals=("home_score", "away_score"),
    settlement_primary="sportdb", capability_state=ACTIVE))
_add(MarketEntry("Soccer", Family.BTTS,
    provider_market_keys=("btts", "both_teams_to_score"),
    aliases=("both_teams",),
    market_class="game_market", line_type="standard",
    selection_schema="yes_no", allowed_sides=("yes", "no"),
    requires_real_line=False,
    model_authority="soccer_game_model",
    settlement_actuals=("home_score", "away_score"),
    settlement_primary="sportdb", capability_state=ACTIVE))
_add(MarketEntry("Soccer", Family.DOUBLE_CHANCE,
    provider_market_keys=("double_chance",),
    aliases=("dc",),
    market_class="game_market", line_type="standard",
    selection_schema="moneyline",
    allowed_sides=("home_or_draw", "away_or_draw", "home_or_away"),
    requires_real_line=False,
    model_authority="soccer_game_model",
    settlement_actuals=("home_score", "away_score"),
    settlement_primary="sportdb", capability_state=ACTIVE))
_add(MarketEntry("Soccer", Family.GOALSCORER_FIRST,
    provider_market_keys=("player_first_goal_scorer",),
    aliases=("first_goalscorer",),
    market_class="player_prop", line_type="standard",
    selection_schema="yes_no", allowed_sides=("yes",),
    requires_real_line=False,
    settlement_actuals=("first_goal_scorer",),
    settlement_primary="sportdb",
    # Product decision: INTENTIONALLY_UNSUPPORTED for publication.
    # Provider markets exist but no authoritative first-goal model
    # or settler is wired. Kept as a distinct canonical entry so the
    # market cannot silently collapse into GOALSCORER_ANY.
    capability_state=RESEARCH_ONLY))
_add(MarketEntry("Soccer", Family.GOALSCORER_SCORE_ASSIST,
    provider_market_keys=("player_to_score_or_assist",),
    aliases=("score_or_assist", "sga"),
    market_class="player_prop", line_type="standard",
    selection_schema="yes_no", allowed_sides=("yes",),
    requires_real_line=False,
    model_authority="soccer_goalscorer_model",
    settlement_actuals=("player_goal_events", "player_assist_events"),
    settlement_primary="sportdb",
    settlement_fallbacks=("understat",), capability_state=ACTIVE))
_add(MarketEntry("Soccer", "soccer_anytime_assist",
    provider_market_keys=("player_anytime_assist",),
    aliases=("anytime_assist",),
    market_class="player_prop", line_type="standard",
    selection_schema="yes_no", allowed_sides=("yes",),
    requires_real_line=False,
    model_authority="soccer_goalscorer_model",
    settlement_actuals=("player_assist_events",),
    settlement_primary="sportdb", capability_state=ACTIVE))
_add(MarketEntry("Soccer", "soccer_shots",
    provider_market_keys=("player_shots", "player_shots_alternate"),
    aliases=("shots",),
    market_class="player_prop", line_type="both",
    settlement_actuals=("player_shots",),
    settlement_primary="sportdb",
    # SLICE 3 wiring in progress — model not authoritative yet.
    capability_state=MODEL_UNAVAILABLE))
_add(MarketEntry("Soccer", "soccer_shots_on_target",
    provider_market_keys=("player_shots_on_target",
                            "player_shots_on_target_alternate"),
    aliases=("shots_on_target", "sot"),
    market_class="player_prop", line_type="both",
    settlement_actuals=("player_shots_on_target",),
    settlement_primary="sportdb",
    capability_state=MODEL_UNAVAILABLE))

# ── NHL / UFC — honest unavailable states, no fake support ─────────
# PERKLOCKS-MAIN 35 · P1-8 — align to `sport_capability_registry`
# which declares NHL h2h/spreads/totals as MODEL_UNAVAILABLE (provider
# is wired end-to-end; no authoritative independent NHL model exists
# yet). Was declared PROVIDER_UNAVAILABLE here — mismatch fixed.
_add(MarketEntry("NHL", Family.MONEYLINE, provider_market_keys=("h2h",),
    market_class="game_market", selection_schema="moneyline",
    allowed_sides=("home", "away"),
    capability_state=MODEL_UNAVAILABLE))
_add(MarketEntry("NHL", "puck_line",
    provider_market_keys=("spreads",),
    aliases=("puckline",),
    market_class="game_market", line_type="both",
    selection_schema="participant", allowed_sides=("home", "away"),
    capability_state=MODEL_UNAVAILABLE))
_add(MarketEntry("NHL", Family.GAME_TOTAL,
    provider_market_keys=("totals",),
    market_class="game_market", line_type="both",
    capability_state=MODEL_UNAVAILABLE))
_add(MarketEntry("UFC", Family.MONEYLINE, provider_market_keys=("h2h",),
    market_class="game_market", selection_schema="moneyline",
    allowed_sides=("home", "away"),
    capability_state=MODEL_UNAVAILABLE))
_add(MarketEntry("UFC", "ufc_rounds_total",
    provider_market_keys=("totals",),
    aliases=("rounds_total",),
    market_class="game_market", line_type="both",
    capability_state=MODEL_UNAVAILABLE))

# ── PERKLOCKS-MAIN 35 · FINAL — provider-supported markets without
# a shipped model (MODEL_UNAVAILABLE). Declared HONESTLY so the
# cross-registry parity test never reports drift.
_add(MarketEntry("NBA", "nba_pts_reb",
    provider_market_keys=("player_points_rebounds",),
    market_class="player_prop", line_type="both",
    capability_state=MODEL_UNAVAILABLE))
_add(MarketEntry("NBA", "nba_pts_ast",
    provider_market_keys=("player_points_assists",),
    market_class="player_prop", line_type="both",
    capability_state=MODEL_UNAVAILABLE))
_add(MarketEntry("NBA", "nba_reb_ast",
    provider_market_keys=("player_rebounds_assists",),
    market_class="player_prop", line_type="both",
    capability_state=MODEL_UNAVAILABLE))
_add(MarketEntry("NBA", "nba_steals",
    provider_market_keys=("player_steals",),
    market_class="player_prop", line_type="standard",
    capability_state=MODEL_UNAVAILABLE))
_add(MarketEntry("NBA", "nba_blocks",
    provider_market_keys=("player_blocks",),
    market_class="player_prop", line_type="standard",
    capability_state=MODEL_UNAVAILABLE))
_add(MarketEntry("NFL", "qb_pass_attempts",
    provider_market_keys=("player_pass_attempts",),
    market_class="player_prop", line_type="standard",
    capability_state=MODEL_UNAVAILABLE))
_add(MarketEntry("NFL", "qb_pass_completions",
    provider_market_keys=("player_pass_completions",),
    market_class="player_prop", line_type="standard",
    capability_state=MODEL_UNAVAILABLE))
_add(MarketEntry("NFL", "rb_rush_attempts",
    provider_market_keys=("player_rush_attempts",),
    market_class="player_prop", line_type="standard",
    capability_state=MODEL_UNAVAILABLE))
_add(MarketEntry("NFL", "rb_rush_tds",
    provider_market_keys=("player_rush_tds",),
    market_class="player_prop", line_type="standard",
    capability_state=MODEL_UNAVAILABLE))
_add(MarketEntry("NFL", "wr_reception_tds",
    provider_market_keys=("player_reception_tds",),
    market_class="player_prop", line_type="standard",
    capability_state=MODEL_UNAVAILABLE))
_add(MarketEntry("NFL", "player_first_td",
    provider_market_keys=("player_1st_td",),
    aliases=("first_touchdown",),
    market_class="player_prop", line_type="standard",
    selection_schema="yes_no", allowed_sides=("yes",),
    requires_real_line=False,
    capability_state=RESEARCH_ONLY))


# ── Public API ──────────────────────────────────────────────────────
def get(sport: str, family: str) -> Optional[MarketEntry]:
    return _REGISTRY.get((sport, family))


def all_entries() -> Dict[Tuple[str, str], MarketEntry]:
    return dict(_REGISTRY)


def resolve_provider_key(sport: str, provider_key: str) -> Optional[MarketEntry]:
    """Given a raw provider `market_key`, resolve to the canonical entry."""
    pk = (provider_key or "").lower()
    for e in _REGISTRY.values():
        if e.sport != sport:
            continue
        if pk in tuple(k.lower() for k in e.provider_market_keys):
            return e
        if pk in tuple(a.lower() for a in e.aliases):
            return e
    return None


def is_alternate(sport: str, provider_key: str) -> bool:
    """Classify a provider key as an alternate market. Used BEFORE any
    standard-prop filtering so alt families (`*_alternate`,
    `alternate_totals_games`) never fall through the standard filter."""
    if not provider_key:
        return False
    pk = provider_key.lower()
    if "alternate" in pk or "_alt" in pk:
        return True
    entry = resolve_provider_key(sport, provider_key)
    return bool(entry and entry.line_type == "alternate")


def capability(sport: str, family: str) -> str:
    e = _REGISTRY.get((sport, family))
    return e.capability_state if e else PROVIDER_UNAVAILABLE
