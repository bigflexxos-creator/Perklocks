"""football-data.co.uk source — historical results + closing odds.

Free, no key, comprehensive. Ships CSV files with one row per match:
    Date, HomeTeam, AwayTeam, FTHG (full-time home goals), FTAG,
    FTR (result: H/D/A), HTHG (half-time), HTAG, HTR,
    Referee, HS/AS (shots), HST/AST (shots on target), HF/AF (fouls),
    HC/AC (corners), HY/AY (yellow cards), HR/AR (reds),
    B365H, B365D, B365A (bet365 opening odds),
    ... (more books ...)
    AvgH, AvgD, AvgA (average opening odds across ALL books),
    AvgCH, AvgCD, AvgCA (average CLOSING odds),
    MaxH, MaxD, MaxA (best available odds),
    BbAv>2.5, BbAv<2.5 (avg over/under 2.5 lines), ...

The CSV columns evolved over the years; the parser here handles both the
modern (Bb* → Avg* rename in 2019/20) and legacy formats.

League slug format:  <league>-<season>.csv → e.g. "E0-2425.csv"
where E0=EPL, E1=Championship, E2=L1, E3=L2, EC=Conference, SP1=La Liga,
D1=Bundesliga, D2=2.Bundesliga, I1=Serie A, I2=Serie B, F1=Ligue 1,
F2=Ligue 2, N1=Eredivisie, P1=Primeira, SC0=SPL, SC1=SD1, B1=Belgian
Jupiler, T1=Turkish, G1=Greek, SWE=Swedish, NOR=Norwegian.

Season slug format: "2425" → 2024/25 season
"""
from __future__ import annotations

import csv
import io
import logging
from typing import Iterable, Optional

import httpx

from services.soccer.models import SoccerMatch

logger = logging.getLogger("lockscore.services.soccer.football_data_co_uk")

_BASE = "https://www.football-data.co.uk/mmz4281"
_HTTP_TIMEOUT = 30.0
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; LockScore/1.0)"}


# Slug map: canonical league code → football-data.co.uk file prefix.
# One league can span multiple CSV prefixes when they cover multiple
# tiers (extra CSVs for "extra leagues" file: BRA.csv, ARG.csv, etc.).
_MAIN_LEAGUE_SLUGS: dict[str, str] = {
    "EPL":          "E0",
    "ELC":          "E1",
    "EL1":          "E2",
    "EL2":          "E3",
    "ECONF":        "EC",
    "LaLiga":       "SP1",
    "LaLiga2":      "SP2",
    "Bundesliga":   "D1",
    "Bundesliga2":  "D2",
    "SerieA":       "I1",
    "SerieB":       "I2",
    "Ligue1":       "F1",
    "Ligue2":       "F2",
    "Eredivisie":   "N1",
    "Primeira":     "P1",
    "SPL":          "SC0",
    "SD1":          "SC1",
    "BEL":          "B1",
    "TUR":          "T1",
    "GRE":          "G1",
}

# Extra-leagues file lives in a separate location and has ALL small
# leagues in one big CSV (Brazil, Argentina, Sweden, Norway, MLS, etc.)
# Columns are a slightly-different schema.
_EXTRA_LEAGUES_URL = "https://www.football-data.co.uk/new"
_EXTRA_LEAGUES_FILES: dict[str, str] = {
    "Brasileirao":   "BRA.csv",
    "Argentina":     "ARG.csv",
    "Allsvenskan":   "SWE.csv",
    "Eliteserien":   "NOR.csv",
    "MLS":           "USA.csv",
    "LigaMX":        "MEX.csv",
    "CHN":           "CHN.csv",
    "DEN":           "DNK.csv",
    "SUI":           "SUI.csv",
    "AUT":           "AUT.csv",
    "POL":           "POL.csv",
    "IRL":           "IRL.csv",
    "FIN":           "FIN.csv",
    "ROU":           "ROU.csv",
    "JPN":           "JPN.csv",
    "RUS":           "RUS.csv",
}


