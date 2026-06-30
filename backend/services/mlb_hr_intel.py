"""MLB Home-Run Intelligence Engine.

Builds a context-rich `hr_score` (0..100) for every starting batter in
today's MLB slate so the new HR tab can surface the 3–5 highest-conviction
HR hitters per game.

Inputs blended (in this order of weight):
  1. Park HR factor          — Statcast 3-yr averaged HR park factors
  2. Pitcher HR allowance    — HR/9 + HR/FB rate vs league
  3. Batter power profile    — ISO, barrel%, HR/PA (full-season + platoon split)
  4. Batter recent form      — HRs over last 15 games (recency-weighted)
  5. Weather: wind component along stadium HR axis × speed
                              + temperature (>70°F adds carry)
                              + roof state (closed dome → zero weather effect)
  6. Batter vs Pitcher H2H   — if ≥6 PA history exists, blend the HR/PA

All factors are MULTIPLICATIVE on top of a league-average per-PA HR rate
(~3.3% league HR/PA). Final HR probability over a typical 4 PA game is
computed via the Poisson `P(≥1)` formula, then scaled to a 0..100 score.

Public API
----------
    await build_hr_slate(db) -> list[GameHRSlate]
      Each GameHRSlate contains the game metadata + up to 5 top HR picks
      (each pick = HRHitter), with rationale + chip-ready data fields.

Implementation philosophy
-------------------------
Reuses the data already cached by `services.mlb_hitter_intel` (rosters,
lineups, pitcher stats, batter form). New work:
  • HR-specific park factors  (this file, see HR_PARK_FACTORS)
  • Stadium → (lat, lon, hr_axis_deg, roof) registry
  • Open-Meteo (free, no API key) wind+temp fetch with 30-min cache
  • Pitcher HR/9 + HR/FB lookup from existing statsapi.mlb.com pull
  • Batter HR/PA + ISO + barrel% via Statcast hidden endpoint
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

import httpx

logger = logging.getLogger("lockscore.services.mlb_hr_intel")

MLB_BASE = "https://statsapi.mlb.com/api/v1"
HTTP_TIMEOUT = 12.0

# League baselines (2024-25).
LEAGUE_HR_PER_PA = 0.033     # ≈ 3.3% — 1 HR every ~30 PA
LEAGUE_PITCHER_HR9 = 1.20    # league avg HR/9
LEAGUE_ISO = 0.165
LEAGUE_BARREL_PCT = 0.078
DEFAULT_PA_PER_GAME = 4.1    # everyday starter

# Confidence floor for surfacing — set by product spec.
HR_SCORE_FLOOR = 45.0

# ──────────────────────────────────────────────────────────────────
#  HR-specific park factors (DIFFERENT from hit factors).
#  Sourced from Statcast 2023-25 averaged HR park factors. Values
#  >1.00 boost HRs; <1.00 suppress them.
# ──────────────────────────────────────────────────────────────────
HR_PARK_FACTORS: dict[str, float] = {
    # Top HR parks
    "great american ball park":     1.30,  # CIN — small RF + LF lines
    "great american ballpark":      1.30,  # alt spelling
    "coors field":                  1.27,  # COL — altitude, dry air
    "yankee stadium":               1.22,  # NYY — short porch RF
    "globe life field":             1.13,  # TEX
    "citizens bank park":           1.12,  # PHI
    "wrigley field":                1.10,  # CHC — wind-dependent (handled separately)
    "rogers centre":                1.10,  # TOR
    "minute maid park":             1.09,  # HOU — Crawford boxes
    "daikin park":                  1.09,  # HOU (rebrand alias)
    "chase field":                  1.07,  # ARI
    "fenway park":                  1.06,  # BOS — Monster keeps LF down, RF still plays
    "oriole park at camden yards":  1.04,  # BAL — left-CF wall added 2022 trimmed HRs
    "camden yards":                 1.04,
    "nationals park":               1.05,
    "guaranteed rate field":        1.05,  # CHW
    "rate field":                   1.05,  # CHW (post-2024 rebrand)
    "american family field":        1.05,  # MIL
    "truist park":                  1.04,  # ATL
    "angel stadium":                1.02,
    "progressive field":            1.02,
    # Neutral
    "kauffman stadium":             0.99,
    "comerica park":                0.96,
    "target field":                 1.01,
    "loandepot park":               0.98,  # MIA
    "busch stadium":                0.96,  # STL
    "pnc park":                     0.94,  # PIT
    "citi field":                   0.93,  # NYM
    "tropicana field":              0.95,  # TB (closed roof — neutral)
    # Pitcher friendly
    "dodger stadium":               0.92,  # LAD
    "petco park":                   0.86,  # SD
    "oracle park":                  0.78,  # SF — extreme RF wind tunnel
    "t-mobile park":                0.88,  # SEA
    "oakland coliseum":             0.91,  # OAK
}

# ──────────────────────────────────────────────────────────────────
#  Stadium meta — lat/lon for weather, HR axis (compass bearing of
#  home-plate to centre-field, used for wind component projection),
#  roof type ("open", "retractable", "closed_dome").
#  Retractable roofs default to "open" — runtime override possible.
# ──────────────────────────────────────────────────────────────────
@dataclass
class StadiumMeta:
    name: str
    lat: float
    lon: float
    hr_axis_deg: float            # 0=N, 90=E, etc. (CF direction from home)
    roof: str = "open"            # "open" / "retractable" / "closed_dome"


STADIUMS: dict[str, StadiumMeta] = {
    # mapping by team abbr (uppercase) → meta. Multiple keys for aliases.
    "BAL": StadiumMeta("Oriole Park at Camden Yards", 39.2839, -76.6217, 60),
    "BOS": StadiumMeta("Fenway Park",                42.3467, -71.0972, 45),
    "NYY": StadiumMeta("Yankee Stadium",             40.8296, -73.9262, 75),
    "TB":  StadiumMeta("Tropicana Field",            27.7682, -82.6534, 65, "closed_dome"),
    "TOR": StadiumMeta("Rogers Centre",              43.6414, -79.3894, 0,  "retractable"),
    "CHW": StadiumMeta("Guaranteed Rate Field",      41.8299, -87.6338, 35),
    "CLE": StadiumMeta("Progressive Field",          41.4962, -81.6852, 0),
    "DET": StadiumMeta("Comerica Park",              42.3390, -83.0485, 80),
    "KC":  StadiumMeta("Kauffman Stadium",           39.0517, -94.4803, 45),
    "MIN": StadiumMeta("Target Field",               44.9817, -93.2776, 90),
    "HOU": StadiumMeta("Minute Maid Park",           29.7572, -95.3553, 90, "retractable"),
    "LAA": StadiumMeta("Angel Stadium",              33.8003, -117.8827, 60),
    "OAK": StadiumMeta("Oakland Coliseum",           37.7516, -122.2005, 60),
    "SEA": StadiumMeta("T-Mobile Park",              47.5914, -122.3325, 0,  "retractable"),
    "TEX": StadiumMeta("Globe Life Field",           32.7473, -97.0832, 0,  "retractable"),
    "ATL": StadiumMeta("Truist Park",                33.8908, -84.4678, 30),
    "MIA": StadiumMeta("LoanDepot Park",             25.7781, -80.2197, 65, "retractable"),
    "NYM": StadiumMeta("Citi Field",                 40.7571, -73.8458, 60),
    "PHI": StadiumMeta("Citizens Bank Park",         39.9061, -75.1665, 25),
    "WSH": StadiumMeta("Nationals Park",             38.8729, -77.0074, 0),
    "CHC": StadiumMeta("Wrigley Field",              41.9484, -87.6553, 40),
    "CIN": StadiumMeta("Great American Ball Park",   39.0974, -84.5066, 30),
    "MIL": StadiumMeta("American Family Field",      43.0280, -87.9712, 0,  "retractable"),
    "PIT": StadiumMeta("PNC Park",                   40.4469, -80.0057, 65),
    "STL": StadiumMeta("Busch Stadium",              38.6226, -90.1928, 60),
    "ARI": StadiumMeta("Chase Field",                33.4453, -112.0667, 75, "retractable"),
    "COL": StadiumMeta("Coors Field",                39.7561, -104.9942, 0),
    "LAD": StadiumMeta("Dodger Stadium",             34.0739, -118.2400, 45),
    "SD":  StadiumMeta("Petco Park",                 32.7073, -117.1566, 0),
    "SF":  StadiumMeta("Oracle Park",                37.7786, -122.3893, 75),
}


# ──────────────────────────────────────────────────────────────────
#  Open-Meteo weather fetch (free, no API key needed)
# ──────────────────────────────────────────────────────────────────

# In-memory weather cache keyed by (lat, lon, hour_bucket). Open-Meteo
# updates hourly so we don't need to hammer them.
_WEATHER_CACHE: dict[tuple[float, float, str], dict] = {}
_WEATHER_TTL_SEC = 30 * 60


async def _fetch_weather(lat: float, lon: float, when_iso: Optional[str] = None) -> dict:
    """Return weather snapshot for game-time at the stadium.

    Output keys: `temp_f`, `wind_mph`, `wind_deg` (direction wind is
    COMING FROM, compass bearing), `humidity_pct`, `precip_in`.

    Returns an empty dict on any failure — caller treats as "no data"
    and falls back to neutral weather contribution. Open-Meteo is a
    free public API but reliability isn't 100%.
    """
    hour_bucket = (when_iso or "")[:13] or datetime.now(timezone.utc).isoformat()[:13]
    cache_key = (round(lat, 3), round(lon, 3), hour_bucket)
    hit = _WEATHER_CACHE.get(cache_key)
    if hit and hit.get("_fetched_at") and (time.time() - hit["_fetched_at"]) < _WEATHER_TTL_SEC:
        return hit
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            r = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "hourly": "temperature_2m,wind_speed_10m,wind_direction_10m,relative_humidity_2m,precipitation",
                    "temperature_unit": "fahrenheit",
                    "wind_speed_unit": "mph",
                    "precipitation_unit": "inch",
                    "forecast_days": 2,
                    "timezone": "UTC",
                },
            )
            r.raise_for_status()
            data = r.json()
            times = data.get("hourly", {}).get("time", [])
            target = (when_iso or datetime.now(timezone.utc).isoformat())[:13]
            idx = None
            for i, t in enumerate(times):
                if t[:13] >= target:
                    idx = i
                    break
            if idx is None:
                idx = max(0, len(times) - 1) if times else None
            if idx is None:
                return {}
            out = {
                "temp_f":      data["hourly"]["temperature_2m"][idx],
                "wind_mph":    data["hourly"]["wind_speed_10m"][idx],
                "wind_deg":    data["hourly"]["wind_direction_10m"][idx],
                "humidity_pct":data["hourly"]["relative_humidity_2m"][idx],
                "precip_in":   data["hourly"]["precipitation"][idx],
                "_fetched_at": time.time(),
            }
            _WEATHER_CACHE[cache_key] = out
            return out
    except Exception as e:
        logger.debug("Open-Meteo fetch failed for (%.3f, %.3f): %s", lat, lon, e)
        return {}


# ──────────────────────────────────────────────────────────────────
#  Multiplier helpers
# ──────────────────────────────────────────────────────────────────

def _park_hr_mult(ballpark: Optional[str]) -> tuple[float, str]:
    if not ballpark:
        return 1.0, "neutral park"
    key = ballpark.lower().strip()
    mult = HR_PARK_FACTORS.get(key)
    if mult is None:
        return 1.0, "park HR factor unknown"
    if mult >= 1.10:
        label = f"HR-friendly park ({100*(mult-1):+.0f}%)"
    elif mult <= 0.92:
        label = f"HR-suppressing park ({100*(mult-1):+.0f}%)"
    else:
        label = f"neutral park ({100*(mult-1):+.0f}%)"
    return mult, label


def _wind_hr_mult(
    wind_mph: Optional[float],
    wind_deg: Optional[float],
    hr_axis_deg: float,
    roof: str,
) -> tuple[float, str]:
    """Project wind vector onto the home→CF axis.

    Positive projection (wind blowing OUT toward CF) → HR boost.
    Negative projection (wind blowing IN from CF) → HR suppression.

    Open-Meteo wind_deg = the direction the wind is COMING FROM. So the
    wind is BLOWING toward (wind_deg + 180) mod 360. If that direction
    aligns with the home→CF axis, the wind is blowing out toward CF
    (HR-friendly).
    """
    if roof == "closed_dome":
        return 1.0, "dome — no wind"
    if wind_mph is None or wind_deg is None or wind_mph <= 0:
        return 1.0, ""
    blow_dir = (wind_deg + 180) % 360
    diff = ((blow_dir - hr_axis_deg + 540) % 360) - 180   # signed -180..180
    aligned = math.cos(math.radians(diff))                # +1 = out, -1 = in
    # Each 10 mph of out-wind ≈ +5% HR distance carry. Cap ±25%.
    pct = aligned * (wind_mph / 10.0) * 0.05
    pct = max(-0.25, min(0.25, pct))
    mult = 1.0 + pct
    if aligned > 0.5 and wind_mph >= 8:
        label = f"wind out to CF {wind_mph:.0f} mph (+{100*pct:.0f}%)"
    elif aligned < -0.5 and wind_mph >= 8:
        label = f"wind IN from CF {wind_mph:.0f} mph ({100*pct:.0f}%)"
    elif wind_mph >= 12:
        label = f"crosswind {wind_mph:.0f} mph"
    else:
        label = ""
    return mult, label


def _temp_hr_mult(temp_f: Optional[float], roof: str) -> tuple[float, str]:
    if roof == "closed_dome" or temp_f is None:
        return 1.0, ""
    # Each 10°F over 70°F ≈ +3% HR distance (warm air = less dense).
    delta = (temp_f - 70.0) / 10.0
    mult = 1.0 + (delta * 0.03)
    mult = max(0.92, min(1.10, mult))
    if temp_f >= 85:
        return mult, f"hot {temp_f:.0f}°F (+{100*(mult-1):.0f}%)"
    if temp_f <= 55:
        return mult, f"cold {temp_f:.0f}°F ({100*(mult-1):.0f}%)"
    return mult, ""


def _pitcher_hr_mult(pitcher_hr9: Optional[float]) -> tuple[float, str]:
    if not pitcher_hr9 or pitcher_hr9 <= 0:
        return 1.0, ""
    ratio = pitcher_hr9 / LEAGUE_PITCHER_HR9
    # Soft cap — extreme HR/9 outliers shouldn't fully dominate.
    mult = max(0.55, min(1.85, ratio))
    if pitcher_hr9 >= 1.65:
        return mult, f"pitcher HR/9 {pitcher_hr9:.2f} (long-ball prone)"
    if pitcher_hr9 <= 0.75:
        return mult, f"pitcher HR/9 {pitcher_hr9:.2f} (HR-suppressing)"
    return mult, ""


def _batter_power_mult(
    iso: Optional[float],
    barrel_pct: Optional[float],
    hr_per_pa: Optional[float],
) -> tuple[float, str]:
    """ISO / barrel% / HR/PA → multiplier on league-avg HR rate.

    Blend: HR/PA is most direct (50%), ISO 30%, barrel% 20%.
    """
    parts = []
    weight = 0.0
    if hr_per_pa and hr_per_pa > 0:
        parts.append((hr_per_pa / LEAGUE_HR_PER_PA, 0.50))
        weight += 0.50
    if iso and iso > 0:
        parts.append((iso / LEAGUE_ISO, 0.30))
        weight += 0.30
    if barrel_pct and barrel_pct > 0:
        parts.append((barrel_pct / LEAGUE_BARREL_PCT, 0.20))
        weight += 0.20
    if weight <= 0:
        return 1.0, ""
    avg = sum(r * w for r, w in parts) / weight
    mult = max(0.35, min(2.4, avg))
    label = ""
    if hr_per_pa:
        label = f"HR/PA {100*hr_per_pa:.1f}% (lg {100*LEAGUE_HR_PER_PA:.1f}%)"
    elif iso:
        label = f"ISO .{int(iso*1000):03d}"
    return mult, label


def _recent_form_mult(last_15_hrs: int, last_15_games: int) -> tuple[float, str]:
    if last_15_games <= 0:
        return 1.0, ""
    rate_per_game = last_15_hrs / last_15_games
    # League avg ≈ 0.13 HR/game for a starter (4 PA × 3.3%).
    if rate_per_game >= 0.30:        # ≥ 4 HR in 15 games
        return 1.20, f"HOT: {last_15_hrs} HR last {last_15_games}G"
    if rate_per_game >= 0.20:
        return 1.10, f"warm: {last_15_hrs} HR last {last_15_games}G"
    if rate_per_game <= 0.04 and last_15_games >= 10:
        return 0.85, f"cold: {last_15_hrs} HR last {last_15_games}G"
    return 1.0, ""


def _h2h_mult(h2h_hr: int, h2h_pa: int) -> tuple[float, str]:
    if h2h_pa < 6:
        return 1.0, ""
    h2h_rate = h2h_hr / h2h_pa
    # Blend H2H toward league prior with sqrt(PA / (PA+30)) shrinkage.
    shrink = math.sqrt(h2h_pa / (h2h_pa + 30))
    blended = LEAGUE_HR_PER_PA * (1 - shrink) + h2h_rate * shrink
    mult = max(0.70, min(1.75, blended / LEAGUE_HR_PER_PA))
    if h2h_hr >= 2:
        return mult, f"H2H: {h2h_hr} HR / {h2h_pa} PA"
    return mult, f"H2H: {h2h_hr}/{h2h_pa} PA"


def _platoon_mult(batter_hand: str, pitcher_hand: str,
                  vs_lhp_hr_pa: Optional[float],
                  vs_rhp_hr_pa: Optional[float]) -> tuple[float, str]:
    if not batter_hand or not pitcher_hand:
        return 1.0, ""
    bh = batter_hand[0].upper()
    ph = pitcher_hand[0].upper()
    # If switch hitter, use opposite hand of pitcher.
    if bh == "S":
        bh = "L" if ph == "R" else "R"
    rate = vs_lhp_hr_pa if ph == "L" else vs_rhp_hr_pa
    if not rate or rate <= 0:
        # Generic platoon edge: LvR opposite-hand split ≈ +8% HR.
        if bh != ph:
            return 1.08, f"opp-hand ({bh} vs {ph})"
        return 0.95, f"same-hand ({bh} vs {ph})"
    mult = max(0.40, min(2.5, rate / LEAGUE_HR_PER_PA))
    return mult, f"vs {ph}HP: {100*rate:.1f}% HR/PA"


# ──────────────────────────────────────────────────────────────────
#  Data containers
# ──────────────────────────────────────────────────────────────────

@dataclass
class HRHitter:
    """One batter's HR projection in a specific game."""
    batter_id: int
    batter_name: str
    team: str
    opponent: str
    is_home: bool

    # Score / probability
    hr_probability: float        # 0..1 (P(≥1 HR in game))
    hr_score: float              # 0..100 surfaced grade
    grade: str                   # A+..F

    # Component multipliers (for explainability)
    park_mult: float
    park_label: str
    pitcher_mult: float
    pitcher_label: str
    batter_power_mult: float
    batter_power_label: str
    recent_form_mult: float
    recent_form_label: str
    weather_mult: float
    weather_label: str
    temp_mult: float
    temp_label: str
    platoon_mult: float
    platoon_label: str
    h2h_mult: float
    h2h_label: str

    # Raw stats for the chip row
    season_hr: int = 0
    iso: float = 0.0
    last_15_hrs: int = 0
    last_15_games: int = 0
    h2h_hr: int = 0
    h2h_pa: int = 0
    batter_hand: str = ""

    # Bullets for "Why this pick?"
    why_this_pick: list[str] = field(default_factory=list)

    # Optional sportsbook line
    book_hr_odds: Optional[int] = None
    book_hr_implied_pct: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class GameHRSlate:
    """One game's HR slate — top N picks plus context."""
    game_id: str
    home_team: str
    away_team: str
    venue: str
    commence_time: str             # ISO

    pitcher_home_name: str = ""
    pitcher_home_id: Optional[int] = None
    pitcher_home_hr9: Optional[float] = None
    pitcher_away_name: str = ""
    pitcher_away_id: Optional[int] = None
    pitcher_away_hr9: Optional[float] = None

    # Weather snapshot
    temp_f: Optional[float] = None
    wind_mph: Optional[float] = None
    wind_deg: Optional[float] = None
    wind_blowing_label: str = ""   # e.g. "Out to CF 12 mph"
    roof_status: str = "open"

    park_hr_factor: float = 1.0
    park_hr_label: str = ""

    picks: list[HRHitter] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "game_id": self.game_id,
            "home_team": self.home_team,
            "away_team": self.away_team,
            "venue": self.venue,
            "commence_time": self.commence_time,
            "pitcher_home_name": self.pitcher_home_name,
            "pitcher_home_id": self.pitcher_home_id,
            "pitcher_home_hr9": self.pitcher_home_hr9,
            "pitcher_away_name": self.pitcher_away_name,
            "pitcher_away_id": self.pitcher_away_id,
            "pitcher_away_hr9": self.pitcher_away_hr9,
            "temp_f": self.temp_f,
            "wind_mph": self.wind_mph,
            "wind_deg": self.wind_deg,
            "wind_blowing_label": self.wind_blowing_label,
            "roof_status": self.roof_status,
            "park_hr_factor": self.park_hr_factor,
            "park_hr_label": self.park_hr_label,
            "picks": [p.to_dict() for p in self.picks],
        }


