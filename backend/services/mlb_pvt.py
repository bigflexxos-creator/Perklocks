"""MLB Pitcher-vs-Team (PvT) enrichment.

USER MANDATE (2026-07-27): "We got Wheeler Under 6.5 K but he does GREAT
against the Marlins. Are we doing the math right?"

Answer: no — the K math engine looked at season K/9 + opponent team K%
but ignored HOW THIS PITCHER performs against THIS SPECIFIC TEAM
historically. Wheeler is 14-4, 2.59 ERA, 198 K in 28 GS vs MIA
(7.07 K/start). His most recent starts vs MIA were 8 K and 9 K.

This module pulls career + recent PvT splits from the FREE MLB Stats
API and returns:
    {
      "gs_vs_team":         28,          # career games vs team
      "k_vs_team":          198,         # career K's vs team
      "k_per_gs_vs_team":   7.07,        # avg K per start vs team
      "opp_avg_vs_team":    0.212,       # team's AVG against pitcher
      "recent_k_vs_team":   [8, 9],      # K's in last 2-3 starts vs team
      "recent_avg_k":       8.5,         # avg K last 2-3 vs team
      "significance":       "high",      # ≥5 GS = high, ≥3 = med, <3 = none
    }

Feeds into services.mlb_k_probability.compute_expected_k as a
multiplier that can shift λ (expected K) up or down 15-25%.

Cached per (pitcher_id, opposing_team_id) for the entire day.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

logger = logging.getLogger("lockscore.mlb_pvt")

MLB_STATS_BASE = "https://statsapi.mlb.com/api/v1"

# ── Caches ──────────────────────────────────────────────────────────
_PVT_CACHE: dict[tuple, Optional[dict]] = {}   # (pitcher_id, opp_team_id) → dict
_TEAM_ID_BY_NAME: dict[str, int] = {}          # "Miami Marlins" → 146

# Curated MLB team-id lookup table (avoids the 2 s cold-start /teams call
# on the first hot pick). Keys match the display names the picks board
# ships. Missing teams fall back to a lazy /teams fetch.
_TEAM_ID_STATIC = {
    "Arizona Diamondbacks": 109, "Atlanta Braves": 144,
    "Baltimore Orioles": 110, "Boston Red Sox": 111,
    "Chicago White Sox": 145, "Chicago Cubs": 112,
    "Cincinnati Reds": 113, "Cleveland Guardians": 114,
    "Colorado Rockies": 115, "Detroit Tigers": 116,
    "Houston Astros": 117, "Kansas City Royals": 118,
    "Los Angeles Angels": 108, "Los Angeles Dodgers": 119,
    "Miami Marlins": 146, "Milwaukee Brewers": 158,
    "Minnesota Twins": 142, "New York Yankees": 147,
    "New York Mets": 121, "Athletics": 133,
    "Oakland Athletics": 133,
    "Philadelphia Phillies": 143, "Pittsburgh Pirates": 134,
    "San Diego Padres": 135, "San Francisco Giants": 137,
    "Seattle Mariners": 136, "St. Louis Cardinals": 138,
    "Tampa Bay Rays": 139, "Texas Rangers": 140,
    "Toronto Blue Jays": 141, "Washington Nationals": 120,
}


async def _get_json(url: str, timeout: float = 8.0) -> Optional[dict]:
    try:
        async with httpx.AsyncClient(timeout=timeout) as cx:
            r = await cx.get(url)
            if r.status_code != 200:
                return None
            return r.json()
    except Exception as e:
        logger.debug("MLB Stats PvT GET failed (%s): %s", url, e)
        return None


async def lookup_team_id(team_name: str) -> Optional[int]:
    """Resolve a display team name → MLB Stats API id."""
    if not team_name:
        return None
    norm = team_name.strip()
    if norm in _TEAM_ID_STATIC:
        return _TEAM_ID_STATIC[norm]
    if norm in _TEAM_ID_BY_NAME:
        return _TEAM_ID_BY_NAME[norm]
    # Slow-path fallback
    data = await _get_json(f"{MLB_STATS_BASE}/teams?sportId=1")
    if data and data.get("teams"):
        for t in data["teams"]:
            _TEAM_ID_BY_NAME[t.get("name", "")] = t.get("id")
            _TEAM_ID_BY_NAME[t.get("teamName", "")] = t.get("id")
    return _TEAM_ID_BY_NAME.get(norm)


async def fetch_pvt(pitcher_id: int, opp_team_id: int) -> Optional[dict]:
    """Return career + recent PvT splits for pitcher vs team.

    Uses TWO API calls:
      - `vsTeamTotal` for the career aggregate row (single record with
        gamesPlayed=N, strikeOuts=Total, avg=season-avg).
      - `vsTeam` for individual game logs (list, chronological). Last 3
        entries = most recent starts.
    """
    key = (int(pitcher_id), int(opp_team_id))
    cached = _PVT_CACHE.get(key)
    if cached is not None or key in _PVT_CACHE:
        return cached

    # ── Career aggregate ──
    total_data = await _get_json(
        f"{MLB_STATS_BASE}/people/{pitcher_id}/stats"
        f"?stats=vsTeamTotal&opposingTeamId={opp_team_id}&group=pitching",
    )
    career = None
    if total_data and total_data.get("stats"):
        for sg in total_data["stats"]:
            for sp in (sg.get("splits") or []):
                st = sp.get("stat") or {}
                if int(st.get("gamesPlayed") or 0) >= 1:
                    career = st
                    break
            if career:
                break

    if not career:
        _PVT_CACHE[key] = None
        return None

    gs = int(career.get("gamesPlayed") or 0)
    k = int(career.get("strikeOuts") or 0)
    if gs < 1:
        _PVT_CACHE[key] = None
        return None

    k_per_gs = k / gs if gs > 0 else 0.0
    opp_avg: Optional[float] = None
    try:
        opp_avg = float(career.get("avg") or 0.0)
    except (TypeError, ValueError):
        pass

    # ── Recent game log (last 3 starts vs team) ──
    # Use `stats=gameLog` filtered by opposing team — gives real per-game
    # results (K's, IP, ER). We need last 2-3 seasons to catch recent
    # form even when the team plays this pitcher just 1-2 times/year.
    from datetime import datetime
    current_year = datetime.now().year
    game_splits: list[dict] = []
    for season in (current_year, current_year - 1, current_year - 2):
        if len(game_splits) >= 3:
            break
        log_data = await _get_json(
            f"{MLB_STATS_BASE}/people/{pitcher_id}/stats"
            f"?stats=gameLog&group=pitching&season={season}",
        )
        if not log_data or not log_data.get("stats"):
            continue
        for sg in log_data["stats"]:
            for sp in (sg.get("splits") or []):
                opp = (sp.get("opponent") or {}).get("id")
                if opp != opp_team_id:
                    continue
                st = sp.get("stat") or {}
                game_splits.append({
                    "date":         sp.get("date"),
                    "strikeOuts":   int(st.get("strikeOuts") or 0),
                    "inningsPitched": st.get("inningsPitched"),
                })
    # Sort by date descending (most recent first) and keep last 3
    def _key(g):
        return g.get("date") or ""
    game_splits.sort(key=_key, reverse=True)
    recent = game_splits[:3]
    recent_k = [g["strikeOuts"] for g in recent]
    recent_avg_k = (sum(recent_k) / len(recent_k)) if recent_k else k_per_gs

    if gs >= 5:
        sig = "high"
    elif gs >= 3:
        sig = "medium"
    else:
        sig = "low"

    out = {
        "gs_vs_team":        gs,
        "k_vs_team":         k,
        "k_per_gs_vs_team":  round(k_per_gs, 2),
        "opp_avg_vs_team":   round(opp_avg, 3) if opp_avg is not None else None,
        "recent_k_vs_team":  recent_k,
        "recent_avg_k":      round(recent_avg_k, 2),
        "significance":      sig,
    }
    _PVT_CACHE[key] = out
    return out


async def get_pvt_for_pitcher_vs_team(
    pitcher_id: int,
    opp_team_name: str,
) -> Optional[dict]:
    """Convenience: name → id → PvT split."""
    tid = await lookup_team_id(opp_team_name)
    if not tid:
        return None
    return await fetch_pvt(pitcher_id, tid)


def compute_pvt_k_multiplier(pvt: dict, league_avg_k_per_gs: float = 5.5) -> float:
    """Multiplier ∈ [0.75, 1.35] applied to expected K's.

    Uses a WEIGHTED signal:
      • 50% recent (last 2-3 starts vs team)
      • 30% career average vs team
      • 20% ballast on league-avg (so we don't over-fit small samples)

    Only trust the signal when significance is medium/high.
    """
    if not pvt:
        return 1.0

    sig = pvt.get("significance") or "low"
    if sig == "low":
        return 1.0  # <3 GS not statistically meaningful

    career_k_per_gs = float(pvt.get("k_per_gs_vs_team") or league_avg_k_per_gs)
    recent = pvt.get("recent_k_vs_team") or []
    recent_avg_k = float(pvt.get("recent_avg_k") or career_k_per_gs)

    if recent:
        blended = (0.5 * recent_avg_k
                   + 0.3 * career_k_per_gs
                   + 0.2 * league_avg_k_per_gs)
    else:
        blended = (0.6 * career_k_per_gs
                   + 0.4 * league_avg_k_per_gs)

    mult = blended / league_avg_k_per_gs
    # Cap the multiplier to avoid outliers dominating
    return max(0.75, min(1.35, mult))


__all__ = [
    "lookup_team_id",
    "fetch_pvt",
    "get_pvt_for_pitcher_vs_team",
    "compute_pvt_k_multiplier",
]