def _season_to_slug(season: str) -> str:
    """'2024-25' → '2425'. Also accepts '2024/25' or the slug itself."""
    s = season.replace("/", "-").replace(" ", "")
    if "-" in s:
        parts = s.split("-", 1)
        y1 = parts[0][-2:].zfill(2)
        y2 = parts[1][-2:].zfill(2)
        return f"{y1}{y2}"
    if len(s) == 4 and s.isdigit():
        return s   # already a slug like '2425'
    return s


async def _fetch_csv(url: str) -> str:
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, headers=_HEADERS) as cx:
        r = await cx.get(url)
        r.raise_for_status()
        return r.text


def _to_int(s) -> Optional[int]:
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return None


def _to_float(s) -> Optional[float]:
    try:
        v = float(s)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def _parse_date(s: str) -> Optional[str]:
    # football-data.co.uk uses "16/08/24" for modern and "16/08/2024" for
    # legacy. Return ISO "2024-08-16".
    if not s:
        return None
    parts = s.split("/")
    if len(parts) != 3:
        return None
    d, m, y = parts
    if len(y) == 2:
        y = "20" + y   # 20YY window
    try:
        return f"{y}-{int(m):02d}-{int(d):02d}"
    except (ValueError, TypeError):
        return None


def _parse_row(row: dict, league: str, season: str) -> Optional[dict]:
    """Row → normalized SoccerMatch dict. Returns None on unparsable row."""
    date = _parse_date(row.get("Date", "") or row.get("date", ""))
    home = (row.get("HomeTeam") or row.get("Home") or row.get("HT") or "").strip()
    away = (row.get("AwayTeam") or row.get("Away") or row.get("AT") or "").strip()
    if not date or not home or not away:
        return None

    fthg = _to_int(row.get("FTHG") or row.get("HG"))
    ftag = _to_int(row.get("FTAG") or row.get("AG"))

    # Opening odds — try Avg* first (post-2019), fall back to Bb* (legacy)
    home_open = _to_float(row.get("AvgH") or row.get("BbAvH") or row.get("PSH") or row.get("B365H"))
    draw_open = _to_float(row.get("AvgD") or row.get("BbAvD") or row.get("PSD") or row.get("B365D"))
    away_open = _to_float(row.get("AvgA") or row.get("BbAvA") or row.get("PSA") or row.get("B365A"))
    # Closing odds — the "C" prefix denotes closing lines
    home_close = _to_float(row.get("AvgCH") or row.get("BbAvCH") or row.get("PSCH"))
    draw_close = _to_float(row.get("AvgCD") or row.get("BbAvCD") or row.get("PSCD"))
    away_close = _to_float(row.get("AvgCA") or row.get("BbAvCA") or row.get("PSCA"))

    return SoccerMatch(
        league=league,
        season=season,
        home_team=home,
        away_team=away,
        date=date,
        home_score=fthg,
        away_score=ftag,
        home_odds_open=home_open,
        draw_odds_open=draw_open,
        away_odds_open=away_open,
        home_odds_close=home_close,
        draw_odds_close=draw_close,
        away_odds_close=away_close,
        status="finished" if fthg is not None else "scheduled",
        source="football_data_co_uk",
    ).to_dict()


def _parse_extra_leagues_row(row: dict) -> Optional[dict]:
    """Extra-leagues CSV has slightly different columns (League, Home, Away, HG, AG)."""
    league_raw = (row.get("League") or "").strip()
    league = _EXTRA_LEAGUE_MAP.get(league_raw)
    if not league:
        return None
    date = _parse_date(row.get("Date", ""))
    home = (row.get("Home") or "").strip()
    away = (row.get("Away") or "").strip()
    season = (row.get("Season") or "").replace("/", "-")
    if not date or not home or not away:
        return None
    return SoccerMatch(
        league=league,
        season=season or "current",
        home_team=home,
        away_team=away,
        date=date,
        home_score=_to_int(row.get("HG")),
        away_score=_to_int(row.get("AG")),
        home_odds_open=_to_float(row.get("AvgH") or row.get("PH")),
        draw_odds_open=_to_float(row.get("AvgD") or row.get("PD")),
        away_odds_open=_to_float(row.get("AvgA") or row.get("PA")),
        home_odds_close=_to_float(row.get("AvgCH")),
        draw_odds_close=_to_float(row.get("AvgCD")),
        away_odds_close=_to_float(row.get("AvgCA")),
        status="finished" if _to_int(row.get("HG")) is not None else "scheduled",
        source="football_data_co_uk",
    ).to_dict()