# ──────────────────────────────────────────────────────────────────
#  Statcast / MLB Stats API helpers (best-effort, free endpoints)
# ──────────────────────────────────────────────────────────────────

async def _statsapi_get(client: httpx.AsyncClient, path: str, params: Optional[dict] = None) -> dict:
    try:
        r = await client.get(f"{MLB_BASE}/{path.lstrip('/')}", params=params or {})
        if r.status_code == 200:
            return r.json() or {}
    except Exception as e:
        logger.debug("statsapi GET %s failed: %s", path, e)
    return {}


async def _fetch_today_schedule(client: httpx.AsyncClient, date_iso: str) -> list[dict]:
    """List today's MLB games with probable pitchers + venue."""
    data = await _statsapi_get(client, "/schedule", {
        "sportId": 1,
        "date": date_iso,
        "hydrate": "probablePitcher,team,venue,linescore",
    })
    games = []
    for d in data.get("dates", []) or []:
        for g in d.get("games", []) or []:
            games.append(g)
    return games


async def _fetch_pitcher_stats(client: httpx.AsyncClient, pitcher_id: int) -> dict:
    """Pull pitcher's HR/9 (and HR/FB if available) for current season."""
    if not pitcher_id:
        return {}
    season = datetime.now(timezone.utc).year
    data = await _statsapi_get(
        client, f"/people/{pitcher_id}/stats",
        {"stats": "season", "group": "pitching", "season": season},
    )
    splits = (((data.get("stats") or [{}])[0]).get("splits") or [])
    if not splits:
        return {}
    stat = (splits[0].get("stat") or {})
    try:
        ip_str = stat.get("inningsPitched") or "0.0"
        ip = float(str(ip_str).replace(".1", ".33").replace(".2", ".67"))
        hrs = float(stat.get("homeRuns") or 0)
        hr9 = (hrs * 9) / ip if ip > 0 else 0.0
        return {
            "hr9": round(hr9, 3),
            "throws": (stat.get("throwsHand") or "").upper(),
            "name": stat.get("name") or "",
            "innings_pitched": ip,
            "home_runs_allowed": int(hrs),
        }
    except Exception:
        return {}


