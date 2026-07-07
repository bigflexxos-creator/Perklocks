"""MLB Pitcher H2H — pitcher's historical performance vs the opposing team.

For a strikeout pick like "Gerrit Cole Over 3.5 K vs BOS", returns:
  • avg K over last 5 starts vs BOS (season + career)
  • last 5 starts vs BOS (date, K count, IP, result)
  • current-season avg K vs BOS
  • current-season avg K overall (for context)

Data pulled from free MLB Stats API. Cached for 12h to limit calls.
"""
from __future__ import annotations
import time
import urllib.parse
from typing import Optional, Any
import httpx

MLB_BASE = "https://statsapi.mlb.com/api/v1"

_CACHE: dict[str, tuple[float, Any]] = {}
_TTL = 12 * 3600  # 12h

# 30-team static abbreviation → full team name map. Used so we can reliably
# match the pitcher's team (from the market parens) against the two team
# strings in the event ("St. Louis Cardinals @ Kansas City Royals"). The
# previous substring check ("kc" in "kansas city royals") falsely failed.
MLB_ABBREV_TO_NAME: dict[str, str] = {
    "ARI": "Arizona Diamondbacks", "ATL": "Atlanta Braves",
    "BAL": "Baltimore Orioles", "BOS": "Boston Red Sox",
    "CHC": "Chicago Cubs", "CWS": "Chicago White Sox", "CHW": "Chicago White Sox",
    "CIN": "Cincinnati Reds", "CLE": "Cleveland Guardians",
    "COL": "Colorado Rockies", "DET": "Detroit Tigers",
    "HOU": "Houston Astros", "KC": "Kansas City Royals", "KCR": "Kansas City Royals",
    "LAA": "Los Angeles Angels", "LAD": "Los Angeles Dodgers",
    "MIA": "Miami Marlins", "MIL": "Milwaukee Brewers",
    "MIN": "Minnesota Twins", "NYM": "New York Mets", "NYY": "New York Yankees",
    "OAK": "Oakland Athletics", "ATH": "Athletics",
    "PHI": "Philadelphia Phillies", "PIT": "Pittsburgh Pirates",
    "SD": "San Diego Padres", "SDP": "San Diego Padres",
    "SEA": "Seattle Mariners", "SF": "San Francisco Giants", "SFG": "San Francisco Giants",
    "STL": "St. Louis Cardinals", "TB": "Tampa Bay Rays", "TBR": "Tampa Bay Rays",
    "TEX": "Texas Rangers", "TOR": "Toronto Blue Jays",
    "WSH": "Washington Nationals", "WAS": "Washington Nationals",
}


def resolve_opp_team_name(event: str, pitcher_abbrev: str) -> Optional[str]:
    """Given event like "St. Louis Cardinals @ Kansas City Royals" and pitcher
    abbrev "KC", return the OPPOSING team's full name ("St. Louis Cardinals").
    Returns None if it can't decide unambiguously."""
    import re as _re
    parts = _re.split(r"\s+(?:@|vs)\s+", event)
    if len(parts) != 2:
        return None
    pteam_name = MLB_ABBREV_TO_NAME.get(pitcher_abbrev.upper(), "")
    pteam_l = pteam_name.lower()
    a, b = parts[0].strip(), parts[1].strip()
    a_l, b_l = a.lower(), b.lower()
    if pteam_l and (pteam_l in a_l or a_l in pteam_l):
        return b
    if pteam_l and (pteam_l in b_l or b_l in pteam_l):
        return a
    # Fallback: city-token match (last word of pteam vs each part)
    if pteam_name:
        city_tokens = [t for t in pteam_name.split() if len(t) > 2]
        for tok in city_tokens:
            tl = tok.lower()
            if tl in a_l:
                return b
            if tl in b_l:
                return a
    return None


async def _get(url: str) -> Optional[dict]:
    now = time.time()
    if url in _CACHE:
        ts, val = _CACHE[url]
        if now - ts < _TTL:
            return val
    try:
        async with httpx.AsyncClient(timeout=8.0) as cli:
            r = await cli.get(url)
            if r.status_code != 200:
                return None
            data = r.json()
            _CACHE[url] = (now, data)
            return data
    except Exception:
        return None


async def _resolve_pitcher_id(name: str) -> Optional[int]:
    q = urllib.parse.quote(name)
    data = await _get(f"{MLB_BASE}/people/search?names={q}")
    if not data:
        return None
    people = data.get("people") or []
    if not people:
        return None
    return people[0].get("id")


async def _resolve_team_id(team_name: str) -> Optional[int]:
    """Match team name fragment against current 30 MLB teams."""
    data = await _get(f"{MLB_BASE}/teams?sportId=1")
    if not data:
        return None
    needle = team_name.lower().strip()
    best = None
    for t in data.get("teams") or []:
        full = (t.get("name") or "").lower()
        code = (t.get("abbreviation") or "").lower()
        team_code = (t.get("teamCode") or "").lower()
        if needle == code or needle == team_code:
            return t.get("id")
        if needle in full or full in needle:
            best = t.get("id")
    return best


