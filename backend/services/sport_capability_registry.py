"""Authoritative Sport Capability Registry — Phase 1 (2026-08-11).

Single source of truth for what markets / features each sport
supports end-to-end.  Every board/filter/endpoint must consult this
module rather than maintaining its own scattered list.

Contract for each sport entry:

    enabled            : bool
        False disables the sport at every ingest/board layer.  WNBA
        and KBO are intentionally disabled — do not re-enable without
        a full product decision.

    game_markets       : list[str]
        Bulk /odds catalog markets that pass through the primary path.

    prop_markets       : list[str]
        Event-level player-prop markets that the Odds API supports and
        we've wired end-to-end (feature engine → scoring → publication).

    fallback_sources   : list[str]
        Secondary/free sources that can populate picks when the
        primary path returns nothing.  All fallbacks feed the SAME
        canonical publication pipeline (no isolated board paths).

    supports_alt_lines : bool
    supports_locks     : bool  # eligible for the strict >85 Locks board
    notes              : str   # human-readable summary

The registry does NOT drive scoring formulas or Lock Score behaviour
— it strictly describes wiring/capabilities.
"""
from __future__ import annotations

from typing import Any


SPORT_CAPABILITIES: dict[str, dict[str, Any]] = {
    "MLB": {
        "enabled": True,
        "production_status": "SUPPORTED",   # PHASE 5 §5A
        "game_markets": ["h2h", "spreads", "totals"],
        "prop_markets": [
            "batter_hits", "batter_hits_alternate",
            "batter_hits_runs_rbis", "batter_hits_runs_rbis_alternate",
            "batter_home_runs", "batter_home_runs_alternate",
            "batter_rbis", "batter_rbis_alternate",
            "batter_total_bases", "batter_total_bases_alternate",
            "pitcher_strikeouts", "pitcher_strikeouts_alternate",
            "pitcher_outs",
        ],
        "fallback_sources": [],
        "supports_alt_lines": True,
        "supports_locks": True,
        "notes": ("Full end-to-end — Odds API + Stats API H2H + BvP "
                  "enrichment + Statcast."),
    },
    "NBA": {
        "enabled": True,
        # PHASE 5 (2026-06) — Production status classification per §5A.
        # Overall sport is SUPPORTED because player props travel the full
        # canonical path.  Game markets are MODEL_UNAVAILABLE — no
        # authoritative independent NBA game model has been wired since
        # Phase 1B retired the sportsbook-follow pseudo-model.
        "production_status": "SUPPORTED",
        "market_status": {
            "h2h":     "MODEL_UNAVAILABLE",
            "spreads": "MODEL_UNAVAILABLE",
            "totals":  "MODEL_UNAVAILABLE",
            # Player props all SUPPORTED (see prop_markets list below).
        },
        "game_markets": ["h2h", "spreads", "totals"],
        "prop_markets": [
            "player_points", "player_rebounds", "player_assists",
            "player_points_alternate", "player_rebounds_alternate",
            "player_assists_alternate",
            "player_points_rebounds_assists",
            "player_points_rebounds_assists_alternate",
            "player_points_rebounds", "player_points_assists",
            "player_rebounds_assists",
            "player_threes", "player_threes_alternate",
            "player_steals", "player_blocks",
        ],
        "fallback_sources": [],
        "supports_alt_lines": True,
        "supports_locks": True,
        "notes": ("Props: full end-to-end (feature engine wired). Game "
                  "markets: reachability only — Phase 1B retired the "
                  "sportsbook-follow pseudo-model; game markets record "
                  "MODEL_UNAVAILABLE until an authoritative NBA game "
                  "model is wired."),
    },
    "NFL": {
        "enabled": True,
        "production_status": "SUPPORTED",   # PHASE 5 §5A
        "game_markets": ["h2h", "spreads", "totals"],
        "prop_markets": [
            "player_pass_yds", "player_pass_yds_alternate",
            "player_pass_tds", "player_pass_attempts",
            "player_pass_completions",
            "player_rush_yds", "player_rush_yds_alternate",
            "player_rush_attempts", "player_rush_tds",
            "player_receptions", "player_receptions_alternate",
            "player_reception_yds", "player_reception_yds_alternate",
            "player_reception_tds",
            "player_anytime_td", "player_1st_td",
        ],
        "fallback_sources": [],
        "supports_alt_lines": True,
        "supports_locks": True,
        "notes": ("Phase 1B (2026-06): game markets (ML/Spread/Total, "
                  "regular + preseason) evaluated by the Platinum game "
                  "simulator (team-strength expected margin/total → "
                  "exact-line probabilities). Props: nflverse feature "
                  "engine + Platinum challenger. When team ratings are "
                  "missing the market records MODEL_UNAVAILABLE — no "
                  "sportsbook-follow."),
    },
    "CFB": {
        "enabled": True,
        # PHASE 5 (2026-06) — INTENTIONALLY_DEFERRED per release scope
        # update.  Not a Phase 10 blocker for this release.  Capability
        # code preserved for future re-enablement.
        "production_status": "INTENTIONALLY_DEFERRED",
        "market_status": {
            "h2h":     "MODEL_UNAVAILABLE",
            "spreads": "MODEL_UNAVAILABLE",
            "totals":  "MODEL_UNAVAILABLE",
        },
        "game_markets": ["h2h", "spreads", "totals"],
        "prop_markets": [],   # thin CFB player-prop catalogue on Odds API
        "fallback_sources": [],
        "supports_alt_lines": True,
        "supports_locks": True,
        "notes": ("Market reachability only. Phase 1B: no authoritative "
                  "independent CFB game-market model is wired — markets "
                  "record MODEL_UNAVAILABLE funnel telemetry (legacy "
                  "sportsbook-follow pseudo-modeling retired). Player "
                  "props NOT wired — The Odds API's CFB prop catalogue "
                  "is sparse and unreliable."),
    },
    "Soccer": {
        "enabled": True,
        "production_status": "SUPPORTED",   # PHASE 5 §5A
        "game_markets": ["h2h", "spreads", "totals", "btts", "double_chance"],
        "prop_markets": [
            "player_goal_scorer_anytime",
            "player_to_score_or_assist",
        ],
        # PHASE 5 FIX 1 (2026-06) — FIRST/LAST goal scorer are
        # INTENTIONALLY_UNSUPPORTED per product requirement.  Previous
        # Soccer repair removed them from acquisition/ingest and they
        # MUST NOT be re-advertised as supported.  The unsupported map
        # is authoritative — consumer surfaces read this to honestly
        # render the market as unsupported.
        "unsupported_markets": {
            "player_first_goal_scorer": "INTENTIONALLY_UNSUPPORTED",
            "player_last_goal_scorer":  "INTENTIONALLY_UNSUPPORTED",
        },
        "fallback_sources": [
            "soccer_hot_scorers",     # top-scorer AGS anchor
            "espn_soccer_fixtures",   # ESPN scoreboard fallback
            "uefa_espn_ingest",       # UEFA/CFB double-chance + form-derived ML
            "csl_espn_leaderboard",   # Chinese Super League ESPN feed
        ],
        "supports_alt_lines": True,
        "supports_locks": True,
        "notes": ("Full end-to-end via the canonical path. Phase 1B "
                  "(T1): soccer/pipeline.py duplicate pick emission "
                  "RETIRED (soccer_predictions cache preserved). "
                  "sportdb synthetic scorer picks are research/model "
                  "evidence only — never published as sportsbook-backed "
                  "picks."),
    },
    "Tennis": {
        "enabled": True,
        "production_status": "SUPPORTED",   # PHASE 5 §5A
        "game_markets": ["h2h", "spreads", "totals"],
        "prop_markets": [],   # tennis props not exposed by Odds API
        "fallback_sources": ["tennis_extra"],  # TennisExplorer scrape
        "supports_alt_lines": True,
        "supports_locks": True,
        "notes": ("Phase 1B (R4): primary Odds-API + tennis math engine "
                  "runtime is the sole production authority. "
                  "tennis_extra is a controlled gap-filler ONLY for "
                  "events missing from primary, and gap-fill picks "
                  "must carry a real sportsbook line."),
    },
    "UFC": {
        "enabled": True,
        # PHASE 5 (2026-06) — INTENTIONALLY_DEFERRED per release scope.
        "production_status": "INTENTIONALLY_DEFERRED",
        "market_status": {
            "h2h":    "MODEL_UNAVAILABLE",
            "totals": "MODEL_UNAVAILABLE",
        },
        "game_markets": ["h2h", "totals"],   # rounds totals + ML
        "prop_markets": [],  # confirmed no MMA props on Odds API
        "fallback_sources": ["ufc_espn_ingest"],
        "supports_alt_lines": False,
        "supports_locks": True,
        "notes": ("Phase 1B (T3b): legacy _ufc_ml_only suppression "
                  "retired — ML + real totals both REACH evaluation. "
                  "No authoritative independent UFC model is wired yet, "
                  "so both markets currently record MODEL_UNAVAILABLE "
                  "(never sportsbook-follow)."),
    },
    "NHL": {
        "enabled": True,
        # PHASE 5 (2026-06) — INTENTIONALLY_DEFERRED per release scope.
        "production_status": "INTENTIONALLY_DEFERRED",
        "market_status": {
            "h2h":     "MODEL_UNAVAILABLE",
            "spreads": "MODEL_UNAVAILABLE",
            "totals":  "MODEL_UNAVAILABLE",
        },
        "game_markets": ["h2h", "spreads", "totals"],
        "prop_markets": [],   # not yet wired
        "fallback_sources": [],
        "supports_alt_lines": False,
        "supports_locks": True,
        "notes": ("Phase 1B (R2a): icehockey_nhl WIRED into production "
                  "generation (fetch_nhl_picks → canonical path). Real "
                  "ML / puck-line / total markets reach evaluation; no "
                  "authoritative independent NHL model exists yet, so "
                  "markets record MODEL_UNAVAILABLE funnel telemetry — "
                  "probability/edge are never fabricated."),
    },
    # ── Intentionally disabled ─────────────────────────────────────
    "WNBA": {
        "enabled": False,
        "game_markets": [], "prop_markets": [], "fallback_sources": [],
        "supports_alt_lines": False,
        "supports_locks": False,
        "notes": "Disabled — see product decision from 2026-06-18.",
    },
    "KBO": {
        "enabled": False,
        "game_markets": [], "prop_markets": [], "fallback_sources": [],
        "supports_alt_lines": False,
        "supports_locks": False,
        "notes": "Disabled — removed 2026-06-18.",
    },
}


