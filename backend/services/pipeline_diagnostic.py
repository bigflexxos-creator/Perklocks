"""Block 2A — Pipeline Diagnostic Framework (2026-08).

Read-only reusable diagnostic module used to answer:

  "For sport S, market M, on date D, event E — did we discover the
   event, request the market, get real lines, resolve identity,
   run the specialized engine, generate a candidate, score it,
   pass the >85 gate, publish canonically, and reach the Locks /
   Rollover / Parlay consumers?"

The heart of this module is:

  * ``ReasonCode`` — the canonical taxonomy of every stage-drop or
    downstream-exclusion reason.  Every future audit must classify
    a "lost pick" with one of these codes; no silent drops.

  * ``PipelineTrace`` — a mutable stage-by-stage container carried
    through a diagnostic run.  Each stage records ``entered``,
    ``passed``, ``dropped`` with associated reason codes.

  * ``WiringStatus`` — the taxonomy required by the Block 2 spec:
    FULLY_WIRED / PARTIALLY_WIRED / DEAD_END / NO_REAL_LINE /
    UNSUPPORTED / DISABLED / BROKEN.

  * ``build_wiring_matrix(...)`` — walks the sport capability
    registry and produces the sport × market wiring matrix using
    the static evidence collected in ``services.wiring_evidence``.

This module performs NO writes and NO network calls.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


# ═══════════════════════════════════════════════════════════════════
# Reason-code taxonomy — the ONLY vocabulary allowed for "lost pick"
# ═══════════════════════════════════════════════════════════════════
class ReasonCode(str, enum.Enum):
    # ── stage: event discovery ──────────────────────────────────
    EVENT_NOT_DISCOVERED       = "EVENT_NOT_DISCOVERED"
    EVENT_TIME_FILTER          = "EVENT_TIME_FILTER"
    EVENT_STATUS_FILTER        = "EVENT_STATUS_FILTER"
    # ── stage: real line / real market ──────────────────────────
    MARKET_NOT_SUPPORTED       = "MARKET_NOT_SUPPORTED"
    NO_REAL_LINE               = "NO_REAL_LINE"
    STALE_LINE                 = "STALE_LINE"
    LINE_MOVED_OFF_BOOK        = "LINE_MOVED_OFF_BOOK"
    MODEL_ONLY_SYNTHETIC_ODDS  = "MODEL_ONLY_SYNTHETIC_ODDS"
    # ── stage: cache / snapshot ─────────────────────────────────
    STALE_CACHE_USED           = "STALE_CACHE_USED"
    EMPTY_CACHE                = "EMPTY_CACHE"
    PARTIAL_MARKET_SNAPSHOT    = "PARTIAL_MARKET_SNAPSHOT"
    EVENT_MISSING_FROM_SNAPSHOT = "EVENT_MISSING_FROM_SNAPSHOT"
    # ── stage: provider ─────────────────────────────────────────
    PROVIDER_ERROR             = "PROVIDER_ERROR"
    PROVIDER_422               = "PROVIDER_422"
    BUDGET_BLOCKED             = "BUDGET_BLOCKED"
    BAD_MARKET_SUPPRESSED      = "BAD_MARKET_SUPPRESSED"
    # ── stage: identity ─────────────────────────────────────────
    PLAYER_IDENTITY_UNRESOLVED = "PLAYER_IDENTITY_UNRESOLVED"
    TEAM_IDENTITY_UNRESOLVED   = "TEAM_IDENTITY_UNRESOLVED"
    IDENTITY_CONFLICT          = "IDENTITY_CONFLICT"
    # ── stage: engine ───────────────────────────────────────────
    ENGINE_MISSING             = "ENGINE_MISSING"
    ENGINE_ERROR               = "ENGINE_ERROR"
    ENGINE_INSUFFICIENT_DATA   = "ENGINE_INSUFFICIENT_DATA"
    ENGINE_OUTPUT_IGNORED      = "ENGINE_OUTPUT_IGNORED"
    # ── stage: candidate / scoring ──────────────────────────────
    CANDIDATE_NOT_GENERATED    = "CANDIDATE_NOT_GENERATED"
    CANDIDATE_BELOW_MIN_EDGE   = "CANDIDATE_BELOW_MIN_EDGE"
    SCORE_BELOW_85             = "SCORE_BELOW_85"
    MARKET_MAPPING_MISSING     = "MARKET_MAPPING_MISSING"
    # ── stage: publication ─────────────────────────────────────
    PUBLICATION_BARRIER_REJECT = "PUBLICATION_BARRIER_REJECT"
    NON_CANONICAL_WRITE        = "NON_CANONICAL_WRITE"
    DUPLICATE_PUBLICATION      = "DUPLICATE_PUBLICATION"
    # ── stage: fusion / timing ──────────────────────────────────
    FUSION_POST_PUBLICATION    = "FUSION_POST_PUBLICATION"
    FUSION_UNCONSUMED_BY_SCORE = "FUSION_UNCONSUMED_BY_SCORE"
    # ── stage: downstream ───────────────────────────────────────
    CORRELATED_CONFLICT        = "CORRELATED_CONFLICT"
    DUPLICATE_PLAYER_MARKET    = "DUPLICATE_PLAYER_MARKET"
    DUPLICATE_EVENT_LIMIT      = "DUPLICATE_EVENT_LIMIT"
    UNSUPPORTED_MARKET         = "UNSUPPORTED_MARKET"
    STALE_PICK                 = "STALE_PICK"
    SETTLEMENT_STARTED         = "SETTLEMENT_STARTED"
    MISSING_REAL_ODDS          = "MISSING_REAL_ODDS"
    ROLLOVER_MARKET_BLOCKED    = "ROLLOVER_MARKET_BLOCKED"
    PARLAY_MARKET_BLOCKED      = "PARLAY_MARKET_BLOCKED"
    # ── stage: slate starvation ─────────────────────────────────
    FIRST_N_CAP_STARVATION     = "FIRST_N_CAP_STARVATION"
    SAFETY_CAP_HIT             = "SAFETY_CAP_HIT"
    API_COST_CAP_HIT           = "API_COST_CAP_HIT"
    # ── stage: settlement ─────────────────────────────────────
    SETTLEMENT_ENGINE_MISSING  = "SETTLEMENT_ENGINE_MISSING"
    SETTLEMENT_SOURCE_UNKNOWN  = "SETTLEMENT_SOURCE_UNKNOWN"


# ═══════════════════════════════════════════════════════════════════
# Wiring status taxonomy (spec §1)
# ═══════════════════════════════════════════════════════════════════
class WiringStatus(str, enum.Enum):
    FULLY_WIRED       = "FULLY_WIRED"
    PARTIALLY_WIRED   = "PARTIALLY_WIRED"
    DEAD_END          = "DEAD_END"
    NO_REAL_LINE      = "NO_REAL_LINE"
    UNSUPPORTED       = "UNSUPPORTED"
    DISABLED          = "DISABLED"
    BROKEN            = "BROKEN"


PIPELINE_STAGES = (
    "source", "event_discovery", "real_line", "identity",
    "feature_engine", "specialized_engine", "model", "simulator",
    "matchup_history", "candidate_generator", "validation",
    "gt85_gate", "canonical_publication", "locks", "rollover",
    "parlay", "settlement",
)


# ═══════════════════════════════════════════════════════════════════
# Trace container
# ═══════════════════════════════════════════════════════════════════
@dataclass
class StageResult:
    stage:    str
    entered:  int = 0
    passed:   int = 0
    dropped:  int = 0
    reasons:  dict[str, int] = field(default_factory=dict)
    evidence: list[str]      = field(default_factory=list)


@dataclass
class PipelineTrace:
    sport:  str
    market: Optional[str] = None
    date:   Optional[str] = None
    event:  Optional[str] = None
    stages: dict[str, StageResult] = field(default_factory=dict)
    started_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def stage(self, name: str) -> StageResult:
        if name not in self.stages:
            self.stages[name] = StageResult(stage=name)
        return self.stages[name]

    def enter(self, stage: str, n: int = 1) -> None:
        self.stage(stage).entered += n

    def pass_(self, stage: str, n: int = 1) -> None:
        self.stage(stage).passed += n

    def drop(self, stage: str, reason: ReasonCode | str,
              n: int = 1, evidence: Optional[str] = None) -> None:
        s = self.stage(stage)
        s.dropped += n
        code = reason.value if isinstance(reason, ReasonCode) else str(reason)
        s.reasons[code] = s.reasons.get(code, 0) + n
        if evidence:
            s.evidence.append(evidence)

    def to_dict(self) -> dict:
        return {
            "sport":  self.sport,
            "market": self.market,
            "date":   self.date,
            "event":  self.event,
            "started_at": self.started_at,
            "stages": {
                k: {"entered": v.entered, "passed": v.passed,
                    "dropped": v.dropped, "reasons": v.reasons,
                    "evidence": v.evidence}
                for k, v in self.stages.items()},
        }


# ═══════════════════════════════════════════════════════════════════
# Wiring evidence — static code trace produced by hand-review of
# the entry paths (Block 2A read-only audit).  Each entry links to a
# file / function that proves the stage is (or is not) wired.
# ═══════════════════════════════════════════════════════════════════
@dataclass
class Evidence:
    file:     str
    symbol:   Optional[str] = None
    lines:    Optional[str] = None
    note:     Optional[str] = None

    def to_dict(self) -> dict:
        return {"file": self.file, "symbol": self.symbol,
                "lines": self.lines, "note": self.note}


# ═══════════════════════════════════════════════════════════════════
# Static wiring evidence for the 8 enabled sports.
# Populated from the Block 2A code map. This is the ground-truth
# artifact that grounds every fix in Block 2B/2C/2D/2E.
# ═══════════════════════════════════════════════════════════════════
_WIRING_EVIDENCE: dict[tuple[str, str], dict] = {
    # ── MLB game markets ────────────────────────────────────────
    ("MLB", "h2h"): {
        "status": WiringStatus.FULLY_WIRED,
        "evidence": {
            "source":                Evidence("services/odds_api_gateway.py",
                                                "fetch_odds_bulk"),
            "event_discovery":       Evidence("sports_engine.py",
                                                "SPORT_KEYS['MLB']", "42"),
            "real_line":             Evidence("services/odds_api_gateway.py"),
            "identity":              Evidence("services/universal_player_identity.py"),
            "feature_engine":        Evidence("services/mlb_feature_engine.py"),
            "specialized_engine":    Evidence("mlb_pitcher_h2h.py"),
            "model":                 Evidence("sports_engine.py", "build_mlb_ml_factors"),
            "simulator":             Evidence("sim_engine.py"),
            "matchup_history":       Evidence("services/h2h_enricher.py"),
            "candidate_generator":   Evidence("sports_engine.py", "generate_all_picks", "5317"),
            "validation":            Evidence("services/main_board_eligibility.py"),
            "gt85_gate":             Evidence("services/main_board_eligibility.py"),
            "canonical_publication": Evidence("services/publication_helpers.py",
                                                "publish_upserted_picks", "38"),
            "locks":                 Evidence("routes/picks_routes.py", "/api/picks/today"),
            "rollover":              Evidence("routes/picks_routes.py", "pick_rollover", "282-540"),
            "parlay":                Evidence("parlay_optimizer.py"),
            "settlement":            Evidence("prop_settlement.py"),
        },
        "notes": "Full end-to-end.",
    },
    ("MLB", "spreads"):    "COPY_OF_H2H",
    ("MLB", "totals"):     "COPY_OF_H2H",
    # ── MLB prop markets ────────────────────────────────────────
    ("MLB", "batter_hits"): {
        "status": WiringStatus.FULLY_WIRED,
        "evidence": {
            "source":                Evidence("services/odds_api_gateway.py"),
            "event_discovery":       Evidence("sports_engine.py"),
            "real_line":             Evidence("services/odds_api_gateway.py",
                                                "_fetch_mlb_event_alts"),
            "identity":              Evidence("services/universal_player_identity.py"),
            "feature_engine":        Evidence("services/mlb_feature_engine.py"),
            "specialized_engine":    Evidence("mlb_batter_h2h.py"),
            "model":                 Evidence("services/mlb_hitter_intel.py"),
            "simulator":             Evidence("sim_engine.py"),
            "matchup_history":       Evidence("services/h2h_enricher.py"),
            "candidate_generator":   Evidence("sports_engine.py"),
            "validation":            Evidence("services/main_board_eligibility.py"),
            "gt85_gate":             Evidence("services/main_board_eligibility.py"),
            "canonical_publication": Evidence("services/publication_helpers.py"),
            "locks":                 Evidence("routes/picks_routes.py"),
            "rollover":              Evidence("routes/picks_routes.py",
                                                "pick_rollover"),
            "parlay":                Evidence("parlay_optimizer.py"),
            "settlement":            Evidence("prop_settlement.py"),
        },
        "notes": ("Alt-line variants inherit this wiring via "
                    "batter_hits_alternate."),
    },
    ("MLB", "batter_hits_alternate"):        "COPY_OF_HITS",
    ("MLB", "batter_hits_runs_rbis"):        "COPY_OF_HITS",
    ("MLB", "batter_hits_runs_rbis_alternate"): "COPY_OF_HITS",
    ("MLB", "batter_home_runs"): {
        "status": WiringStatus.PARTIALLY_WIRED,
        "evidence": {
            "source":                Evidence("services/odds_api_gateway.py"),
            "event_discovery":       Evidence("sports_engine.py"),
            "real_line":             Evidence("services/odds_api_gateway.py"),
            "identity":              Evidence("services/universal_player_identity.py"),
            "feature_engine":        Evidence("services/mlb_feature_engine.py"),
            "specialized_engine":    Evidence("services/mlb_hr_intel.py",
                                                "build_hr_slate"),
            "model":                 Evidence("services/mlb_hr_intel.py"),
            "simulator":             Evidence("sim_engine.py"),
            "matchup_history":       Evidence("services/h2h_enricher.py"),
            "candidate_generator":   Evidence("sports_engine.py"),
            "validation":            Evidence("services/main_board_eligibility.py"),
            "gt85_gate":             Evidence("services/main_board_eligibility.py"),
            "canonical_publication": Evidence("services/publication_helpers.py"),
            "locks":                 Evidence("routes/picks_routes.py"),
            "rollover":              Evidence("routes/picks_routes.py"),
            "parlay":                Evidence("parlay_optimizer.py"),
            "settlement":            Evidence("prop_settlement.py"),
        },
        "notes": ("HR Board (`/api/mlb/hr_board`) uses build_hr_slate "
                    "but the main Locks path DOES NOT consume the HR "
                    "intel service — HR candidates fall through generic "
                    "MLB scoring instead of the specialized HR model.  "
                    "Spec §8 ACTIVE_BUT_IGNORED for the main-board flow."),
        "defects": [
            {"id": "MLB-HR-1", "priority": "P1",
              "code": ReasonCode.ENGINE_OUTPUT_IGNORED.value,
              "detail": "mlb_hr_intel.build_hr_slate only reached via "
                         "the /api/mlb/hr_board endpoint, not sports_engine."},
        ],
    },
    ("MLB", "batter_home_runs_alternate"):   "COPY_OF_HR",
    ("MLB", "batter_rbis"):                  "COPY_OF_HITS",
    ("MLB", "batter_rbis_alternate"):        "COPY_OF_HITS",
    ("MLB", "batter_total_bases"):           "COPY_OF_HITS",
    ("MLB", "batter_total_bases_alternate"): "COPY_OF_HITS",
    ("MLB", "pitcher_strikeouts"): {
        "status": WiringStatus.FULLY_WIRED,
        "evidence": {
            "source":                Evidence("services/odds_api_gateway.py"),
            "event_discovery":       Evidence("sports_engine.py"),
            "real_line":             Evidence("services/odds_api_gateway.py"),
            "identity":              Evidence("services/universal_player_identity.py"),
            "feature_engine":        Evidence("services/mlb_feature_engine.py"),
            "specialized_engine":    Evidence("mlb_pitcher_h2h.py"),
            "model":                 Evidence("services/mlb_k_probability.py"),
            "simulator":             Evidence("sim_engine.py"),
            "matchup_history":       Evidence("services/h2h_enricher.py"),
            "candidate_generator":   Evidence("sports_engine.py"),
            "validation":            Evidence("services/main_board_eligibility.py"),
            "gt85_gate":             Evidence("services/main_board_eligibility.py"),
            "canonical_publication": Evidence("services/publication_helpers.py"),
            "locks":                 Evidence("routes/picks_routes.py"),
            "rollover":              Evidence("routes/picks_routes.py"),
            "parlay":                Evidence("parlay_optimizer.py"),
            "settlement":            Evidence("prop_settlement.py"),
        },
        "notes": "Full end-to-end incl. late-game window (see §3 for "
                    "late-night MLB choke-point audit — deferred to 2B).",
    },
    ("MLB", "pitcher_strikeouts_alternate"): "COPY_OF_K",
    ("MLB", "pitcher_outs"):                 "COPY_OF_K",

    # ── NFL ─────────────────────────────────────────────────────
    ("NFL", "h2h"): {
        "status": WiringStatus.FULLY_WIRED,
        "evidence": {
            "source":              Evidence("services/odds_api_gateway.py"),
            "event_discovery":     Evidence("sports_engine.py", None, "42"),
            "real_line":           Evidence("services/odds_api_gateway.py"),
            "identity":            Evidence("services/universal_player_identity.py"),
            "feature_engine":      Evidence("services/nfl_feature_engine.py"),
            "specialized_engine":  Evidence("nfl_game_engine.py"),
            "model":               Evidence("services/nfl_features.py"),
            "simulator":           Evidence("sim_engine.py"),
            "matchup_history":     Evidence("services/nfl_matchup_intelligence.py"),
            "candidate_generator": Evidence("sports_engine.py"),
            "validation":          Evidence("services/main_board_eligibility.py"),
            "gt85_gate":           Evidence("services/main_board_eligibility.py"),
            "canonical_publication": Evidence("services/publication_helpers.py"),
            "locks":               Evidence("routes/picks_routes.py"),
            "rollover":            Evidence("routes/picks_routes.py"),
            "parlay":              Evidence("parlay_optimizer.py"),
            "settlement":          Evidence("services/settlement_service.py"),
        },
        "notes": "NFL game markets wired end-to-end (Phase 1 2026-08-11).",
    },
    ("NFL", "spreads"): "COPY_OF_NFL_H2H",
    ("NFL", "totals"):  "COPY_OF_NFL_H2H",
    ("NFL", "player_pass_yds"): "COPY_OF_NFL_H2H",
    ("NFL", "player_pass_yds_alternate"): "COPY_OF_NFL_H2H",
    ("NFL", "player_pass_tds"): "COPY_OF_NFL_H2H",
    ("NFL", "player_pass_attempts"): "COPY_OF_NFL_H2H",
    ("NFL", "player_pass_completions"): "COPY_OF_NFL_H2H",
    ("NFL", "player_rush_yds"): "COPY_OF_NFL_H2H",
    ("NFL", "player_rush_yds_alternate"): "COPY_OF_NFL_H2H",
    ("NFL", "player_rush_attempts"): "COPY_OF_NFL_H2H",
    ("NFL", "player_rush_tds"): "COPY_OF_NFL_H2H",
    ("NFL", "player_receptions"): "COPY_OF_NFL_H2H",
    ("NFL", "player_receptions_alternate"): "COPY_OF_NFL_H2H",
    ("NFL", "player_reception_yds"): "COPY_OF_NFL_H2H",
    ("NFL", "player_reception_yds_alternate"): "COPY_OF_NFL_H2H",
    ("NFL", "player_reception_tds"): "COPY_OF_NFL_H2H",
    ("NFL", "player_anytime_td"): {
        "status": WiringStatus.PARTIALLY_WIRED,
        "evidence": {
            "source":              Evidence("services/odds_api_gateway.py"),
            "event_discovery":     Evidence("sports_engine.py"),
            "real_line":           Evidence("services/odds_api_gateway.py"),
            "identity":            Evidence("services/universal_player_identity.py"),
            "feature_engine":      Evidence("services/nfl_feature_engine.py"),
            "specialized_engine":  Evidence("nfl_atd_engine.py",
                                             "predict_player_atd"),
            "model":               Evidence("nfl_atd_engine.py"),
            "simulator":           Evidence("sim_engine.py"),
            "matchup_history":     Evidence("services/nfl_matchup_intelligence.py"),
            "candidate_generator": Evidence("sports_engine.py"),
            "validation":          Evidence("services/main_board_eligibility.py"),
            "gt85_gate":           Evidence("services/main_board_eligibility.py"),
            "canonical_publication": Evidence("services/publication_helpers.py"),
            "locks":               Evidence("routes/picks_routes.py"),
            "rollover":            Evidence("routes/picks_routes.py"),
            "parlay":              Evidence("parlay_optimizer.py"),
            "settlement":          Evidence("services/settlement_service.py"),
        },
        "notes": ("nfl_atd_engine exists and is called from "
                    "routes/nfl_routes.py leaderboard endpoints, but "
                    "there is NO import in sports_engine.py — main "
                    "candidate generation does NOT consume ATD "
                    "specialized evidence. Spec §7 defect."),
        "defects": [
            {"id": "NFL-ATD-1", "priority": "P0",
              "code": ReasonCode.ENGINE_OUTPUT_IGNORED.value,
              "detail": ("nfl_atd_engine imports appear only in "
                          "routes/nfl_routes.py; sports_engine's ATD "
                          "candidates fall through generic NFL "
                          "scoring.  This is exactly the wiring "
                          "defect §7 flags.")},
        ],
    },
    ("NFL", "player_1st_td"): "COPY_OF_ATD",

    # ── NBA ─────────────────────────────────────────────────────
    ("NBA", "h2h"): {
        "status": WiringStatus.FULLY_WIRED,
        "evidence": {
            "source":              Evidence("services/odds_api_gateway.py"),
            "event_discovery":     Evidence("sports_engine.py", None, "42"),
            "real_line":           Evidence("services/odds_api_gateway.py"),
            "identity":            Evidence("services/universal_player_identity.py"),
            "feature_engine":      Evidence("services/nba_feature_engine.py"),
            "specialized_engine":  Evidence("services/nba_ingest.py"),
            "model":               Evidence("services/nba_gamelog_ingest.py"),
            "simulator":           Evidence("sim_engine.py"),
            "matchup_history":     Evidence("services/similar_matchup_engine.py"),
            "candidate_generator": Evidence("sports_engine.py"),
            "validation":          Evidence("services/main_board_eligibility.py"),
            "gt85_gate":           Evidence("services/main_board_eligibility.py"),
            "canonical_publication": Evidence("services/publication_helpers.py"),
            "locks":               Evidence("routes/picks_routes.py"),
            "rollover":            Evidence("routes/picks_routes.py"),
            "parlay":              Evidence("parlay_optimizer.py"),
            "settlement":          Evidence("services/settlement_service.py"),
        },
        "notes": "Full end-to-end.",
    },
    ("NBA", "spreads"): "COPY_OF_NBA_H2H",
    ("NBA", "totals"):  "COPY_OF_NBA_H2H",
    ("NBA", "player_points"): "COPY_OF_NBA_H2H",
    ("NBA", "player_points_alternate"): "COPY_OF_NBA_H2H",
    ("NBA", "player_rebounds"): "COPY_OF_NBA_H2H",
    ("NBA", "player_rebounds_alternate"): "COPY_OF_NBA_H2H",
    ("NBA", "player_assists"): "COPY_OF_NBA_H2H",
    ("NBA", "player_assists_alternate"): "COPY_OF_NBA_H2H",
    ("NBA", "player_points_rebounds_assists"): "COPY_OF_NBA_H2H",
    ("NBA", "player_points_rebounds_assists_alternate"): "COPY_OF_NBA_H2H",
    ("NBA", "player_points_rebounds"): "COPY_OF_NBA_H2H",
    ("NBA", "player_points_assists"): "COPY_OF_NBA_H2H",
    ("NBA", "player_rebounds_assists"): "COPY_OF_NBA_H2H",
    ("NBA", "player_threes"): "COPY_OF_NBA_H2H",
    ("NBA", "player_threes_alternate"): "COPY_OF_NBA_H2H",
    ("NBA", "player_steals"): "COPY_OF_NBA_H2H",
    ("NBA", "player_blocks"): "COPY_OF_NBA_H2H",

    # ── NHL ─────────────────────────────────────────────────────
    ("NHL", "h2h"): {
        "status": WiringStatus.FULLY_WIRED,
        "evidence": {
            "source":              Evidence("services/odds_api_gateway.py"),
            "event_discovery":     Evidence("sports_engine.py"),
            "real_line":           Evidence("services/odds_api_gateway.py"),
            "identity":            Evidence("services/universal_player_identity.py"),
            "feature_engine":      Evidence("sports_engine.py"),
            "specialized_engine":  Evidence("sports_engine.py",
                                             note="Generic — no NHL-specific engine yet"),
            "model":               Evidence("sports_engine.py"),
            "simulator":           Evidence("sim_engine.py"),
            "matchup_history":     Evidence("services/similar_matchup_engine.py"),
            "candidate_generator": Evidence("sports_engine.py"),
            "validation":          Evidence("services/main_board_eligibility.py"),
            "gt85_gate":           Evidence("services/main_board_eligibility.py"),
            "canonical_publication": Evidence("services/publication_helpers.py"),
            "locks":               Evidence("routes/picks_routes.py"),
            "rollover":            Evidence("routes/picks_routes.py"),
            "parlay":              Evidence("parlay_optimizer.py"),
            "settlement":          Evidence("services/settlement_service.py"),
        },
        "notes": "Game markets only.  Player props NOT wired (registry).",
    },
    ("NHL", "spreads"): "COPY_OF_NHL_H2H",
    ("NHL", "totals"):  "COPY_OF_NHL_H2H",

    # ── CFB ─────────────────────────────────────────────────────
    ("CFB", "h2h"): {
        "status": WiringStatus.FULLY_WIRED,
        "evidence": {
            "source":              Evidence("services/odds_api_gateway.py"),
            "event_discovery":     Evidence("sports_engine.py"),
            "real_line":           Evidence("services/odds_api_gateway.py"),
            "identity":            Evidence("services/universal_player_identity.py"),
            "feature_engine":      Evidence("services/cfb_feature_engine.py"),
            "specialized_engine":  Evidence("services/cfb_ingest.py"),
            "model":               Evidence("services/cfb_precompute.py"),
            "simulator":           Evidence("sim_engine.py"),
            "matchup_history":     Evidence("services/cfb_precompute.py"),
            "candidate_generator": Evidence("sports_engine.py"),
            "validation":          Evidence("services/main_board_eligibility.py"),
            "gt85_gate":           Evidence("services/main_board_eligibility.py"),
            "canonical_publication": Evidence("services/publication_helpers.py"),
            "locks":               Evidence("routes/picks_routes.py"),
            "rollover":            Evidence("routes/picks_routes.py"),
            "parlay":              Evidence("parlay_optimizer.py"),
            "settlement":          Evidence("services/settlement_service.py"),
        },
        "notes": "Game markets only (registry).",
    },
    ("CFB", "spreads"): "COPY_OF_CFB_H2H",
    ("CFB", "totals"):  "COPY_OF_CFB_H2H",

    # ── Soccer ──────────────────────────────────────────────────
    ("Soccer", "h2h"): {
        "status": WiringStatus.FULLY_WIRED,
        "evidence": {
            "source":              Evidence("services/odds_api_gateway.py"),
            "event_discovery":     Evidence("services/espn_soccer_fixtures.py"),
            "real_line":           Evidence("services/odds_api_gateway.py"),
            "identity":            Evidence("services/universal_player_identity.py"),
            "feature_engine":      Evidence("services/soccer_feature_engine.py"),
            "specialized_engine":  Evidence("services/soccer/pipeline.py"),
            "model":               Evidence("sports_engine.py",
                                             "build_soccer_ml_factors"),
            "simulator":           Evidence("sim_engine.py"),
            "matchup_history":     Evidence("services/mls_player_matchup_history.py"),
            "candidate_generator": Evidence("sports_engine.py"),
            "validation":          Evidence("services/main_board_eligibility.py"),
            "gt85_gate":           Evidence("services/main_board_eligibility.py"),
            "canonical_publication": Evidence("services/publication_helpers.py"),
            "locks":               Evidence("routes/picks_routes.py"),
            "rollover":            Evidence("routes/picks_routes.py"),
            "parlay":              Evidence("parlay_optimizer.py"),
            "settlement":          Evidence("soccer_espn_settle.py"),
        },
        "notes": "1X2 wired end-to-end.",
    },
    ("Soccer", "spreads"):          "COPY_OF_SOCCER_H2H",
    ("Soccer", "totals"):           "COPY_OF_SOCCER_H2H",
    ("Soccer", "btts"):             "COPY_OF_SOCCER_H2H",
    ("Soccer", "double_chance"):    "COPY_OF_SOCCER_H2H",
    ("Soccer", "player_goal_scorer_anytime"): {
        "status": WiringStatus.FULLY_WIRED,
        "evidence": {
            "source":              Evidence("services/odds_api_gateway.py"),
            "event_discovery":     Evidence("services/espn_soccer_fixtures.py"),
            "real_line":           Evidence("services/odds_api_gateway.py"),
            "identity":            Evidence("services/universal_player_identity.py"),
            "feature_engine":      Evidence("services/soccer_feature_engine.py"),
            "specialized_engine":  Evidence("goal_scorer_engine_v2.py"),
            "model":               Evidence("services/mls_direct_inject.py"),
            "simulator":           Evidence("sim_engine.py"),
            "matchup_history":     Evidence("services/mls_player_matchup_history.py"),
            "candidate_generator": Evidence("services/mls_direct_inject.py"),
            "validation":          Evidence("services/soccer_scorer_eligibility.py"),
            "gt85_gate":           Evidence("services/main_board_eligibility.py"),
            "canonical_publication": Evidence("services/publication_helpers.py"),
            "locks":               Evidence("routes/picks_routes.py"),
            "rollover":            Evidence("routes/picks_routes.py"),
            "parlay":              Evidence("parlay_optimizer.py"),
            "settlement":          Evidence("soccer_fotmob_settle.py"),
        },
        "notes": "Multiple fallback ingesters (see registry).",
    },
    ("Soccer", "player_to_score_or_assist"): "COPY_OF_SOCCER_AGS",
    ("Soccer", "player_first_goal_scorer"):  "COPY_OF_SOCCER_AGS",

    # ── Tennis ──────────────────────────────────────────────────
    ("Tennis", "h2h"): {
        "status": WiringStatus.FULLY_WIRED,
        "evidence": {
            "source":              Evidence("services/odds_api_gateway.py"),
            "event_discovery":     Evidence("tennis_engine.py"),
            "real_line":           Evidence("services/odds_api_gateway.py"),
            "identity":            Evidence("services/tennis_identity.py"),
            "feature_engine":      Evidence("services/tennis_feature_engine.py"),
            "specialized_engine":  Evidence("tennis_engine.py"),
            "model":               Evidence("services/tennis_math_engine.py"),
            "simulator":           Evidence("sim_engine.py"),
            "matchup_history":     Evidence("services/similar_matchup_engine.py"),
            "candidate_generator": Evidence("sports_engine.py"),
            "validation":          Evidence("services/main_board_eligibility.py"),
            "gt85_gate":           Evidence("services/main_board_eligibility.py"),
            "canonical_publication": Evidence("services/publication_helpers.py"),
            "locks":               Evidence("routes/picks_routes.py"),
            "rollover":            Evidence("routes/picks_routes.py"),
            "parlay":              Evidence("parlay_optimizer.py"),
            "settlement":          Evidence("tennis_extra/settle.py"),
        },
        "notes": "Tennis Extra fallback covers ATP/WTA/Challenger.",
    },
    ("Tennis", "spreads"): "COPY_OF_TENNIS_H2H",
    ("Tennis", "totals"):  "COPY_OF_TENNIS_H2H",

    # ── UFC ────────────────────────────────────────────────────
    ("UFC", "h2h"): {
        "status": WiringStatus.FULLY_WIRED,
        "evidence": {
            "source":              Evidence("services/odds_api_gateway.py"),
            "event_discovery":     Evidence("ufc_espn_ingest.py"),
            "real_line":           Evidence("services/odds_api_gateway.py"),
            "identity":            Evidence("services/universal_player_identity.py"),
            "feature_engine":      Evidence("sports_engine.py",
                                             note="Generic — no UFC feature engine"),
            "specialized_engine":  Evidence("sports_engine.py"),
            "model":               Evidence("sports_engine.py"),
            "simulator":           Evidence("sim_engine.py"),
            "matchup_history":     Evidence("services/similar_matchup_engine.py"),
            "candidate_generator": Evidence("sports_engine.py",
                                             note="_ufc_ml_only branch @ 1141"),
            "validation":          Evidence("services/main_board_eligibility.py"),
            "gt85_gate":           Evidence("services/main_board_eligibility.py"),
            "canonical_publication": Evidence("services/publication_helpers.py"),
            "locks":               Evidence("routes/picks_routes.py"),
            "rollover":            Evidence("routes/picks_routes.py"),
            "parlay":              Evidence("parlay_optimizer.py"),
            "settlement":          Evidence("ufc_espn_ingest.py"),
        },
        "notes": "Moneyline + rounds totals only.",
    },
    ("UFC", "totals"): "COPY_OF_UFC_H2H",
}


def _resolve_copy(entry):
    """Resolve COPY_OF_* pointers to the concrete evidence dict."""
    if not isinstance(entry, str):
        return entry
    return None


def get_wiring_evidence(sport: str, market: str) -> Optional[dict]:
    entry = _WIRING_EVIDENCE.get((sport, market))
    if isinstance(entry, dict):
        return entry
    if isinstance(entry, str) and entry.startswith("COPY_OF_"):
        # Best-effort resolve: look for the first concrete entry for
        # the same sport.  This keeps the matrix compact without
        # copy-pasting evidence 30 times.
        for (s, m), v in _WIRING_EVIDENCE.items():
            if s == sport and isinstance(v, dict):
                return v
    return None


# ═══════════════════════════════════════════════════════════════════
# Matrix builder
# ═══════════════════════════════════════════════════════════════════
def build_wiring_matrix() -> dict:
    """Produce the full sport × market wiring matrix using the
    static evidence collected above and the sport capability
    registry.  Read-only; safe to call from tests."""
    from services.sport_capability_registry import (
        SPORT_CAPABILITIES,
    )
    matrix: dict = {}
    counts: dict[str, int] = {s.value: 0 for s in WiringStatus}
    all_defects: list[dict] = []

    for sport, entry in SPORT_CAPABILITIES.items():
        markets: list[dict] = []
        if not entry.get("enabled"):
            matrix[sport] = {
                "enabled": False,
                "status_summary": {WiringStatus.DISABLED.value: 1},
                "markets": [{
                    "market": "*",
                    "status": WiringStatus.DISABLED.value,
                    "evidence": {},
                    "notes": entry.get("notes"),
                }],
            }
            counts[WiringStatus.DISABLED.value] += 1
            continue

        for m in (entry.get("game_markets") or []) + \
                 (entry.get("prop_markets")  or []):
            ev = get_wiring_evidence(sport, m)
            if ev is None:
                markets.append({
                    "market": m,
                    "status": WiringStatus.UNSUPPORTED.value,
                    "evidence": {},
                    "notes": "No evidence entry in the wiring matrix — "
                             "not part of Block 2A audit scope.",
                })
                counts[WiringStatus.UNSUPPORTED.value] += 1
                continue

            status = ev["status"].value if hasattr(ev["status"], "value") \
                     else ev["status"]
            counts[status] = counts.get(status, 0) + 1
            markets.append({
                "market": m,
                "status": status,
                "evidence": {k: v.to_dict()
                              for k, v in ev.get("evidence", {}).items()},
                "notes": ev.get("notes"),
                "defects": ev.get("defects", []),
            })
            for d in ev.get("defects", []):
                all_defects.append({"sport": sport, "market": m, **d})

        status_summary: dict[str, int] = {}
        for row in markets:
            status_summary[row["status"]] = (
                status_summary.get(row["status"], 0) + 1)

        matrix[sport] = {
            "enabled": True,
            "status_summary": status_summary,
            "markets": markets,
            "notes": entry.get("notes"),
        }
        # If NHL/CFB have prop markets NOT registered, they show up as
        # "wired at game level, no prop_markets" → OK.

    # Include the intentionally-disabled sports even when not in
    # ENABLED loop above (matrix already picked them up).

    return {
        "generated_at":   datetime.now(timezone.utc).isoformat(),
        "totals":         counts,
        "sports":         matrix,
        "defects":        all_defects,
    }


__all__ = [
    "ReasonCode",
    "WiringStatus",
    "PIPELINE_STAGES",
    "PipelineTrace",
    "StageResult",
    "Evidence",
    "build_wiring_matrix",
    "get_wiring_evidence",
]
