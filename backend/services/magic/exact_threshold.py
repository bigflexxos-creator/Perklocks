"""Magic Layer 2.0 — Exact-Threshold History.

Player-prop evidence must be evaluated at the EXACT proposed
betting threshold, not at a season average.  This module reads
``db.player_game_actuals`` (or a sport-specific source) and returns:

  * hit-rate at the exact threshold (last-N + season splits)
  * distribution quantiles (Q10/Q25/median/Q75/Q90)
  * variance / IQR
  * sample size and window
  * availability (AVAILABLE / PARTIAL / UNAVAILABLE)

Rules
─────
* PROVISIONAL identity ⇒ ``UNAVAILABLE`` (per Session-D safety gate).
* Never coerce a missing observation to 0 — either the observation
  exists and is counted, or the row is excluded from the sample.
* Direction (``over`` / ``under``) is honored — hit-rate for
  ``Over 1.5 hits`` counts games where ``hits >= 2``.

Usage
─────
    from services.magic.exact_threshold import (
        compute_exact_threshold_evidence,
    )
    ev = await compute_exact_threshold_evidence(
        db,
        canonical_player_id="fd_9554",
        identity_class="MAPPED",
        stat_key="hits",         # actuals.<stat_key>
        threshold=1.5,
        direction="over",
        sport="MLB", market="Player Hits", selection="Over",
        windows=("last_5", "last_10", "last_20", "season"),
    )
"""
from __future__ import annotations

import statistics
from typing import Any, Iterable, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

from services.magic.contract import (
    EvidenceItem, EvidenceType, Availability, availability_from,
)


_WINDOW_LIMIT: dict[str, int] = {
    "last_5":  5,
    "last_10": 10,
    "last_20": 20,
    "season":  400,     # season cap — real N determined by sample.
    "career":  2000,
}


def _hits(observations: list[float], threshold: float, direction: str) -> int:
    """Count exact-threshold hits.  Direction defaults to ``over``."""
    d = (direction or "over").lower()
    if d == "over":
        return sum(1 for v in observations if v > threshold)
    if d in ("over_or_equal", "at_least", "ge"):
        return sum(1 for v in observations if v >= threshold)
    if d == "under":
        return sum(1 for v in observations if v < threshold)
    if d in ("under_or_equal", "at_most", "le"):
        return sum(1 for v in observations if v <= threshold)
    return sum(1 for v in observations if v > threshold)


