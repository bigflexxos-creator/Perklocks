"""TML-Database source — Sackmann-schema CSVs on GitHub."""
from __future__ import annotations

import csv
import io
import logging
from typing import Optional

import httpx

logger = logging.getLogger("lockscore.services.tennis.tml")

_BASE = "https://raw.githubusercontent.com/Tennismylife/TML-Database/master"
_HTTP_TIMEOUT = 30.0
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; LockScore/1.0)"}


def _int(v) -> Optional[int]:
    if v in (None, "", "NA"):
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _f(v) -> Optional[float]:
    if v in (None, "", "NA"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_row(row: dict) -> Optional[dict]:
    winner = (row.get("winner_name") or "").strip()
    loser = (row.get("loser_name") or "").strip()
    tourney_date = (row.get("tourney_date") or "").strip()
    if not winner or not loser or not tourney_date or len(tourney_date) < 8:
        return None
    # tourney_date is YYYYMMDD
    date_iso = f"{tourney_date[:4]}-{tourney_date[4:6]}-{tourney_date[6:8]}"
    return {
        "date":            date_iso,
        "tourney_id":      row.get("tourney_id"),
        "tourney_name":    row.get("tourney_name"),
        "surface":         (row.get("surface") or "").strip(),
        "draw_size":       _int(row.get("draw_size")),
        "tourney_level":   row.get("tourney_level"),
        "indoor":          (row.get("indoor") or "").strip().lower() == "true"
                            or row.get("indoor") == "1",
        "round":           row.get("round"),
        "best_of":         _int(row.get("best_of")),
        "minutes":         _int(row.get("minutes")),
        "score":           row.get("score"),
        # Winner
        "winner_name":     winner,
        "winner_id":       row.get("winner_id"),
        "winner_hand":     row.get("winner_hand"),
        "winner_age":      _f(row.get("winner_age")),
        "winner_rank":     _int(row.get("winner_rank")),
        "w_ace":           _int(row.get("w_ace")),
        "w_df":            _int(row.get("w_df")),
        "w_svpt":          _int(row.get("w_svpt")),
        "w_1stIn":         _int(row.get("w_1stIn")),
        "w_1stWon":        _int(row.get("w_1stWon")),
        "w_2ndWon":        _int(row.get("w_2ndWon")),
        "w_SvGms":         _int(row.get("w_SvGms")),
        "w_bpSaved":       _int(row.get("w_bpSaved")),
        "w_bpFaced":       _int(row.get("w_bpFaced")),
        # Loser
        "loser_name":      loser,
        "loser_id":        row.get("loser_id"),
        "loser_hand":      row.get("loser_hand"),
        "loser_age":       _f(row.get("loser_age")),
        "loser_rank":      _int(row.get("loser_rank")),
        "l_ace":           _int(row.get("l_ace")),
        "l_df":            _int(row.get("l_df")),
        "l_svpt":          _int(row.get("l_svpt")),
        "l_1stIn":         _int(row.get("l_1stIn")),
        "l_1stWon":        _int(row.get("l_1stWon")),
        "l_2ndWon":        _int(row.get("l_2ndWon")),
        "l_SvGms":         _int(row.get("l_SvGms")),
        "l_bpSaved":       _int(row.get("l_bpSaved")),
        "l_bpFaced":       _int(row.get("l_bpFaced")),
        # Retirement / walkover flags — the score column carries them
        "retirement":      "RET" in (row.get("score") or "").upper(),
        "walkover":        "W/O" in (row.get("score") or "").upper()
                            or "WO" in (row.get("score") or "").upper(),
        "source":          "tml_database",
    }


async def fetch_year(year: int) -> list[dict]:
    """Download + parse one year of matches. Returns list of match dicts."""
    url = f"{_BASE}/{year}.csv"
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, headers=_HEADERS) as cx:
            r = await cx.get(url)
            r.raise_for_status()
            text = r.text
    except Exception as e:
        logger.debug("TML %d fetch failed: %s", year, e)
        return []
    if text and text[0] == "\ufeff":
        text = text[1:]
    reader = csv.DictReader(io.StringIO(text))
    out: list[dict] = []
    for row in reader:
        parsed = _parse_row(row)
        if parsed:
            out.append(parsed)
    logger.info("TML %d → %d matches parsed", year, len(out))
    return out


__all__ = ["fetch_year"]
