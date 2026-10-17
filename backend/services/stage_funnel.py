"""Real stage-true funnel telemetry (B2 — items 34-42).

Replaces the generic ``funnel_telemetry.record(...)`` single-loop
instrumentation with per-sport × per-market-family stage counters
that are populated AT THE ACTUAL RUNTIME STAGES — never inferred
from one loop after the fact.

Required stages (per the directive):

    provider_events          — item 35 : actual discovered/acquired provider events
    provider_outcomes        — item 36 : sportsbook market outcomes at consumption point
    source_wagers            — item 37 : exact SourceWager objects (post-validation)
    normalized_rows          — item 38 : provider row mapped into Perklocks schema
    identity_resolved        — item 38 : canonical event/player/team identity verified
    history_ready            — item 39 : REAL model has enough usable inputs
    model_attempts           — item 40 : actual model function invoked
    model_candidates         — item 40 : model returned valid independent probability
    at_or_above_85           — item 41 : final pre-publication Lock Score met floor
    publication_attempts     — item 41 : sent through canonical publication boundary
    published                — item 41 : canonical publication succeeded
    api_visible              — item 41 : actually readable by /api/picks/today
    rejections               — item 42 : {exact_reason: count}

Each counter is isolated per (sport, market_family) tuple so a single
tally never masks a per-family zero (item 42).  Zero-truth: when
``provider_events > 0 AND provider_outcomes = 0`` the funnel report
surfaces an acquisition/market problem *explicitly* — not a silent
``no 85+`` state.
"""
from __future__ import annotations

import threading
from typing import Any, Optional


# ── Stage names ─────────────────────────────────────────────────────
STAGE_PROVIDER_EVENTS      = "provider_events"
STAGE_PROVIDER_OUTCOMES    = "provider_outcomes"
STAGE_SOURCE_WAGERS        = "source_wagers"
STAGE_NORMALIZED_ROWS      = "normalized_rows"
STAGE_IDENTITY_RESOLVED    = "identity_resolved"
STAGE_HISTORY_READY        = "history_ready"
STAGE_MODEL_ATTEMPTS       = "model_attempts"
STAGE_MODEL_CANDIDATES     = "model_candidates"
STAGE_AT_OR_ABOVE_85       = "at_or_above_85"
STAGE_PUBLICATION_ATTEMPTS = "publication_attempts"
STAGE_PUBLISHED            = "published"
STAGE_API_VISIBLE          = "api_visible"

ALL_STAGES: tuple[str, ...] = (
    STAGE_PROVIDER_EVENTS, STAGE_PROVIDER_OUTCOMES,
    STAGE_SOURCE_WAGERS, STAGE_NORMALIZED_ROWS,
    STAGE_IDENTITY_RESOLVED, STAGE_HISTORY_READY,
    STAGE_MODEL_ATTEMPTS, STAGE_MODEL_CANDIDATES,
    STAGE_AT_OR_ABOVE_85, STAGE_PUBLICATION_ATTEMPTS,
    STAGE_PUBLISHED, STAGE_API_VISIBLE,
)

# ── Reason-code taxonomy (item 38) ───────────────────────────────────
REASON_IDENTITY_PLAYER_UNRESOLVED       = "IDENTITY_PLAYER_UNRESOLVED"
REASON_IDENTITY_TEAM_UNRESOLVED         = "IDENTITY_TEAM_UNRESOLVED"
REASON_IDENTITY_EVENT_UNRESOLVED        = "IDENTITY_EVENT_UNRESOLVED"
REASON_NORMALIZATION_MARKET_UNSUPPORTED = "NORMALIZATION_MARKET_UNSUPPORTED"
# Model (item 40).
REASON_MODEL_UNAVAILABLE                = "MODEL_UNAVAILABLE"
REASON_INSUFFICIENT_HISTORY             = "INSUFFICIENT_HISTORY"
REASON_IDENTITY_LAG                     = "IDENTITY_LAG"
REASON_NORMALIZATION_LAG                = "NORMALIZATION_LAG"
REASON_MODEL_ERROR                      = "MODEL_ERROR"
# Publication (item 41).
REASON_SYNTHETIC_BOOK_ODDS              = "SYNTHETIC_BOOK_ODDS"
REASON_NO_REAL_LINE_WITH_ODDS           = "NO_REAL_LINE_WITH_ODDS"
REASON_LINEAGE_MISSING_PROVIDER_EVENT_ID = "LINEAGE_MISSING_PROVIDER_EVENT_ID"
# Scheduler (B3, item 47).
REASON_SPORT_REFRESH_STARVED            = "SPORT_REFRESH_STARVED"


