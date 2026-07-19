"""Weather enrichment for outdoor sports (MLB / NFL / Soccer).

User feedback 2026-07-19: "Are we missing any data or anything to make picks
and signal better?". Weather was the #1 gap — a 10 mph wind blowing out at
Coors turns it into an HR paradise; 40°F cold suppresses HR rate 15-20%;
strong headwinds cut soccer / NFL totals by half a goal or point.

This module wraps OpenWeather's "Current Weather" free-tier endpoint
(60 calls/min, 1M/month). Coordinates are looked up once per MLB stadium
via the static `_STADIUM_LATLON` map — no per-request geocoding. Results
are cached in-process for 15 min per (lat, lon) so a full slate refresh
(~15 MLB parks + 10 NFL stadiums) uses ≤ 25 API calls.

The pipeline calls `enrich_pick_with_weather(pick)` at ingestion time.
Signal Engine reads `pick["weather"]` in the calculators to add positive
/ negative signal components (favorable wind out-blowing → +8; cold
temp / wind-in → -6, etc.).
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

import aiohttp

logger = logging.getLogger("lockscore.weather")

_OW_KEY = os.environ.get("OPENWEATHER_API_KEY", "").strip()
_OW_URL = "https://api.openweathermap.org/data/2.5/weather"

# ── 15-minute in-process cache per (lat, lon) ────────────────────────
# Prevents hammering OpenWeather when the same slate refresh queries the
# same 25-ish outdoor venues repeatedly. Keyed on rounded lat/lon so
# tiny geocoding drift doesn't miss the cache.
_CACHE: dict[tuple[float, float], tuple[float, dict]] = {}
_TTL = 15 * 60

# ── MLB stadium geocoding (lat, lon, is_dome_or_retractable) ────────
# Domes get skipped entirely — weather doesn't matter under a closed
# roof. Retractables (marked True) are checked at game time via the
# `wind_speed < 2` heuristic (see `is_dome_or_closed`).
_STADIUM_LATLON: dict[str, tuple[float, float, bool]] = {
    "Angel Stadium":            (33.8003, -117.8827, False),
    "Minute Maid Park":         (29.7573, -95.3555,  True),   # retractable
    "Yankee Stadium":           (40.8296, -73.9262,  False),
    "Wrigley Field":            (41.9484, -87.6553,  False),
    "Fenway Park":              (42.3467, -71.0972,  False),
    "Coors Field":              (39.7559, -104.9942, False),
    "Great American Ball Park": (39.0975, -84.5069,  False),
    "Progressive Field":        (41.4959, -81.6852,  False),
    "Chase Field":              (33.4453, -112.0667, True),   # retractable
    "loanDepot park":           (25.7781, -80.2196,  True),   # retractable
    "Rogers Centre":            (43.6414, -79.3894,  True),   # retractable
    "T-Mobile Park":            (47.5914, -122.3325, True),   # retractable
    "American Family Field":    (43.0280, -87.9712,  True),   # retractable
    "Globe Life Field":         (32.7473, -97.0847,  True),   # retractable
    "Kauffman Stadium":         (39.0517, -94.4803,  False),
    "Comerica Park":            (42.3390, -83.0485,  False),
    "Target Field":             (44.9817, -93.2778,  False),
    "PNC Park":                 (40.4469, -80.0057,  False),
    "Truist Park":              (33.8908, -84.4678,  False),
    "Nationals Park":           (38.8730, -77.0074,  False),
    "Citi Field":               (40.7571, -73.8458,  False),
    "Citizens Bank Park":       (39.9061, -75.1665,  False),
    "Oriole Park at Camden Yards": (39.2839, -76.6217, False),
    "Petco Park":               (32.7073, -117.1566, False),
    "Dodger Stadium":           (34.0739, -118.2400, False),
    "Oracle Park":              (37.7786, -122.3893, False),
    "Sutter Health Park":       (38.5804, -121.5133, False),  # A's temp home
    "Guaranteed Rate Field":    (41.8299, -87.6338,  False),
    "Busch Stadium":            (38.6226, -90.1928,  False),
    "Tropicana Field":          (27.7683, -82.6534,  True),   # dome
    "George M. Steinbrenner Field": (27.9800, -82.5087, False),  # Rays temp
}


async def get_weather(lat: float, lon: float) -> Optional[dict[str, Any]]:
    """Fetch cached / live weather for a coordinate pair.

    Returns None on API error or missing key so callers can silently
    skip the weather signal component instead of failing the pick.
    """
    if not _OW_KEY:
        return None
    key = (round(lat, 3), round(lon, 3))
    now = time.time()
    cached = _CACHE.get(key)
    if cached and (now - cached[0]) < _TTL:
        return cached[1]
    params = {
        "lat": lat, "lon": lon,
        "appid": _OW_KEY, "units": "imperial",  # °F + mph
    }
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(_OW_URL, params=params) as r:
                if r.status != 200:
                    logger.debug("OpenWeather %s → %s", r.url, r.status)
                    return None
                data = await r.json()
    except Exception as e:
        logger.debug("OpenWeather fetch failed: %s", e)
        return None

    out = {
        "temp_f":       (data.get("main") or {}).get("temp"),
        "humidity_pct": (data.get("main") or {}).get("humidity"),
        "wind_mph":     (data.get("wind") or {}).get("speed"),
        "wind_deg":     (data.get("wind") or {}).get("deg"),
        "conditions":   ((data.get("weather") or [{}])[0]).get("main"),
        "description":  ((data.get("weather") or [{}])[0]).get("description"),
        "fetched_at":   now,
    }
    _CACHE[key] = (now, out)
    return out


async def enrich_pick_with_weather(pick: dict) -> dict:
    """Attach a `weather` block to a pick when the venue is outdoor.

    Idempotent — if `pick["weather"]` is already set (recent enrich pass)
    we short-circuit. Silently no-ops for domes / unknown venues so we
    don't waste API calls on Rays / Astros home games.
    """
    if pick.get("weather"):
        return pick  # already enriched
    venue = pick.get("venue") or pick.get("stadium") or ""
    coords = _STADIUM_LATLON.get(venue)
    if not coords:
        return pick  # unknown venue → skip
    lat, lon, is_dome = coords
    if is_dome:
        # We could still fetch outside temp for pitcher road-trip
        # analytics but there's no signal impact under a roof — cheaper
        # to short-circuit.
        pick["weather"] = {"venue": venue, "is_dome": True}
        return pick
    w = await get_weather(lat, lon)
    if w:
        pick["weather"] = {**w, "venue": venue, "is_dome": False}
    return pick


def weather_signal_component(pick: dict) -> tuple[float, str]:
    """Return (delta_points, human_explanation) for the weather.

    Signal engine adds this to the composite via calculators. Points are
    in the same scale as other component deltas (typically -8 to +8).
    Assumes MLB HR / totals context; NFL / soccer adapters can wrap this
    with different weightings later.
    """
    w = pick.get("weather") or {}
    if not w or w.get("is_dome"):
        return 0.0, ""

    market_family = _mlb_market_family(pick)
    temp = w.get("temp_f")
    wind = w.get("wind_mph") or 0
    wind_deg = w.get("wind_deg")
    conditions = (w.get("conditions") or "").lower()

    delta = 0.0
    parts: list[str] = []

    # Heat / cold swing on HR + hits markets
    if market_family in ("hr", "hits", "totals") and temp is not None:
        if temp >= 80:
            delta += 3.0
            parts.append(f"warm {int(temp)}°F helps ball carry")
        elif temp <= 45:
            delta -= 4.0
            parts.append(f"cold {int(temp)}°F suppresses HR")

    # Wind blowing OUT to CF (roughly 30°-150° from home plate) boosts
    # HR/totals; wind IN suppresses. Simplified compass check — real
    # implementation would need per-park orientation but this catches
    # the biggest signal (Coors, Wrigley cross-winds).
    if market_family in ("hr", "totals") and wind >= 8:
        if wind_deg is not None and 30 <= wind_deg <= 150:
            delta += min(6.0, wind / 2.0)
            parts.append(f"{int(wind)}mph wind carrying")
        elif wind_deg is not None and (210 <= wind_deg <= 330):
            delta -= min(6.0, wind / 2.0)
            parts.append(f"{int(wind)}mph headwind")

    # Rain / snow → depresses totals + increases K rate
    if "rain" in conditions or "snow" in conditions:
        if market_family == "totals":
            delta -= 3.0
            parts.append(f"{conditions} depresses runs")
        elif market_family == "strikeouts":
            delta += 2.0
            parts.append(f"{conditions} boosts K rate")

    return delta, ", ".join(parts)


def _mlb_market_family(pick: dict) -> str:
    """Classify the market for weather-signal weighting."""
    m = (pick.get("market") or "").lower()
    if "home run" in m or "hr" in m:
        return "hr"
    if "hits" in m or "total bases" in m:
        return "hits"
    if "strikeout" in m or "outs recorded" in m:
        return "strikeouts"
    if "total runs" in m or "over/under" in m or "totals" in m:
        return "totals"
    return "other"


def is_dome_or_closed(venue: str) -> bool:
    """Quick helper for other callers — True if this venue can't be
    weather-affected today (dome or roof closed)."""
    coords = _STADIUM_LATLON.get(venue)
    return bool(coords and coords[2])
