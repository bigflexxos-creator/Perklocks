"""NFL Sport Adapter — Phase 4 live wiring (2026-06-25).

Pulls season-aggregate stats from the local free-source player_db
(populated daily by the ESPN public ingestor) and injects them into
the Universal Evidence System.

NFL props in The Odds API come in flavors:
  • Passing props (yards, TDs, completions, interceptions)
  • Rushing props (yards, attempts, longest run, TDs)
  • Receiving props (yards, receptions, longest reception, TDs)
  • Anytime / first TD scorer

Features emitted (gated by what's actually populated):
  • Position archetype (QB/RB/WR/TE) — pulled from player row
  • Season totals matching the prop category
  • Snap-share proxy via GP (games played)
  • Injury status

ESPN season-stat label set differs per position group; we map only
the labels we recognise and skip anything else gracefully.
"""
from __future__ import annotations

import logging
from typing import Any

from sport_adapters import SportAdapter, register
from evidence_engine import EvidenceFeature, _universal_build_features_from_pick
from sport_adapters.nba import _lookup_player_sync, _safe_float  # reuse the bridge

logger = logging.getLogger("lockscore.nfl_adapter")


def _market_targets(market: str) -> dict[str, bool]:
    m = (market or "").lower()
    return {
        "passing_yds":   "pass" in m and ("yard" in m or "yds" in m),
        "passing_tds":   "pass" in m and ("touchdown" in m or "tds" in m),
        "rushing_yds":   "rush" in m and ("yard" in m or "yds" in m),
        "rushing_atts":  "rush" in m and "attempt" in m,
        "receiving_yds": "receiving" in m and ("yard" in m or "yds" in m),
        "receptions":    "reception" in m or "rec yds" in m,
        "anytime_td":    ("anytime" in m and "touchdown" in m) or "atts" in m and False,  # explicit
    }


class NFLAdapter(SportAdapter):
    SPORT = "NFL"

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

        bundle = _lookup_player_sync("nfl", name)
        if not bundle or not bundle.get("player"):
            return feats

        player = bundle["player"] or {}
        stats_row = bundle.get("stats") or {}
        stats = (stats_row.get("stats") or {}) if stats_row else {}
        position = player.get("position")
        targets = _market_targets(pick.get("market") or "")

        # Position archetype — coarse but reliable signal
        if position:
            feats.append(EvidenceFeature(
                name="Position archetype", category="usage",
                value=position,
                sample_size=1, lookback_days=240,
                source="ESPN public",
                importance=0.65,
                reason=f"Listed as {position}",
            ))

        # Season volume — games played, used as a workload proxy
        gp = _safe_float(stats.get("GP"))
        if gp is not None and gp > 0:
            workload = "starter" if gp >= 12 else ("regular" if gp >= 6 else "limited")
            feats.append(EvidenceFeature(
                name="Season games played", category="usage",
                value=int(gp),
                sample_size=int(gp), lookback_days=240,
                source="ESPN public",
                importance=0.55,
                reason=f"{int(gp)} GP this season ({workload})",
            ))

        # Map common ESPN labels for the prop's category. ESPN's NFL
        # overview labels include: CMP, ATT, CMP%, YDS, AVG, TD, INT,
        # SACK, RTG (QB); CAR, YDS, AVG, LNG, BIG, TD, YDS/G (RB);
        # REC, TGTS, YDS, AVG, TD, LNG, YDS/G (WR/TE).
        def _push(label_keys: tuple[str, ...], feat_name: str, market_key: str,
                  importance: float = 0.75) -> None:
            if not targets.get(market_key):
                return
            for k in label_keys:
                v = _safe_float(stats.get(k))
                if v is not None:
                    feats.append(EvidenceFeature(
                        name=feat_name, category="form",
                        value=round(v, 1),
                        sample_size=int(gp or 1), lookback_days=240,
                        source="ESPN public",
                        importance=importance,
                        reason=f"{feat_name}: {v:.1f} (last season)",
                    ))
                    return

        _push(("YDS", "PASS YDS", "PYDS"),         "Season passing yards", "passing_yds")
        _push(("TD",  "PASS TD"),                  "Season passing TDs",   "passing_tds")
        _push(("YDS", "RUSH YDS", "RYDS"),         "Season rushing yards", "rushing_yds")
        _push(("CAR", "ATT"),                      "Season carries",       "rushing_atts")
        _push(("YDS", "REC YDS"),                  "Season receiving yards","receiving_yds")
        _push(("REC", "RECEPTIONS"),               "Season receptions",    "receptions")

        # Injury status
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


register(NFLAdapter())
