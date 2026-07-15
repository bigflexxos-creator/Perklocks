"""Normalized data models — every source normalizes to these shapes."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SoccerMatch:
    """A single historical or scheduled match. Odds fields may be None
    when the source doesn't publish them (e.g. TheSportsDB has no odds,
    football-data.co.uk always does)."""
    league:            str                        # normalized league code, e.g. "EPL"
    season:            str                        # "2024-25"
    home_team:         str
    away_team:         str
    date:              str                        # ISO date "2025-08-16"
    home_score:        Optional[int] = None
    away_score:        Optional[int] = None
    home_xg:           Optional[float] = None
    away_xg:           Optional[float] = None
    home_odds_close:   Optional[float] = None     # decimal odds
    draw_odds_close:   Optional[float] = None
    away_odds_close:   Optional[float] = None
    home_odds_open:    Optional[float] = None
    draw_odds_open:    Optional[float] = None
    away_odds_open:    Optional[float] = None
    status:            str = "finished"           # scheduled|live|finished|postponed
    source:            str = "unknown"
    fetched_at:        str = field(default_factory=_now_iso)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SoccerTeam:
    name:              str
    league:            Optional[str] = None
    country:           Optional[str] = None
    stadium:           Optional[str] = None
    founded:           Optional[int] = None
    website:           Optional[str] = None
    thesportsdb_id:    Optional[str] = None
    espn_id:           Optional[str] = None
    football_data_id:  Optional[int] = None
    source:            str = "unknown"
    fetched_at:        str = field(default_factory=_now_iso)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SoccerStanding:
    league:            str
    season:            str
    team:              str
    position:          int
    played:            int
    won:               int
    drawn:             int
    lost:              int
    goals_for:         int
    goals_against:     int
    goal_diff:         int
    points:            int
    form:              Optional[str] = None       # e.g. "WWDLW"
    source:            str = "unknown"
    fetched_at:        str = field(default_factory=_now_iso)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SoccerFixture:
    league:            str
    season:            str
    home_team:         str
    away_team:         str
    utc_kickoff:       str            # ISO
    matchday:          Optional[int] = None
    venue:             Optional[str] = None
    referee:           Optional[str] = None
    status:            str = "SCHEDULED"
    source:            str = "unknown"
    fetched_at:        str = field(default_factory=_now_iso)

    def to_dict(self) -> dict:
        return asdict(self)


# Canonical league codes used across sources. Individual source files
# have their own slug-map that translates provider-native codes into
# these canonical keys. Keeping this list here means the fallback chain
# has a single vocabulary — a query for `league="EPL"` can hit ANY
# provider and get normalised results.
LEAGUE_CODES: dict[str, dict] = {
    "EPL":           {"name": "English Premier League",   "country": "England",  "tier": 1},
    "ELC":           {"name": "English Championship",     "country": "England",  "tier": 2},
    "EL1":           {"name": "English League One",       "country": "England",  "tier": 3},
    "EL2":           {"name": "English League Two",       "country": "England",  "tier": 4},
    "ECONF":         {"name": "English National League",  "country": "England",  "tier": 5},
    "LaLiga":        {"name": "Spanish La Liga",           "country": "Spain",    "tier": 1},
    "LaLiga2":       {"name": "Spanish Segunda División",  "country": "Spain",    "tier": 2},
    "Bundesliga":    {"name": "German Bundesliga",         "country": "Germany",  "tier": 1},
    "Bundesliga2":   {"name": "German 2. Bundesliga",      "country": "Germany",  "tier": 2},
    "SerieA":        {"name": "Italian Serie A",           "country": "Italy",    "tier": 1},
    "SerieB":        {"name": "Italian Serie B",           "country": "Italy",    "tier": 2},
    "Ligue1":        {"name": "French Ligue 1",            "country": "France",   "tier": 1},
    "Ligue2":        {"name": "French Ligue 2",            "country": "France",   "tier": 2},
    "Eredivisie":    {"name": "Dutch Eredivisie",          "country": "Netherlands", "tier": 1},
    "Primeira":      {"name": "Portuguese Primeira Liga",  "country": "Portugal", "tier": 1},
    "SPL":           {"name": "Scottish Premiership",      "country": "Scotland", "tier": 1},
    "SD1":           {"name": "Scottish Championship",     "country": "Scotland", "tier": 2},
    "BEL":           {"name": "Belgian Pro League",        "country": "Belgium",  "tier": 1},
    "TUR":           {"name": "Turkish Süper Lig",         "country": "Turkey",   "tier": 1},
    "GRE":           {"name": "Greek Super League",        "country": "Greece",   "tier": 1},
    "Allsvenskan":   {"name": "Swedish Allsvenskan",       "country": "Sweden",   "tier": 1},
    "Eliteserien":   {"name": "Norwegian Eliteserien",     "country": "Norway",   "tier": 1},
    "MLS":           {"name": "USA Major League Soccer",   "country": "USA",      "tier": 1},
    "Brasileirao":   {"name": "Brazilian Série A",         "country": "Brazil",   "tier": 1},
    "Argentina":     {"name": "Argentine Primera",         "country": "Argentina","tier": 1},
    "LigaMX":        {"name": "Mexican Liga MX",            "country": "Mexico",   "tier": 1},
    "UCL":           {"name": "UEFA Champions League",     "country": "Europe",   "tier": 0},
    "UEL":           {"name": "UEFA Europa League",        "country": "Europe",   "tier": 0},
    "UECL":          {"name": "UEFA Conference League",    "country": "Europe",   "tier": 0},
}