def is_enabled(sport: str) -> bool:
    entry = SPORT_CAPABILITIES.get(sport) or {}
    return bool(entry.get("enabled"))


def prop_markets_for(sport: str) -> list[str]:
    entry = SPORT_CAPABILITIES.get(sport) or {}
    return list(entry.get("prop_markets") or [])


def game_markets_for(sport: str) -> list[str]:
    entry = SPORT_CAPABILITIES.get(sport) or {}
    return list(entry.get("game_markets") or [])


def enabled_sports() -> list[str]:
    return [s for s, e in SPORT_CAPABILITIES.items() if e.get("enabled")]


def supports_locks(sport: str) -> bool:
    entry = SPORT_CAPABILITIES.get(sport) or {}
    return bool(entry.get("supports_locks"))


# ─────────────────────────────────────────────────────────────────────
# PHASE 5 (2026-06) — Production-status helpers per §5A.
#
# Contract: consumer surfaces MUST call these to render honest
# capability strings rather than checking ``enabled`` alone.  A sport
# with ``enabled=True`` but ``production_status="INTENTIONALLY_DEFERRED"``
# is preserved in the codebase but MUST NOT appear as production-ready
# in any UI badge / capability API / filter enumeration.
#
# Valid values:
#   SUPPORTED
#   PROVIDER_UNAVAILABLE
#   MODEL_UNAVAILABLE
#   INTENTIONALLY_UNSUPPORTED
#   INTENTIONALLY_DEFERRED     (current release only — not permanent)
# ─────────────────────────────────────────────────────────────────────
VALID_PRODUCTION_STATUSES: frozenset[str] = frozenset({
    "SUPPORTED", "PROVIDER_UNAVAILABLE", "MODEL_UNAVAILABLE",
    "INTENTIONALLY_UNSUPPORTED", "INTENTIONALLY_DEFERRED",
})


