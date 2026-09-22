"""Replaceable enrichment adapters — Weather & Availability.

Contract: adapters return a *report* dataclass with a status field.
The universal engine consumes those reports as OPTIONAL features that
modify distribution parameters or confidence, NEVER as admission gates.

WEATHER_STATUS values
    AVAILABLE     — provider returned trustworthy game-time conditions
    UNAVAILABLE   — no source could provide data; engine uses NEUTRAL modifier

INJURY_STATUS values
    AVAILABLE     — authoritative feed returned status for the player
    PARTIAL       — feed available but this player not on the report
    UNAVAILABLE   — feed itself is unreachable

No synthetic values are ever manufactured.  UNAVAILABLE simply means
"no signal" — the engine keeps its neutral prior.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol, Any


WEATHER_STATUS_AVAILABLE   = "AVAILABLE"
WEATHER_STATUS_UNAVAILABLE = "UNAVAILABLE"

INJURY_STATUS_AVAILABLE   = "AVAILABLE"
INJURY_STATUS_PARTIAL     = "PARTIAL"
INJURY_STATUS_UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class WeatherReport:
    status: str
    is_indoor: Optional[bool] = None
    roof_status: Optional[str] = None       # open / closed / retractable_unknown
    temperature_f: Optional[float] = None
    wind_mph: Optional[float] = None
    wind_gust_mph: Optional[float] = None
    precip_probability: Optional[float] = None
    precip_type: Optional[str] = None
    humidity: Optional[float] = None
    provenance: Optional[str] = None
    snapshot_at: Optional[str] = None


@dataclass(frozen=True)
class AvailabilityReport:
    status: str
    player_name: Optional[str] = None
    designation: Optional[str] = None      # OUT / DOUBTFUL / QUESTIONABLE / LIMITED / FULL / IR / INACTIVE / None
    availability_probability: Optional[float] = None   # 0..1
    role_uncertainty: Optional[float] = None           # 0..1  (higher = wider distribution)
    is_inactive: Optional[bool] = None
    provenance: Optional[str] = None
    snapshot_at: Optional[str] = None


class WeatherProvider(Protocol):
    async def report_for_game(self, db, game: dict) -> WeatherReport: ...


class AvailabilityProvider(Protocol):
    async def report_for_player(self, db, *, player_name: str,
                                team: str, sport: str = "nfl") -> AvailabilityReport: ...


# ─────────────────────────────────────────────────────────────────────
# DEFAULT IMPLEMENTATIONS — reuse existing PerkLocks feeds
# ─────────────────────────────────────────────────────────────────────

class DefaultWeatherProvider:
    """Uses whatever weather/stadium data already lives on the game doc.

    PerkLocks does not currently ingest NFL weather — this adapter is
    intentionally minimal and will return WEATHER_STATUS_UNAVAILABLE
    for the vast majority of games.  When a real provider is added
    later, replace this class or subclass it — the probability
    architecture stays unchanged.
    """

    async def report_for_game(self, db, game: dict) -> WeatherReport:
        # Look for any pre-existing indoor/dome/roof marker on the doc.
        # Also honour a doc-level ``weather`` block if a future ingest
        # decides to attach one.
        w = (game or {}).get("weather") or {}
        indoor_flag = (game or {}).get("is_indoor") or w.get("is_indoor")
        roof = (game or {}).get("roof_status") or w.get("roof_status")
        if w:
            return WeatherReport(
                status=WEATHER_STATUS_AVAILABLE,
                is_indoor=bool(indoor_flag) if indoor_flag is not None else None,
                roof_status=roof,
                temperature_f=w.get("temperature_f"),
                wind_mph=w.get("wind_mph"),
                wind_gust_mph=w.get("wind_gust_mph"),
                precip_probability=w.get("precip_probability"),
                precip_type=w.get("precip_type"),
                humidity=w.get("humidity"),
                provenance=w.get("provenance") or "game_doc",
                snapshot_at=w.get("snapshot_at"),
            )
        if indoor_flag is not None or roof:
            return WeatherReport(
                status=WEATHER_STATUS_AVAILABLE,
                is_indoor=bool(indoor_flag) if indoor_flag is not None else None,
                roof_status=roof,
                provenance="game_doc.indoor_flag",
            )
        return WeatherReport(status=WEATHER_STATUS_UNAVAILABLE)


class DefaultAvailabilityProvider:
    """Consumes ``services.espn_injury_notes`` — the existing NFL
    injury/inactive feed that PerkLocks already ingests.
    """

    async def report_for_player(self, db, *, player_name: str,
                                team: str, sport: str = "nfl") -> AvailabilityReport:
        try:
            from services.espn_injury_notes import get_team_injuries
        except Exception:
            return AvailabilityReport(status=INJURY_STATUS_UNAVAILABLE,
                                       player_name=player_name,
                                       provenance="espn_injury_notes:import_error")
        try:
            entries = await get_team_injuries(db, sport, team)
        except Exception:
            return AvailabilityReport(status=INJURY_STATUS_UNAVAILABLE,
                                       player_name=player_name,
                                       provenance="espn_injury_notes:query_error")
        if not entries:
            # Feed is up but this team has no entries — could mean fully
            # healthy OR feed hasn't refreshed for this team.  Report as
            # PARTIAL so callers know we didn't get an explicit signal.
            return AvailabilityReport(status=INJURY_STATUS_PARTIAL,
                                       player_name=player_name,
                                       provenance="espn_injury_notes:team_empty")
        needle = (player_name or "").strip().lower()
        for e in entries:
            n = (e.get("player") or e.get("athlete") or e.get("name") or "").strip().lower()
            if needle and needle in n:
                designation = (e.get("status") or e.get("designation") or "").upper() or None
                # Map designations to availability probability.
                probability = _designation_to_probability(designation)
                return AvailabilityReport(
                    status=INJURY_STATUS_AVAILABLE,
                    player_name=player_name,
                    designation=designation,
                    availability_probability=probability,
                    role_uncertainty=_designation_to_role_uncertainty(designation),
                    is_inactive=(designation in ("OUT", "IR", "INACTIVE")),
                    provenance="espn_injury_notes",
                    snapshot_at=e.get("updated_at") or e.get("snapshot_at"),
                )
        # Player not on the report — most likely healthy but we don't
        # explicitly know.  PARTIAL is honest.
        return AvailabilityReport(status=INJURY_STATUS_PARTIAL,
                                   player_name=player_name,
                                   availability_probability=None,
                                   role_uncertainty=0.0,
                                   provenance="espn_injury_notes:not_listed")


def _designation_to_probability(d: Optional[str]) -> Optional[float]:
    if not d:
        return None
    d = d.upper()
    return {
        "OUT": 0.0, "IR": 0.0, "INACTIVE": 0.0,
        "DOUBTFUL": 0.25, "QUESTIONABLE": 0.65,
        "LIMITED": 0.85, "FULL": 0.95, "PROBABLE": 0.95,
    }.get(d)


def _designation_to_role_uncertainty(d: Optional[str]) -> Optional[float]:
    if not d:
        return 0.0
    d = d.upper()
    return {
        "OUT": 1.0, "IR": 1.0, "INACTIVE": 1.0,
        "DOUBTFUL": 0.6, "QUESTIONABLE": 0.35,
        "LIMITED": 0.15, "FULL": 0.05, "PROBABLE": 0.05,
    }.get(d, 0.1)


__all__ = [
    "WeatherProvider", "AvailabilityProvider",
    "WeatherReport", "AvailabilityReport",
    "DefaultWeatherProvider", "DefaultAvailabilityProvider",
    "WEATHER_STATUS_AVAILABLE", "WEATHER_STATUS_UNAVAILABLE",
    "INJURY_STATUS_AVAILABLE", "INJURY_STATUS_PARTIAL",
    "INJURY_STATUS_UNAVAILABLE",
]