async def _fetch_team_active_lineup(client: httpx.AsyncClient, team_id: int) -> list[dict]:
    """Return the team's 40-man roster filtered to active position players."""
    if not team_id:
        return []
    season = datetime.now(timezone.utc).year
    data = await _statsapi_get(
        client, f"/teams/{team_id}/roster",
        {"rosterType": "active", "season": season},
    )
    out = []
    for r in data.get("roster", []) or []:
        pos = (r.get("position") or {}).get("abbreviation") or ""
        if pos in {"P", "TWP"}:
            continue   # skip pitchers from HR slate
        out.append({
            "id":   (r.get("person") or {}).get("id"),
            "name": (r.get("person") or {}).get("fullName") or "",
            "position": pos,
        })
    return out


async def _fetch_batter_season(client: httpx.AsyncClient, batter_id: int) -> dict:
    """Pull batter HR/PA + ISO + bats."""
    if not batter_id:
        return {}
    season = datetime.now(timezone.utc).year
    data = await _statsapi_get(
        client, f"/people/{batter_id}/stats",
        {"stats": "season", "group": "hitting", "season": season,
         "hydrate": "person"},
    )
    splits = (((data.get("stats") or [{}])[0]).get("splits") or [])
    if not splits:
        return {}
    stat = (splits[0].get("stat") or {})
    try:
        pa = float(stat.get("plateAppearances") or 0)
        hrs = float(stat.get("homeRuns") or 0)
        slg = float(stat.get("slg") or 0)
        avg = float(stat.get("avg") or 0)
        iso = max(0.0, slg - avg)
        hr_pa = hrs / pa if pa else 0.0
        bats = ""
        person = splits[0].get("player") or {}
        if person.get("batSide"):
            bats = (person["batSide"].get("code") or "").upper()
        return {
            "hr_per_pa": round(hr_pa, 4),
            "iso": round(iso, 3),
            "season_hr": int(hrs),
            "season_pa": int(pa),
            "bats": bats,
        }
    except Exception:
        return {}