async def fetch_pitcher_h2h(pitcher_name: str, opp_team_name: str) -> dict:
    """Return aggregate stats for a pitcher vs a specific team.

    Returns shape:
      { pitcher, opp_team, season_avg_k, season_starts, vs_team_starts,
        vs_team_avg_k, vs_team_recent: [{date, opp, k, ip, result}], ok: bool }
    """
    out: dict[str, Any] = {"pitcher": pitcher_name, "opp_team": opp_team_name, "ok": False}
    pid = await _resolve_pitcher_id(pitcher_name)
    if not pid:
        out["error"] = "pitcher_not_found"
        return out
    tid = await _resolve_team_id(opp_team_name)

    # 2026 season gameLog (currently in season)
    season = 2026
    data = await _get(
        f"{MLB_BASE}/people/{pid}/stats?stats=gameLog&group=pitching&season={season}"
    )
    if not data:
        out["error"] = "no_game_log"
        return out

    splits = (
        (((data.get("stats") or [{}])[0]).get("splits")) or []
    )

    total_k = 0
    total_starts = 0
    vs_team_k = 0
    vs_team_starts = 0
    recent_vs_team = []
    all_starts: list[dict] = []   # chronological list of ALL starts this season
    for sp in splits:
        st = sp.get("stat") or {}
        op = (sp.get("opponent") or {}).get("name") or ""
        op_id = (sp.get("opponent") or {}).get("id")
        k = int(st.get("strikeOuts") or 0)
        ip = st.get("inningsPitched") or "0.0"
        date = sp.get("date") or ""
        # Filter only starts (games where IP >= 4.0 — proxies as starts)
        ip_float = float(str(ip).replace(".1", ".33").replace(".2", ".67"))
        is_start = ip_float >= 4.0
        if is_start:
            total_starts += 1
            total_k += k
            all_starts.append({
                "date": date, "opp": op, "k": k, "ip": ip_float,
                "er": int(st.get("earnedRuns") or 0),
                "h": int(st.get("hits") or 0),
                "bb": int(st.get("baseOnBalls") or 0),
            })
        # Match vs opp team
        if tid and op_id == tid:
            vs_team_starts += 1
            vs_team_k += k
            recent_vs_team.append({
                "date": date,
                "opp": op,
                "k": k,
                "ip": str(ip),
            })

    # ─── L5/L10/L20 rolling windows (user spec 2026-07-03) ────
    # Sort starts by date ascending, then slice last N. Provide
    # K/IP averages for each window so the pick card can show
    # a hot/cold arc across the pitcher's rolling form.
    all_starts.sort(key=lambda s: s.get("date") or "")

    def _win(n: int) -> dict:
        w = all_starts[-n:]
        if not w:
            return {"starts": 0}
        gk = sum(s["k"] for s in w)
        gip = sum(s["ip"] for s in w)
        ger = sum(s["er"] for s in w)
        gh = sum(s.get("h", 0) for s in w)
        gbb = sum(s.get("bb", 0) for s in w)
        return {
            "starts": len(w),
            # Strikeout metrics
            "avg_k": round(gk / len(w), 2),
            "total_k": gk,
            # Innings & derived outs (1 IP = 3 outs; ip_float is decimal)
            "avg_ip": round(gip / len(w), 2),
            "total_ip": round(gip, 2),
            "avg_outs": round((gip / len(w)) * 3, 2),
            "total_outs": round(gip * 3, 0),
            # Earned runs
            "avg_er": round(ger / len(w), 2),
            "total_er": ger,
            "era": round(9.0 * ger / gip, 2) if gip > 0 else None,
            # Hits allowed
            "avg_h": round(gh / len(w), 2),
            "total_h": gh,
            # Walks allowed
            "avg_bb": round(gbb / len(w), 2),
            "total_bb": gbb,
        }
    l5, l10, l20 = _win(5), _win(10), _win(20)

    out["ok"] = True
    out["season_starts"] = total_starts
    out["season_avg_k"] = round(total_k / total_starts, 2) if total_starts else 0
    # Season-wide non-K aggregates for outs/walks/earned-run/hits props
    _tot_ip = sum(s["ip"] for s in all_starts)
    _tot_er = sum(s["er"] for s in all_starts)
    _tot_h = sum(s.get("h", 0) for s in all_starts)
    _tot_bb = sum(s.get("bb", 0) for s in all_starts)
    out["season_avg_ip"] = round(_tot_ip / total_starts, 2) if total_starts else 0
    out["season_avg_outs"] = round((_tot_ip / total_starts) * 3, 2) if total_starts else 0
    out["season_avg_er"] = round(_tot_er / total_starts, 2) if total_starts else 0
    out["season_avg_h"] = round(_tot_h / total_starts, 2) if total_starts else 0
    out["season_avg_bb"] = round(_tot_bb / total_starts, 2) if total_starts else 0
    out["season_era"] = round(9.0 * _tot_er / _tot_ip, 2) if _tot_ip > 0 else None
    out["vs_team_starts"] = vs_team_starts
    out["vs_team_avg_k"] = round(vs_team_k / vs_team_starts, 2) if vs_team_starts else 0
    out["vs_team_recent"] = sorted(recent_vs_team, key=lambda x: x["date"], reverse=True)[:5]
    # Per-start details for the vs-team log (needed for outs/walks context)
    _vs_starts_full = [s for s in all_starts if s.get("opp") == opp_team_name] if opp_team_name else []
    if _vs_starts_full:
        out["vs_team_avg_ip"] = round(sum(s["ip"] for s in _vs_starts_full) / len(_vs_starts_full), 2)
        out["vs_team_avg_outs"] = round(out["vs_team_avg_ip"] * 3, 2)
        out["vs_team_avg_er"] = round(sum(s["er"] for s in _vs_starts_full) / len(_vs_starts_full), 2)
        out["vs_team_avg_bb"] = round(sum(s.get("bb", 0) for s in _vs_starts_full) / len(_vs_starts_full), 2)
        out["vs_team_avg_h"] = round(sum(s.get("h", 0) for s in _vs_starts_full) / len(_vs_starts_full), 2)
    out["last5"] = l5
    out["last10"] = l10
    out["last20"] = l20
    return out