# ── Thread-safe in-memory store (per cycle) ──────────────────────────
_LOCK = threading.Lock()
_STAGES: dict[tuple[str, str], dict[str, int]] = {}
_REJECTIONS: dict[tuple[str, str], dict[str, int]] = {}


def _ns(sport: str, family: Optional[str]) -> tuple[str, str]:
    s = (sport or "unknown").strip()
    f = (family or "*").strip() or "*"
    return (s, f)


# ─────────────────────────────────────────────────────────────────────
# Public API — producers call these at the exact runtime stage
# ─────────────────────────────────────────────────────────────────────
def record_stage(sport: str, family: Optional[str],
                 stage: str, delta: int = 1) -> None:
    """Increment ``stage`` counter for ``(sport, family)``."""
    key = _ns(sport, family)
    with _LOCK:
        bucket = _STAGES.setdefault(key, {s: 0 for s in ALL_STAGES})
        if stage not in bucket:
            bucket[stage] = 0
        bucket[stage] += int(delta)


def record_rejection(sport: str, family: Optional[str],
                     reason: str, delta: int = 1) -> None:
    """Count a rejection with its exact reason code."""
    key = _ns(sport, family)
    with _LOCK:
        bucket = _REJECTIONS.setdefault(key, {})
        bucket[reason] = bucket.get(reason, 0) + int(delta)


def reset(sport: Optional[str] = None,
          family: Optional[str] = None) -> None:
    """Reset counters (per-sport or wholesale)."""
    with _LOCK:
        if sport is None and family is None:
            _STAGES.clear()
            _REJECTIONS.clear()
            return
        for key in list(_STAGES.keys()):
            if sport and key[0] != sport:
                continue
            if family and key[1] != family:
                continue
            _STAGES.pop(key, None)
            _REJECTIONS.pop(key, None)


# ─────────────────────────────────────────────────────────────────────
# Report API
# ─────────────────────────────────────────────────────────────────────
def report(sport: Optional[str] = None,
           family: Optional[str] = None) -> dict[str, Any]:
    """Return the current funnel snapshot.

    Shape::

        {
          "sports": {
            "MLB": {
              "families": {
                "batter_hits": {
                   "provider_events": 42, "provider_outcomes": 310,
                   "source_wagers": 300, ..., "api_visible": 7,
                   "rejections": {"SYNTHETIC_BOOK_ODDS": 2, ...}
                },
                "*": { ... }
              },
              "total": { aggregated stages across families }
            },
            ...
          }
        }
    """
    with _LOCK:
        snapshot_stages = {k: dict(v) for k, v in _STAGES.items()}
        snapshot_rej = {k: dict(v) for k, v in _REJECTIONS.items()}

    sports: dict[str, dict[str, Any]] = {}
    for (sp, fam), bucket in snapshot_stages.items():
        if sport and sp != sport:
            continue
        if family and fam != family:
            continue
        s = sports.setdefault(sp, {"families": {}, "total":
                                     {s: 0 for s in ALL_STAGES}})
        fam_entry = dict(bucket)
        fam_entry["rejections"] = snapshot_rej.get((sp, fam), {})
        s["families"][fam] = fam_entry
        for st, v in bucket.items():
            s["total"][st] = s["total"].get(st, 0) + v
    return {"sports": sports}