# Mapping from the League column in extra-leagues CSVs to our canonical
# league code.
_EXTRA_LEAGUE_MAP: dict[str, str] = {
    "Brazil Serie A":     "Brasileirao",
    "Argentina Primera":  "Argentina",
    "Argentina Primera División":  "Argentina",
    "Sweden Allsvenskan": "Allsvenskan",
    "Norway Eliteserien": "Eliteserien",
    "USA MLS":            "MLS",
    "Mexico Liga MX":     "LigaMX",
    "Mexican Primera División": "LigaMX",
    "Mexican Liga MX":    "LigaMX",
    "China Super League": "CHN",
    "Denmark Superliga":  "DEN",
    "Switzerland Super League": "SUI",
    "Austria Bundesliga": "AUT",
    "Poland Ekstraklasa": "POL",
    "Ireland Premier":    "IRL",
    "Finland Veikkausliiga": "FIN",
    "Romania Liga I":     "ROU",
    "Japan J-League":     "JPN",
    "Russia Premier":     "RUS",
}


async def fetch_league_season(league: str, season: str) -> list[dict]:
    """Download + parse ONE league-season CSV. Returns list of match dicts.
    Falls back to empty list on 404 (season file not yet published).
    """
    slug = _MAIN_LEAGUE_SLUGS.get(league)
    if not slug:
        # Could be an extra-leagues league — those are handled by
        # `fetch_extra_leagues`
        return []
    season_slug = _season_to_slug(season)
    url = f"{_BASE}/{season_slug}/{slug}.csv"
    try:
        text = await _fetch_csv(url)
    except Exception as e:
        logger.debug("fetch %s failed: %s", url, e)
        return []
    reader = csv.DictReader(io.StringIO(text))
    out: list[dict] = []
    for row in reader:
        parsed = _parse_row(row, league, season)
        if parsed:
            out.append(parsed)
    logger.info("football-data.co.uk %s %s → %d matches", league, season, len(out))
    return out


async def fetch_extra_leagues() -> list[dict]:
    """Download the extra-leagues CSVs (Brazil, Argentina, Sweden,
    Norway, MLS, etc.) — one file per league, covers all seasons in a
    single download."""
    out: list[dict] = []
    for canonical, filename in _EXTRA_LEAGUES_FILES.items():
        url = f"{_EXTRA_LEAGUES_URL}/{filename}"
        try:
            text = await _fetch_csv(url)
        except Exception as e:
            logger.debug("fetch %s failed: %s", url, e)
            continue
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            parsed = _parse_extra_leagues_row(row)
            if parsed:
                out.append(parsed)
        logger.info("football-data.co.uk extra %s → running total %d matches",
                    canonical, len(out))
    return out


async def fetch_all_leagues(seasons: Iterable[str]) -> list[dict]:
    """Bulk fetch: all main leagues × supplied seasons + extra leagues.
    Handles ~40 leagues × N seasons in a single call. Returns
    deduplicated list."""
    seen: set[tuple] = set()
    out: list[dict] = []
    # Main leagues (per-season files)
    for league in _MAIN_LEAGUE_SLUGS:
        for season in seasons:
            matches = await fetch_league_season(league, season)
            for m in matches:
                key = (m["league"], m["season"], m["home_team"],
                       m["away_team"], m["date"])
                if key in seen:
                    continue
                seen.add(key)
                out.append(m)
    # Extra leagues (single file each, all seasons)
    extras = await fetch_extra_leagues()
    for m in extras:
        key = (m["league"], m["season"], m["home_team"],
               m["away_team"], m["date"])
        if key in seen:
            continue
        seen.add(key)
        out.append(m)
    return out


__all__ = [
    "fetch_league_season",
    "fetch_extra_leagues",
    "fetch_all_leagues",
    "_MAIN_LEAGUE_SLUGS",
    "_EXTRA_LEAGUES_FILES",
]
