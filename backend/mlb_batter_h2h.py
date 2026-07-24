"""MLB Batter H2H — hitter's historical performance vs the opposing team.

For a batter pick like "Miguel Vargas Over 0.5 Hits vs KC", returns:
  • at_bats vs KC (season + career-if-available)
  • hits vs KC
  • batting_avg vs KC
  • recent games vs KC (date, AB, H, HR, RBI)

Data pulled from the free MLB Stats API. 12h cache keyed by URL.

Contract with `services.h2h_enricher`:
  fetch_batter_h2h(batter_name, opp_team_name) -> {
      ok: bool,
      batter: str, opp_team: str,
      season_ab: int, season_hits: int, season_avg: float, season_games: int,
      vs_team_ab: int, vs_team_hits: int, vs_team_hr: int, vs_team_rbi: int,
      vs_team_games: int, vs_team_avg: float,
      vs_team_recent: [{date, opp, ab, h, hr, rbi}],
  }
"""
from __future__ import annotations
import time
import urllib.parse
from typing import Optional, Any
import httpx

MLB_BASE = "https://statsapi.mlb.com/api/v1"

_CACHE: dict[str, tuple[float, Any]] = {}
_TTL = 12 * 3600  # 12h

# Re-use the abbreviation → full team map from the pitcher module so the
# batter H2H sees the exact same team-name normalisation.
from mlb_pitcher_h2h import (  # noqa: E402
    MLB_ABBREV_TO_NAME,
    resolve_opp_team_name,
    _get as _pitcher_get,
    _resolve_team_id as _pitcher_resolve_team_id,
)


async def _resolve_batter_id(name: str) -> Optional[int]:
    q = urllib.parse.quote(name)
    data = await _pitcher_get(f"{MLB_BASE}/people/search?names={q}")
    if not data:
        return None
    people = data.get("people") or []
    if not people:
        return None
    return people[0].get("id")


async def fetch_batter_h2h(batter_name: str, opp_team_name: str) -> dict:
    """Return aggregate hitting stats for a batter vs a specific team.

    Uses the batter's season gameLog and filters to games where the opponent
    matches `opp_team_name`. Season totals are computed alongside for context.
    """
    out: dict[str, Any] = {
        "batter": batter_name,
        "opp_team": opp_team_name,
        "ok": False,
    }
    bid = await _resolve_batter_id(batter_name)
    if not bid:
        out["error"] = "batter_not_found"
        return out
    tid = await _pitcher_resolve_team_id(opp_team_name)

    season = 2026
    data = await _pitcher_get(
        f"{MLB_BASE}/people/{bid}/stats"
        f"?stats=gameLog&group=hitting&season={season}"
    )
    if not data:
        out["error"] = "no_game_log"
        return out

    splits = (((data.get("stats") or [{}])[0]).get("splits")) or []

    season_ab = 0
    season_h = 0
    season_games = 0
    vs_ab = 0
    vs_h = 0
    vs_hr = 0
    vs_rbi = 0
    vs_games = 0
    recent_vs: list[dict] = []
    for sp in splits:
        st = sp.get("stat") or {}
        op = (sp.get("opponent") or {}).get("name") or ""
        op_id = (sp.get("opponent") or {}).get("id")
        ab = int(st.get("atBats") or 0)
        h = int(st.get("hits") or 0)
        hr = int(st.get("homeRuns") or 0)
        rbi = int(st.get("rbi") or 0)
        date = sp.get("date") or ""

        # Season totals — count every game with a plate appearance.
        if ab > 0 or int(st.get("plateAppearances") or 0) > 0:
            season_games += 1
        season_ab += ab
        season_h += h

        # vs-team split.
        if tid and op_id == tid:
            vs_games += 1
            vs_ab += ab
            vs_h += h
            vs_hr += hr
            vs_rbi += rbi
            recent_vs.append({
                "date": date,
                "opp": op,
                "ab": ab,
                "h": h,
                "hr": hr,
                "rbi": rbi,
            })

    out["ok"] = True
    out["season_games"] = season_games
    out["season_ab"] = season_ab
    out["season_hits"] = season_h
    out["season_avg"] = round(season_h / season_ab, 3) if season_ab else 0.0
    out["vs_team_games"] = vs_games
    out["vs_team_ab"] = vs_ab
    out["vs_team_hits"] = vs_h
    out["vs_team_hr"] = vs_hr
    out["vs_team_rbi"] = vs_rbi
    out["vs_team_avg"] = round(vs_h / vs_ab, 3) if vs_ab else 0.0
    out["vs_team_recent"] = sorted(
        recent_vs, key=lambda x: x["date"], reverse=True,
    )[:5]
    return out


__all__ = ["fetch_batter_h2h", "resolve_opp_team_name"]
