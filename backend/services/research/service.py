"""Research Service — Strategy Lab 10X aggregator.

Fans out to per-sport adapters (mlb.py / nfl.py / nba.py) and returns
a canonical snapshot. This service is the single choke-point between the
Lab workstation and existing production data. The workstation UI, the
`/api/lab/research/context` endpoint, and any experimental research code
all read through here.

Design invariants:
  * ONLY MLB / NFL / NBA are supported in this build (per user directive).
  * SHADOW signals are surfaced but NEVER threaded into production
    contexts. `factual_ctx()` returns FACTUAL rows only.
  * `distribution()` / `line_explorer()` / `calibration_center()` /
    `pattern_discovery()` are workstation lenses that read from existing
    `db.picks` and `db.player_game_logs` — no new provider dependency.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from deps import db
from .contract import (
    CanonicalResearchSnapshot, ResearchFact, ResearchProvenance,
    ResearchQuality, ResearchSection, ResearchShadowSignal,
)

log = logging.getLogger("lockscore.research.service")

SUPPORTED_SPORTS = {"MLB", "NFL", "NBA"}


class ResearchService:
    """Single façade for all Strategy Lab research data."""

    # ── snapshot / subject research ──────────────────────────────────
    async def build_snapshot(
        self,
        sport: str,
        subject: str | None = None,
        opponent: str | None = None,
        event_id: str | None = None,
        event_label: str | None = None,
        role: str = "player",
        include_shadow: bool = True,
        include_distribution: bool = False,
        include_calibration: bool = False,
    ) -> CanonicalResearchSnapshot:
        sport = (sport or "").upper()
        if sport not in SUPPORTED_SPORTS:
            return CanonicalResearchSnapshot(
                sport=sport,
                generated_at=datetime.now(timezone.utc).isoformat(),
                notes=[f"Sport {sport} not supported in Strategy Lab 10X "
                       f"(MLB/NFL/NBA only)"],
            )
        if sport == "MLB":
            from . import mlb
            snap = await mlb.build_snapshot(subject=subject, opponent=opponent,
                                            event_id=event_id, event_label=event_label,
                                            role=role or "batter",
                                            include_shadow=include_shadow)
        elif sport == "NFL":
            from . import nfl
            snap = await nfl.build_snapshot(subject=subject, opponent=opponent,
                                            event_id=event_id, event_label=event_label,
                                            role=role, include_shadow=include_shadow)
        else:  # NBA
            from . import nba
            snap = await nba.build_snapshot(subject=subject, opponent=opponent,
                                            event_id=event_id, event_label=event_label,
                                            role=role, include_shadow=include_shadow)

        if include_distribution and subject:
            snap.distribution = await self.distribution(
                sport=sport, subject=subject)
        if include_calibration:
            snap.calibration = await self.calibration_center(sport=sport)
        return snap

    # ── Section 7: distribution / line explorer ──────────────────────
    async def distribution(
        self,
        sport: str,
        subject: str,
        market_hint: str | None = None,
    ) -> dict[str, Any]:
        """Build an empirical distribution of a player's recent output for
        the workstation's fair-price explorer. Returns histogram bins +
        percentiles + implied prob over a candidate line.
        """
        sport = (sport or "").upper()
        stat_field = self._distribution_stat_field(sport, market_hint)
        try:
            cursor = db.player_game_logs.find(
                {"sport": sport.lower(), "player_name": subject},
                {"_id": 0, "stats": 1, "date": 1},
            ).sort("date", -1).limit(30)
            rows = await cursor.to_list(length=30)
        except Exception:
            rows = []
        values: list[float] = []
        for r in rows:
            s = r.get("stats") or {}
            v = s.get(stat_field)
            if v is None and stat_field == "pra" and s:
                v = (s.get("points") or 0) + (s.get("rebounds") or 0) + (s.get("assists") or 0)
            if v is None:
                continue
            try:
                values.append(float(v))
            except Exception:
                continue
        if not values:
            return {"available": False, "reason": "no_data", "stat": stat_field}
        values.sort()
        n = len(values)
        mean = sum(values) / n
        var = sum((x - mean) ** 2 for x in values) / n
        std = math.sqrt(var)
        def _pct(p: float) -> float:
            k = min(n - 1, max(0, int(round(p * (n - 1)))))
            return values[k]
        return {
            "available": True,
            "stat": stat_field,
            "sample_size": n,
            "mean": round(mean, 2),
            "median": _pct(0.5),
            "std": round(std, 2),
            "p10": _pct(0.10), "p25": _pct(0.25),
            "p50": _pct(0.50), "p75": _pct(0.75), "p90": _pct(0.90),
            "min": values[0], "max": values[-1],
            "values": values,
        }

    @staticmethod
    def _distribution_stat_field(sport: str, hint: str | None) -> str:
        s = (hint or "").lower()
        if sport == "MLB":
            if "hr" in s: return "hr"
            if "strike" in s: return "strikeouts"
            if "total_bases" in s or "tb" in s: return "total_bases"
            if "rbi" in s: return "rbi"
            return "hits"
        if sport == "NFL":
            if "rush" in s: return "rushing_yards"
            if "rec" in s and "yard" in s: return "receiving_yards"
            if "target" in s: return "targets"
            if "pass" in s: return "passing_yards"
            return "targets"
        if sport == "NBA":
            if "reb" in s: return "rebounds"
            if "ast" in s: return "assists"
            if "3" in s or "three" in s: return "three_pointers_made"
            if "pra" in s: return "pra"
            return "points"
        return "value"

    # ── Fair-price explorer ─────────────────────────────────────────
    async def line_explorer(
        self,
        sport: str,
        subject: str,
        line: float,
        market_hint: str | None = None,
    ) -> dict[str, Any]:
        dist = await self.distribution(sport, subject, market_hint)
        if not dist.get("available"):
            return {"available": False, "reason": dist.get("reason")}
        values: list[float] = dist["values"]
        n = len(values)
        over = sum(1 for v in values if v > line)
        under = sum(1 for v in values if v < line)
        push = n - over - under
        p_over = over / n if n else 0.0
        p_under = under / n if n else 0.0
        def _to_american(p: float) -> int | None:
            if p <= 0.0 or p >= 1.0:
                return None
            if p >= 0.5:
                return int(round(-100 * p / (1 - p)))
            return int(round(100 * (1 - p) / p))
        return {
            "available": True,
            "sample_size": n,
            "stat": dist["stat"],
            "line": line,
            "empirical_over_rate": round(p_over, 3),
            "empirical_under_rate": round(p_under, 3),
            "push_rate": round(push / n, 3) if n else None,
            "fair_over_odds": _to_american(p_over),
            "fair_under_odds": _to_american(p_under),
        }

    # ── Section 9: calibration center ────────────────────────────────
    async def calibration_center(self, sport: str) -> dict[str, Any]:
        """Read-only historical calibration — aggregate from settled picks.
        No history reconstruction. No settlement rewrite. Pure projection.
        """
        sport = (sport or "").upper()
        try:
            q = {"sport": sport, "status": {"$in": ["won", "lost"]}}
            cursor = db.picks.find(q, {
                "_id": 0, "win_probability": 1, "status": 1, "lock_score": 1,
                "book_odds": 1, "units_profit": 1, "units_risked": 1,
            }).limit(20000)
            rows = await cursor.to_list(length=20000)
        except Exception as e:
            log.warning("calibration_center fetch failed: %s", e)
            return {"available": False}
        if not rows:
            return {"available": False, "reason": "no_settled"}
        buckets = {
            "50-59": {"n": 0, "w": 0, "sum": 0.0, "profit": 0.0, "risk": 0.0},
            "60-69": {"n": 0, "w": 0, "sum": 0.0, "profit": 0.0, "risk": 0.0},
            "70-79": {"n": 0, "w": 0, "sum": 0.0, "profit": 0.0, "risk": 0.0},
            "80-89": {"n": 0, "w": 0, "sum": 0.0, "profit": 0.0, "risk": 0.0},
            "90-100": {"n": 0, "w": 0, "sum": 0.0, "profit": 0.0, "risk": 0.0},
        }
        for r in rows:
            wp = r.get("win_probability")
            if wp is None:
                continue
            try:
                wpf = float(wp)
            except Exception:
                continue
            if wpf < 50: continue
            if wpf < 60: b = "50-59"
            elif wpf < 70: b = "60-69"
            elif wpf < 80: b = "70-79"
            elif wpf < 90: b = "80-89"
            else: b = "90-100"
            slot = buckets[b]
            slot["n"] += 1
            slot["sum"] += wpf
            if r.get("status") == "won":
                slot["w"] += 1
            slot["profit"] += float(r.get("units_profit") or 0.0)
            slot["risk"] += float(r.get("units_risked") or 1.0)
        out_rows = []
        for b, s in buckets.items():
            if s["n"] == 0:
                continue
            hit_rate = s["w"] / s["n"]
            avg_pred = (s["sum"] / s["n"]) / 100.0
            roi = (s["profit"] / s["risk"]) if s["risk"] > 0 else 0.0
            out_rows.append({
                "bucket": b,
                "n": s["n"],
                "avg_pred_prob": round(avg_pred, 3),
                "actual_hit_rate": round(hit_rate, 3),
                "gap_pp": round((avg_pred - hit_rate) * 100.0, 1),
                "roi_pct": round(roi * 100.0, 2),
                "sample_class": self._sample_class(s["n"]),
            })
        return {"available": True, "sport": sport, "rows": out_rows}

    @staticmethod
    def _sample_class(n: int) -> str:
        if n >= 500: return "RELIABLE"
        if n >= 100: return "EARLY_SIGNAL"
        return "INSUFFICIENT"

    # ── Section 10: pattern discovery 3.0 ────────────────────────────
    async def pattern_discovery(
        self,
        sport: str,
        limit: int = 20,
        min_sample: int = 25,
    ) -> dict[str, Any]:
        """Mine settled picks for buckets with statistically-credible
        hit-rate lifts. Returns SHADOW signals (NEVER used by Lock math).
        """
        sport = (sport or "").upper()
        try:
            q = {"sport": sport, "status": {"$in": ["won", "lost"]}}
            cursor = db.picks.find(q, {
                "_id": 0, "market": 1, "status": 1, "book_odds": 1,
                "lock_score": 1, "player_name": 1, "team": 1,
            }).limit(50000)
            rows = await cursor.to_list(length=50000)
        except Exception:
            rows = []
        if not rows:
            return {"available": False}
        buckets: dict[str, dict[str, int]] = {}
        for r in rows:
            m = (r.get("market") or "?").split(" - ")[0].strip()
            odds = r.get("book_odds")
            if odds is None:
                odds_b = "?"
            else:
                try:
                    o = int(odds)
                except Exception:
                    o = 0
                if o <= -300: odds_b = "chalk"
                elif o <= -150: odds_b = "heavy_fav"
                elif o <= -110: odds_b = "slight_fav"
                elif o <= 100: odds_b = "pickem"
                elif o <= 200: odds_b = "medium_dog"
                else: odds_b = "big_dog"
            key = f"{m} / {odds_b}"
            slot = buckets.setdefault(key, {"n": 0, "w": 0})
            slot["n"] += 1
            if r.get("status") == "won":
                slot["w"] += 1
        signals: list[dict[str, Any]] = []
        for k, s in buckets.items():
            if s["n"] < min_sample:
                continue
            hr = s["w"] / s["n"]
            z = 1.96
            denom = 1 + (z * z) / s["n"]
            center = hr + (z * z) / (2 * s["n"])
            margin = z * math.sqrt(
                hr * (1 - hr) / s["n"] + (z * z) / (4 * s["n"] * s["n"])
            )
            wilson = max(0.0, (center - margin) / denom)
            signals.append({
                "bucket": k,
                "n": s["n"], "w": s["w"],
                "hit_rate": round(hr, 3),
                "wilson_lower": round(wilson, 3),
                "provenance": "SHADOW_SIGNAL",
                "strength": ("strong" if wilson >= 0.6
                             else "moderate" if wilson >= 0.5
                             else "weak"),
            })
        signals.sort(key=lambda s: s["wilson_lower"], reverse=True)
        return {"available": True, "sport": sport, "signals": signals[:limit]}


_singleton: ResearchService | None = None


def get_research_service() -> ResearchService:
    global _singleton
    if _singleton is None:
        _singleton = ResearchService()
    return _singleton