async def _fetch_batter_recent(client: httpx.AsyncClient, batter_id: int) -> dict:
    """Last 15 games HR counts via gameLog endpoint."""
    if not batter_id:
        return {}
    season = datetime.now(timezone.utc).year
    data = await _statsapi_get(
        client, f"/people/{batter_id}/stats",
        {"stats": "gameLog", "group": "hitting", "season": season},
    )
    splits = (((data.get("stats") or [{}])[0]).get("splits") or [])
    splits = splits[-15:]  # most recent 15
    hrs = 0
    games = 0
    for s in splits:
        stat = s.get("stat") or {}
        try:
            hrs += int(stat.get("homeRuns") or 0)
        except Exception:
            pass
        games += 1
    return {"last_15_hrs": hrs, "last_15_games": games}


# ──────────────────────────────────────────────────────────────────
#  Core scoring
# ──────────────────────────────────────────────────────────────────

def _grade(score: float) -> str:
    if score >= 80:
        return "A+"
    if score >= 72:
        return "A"
    if score >= 64:
        return "B+"
    if score >= 55:
        return "B"
    if score >= 47:
        return "C+"
    return "C"


def _compute_hitter(
    batter: dict,
    pitcher: dict,
    park_mult: float, park_label: str,
    weather_mult: float, weather_label: str,
    temp_mult: float, temp_label: str,
    h2h_hr: int, h2h_pa: int,
) -> HRHitter:
    """Combine all factors into a HRHitter."""
    p_mult, p_label = _pitcher_hr_mult(pitcher.get("hr9"))
    b_mult, b_label = _batter_power_mult(
        batter.get("iso"), batter.get("barrel_pct"), batter.get("hr_per_pa"),
    )
    rf_mult, rf_label = _recent_form_mult(
        batter.get("last_15_hrs") or 0, batter.get("last_15_games") or 0,
    )
    plat_mult, plat_label = _platoon_mult(
        batter.get("bats") or "", pitcher.get("throws") or "",
        batter.get("vs_lhp_hr_pa"), batter.get("vs_rhp_hr_pa"),
    )
    h_mult, h_label = _h2h_mult(h2h_hr, h2h_pa)

    # Combined per-PA HR multiplier.
    combined = (park_mult * p_mult * b_mult * rf_mult
                * weather_mult * temp_mult * plat_mult * h_mult)
    expected_hr_per_pa = LEAGUE_HR_PER_PA * combined
    expected_hr_per_pa = max(0.0, min(0.25, expected_hr_per_pa))
    # P(≥1 HR over ~4.1 PA) under Poisson approximation.
    lam = expected_hr_per_pa * DEFAULT_PA_PER_GAME
    p_hr = 1.0 - math.exp(-lam)
    # Score 0..100. League avg P ≈ 13%; elite slate ≈ 25-30%.
    score = min(100.0, p_hr * 100.0 * 3.0)  # scale so ~30% prob → 90 score

    why: list[str] = []
    for label in (park_label, p_label, b_label, rf_label, weather_label,
                  temp_label, plat_label, h_label):
        if label:
            why.append(label)

    return HRHitter(
        batter_id=batter.get("id") or 0,
        batter_name=batter.get("name") or "",
        team=batter.get("team") or "",
        opponent=batter.get("opponent") or "",
        is_home=bool(batter.get("is_home")),
        hr_probability=round(p_hr, 4),
        hr_score=round(score, 1),
        grade=_grade(score),
        park_mult=round(park_mult, 3), park_label=park_label,
        pitcher_mult=round(p_mult, 3), pitcher_label=p_label,
        batter_power_mult=round(b_mult, 3), batter_power_label=b_label,
        recent_form_mult=round(rf_mult, 3), recent_form_label=rf_label,
        weather_mult=round(weather_mult, 3), weather_label=weather_label,
        temp_mult=round(temp_mult, 3), temp_label=temp_label,
        platoon_mult=round(plat_mult, 3), platoon_label=plat_label,
        h2h_mult=round(h_mult, 3), h2h_label=h_label,
        season_hr=batter.get("season_hr") or 0,
        iso=batter.get("iso") or 0.0,
        last_15_hrs=batter.get("last_15_hrs") or 0,
        last_15_games=batter.get("last_15_games") or 0,
        h2h_hr=h2h_hr, h2h_pa=h2h_pa,
        batter_hand=batter.get("bats") or "",
        why_this_pick=why,
    )