def _quantile(values: list[float], q: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    if n == 1:
        return float(s[0])
    idx = q * (n - 1)
    lo = int(idx)
    hi = min(lo + 1, n - 1)
    frac = idx - lo
    return float(s[lo]) * (1 - frac) + float(s[hi]) * frac


async def _fetch_actuals(
    db: AsyncIOMotorDatabase,
    *,
    canonical_player_id: str,
    stat_key: str,
    limit: int,
) -> list[dict]:
    return await db.player_game_actuals.find(
        {"canonical_player_id": canonical_player_id,
         f"actuals.{stat_key}": {"$exists": True}},
        projection={"_id": 0, "event_time": 1, "actuals": 1,
                     "opponent": 1, "home_away": 1, "season": 1},
        sort=[("event_time", -1)],
    ).limit(int(limit)).to_list(length=int(limit))


async def compute_exact_threshold_evidence(
    db: AsyncIOMotorDatabase,
    *,
    canonical_player_id: str,
    identity_class: str,
    stat_key: str,
    threshold: float,
    direction: str = "over",
    sport: str = "",
    league: Optional[str] = None,
    market: Optional[str] = None,
    selection: Optional[str] = None,
    windows: Iterable[str] = ("last_5", "last_10", "last_20", "season"),
    min_sample: int = 5,
) -> list[EvidenceItem]:
    """Return one EvidenceItem per window.  Safe on PROVISIONAL id."""
    # Session-D safety: PROVISIONAL / UNRESOLVED must NOT consume
    # authoritative history.
    if identity_class not in ("AUTHORITATIVE", "MAPPED"):
        return [EvidenceItem(
            evidence_type=EvidenceType.HISTORICAL_EXACT_THRESHOLD,
            availability=Availability.UNAVAILABLE,
            sport=sport, league=league, market=market, selection=selection,
            line=threshold,
            canonical_player_id=canonical_player_id,
            notes="identity_class not verified — authoritative history "
                  "consumption blocked (Session-D safety).",
            provenance={"identity_gate": identity_class or "MISSING"},
        )]

    # Fetch the deepest window once — all shorter windows slice this.
    max_limit = max((_WINDOW_LIMIT.get(w, 20) for w in windows), default=20)
    try:
        rows = await _fetch_actuals(
            db, canonical_player_id=canonical_player_id,
            stat_key=stat_key, limit=max_limit,
        )
    except Exception as e:
        return [EvidenceItem(
            evidence_type=EvidenceType.HISTORICAL_EXACT_THRESHOLD,
            availability=Availability.UNAVAILABLE,
            sport=sport, league=league, market=market, selection=selection,
            line=threshold,
            canonical_player_id=canonical_player_id,
            notes=f"history_fetch_error:{e.__class__.__name__}",
        )]
    if not rows:
        return [EvidenceItem(
            evidence_type=EvidenceType.HISTORICAL_EXACT_THRESHOLD,
            availability=Availability.UNAVAILABLE,
            sport=sport, league=league, market=market, selection=selection,
            line=threshold,
            canonical_player_id=canonical_player_id,
            notes="no player_game_actuals rows for this player + stat.",
            provenance={"stat_key": stat_key},
        )]

    def _pluck(subset: list[dict]) -> list[float]:
        vals = []
        for r in subset:
            v = (r.get("actuals") or {}).get(stat_key)
            if v is None:
                continue
            try:
                vals.append(float(v))
            except (TypeError, ValueError):
                continue
        return vals

    out: list[EvidenceItem] = []
    for w in windows:
        cap = _WINDOW_LIMIT.get(w, 20)
        subset = rows[:cap]
        obs = _pluck(subset)
        n = len(obs)
        if n == 0:
            out.append(EvidenceItem(
                evidence_type=EvidenceType.HISTORICAL_EXACT_THRESHOLD,
                availability=Availability.UNAVAILABLE,
                sport=sport, league=league, market=market,
                selection=selection, line=threshold,
                canonical_player_id=canonical_player_id,
                time_window=w,
                provenance={"stat_key": stat_key,
                             "requested_limit": cap},
            ))
            continue
        hits = _hits(obs, threshold, direction)
        rate = float(hits) / n
        av = availability_from(rate, sample_size=n, min_sample=min_sample)
        mean_v = sum(obs) / n
        try:
            std_v = statistics.stdev(obs) if n > 1 else 0.0
        except statistics.StatisticsError:
            std_v = 0.0
        q10  = _quantile(obs, 0.10)
        q25  = _quantile(obs, 0.25)
        med  = _quantile(obs, 0.50)
        q75  = _quantile(obs, 0.75)
        q90  = _quantile(obs, 0.90)
        iqr  = (q75 - q25) if (q75 is not None and q25 is not None) else None
        direction_flag = ("positive" if rate >= 0.6
                            else "negative" if rate <= 0.4 else "neutral")
        out.append(EvidenceItem(
            evidence_type=EvidenceType.HISTORICAL_EXACT_THRESHOLD,
            availability=av,
            sport=sport, league=league, market=market,
            selection=selection, line=threshold,
            canonical_player_id=canonical_player_id,
            value=round(rate, 4),
            label=f"{hits}/{n} @ {direction} {threshold}",
            direction=direction_flag,
            confidence=min(1.0, n / 20.0),
            sample_size=n, time_window=w,
            source="player_game_actuals",
            source_class="authoritative",
            provenance={
                "stat_key":     stat_key,
                "threshold":    threshold,
                "direction":    direction,
                "mean":         round(mean_v, 4),
                "std":          round(std_v, 4),
                "q10":          q10, "q25": q25, "median": med,
                "q75":          q75, "q90": q90, "iqr": iqr,
                "raw_hits":     hits,
                "raw_obs":      n,
                "identity_class": identity_class,
            },
        ))
    return out


__all__ = ["compute_exact_threshold_evidence"]
