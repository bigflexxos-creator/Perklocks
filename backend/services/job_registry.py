"""job_registry — Phase 2β source-of-truth job inventory.

Every recurring or expensive background job identified in the
Phase 2α audit is registered here.  This is a **declarative** file
consumed by:

  • Admin observability endpoints — display cadence, providers,
    credit estimates, migration status.
  • Phase 2γ cutover work — the JobCoordinator + ProviderBudget
    wiring will iterate over this registry to migrate loops.

Nothing in this file schedules or removes work.  The Phase 2β
constraints explicitly forbid removing existing loops.
"""
from __future__ import annotations

from typing import Any

# ── Migration status vocabulary ──────────────────────────────────────
MIGRATION_NOT_STARTED = "not_started"      # still owns its own loop
MIGRATION_SHADOW      = "shadow"           # instrumented but decisions logged only
MIGRATION_LEASED      = "leased"           # runs under JobCoordinator lease
MIGRATION_BUDGETED    = "budgeted"         # goes through ProviderBudget
MIGRATION_FULL        = "fully_managed"    # leased + budgeted + registry-driven

# ── Provider names ────────────────────────────────────────────────────
PROV_ODDS_API      = "odds_api"
PROV_FOOTBALL_DATA = "football_data_org"
PROV_API_SPORTS    = "api_sports"
PROV_PROPLINE      = "propline"
PROV_ESPN          = "espn"          # free
PROV_SPORTDB       = "sportdb"       # free tier
PROV_OPENWEATHER   = "openweather"   # free tier


# Registry entry schema:
#   job_name              — unique key
#   entrypoint            — module.path:callable (informational)
#   current_cadence       — human-readable
#   intended_cadence      — human-readable
#   paid_providers        — list[str] (charged services)
#   free_providers        — list[str]
#   estimated_max_credits — per run, upper bound
#   min_interval_seconds  — coordinator-enforced cooldown
#   lease_seconds         — default lease duration
#   timeout_seconds       — soft target for the job body
#   retry_policy          — {mode: "linear|expo", base: int, max: int}
#   emergency_eligible    — bool, may this job request emergency reserve?
#   migration_status      — one of the constants above
#   notes                 — free-form audit note

