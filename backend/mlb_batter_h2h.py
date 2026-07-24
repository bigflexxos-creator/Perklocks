"""MLB Batter H2H — hitter's historical performance vs the opposing team.

For a batter pick like "Miguel Vargas Over 0.5 Hits vs KC", returns:
  • CAREER at-bats + hits + HR + RBI vs KC (from MLB Stats API vsTeam split)
  • Career games vs the opponent
  • Career batting_avg vs KC
  • Recent per-game log (this season's games vs KC with AB / H / HR / RBI)

Contract:
  fetch_batter_h2h(batter_name, opp_team_name) -> {
      ok: bool,
      batter: str, opp_team: str,
      season_ab: int, season_hits: int, season_avg: float, season_games: int,
      vs_team_ab: int, vs_team_hits: int, vs_team_hr: int, vs_team_rbi: int,
      vs_team_games: int, vs_team_avg: float,
      vs_team_recent: [{date, opp, ab, h, hr, rbi, stat}],
  }

2026-02 fix (user report: "0% against Mets not true — Ohtani has .282, 20 hits
in 19 career games vs Mets"): switched primary source from single-season
gameLog to MLB Stats API `stats=vsTeam` career splits. GameLog kept only for
the per-game recent list.
"""
from __future__ import annotations
import urllib.parse
from typing import Optional, Any

MLB_BASE = "https://statsapi.mlb.com/api/v1"

# Re-use the abbreviation → full team map from the pitcher module so the
# batter H2H sees the exact same team-name normalisation.
from mlb_pitcher_h2h import (  # noqa: E402
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
    """Return CAREER + season vs-team hitting splits for a batter.

    Career totals come from MLB Stats API `stats=vsTeam` split — the same
    endpoint StatMuse and Baseball Savant use for "player vs team" queries.
    Per-game recent list still comes from the current-season gameLog so we
    can render a per-game breakdown (AB, H, HR, RBI).
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
    if not tid:
        out["error"] = "opp_team_not_found"
        return out

    season = 2026

    # ── PRIMARY: career vs-team totals via `stats=vsTeam` ─────────────
    # Endpoint: /people/{pid}/stats?stats=vsTeam&group=hitting&opposingTeamId={tid}
    # Career totals (across all MLB seasons) — the correct source of
    # truth for "X-for-Y career vs OPP" claims.
    vs_ab = 0
    vs_h = 0
    vs_hr = 0
    vs_rbi = 0
    vs_games = 0
    vs_avg = 0.0
    vs_url = (f"{MLB_BASE}/people/{bid}/stats"
              f"?stats=vsTeam&group=hitting&opposingTeamId={tid}&sportId=1")
    vs_data = await _pitcher_get(vs_url)
    if vs_data:
        # Response shape: stats[0].splits[0].stat.{atBats, hits, homeRuns, rbi, avg, gamesPlayed}
        splits_list = ((vs_data.get("stats") or [{}])[0]).get("splits") or []
        for sp in splits_list:
            st = sp.get("stat") or {}
            vs_ab += int(st.get("atBats") or 0)
            vs_h += int(st.get("hits") or 0)
            vs_hr += int(st.get("homeRuns") or 0)
            vs_rbi += int(st.get("rbi") or 0)
            vs_games += int(st.get("gamesPlayed") or 0)
        if vs_ab:
            vs_avg = round(vs_h / vs_ab, 3)

    # ── SEASON + recent gameLog for the per-game breakdown ─────────
    season_ab = 0
    season_h = 0
    season_games = 0
    recent_vs: list[dict] = []
    log_url = (f"{MLB_BASE}/people/{bid}/stats"
               f"?stats=gameLog&group=hitting&season={season}")
    log_data = await _pitcher_get(log_url)
    if log_data:
        log_splits = ((log_data.get("stats") or [{}])[0]).get("splits") or []
        for sp in log_splits:
            st = sp.get("stat") or {}
            op_id = (sp.get("opponent") or {}).get("id")
            op_name = (sp.get("opponent") or {}).get("name") or ""
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

            # Per-game log vs the opponent (for the recent list only —
            # career totals above are the source of truth for the H2H).
            if op_id == tid:
                recent_vs.append({
                    "date": date,
                    "opp": op_name,
                    "ab": ab,
                    "h": h,
                    "hr": hr,
                    "rbi": rbi,
                    # Pre-formatted display string like "1-4" so the
                    # frontend doesn't have to string-build.
                    "stat": f"{h}-{ab}",
                })

    # Belt-and-suspenders: if vsTeam returned nothing (very rare — API
    # can miss for players with < 5 career PA vs the opponent), fall
    # back to the season-only gameLog counts.
    if vs_ab == 0 and any(r["ab"] > 0 for r in recent_vs):
        vs_ab = sum(r["ab"] for r in recent_vs)
        vs_h = sum(r["h"] for r in recent_vs)
        vs_hr = sum(r["hr"] for r in recent_vs)
        vs_rbi = sum(r["rbi"] for r in recent_vs)
        vs_games = len(recent_vs)
        vs_avg = round(vs_h / vs_ab, 3) if vs_ab else 0.0

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
    out["vs_team_avg"] = vs_avg
    out["vs_team_recent"] = sorted(
        recent_vs, key=lambda x: x["date"], reverse=True,
    )[:5]
    return out


__all__ = ["fetch_batter_h2h", "resolve_opp_team_name"]