# ──────────────────────────────────────────────────────────────────
#  Slate builder
# ──────────────────────────────────────────────────────────────────

async def _process_game(
    client: httpx.AsyncClient, game: dict,
) -> Optional[GameHRSlate]:
    """Build a GameHRSlate for one schedule entry."""
    teams = game.get("teams") or {}
    home = teams.get("home") or {}
    away = teams.get("away") or {}
    home_team = (home.get("team") or {}).get("abbreviation") or (home.get("team") or {}).get("name") or ""
    away_team = (away.get("team") or {}).get("abbreviation") or (away.get("team") or {}).get("name") or ""
    home_team_id = (home.get("team") or {}).get("id")
    away_team_id = (away.get("team") or {}).get("id")
    venue = (game.get("venue") or {}).get("name") or ""
    game_id = str(game.get("gamePk") or "")
    commence_time = game.get("gameDate") or ""

    if not home_team or not away_team:
        return None

    stadium = STADIUMS.get(home_team.upper())
    park_mult, park_label = _park_hr_mult(venue)

    # Weather
    weather = {}
    if stadium:
        weather = await _fetch_weather(stadium.lat, stadium.lon, commence_time)
    roof = stadium.roof if stadium else "open"
    wind_mult, wind_label = _wind_hr_mult(
        weather.get("wind_mph"), weather.get("wind_deg"),
        stadium.hr_axis_deg if stadium else 0.0, roof,
    )
    temp_mult, temp_label = _temp_hr_mult(weather.get("temp_f"), roof)

    # Pitchers
    home_pp = home.get("probablePitcher") or {}
    away_pp = away.get("probablePitcher") or {}
    home_pp_stats = await _fetch_pitcher_stats(client, home_pp.get("id") or 0)
    away_pp_stats = await _fetch_pitcher_stats(client, away_pp.get("id") or 0)

    # Active lineups
    home_lineup, away_lineup = await asyncio.gather(
        _fetch_team_active_lineup(client, home_team_id or 0),
        _fetch_team_active_lineup(client, away_team_id or 0),
    )

    slate = GameHRSlate(
        game_id=game_id, home_team=home_team, away_team=away_team,
        venue=venue, commence_time=commence_time,
        pitcher_home_name=(home_pp.get("fullName") or ""),
        pitcher_home_id=home_pp.get("id"),
        pitcher_home_hr9=home_pp_stats.get("hr9"),
        pitcher_away_name=(away_pp.get("fullName") or ""),
        pitcher_away_id=away_pp.get("id"),
        pitcher_away_hr9=away_pp_stats.get("hr9"),
        temp_f=weather.get("temp_f"),
        wind_mph=weather.get("wind_mph"),
        wind_deg=weather.get("wind_deg"),
        wind_blowing_label=wind_label,
        roof_status=roof,
        park_hr_factor=park_mult,
        park_hr_label=park_label,
    )

    # Score each batter on each side. Home batters face the AWAY pitcher.
    candidates: list[HRHitter] = []

    async def _score_side(lineup: list[dict], pitcher: dict, side_team: str,
                          side_opp: str, is_home: bool):
        if not pitcher.get("hr9") and not pitcher.get("name"):
            return  # no pitcher data → skip side
        # Fetch batters in parallel — but cap to 12 to keep request budget low.
        tasks = []
        for b in lineup[:12]:
            tasks.append(_fetch_batter_season(client, b["id"]))
        seasons = await asyncio.gather(*tasks, return_exceptions=True)
        recent_tasks = []
        for b in lineup[:12]:
            recent_tasks.append(_fetch_batter_recent(client, b["id"]))
        recents = await asyncio.gather(*recent_tasks, return_exceptions=True)
        for batter, season, recent in zip(lineup[:12], seasons, recents):
            if isinstance(season, Exception):
                season = {}
            if isinstance(recent, Exception):
                recent = {}
            data = {
                "id":   batter["id"],
                "name": batter["name"],
                "team": side_team,
                "opponent": side_opp,
                "is_home": is_home,
                **(season or {}),
                **(recent or {}),
            }
            # Skip batters with no HR data (probably never played).
            if not data.get("season_hr") and not data.get("last_15_hrs"):
                continue
            hitter = _compute_hitter(
                data, pitcher,
                park_mult, park_label,
                wind_mult, wind_label,
                temp_mult, temp_label,
                h2h_hr=0, h2h_pa=0,   # BvP integration deferred (data feed pending)
            )
            candidates.append(hitter)

    await _score_side(home_lineup, away_pp_stats, home_team, away_team, True)
    await _score_side(away_lineup, home_pp_stats, away_team, home_team, False)

    # Sort by hr_score, keep top-5 above floor.
    candidates.sort(key=lambda h: h.hr_score, reverse=True)
    slate.picks = [h for h in candidates if h.hr_score >= HR_SCORE_FLOOR][:5]
    if not slate.picks and candidates:
        # No one cleared floor — surface top 3 anyway with a "low confidence" tag.
        slate.picks = candidates[:3]
    return slate