JOB_REGISTRY: dict[str, dict[str, Any]] = {
    # ── Odds API (paid) ─────────────────────────────────────────────
    "alt_lines_feed": {
        "entrypoint":            "alt_lines_feed:refresh_alt_lines",
        "current_cadence":       "3×/day (12/18/23 UTC)",
        "intended_cadence":      "3×/day (12/18/23 UTC)",
        "paid_providers":        [PROV_ODDS_API],
        "free_providers":        [],
        "estimated_max_credits": 400,
        "min_interval_seconds":  1800,
        "lease_seconds":         600,
        "timeout_seconds":       300,
        "retry_policy":          {"mode": "linear", "base": 600, "max": 3},
        "emergency_eligible":    False,
        "migration_status":      MIGRATION_FULL,
        "notes": "Picks-scope-only alt-line snapshot. Phase 2γ: lease + "
                 "budget gated, no startup burst.",
    },
    "mls_direct_inject": {
        "entrypoint":            "services.mls_direct_inject:run_once",
        "current_cadence":       "3×/day (12/18/23 UTC)",
        "intended_cadence":      "3×/day (12/18/23 UTC)",
        "paid_providers":        [PROV_ODDS_API],
        "free_providers":        [PROV_ESPN],
        "estimated_max_credits": 100,
        "min_interval_seconds":  1800,
        "lease_seconds":         600,
        "timeout_seconds":       300,
        "retry_policy":          {"mode": "linear", "base": 600, "max": 3},
        "emergency_eligible":    False,
        "migration_status":      MIGRATION_FULL,
        "notes": "MLS scorer direct-inject bypass — Phase 2γ full lease + budget.",
    },
    "soccer_prop_inject": {
        "entrypoint":            "services.soccer_prop_inject:run_once",
        "current_cadence":       "3×/day (12/18/23 UTC)",
        "intended_cadence":      "3×/day (12/18/23 UTC)",
        "paid_providers":        [PROV_ODDS_API],
        "free_providers":        [PROV_ESPN, PROV_SPORTDB],
        "estimated_max_credits": 200,
        "min_interval_seconds":  1800,
        "lease_seconds":         600,
        "timeout_seconds":       300,
        "retry_policy":          {"mode": "linear", "base": 600, "max": 3},
        "emergency_eligible":    False,
        "migration_status":      MIGRATION_FULL,
        "notes": "Big-5 + UCL prop-injects — Phase 2γ full lease + budget.",
    },
    "picks_refresh_today": {
        "entrypoint":            "server:_refresh_picks",
        "current_cadence":       "on-demand (admin, cron, cold-start)",
        "intended_cadence":      "3× per UTC day + admin push (Phase 2γ snapshot mode)",
        "paid_providers":        [PROV_ODDS_API],
        "free_providers":        [PROV_ESPN, PROV_SPORTDB],
        "estimated_max_credits": 800,
        "min_interval_seconds":  900,
        "lease_seconds":         900,
        "timeout_seconds":       600,
        "retry_policy":          {"mode": "expo", "base": 60, "max": 3},
        "emergency_eligible":    True,
        "migration_status":      MIGRATION_LEASED,
        "notes": "Admin force-refresh route goes through JobCoordinator + "
                 "ProviderBudget.  Normal-user /picks/refresh no longer "
                 "triggers paid work.  Phase 2γ: ODDS_GLOBAL_REFRESH_MODE "
                 "controls snapshot vs legacy_hourly cadence.",
    },
    "mlb_pregame_refresh_today": {
        "entrypoint":            "server:_mlb_pregame_loop (today branch)",
        "current_cadence":       "every 5 min during 15:00-03:00 UTC",
        "intended_cadence":      "every 5 min during window (lease-gated)",
        "paid_providers":        [PROV_ODDS_API],
        "free_providers":        [PROV_ESPN, PROV_SPORTDB],
        "estimated_max_credits": 60,
        "min_interval_seconds":  180,
        "lease_seconds":         180,
        "timeout_seconds":       120,
        "retry_policy":          {"mode": "linear", "base": 120, "max": 2},
        "emergency_eligible":    False,
        "migration_status":      MIGRATION_LEASED,
        "notes": "Phase 2γ: preserved 5-min cadence for near-start games "
                 "(user-visible value) but now coordinator-gated so "
                 "duplicate workers don't fan out.",
    },
    "mlb_pregame_refresh_tomorrow": {
        "entrypoint":            "server:_mlb_pregame_loop (tomorrow branch)",
        "current_cadence":       "every 5 min (legacy)",
        "intended_cadence":      "every 30 min (Phase 2γ)",
        "paid_providers":        [PROV_ODDS_API],
        "free_providers":        [PROV_ESPN, PROV_SPORTDB],
        "estimated_max_credits": 40,
        "min_interval_seconds":  1800,
        "lease_seconds":         180,
        "timeout_seconds":       120,
        "retry_policy":          {"mode": "linear", "base": 300, "max": 2},
        "emergency_eligible":    False,
        "migration_status":      MIGRATION_LEASED,
        "notes": "Phase 2γ: tomorrow was previously refreshed every 5 min "
                 "alongside today.  Reduced to 30-min cadence — books post "
                 "next-day props hours in advance so tight polling is waste.",
    },

    # ── Free-provider recurring jobs (registered for observability) ─
    "csl_espn_live": {
        "entrypoint":            "csl_espn_live:arm_scheduler",
        "current_cadence":       "every 12h",
        "intended_cadence":      "every 12h",
        "paid_providers":        [],
        "free_providers":        [PROV_ESPN],
        "estimated_max_credits": 0,
        "min_interval_seconds":  6 * 3600,
        "lease_seconds":         600,
        "timeout_seconds":       300,
        "retry_policy":          {"mode": "linear", "base": 600, "max": 3},
        "emergency_eligible":    False,
        "migration_status":      MIGRATION_NOT_STARTED,
        "notes": "Retired-player filter for CSL scorers.  Free source.",
    },
    "services_ingest_loop": {
        "entrypoint":            "server:_services_loop",
        "current_cadence":       "long-running gather()",
        "intended_cadence":      "per-sport lease + coordinator schedule",
        "paid_providers":        [],
        "free_providers":        [PROV_ESPN, PROV_SPORTDB],
        "estimated_max_credits": 0,
        "min_interval_seconds":  3600,
        "lease_seconds":         1800,
        "timeout_seconds":       1500,
        "retry_policy":          {"mode": "linear", "base": 900, "max": 3},
        "emergency_eligible":    False,
        "migration_status":      MIGRATION_NOT_STARTED,
        "notes": "NBA/NFL/soccer/CFB active-player registry ingest.",
    },
    "mls_matchup_history": {
        "entrypoint":            "services.mls_player_matchup_history:refresh_all",
        "current_cadence":       "weekly",
        "intended_cadence":      "weekly",
        "paid_providers":        [],
        "free_providers":        [PROV_ESPN],
        "estimated_max_credits": 0,
        "min_interval_seconds":  6 * 24 * 3600,
        "lease_seconds":         1800,
        "timeout_seconds":       1500,
        "retry_policy":          {"mode": "linear", "base": 3600, "max": 3},
        "emergency_eligible":    False,
        "migration_status":      MIGRATION_NOT_STARTED,
        "notes": "MLS BvP history refresh.",
    },
}


def list_jobs() -> list[dict[str, Any]]:
    """Return the registry as a list of dicts including ``job_name``."""
    return [{"job_name": k, **v} for k, v in JOB_REGISTRY.items()]


def get_job(job_name: str) -> dict[str, Any] | None:
    v = JOB_REGISTRY.get(job_name)
    if v is None:
        return None
    return {"job_name": job_name, **v}


def paid_jobs() -> list[dict[str, Any]]:
    return [j for j in list_jobs() if j.get("paid_providers")]


__all__ = [
    "JOB_REGISTRY", "list_jobs", "get_job", "paid_jobs",
    "MIGRATION_NOT_STARTED", "MIGRATION_SHADOW", "MIGRATION_LEASED",
    "MIGRATION_BUDGETED", "MIGRATION_FULL",
    "PROV_ODDS_API", "PROV_FOOTBALL_DATA", "PROV_API_SPORTS",
    "PROV_PROPLINE", "PROV_ESPN", "PROV_SPORTDB", "PROV_OPENWEATHER",
]