def zero_truth_gaps(sport: Optional[str] = None) -> list[dict[str, Any]]:
    """Return per-(sport, family) gap diagnostics (item 42).

    Each entry describes an UNEXPLAINED zero that the operator must
    investigate: an upstream stage is non-zero but a downstream stage
    (one it logically feeds) is zero.
    """
    rep = report(sport=sport)
    gaps: list[dict[str, Any]] = []
    # Logical adjacency: upstream stage → downstream stage.
    adj = [
        (STAGE_PROVIDER_EVENTS,      STAGE_PROVIDER_OUTCOMES),
        (STAGE_PROVIDER_OUTCOMES,    STAGE_SOURCE_WAGERS),
        (STAGE_SOURCE_WAGERS,        STAGE_NORMALIZED_ROWS),
        (STAGE_NORMALIZED_ROWS,      STAGE_IDENTITY_RESOLVED),
        (STAGE_IDENTITY_RESOLVED,    STAGE_HISTORY_READY),
        (STAGE_HISTORY_READY,        STAGE_MODEL_ATTEMPTS),
        (STAGE_MODEL_ATTEMPTS,       STAGE_MODEL_CANDIDATES),
        (STAGE_MODEL_CANDIDATES,     STAGE_AT_OR_ABOVE_85),
        (STAGE_AT_OR_ABOVE_85,       STAGE_PUBLICATION_ATTEMPTS),
        (STAGE_PUBLICATION_ATTEMPTS, STAGE_PUBLISHED),
        (STAGE_PUBLISHED,            STAGE_API_VISIBLE),
    ]
    for sp, s_entry in rep.get("sports", {}).items():
        for fam, fam_entry in s_entry.get("families", {}).items():
            for up, down in adj:
                if fam_entry.get(up, 0) > 0 and fam_entry.get(down, 0) == 0:
                    gaps.append({
                        "sport": sp, "family": fam,
                        "upstream_stage":   up,
                        "upstream_count":   fam_entry.get(up, 0),
                        "downstream_stage": down,
                        "downstream_count": 0,
                        "rejections":       fam_entry.get("rejections") or {},
                    })
    return gaps


__all__ = [
    # Stage names.
    "STAGE_PROVIDER_EVENTS", "STAGE_PROVIDER_OUTCOMES",
    "STAGE_SOURCE_WAGERS", "STAGE_NORMALIZED_ROWS",
    "STAGE_IDENTITY_RESOLVED", "STAGE_HISTORY_READY",
    "STAGE_MODEL_ATTEMPTS", "STAGE_MODEL_CANDIDATES",
    "STAGE_AT_OR_ABOVE_85", "STAGE_PUBLICATION_ATTEMPTS",
    "STAGE_PUBLISHED", "STAGE_API_VISIBLE",
    "ALL_STAGES",
    # Reasons.
    "REASON_IDENTITY_PLAYER_UNRESOLVED",
    "REASON_IDENTITY_TEAM_UNRESOLVED",
    "REASON_IDENTITY_EVENT_UNRESOLVED",
    "REASON_NORMALIZATION_MARKET_UNSUPPORTED",
    "REASON_MODEL_UNAVAILABLE", "REASON_INSUFFICIENT_HISTORY",
    "REASON_IDENTITY_LAG", "REASON_NORMALIZATION_LAG",
    "REASON_MODEL_ERROR",
    "REASON_SYNTHETIC_BOOK_ODDS", "REASON_NO_REAL_LINE_WITH_ODDS",
    "REASON_LINEAGE_MISSING_PROVIDER_EVENT_ID",
    "REASON_SPORT_REFRESH_STARVED",
    # API.
    "record_stage", "record_rejection", "reset",
    "report", "zero_truth_gaps",
]