def production_status(sport: str) -> str:
    """Return the honest production status for `sport`.  Defaults to
    ``MODEL_UNAVAILABLE`` when the registry lacks an explicit tag."""
    entry = SPORT_CAPABILITIES.get(sport) or {}
    status = entry.get("production_status")
    if status in VALID_PRODUCTION_STATUSES:
        return status
    # Legacy entries without an explicit status default to
    # MODEL_UNAVAILABLE (honest — we haven't classified them yet).
    return "MODEL_UNAVAILABLE"


def market_production_status(sport: str, market_key: str) -> str:
    """Return the honest per-market production status.  Falls back to
    the sport-level status when a per-market override is absent.

    PHASE 5 FIX 1 (2026-06) — checks ``unsupported_markets`` FIRST so
    an intentionally-unsupported prop (e.g. Soccer first/last goal
    scorer) cannot inherit the sport-level SUPPORTED status.
    """
    entry = SPORT_CAPABILITIES.get(sport) or {}
    unsupported = entry.get("unsupported_markets") or {}
    if market_key in unsupported:
        val = unsupported[market_key]
        if val in VALID_PRODUCTION_STATUSES:
            return val
        return "INTENTIONALLY_UNSUPPORTED"
    market_status = entry.get("market_status") or {}
    override = market_status.get(market_key)
    if override in VALID_PRODUCTION_STATUSES:
        return override
    return production_status(sport)


def core_release_sports() -> list[str]:
    """The current five sports required for production certification."""
    return ["MLB", "NFL", "NBA", "Soccer", "Tennis"]


def is_production_ready(sport: str) -> bool:
    """True iff the sport can be advertised as production-ready on
    consumer surfaces.  Deferred / model-unavailable / provider-
    unavailable sports return False."""
    return production_status(sport) == "SUPPORTED"


def capability_matrix() -> dict[str, dict[str, Any]]:
    """Return a shallow copy of the registry for read-only consumers."""
    return {k: dict(v) for k, v in SPORT_CAPABILITIES.items()}


__all__ = [
    "SPORT_CAPABILITIES",
    "is_enabled",
    "prop_markets_for",
    "game_markets_for",
    "enabled_sports",
    "supports_locks",
    "capability_matrix",
    # PHASE 5 (2026-06) — production-status helpers
    "VALID_PRODUCTION_STATUSES",
    "production_status",
    "market_production_status",
    "core_release_sports",
    "is_production_ready",
]
