"""Sport-specific rationale builders — replaces the generic
"💯 Model gives X% win prob — high confidence" line with REAL stats
that explain WHY this particular pick made the board.

User mandate (2026-06-28):
   "None of the why this pick should be generic. MLB pitchers, spread,
    etc across all sports."

This module owns the dispatch table:

    sport+market signature  →  rationale builder

Each builder:
  • is ASYNC (DB lookups, occasional MLB Stats / ESPN calls)
  • returns a list of `evidence` strings + optional `concerns` strings
  • is BEST-EFFORT — it can return an empty list if data is sparse,
    in which case `pick_enrichment` keeps the upstream summary bullet
    and the universal model/edge framing as a last-resort fallback.

Build order picked to match today's slate volume:
  ✅ MLB pitcher props (K's, Outs Recorded, Walks, Earned Runs)
  ✅ MLB team-level (Moneyline, Run Line, Total Runs)
  ✅ Tennis Moneyline + Total Games
  ⏳ NFL spread / Moneyline / Total          (built when in-season picks land)
  ⏳ NBA player props (Pts / Reb / Ast)      (built when in-season picks land)
  ⏳ UFC fight Moneyline                     (built when bouts land)

Soccer goalscorer + MLB hit-prop + CFB picks already have their own
dedicated rationale builders (`csl_espn_live`, `mlb_hitter_intel`,
`cfb_rationale`) and skip this layer entirely.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

logger = logging.getLogger("lockscore.sport_rationale")


# ────────────────────────────────────────────────────────────────────
# MLB PITCHER PROPS — Strikeouts, Outs Recorded, Walks, Earned Runs
# ────────────────────────────────────────────────────────────────────
_MLB_PITCHER_MARKETS = (
    "strikeouts", "k's", "ks ", " k ", " ks",
    "outs recorded", "outs allowed",
    "walks allowed", " walks",
    "earned runs",
    "hits allowed",
    "pitching outs",
)


def is_mlb_pitcher_prop(market: str) -> bool:
    m = (market or "").lower()
    return any(needle in m for needle in _MLB_PITCHER_MARKETS)


async def build_mlb_pitcher_rationale(
    pick: dict, player_name: str,
) -> dict[str, list[str]]:
    """Returns {evidence, concerns} bullets for an MLB pitcher prop
    based on the pitcher's season form + recent-form vs opp team.

    Uses `mlb_pitcher_h2h.fetch_pitcher_h2h` which itself talks to MLB
    Stats API (free) — no Odds credits consumed."""
    out = {"evidence": [], "concerns": []}
    if not player_name:
        return out
    # Parse opponent from "Houston Astros @ Detroit Tigers" + pick.team
    event = (pick.get("event") or "")
    opp = ""
    if "@" in event:
        away, _, home = event.partition("@")
        away, home = away.strip(), home.strip()
        team = (pick.get("team") or "").strip()
        if team and team == home:
            opp = away
        elif team and team == away:
            opp = home
        else:
            opp = away  # best guess for pitcher props w/o team field
    try:
        from mlb_pitcher_h2h import fetch_pitcher_h2h
        data = await fetch_pitcher_h2h(player_name, opp)
    except Exception as e:
        logger.debug("MLB pitcher h2h failed for %s: %s", player_name, e)
        return out
    if not data.get("ok"):
        return out

    # Pull the prop line (e.g. "Over 6.5" → 6.5) from the market string
    market = pick.get("market") or ""
    line: Optional[float] = None
    m_line = re.search(r"\b(?:Over|Under)\s+(\d+(?:\.\d+)?)", market, re.I)
    if m_line:
        try:
            line = float(m_line.group(1))
        except Exception:
            line = None

    # Season form
    season_avg_k = data.get("season_avg_k")
    starts = data.get("season_starts") or 0
    if isinstance(season_avg_k, (int, float)) and starts >= 3:
        # "K's" market: compare avg to the line
        is_k_market = any(s in market.lower() for s in ("strikeout", "k's", " ks", " k "))
        if is_k_market and line is not None:
            delta = season_avg_k - line
            if delta >= 1.0:
                out["evidence"].append(
                    f"📈 {player_name.split()[-1]} averaging {season_avg_k:.1f} K's"
                    f" across {starts} starts (line {line:g}, +{delta:.1f} cushion)"
                )
            elif delta <= -1.0:
                out["concerns"].append(
                    f"📉 Season avg only {season_avg_k:.1f} K's / start —"
                    f" line of {line:g} requires above-trend performance"
                )
            else:
                out["evidence"].append(
                    f"⚾ Season avg {season_avg_k:.1f} K's / start"
                    f" — sitting right at the {line:g} line"
                )
        elif starts >= 3:
            out["evidence"].append(
                f"⚾ {starts} starts this season, averaging {season_avg_k:.1f} K"
            )

    # Recent vs opp team form
    vs_starts = data.get("vs_team_starts") or 0
    vs_avg_k = data.get("vs_team_avg_k")
    if vs_starts >= 2 and isinstance(vs_avg_k, (int, float)):
        if line is not None and vs_avg_k >= line:
            out["evidence"].append(
                f"🎯 {vs_avg_k:.1f} K avg vs {opp} in {vs_starts} prior starts"
            )
        elif line is not None and vs_avg_k < line - 1:
            out["concerns"].append(
                f"⚠️ Only {vs_avg_k:.1f} K avg vs {opp} in last {vs_starts} starts"
            )
        else:
            out["evidence"].append(
                f"📅 {vs_starts} prior starts vs {opp} — {vs_avg_k:.1f} K avg"
            )

    return out


# ────────────────────────────────────────────────────────────────────
# MLB TEAM-LEVEL — Moneyline, Run Line, Total Runs
# ────────────────────────────────────────────────────────────────────
_MLB_TEAM_MARKETS = ("moneyline", "run line", "spread", "total runs", " total ", "over/under")


def is_mlb_team_market(market: str, player_name: str) -> bool:
    """A market is "team-level" iff no player is attached and the market
    string smells like a side bet (ML / RL / total)."""
    if player_name:
        return False
    m = (market or "").lower()
    return any(needle in m for needle in _MLB_TEAM_MARKETS)


async def build_mlb_team_rationale(db, pick: dict) -> dict[str, list[str]]:
    """Best-effort MLB team rationale. Looks up the picked team's
    starting pitcher info via the matchup_resolver cache when
    available; otherwise produces a directional summary."""
    out = {"evidence": [], "concerns": []}
    event = (pick.get("event") or "")
    if "@" not in event:
        return out
    away, _, home = event.partition("@")
    away, home = away.strip(), home.strip()
    team = (pick.get("team") or "").strip()
    is_home = team == home
    # Try to surface the day's resolved pitcher (we already cache it
    # from the hit-prop pipeline). If the pitcher is on our team's
    # opponent, that's WHO this team is hitting against today.
    try:
        # Find a cached batter from THIS team and reveal opponent pitcher.
        anchor = await db.mlb_matchup_resolver_cache.find_one(
            {"data.batter_team": team},
            sort=[("ts", -1)],
        )
    except Exception:
        anchor = None
    if anchor and (anchor.get("data") or {}).get("pitcher_name"):
        opp_pitcher = anchor["data"]["pitcher_name"]
        out["evidence"].append(
            f"⚾ {team}: facing {opp_pitcher} today"
            f" — opportunity to hit a rated arm"
        )
    elif team:
        side = "home" if is_home else "road"
        out["evidence"].append(
            f"🏟 {team} playing as the {side} team"
            + (f" at {home}" if is_home else f" at {home}")
        )
    return out


# ────────────────────────────────────────────────────────────────────
# TENNIS — Moneyline, Total Games, Set Spread
# ────────────────────────────────────────────────────────────────────
def is_tennis_market(sport: str) -> bool:
    return (sport or "").lower() in ("tennis", "atp", "wta")


async def build_tennis_rationale(db, pick: dict) -> dict[str, list[str]]:
    """Tennis rationale from cached ELO + 7-day form."""
    out = {"evidence": [], "concerns": []}
    # Extract player name carrying the bet — first capitalized substring
    # before " Moneyline" / " Under " / " Over ".
    market = (pick.get("market") or "")
    player = (pick.get("player_name") or "").strip()
    if not player:
        m = re.match(r"^([A-Z][A-Za-z'\-\.]+(?:\s+[A-Z][A-Za-z'\-\.]+){0,2})", market)
        if m:
            player = m.group(1).strip()
    if not player:
        return out
    try:
        doc = await db.tennis_players.find_one(
            {"name_norm": player.lower()}
        ) or await db.tennis_players.find_one(
            {"name": {"$regex": f"^{re.escape(player)}$", "$options": "i"}}
        )
    except Exception:
        doc = None
    if not doc:
        return out
    elo = doc.get("elo_overall")
    if isinstance(elo, (int, float)):
        if elo >= 2000:
            out["evidence"].append(
                f"🎾 {player}: ELO {elo:.0f} — top-tier player"
            )
        elif elo >= 1800:
            out["evidence"].append(
                f"🎾 {player}: ELO {elo:.0f} — established tour player"
            )
        elif elo <= 1500:
            out["concerns"].append(
                f"🎾 {player}: ELO only {elo:.0f} — challenger/futures level"
            )
        else:
            out["evidence"].append(
                f"🎾 {player}: ELO {elo:.0f}"
            )
    form = doc.get("form") or {}
    wins = form.get("wins") or 0
    losses = form.get("losses") or 0
    n = wins + losses
    if n >= 5:
        wr = wins / n * 100
        if wr >= 70:
            out["evidence"].append(
                f"🔥 Hot form: {wins}-{losses} ({wr:.0f}% wins) recent"
            )
        elif wr <= 30:
            out["concerns"].append(
                f"❄️ Cold form: {wins}-{losses} ({wr:.0f}% wins) recent"
            )
    # Match minutes load over last 7 days — fatigue check
    last7 = doc.get("matches_7d") or []
    if isinstance(last7, list) and len(last7) >= 3:
        total_sets = sum((m.get("sets") or 0) for m in last7)
        if total_sets >= 10:
            out["concerns"].append(
                f"😮‍💨 {len(last7)} matches / {total_sets} sets in last 7 days"
                f" — fatigue risk"
            )
    return out


# ────────────────────────────────────────────────────────────────────
# Main dispatcher
# ────────────────────────────────────────────────────────────────────
async def build_sport_specific(
    db, pick: dict, sport: str, player_name: Optional[str] = None,
) -> dict[str, list[str]]:
    """Single entry point. Returns {evidence, concerns} bullets keyed
    to the sport+market signature. Empty lists if no builder matches."""
    out: dict[str, list[str]] = {"evidence": [], "concerns": []}
    sport = (sport or "").lower()
    market = pick.get("market") or ""

    # MLB
    if sport == "mlb":
        if player_name and is_mlb_pitcher_prop(market):
            r = await build_mlb_pitcher_rationale(pick, player_name)
            out["evidence"].extend(r.get("evidence") or [])
            out["concerns"].extend(r.get("concerns") or [])
        elif is_mlb_team_market(market, player_name or ""):
            r = await build_mlb_team_rationale(db, pick)
            out["evidence"].extend(r.get("evidence") or [])
            out["concerns"].extend(r.get("concerns") or [])

    # NFL (2026-07-01 season prep — player prop evidence from ESPN
    # season stats + last-5 game logs. Team markets fall through to
    # the universal edge summary.)
    elif sport == "nfl":
        try:
            from services import nfl_rationale
            if player_name and nfl_rationale.is_nfl_player_prop(market):
                r = await nfl_rationale.build_nfl_rationale(db, pick, player_name)
                out["evidence"].extend(r.get("evidence") or [])
                out["concerns"].extend(r.get("concerns") or [])
        except Exception as e:
            logger.debug("NFL rationale failed: %s", e)

    # NBA (2026-07-01 season prep — season-avg PPG/RPG/APG/PRA
    # comparisons + minutes context. Game-log persistence TBD.)
    elif sport == "nba":
        try:
            from services import nba_rationale
            if player_name and nba_rationale.is_nba_player_prop(market):
                r = await nba_rationale.build_nba_rationale(db, pick, player_name)
                out["evidence"].extend(r.get("evidence") or [])
                out["concerns"].extend(r.get("concerns") or [])
        except Exception as e:
            logger.debug("NBA rationale failed: %s", e)

    # Tennis
    elif is_tennis_market(sport):
        r = await build_tennis_rationale(db, pick)
        out["evidence"].extend(r.get("evidence") or [])
        out["concerns"].extend(r.get("concerns") or [])

    return out


# ────────────────────────────────────────────────────────────────────
# Sync wrapper used from pick_enrichment._build_rationale
# ────────────────────────────────────────────────────────────────────
def build_sport_specific_sync(
    pick: dict, sport: str, player_name: Optional[str] = None,
) -> dict[str, list[str]]:
    """Sync wrapper that runs the async dispatcher in a worker thread.
    Returns empty lists on any failure so the caller can degrade
    gracefully back to the universal model/edge bullets."""
    import asyncio
    import concurrent.futures
    try:
        from server import db
    except Exception:
        return {"evidence": [], "concerns": []}
    # We're called from sync `enrich_picks_with_active_registry` which
    # itself runs inside an async refresh handler. A nested loop would
    # raise — use a worker thread with its own asyncio.run() context.
    try:
        try:
            asyncio.get_running_loop()
            inside_loop = True
        except RuntimeError:
            inside_loop = False
        if inside_loop:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                future = ex.submit(
                    lambda: asyncio.run(build_sport_specific(db, pick, sport, player_name))
                )
                return future.result(timeout=20)
        return asyncio.run(build_sport_specific(db, pick, sport, player_name))
    except Exception as e:
        logger.debug("sport_rationale sync wrap failed: %s", e)
        return {"evidence": [], "concerns": []}
