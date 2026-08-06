"""NBA Sport Adapter — Phase 4 live wiring (2026-06-25).

Pulls season-aggregate stats from the local free-source player_db
(populated daily by the ESPN public ingestor) and injects them as
EvidenceFeature envelopes for the Universal Evidence System.

Features emitted (per-pick basis, gated by what the player_stats
row actually contains):
  • Minutes projection (volume tier)
  • PPG / REB / AST baseline matching the prop category
  • Field-goal efficiency for shooting markets
  • 3P% for threes-made markets
  • Free-throw efficiency for fouls/FTs-made markets
  • Injury status (active / probable / out)

ESPN season-stat labels (set by ingestor): GP, MIN, FG%, 3P%, FT%,
REB, AST, BLK, STL, PF, PPG (or PTS). Missing labels → feature
quietly skipped (the Evidence Governor down-weights sparse picks).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from sport_adapters import SportAdapter, register
from evidence_engine import EvidenceFeature, _universal_build_features_from_pick

logger = logging.getLogger("lockscore.nba_adapter")


def _safe_float(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _canonical(name: str) -> str:
    return (name or "").strip().lower()


def _market_targets(market: str) -> dict[str, bool]:
    m = (market or "").lower()
    return {
        "points":    "points" in m or m.endswith(" pts") or "pts " in m,
        "rebounds":  "rebound" in m or "reb" in m,
        "assists":   "assist" in m or "ast" in m,
        "threes":    "3-pointer" in m or "threes" in m or "made 3" in m,
        "blocks":    "block" in m,
        "steals":    "steal" in m,
        "pra":       "pts+reb+ast" in m or "pts + reb + ast" in m or "p+r+a" in m,
    }


def _lookup_player_sync(sport: str, name: str) -> dict | None:
    """Synchronous adapter into the player_db.  Phase 3B — uses the
    shared pymongo client owned by services.database.  Cheap: the
    shared client is pooled, and each call executes against it
    without re-establishing a connection."""
    target = _canonical(name)
    if not target:
        return None
    try:
        from services.database import get_sync_database
        sdb = get_sync_database()
        player = sdb.players.find_one(
            {"sport": sport, "canonical_name": target}, {"_id": 0},
        )
        stats = sdb.player_stats.find_one(
            {"sport": sport, "canonical_name": target}, {"_id": 0},
            sort=[("season", -1)],
        )
        injury = sdb.injuries.find_one(
            {"sport": sport, "canonical_name": target}, {"_id": 0},
        )
        return {"player": player, "stats": stats, "injury": injury}
    except Exception as e:
        logger.debug("player_db sync lookup failed for %s/%s: %s", sport, name, e)
        return None


class NBAAdapter(SportAdapter):
    SPORT = "NBA"

    def collect_features(self, pick: dict) -> list[EvidenceFeature]:
        # Start with universal features (factors, sim, edge, learning, etc.).
        feats = _universal_build_features_from_pick(pick)
        # Extract player name from pick — prefer the explicit field, fall
        # back to extracting from the market string.
        name = pick.get("player") or pick.get("canonical_name")
        if not name:
            try:
                from player_intel.resolver import extract_player_from_market
                name = extract_player_from_market(pick.get("market") or "")
            except Exception:
                name = None
        if not name:
            return feats

        bundle = _lookup_player_sync("nba", name)
        if not bundle or not bundle.get("stats"):
            return feats

        stats = (bundle["stats"].get("stats") or {})
        gp = _safe_float(stats.get("GP"))
        if gp is None or gp < 5:
            # too few games to use as a meaningful baseline
            return feats

        targets = _market_targets(pick.get("market") or "")

        # Volume tier — minutes per game
        mins = _safe_float(stats.get("MIN"))
        if mins is not None:
            tier = "starter" if mins >= 28 else ("rotation" if mins >= 18 else "bench")
            feats.append(EvidenceFeature(
                name="Minutes projection", category="usage",
                value=round(mins, 1),
                sample_size=int(gp),
                lookback_days=240,
                source="ESPN public",
                importance=0.85,
                reason=f"Averages {mins:.1f} min/g ({tier}) over {int(gp)} GP this season",
            ))

        # Market-specific stat anchors
        if targets["points"] or targets["pra"]:
            ppg = _safe_float(stats.get("PPG") or stats.get("PTS"))
            if ppg is not None:
                feats.append(EvidenceFeature(
                    name="Season PPG", category="form",
                    value=round(ppg, 1),
                    sample_size=int(gp),
                    lookback_days=240,
                    source="ESPN public",
                    importance=0.85,
                    reason=f"{ppg:.1f} PPG season baseline",
                ))
        if targets["rebounds"] or targets["pra"]:
            reb = _safe_float(stats.get("REB"))
            if reb is not None:
                feats.append(EvidenceFeature(
                    name="Season REB", category="form",
                    value=round(reb, 1),
                    sample_size=int(gp),
                    lookback_days=240,
                    source="ESPN public",
                    importance=0.80,
                    reason=f"{reb:.1f} REB season baseline",
                ))
        if targets["assists"] or targets["pra"]:
            ast = _safe_float(stats.get("AST"))
            if ast is not None:
                feats.append(EvidenceFeature(
                    name="Season AST", category="form",
                    value=round(ast, 1),
                    sample_size=int(gp),
                    lookback_days=240,
                    source="ESPN public",
                    importance=0.80,
                    reason=f"{ast:.1f} AST season baseline",
                ))
        if targets["threes"]:
            pct3 = _safe_float(stats.get("3P%"))
            if pct3 is not None:
                feats.append(EvidenceFeature(
                    name="Season 3P%", category="form",
                    value=round(pct3, 1),
                    sample_size=int(gp),
                    lookback_days=240,
                    source="ESPN public",
                    importance=0.70,
                    reason=f"Shooting {pct3:.1f}% from three this season",
                ))
        if targets["blocks"]:
            blk = _safe_float(stats.get("BLK"))
            if blk is not None:
                feats.append(EvidenceFeature(
                    name="Season BLK", category="form",
                    value=round(blk, 1),
                    sample_size=int(gp),
                    lookback_days=240,
                    source="ESPN public",
                    importance=0.65,
                    reason=f"{blk:.1f} BLK/g baseline",
                ))
        if targets["steals"]:
            stl = _safe_float(stats.get("STL"))
            if stl is not None:
                feats.append(EvidenceFeature(
                    name="Season STL", category="form",
                    value=round(stl, 1),
                    sample_size=int(gp),
                    lookback_days=240,
                    source="ESPN public",
                    importance=0.55,
                    reason=f"{stl:.1f} STL/g baseline",
                ))

        # Injury status — coarse signal
        injury = bundle.get("injury") or {}
        if injury and injury.get("status"):
            feats.append(EvidenceFeature(
                name="Injury report", category="context",
                value=injury["status"],
                sample_size=1, lookback_days=2,
                source="ESPN injury",
                importance=0.90,
                reason=f"Status: {injury['status']}"
                       + (f" ({injury.get('description')})" if injury.get('description') else ""),
            ))
        return feats


register(NBAAdapter())
