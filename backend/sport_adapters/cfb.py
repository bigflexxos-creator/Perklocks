"""CFB Sport Adapter — Phase 4 live wiring (2026-06-25).

Powered by ESPN public's college-football endpoints (already proved
in espn_public ingestor for NBA/NFL). Pulls rosters & per-athlete
stats from ~130 FBS teams.

Features emitted:
  • Conference / team class (Power-5 vs G5) — proxy strength signal
  • Position archetype
  • Season totals matching the prop category
  • Injury status

CFB-specific deeper signals (returning production, EPA, SOS) need
CollegeFootballData.com which requires a free key. Left as a
follow-up — the present adapter wires the framework so the data
flows the moment that ingestion lands.
"""
from __future__ import annotations

import logging

from sport_adapters import SportAdapter, register
from evidence_engine import EvidenceFeature, _universal_build_features_from_pick
from sport_adapters.nba import _lookup_player_sync, _safe_float

logger = logging.getLogger("lockscore.cfb_adapter")


# Power-5 conferences (broad strength signal). Set of common short names
# ESPN tends to use for the team object.
_POWER_5 = {
    "SEC", "Big Ten", "Big 12", "ACC",
    "Atlantic Coast Conference", "Southeastern Conference",
    "Pac-12", "Notre Dame",  # ND independent treated as P5 for strength
}


class CFBAdapter(SportAdapter):
    SPORT = "CFB"

    def collect_features(self, pick: dict) -> list[EvidenceFeature]:
        feats = _universal_build_features_from_pick(pick)
        name = pick.get("player") or pick.get("canonical_name")
        if not name:
            try:
                from player_intel.resolver import extract_player_from_market
                name = extract_player_from_market(pick.get("market") or "")
            except Exception:
                name = None
        if not name:
            return feats

        bundle = _lookup_player_sync("cfb", name)
        if not bundle or not bundle.get("player"):
            return feats
        player = bundle["player"]
        stats_row = bundle.get("stats") or {}
        stats = (stats_row.get("stats") or {}) if stats_row else {}

        position = player.get("position")
        if position:
            feats.append(EvidenceFeature(
                name="Position archetype", category="usage",
                value=position,
                sample_size=1, lookback_days=240,
                source="ESPN public",
                importance=0.60,
                reason=f"Listed as {position}",
            ))

        # Conference strength tier — Power-5 vs Group-of-5
        conference = player.get("conference") or player.get("team_conference")
        if conference:
            tier = "Power-5" if conference in _POWER_5 else "Group-of-5"
            feats.append(EvidenceFeature(
                name="Conference tier", category="context",
                value=tier,
                sample_size=1, lookback_days=240,
                source="ESPN public",
                importance=0.55,
                reason=f"{conference} ({tier})",
            ))

        # Volume signal
        gp = _safe_float(stats.get("GP"))
        if gp is not None and gp > 0:
            feats.append(EvidenceFeature(
                name="Season games played", category="usage",
                value=int(gp),
                sample_size=int(gp), lookback_days=240,
                source="ESPN public",
                importance=0.50,
                reason=f"{int(gp)} GP last season",
            ))

        # Stat-category match — same labels as NFL ESPN endpoint.
        market = (pick.get("market") or "").lower()
        if "passing" in market and ("yard" in market or "yds" in market):
            v = _safe_float(stats.get("YDS"))
            if v is not None:
                feats.append(EvidenceFeature(
                    name="Season passing yards", category="form",
                    value=round(v, 1),
                    sample_size=int(gp or 1), lookback_days=240,
                    source="ESPN public", importance=0.75,
                    reason=f"{v:.0f} passing yds last season",
                ))
        if "rushing" in market and ("yard" in market or "yds" in market):
            v = _safe_float(stats.get("YDS"))
            if v is not None:
                feats.append(EvidenceFeature(
                    name="Season rushing yards", category="form",
                    value=round(v, 1),
                    sample_size=int(gp or 1), lookback_days=240,
                    source="ESPN public", importance=0.75,
                    reason=f"{v:.0f} rush yds last season",
                ))
        if "receiving" in market and ("yard" in market or "yds" in market):
            v = _safe_float(stats.get("YDS"))
            if v is not None:
                feats.append(EvidenceFeature(
                    name="Season receiving yards", category="form",
                    value=round(v, 1),
                    sample_size=int(gp or 1), lookback_days=240,
                    source="ESPN public", importance=0.70,
                    reason=f"{v:.0f} rec yds last season",
                ))

        # Injury — if available
        injury = bundle.get("injury") or {}
        if injury and injury.get("status"):
            feats.append(EvidenceFeature(
                name="Injury report", category="context",
                value=injury["status"],
                sample_size=1, lookback_days=2,
                source="ESPN injury",
                importance=0.85,
                reason=f"Status: {injury['status']}",
            ))
        return feats


register(CFBAdapter())
