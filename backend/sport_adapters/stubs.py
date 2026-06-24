"""NBA / NFL / CFB adapter shells.

Interface ready — plug in fresh data sources when seasons start serving.
Phase 2 only ships the contract; live ingestion comes when:
  • NBA: regular season returns (Oct 2026)
  • NFL: regular season kicks off (Sep 2026)
  • CFB: regular season kicks off (Aug 2026)

Until then, these adapters fall back to the generic Phase 1 feature
extraction (factors, sim_runs, learning, edge) so no pick goes ungoverned.
"""
from __future__ import annotations

from sport_adapters import SportAdapter, register
from evidence_engine import EvidenceFeature, build_features_from_pick


class _SeasonStubAdapter(SportAdapter):
    """Shared shell for NBA/NFL/CFB until live ingestion is wired."""
    SPORT = ""  # overridden by subclass

    # Future adapter features to wire up when ingestion lands.
    FUTURE_FEATURES: tuple[str, ...] = ()

    def collect_features(self, pick: dict) -> list[EvidenceFeature]:
        # Fall back to the universal extractor — emits features for
        # whatever provenance the pick already carries (factors, sim,
        # learning, edge). Phase 2 lands the deeper sport-specific
        # pulls; until then the universal extractor keeps governance
        # working end-to-end.
        return build_features_from_pick(pick)


class NBAAdapter(_SeasonStubAdapter):
    SPORT = "NBA"
    FUTURE_FEATURES = (
        "usage_rate", "pace", "minutes_projection",
        "lineup_impact", "matchup_DvP",
    )


class NFLAdapter(_SeasonStubAdapter):
    SPORT = "NFL"
    FUTURE_FEATURES = (
        "snap_share", "route_participation", "red_zone_usage",
        "goal_line_carries", "defensive_matchup", "weather", "pace",
    )


class CFBAdapter(_SeasonStubAdapter):
    SPORT = "CFB"
    FUTURE_FEATURES = (
        "EPA_per_play", "pace", "success_rate",
        "returning_production", "strength_of_schedule",
    )


register(NBAAdapter())
register(NFLAdapter())
register(CFBAdapter())
