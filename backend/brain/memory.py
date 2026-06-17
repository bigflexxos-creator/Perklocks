"""Prediction Memory Layer.

Maintains a fast-read snapshot of every historical prediction's outcome,
organised for the brain components above to query in O(1):

  • per-(sport, market_family) → ROI, win_rate, sample_n, calibration_err,
    clv_avg, last_seen_at
  • per-band (95-98, 90-94, …)  → expected_pct, actual_pct, sample_n
  • global rolling 100-pick win/loss tape

Rebuilt once per refresh cycle (or every 5min, whichever comes first) and
cached in-process — heavy aggregations don't run per-pick.

Note: this layer is ADDITIVE to the existing learning_engine /
learning_system_v2 collections. We don't replace them; we consolidate
their read-side into a single dict so the rest of the brain doesn't
have to round-trip Mongo on every pick.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("lockscore.brain.memory")

CACHE_TTL_SEC = 300        # 5 minutes
MIN_SAMPLE_FOR_PRIOR = 8   # below this we use neutral priors


@dataclass
class MarketStats:
    sport: str
    family: str               # selection_v2.market.family
    n: int = 0
    won: int = 0
    lost: int = 0
    push: int = 0
    roi_pct: float = 0.0      # (units_profit / units_risked) * 100
    win_rate: float = 0.5     # 0..1 (excludes pushes from denominator)
    calibration_err: float = 0.0   # mean |expected - actual| (0..1)
    clv_avg: float = 0.0
    last_seen_at: Optional[str] = None


@dataclass
class BandStats:
    band: str                 # "99" / "95-98" / "90-94" / "85-89" / "80-84" / "<80"
    expected_pct: float       # the band's calibration target
    actual_pct: float = 0.0
    n: int = 0


@dataclass
class BrainMemory:
    built_at: float = field(default_factory=time.time)
    settled_total: int = 0
    market_stats: dict[tuple[str, str], MarketStats] = field(default_factory=dict)
    band_stats: dict[str, BandStats] = field(default_factory=dict)
    global_win_rate: float = 0.5
    global_roi: float = 0.0

    def market(self, sport: str, family: str) -> Optional[MarketStats]:
        return self.market_stats.get((sport, family))

    def band(self, band_name: str) -> Optional[BandStats]:
        return self.band_stats.get(band_name)

    @property
    def is_fresh(self) -> bool:
        return (time.time() - self.built_at) < CACHE_TTL_SEC


# Standard score bands used across the brain.
CAL_BANDS = [
    {"name": "99",    "min": 99.0, "max": 99.9, "expected": 98.0},
    {"name": "95-98", "min": 95.0, "max": 98.99, "expected": 80.0},
    {"name": "90-94", "min": 90.0, "max": 94.99, "expected": 70.0},
    {"name": "85-89", "min": 85.0, "max": 89.99, "expected": 62.0},
    {"name": "80-84", "min": 80.0, "max": 84.99, "expected": 55.0},
    {"name": "<80",   "min":  0.0, "max": 79.99, "expected": 48.0},
]


def band_for_score(score: float) -> str:
    for b in CAL_BANDS:
        if b["min"] <= score <= b["max"]:
            return b["name"]
    return "<80"


# ────────────────────────────────────────────────────────────────────────
# Lazy-built process-wide cache
# ────────────────────────────────────────────────────────────────────────

_CACHE: Optional[BrainMemory] = None
_BUILDING_LOCK = asyncio.Lock()


async def get_or_build_memory(db, force: bool = False) -> BrainMemory:
    """Return the cached snapshot or rebuild if stale."""
    global _CACHE
    if not force and _CACHE is not None and _CACHE.is_fresh:
        return _CACHE
    async with _BUILDING_LOCK:
        if not force and _CACHE is not None and _CACHE.is_fresh:
            return _CACHE
        _CACHE = await _build_memory(db)
        return _CACHE


async def invalidate_memory() -> None:
    """Forced cache bust (called from the settlement hook)."""
    global _CACHE
    _CACHE = None


async def _build_memory(db) -> BrainMemory:
    """Rebuild the snapshot by scanning settled picks.

    Cost: one cursor + small aggregations. Runs once per 5min, not per-pick.
    """
    mem = BrainMemory()
    # Seed band stats with calibration targets.
    for b in CAL_BANDS:
        mem.band_stats[b["name"]] = BandStats(
            band=b["name"], expected_pct=b["expected"]
        )

    global_won = global_lost = 0
    global_units_risked = global_units_profit = 0.0

    cursor = db.picks.find(
        {"status": {"$in": ["won", "lost", "push"]}},
        {
            "_id": 0, "sport": 1, "status": 1, "lock_score": 1,
            "win_probability": 1, "units_risked": 1, "units_profit": 1,
            "clv_value": 1, "settled_at": 1, "selection_v2": 1, "market": 1,
        },
    )
    async for p in cursor:
        sport = p.get("sport") or "Unknown"
        # Prefer the brain-canonical market family from sportsbook_mapper.
        sv2 = p.get("selection_v2") or {}
        family = (sv2.get("market") or {}).get("family") or _legacy_family(p.get("market"))
        status = p.get("status")
        units_risked = float(p.get("units_risked") or 0)
        units_profit = float(p.get("units_profit") or 0)
        clv = p.get("clv_value")

        key = (sport, family)
        stats = mem.market_stats.setdefault(key, MarketStats(sport=sport, family=family))
        stats.n += 1
        if status == "won":
            stats.won += 1
            global_won += 1
        elif status == "lost":
            stats.lost += 1
            global_lost += 1
        elif status == "push":
            stats.push += 1
        stats.last_seen_at = p.get("settled_at") or stats.last_seen_at
        # Running ROI sums
        stats._units_risked = getattr(stats, "_units_risked", 0.0) + units_risked
        stats._units_profit = getattr(stats, "_units_profit", 0.0) + units_profit
        if clv is not None:
            stats._clv_sum = getattr(stats, "_clv_sum", 0.0) + float(clv)
            stats._clv_n = getattr(stats, "_clv_n", 0) + 1
        # Calibration: track mean predicted probability vs actual rate for
        # bucket-level miscalibration. The bucket calibration_err is then
        # |mean_predicted - actual_rate| (e.g. 0.10 = bucket is 10pp off).
        wp = p.get("win_probability")
        if wp is not None and status != "push":
            stats._wp_sum = getattr(stats, "_wp_sum", 0.0) + float(wp) / 100.0
            stats._wp_n = getattr(stats, "_wp_n", 0) + 1

        global_units_risked += units_risked
        global_units_profit += units_profit

        # Band stats
        ls = p.get("lock_score")
        if ls is not None and status in ("won", "lost"):
            b = band_for_score(float(ls))
            band = mem.band_stats[b]
            band.n += 1
            if status == "won":
                band._won = getattr(band, "_won", 0) + 1

    # Finalise derived metrics.
    for stats in mem.market_stats.values():
        ur = getattr(stats, "_units_risked", 0.0)
        up = getattr(stats, "_units_profit", 0.0)
        stats.roi_pct = (up / ur * 100.0) if ur > 0 else 0.0
        decided = stats.won + stats.lost
        stats.win_rate = (stats.won / decided) if decided else 0.5
        cal_n = getattr(stats, "_wp_n", 0)
        if cal_n and (stats.won + stats.lost) > 0:
            mean_predicted = getattr(stats, "_wp_sum", 0.0) / cal_n
            actual_rate = stats.won / (stats.won + stats.lost)
            stats.calibration_err = abs(mean_predicted - actual_rate)
        else:
            stats.calibration_err = 0.0
        clv_n = getattr(stats, "_clv_n", 0)
        stats.clv_avg = (
            getattr(stats, "_clv_sum", 0.0) / clv_n if clv_n else 0.0
        )

    for band in mem.band_stats.values():
        won = getattr(band, "_won", 0)
        band.actual_pct = (won / band.n * 100.0) if band.n else band.expected_pct

    decided_global = global_won + global_lost
    mem.global_win_rate = (global_won / decided_global) if decided_global else 0.5
    mem.global_roi = (global_units_profit / global_units_risked * 100.0) if global_units_risked > 0 else 0.0
    mem.settled_total = decided_global + sum(s.push for s in mem.market_stats.values())

    logger.info(
        "Brain memory rebuilt: settled=%d markets=%d bands=%d global_wr=%.1f%% global_roi=%.1f%%",
        mem.settled_total, len(mem.market_stats), len(mem.band_stats),
        mem.global_win_rate * 100.0, mem.global_roi,
    )
    return mem


def _legacy_family(market: str | None) -> str:
    """Fallback when a settled pick predates sportsbook_mapper enrichment."""
    if not market:
        return "other"
    m = market.lower()
    if "moneyline" in m or "to win" in m:
        return "moneyline"
    if "spread" in m or "run line" in m or "puck line" in m:
        return "spread"
    if "total" in m:
        return "totals"
    if any(k in m for k in ("hits", "total bases", "strikeout", "goal scorer",
                             "to score", "points", "rebounds", "assists")):
        return "player_prop"
    if "both teams" in m or m.startswith("btts"):
        return "btts"
    if "draw" in m and "no bet" in m:
        return "draw_no_bet"
    if "double chance" in m or "win or draw" in m:
        return "double_chance"
    return "other"