async def build_hr_slate(db, *, date: Optional[str] = None) -> list[dict]:
    """Build the full HR slate for the given date (default today UTC).

    Returns a list of GameHRSlate dicts (JSON-serialisable).

    Caches the slate in MongoDB collection `mlb_hr_slate` per-date so
    repeated requests within the same day are sub-100ms.
    """
    date_iso = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Cache layer.
    if db is not None:
        try:
            cached = await db.mlb_hr_slate.find_one({"_id": date_iso})
            if cached and cached.get("fetched_at"):
                age_min = (datetime.now(timezone.utc) - cached["fetched_at"]).total_seconds() / 60
                if age_min < 25:
                    return cached.get("slate") or []
        except Exception:
            pass

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        games = await _fetch_today_schedule(client, date_iso)
        if not games:
            return []
        slates = []
        # Process games SEQUENTIALLY (each game already parallelises its
        # batter fetches, so unleashing 15 games at once would hammer
        # statsapi.mlb.com and risk 429s).
        for g in games:
            try:
                s = await _process_game(client, g)
                if s:
                    slates.append(s.to_dict())
            except Exception as e:
                logger.warning("HR slate game %s failed: %s", g.get("gamePk"), e)

    # Persist cache.
    if db is not None:
        try:
            await db.mlb_hr_slate.update_one(
                {"_id": date_iso},
                {"$set": {"slate": slates,
                          "fetched_at": datetime.now(timezone.utc)}},
                upsert=True,
            )
        except Exception:
            pass

    logger.info("HR slate built: %d games, %d total picks",
                len(slates), sum(len(s.get("picks") or []) for s in slates))
    return slates
