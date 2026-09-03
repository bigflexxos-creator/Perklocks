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

# ── NBA — declared honestly ────────────────────────────────────────
_add(MarketEntry("NBA", Family.MONEYLINE, provider_market_keys=("h2h",),
    market_class="game_market", selection_schema="moneyline",
    allowed_sides=("home", "away"),
    capability_state=PROVIDER_UNAVAILABLE))  # off-season today
_add(MarketEntry("NBA", Family.NBA_POINTS,
    provider_market_keys=("player_points", "player_points_alternate"),
    market_class="player_prop", line_type="both",
    model_authority="nba_points_model",
    settlement_actuals=("player_points",),
    settlement_primary="espn_boxscore",
    capability_state=PROVIDER_UNAVAILABLE))

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

# ── NHL / UFC — honest unavailable states, no fake support ─────────
_add(MarketEntry("NHL", Family.MONEYLINE, provider_market_keys=("h2h",),
    market_class="game_market", selection_schema="moneyline",
    allowed_sides=("home", "away"),
    capability_state=PROVIDER_UNAVAILABLE))
_add(MarketEntry("UFC", Family.MONEYLINE, provider_market_keys=("h2h",),
    market_class="game_market", selection_schema="moneyline",
    allowed_sides=("home", "away"),
    capability_state=MODEL_UNAVAILABLE))


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
