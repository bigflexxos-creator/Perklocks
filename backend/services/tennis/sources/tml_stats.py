"""TennisMyLife stats mirror — ATP Challenger + Qualifying (Phase 3b).

`stats.tennismylife.org/data/` is the extended TennisMyLife mirror that
hosts data NOT in the GitHub TML-Database repo. Specifically:

    stats.tennismylife.org/data/{year}_challenger.csv       ATP Challenger Tour
    stats.tennismylife.org/data/atp_quali/{year}_atp_quali.csv  ATP Qualifying

Schema is Sackmann-compatible (same columns as our main-tour ingester in
`services/tennis/sources/tml_database.py`), with additional `winner_seed`,
`winner_entry`, `loser_seed`, `loser_entry` columns that we simply ignore.

Why this matters:
    Sportsbooks price ATP Challenger events all year — a slate of picks
    like "Elias Ymer Moneyline @ Barcelona Challenger" was previously
    invisible to our signal engine because we only ingested the ATP main
    tour. Adding Challenger + Qualifying matches roughly doubles the
    match volume in our history collection and unlocks meaningful
    surface-specific serve/return stats for the ~300 players who cycle
    between tour and challenger levels.
"""
from __future__ import annotations

import csv
import io
import logging
from typing import Iterable

import httpx

from services.tennis.sources.tml_database import _parse_row

logger = logging.getLogger("lockscore.services.tennis.tml_stats")

_BASE = "https://stats.tennismylife.org/data"
_HTTP_TIMEOUT = 30.0
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; LockScore/1.0)"}


async def _fetch_csv(url: str) -> str:
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, headers=_HEADERS) as cx:
        r = await cx.get(url)
        r.raise_for_status()
        return r.text


def _parse_text(text: str, level_tag: str) -> list[dict]:
    if text and text[0] == "\ufeff":
        text = text[1:]
    reader = csv.DictReader(io.StringIO(text))
    out: list[dict] = []
    for row in reader:
        parsed = _parse_row(row)
        if not parsed:
            continue
        # Tag the level explicitly (defensive — the source already stamps
        # `tourney_level=C` for Challenger and `Q` for qualifying, but we
        # add our own `circuit` field for easy downstream filtering).
        parsed["circuit"] = level_tag
        parsed["source"] = "tml_stats"
        out.append(parsed)
    return out


async def fetch_challenger_year(year: int) -> list[dict]:
    """One year of ATP Challenger Tour matches."""
    url = f"{_BASE}/{year}_challenger.csv"
    try:
        text = await _fetch_csv(url)
    except Exception as e:
        logger.debug("tml_stats challenger %d fetch failed: %s", year, e)
        return []
    matches = _parse_text(text, "challenger")
    logger.info("tml_stats challenger %d → %d matches", year, len(matches))
    return matches


async def fetch_atp_quali_year(year: int) -> list[dict]:
    """One year of ATP Tour qualifying-round matches."""
    url = f"{_BASE}/atp_quali/{year}_atp_quali.csv"
    try:
        text = await _fetch_csv(url)
    except Exception as e:
        logger.debug("tml_stats atp_quali %d fetch failed: %s", year, e)
        return []
    matches = _parse_text(text, "atp_quali")
    logger.info("tml_stats atp_quali %d → %d matches", year, len(matches))
    return matches


async def fetch_all_years(years: Iterable[int]) -> list[dict]:
    """Bulk fetch Challenger + Qualifying for the given years."""
    out: list[dict] = []
    for year in years:
        out.extend(await fetch_challenger_year(year))
        out.extend(await fetch_atp_quali_year(year))
    return out


__all__ = [
    "fetch_challenger_year",
    "fetch_atp_quali_year",
    "fetch_all_years",
]
